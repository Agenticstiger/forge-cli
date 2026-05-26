# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import json
import logging
import os
import traceback

from ..observability.tracing import traced_stage as _traced_stage
from ._common import CLIError, build_provider
from ._logging import info

COMMAND = "policy-apply"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate a pre-created parser with the policy-apply args.

    Shared between the legacy ``fluid policy-apply`` top-level command
    and the new ``fluid policy apply`` subcommand so the argument
    surface stays single-sourced.
    """
    parser.add_argument("bindings", help="runtime/policy/bindings.json")
    parser.add_argument(
        "--mode", choices=["check", "enforce"], default="check", help="dry-run or enforce"
    )
    parser.set_defaults(cmd=COMMAND, func=run)


def register(subparsers: argparse._SubParsersAction):
    """Register the legacy top-level ``fluid policy-apply`` command.

    New code should prefer ``fluid policy apply``. This surface is
    kept as a deprecation alias for one release window so existing
    CI templates that call ``fluid policy-apply`` continue to work.
    """
    p = subparsers.add_parser(COMMAND, help="Apply compiled IAM bindings")
    _add_arguments(p)


def _resolve_from_bindings(data: dict) -> tuple[str, str]:
    """Read provider and project from bindings.json metadata.

    The policy compiler embeds 'provider' on each binding and 'project'
    where applicable — both derived from the contract's binding.platform
    and binding.location.  This means policy-apply never needs --provider
    or --project flags.
    """
    provider = ""
    project = ""
    for b in data.get("bindings", []):
        if not provider:
            provider = b.get("provider", "")
        if not project:
            project = b.get("project", "")
        if provider and project:
            break
    return provider, project


@_traced_stage("policy_apply")
def run(args, logger: logging.Logger) -> int:
    try:
        # F1 / F6: validate the operator-supplied bindings path
        # (traversal, forbidden system paths, symlink) before the raw
        # ``open()`` below.
        from fluid_build.cli.security import validate_cli_path

        bindings_path = validate_cli_path(args.bindings, mode="read", file_type="policy bindings")
        args.bindings = str(bindings_path)

        with open(bindings_path, encoding="utf-8") as f:
            data = json.load(f)

        # An empty bindings file is a legitimate no-op, not an error: a
        # contract with no ``accessPolicy`` grants — every raw bronze / SDP
        # acquisition product — compiles to zero bindings. There is nothing
        # to apply and no provider to resolve, so succeed cleanly rather than
        # failing ``provider_not_specified`` in ``build_provider`` below.
        if not (data.get("bindings") or []):
            info(logger, "policy_apply_result", status="noop", note="no bindings to apply")
            return 0

        # Provider and project come from the bindings file (set by policy-compile
        # from the contract schema).  CLI flags and env vars are overrides only.
        bindings_provider, bindings_project = _resolve_from_bindings(data)

        provider_name = (
            getattr(args, "provider", None)
            or bindings_provider
            or os.getenv("FLUID_PROVIDER")
            or ""
        )
        project_name = getattr(args, "project", None) or bindings_project or None

        if provider_name:
            source = "contract" if provider_name == bindings_provider else "flag/env"
            logger.info(f"Provider: {provider_name} (from {source})")

        provider = build_provider(
            provider_name or None, project_name, getattr(args, "region", None), logger
        )

        if hasattr(provider, "apply_policy"):
            res = provider.apply_policy(data, mode=args.mode)
        else:
            res = {"status": "noop", "note": "provider has no policy applier"}
        info(logger, "policy_apply_result", **res)

        # ── Acquisition pattern: register retention + alert + cost policies ─
        # Bronze acquisition contracts emit retention sweeper schedules,
        # alerter-channel config, PII-masking actions for classified
        # columns, and cost-budget guards. These land under
        # ``.fluid/policies/<contract-id>/`` and are picked up at next
        # apply by the acquisition runtime.
        try:
            contract = data.get("contract") or {}
            if contract:
                from pathlib import Path as _Path

                from fluid_build.cli._acquisition_stage_ext import (
                    is_acquisition_contract,
                    policy_apply_acquisition,
                )

                if is_acquisition_contract(contract):
                    acq_results = policy_apply_acquisition(contract, _Path.cwd())
                    for r in acq_results:
                        info(
                            logger,
                            "policy_apply_acquisition",
                            product_id=r.product_id,
                            build_id=r.build_id,
                            actions_applied=r.actions_applied,
                            skipped=r.skipped,
                        )
        except Exception as acq_exc:  # noqa: BLE001
            logger.warning(f"acquisition policy registration skipped: {acq_exc}")

        return 0 if res.get("status") in ("ok", "noop") else 1
    except CLIError:
        raise
    except Exception as e:
        logger.error(f"Policy apply error: {str(e)}")
        logger.error(traceback.format_exc())
        raise CLIError(1, "policy_apply_failed", {"error": str(e)})
