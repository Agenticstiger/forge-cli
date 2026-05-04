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
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Set

from ..observability.tracing import traced_stage as _traced_stage
from ._common import (
    CLIError,
    build_provider,
    load_contract_with_overlay,
    read_json,
    resolve_provider_from_contract,
    write_json,
)
from ._logging import info

COMMAND = "diff"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Compare desired state vs current provider state (drift)",
        description="Detect configuration drift by comparing the desired state (from contract) with actual provider resources.",
    )
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--state", help="previous apply_report.json (optional)")
    p.add_argument("--env", help="environment overlay (dev, staging, prod)")
    p.add_argument("--out", default="runtime/diff.json", help="output file for drift report")
    p.add_argument(
        "--exit-on-drift", action="store_true", help="exit with code 1 if drift detected"
    )
    p.set_defaults(cmd=COMMAND, func=run)


@_traced_stage("diff")
def run(args, logger: logging.Logger) -> int:
    try:
        # Load contract and generate desired state
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)

        # Bug 5a: infer the provider from ``binding.platform`` when the
        # operator didn't pass ``--provider`` and ``FLUID_PROVIDER`` env
        # isn't set. Every other FLUID command auto-detects this way
        # (apply, plan, verify); ``diff`` was the odd one out — it
        # raised ``provider_not_specified`` and forced operators to
        # re-run with the env var. The inferred name is passed to
        # :func:`build_provider` which still honours explicit
        # ``--provider`` / ``FLUID_PROVIDER`` (either wins over the
        # contract inference, matching the existing precedence).
        provider_arg = getattr(args, "provider", None)
        if not provider_arg and not os.environ.get("FLUID_PROVIDER"):
            inferred_platform, _inferred_location = resolve_provider_from_contract(contract)
            if inferred_platform:
                info(
                    logger,
                    "diff_provider_inferred",
                    platform=inferred_platform,
                    source="contract.binding.platform",
                )
                provider_arg = inferred_platform

        provider = build_provider(
            provider_arg,
            getattr(args, "project", None),
            getattr(args, "region", None),
            logger,
        )

        info(logger, "diff_planning", contract_kind=contract.get("kind", "unknown"))
        desired_actions = provider.plan(contract)

        # Extract resource identifiers from desired state
        desired_resources = _extract_resource_ids(desired_actions)

        # Load previous state if provided
        actual_resources: Set[str] = set()
        has_baseline = False
        if args.state and Path(args.state).exists():
            info(logger, "diff_loading_state", state_file=args.state)
            state = read_json(args.state)
            actual_resources = _extract_resource_ids(state.get("results", []))
            has_baseline = True
        else:
            # Bug 5b: ``info(logger, message, **payload)`` — the second
            # positional param is named ``message``. Passing
            # ``message=...`` as a kwarg here collided with Python's
            # argument binding: ``TypeError: info() got multiple values
            # for argument 'message'``. Rename to ``detail`` (lands in
            # the JSON payload as a structured field, same semantics).
            #
            # Note: Most providers don't implement live-inventory yet.
            # Without ``--state``, ``actual_resources`` stays empty and
            # every desired resource shows up as "added" — which the
            # drift summary would otherwise hard-fail on under
            # ``--exit-on-drift``. That's wrong: the drift gate should
            # detect UNEXPECTED changes, not "we don't know the
            # baseline." The ``has_baseline`` flag below downgrades
            # the exit-on-drift check to a warning-only path in the
            # no-state case (see summary logic below).
            info(
                logger,
                "diff_no_state",
                detail=(
                    "No previous state file; treating this as a fresh "
                    "baseline — exit-on-drift will NOT fire without a "
                    "prior state to compare against. Pass --state "
                    "<path-to-prior-apply-report.json> to enable "
                    "drift-based gating."
                ),
            )

        # Compare and categorize changes
        added = desired_resources - actual_resources
        removed = actual_resources - desired_resources
        unchanged = desired_resources & actual_resources

        # Build diff report
        drift_report = {
            "timestamp": time.time(),
            "contract": args.contract,
            "env": getattr(args, "env", None),
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "unchanged": len(unchanged),
                "has_drift": len(added) > 0 or len(removed) > 0,
            },
            "changes": {
                "added": sorted(list(added)),
                "removed": sorted(list(removed)),
                "unchanged": sorted(list(unchanged)),
            },
            "desired_actions": desired_actions,
        }

        # Write report
        write_json(args.out, drift_report)

        # Log summary
        if drift_report["summary"]["has_drift"]:
            info(
                logger, "diff_drift_detected", added=len(added), removed=len(removed), out=args.out
            )
            # ``--exit-on-drift`` only fires when we had an actual
            # baseline to compare against. Without ``--state``, the
            # whole desired set counts as "added" — gating on that
            # would make the first-ever Jenkins build of a product
            # always fail at the drift stage. Closes the gap where
            # the Jenkins template defaulted DIFF_EXIT_ON_DRIFT=true
            # and every fresh pipeline run hit exit 1.
            if args.exit_on_drift and has_baseline:
                return 1
            if args.exit_on_drift and not has_baseline:
                info(
                    logger,
                    "diff_exit_on_drift_skipped",
                    detail=(
                        "--exit-on-drift requested but no --state "
                        "baseline was supplied; drift cannot be "
                        "cryptographically compared to a prior apply, "
                        "so the gate is DOWNGRADED to a warning. "
                        "Wire the last apply-report.json as --state "
                        "to re-enable hard-fail drift gating."
                    ),
                )
        else:
            info(logger, "diff_no_drift", resources=len(unchanged), out=args.out)

        return 0

    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "diff_failed", {"error": str(e)})


def _extract_resource_ids(actions: List[Dict[str, Any]]) -> Set[str]:
    """Extract unique resource identifiers from action list."""
    resources = set()
    for action in actions:
        # Generate resource ID from action properties
        op = action.get("op", "unknown")
        resource_type = action.get("resource_type", action.get("type", ""))
        resource_id = action.get("resource_id", action.get("id", action.get("name", "")))

        if resource_id:
            resources.add(f"{resource_type}:{resource_id}")
        elif op:
            # Fallback: use operation name if no specific ID
            resources.add(f"action:{op}")

    return resources
