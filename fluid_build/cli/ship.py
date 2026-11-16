# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship`` — the canonical 11-stage pipeline runner.

One runner, used identically on a laptop and in CI::

    bundle → validate → generate artifacts → validate artifacts → diff →
    plan → apply → policy apply → verify → [publish] → [schedule sync]

Apply runs the plan stage 6 wrote against the bundle stage 1 froze, never a
re-plan from the contract, so what was reviewed is what is applied. Each stage
is its own ``fluid`` process (``python -m fluid_build.cli <stage> ...``), so
flag handling is exactly the standalone command's and a failing stage leaves
the same exit code and output it would on its own. Output streams through
unbuffered; ship adds only CI-native group markers around it.

Stage semantics live in :mod:`fluid_build.cli._ship_stages` and mirror the
shell bodies generated CI files ran before (a test runs both and compares).
``fluid ship --explain`` prints the resolved plan and runs nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..observability.tracing import traced_stage
from . import _ci_output
from . import _ship_report as report
from . import _ship_stages as st
from ._common import CLIError, auto_find_contract

COMMAND = "ship"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Run the 11-stage pipeline: bundle → validate → … → apply → verify",
        description=(
            "Runs the canonical 11-stage pipeline, the same way on a laptop and in CI:\n\n"
            "   1 bundle     2 validate        3 generate artifacts   4 validate artifacts\n"
            "   5 diff       6 plan            7 apply                8 policy apply\n"
            "   9 verify    10 publish (--publish)   11 schedule sync (--schedule-sync)\n\n"
            "Apply runs the reviewed plan (runtime/plan.json) against the frozen bundle "
            "(runtime/bundle.tgz). Stops at the first failing stage and returns its exit "
            "code. Every run writes runtime/ship-report.json and runtime/ship-junit.xml. "
            "Precedence: flag > environment variable > default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Stages (name or number): bundle, validate, generate-artifacts (artifacts), "
            "validate-artifacts, diff, plan, apply, policy-apply (policy), verify, publish, "
            "schedule-sync.\n\n"
            "Environment variables read when the flag is absent: FLUID_ENV, APPLY_MODE, "
            "APPLY_BUILD_ID, ALLOW_DATA_LOSS, NO_VERIFY_DIGEST, PUBLISH_TARGETS, SCHEDULER, "
            "SCHEDULER_DESTINATION, SCHEDULER_ENVIRONMENT_NAME, SCHEDULER_LOCATION, "
            "SCHEDULER_WORKSPACE, SCHEDULE_SYNC_DRY_RUN.\n\n"
            "Examples:\n"
            "  fluid ship --explain                      # show the plan, run nothing\n"
            "  fluid ship contract.fluid.yaml --env dev --yes\n"
            "  fluid ship --until plan                   # stages 1-6, then review the plan\n"
            "  fluid ship --stage apply,verify --yes     # apply the reviewed plan\n"
            "  fluid ship --apply-mode dry-run --yes     # nothing is written\n"
        ),
    )
    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help="Path to contract.fluid.yaml (auto-found in the current directory when omitted).",
    )
    p.add_argument("--env", default=None, help="Environment overlay (default: $FLUID_ENV, dev).")
    p.add_argument(
        "-y", "--yes", action="store_true", help="Pass --yes to apply (non-interactive / CI)."
    )
    p.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass --strict to validate and validate-artifacts (default; --no-strict drops it).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Same as --apply-mode dry-run: plan and apply write nothing.",
    )
    p.add_argument(
        "--skip-bundle",
        action="store_true",
        help="Reuse an existing runtime/bundle.tgz (bundles anyway, with a warning, if absent).",
    )
    p.add_argument(
        "--skip-plan",
        action="store_true",
        help="Removed: apply runs only the reviewed plan. Use --stage / --until.",
    )

    g = p.add_argument_group("apply")
    g.add_argument(
        "--apply-mode",
        choices=st.APPLY_MODES,
        default=None,
        help="Mode for plan and apply (default: $APPLY_MODE, else amend).",
    )
    g.add_argument(
        "--build-id",
        default=None,
        help=(
            "Build id for amend-and-build / replace-and-build (default: $APPLY_BUILD_ID, else "
            "the contract's only build). An empty value means no build id."
        ),
    )
    g.add_argument(
        "--allow-data-loss", action="store_true", help="Pass --allow-data-loss to apply."
    )
    g.add_argument(
        "--no-verify-digest",
        action="store_true",
        help="Pass --no-verify-plan-binding --no-verify-federation to apply.",
    )
    g.add_argument(
        "--no-verify-strict", action="store_true", help="Drop --strict from the verify stage."
    )
    g.add_argument(
        "--no-generate-artifacts", action="store_true", help="Skip stage 3 (generate artifacts)."
    )

    g = p.add_argument_group("publish and schedule (opt-in stages 10 and 11)")
    g.add_argument("--publish", action="store_true", help="Run stage 10 (publish).")
    g.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="CATALOG",
        help="Catalog to publish to; repeatable (default: $PUBLISH_TARGETS, datamesh-manager).",
    )
    g.add_argument("--schedule-sync", action="store_true", help="Run stage 11 (schedule sync).")
    g.add_argument("--scheduler", default=None, help="Scheduler for stage 11 ($SCHEDULER).")
    g.add_argument("--scheduler-destination", default=None, help="schedule-sync --destination.")
    g.add_argument(
        "--scheduler-environment-name", default=None, help="schedule-sync --environment-name."
    )
    g.add_argument("--scheduler-location", default=None, help="schedule-sync --location.")
    g.add_argument("--scheduler-workspace", default=None, help="schedule-sync --workspace.")
    g.add_argument(
        "--schedule-sync-dry-run", action="store_true", help="Pass --dry-run to schedule-sync."
    )

    g = p.add_argument_group("selecting stages")
    g.add_argument("--until", default=None, metavar="STAGE", help="Stop after STAGE.")
    g.add_argument(
        "--stage",
        default=None,
        metavar="SPEC",
        help="Run only these stages: 7, 6-9, plan,apply (names and numbers both work).",
    )
    g.add_argument(
        "--skip-stage",
        action="append",
        default=None,
        metavar="STAGE",
        help="Skip a stage; repeatable.",
    )
    g.add_argument(
        "--explain",
        action="store_true",
        help="Print the resolved stage plan (argv and skip reasons), run nothing, exit 0.",
    )

    g = p.add_argument_group("output")
    g.add_argument(
        "--ci-output",
        choices=_ci_output.MODES,
        default="auto",
        help="Log grouping for the CI system (default: detect from the environment).",
    )
    g.add_argument(
        "--report-dir",
        default="runtime",
        help="Where ship-report.json and ship-junit.xml go (default: runtime).",
    )
    p.set_defaults(cmd=COMMAND, func=run)


@traced_stage("ship")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Run the selected pipeline stages in order; stop at the first failure."""
    try:
        return _run(args, logger)
    except st.ShipUsageError as exc:
        logger.error("fluid ship: %s", _ci_output.oneline(str(exc)))
        return 2


def _run(args: argparse.Namespace, logger: logging.Logger) -> int:
    opts, selected, not_selected = _prepare(args)
    if args.explain:
        return _explain(opts, selected, not_selected)
    st.check_preconditions(selected, opts)

    run_id = _run_id()
    ctx = _Context(
        opts=opts,
        ci=_ci_output.CiOutput(_ci_output.detect(args.ci_output)),
        fluid_bin=_resolve_fluid_bin(),
        child_env=_child_env(run_id),
        logger=logger,
    )
    records = {
        num: report.StageRecord(num, st.STAGE_NAMES[num], reason=not_selected.get(num))
        for num in st.STAGE_NUMBERS
    }
    exit_code = 0
    rows: List[Tuple[int, str, str, float]] = []
    for num in selected:
        rec = records[num]
        _run_one(num, rec, ctx)
        skipped = rec.status == report.SKIPPED
        rows.append((num, rec.name, rec.status, 0.0 if skipped else rec.duration_seconds))
        if rec.status == report.FAILED:
            exit_code = int(rec.exit_code or 1)
            _report_abort(num, rec, ctx)
            break
    return _finish(args.report_dir, run_id, records, rows, exit_code, ctx)


@dataclass
class _Context:
    """What every stage of one run shares."""

    opts: st.ShipOptions
    ci: _ci_output.CiOutput
    fluid_bin: List[str]
    child_env: Dict[str, str]
    logger: logging.Logger


def _prepare(args: argparse.Namespace) -> Tuple[st.ShipOptions, List[int], Dict[int, str]]:
    """Every refusal ship makes before a stage runs (exit 2, no report)."""
    if getattr(args, "skip_plan", False):
        raise st.ShipUsageError(
            "--skip-plan was removed: apply runs only the reviewed plan (runtime/plan.json). "
            "To run part of the pipeline use --stage (e.g. --stage apply,verify) or --until."
        )
    if not auto_find_contract(args):
        raise CLIError(
            1,
            "contract_required",
            {
                "message": (
                    "No contract path supplied and no contract.fluid.yaml in the current directory."
                )
            },
        )
    # Ship runs from the project root: the contract is a cwd-relative POSIX path
    # for every stage (``generate artifacts --contract-path`` refuses any other).
    args.contract = st.project_relative_contract(args.contract)
    opts = st.resolve_options(args, args.contract, os.environ)
    selected, not_selected = st.select_stages(
        publish=bool(args.publish),
        schedule_sync=bool(args.schedule_sync),
        stage_spec=args.stage,
        until=args.until,
        skip_stages=args.skip_stage or [],
    )
    if not selected:
        raise st.ShipUsageError("no stage left to run (check --stage, --until and --skip-stage)")
    if 10 in selected and opts.mode != "dry-run":
        st.check_targets(opts.targets)
    return opts, selected, not_selected


def _run_one(num: int, rec: report.StageRecord, ctx: _Context) -> None:
    """Run stage ``num`` into ``rec``: passed, failed (with its exit code) or skipped."""
    started = time.monotonic()
    ctx.ci.stage_start(num, rec.name)
    rc = _attempt(num, rec, ctx)
    if rc is not None:  # a skipped stage keeps no duration and no exit code
        rec.duration_seconds = time.monotonic() - started
        rec.exit_code = rc
        rec.status = report.PASSED if rc == 0 else report.FAILED
    ctx.ci.stage_end(num, rec.name)


def _attempt(num: int, rec: report.StageRecord, ctx: _Context) -> Optional[int]:
    """Stage ``num``'s exit code, or ``None`` when it is skipped (``rec`` says why)."""
    try:
        action = st.build_stage(num, ctx.opts, st.Workspace())
    except st.ShipUsageError as exc:
        ctx.ci.info(f"stage {num} {rec.name}: {exc}")
        return 2
    for note in action.notes:
        ctx.ci.info(f"stage {num} {rec.name}: {note}")
    if action.argv is None:
        rec.status, rec.reason = report.SKIPPED, action.skip_reason
        ctx.ci.info(f"stage {num} {rec.name}: skipped — {rec.reason}")
        return None
    rec.argv = _safe_argv(action.argv)
    missing = st.missing_input(num, st.Workspace())
    if missing:
        ctx.ci.info(missing)
        return 2
    if num in (1, 10):
        Path("runtime").mkdir(parents=True, exist_ok=True)
    try:
        return _run_stage(
            ctx.fluid_bin, action.argv[0], action.argv[1:], ctx.logger, env=ctx.child_env, ci=ctx.ci
        )
    except KeyboardInterrupt:
        rec.reason = "interrupted"
        ctx.ci.info(f"stage {num} {rec.name}: interrupted")
        return 130


def _report_abort(num: int, rec: report.StageRecord, ctx: _Context) -> None:
    ctx.ci.failure(num, rec.name, int(rec.exit_code or 1))
    ctx.logger.error(
        "Ship aborted at stage %d %s (exit %d%s). Re-run it alone: fluid ship --stage %d",
        num,
        rec.name,
        int(rec.exit_code or 1),
        ", interrupted" if rec.reason == "interrupted" else "",
        num,
    )


def _finish(
    report_dir: str,
    run_id: str,
    records: Dict[int, report.StageRecord],
    rows: List[Tuple[int, str, str, float]],
    exit_code: int,
    ctx: _Context,
) -> int:
    """Reports, the CI summary and the closing line; returns the run's exit code."""
    ordered = [records[n] for n in st.STAGE_NUMBERS]
    for rec in ordered:
        if rec.status == report.NOT_RUN and rec.reason is None:
            rec.reason = "an earlier stage failed"
    if any(r.reason == "interrupted" for r in ordered):
        exit_code = 130
    _write_reports(report_dir, run_id, ctx.opts, exit_code, ordered, ctx.logger)
    ctx.ci.summary(rows)
    if exit_code == 0:
        passed = sum(1 for r in ordered if r.status == report.PASSED)
        skipped = sum(1 for r in ordered if r.status == report.SKIPPED)
        ctx.ci.line(f"Ship complete: {passed} stage(s) passed, {skipped} skipped")
    return exit_code


def _explain(opts: st.ShipOptions, selected: List[int], not_selected: Dict[int, str]) -> int:
    ws = st.Workspace()
    _out(
        f"fluid ship: {len(selected)} stage(s) selected "
        f"(env={opts.env}, mode={opts.mode}, contract={opts.contract})"
    )
    problem = _precondition_problem(selected, opts)
    for num in st.STAGE_NUMBERS:
        refused = _explain_stage(num, opts, ws, selected, not_selected)
        problem = problem or refused
    if problem:
        _out(f"warning: a real run would stop: {problem}")
    return 0


def _precondition_problem(selected: List[int], opts: st.ShipOptions) -> Optional[str]:
    try:
        st.check_preconditions(selected, opts)
    except st.ShipUsageError as exc:
        return str(exc)
    return None


def _explain_stage(
    num: int,
    opts: st.ShipOptions,
    ws: st.Workspace,
    selected: List[int],
    not_selected: Dict[int, str],
) -> Optional[str]:
    """Print stage ``num``'s line of the plan; return why a real run would refuse it."""
    head = f"  {num:>2} {st.STAGE_NAMES[num]:<19}"
    if num not in selected:
        _out(f"{head} not run  ({not_selected.get(num, 'not selected')})")
        return None
    try:
        action = st.build_stage(num, opts, ws)
    except st.ShipUsageError as exc:
        _out(f"{head} refused  {exc}")
        return str(exc)
    if action.argv is None:
        _out(f"{head} skip     {action.skip_reason}")
    else:
        _out(f"{head} run      fluid {shlex.join(_safe_argv(action.argv))}")
    for note in action.notes:
        _out(f"     {'':<19} note: {note}")
    _assume_outputs(num, action, ws)
    return None


def _assume_outputs(num: int, action: st.StageAction, ws: st.Workspace) -> None:
    """What a real run would have on disk by the time later stages look."""
    if num == 1:
        ws.assume.add(st.BUNDLE_PATH)
    elif num == 6:
        ws.assume.add(st.PLAN_PATH)
    elif num == 3 and action.argv is not None:
        ws.assume.update({"dist/artifacts", st.POLICY_BINDINGS, st.SCHEDULE_DIR})


def _out(text: str) -> None:
    sys.stdout.write(_ci_output.oneline(text) + "\n")
    sys.stdout.flush()


def _safe_argv(argv: List[str]) -> List[str]:
    from fluid_build.cli.auth import _sanitize_argv

    return list(_sanitize_argv(list(argv)))


def _run_id() -> str:
    from fluid_build.observability.run_id import get_or_create_run_id

    return str(get_or_create_run_id())


def _child_env(run_id: str) -> Dict[str, str]:
    from fluid_build.credentials.dotenv_store import environment_for_project_code

    env = dict(environment_for_project_code())
    env["FLUID_RUN_ID"] = run_id
    return env


def _write_reports(
    report_dir: str,
    run_id: str,
    opts: st.ShipOptions,
    exit_code: int,
    ordered: List[report.StageRecord],
    logger: logging.Logger,
) -> None:
    try:
        body = report.build_report(
            ordered,
            run_id=run_id,
            env=opts.env,
            mode=opts.mode,
            contract=opts.contract,
            exit_code=exit_code,
        )
        directory = Path(report_dir)
        merged = report.merge_with_previous(directory, run_id, ordered)
        if merged is not ordered:
            body = report.build_report(
                merged,
                run_id=run_id,
                env=opts.env,
                mode=opts.mode,
                contract=opts.contract,
                exit_code=report.first_failure(merged),
            )
        report.write_reports(directory, body, merged)
    except OSError as exc:
        logger.warning("fluid ship: could not write the run report to %s: %s", report_dir, exc)


def _resolve_fluid_bin() -> List[str]:
    """Resolve the ``fluid`` entry-point invocation for subprocesses.

    Prefer ``sys.executable -m fluid_build.cli`` over the bare ``fluid`` shim:
    an editable install may not have ``fluid`` on PATH, and ``sys.executable``
    guarantees the same Python (and venv) the runner is in.
    """
    return [sys.executable, "-m", "fluid_build.cli"]


def _run_stage(
    fluid_bin: List[str],
    stage: str,
    argv: List[str],
    logger: logging.Logger,
    env: Optional[Dict[str, str]] = None,
    ci: Optional[_ci_output.CiOutput] = None,
) -> int:
    """Subprocess one stage; return its exit code. Stream output through.

    A negative return code (killed by a signal) is reported as 1.
    """
    cmd = fluid_bin + [stage] + argv
    # Sanitise argv before logging — a stage may carry credential-bearing
    # flags (``--*-secret`` / ``--*-token``) the operator passed through.
    shown = shlex.join(["fluid", stage, *_safe_argv(argv)])
    if ci is not None:
        ci.before_child()  # opens the fence first: nothing below can issue a command
        ci.info(f"$ {shown}")
    else:
        logger.info("[ship %s] $ %s", stage, _ci_output.oneline(shown))
    if env is None:
        from fluid_build.credentials.dotenv_store import environment_for_project_code

        # Without the python-dotenv switch this process set: the stage's own
        # main() sets it again, and so knows a python build step may load
        # its own dotenv file.
        env = environment_for_project_code()
    try:
        rc = subprocess.run(cmd, check=False, env=env).returncode
    except FileNotFoundError as exc:
        logger.error("ship_subprocess_failed: %s", exc)
        rc = 127
    finally:
        if ci is not None:
            ci.after_child()
    return 1 if rc < 0 else rc
