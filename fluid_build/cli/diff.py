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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

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
from ._logging import info, warn

if TYPE_CHECKING:
    from ._diff_live import LiveDriftReport

COMMAND = "diff"

#: ``--exit-on-drift`` exit codes. They follow diff(1) ("0 means no differences
#: were found, 1 means some differences were found, and 2 means trouble") and
#: ``fluid mission`` (1 red, 2 harness error): 0 no drift, 1 drift, 2 a live
#: target could not be inspected. OpenTofu's ``-detailed-exitcode`` spells
#: drift 2 and errors 1; this command keeps the 1 that ``--state`` drift has
#: always returned, so one flag means one thing whichever baseline it compared
#: against, and an argparse usage error (also 2) never reads as drift.
EXIT_DRIFT = 1
EXIT_INSPECTION_FAILED = 2


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Compare contract-vs-live (drift) or contract-vs-contract (version)",
        description=(
            "Two modes: (1) drift detection — compares the desired state from "
            "the contract against actual provider resources (default). With "
            "--state it compares against that prior apply report; without it, "
            "it reads each expose's live target (local file, Glue table, "
            "BigQuery table) and compares its columns with the contract; "
            "(2) version diff — when --baseline is set, compares the positional "
            "contract (new) against the baseline contract (old) for breaking-"
            "change classification. The two modes are mutually exclusive."
        ),
    )
    p.add_argument("contract", help="contract.fluid.yaml (new version when --baseline is set)")
    p.add_argument(
        "--state",
        help=(
            "previous apply_report.json (drift mode, optional). A path that does "
            "not exist falls back to the live comparison"
        ),
    )
    p.add_argument("--env", help="environment overlay (dev, staging, prod) — drift mode only")
    p.add_argument("--out", default="runtime/diff.json", help="output file for the diff report")
    p.add_argument(
        "--exit-on-drift",
        action="store_true",
        help=(
            "exit 1 if drift is detected (against --state when given, else "
            "against the live targets); exit 2 if a live target could not be "
            "inspected"
        ),
    )
    # Its own ``dest``: a subcommand flag sharing ``dest="region"`` with the
    # global ``--region`` overwrites the global value with its default, so
    # ``fluid --region X diff`` and ``FLUID_REGION`` were dropped.
    p.add_argument(
        "--region",
        dest="diff_region",
        metavar="REGION",
        help=(
            "override region/location for the provider plan (default: the "
            "binding's, then the global --region / FLUID_REGION); the live check "
            "reads each target where apply puts it"
        ),
    )
    p.add_argument(
        "--last-applied",
        metavar="PLAN",
        help=(
            "drift mode: the plan.json the last successful apply ran (or that "
            "contract). A column the contract changed since then, where the "
            "target still matches it, is reported as pending, not drift. A path "
            "that does not exist is ignored (first run)"
        ),
    )
    p.add_argument(
        "--no-live",
        dest="live",
        action="store_false",
        help=(
            "drift mode: do not read the live targets. Without --state there is "
            "then no baseline, and --exit-on-drift is downgraded to a warning"
        ),
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
        # F1 / F6: validate every operator-supplied path argument
        # (traversal, forbidden system paths, symlink) before it reaches
        # ``load_contract_with_overlay`` / ``read_json`` / ``write_json``.
        # Covers both diff modes: positional ``contract`` (always),
        # ``--baseline`` (version mode), ``--state`` (drift mode), and
        # the ``--out`` write target.
        from fluid_build.cli.security import validate_cli_path

        args.contract = str(validate_cli_path(args.contract, mode="read", file_type="contract"))
        if getattr(args, "baseline", None):
            args.baseline = str(
                validate_cli_path(args.baseline, mode="read", file_type="baseline contract")
            )
        # ``must_exist=False``: a missing --state (the first run, before any
        # apply report exists) falls back to the live comparison below
        # (``diff_state_not_found``) instead of failing with exit 1, which
        # would read as drift. Traversal and symlink checks still apply.
        if getattr(args, "state", None):
            args.state = str(
                validate_cli_path(args.state, mode="read", must_exist=False, file_type="state file")
            )
        if getattr(args, "last_applied", None):
            args.last_applied = str(
                validate_cli_path(
                    args.last_applied, mode="read", must_exist=False, file_type="last-applied plan"
                )
            )
        if getattr(args, "out", None):
            args.out = str(
                validate_cli_path(args.out, mode="write", must_exist=False, file_type="output")
            )

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
        provider_arg = getattr(args, "provider", None) or os.environ.get("FLUID_PROVIDER")
        project_arg = getattr(args, "project", None)
        binding_region = None
        inferred_platform, inferred_location = resolve_provider_from_contract(contract)
        if not provider_arg and inferred_platform:
            info(
                logger,
                "diff_provider_inferred",
                platform=inferred_platform,
                source="contract.binding.platform",
            )
            provider_arg = inferred_platform
        if inferred_platform and _same_provider(provider_arg, inferred_platform):
            # The binding's project and region, as ``fluid plan`` takes them,
            # whether the provider was inferred or named: ``--provider aws``
            # does not make an EU binding's region any less where the table
            # is. Without this the AWS provider was built in the global
            # default, a GCP region, and a contract whose sovereignty block
            # denies it failed with "data residency violation" before
            # anything was compared.
            project_arg = project_arg or inferred_location.get("project")
            binding_region = inferred_location.get("region")
        region_arg = getattr(args, "diff_region", None) or binding_region or _global_region(args)

        provider = build_provider(provider_arg, project_arg, region_arg, logger)

        info(logger, "diff_planning", contract_kind=contract.get("kind", "unknown"))
        desired_actions = provider.plan(contract)

        # Extract resource identifiers from desired state
        desired_resources = _extract_resource_ids(desired_actions)

        # Load previous state if provided
        actual_resources: Set[str] = set()
        has_baseline = False
        live_report = None
        if args.state and Path(args.state).exists():
            info(logger, "diff_loading_state", state_file=args.state)
            state = read_json(args.state)
            actual_resources = _extract_resource_ids(state.get("results", []))
            has_baseline = True
        elif getattr(args, "live", True):
            # No baseline file: read each expose's target as it exists now
            # and compare it with the contract (see ``_diff_live``).
            if args.state:
                info(logger, "diff_state_not_found", state_file=args.state, fallback="live")
            live_report = _compare_live(contract, _load_last_applied(args, logger), args, logger)
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
                    "No previous state file and --no-live; treating this "
                    "as a fresh baseline — exit-on-drift will NOT fire "
                    "without something to compare against. Pass --state "
                    "<path-to-prior-apply-report.json>, or drop --no-live "
                    "to compare against the live targets."
                ),
            )

        # Compare and categorize changes
        added = desired_resources - actual_resources
        removed = actual_resources - desired_resources
        unchanged = desired_resources & actual_resources

        # Build diff report. ``added`` / ``removed`` / ``unchanged`` keep
        # their meaning (plan resources against the --state baseline);
        # ``has_drift`` comes from whichever comparison ran, named by
        # ``drift_source``.
        if live_report is not None:
            drift_source = "live"
            has_drift = live_report.has_drift
        else:
            drift_source = "state" if has_baseline else "none"
            has_drift = len(added) > 0 or len(removed) > 0
        drift_report = {
            "timestamp": time.time(),
            "contract": args.contract,
            "env": getattr(args, "env", None),
            "drift_source": drift_source,
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "unchanged": len(unchanged),
                "has_drift": has_drift,
            },
            "changes": {
                "added": sorted(list(added)),
                "removed": sorted(list(removed)),
                "unchanged": sorted(list(unchanged)),
            },
            "desired_actions": desired_actions,
        }
        if live_report is not None:
            drift_report["live"] = live_report.to_dict()

        # Write report
        write_json(args.out, drift_report)

        if live_report is not None:
            return _finish_live(live_report, args, logger)

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
                return EXIT_DRIFT
            if args.exit_on_drift and not has_baseline:
                info(
                    logger,
                    "diff_exit_on_drift_skipped",
                    detail=(
                        "--exit-on-drift requested with --no-live and no "
                        "--state baseline; there is nothing to compare "
                        "against, so the gate is DOWNGRADED to a warning. "
                        "Drop --no-live, or wire the last apply-report.json "
                        "as --state, to re-enable hard-fail drift gating."
                    ),
                )
        else:
            info(logger, "diff_no_drift", resources=len(unchanged), out=args.out)

        return 0

    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "diff_failed", {"error": str(e)})


def _same_provider(provider: Optional[str], platform: str) -> bool:
    """Do a provider name and a binding platform name the same cloud?"""
    from fluid_build.iac.provider_match import canonical_cloud

    def _norm(token: Optional[str]) -> str:
        raw = str(token or "").strip().lower().replace("-", "_")
        return canonical_cloud(raw) or raw

    return bool(provider) and _norm(provider) == _norm(platform)


def _global_region(args: argparse.Namespace) -> Optional[str]:
    """The global ``--region`` / ``FLUID_REGION``, or ``None`` for its built-in default.

    The built-in default is a GCP region, so passing it to any other
    provider is what failed EU-only AWS contracts on residency. A value the
    operator set (``FLUID_REGION``, or a ``--region`` that differs from the
    default) is kept, as it is on ``main``; otherwise the provider resolves
    its own (the AWS provider reads the AWS environment).
    """
    from . import DEFAULT_REGION

    region = getattr(args, "region", None)
    if not region:
        return None
    if os.environ.get("FLUID_REGION") or region != DEFAULT_REGION:
        return str(region)
    return None


def _load_last_applied(
    args: argparse.Namespace, logger: logging.Logger
) -> Optional[Dict[str, Any]]:
    """The contract ``--last-applied`` names, or ``None`` when there is none.

    A path that does not exist is the first run, before any apply: logged
    and ignored. A file that exists but holds no contract is an error, never
    a silent two-way comparison, because the gate would then read a pending
    change as drift, or the other way round.
    """
    path = getattr(args, "last_applied", None)
    if not path:
        return None
    if not Path(path).exists():
        info(logger, "diff_last_applied_not_found", last_applied=path, baseline="contract")
        return None
    from ._diff_live import last_applied_contract

    try:
        import yaml

        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported as the typed error below
        document = None
        reason = f"{type(exc).__name__}: not JSON or YAML"
    else:
        reason = "no 'contract' with 'exposes' in it"
    applied = last_applied_contract(document)
    if applied is None:
        raise CLIError(
            EXIT_INSPECTION_FAILED,
            "diff_last_applied_invalid",
            {
                "last_applied": path,
                "detail": (
                    f"--last-applied must be the plan.json an apply ran, or a contract ({reason})"
                ),
            },
        )
    info(
        logger,
        "diff_last_applied_loaded",
        last_applied=path,
        exposes=len(applied.get("exposes") or []),
    )
    return dict(applied)


def _compare_live(
    contract: Dict[str, Any],
    last_applied: Optional[Dict[str, Any]],
    args: argparse.Namespace,
    logger: logging.Logger,
) -> "LiveDriftReport":
    """Read every expose's live target and compare it with the contract."""
    from fluid_build._contract_loader import source_contract_dir

    from ._diff_live import compare_live

    exposes = contract.get("exposes") or []
    info(logger, "diff_live_comparing", exposes=len(exposes))
    # Relative local paths are anchored at the SOURCE contract's directory,
    # where the build writes them: the contract itself, or the one a bundle's
    # MANIFEST records. The bundle's own directory (runtime/) would read an
    # absent file, "to be created", and let a drifted target pass the gate.
    anchor_dir = source_contract_dir(args.contract, logger)
    with _traced_span("diff.live", attributes={"fluid.diff.mode": "live"}) as span:
        report = compare_live(
            contract,
            anchor_dir,
            last_applied=last_applied,
            default_project=getattr(args, "project", None),
        )
        for status, count in report.counts().items():
            span.set_attribute(f"fluid.diff.live.{status}", count)
    return report


def _finish_live(
    report: "LiveDriftReport", args: argparse.Namespace, logger: logging.Logger
) -> int:
    """Print and log the live comparison, then decide the exit code.

    Without ``--exit-on-drift`` the comparison is informational and exits 0.
    With it: a target that could not be inspected exits
    :data:`EXIT_INSPECTION_FAILED` (``diff_live_inspection_failed``), checked
    before drift the way OpenTofu returns a failed operation's status before
    it looks at ``-detailed-exitcode``, because a target that could not be
    read might have drifted too; drift exits :data:`EXIT_DRIFT`
    (``diff_live_drift_detected``). A target that does not exist yet ("to be
    created"), one that differs only as its ``schemaPolicy`` allows
    (``evolved``), and one the contract changed since the last apply
    (``pending``) are not drift.
    """
    from fluid_build.cli.console import cprint

    from . import _diff_live as live

    # Expose ids and paths come from the contract, column names and inspector
    # errors from the target, so they are printed as plain text (never Rich
    # markup) with control characters replaced.
    def _say(text: str) -> None:
        cprint(live.printable(text), markup=False, soft_wrap=True)

    _say(f"Live drift check: {len(report.exposes)} expose(s)")
    labels = {
        live.ABSENT: "absent (to be created)",
        live.PENDING: "pending (apply will change it)",
        live.EVOLVED: "evolved (allowed by schemaPolicy)",
    }
    for result in report.exposes:
        status = labels.get(result.status, result.status)
        line = f"  {result.expose_id} [{result.platform or 'unset'}] {status}"
        if result.target:
            line += f"  {result.target}"
        _say(line)
        for col in result.columns:
            _say(f"      - {col.human()}")
        if result.detail and result.status not in (live.MATCH, live.ABSENT):
            _say(f"      {result.detail}")

    for result in report.with_status(live.ABSENT):
        info(logger, "diff_live_target_absent", expose=result.expose_id, target=result.target)
    for result in report.with_status(live.PENDING):
        info(
            logger,
            "diff_live_changes_pending",
            expose=result.expose_id,
            columns=[c.column for c in result.columns if c.classification == live.COLUMN_PENDING],
        )
    for result in report.exposes:
        allowed = [c for c in result.columns if c.classification == live.COLUMN_ALLOWED]
        if allowed:
            info(
                logger,
                "diff_live_evolved_by_policy",
                expose=result.expose_id,
                schema_policy=result.schema_policy,
                columns=[f"{c.column}:{c.event}->{c.action}" for c in allowed],
            )
    for result in report.with_status(live.NOT_CHECKED):
        warn(
            logger,
            "diff_live_not_checked",
            expose=result.expose_id,
            platform=result.platform,
            detail=result.detail,
        )
    errors = report.with_status(live.ERROR)
    for result in errors:
        warn(
            logger,
            "diff_live_inspection_failed",
            expose=result.expose_id,
            target=result.target,
            detail=result.detail,
        )
    drifted = report.with_status(live.DRIFT)
    if drifted:
        warn(
            logger,
            "diff_live_drift_detected",
            exposes=[r.expose_id for r in drifted],
            columns=sum(
                1 for r in drifted for c in r.columns if c.classification == live.COLUMN_DRIFT
            ),
            out=args.out,
        )
    elif not errors:
        info(
            logger,
            "diff_no_drift",
            drift_source="live",
            compared=report.compared,
            out=args.out,
        )

    if not args.exit_on_drift:
        return 0
    if errors:
        raise CLIError(
            EXIT_INSPECTION_FAILED,
            "diff_live_inspection_failed",
            {
                "exposes": [r.expose_id for r in errors],
                "detail": (
                    "--exit-on-drift could not read every live target, so it "
                    "cannot say there is no drift; see the report's live.exposes"
                ),
                "out": args.out,
            },
        )
    if drifted:
        return EXIT_DRIFT
    if report.compared == 0:
        warn(
            logger,
            "diff_exit_on_drift_skipped",
            detail=(
                "--exit-on-drift requested but no expose has a live target "
                "forge can inspect, so nothing was compared and the gate is "
                "DOWNGRADED to a warning."
            ),
        )
    return 0


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
