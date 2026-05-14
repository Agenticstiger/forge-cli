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

from ..observability.tracing import traced_span as _traced_span
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
        help="Compare contract-vs-live (drift) or contract-vs-contract (version)",
        description=(
            "Two modes: (1) drift detection — compares the desired state from "
            "the contract against actual provider resources (default); "
            "(2) version diff — when --baseline is set, compares the positional "
            "contract (new) against the baseline contract (old) for breaking-"
            "change classification. The two modes are mutually exclusive."
        ),
    )
    p.add_argument("contract", help="contract.fluid.yaml (new version when --baseline is set)")
    p.add_argument("--state", help="previous apply_report.json (drift mode, optional)")
    p.add_argument("--env", help="environment overlay (dev, staging, prod) — drift mode only")
    p.add_argument("--out", default="runtime/diff.json", help="output file for the diff report")
    p.add_argument(
        "--exit-on-drift", action="store_true", help="exit with code 1 if drift detected"
    )

    # Version-diff mode flags. ``--baseline`` toggles the new mode; the other
    # two are no-ops without it.
    p.add_argument(
        "--baseline",
        metavar="OLD_CONTRACT",
        help=(
            "Path to a baseline (old) contract.fluid.yaml. When set, switches "
            "from drift mode to contract-vs-contract version diff and emits a "
            "breaking-change classification."
        ),
    )
    p.add_argument(
        "--fail-on-breaking",
        action="store_true",
        help=(
            "Version-diff mode only: exit with code 1 if any breaking change "
            "is detected. Use in CI to gate on contract version compatibility."
        ),
    )
    p.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="Version-diff mode only: stdout rendering format (default: text)",
    )
    p.set_defaults(cmd=COMMAND, func=run)


@_traced_stage("diff")
def run(args, logger: logging.Logger) -> int:
    try:
        # Version-diff mode (contract-vs-contract). When ``--baseline`` is
        # supplied we bypass provider lookup entirely — the comparison is
        # pure structural diff between two parsed contracts.
        baseline_path = getattr(args, "baseline", None)
        if baseline_path:
            return _run_version_diff(args, logger)

        # Drift mode (contract-vs-live-warehouse) — the original behaviour.
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


def _run_version_diff(args, logger: logging.Logger) -> int:
    """Contract-vs-contract version diff branch (``--baseline`` mode).

    Loads two contracts, runs the changelog engine, prints in the requested
    format, optionally writes a JSON envelope to ``--out``, and returns a
    non-zero exit code when ``--fail-on-breaking`` is set and any breaking
    change was detected.
    """
    from fluid_build.cli.console import cprint

    from ..api.changelog import compare_contracts, render_markdown, render_text

    if getattr(args, "env", None):
        # Environment overlays are a drift-mode concept (they shape the
        # desired state for live comparison). Combining --baseline + --env
        # would silently pick the overlay applied to "new" but not the
        # baseline, which is more confusing than helpful. Reject up front.
        raise CLIError(
            2,
            "diff_modes_mutually_exclusive",
            {
                "detail": (
                    "--baseline and --env are mutually exclusive: --baseline "
                    "selects contract-vs-contract version diff, --env selects "
                    "contract-vs-live drift. Pick one."
                ),
            },
        )
    if getattr(args, "state", None):
        raise CLIError(
            2,
            "diff_modes_mutually_exclusive",
            {
                "detail": (
                    "--baseline and --state are mutually exclusive: --state is "
                    "the prior apply_report.json (drift mode), --baseline is an "
                    "older contract (version mode)."
                ),
            },
        )

    baseline_path = args.baseline
    new_path = args.contract
    info(logger, "version_diff_loading", baseline=baseline_path, new=new_path)

    # Load both contracts the same way as the rest of the CLI does for
    # consistency (auto-bundle, alias normalization, etc.). ``env=None``
    # because environment overlays don't apply to a version compare.
    baseline = load_contract_with_overlay(baseline_path, None, logger)
    new = load_contract_with_overlay(new_path, None, logger)

    # Open a child span for the version-diff sub-mode so operators can
    # filter on ``fluid.diff.mode=version`` in OTel exporters. The outer
    # ``@traced_stage("diff")`` span stays generic; this attribute set
    # distinguishes the two modes inside it.
    with _traced_span(
        "diff.version",
        attributes={
            "fluid.diff.mode": "version",
            "fluid.diff.baseline_path": baseline_path,
            "fluid.diff.new_path": new_path,
            "fluid.diff.fail_on_breaking": bool(getattr(args, "fail_on_breaking", False)),
            "fluid.diff.format": getattr(args, "format", "text") or "text",
        },
    ) as span:
        report = compare_contracts(baseline, new)
        span.set_attribute("fluid.diff.breaking_count", len(report.breaking))
        span.set_attribute("fluid.diff.non_breaking_count", len(report.non_breaking))
        span.set_attribute("fluid.diff.info_count", len(report.info))

    # Render to stdout in the requested format.
    fmt = getattr(args, "format", "text") or "text"
    if fmt == "json":
        # The JSON envelope is also written to --out (below) for CI
        # artifact collection. Print to stdout here for piping.
        import json as _json

        cprint(_json.dumps(report.to_dict(), indent=2))
    elif fmt == "markdown":
        cprint(render_markdown(report))
    else:
        cprint(render_text(report))

    # Always write the structured envelope to --out so CI runners that don't
    # parse stdout still get a machine-readable artifact.
    from ._common import write_json

    write_json(args.out, report.to_dict())

    info(
        logger,
        "version_diff_done",
        breaking=len(report.breaking),
        non_breaking=len(report.non_breaking),
        info_count=len(report.info),
        out=args.out,
    )

    if getattr(args, "fail_on_breaking", False) and report.has_breaking:
        return 1
    return 0
