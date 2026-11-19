# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The 11-stage pipeline model behind ``fluid ship``.

The stage semantics (argv, skip rules, ordering) mirror the shell bodies of
``fluid_build.forge.core.pipeline_systems._base._stage_specs``, which is what
every generated CI file ran until now. ``tests/cli/test_ship_pipeline.py``
runs those shell bodies against a fake ``fluid`` and asserts the argv recorded
equals the argv built here, so the two cannot drift apart silently.

Standard library only at module scope: ``fluid --help`` imports ``ship`` and
must stay light. Anything heavy (``generate_ci`` reads and composes the
contract) is imported inside the function that needs it.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterator,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ._ship_table import (
    ARTIFACTS_DIR,
    BUNDLE_PATH,
    PLAN_PATH,
    STAGE_NAMES,
    STAGE_NUMBERS,
    STAGE_TOGGLES,
    STAGES,
)

ARTIFACTS_MANIFEST = "dist/artifacts/MANIFEST.json"
POLICY_BINDINGS = "dist/artifacts/policy/bindings.json"
SCHEDULE_DIR = "dist/artifacts/schedule"

APPLY_MODES: Tuple[str, ...] = (
    "dry-run",
    "create-only",
    "amend",
    "amend-and-build",
    "replace",
    "replace-and-build",
)
BUILD_APPLY_MODES: Tuple[str, ...] = ("amend-and-build", "replace-and-build")

DEFAULT_ENV = "dev"
DEFAULT_MODE = "amend"
DEFAULT_PUBLISH_TARGET = "datamesh-manager"

#: Stages that read runtime/bundle.tgz (stage 7 reads it too, beside the plan).
NEEDS_BUNDLE = frozenset({2, 3, 5, 6, 7, 9})
OPT_IN_FLAG = {10: "--publish", 11: "--schedule-sync"}
APPLY_STAGE = 7
POLICY_MODES: Tuple[str, ...] = ("enforce", "check")

VALIDATE_REPORT = "runtime/validate-report.json"
VALIDATE_ARTIFACTS_REPORT = "runtime/validate-artifacts-report.json"
PLAN_HTML_PATH = "runtime/plan.html"
SCHEDULE_SYNC_REPORT = "runtime/schedule-sync-report.json"

#: The ``generate artifacts --emit`` keys ``--require-artifacts`` accepts, in
#: that command's canonical order (``artifact_fanout.EMIT_KEYS``).
REQUIRABLE_ARTIFACTS: Tuple[str, ...] = ("odps-bitol", "odcs", "opds", "schedule", "policies")


class ShipUsageError(Exception):
    """A request ship refuses before (or instead of) running: exit code 2."""


@dataclass
class ShipOptions:
    """Everything the stages need, with flag > env > default already applied."""

    contract: str
    env: str = DEFAULT_ENV
    mode: str = DEFAULT_MODE
    build_id: Optional[str] = None  # None: derive from the contract at stage 7
    yes: bool = False
    strict: bool = True
    allow_data_loss: bool = False
    no_verify_digest: bool = False
    no_verify_strict: bool = False
    # False: --no-generate-artifacts, or RUN_STAGE_3_GENERATE_ARTIFACTS=false (stage 3
    # is not in this pipeline, so stage 4 skips instead of asking for its output).
    generate_artifacts: bool = True
    targets: List[str] = field(default_factory=lambda: [DEFAULT_PUBLISH_TARGET])
    scheduler: str = ""
    scheduler_destination: str = ""
    scheduler_environment_name: str = ""
    scheduler_location: str = ""
    scheduler_workspace: str = ""
    schedule_sync_dry_run: bool = False
    skip_bundle: bool = False
    # False when --skip-stage drops stage 7: this pipeline applies nowhere (the
    # generated shell's RUN_STAGE_7_APPLY=false), so a dry-run mode skips nothing.
    # ``--stage 9`` alone keeps it True: stage 7 runs in another step of the run.
    apply_in_pipeline: bool = True
    emit: str = ""  # stage 3 --emit; blank: every emitter
    diff_exit_on_drift: bool = True  # stage 5 --exit-on-drift
    diff_last_applied: str = ""  # stage 5 --last-applied: the plan the last apply ran
    plan_html: bool = False  # stage 6 --html runtime/plan.html
    policy_mode: str = "enforce"  # stage 8 --mode, unless stage 7 ran as a dry run
    # --require-artifacts: stage 3 fails unless this run writes each key's files.
    require_artifacts: Tuple[str, ...] = ()


@dataclass
class StageAction:
    """What one stage does right now: run ``argv``, or skip with ``skip_reason``."""

    num: int
    name: str
    argv: Optional[List[str]] = None
    skip_reason: Optional[str] = None
    # A skip because there is nothing for the stage to act on (no policy
    # bindings, no DAGs, no scheduler), not because a flag or switch said so.
    unsupported: bool = False
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Option resolution: flag > env var > default
# --------------------------------------------------------------------------


def _text(flag: Optional[str], env: Mapping[str, str], name: str, default: str) -> str:
    if flag is not None:
        return flag
    return env.get(name, default)


def _truth(flag: bool, env: Mapping[str, str], name: str) -> bool:
    return bool(flag) or env.get(name, "") == "true"


def _off_unless_true(flag: Optional[bool], env: Mapping[str, str], name: str) -> bool:
    """A switch that is off by default: the flag when given (``--x`` or
    ``--no-x``), else on only for the exact word ``true`` in the variable."""
    if flag is not None:
        return bool(flag)
    return env.get(name, "") == "true"


def check_apply_mode(mode: str, where: str) -> str:
    if mode not in APPLY_MODES:
        raise ShipUsageError(f"{where} {mode!r} is not an apply mode: {', '.join(APPLY_MODES)}")
    return mode


def check_env(value: str, where: str) -> str:
    """Refuse, before stage 1, an environment name a later stage would refuse.

    Every stage passes it to ``--env``; stage 3 (``generate artifacts``) and
    stage 10 (``publish``) hold it to a grammar. Without this check a bad
    name runs stages 1 and 2 against the base contract (``bundle`` only warns
    ``overlay_not_found``) and fails at stage 3. The rule is the one the
    generated pipelines already apply to their defaults
    (``pipeline_systems._base.check_env_name``), reused rather than copied.
    """
    from fluid_build.forge.core.pipeline_systems._base import check_env_name

    try:
        return check_env_name(where, value)
    except ValueError as exc:
        raise ShipUsageError(str(exc)) from None


def _resolve_mode(flag_mode: object, dry_run: bool, env: Mapping[str, str]) -> str:
    """``--dry-run`` / ``--apply-mode`` > ``$APPLY_MODE`` > amend."""
    if dry_run:
        if flag_mode not in (None, "dry-run"):
            raise ShipUsageError(f"--dry-run conflicts with --apply-mode {flag_mode}")
        flag_mode = "dry-run"
    if flag_mode is not None:
        return check_apply_mode(str(flag_mode), "--apply-mode")
    return check_apply_mode(env.get("APPLY_MODE") or DEFAULT_MODE, "APPLY_MODE")


def _resolve_build_id(flag: object, env: Mapping[str, str]) -> Optional[str]:
    """``--build-id`` > ``$APPLY_BUILD_ID`` > ``None`` (derive from the contract).

    A set-but-empty ``APPLY_BUILD_ID`` is kept as ``""``: deliberately no build id.
    """
    return str(flag) if flag is not None else env.get("APPLY_BUILD_ID")


def _resolve_targets(flags: Sequence[object], env: Mapping[str, str]) -> List[str]:
    """``--target`` (repeatable) > ``$PUBLISH_TARGETS`` > the legacy ``$CATALOG`` >
    the default, as the generated pipelines' ``${PUBLISH_TARGETS:-${CATALOG:-...}}``."""
    if flags:
        return [str(t) for t in flags]
    return (
        env.get("PUBLISH_TARGETS", "").split()
        or env.get("CATALOG", "").split()
        or [DEFAULT_PUBLISH_TARGET]
    )


def _on_unless_false(flag: Optional[bool], env: Mapping[str, str], name: str) -> bool:
    """A switch that is on by default: the flag when given, else off only for
    the exact word ``false`` in the variable (as the generated pipelines read it)."""
    if flag is not None:
        return bool(flag)
    return env.get(name, "") != "false"


def _resolve_policy_mode(flag: object, env: Mapping[str, str]) -> str:
    """``--policy-mode`` > ``$POLICY_APPLY_MODE`` > enforce."""
    where = "--policy-mode" if flag is not None else "POLICY_APPLY_MODE"
    value = str(flag) if flag is not None else (env.get("POLICY_APPLY_MODE") or "enforce")
    if value not in POLICY_MODES:
        raise ShipUsageError(f"{where} {value!r} is not a policy mode: {', '.join(POLICY_MODES)}")
    return value


def stage_switches(env: Mapping[str, str]) -> Tuple[Set[int], Set[int]]:
    """``(switched on, switched off)`` by the ``RUN_STAGE_<N>_<NAME>`` variables:
    ``true`` switches on stage 10 or 11 (the others are on anyway), ``false``
    switches any stage off. Any other value leaves the default."""
    on = {num for num in (10, 11) if env.get(STAGE_TOGGLES[num], "") == "true"}
    off = {num for num, name in STAGE_TOGGLES.items() if env.get(name, "") == "false"}
    return on, off


def _resolve_env_name(flag_env: Optional[str], env: Mapping[str, str]) -> str:
    """``--env`` > ``$FLUID_ENV`` > dev, refused up front when a stage would refuse it."""
    value = _text(flag_env, env, "FLUID_ENV", DEFAULT_ENV) or DEFAULT_ENV
    return check_env(value, "--env" if flag_env is not None else "FLUID_ENV")


def resolve_options(args: object, contract: str, env: Mapping[str, str]) -> ShipOptions:
    """Build :class:`ShipOptions` from the parsed flags and the environment."""

    def get(name: str, default: object = None) -> Any:
        return getattr(args, name, default)

    def opt(flag: str, var: str) -> str:
        value = get(flag)
        return _text(None if value is None else str(value), env, var, "")

    mode = _resolve_mode(get("apply_mode"), bool(get("dry_run")), env)
    build_id = _resolve_build_id(get("build_id"), env)
    targets = _resolve_targets(get("target") or [], env)
    env_name = _resolve_env_name(str(get("env")) if get("env") else None, env)
    switched_off = stage_switches(env)[1]

    return ShipOptions(
        contract=contract,
        env=env_name,
        mode=mode,
        build_id=build_id,
        yes=bool(get("yes")),
        strict=_on_unless_false(get("strict"), env, "VALIDATE_STRICT"),
        allow_data_loss=_truth(bool(get("allow_data_loss")), env, "ALLOW_DATA_LOSS"),
        no_verify_digest=_truth(bool(get("no_verify_digest")), env, "NO_VERIFY_DIGEST"),
        no_verify_strict=not _on_unless_false(get("verify_strict"), env, "VERIFY_STRICT"),
        generate_artifacts=not get("no_generate_artifacts") and 3 not in switched_off,
        targets=targets,
        scheduler=opt("scheduler", "SCHEDULER"),
        scheduler_destination=opt("scheduler_destination", "SCHEDULER_DESTINATION"),
        scheduler_environment_name=opt("scheduler_environment_name", "SCHEDULER_ENVIRONMENT_NAME"),
        scheduler_location=opt("scheduler_location", "SCHEDULER_LOCATION"),
        scheduler_workspace=opt("scheduler_workspace", "SCHEDULER_WORKSPACE"),
        schedule_sync_dry_run=_truth(
            bool(get("schedule_sync_dry_run")), env, "SCHEDULE_SYNC_DRY_RUN"
        ),
        skip_bundle=bool(get("skip_bundle")),
        apply_in_pipeline=APPLY_STAGE
        not in {resolve_stage(t) for t in get("skip_stage") or []} | switched_off,
        emit=opt("emit", "GENERATE_EMIT"),
        diff_exit_on_drift=_on_unless_false(get("exit_on_drift"), env, "DIFF_EXIT_ON_DRIFT"),
        diff_last_applied=opt("last_applied", "DIFF_LAST_APPLIED"),
        plan_html=_off_unless_true(get("plan_html"), env, "PLAN_HTML"),
        policy_mode=_resolve_policy_mode(get("policy_mode"), env),
        require_artifacts=_resolve_required_artifacts(get("require_artifacts")),
    )


def _resolve_required_artifacts(flag: object) -> Tuple[str, ...]:
    """``--require-artifacts odcs,odps-bitol`` (repeatable: every use counts) ->
    the keys in canonical order."""
    if flag is None:
        return ()
    values = flag if isinstance(flag, (list, tuple)) else [flag]
    keys = {part.strip() for value in values for part in str(value).split(",") if part.strip()}
    if not keys:
        raise ShipUsageError("--require-artifacts names no artifact")
    unknown = sorted(keys - set(REQUIRABLE_ARTIFACTS))
    if unknown:
        raise ShipUsageError(
            f"--require-artifacts: unknown key(s) {', '.join(unknown)}: use "
            + ", ".join(REQUIRABLE_ARTIFACTS)
        )
    return tuple(k for k in REQUIRABLE_ARTIFACTS if k in keys)


def check_targets(targets: Sequence[str]) -> None:
    """A target may name a catalog, never an endpoint: the runner's catalog
    credential would go to whatever the endpoint is."""
    for target in targets:
        if ":" in target:
            raise ShipUsageError(
                f"publish target {target!r} is refused: targets name catalogs, not endpoints "
                "(no ':' allowed)"
            )


# --------------------------------------------------------------------------
# Stage selection
# --------------------------------------------------------------------------


def _unknown_stage(token: str) -> NoReturn:
    raise ShipUsageError(
        f"unknown stage {token!r}: use a number 1-11 or one of "
        + ", ".join(name for _, name, _ in STAGES)
    )


def resolve_stage(token: str) -> int:
    """``7``, ``apply``, ``policy``, ``generate_artifacts`` -> the stage number."""
    text = token.strip().lower()
    # ASCII only: str.isdigit() is also true for a superscript two, which int() refuses.
    if text.isascii() and text.isdigit() and int(text) in STAGE_NAMES:
        return int(text)
    for num, name, aliases in STAGES:
        if text == name or text in aliases:
            return num
    _unknown_stage(token)


def _split_range(token: str) -> Optional[Tuple[int, int]]:
    """The first hyphen that splits ``token`` into two stages, as their numbers.

    Stage names contain hyphens themselves (``validate-artifacts-verify`` is
    stage 4 to stage 9), so every hyphen is tried in turn.
    """
    for idx, ch in enumerate(token):
        if ch != "-":
            continue
        try:
            return resolve_stage(token[:idx]), resolve_stage(token[idx + 1 :])
        except ShipUsageError:
            continue
    return None


def _parse_stage_token(token: str) -> Set[int]:
    """One comma-separated ``--stage`` token: a stage, or a ``from-to`` range."""
    try:
        return {resolve_stage(token)}
    except ShipUsageError:
        pass
    bounds = _split_range(token)
    if bounds is None:
        _unknown_stage(token)
    lo, hi = bounds
    if lo > hi:
        raise ShipUsageError(f"stage range {token!r} runs backwards")
    return set(range(lo, hi + 1))


def parse_stage_spec(spec: str) -> List[int]:
    """``7``, ``6-9``, ``plan,apply``, ``validate-artifacts-verify`` -> sorted numbers."""
    selected: Set[int] = set()
    for raw in spec.split(","):
        if raw.strip():
            selected |= _parse_stage_token(raw.strip())
    if not selected:
        raise ShipUsageError("--stage names no stage")
    return sorted(selected)


def _initial_stages(stage_spec: Optional[str], publish: bool, schedule_sync: bool) -> Set[int]:
    """``--stage`` when given, else 1-9; plus the opt-in stages that were asked
    for, either way: ``--stage 7-9 --publish`` publishes too."""
    chosen = set(parse_stage_spec(stage_spec)) if stage_spec is not None else set(range(1, 10))
    return chosen | ({10} if publish else set()) | ({11} if schedule_sync else set())


def compact_spec(numbers: Sequence[int]) -> str:
    """``[7, 8, 9, 11]`` -> ``"7-9,11"``: the ``--stage`` value naming exactly these."""
    spans: List[List[int]] = []
    for num in sorted(set(numbers)):
        if spans and num == spans[-1][-1] + 1:
            spans[-1].append(num)
        else:
            spans.append([num])
    return ",".join(str(s[0]) if len(s) == 1 else f"{s[0]}-{s[-1]}" for s in spans)


def skipped_by_policy(reason: Optional[str]) -> bool:
    """Whether a stage :func:`select_stages` left out was switched off (``--skip-stage``,
    a ``RUN_STAGE_<N>_<NAME>=false`` variable) rather than not selected."""
    return reason == "--skip-stage" or (reason or "").endswith("=false")


def _why_not_chosen(num: int, stage_spec: Optional[str]) -> str:
    if stage_spec is not None:
        return "not in --stage"
    if num in OPT_IN_FLAG:
        return f"opt-in: pass {OPT_IN_FLAG[num]} or set {STAGE_TOGGLES[num]}=true"
    return "not selected"


def select_stages(
    *,
    publish: bool,
    schedule_sync: bool,
    stage_spec: Optional[str] = None,
    until: Optional[str] = None,
    skip_stages: Sequence[str] = (),
    switched_off: Collection[int] = (),
) -> Tuple[List[int], Dict[int, str]]:
    """The stage numbers to consider, plus why each other stage is not in the set.
    ``switched_off``: the stages a ``RUN_STAGE_<N>_<NAME>=false`` variable turned off.
    A switched-off stage that no flag removed first is reported as switched off, in
    or out of ``--stage``: per-stage CI steps share one report, and the step that
    owns the stage writes nothing for it."""
    chosen = _initial_stages(stage_spec, publish, schedule_sync)
    why: Dict[int, str] = {}
    if until is not None:
        limit = resolve_stage(until)
        for num in sorted(n for n in chosen if n > limit):
            chosen.discard(num)
            why[num] = f"after --until {limit}"
    for num in (resolve_stage(token) for token in skip_stages):
        if num in chosen:
            chosen.discard(num)
            why[num] = "--skip-stage"
    for num in sorted(switched_off):
        chosen.discard(num)
        why.setdefault(num, f"{STAGE_TOGGLES[num]}=false")
    for num in STAGE_NUMBERS:
        if num not in chosen:
            why.setdefault(num, _why_not_chosen(num, stage_spec))
    return sorted(chosen), why


# --------------------------------------------------------------------------
# Per-stage argv and skip rules
# --------------------------------------------------------------------------


class Workspace:
    """The files stage rules look at, relative to ``root``.

    ``assume`` lists paths a selected earlier stage will produce; ``--explain``
    passes them so the plan shows what a real run would do.
    """

    def __init__(self, root: Optional[Path] = None, assume: Optional[Set[str]] = None) -> None:
        self.root = root or Path.cwd()
        self.assume = assume or set()

    def has_file(self, rel: str) -> bool:
        return rel in self.assume or (self.root / rel).is_file()

    def has_dir(self, rel: str) -> bool:
        rel = rel.rstrip("/")
        return rel in self.assume or (self.root / rel).is_dir()

    def dir_has_entries(self, rel: str) -> bool:
        path = self.root / rel
        return rel in self.assume or (path.is_dir() and any(path.iterdir()))


def _env_args(opts: ShipOptions) -> List[str]:
    return ["--env", opts.env]


def applies_changes(opts: ShipOptions) -> bool:
    """Whether stage 7 changes anything in this mode (every mode but dry-run)."""
    return opts.mode != "dry-run"


def dry_run_applied_nothing(opts: ShipOptions) -> bool:
    """As the generated shell decides it: stage 7 is in this pipeline, as a dry
    run. A pipeline without stage 7 applies elsewhere, so its mode says nothing
    about what is live, and verify, publish and schedule-sync run."""
    return not applies_changes(opts) and opts.apply_in_pipeline


def _skip_after_dry_run(opts: ShipOptions, what: str) -> Optional[str]:
    if dry_run_applied_nothing(opts):
        return f"APPLY_MODE dry-run applied nothing, so {what}"
    return None


@contextlib.contextmanager
def _quiet_probe() -> Iterator[None]:
    """Ship's own look into the contract (its one build? anything to schedule?)
    prints and logs nothing. Loading it warns about the overlays with their ids
    copied in verbatim (``overlay_entry_added``), and on GitHub Actions a line of
    the checkout's text is a workflow command; the stage children load the same
    files and report them inside the stop-commands fence."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


def emits(opts: ShipOptions, key: str) -> bool:
    """Whether stage 3's ``--emit`` (blank: every emitter) includes ``key``.
    ``odps`` is ``generate artifacts``' deprecated spelling of ``opds``."""
    if not opts.emit.strip():
        return True
    parts = {part.strip() for part in opts.emit.split(",")}
    return key in {"opds" if p == "odps" else p for p in parts}


def schedules_expected(opts: ShipOptions) -> bool:
    """Whether stage 3 writes schedule DAGs for this contract, by the rule
    ``generate artifacts`` applies (an ``orchestration.engine``, or a build with
    ``execution.trigger.schedule``). Only ``--explain`` asks; a run looks."""
    from fluid_build.forge.core.artifact_fanout import (
        FanoutError,
        _load_schedule_contract,
        _schedule_skip_reason,
    )

    with _quiet_probe():
        try:
            contract = _load_schedule_contract(Path(opts.contract), opts.env, logging.getLogger())
        except FanoutError:  # a contract stage 3 cannot load fails stage 3 itself
            return False
        return _schedule_skip_reason(contract) is None


def _effective_build_id(opts: ShipOptions) -> str:
    if opts.build_id is not None:
        return opts.build_id
    from fluid_build.cli.generate_ci import _single_build_id

    with _quiet_probe():
        return str(_single_build_id(opts.contract) or "")


def build_stage(num: int, opts: ShipOptions, ws: Workspace) -> StageAction:
    """The argv (or skip reason) of stage ``num`` for the workspace as it is now."""
    builder = _BUILDERS[num]
    action = StageAction(num=num, name=STAGE_NAMES[num])
    builder(action, opts, ws)
    return action


def _bundle(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    if o.skip_bundle:
        # Reusing runtime/bundle.tgz could apply an older contract than the one
        # given, with exit 0; bundling takes about a second, so ship always does.
        a.notes.append(
            "--skip-bundle is ignored: ship always bundles, so every later stage runs "
            "the contract as it is now"
        )
    a.argv = ["bundle", o.contract, *_env_args(o), "--format", "tgz", "--out", BUNDLE_PATH]


def _validate(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    a.argv = [
        "validate",
        BUNDLE_PATH,
        *_env_args(o),
        "--report",
        VALIDATE_REPORT,
        *(["--strict"] if o.strict else []),
    ]


def _generate_artifacts(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    # A reference-only contract runs it too: the transformation lives elsewhere,
    # but its ODCS, ODPS and policy artifacts do not depend on where it runs
    # (``generate artifacts`` itself drops no key for one; schedule has its own gate).
    if not o.generate_artifacts:
        a.skip_reason = "--no-generate-artifacts"
    else:
        a.argv = [
            "generate",
            "artifacts",
            BUNDLE_PATH,
            *_env_args(o),
            "--contract-path",
            o.contract,
            "--out",
            ARTIFACTS_DIR,
            *(["--emit", o.emit] if o.emit else []),
        ]
        if o.require_artifacts:
            a.notes.append(
                f"--require-artifacts {','.join(o.require_artifacts)}: this stage fails unless "
                f"it writes each of them, listed in a new {ARTIFACTS_MANIFEST}"
            )


def _validate_artifacts(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    if not ws.has_dir(ARTIFACTS_DIR):
        if o.generate_artifacts:
            # Stage 3 was to write it: a missing directory is a failure, never
            # a pass that validated nothing.
            raise ShipUsageError(
                f"{ARTIFACTS_DIR} does not exist, but stage 3 generate-artifacts was to write "
                "it; pass --no-generate-artifacts when this pipeline generates no artifacts"
            )
        a.skip_reason = (
            f"{ARTIFACTS_DIR} does not exist (stage 3 is off: --no-generate-artifacts "
            f"or {STAGE_TOGGLES[3]}=false)"
        )
        return
    a.argv = [
        "validate-artifacts",
        ARTIFACTS_DIR,
        "--report",
        VALIDATE_ARTIFACTS_REPORT,
        *(["--strict"] if o.strict else []),
    ]


def _diff(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    a.argv = [
        "diff",
        BUNDLE_PATH,
        *(["--exit-on-drift"] if o.diff_exit_on_drift else []),
        *_env_args(o),
        "--out",
        "runtime/diff-report.json",
    ]
    if o.diff_last_applied:
        a.argv += ["--last-applied", o.diff_last_applied]
        if not ws.has_file(o.diff_last_applied):
            a.notes.append(
                f"no plan at {o.diff_last_applied} yet (a first run): the target is "
                "compared with the contract alone"
            )


def _plan(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    a.argv = [
        "plan",
        BUNDLE_PATH,
        "--out",
        PLAN_PATH,
        "--mode",
        o.mode,
        *_env_args(o),
        "--check-sovereignty",
        *(["--html", PLAN_HTML_PATH] if o.plan_html else []),
    ]


def _apply(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    argv = [
        "apply",
        PLAN_PATH,
        "--bundle",
        BUNDLE_PATH,
        "--mode",
        o.mode,
        *_env_args(o),
        *(["--yes"] if o.yes else []),
        "--ensure-opentofu",
        "--report",
        "runtime/apply-report.html",
    ]
    if o.mode in BUILD_APPLY_MODES:
        # Derived from the contract only here, where apply uses it: other
        # modes never read the contract in the ship process.
        build_id = _effective_build_id(o)
        if build_id:
            argv += ["--build-id", build_id]
    elif o.build_id:  # only an id the operator gave, never one derived from the contract
        a.notes.append(
            "APPLY_BUILD_ID is not passed on: "
            f"APPLY_MODE {o.mode} runs no build (amend-and-build and replace-and-build do)"
        )
    if o.allow_data_loss:
        argv.append("--allow-data-loss")
    if o.no_verify_digest:
        argv += ["--no-verify-plan-binding", "--no-verify-federation"]
    a.argv = argv


def _policy_apply(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    if not ws.has_file(POLICY_BINDINGS):
        a.skip_reason = f"{POLICY_BINDINGS} does not exist (no policy bindings to apply)"
        a.unsupported = True
        return
    mode = o.policy_mode
    if dry_run_applied_nothing(o) and mode == "enforce":
        mode = "check"
        a.notes.append(
            "APPLY_MODE dry-run applied nothing: policy bindings are checked, not enforced"
        )
    a.argv = ["policy-apply", POLICY_BINDINGS, "--mode", mode]


def _verify(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    reason = _skip_after_dry_run(o, "there is nothing to verify")
    if reason:
        a.skip_reason = reason
        return
    a.argv = [
        "verify",
        BUNDLE_PATH,
        *([] if o.no_verify_strict else ["--strict"]),
        *_env_args(o),
        "--out",
        "runtime/verify-report.json",
    ]


def _publish(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    reason = _skip_after_dry_run(o, "there is no applied product to publish")
    if reason:
        a.skip_reason = reason
        return
    check_targets(o.targets)
    a.argv = [
        "publish",
        o.contract,
        *_env_args(o),
        "--format",
        "json",
        *[f"--target={t}" for t in o.targets],
    ]


def _schedule_sync(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    reason = _skip_after_dry_run(o, "there is no applied product to schedule")
    if reason:
        a.skip_reason = reason
    elif not o.scheduler:
        a.skip_reason = "SCHEDULER is blank: no scheduler to sync to"
        a.unsupported = True
    elif not ws.dir_has_entries(SCHEDULE_DIR):
        a.unsupported = True
        a.skip_reason = (
            f"no {SCHEDULE_DIR}/ DAGs to sync (stage 3 not run, or the contract declares "
            "no orchestration.engine and no scheduled build)"
        )
    else:
        argv = [
            "schedule-sync",
            "--scheduler",
            o.scheduler,
            "--dags-dir",
            SCHEDULE_DIR + "/",
            *_env_args(o),
            "--delete-scope",
            "product",
            "--report",
            SCHEDULE_SYNC_REPORT,
        ]
        for flag, value in (
            ("--destination", o.scheduler_destination),
            ("--environment-name", o.scheduler_environment_name),
            ("--location", o.scheduler_location),
            ("--workspace", o.scheduler_workspace),
        ):
            if value:
                argv += [flag, value]
        if o.schedule_sync_dry_run:
            argv.append("--dry-run")
        a.argv = argv


_BUILDERS: Dict[int, Callable[[StageAction, ShipOptions, Workspace], None]] = {
    1: _bundle,
    2: _validate,
    3: _generate_artifacts,
    4: _validate_artifacts,
    5: _diff,
    6: _plan,
    7: _apply,
    8: _policy_apply,
    9: _verify,
    10: _publish,
    11: _schedule_sync,
}


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def missing_input(num: int, ws: Workspace) -> Optional[str]:
    """``None`` when stage ``num`` has its inputs, else the message naming the
    stage that produces what is missing."""
    if num == 7 and not ws.has_file(PLAN_PATH):
        return f"stage 7 apply needs {PLAN_PATH}: run stage 6 (fluid ship --stage plan) first"
    if num in NEEDS_BUNDLE and not ws.has_file(BUNDLE_PATH):
        return (
            f"stage {num} {STAGE_NAMES[num]} needs {BUNDLE_PATH}: "
            "run stage 1 (fluid ship --stage bundle) first"
        )
    return None


def check_preconditions(
    selected: Sequence[int], opts: ShipOptions, root: Optional[Path] = None
) -> None:
    """Refuse up front, before any stage runs, when a selected stage's input is
    neither on disk nor produced by an earlier selected stage."""
    produced: Set[str] = set()
    for num in selected:
        ws = Workspace(root, produced)
        message = missing_input(num, ws)
        if message and not (num == 9 and dry_run_applied_nothing(opts)):
            # verify is skipped after a dry run, so it reads nothing
            raise ShipUsageError(message)
        if num == 4 and not ws.has_dir(ARTIFACTS_DIR) and opts.generate_artifacts:
            raise ShipUsageError(
                f"stage 4 validate-artifacts needs {ARTIFACTS_DIR}: "
                "run stage 3 (fluid ship --stage artifacts) first"
            )
        if num == 1:
            produced.add(BUNDLE_PATH)
        if num == 6:
            produced.add(PLAN_PATH)
        if num == 3 and opts.generate_artifacts:
            produced.add(ARTIFACTS_DIR.rstrip("/"))


def check_required_artifacts(
    selected: Sequence[int], not_selected: Mapping[int, str], opts: ShipOptions
) -> None:
    """Refuse ``--require-artifacts`` when this invocation cannot meet it: stage
    3 does not run here, or its ``--emit`` leaves a required key out. Artifacts
    an earlier run left in dist/artifacts/ never count."""
    if not opts.require_artifacts:
        return
    keys = ",".join(opts.require_artifacts)
    if 3 not in selected or not opts.generate_artifacts:
        why = not_selected.get(3) or "--no-generate-artifacts"
        raise ShipUsageError(
            f"--require-artifacts {keys} needs stage 3 generate-artifacts in this invocation, "
            f"and it does not run ({why})"
        )
    left_out = [k for k in opts.require_artifacts if not emits(opts, k)]
    if left_out:
        raise ShipUsageError(
            f"--require-artifacts {','.join(left_out)} is not in --emit {opts.emit}: "
            "stage 3 would not write it"
        )


# --------------------------------------------------------------------------
# Required artifacts: checked after stage 3 passes
# --------------------------------------------------------------------------


def forget_artifacts_manifest(root: Path) -> None:
    """Remove the MANIFEST an earlier run left, before stage 3 runs, so the one
    read afterwards is this run's. ``generate artifacts`` removes the
    subdirectories it writes itself; the MANIFEST it rewrites only at the end."""
    with contextlib.suppress(FileNotFoundError):
        (root / ARTIFACTS_MANIFEST).unlink()


def listed_file_problem(root: Path, files: Mapping[str, object], rel: str) -> Optional[str]:
    """Why ``rel`` (under dist/artifacts/) is not this run's output, or ``None``."""
    from fluid_build.cli._attestation import _sha256_file

    if rel not in files:
        return "not in its MANIFEST"
    base = root / ARTIFACTS_DIR
    path = base / rel
    try:
        # The MANIFEST is the checkout's: a key that leads out of dist/artifacts/
        # (``schedule/../..``, a symlinked directory) names nothing stage 3 wrote.
        if not path.resolve().is_relative_to(base.resolve()):
            return "outside dist/artifacts/"
        if path.is_symlink() or not path.is_file():
            return "listed in its MANIFEST but not on disk"
        if "sha256:" + _sha256_file(path) != files[rel]:
            return "on disk with a digest its MANIFEST does not list"
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop before 3.13
        return f"unreadable ({getattr(exc, 'strerror', None) or exc})"
    return None


def bundled_contract(root: Path) -> Dict[str, Any]:
    """The contract stage 1 bundled (``contract.resolved.yaml`` in the bundle,
    read without extracting). ``ValueError`` when the bundle cannot be read."""
    import tarfile

    import yaml

    from fluid_build.forge.core.bundle import read_tar_member_bounded

    try:
        with tarfile.open(root / BUNDLE_PATH, mode="r:gz") as tar:
            text = read_tar_member_bounded(tar, "contract.resolved.yaml")
        contract = yaml.safe_load(text.decode("utf-8"))
    except (OSError, KeyError, tarfile.TarError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read the contract in {BUNDLE_PATH}: {exc}") from None
    return contract if isinstance(contract, dict) else {}


def _bundle_ports(root: Path) -> List[str]:
    """The ``exposes[]`` ids of the contract stage 1 bundled, as the ODCS
    emitter names them (``exposeId``, else ``id``; an entry without either has
    no file). ``ValueError`` when the bundle cannot be read."""
    exposes = bundled_contract(root).get("exposes")
    ports: List[str] = []
    for expose in exposes if isinstance(exposes, list) else []:
        if isinstance(expose, dict):
            eid = expose.get("exposeId") or expose.get("id")
            if eid:
                ports.append(str(eid))
    return ports


def _odcs_problems(root: Path, files: Mapping[str, object]) -> List[str]:
    from fluid_build.providers._path_safety import safe_filename_stem

    try:
        ports = _bundle_ports(root)
    except ValueError as exc:
        return [f"odcs: {exc}"]
    if not ports:
        return ["odcs: the contract declares no exposes[] port, so there is no ODCS file"]
    problems = []
    for port in ports:
        # The name the ODCS writer gives the port's file (artifact_fanout._emit_odcs).
        rel = f"odcs/{safe_filename_stem(f'product.odcs.{port}', 'product.odcs')}.yaml"
        why = listed_file_problem(root, files, rel)
        if why:
            problems.append(f"odcs: port {port!r} has no ODCS ({ARTIFACTS_DIR}{rel} {why})")
    return problems


def _one_or_more(
    root: Path, files: Mapping[str, object], key: str, what: str, match: Callable[[str], bool]
) -> List[str]:
    found = sorted(rel for rel in files if match(rel))
    if not found:
        return [f"{key}: stage 3 wrote no {what}"]
    return [
        f"{key}: {ARTIFACTS_DIR}{rel} {why}"
        for rel, why in ((rel, listed_file_problem(root, files, rel)) for rel in found)
        if why
    ]


def _top_level(subdir: str, suffix: str) -> Callable[[str], bool]:
    def match(rel: str) -> bool:
        head, _, name = rel.partition("/")
        return head == subdir and "/" not in name and name.endswith(suffix)

    return match


def _key_problems(key: str, root: Path, files: Mapping[str, object]) -> List[str]:
    if key == "odcs":
        return _odcs_problems(root, files)
    if key == "odps-bitol":
        product = _top_level("odps-bitol", ".odps.yaml")
        return _one_or_more(root, files, key, "ODPS product (odps-bitol/*.odps.yaml)", product)
    if key == "opds":
        return _one_or_more(
            root, files, key, "OPDS document (opds/*.opds.json)", _top_level("opds", ".opds.json")
        )
    if key == "policies":
        why = listed_file_problem(root, files, "policy/bindings.json")
        return [f"policies: {POLICY_BINDINGS} {why}"] if why else []
    what = (
        "schedule file (orchestration.engine is none, or it is unset and no build has a "
        "schedule trigger)"
    )
    return _one_or_more(root, files, key, what, lambda rel: rel.startswith("schedule/"))


def missing_required_artifacts(opts: ShipOptions, root: Optional[Path] = None) -> List[str]:
    """Every ``--require-artifacts`` output this run's stage 3 did not write,
    one line each; ``[]`` when all are there. An output counts only when the
    MANIFEST stage 3 wrote lists it and the file on disk has the listed digest."""
    root = root or Path.cwd()
    if not opts.require_artifacts:
        return []
    try:
        manifest = json.loads((root / ARTIFACTS_MANIFEST).read_text(encoding="utf-8"))
        files = manifest["files"]
        if not isinstance(files, dict):
            raise TypeError("files is not a mapping")
    except FileNotFoundError:
        return [f"stage 3 wrote no {ARTIFACTS_MANIFEST}"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        return [f"{ARTIFACTS_MANIFEST} is not one generate artifacts writes ({exc})"]
    problems: List[str] = []
    for key in opts.require_artifacts:
        problems += _key_problems(key, root, files)
    return problems


def check_contract_path(path: str) -> None:
    """Refuse, before anything is written, a contract stage 1 would refuse: the
    input checks ``fluid bundle`` runs (``cli/security.validate_cli_path``: a
    symlinked file, a system path, ...), on the normalised path ship hands the
    stages. A symlink is refused, not followed: one planted in a checkout could
    point the run at a file outside it. The message becomes ship's own line."""
    from fluid_build.cli.core import FluidCLIError
    from fluid_build.cli.security import validate_cli_path

    try:
        with _quiet_probe():  # its path is the checkout's text: nothing is printed
            validate_cli_path(path, mode="read", file_type="contract")
    except FluidCLIError as exc:
        raise ShipUsageError(str(exc.message)) from None


def project_location(contract: str, cwd: Optional[str] = None) -> Tuple[str, str]:
    """``(directory to run in, contract path relative to it in POSIX form)``.

    A contract below the current directory keeps it as the project root, so a
    checkout-relative path such as ``products/a/contract.fluid.yaml`` is what
    ``generate artifacts --contract-path`` records. A contract anywhere else
    (``fluid ship /tmp/x/contract.fluid.yaml``, as Command Center calls it) runs in
    its own directory.

    Directories are compared resolved (macOS's /tmp is /private/tmp); the
    contract file is not, so :func:`check_contract_path` sees a symlinked
    contract as the link it is, and refuses it instead of following it.
    """
    base = os.path.realpath(cwd or os.getcwd())
    given = os.path.join(base, contract)
    directory = os.path.realpath(os.path.dirname(given))
    name = os.path.basename(given)
    try:
        rel = os.path.relpath(os.path.join(directory, name), base)
    except ValueError:  # another drive (Windows): never below the current directory
        return directory, name
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return directory, name
    return base, Path(rel).as_posix()
