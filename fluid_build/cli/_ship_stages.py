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
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
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

BUNDLE_PATH = "runtime/bundle.tgz"
PLAN_PATH = "runtime/plan.json"
ARTIFACTS_DIR = "dist/artifacts/"
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

#: (number, name, aliases). The names are what ``--stage`` / ``--until`` /
#: ``--skip-stage`` accept next to the number.
STAGES: Tuple[Tuple[int, str, Tuple[str, ...]], ...] = (
    (1, "bundle", ()),
    (2, "validate", ()),
    (3, "generate-artifacts", ("artifacts", "generate_artifacts")),
    (4, "validate-artifacts", ("validate_artifacts",)),
    (5, "diff", ()),
    (6, "plan", ()),
    (7, "apply", ()),
    (8, "policy-apply", ("policy", "policy_apply")),
    (9, "verify", ()),
    (10, "publish", ()),
    (11, "schedule-sync", ("schedule_sync",)),
)
STAGE_NUMBERS: Tuple[int, ...] = tuple(s[0] for s in STAGES)
STAGE_NAMES: Dict[int, str] = {num: name for num, name, _ in STAGES}

#: Stages that read runtime/bundle.tgz (stage 7 reads it too, beside the plan).
NEEDS_BUNDLE = frozenset({2, 3, 5, 6, 7, 9})
OPT_IN_FLAG = {10: "--publish", 11: "--schedule-sync"}


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
    generate_artifacts: bool = True
    targets: List[str] = field(default_factory=lambda: [DEFAULT_PUBLISH_TARGET])
    scheduler: str = ""
    scheduler_destination: str = ""
    scheduler_environment_name: str = ""
    scheduler_location: str = ""
    scheduler_workspace: str = ""
    schedule_sync_dry_run: bool = False
    skip_bundle: bool = False


@dataclass
class StageAction:
    """What one stage does right now: run ``argv``, or skip with ``skip_reason``."""

    num: int
    name: str
    argv: Optional[List[str]] = None
    skip_reason: Optional[str] = None
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

    return ShipOptions(
        contract=contract,
        env=env_name,
        mode=mode,
        build_id=build_id,
        yes=bool(get("yes")),
        strict=bool(get("strict", True)),
        allow_data_loss=_truth(bool(get("allow_data_loss")), env, "ALLOW_DATA_LOSS"),
        no_verify_digest=_truth(bool(get("no_verify_digest")), env, "NO_VERIFY_DIGEST"),
        no_verify_strict=bool(get("no_verify_strict")),
        generate_artifacts=not get("no_generate_artifacts"),
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
    )


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
    if text.isdigit() and int(text) in STAGE_NAMES:
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
    """``--stage`` when given; else 1-9, plus the opt-in stages that were asked for."""
    if stage_spec is not None:
        return set(parse_stage_spec(stage_spec))
    return set(range(1, 10)) | ({10} if publish else set()) | ({11} if schedule_sync else set())


def _why_not_chosen(num: int, stage_spec: Optional[str]) -> str:
    if stage_spec is not None:
        return "not in --stage"
    if num in OPT_IN_FLAG:
        return f"opt-in: pass {OPT_IN_FLAG[num]}"
    return "not selected"


def select_stages(
    *,
    publish: bool,
    schedule_sync: bool,
    stage_spec: Optional[str] = None,
    until: Optional[str] = None,
    skip_stages: Sequence[str] = (),
) -> Tuple[List[int], Dict[int, str]]:
    """The stage numbers to consider, plus why each other stage is not in the set."""
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


def _skip_after_dry_run(opts: ShipOptions, what: str) -> Optional[str]:
    if opts.mode == "dry-run":
        return f"APPLY_MODE dry-run applied nothing, so {what}"
    return None


@contextlib.contextmanager
def _quiet_probe() -> Iterator[None]:
    """Ship's own look into the contract (is it reference-only? its one build?)
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


def _contract_reference_only(contract: str) -> bool:
    from fluid_build.cli.generate_ci import _contract_is_reference_only

    with _quiet_probe():
        return bool(_contract_is_reference_only(contract))


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
    a.argv = ["validate", BUNDLE_PATH, *_env_args(o), *(["--strict"] if o.strict else [])]


def _generate_artifacts(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    if not o.generate_artifacts:
        a.skip_reason = "--no-generate-artifacts"
    elif _contract_reference_only(o.contract):
        a.skip_reason = "reference-only contract: nothing to generate"
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
        ]


def _validate_artifacts(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    if not ws.has_dir(ARTIFACTS_DIR):
        a.skip_reason = f"{ARTIFACTS_DIR} does not exist (stage 3 wrote nothing)"
        return
    a.argv = ["validate-artifacts", ARTIFACTS_DIR, *(["--strict"] if o.strict else [])]


def _diff(a: StageAction, o: ShipOptions, ws: Workspace) -> None:
    a.argv = [
        "diff",
        BUNDLE_PATH,
        "--exit-on-drift",
        *_env_args(o),
        "--out",
        "runtime/diff-report.json",
    ]


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
        return
    mode = "enforce"
    if o.mode == "dry-run":
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
    elif not ws.dir_has_entries(SCHEDULE_DIR):
        a.skip_reason = (
            f"no {SCHEDULE_DIR}/ DAGs to sync (reference-only contract, stage 3 not run, "
            "or no orchestration.engine configured)"
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
        if message and not (num == 9 and opts.mode == "dry-run"):
            # verify is skipped after a dry run, so it reads nothing
            raise ShipUsageError(message)
        if num == 4 and not ws.has_dir(ARTIFACTS_DIR) and _generation_expected(opts):
            raise ShipUsageError(
                f"stage 4 validate-artifacts needs {ARTIFACTS_DIR}: "
                "run stage 3 (fluid ship --stage artifacts) first"
            )
        if num == 1:
            produced.add(BUNDLE_PATH)
        if num == 6:
            produced.add(PLAN_PATH)
        if num == 3 and _generation_expected(opts):
            produced.add(ARTIFACTS_DIR.rstrip("/"))


def _generation_expected(opts: ShipOptions) -> bool:
    """Stage 3 will write dist/artifacts/ (it is on and the contract is not
    reference-only). When it is not, stage 4 skips with a reason instead."""
    return opts.generate_artifacts and not _contract_reference_only(opts.contract)


def project_location(contract: str, cwd: Optional[str] = None) -> Tuple[str, str]:
    """``(directory to run in, contract path relative to it in POSIX form)``.

    A contract below the current directory keeps it as the project root, so a
    checkout-relative path such as ``products/a/contract.fluid.yaml`` is what
    ``generate artifacts --contract-path`` records. A contract anywhere else
    (``fluid ship /tmp/x/contract.fluid.yaml``, as Command Center calls it) runs in
    its own directory.
    """
    base = os.path.realpath(cwd or os.getcwd())
    target = os.path.realpath(os.path.join(base, contract))
    rel = os.path.relpath(target, base)
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return os.path.dirname(target), os.path.basename(target)
    return base, Path(rel).as_posix()
