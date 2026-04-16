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
import time
from pathlib import Path
from typing import Any, Dict, List, Set

from ._common import CLIError, build_provider, load_contract_with_overlay, read_json, write_json
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


def run(args, logger: logging.Logger) -> int:
    try:
        # Load contract and generate desired state
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        provider = build_provider(
            getattr(args, "provider", None),
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
        if args.state and Path(args.state).exists():
            info(logger, "diff_loading_state", state_file=args.state)
            state = read_json(args.state)
            actual_resources = _extract_resource_ids(state.get("results", []))
        else:
            # Note: Most providers don't implement inventory yet, so we'll simulate
            info(
                logger,
                "diff_no_state",
                message="No previous state file; showing planned changes only",
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
            if args.exit_on_drift:
                return 1
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
