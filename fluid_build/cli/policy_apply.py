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

from ._common import CLIError, build_provider
from ._logging import info

COMMAND = "policy-apply"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Apply compiled IAM bindings")
    p.add_argument("bindings", help="runtime/policy/bindings.json")
    p.add_argument(
        "--mode", choices=["check", "enforce"], default="check", help="dry-run or enforce"
    )
    p.set_defaults(cmd=COMMAND, func=run)


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


def run(args, logger: logging.Logger) -> int:
    try:
        with open(args.bindings, encoding="utf-8") as f:
            data = json.load(f)

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
        return 0 if res.get("status") in ("ok", "noop") else 1
    except CLIError:
        raise
    except Exception as e:
        logger.error(f"Policy apply error: {str(e)}")
        logger.error(traceback.format_exc())
        raise CLIError(1, "policy_apply_failed", {"error": str(e)})
