# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship``: the canonical 11-stage pipeline runner.

One runner, used identically on a laptop and in CI::

    bundle → validate → generate artifacts → validate artifacts → diff →
    plan → apply → policy apply → verify → [publish] → [schedule sync]

Apply runs the plan stage 6 wrote against the bundle stage 1 froze, never a
re-plan from the contract, so what was reviewed is what is applied. Each stage
is its own ``fluid`` process (``python -m fluid_build.cli <stage> ...``), so
flag handling is exactly the standalone command's and a failing stage prints
what it would on its own. Output streams through unbuffered; ship adds only
CI-native group markers around it. Ship's exit status is its own (0, 1, 2, 3
or 130, see ``_ship_report``); each stage's exit code is in the run report.

Stage semantics live in :mod:`fluid_build.cli._ship_stages` and mirror the
shell bodies generated CI files ran before (a test runs both and compares).
``fluid ship --explain`` prints the resolved plan and runs nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import logging
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from ..observability.tracing import traced_stage
from . import _ci_output
from . import _ship_journal as journal
from . import _ship_release as release
from . import _ship_report as report
from . import _ship_stages as st
from ._common import CLIError, auto_find_contract

COMMAND = "ship"
DEFAULT_REPORT_DIR = "runtime"


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
            "(runtime/bundle.tgz). Stops at the first failing stage. Every run that starts "
            "a stage writes runtime/ship-report.json and runtime/ship-junit.xml, with each "
            "stage's exit code. Precedence: flag > environment variable > default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Stages (name or number): bundle, validate, generate-artifacts (artifacts), "
            "validate-artifacts, diff, plan, apply, policy-apply (policy), verify, publish, "
            "schedule-sync.\n\n"
            "Exit status: 0 every selected stage passed or was skipped; 1 a stage failed; "
            "2 ship refused its arguments; 3 a stage failed after apply changed something, "
            "so those changes are live; 130 interrupted.\n\n"
            "Environment variables read when the flag is absent: FLUID_ENV, APPLY_MODE, "
            "APPLY_BUILD_ID, ALLOW_DATA_LOSS, NO_VERIFY_DIGEST, PUBLISH_TARGETS, SCHEDULER, "
            "SCHEDULER_DESTINATION, SCHEDULER_ENVIRONMENT_NAME, SCHEDULER_LOCATION, "
            "SCHEDULER_WORKSPACE, SCHEDULE_SYNC_DRY_RUN, VALIDATE_STRICT, VERIFY_STRICT, "
            "GENERATE_EMIT, DIFF_EXIT_ON_DRIFT, DIFF_LAST_APPLIED, PLAN_HTML, POLICY_APPLY_MODE, "
            "and RUN_STAGE_<N>_<NAME>: false switches a stage off, even one --stage names "
            "(a run with nothing left says so and exits 0); true switches on 10 or 11 when "
            "--stage is not given.\n\n"
            "Stage journal (--journal-dir, or with --release-dir): a stage that changes the "
            "target is refused after an attempt with no known outcome until "
            "--acknowledge-inspected names it."
            "\n\n"
            "Examples:\n"
            "  fluid ship --explain                      # show the plan, run nothing\n"
            "  fluid ship contract.fluid.yaml --env dev --yes\n"
            "  fluid ship --until plan                   # stages 1-6, then review the plan\n"
            "  fluid ship --stage apply,verify --yes     # apply the reviewed plan\n"
            "  fluid ship --apply-mode dry-run --yes     # nothing is written\n"
        ),
    )
    _add_run_options(p)
    _add_apply_options(p.add_argument_group("apply"))
    _add_stage_options(p.add_argument_group("stage options"))
    _add_publish_options(p.add_argument_group("publish and schedule (opt-in stages 10 and 11)"))
    _add_selection_options(p.add_argument_group("selecting stages"))
    _add_output_options(p.add_argument_group("output"))
    p.set_defaults(cmd=COMMAND, func=run)


def _add_run_options(p: argparse.ArgumentParser) -> None:
    """The contract and the switches every run reads."""
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
        default=None,
        help=(
            "Pass --strict to validate and validate-artifacts (default: on unless "
            "$VALIDATE_STRICT is false; --no-strict drops it)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Same as --apply-mode dry-run: apply changes nothing in the target.",
    )
    p.add_argument(
        "--skip-bundle",
        action="store_true",
        help="Ignored: ship always bundles, so no stage runs a stale bundle.",
    )
    p.add_argument(
        "--skip-plan",
        action="store_true",
        help="Removed: apply runs only the reviewed plan. Use --stage / --until.",
    )


def _add_apply_options(g: argparse._ArgumentGroup) -> None:
    """Stage 7's mode and its overrides."""
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
            "Build id of the -and-build modes (default: $APPLY_BUILD_ID, else the contract's "
            "only build; empty: none)."
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


def _add_artifact_options(g: argparse._ArgumentGroup) -> None:
    """Stage 3: whether it runs, what it emits, what it must write."""
    g.add_argument(
        "--no-generate-artifacts", action="store_true", help="Skip stage 3 (generate artifacts)."
    )
    g.add_argument(
        "--emit",
        default=None,
        metavar="LIST",
        help="Stage 3 --emit, comma-separated (default: $GENERATE_EMIT, all).",
    )
    g.add_argument(
        "--require-artifacts",
        action="append",
        default=None,
        metavar="LIST",
        help=(
            "Stage 3 fails unless this run writes each (repeatable, comma-separated): odcs "
            "(one per port), odps-bitol, opds, policies, schedule. Earlier runs' files never count."
        ),
    )


def _add_stage_options(g: argparse._ArgumentGroup) -> None:
    """What single stages pass to their command."""
    _add_artifact_options(g)
    g.add_argument(
        "--exit-on-drift",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Stage 5 fails on drift (default: on unless $DIFF_EXIT_ON_DRIFT is false; "
            "--no-exit-on-drift reports drift without failing)."
        ),
    )
    g.add_argument(
        "--last-applied",
        default=None,
        metavar="PLAN",
        help="Stage 5 diffs against the last applied plan ($DIFF_LAST_APPLIED).",
    )
    g.add_argument(
        "--plan-html",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Stage 6 also writes runtime/plan.html (default: $PLAN_HTML, off).",
    )
    g.add_argument(
        "--policy-mode",
        choices=st.POLICY_MODES,
        default=None,
        help=(
            "Stage 8 mode (default: $POLICY_APPLY_MODE, else enforce); check after a dry-run "
            "apply either way."
        ),
    )
    g.add_argument(
        "--verify-strict",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Pass --strict to stage 9 (default: on unless $VERIFY_STRICT is false; "
            "--no-verify-strict drops it)."
        ),
    )


def _add_publish_options(g: argparse._ArgumentGroup) -> None:
    """The opt-in stages 10 and 11."""
    g.add_argument(
        "--publish", action="store_true", help="Run stage 10 (publish), with --stage too."
    )
    g.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="CATALOG",
        help="Catalog; repeatable (default: $PUBLISH_TARGETS, datamesh-manager).",
    )
    g.add_argument(
        "--schedule-sync",
        action="store_true",
        help="Run stage 11 (schedule sync), with --stage too.",
    )
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


def _add_selection_options(g: argparse._ArgumentGroup) -> None:
    """Which stages run."""
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
        help="Print the resolved stage plan (argv, skip reasons) and run nothing.",
    )


def _add_output_options(g: argparse._ArgumentGroup) -> None:
    """Log grouping and the run report."""
    g.add_argument(
        "--ci-output",
        choices=_ci_output.MODES,
        default="auto",
        help="Log grouping for the CI system (default: detect from the environment).",
    )
    g.add_argument(
        "--report-dir",
        default=None,
        help="Where ship-report.json and ship-junit.xml go (default: runtime/).",
    )
    g.add_argument(
        "--release-dir",
        default=None,
        metavar="DIR",
        help="Publish a release in DIR (new) when all stages pass (needs 1-4).",
    )
    g.add_argument(
        "--journal-dir",
        default=None,
        metavar="DIR",
        help="Keep a stage journal in DIR (see below).",
    )
    g.add_argument(
        "--acknowledge-inspected",
        action="append",
        default=None,
        metavar="STAGE",
        help="Rerun STAGE whose unknown outcome you inspected.",
    )


@traced_stage("ship")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Run the selected pipeline stages in order; stop at the first failure."""
    try:
        return _run(args, logger)
    except _NothingToRun as exc:
        _out(f"fluid ship: {exc}")
        return report.EXIT_OK
    except st.ShipUsageError as exc:
        logger.error("fluid ship: %s", _ci_output.oneline(str(exc)))
        return report.EXIT_USAGE


def _run(args: argparse.Namespace, logger: logging.Logger) -> int:
    # Taken from where the command was typed, before ship moves to the contract.
    given = getattr(args, "report_dir", None)
    report_dir = os.path.abspath(given) if given else DEFAULT_REPORT_DIR
    wanted = getattr(args, "release_dir", None)
    release_dir = Path(os.path.abspath(wanted)) if wanted else None
    journal_flag = getattr(args, "journal_dir", None)
    journal_dir = Path(os.path.abspath(journal_flag)) if journal_flag else None
    acknowledged = _acknowledged(args, journal_dir or release_dir)
    opts, selected, not_selected, project_dir = _prepare(args)
    with _working_directory(project_dir):
        anchor = None  # an explicit --journal-dir is the operator's, links and all
        if journal_dir is None and release_dir is not None:
            # In the checkout: no link below the project is followed.
            anchor = Path(os.path.abspath(os.curdir))
            journal_dir = anchor / journal.DEFAULT_JOURNAL_DIR
        keep = None if journal_dir is None else _Journaling(journal_dir, acknowledged, anchor)
        if args.explain:
            return _explain(opts, selected, not_selected, release_dir, keep)
        _check_run(selected, not_selected, opts, release_dir)
        return _run_selected(
            args, logger, opts, selected, not_selected, report_dir, release_dir, keep
        )


def _acknowledged(args: argparse.Namespace, journal_on: Optional[Path]) -> Set[int]:
    """The stages ``--acknowledge-inspected`` names (exit 2 for a stage that
    changes nothing, or without a journal to record it in)."""
    given = getattr(args, "acknowledge_inspected", None) or []
    stages = {st.resolve_stage(t) for v in given for t in str(v).split(",") if t.strip()}
    if not stages:
        return set()
    if journal_on is None:
        raise st.ShipUsageError(
            "--acknowledge-inspected needs a stage journal to record it in "
            "(--journal-dir or --release-dir)"
        )
    inert = sorted(stages - journal.TARGET_STAGES)
    if inert:
        names = ", ".join(f"{n} {st.STAGE_NAMES[n]}" for n in inert)
        raise st.ShipUsageError(
            f"--acknowledge-inspected: {names} changes nothing in the target, so no outcome "
            "of it needs inspecting (apply, policy-apply, publish and schedule-sync do)"
        )
    return stages


def _check_run(
    selected: List[int],
    not_selected: Dict[int, str],
    opts: st.ShipOptions,
    release_dir: Optional[Path],
) -> None:
    """Every refusal a run makes once it knows its stages (exit 2, no report)."""
    st.check_preconditions(selected, opts)
    st.check_required_artifacts(selected, not_selected, opts)
    problem = release.refusal(release_dir, selected, not_selected, opts)
    if problem:
        raise st.ShipUsageError(problem)


@contextlib.contextmanager
def _working_directory(path: str) -> Iterator[None]:
    """Run inside the project: the contract's directory when it is not below the
    current one (``fluid ship /tmp/x/contract.fluid.yaml`` from anywhere), so the
    run's runtime/ and dist/ land beside the contract, never in an unrelated cwd."""
    previous = os.getcwd()
    if os.path.realpath(path) != os.path.realpath(previous):
        _out(f"fluid ship: running in {path} (the contract's directory)")
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_selected(
    args: argparse.Namespace,
    logger: logging.Logger,
    opts: st.ShipOptions,
    selected: List[int],
    not_selected: Dict[int, str],
    report_dir: str,
    release_dir: Optional[Path] = None,
    keep: Optional["_Journaling"] = None,
) -> int:
    # A run that bundles is a new run; one that starts later picks up where the
    # last run in this directory stopped (one CI step per stage).
    fresh = 1 in selected
    try:
        if keep is not None:  # before any stage: one run at a time holds the target
            keep.hold(opts)
        return _run_held(args, logger, opts, selected, not_selected, report_dir, release_dir, keep)
    finally:
        if keep is not None:
            keep.close()


def _run_held(
    args: argparse.Namespace,
    logger: logging.Logger,
    opts: st.ShipOptions,
    selected: List[int],
    not_selected: Dict[int, str],
    report_dir: str,
    release_dir: Optional[Path],
    keep: Optional["_Journaling"],
) -> int:
    fresh = 1 in selected
    run_id = _run_id(fresh, logger)
    staging = None
    if release_dir is not None:
        try:
            release.forget_stage_reports(Path.cwd())
            staging = release.open_staging(release_dir, run_id)
        except OSError as exc:
            raise st.ShipUsageError(
                f"--release-dir: cannot prepare runtime/ or a staging directory beside {release_dir}: "
                f"{exc.strerror or exc}"
            ) from None
    ctx = _Context(
        opts=opts,
        ci=_ci_output.CiOutput(_ci_output.detect(args.ci_output)),
        fluid_bin=_resolve_fluid_bin(),
        child_env=_child_env(run_id),
        logger=logger,
        run_id=run_id,
        fresh=fresh,
        selected=selected,
        report_dir=report_dir,
        release_dir=release_dir,
        staging=staging,
        keep=keep,
    )
    records = {num: _unselected_record(num, not_selected.get(num)) for num in st.STAGE_NUMBERS}
    rows: List[Tuple[int, str, str, float]] = []
    for num in selected:
        rec = records[num]
        _run_one(num, rec, ctx)
        skipped = rec.status in report.SKIPPED
        rows.append((num, rec.name, rec.status, 0.0 if skipped else rec.duration_seconds))
        if rec.status in report.STOPS:
            _report_abort(num, rec, ctx)
            break
    return _finish(records, rows, ctx)


def _unselected_record(num: int, reason: Optional[str]) -> report.StageRecord:
    """A stage's record before it runs: a stage this invocation left out is
    skipped by policy when a flag or switch turned it off, else not run."""
    status = report.SKIPPED_BY_POLICY if st.skipped_by_policy(reason) else report.NOT_RUN
    return report.StageRecord(num, st.STAGE_NAMES[num], status=status, reason=reason)


class _NothingToRun(Exception):
    """Every stage the flags selected is switched off: exit 0, no report (the
    step that owns a switched-off stage writes nothing for it)."""

    def __init__(self, stages: List[int]) -> None:
        names = ", ".join(f"{num} {st.STAGE_NAMES[num]}" for num in stages)
        switches = ", ".join(f"{st.STAGE_TOGGLES[num]}=false" for num in stages)
        verb = "is" if len(stages) == 1 else "are"
        noun = "stage" if len(stages) == 1 else "stages"
        super().__init__(f"{noun} {names} {verb} switched off ({switches}): nothing to run")


@dataclass
class _Context:
    """What every stage of one run shares."""

    opts: st.ShipOptions
    ci: _ci_output.CiOutput
    fluid_bin: List[str]
    child_env: Dict[str, str]
    logger: logging.Logger
    run_id: str
    fresh: bool  # this run bundles: it starts a new report, never merges an old one
    selected: List[int]  # this invocation's stages: their records are never merged in
    report_dir: str
    release_dir: Optional[Path] = None  # --release-dir: published when every stage passed
    staging: Optional[Path] = None  # its staging directory, beside it
    keep: Optional["_Journaling"] = None  # the stage journal, when one is kept


class _Journaling:
    """The stage journal of one run: opened once the bundle names the release,
    and which stage this run has opened an attempt of."""

    def __init__(
        self, directory: Path, acknowledged: Set[int], anchor: Optional[Path] = None
    ) -> None:
        self.directory = directory
        self.acknowledged = acknowledged
        self.anchor = anchor
        self.journal: Optional[journal.Journal] = None
        self.held: Optional[journal.Hold] = None  # the contract's target, before stage 1
        self.bundled: Optional[journal.Hold] = None  # another the bundle names (an overlay id)
        self.open_attempt: Optional[int] = None  # the stage started and not yet finished
        self.before: journal.Snapshot = {}  # its report files before it ran

    def hold(self, opts: st.ShipOptions) -> None:
        """Hold the contract's target before stage 1 (exit 2 when another run
        holds it or the journal directory cannot be created)."""
        try:
            self.held = journal.hold_target(
                self.directory,
                product_id=_contract_id(opts.contract),
                environment=opts.env,
                anchor=self.anchor,
            )
        except journal.JournalError as exc:
            raise st.ShipUsageError(f"stage journal: {exc}") from None

    def ensure_open(self, ctx: "_Context") -> journal.Journal:
        """The journal of the release the bundle names (``JournalError`` when
        there is no readable bundle or the journal cannot be written). The
        acknowledgements this run was given are recorded first."""
        if self.journal is not None:
            return self.journal
        root = Path.cwd()
        digest = journal.bundle_digest(root)
        if digest is None:
            raise journal.JournalError(
                f"no readable {st.BUNDLE_PATH} to name the release in the stage journal"
            )
        try:
            product = st.bundled_contract(root).get("id")
        except Exception as exc:  # noqa: BLE001 - an archive the checkout made
            raise journal.JournalError(f"cannot read {st.BUNDLE_PATH}: {exc}") from None
        jr = journal.Journal.open_in(
            self._hold_for(product if isinstance(product, str) and product else "product", ctx),
            product_id=product if isinstance(product, str) and product else "product",
            digest=digest,
            run_id=ctx.run_id,
        )
        try:  # an acknowledgement is recorded before any stage it waives runs
            for num in sorted(self.acknowledged):
                for where, number in jr.acknowledge(num, ctx.run_id):
                    ctx.ci.info(
                        f"stage journal: recorded that attempt {number} of stage {num} "
                        f"{st.STAGE_NAMES[num]} in {where} was inspected (--acknowledge-inspected)"
                    )
        except BaseException:
            jr.close()
            raise
        self.journal = jr
        return jr

    def _hold_for(self, product_id: str, ctx: "_Context") -> journal.Hold:
        """The held target of ``product_id``: the contract's, or, when the
        bundle names another product (an env overlay sets ``id``), that one,
        held now as well."""
        if self.held is None:
            raise journal.JournalError("the stage journal's target is not held")
        if journal.target_stem(product_id) == self.held.product_stem:
            return self.held
        if self.bundled is None or self.bundled.product_stem != journal.target_stem(product_id):
            if self.bundled is not None:
                self.bundled.close()
                self.bundled = None
            self.bundled = journal.hold_target(
                self.directory, product_id=product_id, environment=ctx.opts.env, anchor=self.anchor
            )
        return self.bundled

    def start(self, num: int, argv: List[str], ctx: "_Context") -> Optional[str]:
        """Open stage ``num``'s attempt, or say why it must not run. A stage
        that changes nothing in the target runs even when the journal cannot be
        opened (no bundle yet names the release), unrecorded."""
        try:
            jr = self.ensure_open(ctx)
        except journal.JournalError as exc:
            if journal.side_effects(num, argv):
                return f"stage journal: {exc}"
            ctx.ci.info(
                f"stage {num} {st.STAGE_NAMES[num]}: stage journal: {exc}; this stage changes "
                "nothing in the target, so it runs without a journal entry"
            )
            return None
        refused = jr.refusal(num, argv, Path.cwd(), self.acknowledged)
        if refused:
            return f"stage journal: {refused}"
        try:
            jr.started(num, argv, ctx.run_id, Path.cwd(), shown=_safe_argv(argv))
        except journal.JournalError as exc:
            if journal.side_effects(num, argv):
                return f"stage journal: {exc}"
            ctx.ci.info(
                f"stage {num} {st.STAGE_NAMES[num]}: stage journal: {exc}; this stage changes "
                "nothing in the target, so it runs without a journal entry"
            )
            return None
        self.open_attempt = num
        return None

    def finish(self, num: int, rec: report.StageRecord, ctx: "_Context") -> None:
        """Close the attempt this run opened, with its receipts. A journal that
        cannot be written leaves the attempt without an outcome, which the next
        run treats as unknown."""
        if self.journal is None or self.open_attempt != num:
            return
        self.open_attempt = None
        try:
            self.journal.finished(
                num,
                status=rec.status,
                exit_code=rec.exit_code,
                reason=rec.reason,
                receipts=journal.receipts(num, Path.cwd(), self.before),
            )
        except journal.JournalError as exc:
            ctx.ci.info(f"stage {num} {rec.name}: {exc}; its outcome stays unknown there")

    def block(self) -> Optional[Dict[str, object]]:
        """The report's ``journal`` entry."""
        if self.journal is None:
            return {"dir": str(self.directory), "path": None, "digest": None}
        return {
            "dir": str(self.directory),
            "path": str(self.journal.path),
            "digest": self.journal.digest(),
        }

    def close(self) -> None:
        if self.journal is not None:
            self.journal.close()
        for name in ("bundled", "held"):
            hold = getattr(self, name)
            if hold is not None:
                setattr(self, name, None)
                hold.close()


def _contract_id(contract: str) -> str:
    """The contract's ``id`` (the target is held by it before stage 1 bundles
    it), or ``product`` when it has none."""
    import yaml

    try:
        with open(contract, "rb") as fh:
            body = yaml.safe_load(fh.read(4 << 20))
    except Exception:  # noqa: BLE001 - the checkout's text; stage 1 reports what is wrong
        return "product"
    raw = body.get("id") if isinstance(body, dict) else None
    return raw if isinstance(raw, str) and raw else "product"  # never str() of a tree


def _prepare(
    args: argparse.Namespace,
) -> Tuple[st.ShipOptions, List[int], Dict[int, str], str]:
    """Every refusal ship makes before a stage runs (exit 2, no report), and the
    directory the stages run in."""
    if getattr(args, "skip_plan", False):
        raise st.ShipUsageError(
            "--skip-plan was removed: apply runs only the reviewed plan (runtime/plan.json). "
            "To run part of the pipeline use --stage (e.g. --stage apply,verify) or --until."
        )
    if not auto_find_contract(args):
        raise CLIError(
            report.EXIT_USAGE,
            "contract_required",
            {
                "message": (
                    "No contract path supplied and no contract.fluid.yaml in the current directory."
                )
            },
        )
    if not os.path.isfile(args.contract):
        # Before anything is written: a mistyped path never leaves a .fluid/ or a
        # runtime/ behind in whatever directory it names.
        raise st.ShipUsageError(f"no contract file at {args.contract!r}")
    # Every stage gets the contract as a POSIX path relative to the directory it
    # runs in (``generate artifacts --contract-path`` refuses any other form).
    project_dir, args.contract = st.project_location(args.contract)
    st.check_contract_path(os.path.join(project_dir, args.contract))
    opts = st.resolve_options(args, args.contract, os.environ)
    # RUN_STAGE_<N>_<NAME>: the switches a generated pipeline passes through.
    # An explicit --publish or --schedule-sync beats its own switch; a true
    # switch adds its stage only to a run that does not name its stages, so a
    # CI step running --stage 3 never publishes.
    switched_on, switched_off = st.stage_switches(os.environ)
    switched_off -= {10} if args.publish else set()
    switched_off -= {11} if args.schedule_sync else set()
    whole_run = args.stage is None
    select = functools.partial(
        st.select_stages,
        publish=bool(args.publish) or (whole_run and 10 in switched_on),
        schedule_sync=bool(args.schedule_sync) or (whole_run and 11 in switched_on),
        stage_spec=args.stage,
        until=args.until,
        skip_stages=args.skip_stage or [],
    )
    selected, not_selected = select(switched_off=switched_off)
    if not selected:
        flagged, _ = select()  # what the flags alone left to run
        if flagged:  # all of it switched off: a CI step whose stage is off
            # ...unless the step asked for artifacts: a switched-off stage 3 writes none.
            st.check_required_artifacts(selected, not_selected, opts)
            raise _NothingToRun(flagged)
        raise st.ShipUsageError("no stage left to run (check --stage, --until and --skip-stage)")
    if 10 in selected and not st.dry_run_applied_nothing(opts):  # stage 10 will publish
        st.check_targets(opts.targets)
    return opts, selected, not_selected, project_dir


def _run_one(num: int, rec: report.StageRecord, ctx: _Context) -> None:
    """Run stage ``num`` into ``rec``: passed, failed (with its exit code), skipped
    (by policy, or unsupported) or unknown (interrupted, or killed by a signal)."""
    started = time.monotonic()
    rec.started_at = report.utc_now()
    ctx.ci.stage_start(num, rec.name)
    try:
        # The whole stage body is fenced, not only the child: building the stage
        # reads the contract in this process (its build id, its ports for
        # --require-artifacts), and that text is the checkout's, not ship's.
        with ctx.ci.fenced():
            rc = _attempt(num, rec, ctx)
            unmet = _unmet_requirements(num, rc, ctx)
        if rc is not None:  # a skipped stage keeps no duration and no exit code
            rec.duration_seconds = time.monotonic() - started
            rec.exit_code = rc
            rec.status = report.PASSED if rc == 0 and not unmet else report.FAILED
            if unmet:
                rec.reason = unmet
            if rec.reason == "interrupted" or (rec.reason or "").startswith(_KILLED):
                rec.status = report.UNKNOWN  # what the stage had done by then is not known
        if ctx.keep is not None:
            with ctx.ci.fenced():
                _journal_stage(num, rec, ctx, ctx.keep)
    finally:  # closed even when ship itself fails, so its error is not folded away
        rec.finished_at = report.utc_now()
        ctx.ci.stage_end(num, rec.name)


def _journal_stage(num: int, rec: report.StageRecord, ctx: _Context, keep: "_Journaling") -> None:
    """Close stage ``num``'s attempt in the journal. Stage 1 is recorded once it
    has passed: its bundle is what names the release."""
    if num == 1 and rec.status == report.PASSED and rec.argv is not None:
        keep.start(1, rec.argv, ctx)  # bundle changes nothing: unrecorded at worst
    keep.finish(num, rec, ctx)


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
        rec.status = report.UNSUPPORTED if action.unsupported else report.SKIPPED_BY_POLICY
        rec.reason = action.skip_reason
        ctx.ci.info(f"stage {num} {rec.name}: skipped ({rec.reason})")
        return None
    rec.argv = _safe_argv(action.argv)
    missing = st.missing_input(num, st.Workspace())
    if missing:
        ctx.ci.info(missing)
        return 2
    if ctx.keep is not None and num != 1:
        refused = ctx.keep.start(num, action.argv, ctx)
        if refused:
            rec.reason = refused
            ctx.ci.info(f"stage {num} {rec.name}: {refused}")
            return 2
    if num in (1, 10):
        try:
            Path("runtime").mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # a file named runtime, a read-only checkout
            rec.reason = f"cannot create runtime/: {exc.strerror or exc}"
            ctx.ci.info(f"stage {num} {rec.name}: {rec.reason}")
            return 1
    if num == 3 and ctx.opts.require_artifacts:
        try:
            st.forget_artifacts_manifest(Path.cwd())
        except OSError as exc:  # a directory named MANIFEST.json, a read-only checkout
            rec.reason = f"cannot remove the earlier {st.ARTIFACTS_MANIFEST}: {exc.strerror or exc}"
            ctx.ci.info(f"stage {num} {rec.name}: {rec.reason}")
            return 1
    if ctx.keep is not None:
        ctx.keep.before = journal.snapshot(num, Path.cwd())
    killed: List[int] = []
    try:
        rc = _run_stage(
            ctx.fluid_bin,
            action.argv[0],
            action.argv[1:],
            ctx.logger,
            env=ctx.child_env,
            ci=ctx.ci,
            killed=killed,
        )
    except KeyboardInterrupt:
        rec.reason = "interrupted"
        ctx.ci.info(f"stage {num} {rec.name}: interrupted")
        return 130
    if killed:
        rec.reason = f"{_KILLED} {killed[0]}"
    elif 128 < rc < 160:  # the child turned a signal into its exit (fluid apply's 130)
        rec.reason = f"{_KILLED} {rc - 128} (it exited {rc})"
    if killed or 128 < rc < 160:
        ctx.ci.info(f"stage {num} {rec.name}: {rec.reason}, so what it did is not known")
    return rc


_KILLED = "killed by signal"


def _unmet_requirements(num: int, rc: Optional[int], ctx: _Context) -> Optional[str]:
    """After stage 3 passed, what ``--require-artifacts`` asked for and it did
    not write, as the stage's failure reason (printed, one line each), or ``None``."""
    if num != 3 or rc != 0 or not ctx.opts.require_artifacts:
        return None
    missing = st.missing_required_artifacts(ctx.opts)
    if not missing:
        ctx.ci.info(
            f"stage 3 generate-artifacts: wrote every required artifact "
            f"({','.join(ctx.opts.require_artifacts)})"
        )
        return None
    for line in missing:
        ctx.ci.info(f"stage 3 generate-artifacts: required artifact missing: {line}")
    return f"--require-artifacts not met: {'; '.join(missing)}"


def _report_abort(num: int, rec: report.StageRecord, ctx: _Context) -> None:
    """Name the failed stage and how to go on. Going on runs every stage of this
    command from the failed one: with --publish, plan and apply run again before
    stage 10 publishes."""
    code = int(rec.exit_code or 1)
    ctx.ci.failure(num, rec.name, code)
    rest = [n for n in ctx.selected if n >= num]
    resume = (
        ""
        if rest == ctx.selected
        else f"; to go on from that stage with this bundle, use --stage {st.compact_spec(rest)}"
    )
    # Fixed words only: the reason can hold the checkout's text, and this line is
    # printed outside the stop-commands fence (the reason is in the report).
    if rec.reason == "interrupted":
        why = f"exit {code}, interrupted"
    elif (rec.reason or "").startswith(_KILLED):
        why = f"exit {code}, {_KILLED}"
    elif (rec.reason or "").startswith("stage journal"):
        why = f"exit {code}, refused by the stage journal"
    elif rec.exit_code == 0:
        why = "it exited 0, but --require-artifacts was not met"
    else:
        why = f"exit {code}"
    then = (
        "Do what the stage journal's reason above says (inspect the target and pass "
        "--acknowledge-inspected, or plan again), then run again"
        if (rec.reason or "").startswith("stage journal")
        else "After the fix, run the same command again"
    )
    ctx.logger.error(
        "Ship aborted at stage %d %s (%s). %s%s.",
        num,
        rec.name,
        why,
        then,
        resume,
    )


def _finish(
    records: Dict[int, report.StageRecord],
    rows: List[Tuple[int, str, str, float]],
    ctx: _Context,
) -> int:
    """Reports, the CI summary and the closing line; returns the exit status."""
    ordered = [records[n] for n in st.STAGE_NUMBERS]
    for rec in ordered:
        if rec.status == report.NOT_RUN and rec.reason is None:
            rec.reason = "an earlier stage failed"
    interrupted = any(r.reason == "interrupted" for r in ordered)
    merged = _merged(ctx, ordered)
    applies = st.applies_changes(ctx.opts)
    if interrupted:
        exit_code = run_code = report.EXIT_INTERRUPTED
    else:
        # An apply that passed in an earlier step of this run (one CI step per
        # stage) makes a failure in this step partial too.
        applied_earlier = any(
            r.number == st.APPLY_STAGE and r.status == report.PASSED
            for r in merged
            if r.number not in ctx.selected
        )
        exit_code = report.exit_status(ordered, applies=applies, applied_earlier=applied_earlier)
        run_code = report.exit_status(merged, applies=applies)
    identity: Optional[Dict[str, object]] = None
    problem: Optional[str] = None
    if ctx.staging is not None and exit_code == report.EXIT_OK:
        identity, problem = _release_identity(ctx, ordered)
        if problem:
            exit_code = run_code = _failed_code(ordered, applies)
    written_at = _write_reports(
        ctx, merged, run_code, _release_block(ctx, identity, problem, ordered)
    )
    if ctx.staging is not None:
        exit_code = _close_release(ctx, ordered, identity, problem, written_at, exit_code, merged)
    ctx.ci.summary(rows)
    if exit_code == report.EXIT_PARTIAL:
        ctx.logger.error(
            "Stage 7 apply had passed, so its changes are live and the run is incomplete "
            "(exit %d).",
            exit_code,
        )
    if exit_code == 0:
        passed = sum(1 for r in ordered if r.status == report.PASSED)
        skipped = sum(1 for r in ordered if r.status in report.SKIPPED)
        ctx.ci.line(f"Ship complete: {passed} stage(s) passed, {skipped} skipped")
        _hint_after_until(ordered, ctx.ci)
    return exit_code


def _failed_code(ordered: List[report.StageRecord], applies: bool) -> int:
    """The status of a run whose stages passed but whose release could not be
    published: a failure, partial when apply changed something."""
    applied = applies and any(
        r.number == st.APPLY_STAGE and r.status == report.PASSED for r in ordered
    )
    return report.EXIT_PARTIAL if applied else report.EXIT_FAILED


def _plan_in_release(ordered: List[report.StageRecord]) -> bool:
    return any(r.number == 6 and r.status == report.PASSED for r in ordered)


def _release_identity(
    ctx: _Context, ordered: List[report.StageRecord]
) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    """``(identity, None)``, or ``(None, why it cannot be built)``."""
    try:
        identity = release.build_identity(
            ctx.opts,
            Path.cwd(),
            with_plan=_plan_in_release(ordered),
            applied=any(r.number == st.APPLY_STAGE and r.status == report.PASSED for r in ordered),
        )
    except release.ReleaseError as exc:
        return None, str(exc)
    return identity, None


def _release_block(
    ctx: _Context,
    identity: Optional[Dict[str, object]],
    problem: Optional[str],
    ordered: Sequence[report.StageRecord] = (),
) -> Optional[Dict[str, object]]:
    """The report's ``release`` entry: whether this run publishes one, and why not."""
    if ctx.release_dir is None:
        return None
    if identity is None and not problem:
        problem = _first_stop(ordered)
    return {
        "dir": str(ctx.release_dir),
        "status": "published" if identity is not None else "not-published",
        "identity_digest": None if identity is None else release.identity_digest(identity),
        "reason": problem,
    }


def _first_stop(ordered: Sequence[report.StageRecord]) -> str:
    """Why a run published nothing: the first stage that did not pass."""
    for rec in ordered:
        if rec.status in report.STOPS:
            code = "" if rec.exit_code is None else f", exit {rec.exit_code}"
            why = f", {rec.reason}" if rec.reason else ""
            return f"stage {rec.number} {rec.name} {rec.status}{code}{why}"
    return "a stage did not pass"


def _close_release(
    ctx: _Context,
    ordered: List[report.StageRecord],
    identity: Optional[Dict[str, object]],
    problem: Optional[str],
    written_at: Optional[str],
    exit_code: int,
    merged: List[report.StageRecord],
) -> int:
    """Copy the reports into the staging directory; then publish it, or leave it
    beside DIR for a reader. Returns the exit status."""
    assert ctx.staging is not None and ctx.release_dir is not None
    passed = {r.number for r in ordered if r.status == report.PASSED}
    if identity is None:
        _keep_reports(ctx, passed)
        _say_why_not(ctx, problem)
        return exit_code
    try:
        if written_at is None:  # the report dir holds no report of this run
            raise release.ReleaseError(f"the run report could not be written to {ctx.report_dir}")
        release.copy_reports(ctx.staging, Path(ctx.report_dir), Path.cwd(), passed)
        release.publish(
            ctx.staging,
            ctx.release_dir,
            root=Path.cwd(),
            identity=identity,
            gates=release.gates(ordered),
            provenance=release.provenance(
                ordered,
                run_id=ctx.run_id,
                report_written_at=written_at,
                journal=None if ctx.keep is None else ctx.keep.block(),
            ),
            with_plan=_plan_in_release(ordered),
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - any failure publishes nothing
        interrupted = isinstance(exc, KeyboardInterrupt)
        if interrupted:
            why, failed = "interrupted while publishing", report.EXIT_INTERRUPTED
        else:
            why = str(exc) if isinstance(exc, release.ReleaseError) else f"{type(exc).__name__}"
            failed = _failed_code(ordered, st.applies_changes(ctx.opts))
        # Both copies of the report say so: runtime/ and the one kept in staging,
        # which holds no release.json (it is no release).
        release.unpublish(ctx.staging)
        _write_reports(ctx, merged, failed, _release_block(ctx, None, why))
        _keep_reports(ctx, passed)
        _say_why_not(ctx, why)
        return failed
    ctx.ci.info(f"Release published: {ctx.release_dir} ({release.identity_digest(identity)})")
    return exit_code


def _keep_reports(ctx: _Context, passed: Set[int]) -> None:
    """Best effort: a run that publishes nothing keeps its reports in staging too."""
    assert ctx.staging is not None
    with contextlib.suppress(OSError, release.ReleaseError):
        release.copy_reports(ctx.staging, Path(ctx.report_dir), Path.cwd(), passed)


def _say_why_not(ctx: _Context, problem: Optional[str]) -> None:
    """No release: why (inside the fence, since the reason can quote the
    checkout), and where this run's reports are kept."""
    if problem:
        with ctx.ci.fenced():
            ctx.ci.info(f"fluid ship: release not published: {problem}")
    ctx.logger.error(
        "fluid ship: no release in %s; this run's reports are kept in %s",
        ctx.release_dir,
        ctx.staging,
    )


def _hint_after_until(ordered: List[report.StageRecord], ci: _ci_output.CiOutput) -> None:
    """After ``--until``, say how to go on from where the run stopped: the stages
    the same command would have run past the limit, as one ``--stage`` value."""
    stopped = [r.number for r in ordered if (r.reason or "").startswith("after --until")]
    if not stopped:
        return
    nxt = stopped[0]
    review = " Review runtime/plan.json first." if nxt == st.APPLY_STAGE else ""
    ci.info(
        f"Stopped before stage {nxt} {st.STAGE_NAMES[nxt]} (--until).{review} To go on, run "
        f"the same command with --stage {st.compact_spec(stopped)} in place of --until."
    )


def _explain(
    opts: st.ShipOptions,
    selected: List[int],
    not_selected: Dict[int, str],
    release_dir: Optional[Path] = None,
    keep: Optional[_Journaling] = None,
) -> int:
    ws = st.Workspace()
    jr = _explain_journal(opts, selected, keep)
    _out(
        f"fluid ship: {len(selected)} stage(s) selected "
        f"(env={opts.env}, mode={opts.mode}, contract={opts.contract})"
    )
    problem = _precondition_problem(selected, opts, not_selected, release_dir)
    for num in st.STAGE_NUMBERS:
        refused = _explain_stage(num, opts, ws, selected, not_selected, jr, keep)
        problem = problem or refused
    if release_dir is not None and not problem:
        _out(
            f"release: published to {release_dir} once every selected stage passes "
            f"(staged beside it in {release_dir.name}.partial-*)"
        )
    if problem:
        _out(f"warning: a real run would stop: {problem}")
    return 0


def _precondition_problem(
    selected: List[int],
    opts: st.ShipOptions,
    not_selected: Dict[int, str],
    release_dir: Optional[Path] = None,
) -> Optional[str]:
    try:
        _check_run(selected, not_selected, opts, release_dir)
    except st.ShipUsageError as exc:
        return str(exc)
    return None


def _explain_stage(
    num: int,
    opts: st.ShipOptions,
    ws: st.Workspace,
    selected: List[int],
    not_selected: Dict[int, str],
    jr: Optional[journal.Journal] = None,
    keep: Optional[_Journaling] = None,
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
    _assume_outputs(num, action, ws, opts)
    if jr is not None and keep is not None and action.argv is not None:
        refused = jr.refusal(
            num,
            action.argv,
            Path.cwd(),
            keep.acknowledged,
            replans_first=num == st.APPLY_STAGE and 6 in selected,
        )
        if refused:
            _out(f"     {'':<19} refused by the stage journal: {refused}")
            return f"stage journal: {refused}"
    return None


def _explain_journal(
    opts: st.ShipOptions, selected: List[int], keep: Optional[_Journaling]
) -> Optional[journal.Journal]:
    """Say where the stage journal is and read it, without creating or
    locking anything. A run that bundles names its release only after stage 1,
    so the journal read is that of the bundle on disk now."""
    if keep is None:
        return None
    root = Path.cwd()
    digest = journal.bundle_digest(root)
    if digest is None:
        _out(f"stage journal: {keep.directory} (the release is named once stage 1 has bundled)")
        return None
    try:
        product = st.bundled_contract(root).get("id")
    except ValueError:
        product = None
    try:
        jr = journal.Journal.read(
            keep.directory,
            product_id=str(product or "product"),
            environment=opts.env,
            digest=digest,
            anchor=keep.anchor,
        )
    except journal.JournalError as exc:
        _out(f"stage journal: {exc}")
        return None
    rebundles = " (stage 1 bundles again first)" if 1 in selected else ""
    tried = sum(len(v) for v in jr.attempts.values())
    _out(f"stage journal: {jr.path}, {tried} attempt(s) of this release{rebundles}")
    return jr


def _assume_outputs(
    num: int, action: st.StageAction, ws: st.Workspace, opts: st.ShipOptions
) -> None:
    """What a real run would have on disk by the time later stages look."""
    if num == 1:
        ws.assume.add(st.BUNDLE_PATH)
    elif num == 6:
        ws.assume.add(st.PLAN_PATH)
    elif num == 3 and action.argv is not None:
        # Policy bindings are written whenever --emit includes policies (empty when
        # no policy is declared); schedule DAGs when it includes schedule and the
        # contract declares something to schedule.
        ws.assume.add("dist/artifacts")
        if st.emits(opts, "policies"):
            ws.assume.add(st.POLICY_BINDINGS)
        if st.emits(opts, "schedule") and st.schedules_expected(opts):
            ws.assume.add(st.SCHEDULE_DIR)


def _out(text: str) -> None:
    sys.stdout.write(_ci_output.oneline(text) + "\n")
    sys.stdout.flush()


def _safe_argv(argv: List[str]) -> List[str]:
    from fluid_build.cli.auth import _sanitize_argv

    return list(_sanitize_argv(list(argv)))


def _run_id(fresh: bool, logger: logging.Logger) -> str:
    """``$FLUID_RUN_ID`` (CI sets it per job), else ``.fluid/run-id.txt``. A fresh
    run (one that bundles) replaces a persisted id, so it never merges into the
    report of an earlier, unrelated run in the same directory. An id that cannot
    be kept there (``.fluid`` is a file, the file is not text, the checkout is
    read-only) still names this run; the stages get it through the environment."""
    from fluid_build.observability.run_id import (
        RUN_ID_ENV_VAR,
        clear_run_id,
        get_or_create_run_id,
    )

    try:
        if fresh and not os.environ.get(RUN_ID_ENV_VAR):
            clear_run_id()
        return str(get_or_create_run_id())
    except (OSError, ValueError) as exc:
        logger.warning(
            "fluid ship: cannot keep the run id in .fluid/run-id.txt (%s); a later "
            "--stage run starts its own report",
            _ci_output.oneline(str(exc)),
        )
        return secrets.token_hex(6)


def _child_env(run_id: str) -> Dict[str, str]:
    from fluid_build.credentials.dotenv_store import environment_for_project_code

    env = dict(environment_for_project_code())
    env["FLUID_RUN_ID"] = run_id
    return env


def _merged(ctx: _Context, ordered: List[report.StageRecord]) -> List[report.StageRecord]:
    """The records of the whole run: this invocation's, plus those an earlier
    invocation of the same run left for the stages not selected now. A run that
    bundles is a new run and keeps none."""
    if ctx.fresh:
        return ordered
    opts = ctx.opts
    return report.merge_with_previous(
        Path(ctx.report_dir),
        ctx.run_id,
        ordered,
        selected=ctx.selected,
        env=opts.env,
        mode=opts.mode,
        contract=opts.contract,
    )


def _write_reports(
    ctx: _Context,
    merged: List[report.StageRecord],
    run_code: int,
    release_block: Optional[Dict[str, object]] = None,
) -> Optional[str]:
    """Write the run's report; return when it was written. Its ``exit_code`` is
    the run's status, which per-stage CI steps spread over several invocations."""
    opts = ctx.opts
    try:
        body = report.build_report(
            merged,
            run_id=ctx.run_id,
            env=opts.env,
            mode=opts.mode,
            contract=opts.contract,
            exit_code=run_code,
            release=release_block,
            journal=None if ctx.keep is None else ctx.keep.block(),
        )
        report.write_reports(Path(ctx.report_dir), body, merged)
    except (OSError, ValueError) as exc:
        ctx.logger.warning(
            "fluid ship: could not write the run report to %s: %s",
            ctx.report_dir,
            _ci_output.oneline(str(exc)),
        )
        return None
    return str(body["written_at"])


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
    killed: Optional[List[int]] = None,
) -> int:
    """Subprocess one stage; return its exit code. Stream output through.

    A negative return code (killed by a signal) is reported as 1, and the
    signal is appended to ``killed`` when it is given.
    """
    cmd = fluid_bin + [stage] + argv
    # Sanitise argv before logging: a stage may carry credential-bearing
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
    # On GitHub the child's stderr shares its stdout: two pipes reach the runner
    # in no fixed order, so a late stderr line could land after the resume token.
    stderr = subprocess.STDOUT if ci is not None and ci.merge_child_stderr else None
    say = ci.info if ci is not None else logger.warning
    try:
        with _patient_interrupts(say) as interrupts:
            try:
                rc = subprocess.run(cmd, check=False, env=env, stderr=stderr).returncode
            except FileNotFoundError as exc:
                logger.error("ship_subprocess_failed: %s", exc)
                rc = 127
    finally:
        if ci is not None:
            ci.after_child()
    if interrupts:
        raise KeyboardInterrupt  # the stage has ended; stop the run after it
    if rc < 0 and killed is not None:
        killed.append(-rc)
    return 1 if rc < 0 else rc


@contextlib.contextmanager
def _patient_interrupts(say: Callable[[str], None]) -> Iterator[List[int]]:
    """While a stage runs, the first Ctrl-C lets it end on its own.

    A terminal sends SIGINT to the whole foreground process group, so the child
    got it too and may be cleaning up (``fluid apply`` releasing an OpenTofu
    state lock). ``subprocess.run`` would kill it 0.25s later; instead the first
    Ctrl-C is recorded and the run stops once the child has exited. A SIGINT sent
    to ship's process alone does not reach the child, which then runs to its
    end; ship never forwards it, since a second SIGINT makes OpenTofu abandon a
    running apply. A second Ctrl-C raises here, and ``subprocess.run`` kills the
    child, as ``make`` and OpenTofu do.
    """
    hits: List[int] = []

    def on_sigint(signum: int, _frame: object) -> None:
        hits.append(signum)
        if len(hits) > 1:
            raise KeyboardInterrupt
        say("interrupted: ship stops when this stage ends (press Ctrl-C again to stop it now)")

    try:
        previous = signal.signal(signal.SIGINT, on_sigint)
    except ValueError:  # not the main thread: keep Python's default handling
        yield hits
        return
    try:
        yield hits
    finally:
        signal.signal(signal.SIGINT, previous)
