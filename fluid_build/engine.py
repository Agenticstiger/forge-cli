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

"""High-level async facade over the 11-stage CLI pipeline.

Phase 1 of the engine-as-library refactor (FLUID master roadmap
"Move 10"). In-process consumers — the FLUID Command Center backend,
hosted runners, CI tooling — call these async functions instead of
shelling out to ``python -m fluid_build.cli``. The CLI itself remains
the canonical consumer; this module is a thin wrapper around the same
``cli/<stage>.run()`` functions the CLI binary already calls.

Phase 1 scope
-------------

* Async surface: every public callable is ``async``. The underlying
  stage code is synchronous + uses argparse internally; this wrapper
  runs each stage in ``asyncio.to_thread`` (no behavior change). One
  stage (``publish``) already had an ``async def run_async`` upstream
  — we wire to that directly without the thread hop.

* Eleven commands covered. Phase 1 shipped ``validate``, ``plan``,
  ``apply``, ``diff``, ``publish``; Phase 1.1 added the remaining
  six 11-stage pipeline commands (``bundle``, ``verify``,
  ``policy_apply``, ``generate_artifacts``, ``validate_artifacts``,
  ``schedule_sync``). ``fluid generate`` itself is a subcommand
  router; consumers call ``generate_artifacts`` directly today;
  ``generate_iac`` / ``generate_ci`` are sequenced for follow-ups.

* No new logic. No new validation. No new error semantics. Each
  wrapper builds an ``argparse.Namespace`` mimicking the CLI's
  expected shape, runs the stage, and returns a typed result
  carrying exit code + captured stdout/stderr + any artifacts the
  stage left on disk.

* Single package: ``fluid_forge_engine`` ships inside ``fluid-forge``
  (no separate distribution). Importable as ``from fluid_build.engine
  import validate, plan, apply, ...``. Matches Pulumi's
  ``pulumi.automation`` model.

Phase 2 (separate work) will refactor each stage's ``run()`` into a
typed pure function + a thin CLI shim, eliminating the
argparse.Namespace round-trip and producing structured outputs
directly from the stage logic. Phase 1 is deliberately faithful to
the existing CLI semantics so the cutover can be incremental.

Provider plugin contract is intentionally out of scope (master-
roadmap Domain 12).

Phase 1 limits — READ BEFORE INTEGRATING
----------------------------------------

The wrapper is a true thin pass-through. That means it inherits some
behaviour from the CLI that is sharp-edged when invoked in-process.
Each of these is documented per-stage too; this section is the
all-in-one summary.

1. **Concurrency**: stage calls **serialize through a process-wide
   lock** (``_STAGE_LOCK``). ``contextlib.redirect_stdout`` is process-
   global and not safe for concurrent threaded use — the lock is the
   pragmatic Phase 1 fix. Two ``await engine.validate(...)`` calls in
   the same process will not run in parallel even when wrapped in
   ``asyncio.gather``. Phase 2 (pure-function stages, structured
   output) removes the need for stdout capture and this lock.

2. **Cancellation is best-effort**: every wrapper except ``publish``
   dispatches via ``asyncio.to_thread``, and Python threads cannot be
   cancelled. When the awaiting task is cancelled, the stage thread
   keeps running — possibly for hours against a real cloud. Callers
   must assume side-effects continue after ``CancelledError``.

3. **``apply`` mutates ``os.environ``**: the CLI's apply stage loads
   process-CWD dotenv files into the process environment. The engine
   snapshots ``os.environ`` before the call and restores it after, but
   this is only safe under ``_STAGE_LOCK`` — don't bypass it.

4. **Kwarg names are stable from this PR forward**: the engine surface
   is now a public API. Adding new optional kwargs is fine; renaming or
   removing existing ones is a breaking change. Phase 2's pure-function
   refactor preserves these names where possible and marks any
   rename as a deliberate API break.

5. **Captured stderr includes log records**: the wrapper attaches a
   handler to ``fluid_build.cli.<stage>`` for the call duration so
   ``result.stderr`` carries the stage's ``logger.info / .warning /
   .error`` output alongside raw ``print()``. Captured stdout is
   ``print()``-only.

6. **Lazy stage imports**: each stage module is imported on first call.
   First ``apply`` pays the Rich + dbt-runner import cost (hundreds of
   ms); subsequent calls are fast.

Example
-------

::

    from pathlib import Path
    from fluid_build.engine import validate, ValidateResult

    async def check(contract_path: Path) -> bool:
        result: ValidateResult = await validate(contract_path)
        if not result.success:
            print(result.stderr)
            return False
        return True

See also
--------

* ``fluid_build/api/`` — the governed *extension* contract that third-
  party Provider/Runner implementations target. This module is the
  *consumer* contract, layered on top.
* ``cli/<stage>.py`` — the canonical implementation of each stage.
* docs/architecture/ENGINE-AS-LIBRARY.md in the Command Center repo
  for the broader rationale and migration plan.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional

__all__ = [
    # Exceptions
    "EngineError",
    "EngineUsageError",
    "ValidateFailed",
    "PlanFailed",
    "ApplyFailed",
    "DiffFailed",
    "PublishFailed",
    "BundleFailed",
    "VerifyFailed",
    "PolicyApplyFailed",
    "GenerateArtifactsFailed",
    "ValidateArtifactsFailed",
    "ScheduleSyncFailed",
    # Result types
    "StageResult",
    "ValidateResult",
    "PlanResult",
    "ApplyResult",
    "DiffResult",
    "PublishResult",
    "BundleResult",
    "VerifyResult",
    "PolicyApplyResult",
    "GenerateArtifactsResult",
    "ValidateArtifactsResult",
    "ScheduleSyncResult",
    # Public callables
    "validate",
    "plan",
    "apply",
    "diff",
    "publish",
    "bundle",
    "verify",
    "policy_apply",
    "generate_artifacts",
    "validate_artifacts",
    "schedule_sync",
]


# ── Concurrency and capture safety ────────────────────────────────────
#
# IMPORTANT FOR CONSUMERS
# -----------------------
#
# Phase 1's capture mechanism (``contextlib.redirect_stdout`` +
# ``redirect_stderr``) patches process-global ``sys.stdout`` / ``sys.stderr``.
# Python's contextlib docs state this is unsafe for "library code and
# most threaded applications" — exactly our situation.
#
# To avoid silent cross-contamination of captured output between concurrent
# in-process calls, the engine acquires a **process-wide lock**
# (``_STAGE_LOCK``) for the duration of each ``_run_sync_stage`` call.
# Calls into the engine therefore **serialize** through this lock — even
# when wrapped in ``asyncio.gather(...)``.
#
# Why a lock and not per-thread fd dup (à la ``wurlitzer``)? The fd-dup
# approach handles C-extension output too, but it's a complexity bump
# (per-thread pipes, drain threads, OS-specific fd handling) and a new
# dependency. Phase 1 prioritises correctness over throughput. Phase 2
# replaces this with per-stage pure functions that emit structured
# output directly — at which point neither stdout capture nor this lock
# is needed.
#
# Cancellation
# ------------
#
# ``await engine.apply(...)`` (and every other ``_run_sync_stage`` call)
# dispatches via ``asyncio.to_thread``. Python threads CANNOT be cancelled.
# When a consumer's task is cancelled (timeout, client disconnect, shutdown)
# the awaiter receives ``CancelledError`` but the underlying stage thread
# keeps running — possibly for hours against a real cloud. Provider
# connections stay open, side-effects keep happening.
#
# Cancellation is therefore **best-effort** in Phase 1. A future
# ``CancellationToken`` mechanism (Pulumi automation API style) is part of
# Phase 2's pure-function refactor.
#
# Multi-tenant env mutation in apply()
# ------------------------------------
#
# ``fluid_build.cli.apply.run`` calls ``hydrate_dotenv(Path.cwd(), ...)``
# which writes the **process's** CWD dotenv into ``os.environ``. In a
# server consumer (CC backend) running multiple tenants' applies, this
# would leak credentials across tenants. The engine's ``apply`` wrapper
# snapshots ``os.environ`` before the call and restores it after — safe
# under the process-wide lock above, racy if the lock is bypassed.
_STAGE_LOCK = threading.Lock()


# ── Result types ──────────────────────────────────────────────────────


@dataclass
class StageResult:
    """Generic result for an engine stage call.

    Attributes
    ----------
    exit_code
        Mirrors the CLI's return value: ``0`` = success, non-zero =
        failure. The :attr:`success` property is the canonical
        check; ``exit_code`` is also stored for callers who want to
        distinguish different failure modes (the CLI's exit codes
        are documented per-stage).
    artifacts
        Stage-specific structured outputs. Populated by the wrapper
        after the stage returns (e.g. ``plan.json`` parsed into a
        dict for the plan stage, validation report parsed into a
        dict for validate, etc.).
    stdout, stderr
        Verbatim captured CLI output (raw ``print()`` output PLUS log
        records emitted by the stage's own logger during the call).
        Useful for surfacing in audit logs, UIs, or for diagnosing
        failures.

    Note on ``_stage_name``
    ----------------------

    Each subclass declares ``_stage_name: ClassVar[str] = "..."`` so the
    name is a class-level constant — it is NOT a constructor argument
    and does NOT appear in ``repr`` or equality. ``raise_for_status``
    reads it to pick the right :class:`EngineError` subtype.
    """

    exit_code: int
    artifacts: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""

    # Subclasses override this; consulted by ``raise_for_status`` to
    # decide which typed exception to raise. ClassVar (not a dataclass
    # field) so it isn't a constructor arg and doesn't surface in
    # ``repr`` / equality / ``dataclasses.fields(...)``.
    _stage_name: ClassVar[str] = "unknown"

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def raise_for_status(self) -> None:
        """Raise the stage-specific :class:`EngineError` subclass if the
        stage failed. Mirrors ``requests.Response.raise_for_status``.

        The raised exception type is one of :class:`ValidateFailed`,
        :class:`PlanFailed`, :class:`ApplyFailed`, etc. — all of which
        subclass :class:`EngineError`, so callers can catch broadly
        (``except EngineError``) or narrowly (``except PlanFailed``).
        """
        if not self.success:
            exc_cls = _ERROR_CLASSES.get(self._stage_name, EngineError)
            raise exc_cls(self._stage_name, self)


@dataclass
class ValidateResult(StageResult):
    """Result of :func:`validate`. ``artifacts["report"]`` is the
    validation report dict when output_format="json"."""

    _stage_name: ClassVar[str] = "validate"


@dataclass
class PlanResult(StageResult):
    """Result of :func:`plan`. ``artifacts["plan"]`` is the parsed
    ``plan.json`` body when one was written."""

    _stage_name: ClassVar[str] = "plan"


@dataclass
class ApplyResult(StageResult):
    """Result of :func:`apply`."""

    _stage_name: ClassVar[str] = "apply"


@dataclass
class DiffResult(StageResult):
    """Result of :func:`diff`."""

    _stage_name: ClassVar[str] = "diff"


@dataclass
class PublishResult(StageResult):
    """Result of :func:`publish`. Publish already has an upstream
    async surface; this wrapper does not add a thread hop."""

    _stage_name: ClassVar[str] = "publish"


@dataclass
class BundleResult(StageResult):
    """Result of :func:`bundle`."""

    _stage_name: ClassVar[str] = "bundle"


@dataclass
class VerifyResult(StageResult):
    """Result of :func:`verify`. ``artifacts['report']`` is the parsed
    drift report when ``out=`` was supplied."""

    _stage_name: ClassVar[str] = "verify"


@dataclass
class PolicyApplyResult(StageResult):
    """Result of :func:`policy_apply`."""

    _stage_name: ClassVar[str] = "policy_apply"


@dataclass
class GenerateArtifactsResult(StageResult):
    """Result of :func:`generate_artifacts`. ``artifacts['manifest']``
    is the parsed ``MANIFEST.json`` from the output dir."""

    _stage_name: ClassVar[str] = "generate_artifacts"


@dataclass
class ValidateArtifactsResult(StageResult):
    """Result of :func:`validate_artifacts`. ``artifacts['report']`` is
    the parsed JSON report when ``report=`` was supplied."""

    _stage_name: ClassVar[str] = "validate_artifacts"


@dataclass
class ScheduleSyncResult(StageResult):
    """Result of :func:`schedule_sync`. ``artifacts['report']`` is the
    parsed sync result when ``report=`` was supplied."""

    _stage_name: ClassVar[str] = "schedule_sync"


# ── Exception hierarchy ───────────────────────────────────────────────


class EngineError(RuntimeError):
    """A stage exited non-zero. Carries the :class:`StageResult`.

    This is the base for every stage-specific failure (:class:`PlanFailed`,
    :class:`ApplyFailed`, etc.). Catch broadly (``except EngineError``) when
    you want any stage failure; catch narrowly (``except PlanFailed``) when
    you want to handle a specific stage's failure differently.

    Use ``raise EngineError(stage, result) from underlying`` to preserve a
    causal exception when one is available (Python's standard ``__cause__``
    chain).

    Security note for stage authors
    -------------------------------

    ``EngineError.__init__`` echoes the first 200 chars of captured
    stderr into the exception message. Every wrapped stage already
    routes secrets through ``observability.secret_redactor`` for
    logging, but if a stage emits secrets via ``print(..., file=
    sys.stderr)`` (which bypasses the logging filter chain), that
    output ends up verbatim in this exception string. The engine is
    creating a new path — exception messages — that may be re-logged
    or surfaced in API responses where a stack trace isn't expected.

    If you're writing a new stage: always route user-relevant output
    through the redacted ``logger``, never raw stderr.
    """

    def __init__(self, stage: str, result: StageResult) -> None:
        # First 200 chars of stderr in the message — enough for most
        # failure modes, full text available via ``self.result.stderr``.
        super().__init__(
            f"fluid_build.engine.{stage} exited {result.exit_code}: "
            f"{(result.stderr or '').strip()[:200]}"
        )
        self.stage = stage
        self.result = result


class EngineUsageError(ValueError):
    """Raised when a consumer calls the engine with arguments the wrapper
    itself rejects (e.g. ``engine.apply(..., yes=False, dry_run=False)``
    has no way to surface an interactive prompt through the in-process
    surface).

    This is a wrapper-level usage error, NOT a stage failure — the stage
    was never called. Subclasses ``ValueError`` (not :class:`EngineError`)
    because the issue is in the caller's invocation, not in the stage's
    logic. Catching ``EngineError`` will NOT catch usage errors.
    """


class ValidateFailed(EngineError):
    """``fluid validate`` exited non-zero."""


class PlanFailed(EngineError):
    """``fluid plan`` exited non-zero."""


class ApplyFailed(EngineError):
    """``fluid apply`` exited non-zero."""


class DiffFailed(EngineError):
    """``fluid diff`` exited non-zero (incl. exit-on-drift signal)."""


class PublishFailed(EngineError):
    """``fluid publish`` failed (catalog handshake, signing, etc.)."""


class BundleFailed(EngineError):
    """``fluid bundle`` exited non-zero."""


class VerifyFailed(EngineError):
    """``fluid verify`` exited non-zero (drift detected, strict mode)."""


class PolicyApplyFailed(EngineError):
    """``fluid policy-apply`` exited non-zero."""


class GenerateArtifactsFailed(EngineError):
    """``fluid generate artifacts`` exited non-zero."""


class ValidateArtifactsFailed(EngineError):
    """``fluid validate-artifacts`` exited non-zero (tamper / format)."""


class ScheduleSyncFailed(EngineError):
    """``fluid schedule-sync`` exited non-zero."""


# Resolved by ``StageResult.raise_for_status`` to pick a typed subtype.
# Unknown stages fall through to the base ``EngineError``.
_ERROR_CLASSES: dict[str, type[EngineError]] = {
    "validate": ValidateFailed,
    "plan": PlanFailed,
    "apply": ApplyFailed,
    "diff": DiffFailed,
    "publish": PublishFailed,
    "bundle": BundleFailed,
    "verify": VerifyFailed,
    "policy_apply": PolicyApplyFailed,
    "generate_artifacts": GenerateArtifactsFailed,
    "validate_artifacts": ValidateArtifactsFailed,
    "schedule_sync": ScheduleSyncFailed,
}


# ── Internal: argparse.Namespace builder + stage runner ───────────────


def _logger(stage: str) -> logging.Logger:
    """Per-stage logger. Records go to the engine's own namespace so
    consumers can filter without affecting the CLI's own log topics.

    Note: this is the logger the engine PASSES to ``stage_module.run(ns,
    logger)``. Many stages ignore it and use their own
    ``logging.getLogger(__name__)`` instead — those records are captured
    separately by :func:`_run_sync_stage`'s in-flight handler.
    """
    return logging.getLogger(f"fluid.engine.{stage}")


class _BufferingHandler(logging.Handler):
    """Logging handler that appends formatted records to a StringIO buffer.

    Used by :func:`_run_sync_stage` to capture ``logger.info / .warning /
    .error`` calls from the wrapped stage into ``result.stderr``. Without
    this, ``result.stderr`` only contains raw ``print(..., file=stderr)``
    output and misses the bulk of stage diagnostics, which go through
    ``logging``.

    Defaults to ``DEBUG`` level so the handler captures anything the
    logger lets through. The owning ``_run_sync_stage`` also lowers the
    stage logger's own effective level to ``DEBUG`` for the duration of
    the call (restored afterwards) — without that, the logger's own
    level filter (often inherited as ``WARNING`` from root) drops INFO
    records before they ever reach a handler.
    """

    def __init__(self, buffer: io.StringIO) -> None:
        super().__init__(level=logging.DEBUG)
        self._buffer = buffer
        # Format records inline so the buffer carries the level + message,
        # not the raw record object. Keep it minimal — consumers usually
        # surface this in a UI / API response.
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover - never let logging crash the stage
            self.handleError(record)


def _build_namespace(stage_module: Any, overrides: dict[str, Any]) -> argparse.Namespace:
    """Build an ``argparse.Namespace`` for ``stage_module.run()`` by
    asking the CLI's own argparse for the canonical default shape, then
    merging the engine caller's overrides on top.

    Why this matters: hand-rolling the namespace risks two failure modes
    that monkeypatched tests CANNOT catch:

    1. **Missing fields.** The CLI's ``run(args, logger)`` reads
       attributes directly (``args.clear_cache``, ``args.config_override``,
       …). If the engine doesn't set them, the real stage raises
       ``AttributeError`` the first time it consumes them.
    2. **Wrong field names.** The engine kwargs and the CLI's
       ``dest=`` don't always match (e.g. engine ``output=`` ↔ CLI
       ``dest="out"``, engine ``other=`` ↔ CLI ``dest="baseline"``).

    Letting the CLI's own ``register(subparsers)`` define the shape
    eliminates both classes of bug — the namespace is self-correcting
    and will pick up any new ``add_argument`` upstream without a wrapper
    change.

    The throwaway parser + ``parse_args([])`` is a known pattern; cost
    is negligible (one-time per call, no subprocess) and the wrapper
    stays a true thin pass-through.

    Unknown ``overrides`` keys (caller asked for something the CLI
    doesn't expose) are passed through onto the namespace anyway —
    they're harmless if the stage doesn't read them, surface naturally
    if the stage does. Future-proofs against the wrapper outpacing the
    CLI.
    """
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers()
    # Stages register either via ``register`` or ``register_subcommand``;
    # the former is the common case (top-level CLI command), the latter
    # is a router-subcommand entry (``generate_artifacts`` lives under
    # ``fluid generate``).
    if hasattr(stage_module, "register"):
        stage_module.register(subparsers)
    elif hasattr(stage_module, "register_subcommand"):
        stage_module.register_subcommand(subparsers)
    else:  # pragma: no cover - every stage exposes one of these
        raise RuntimeError(
            f"engine: stage {stage_module.__name__} has neither "
            "register() nor register_subcommand() — cannot derive args"
        )

    # parse_args([]) seeds every ``add_argument(default=...)`` plus
    # ``set_defaults(...)`` (which is how stages wire their ``func``
    # / ``cmd`` attributes). For stages with REQUIRED positional or
    # keyword args, argparse would exit(2); we suppress that by
    # constructing the namespace directly from the parser's actions.
    ns = argparse.Namespace()
    for action in parser._actions:  # noqa: SLF001 — argparse internals are stable
        if action.dest == "help":
            continue
        setattr(ns, action.dest, action.default)
    # Subparser ``set_defaults`` (func, cmd, generate_sub, …) live on
    # the chosen subparser's defaults, not the top-level parser's.
    # Find the registered subparser and merge its set_defaults values.
    for action in parser._actions:  # noqa: SLF001
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        for sub_name, sub_parser in action.choices.items():
            del sub_name  # only one is registered per stage
            for sub_action in sub_parser._actions:  # noqa: SLF001
                if sub_action.dest == "help":
                    continue
                setattr(ns, sub_action.dest, sub_action.default)
            for k, v in sub_parser._defaults.items():  # noqa: SLF001
                setattr(ns, k, v)
            break

    # Merge caller overrides last so they win.
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


async def _run_sync_stage(
    stage_name: str,
    stage_module: Any,
    ns: argparse.Namespace,
    result_cls: type[StageResult] = StageResult,
    *,
    env_snapshot: bool = False,
) -> StageResult:
    """Run a sync ``cli/<stage>.run(args, logger) -> int`` in a worker
    thread, capturing stdout/stderr/exit-code + the stage's log records.

    Concurrency safety
    ------------------

    Acquires :data:`_STAGE_LOCK` for the duration of the stage call. This
    is a process-wide lock — concurrent calls into the engine serialize
    through it. See the module docstring for rationale.

    Log-record capture
    ------------------

    Attaches a :class:`_BufferingHandler` to ``fluid_build.cli.<stage>``
    (the logger the stage modules actually use via
    ``logging.getLogger(__name__)``) for the duration of the call. Without
    this, ``result.stderr`` would only contain raw ``print()`` output and
    miss everything the stage emits through ``logger.info / .warning /
    .error``. The handler is detached in a ``finally`` so it doesn't leak.

    Env snapshot
    ------------

    When ``env_snapshot=True`` (set by :func:`apply`), takes a snapshot of
    ``os.environ`` before the call and restores it after. The ``apply``
    stage mutates ``os.environ`` via ``hydrate_dotenv``; without this
    restore, a tenant's dotenv leaks into subsequent calls.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    engine_log = _logger(stage_name)
    # The stage modules use ``logging.getLogger(__name__)`` — that's
    # ``fluid_build.cli.<stage>``, NOT the engine's per-stage logger.
    stage_logger_name = stage_module.__name__  # e.g. "fluid_build.cli.validate"
    stage_logger = logging.getLogger(stage_logger_name)
    capture_handler = _BufferingHandler(stderr_buf)

    def _invoke() -> int:
        stage_logger.addHandler(capture_handler)
        # Lower the stage logger's effective level so INFO/DEBUG records
        # reach the handler. Without this, a logger inheriting WARNING
        # from root would filter INFO before it ever hit the buffer.
        # Restored in the ``finally`` so we don't leave the logger noisy.
        previous_level = stage_logger.level
        stage_logger.setLevel(logging.DEBUG)
        env_backup: dict[str, str] | None = None
        if env_snapshot:
            env_backup = os.environ.copy()
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                return stage_module.run(ns, engine_log)
        finally:
            stage_logger.setLevel(previous_level)
            stage_logger.removeHandler(capture_handler)
            if env_backup is not None:
                # Restore env atomically: clear new keys the stage added
                # then re-apply the snapshot.
                added = set(os.environ) - set(env_backup)
                for key in added:
                    os.environ.pop(key, None)
                for key, value in env_backup.items():
                    if os.environ.get(key) != value:
                        os.environ[key] = value

    def _locked_invoke() -> int:
        with _STAGE_LOCK:
            return _invoke()

    exit_code = await asyncio.to_thread(_locked_invoke)
    return result_cls(
        exit_code=exit_code,
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
    )


# ── Public surface: one async function per stage ──────────────────────


async def validate(
    contract: Path | str,
    *,
    env: Optional[str] = None,
    schema_version: Optional[str] = None,
    min_version: Optional[str] = None,
    max_version: Optional[str] = None,
    strict: bool = False,
    cache_dir: Optional[Path] = None,
    output_format: str = "json",
    verbose: bool = False,
    show_schema: bool = False,
    list_versions: bool = False,
) -> ValidateResult:
    """Run ``fluid validate`` in-process.

    Returns a :class:`ValidateResult`. When ``output_format="json"``,
    ``result.artifacts["report"]`` carries the parsed validation
    report; otherwise the result is text in ``result.stdout``.
    """
    from fluid_build.cli import validate as _validate

    ns = _build_namespace(
        _validate,
        {
            "contract": str(contract),
            "env": env,
            "schema_version": schema_version,
            "min_version": min_version,
            "max_version": max_version,
            "strict": strict,
            "cache_dir": cache_dir,
            "format": output_format,
            "verbose": verbose,
            "show_schema": show_schema,
            "list_versions": list_versions,
        },
    )
    result = await _run_sync_stage("validate", _validate, ns, ValidateResult)
    # Best-effort parse of the JSON report if the stage produced one.
    if output_format == "json" and result.stdout.strip():
        try:
            result.artifacts["report"] = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Stage emitted non-JSON despite format=json — surfaced as
            # raw stdout, not an engine error.
            pass
    return result


async def plan(
    contract: Path | str,
    *,
    env: Optional[str] = None,
    provider: Optional[str] = None,
    project: Optional[str] = None,
    region: Optional[str] = None,
    output: Optional[Path] = None,
    estimate_cost: bool = False,
) -> PlanResult:
    """Run ``fluid plan`` in-process.

    Returns a :class:`PlanResult`. If a ``plan.json`` is written
    (default behavior unless suppressed by stage flags), it is read
    back and surfaced as ``result.artifacts["plan"]``.

    ``estimate_cost`` maps to the CLI's ``--cost-estimate`` flag. The
    pre-namespace-fix engine had separate ``cost_estimate``,
    ``fluid_version``, and ``no_verify_digest`` kwargs that didn't
    match CLI dest names — removed for accuracy; let the CLI's
    argparse define what's available.
    """
    from fluid_build.cli import plan as _plan

    overrides: dict[str, Any] = {
        "contract": str(contract),
        "env": env,
        "provider": provider,
        "project": project,
        "region": region,
        # CLI uses ``dest="out"`` (not "output"); auto-namespace handles
        # the default, we override only when caller supplied one.
        "estimate_cost": estimate_cost,
    }
    if output is not None:
        overrides["out"] = str(output)
    ns = _build_namespace(_plan, overrides)
    # Wall-clock floor for the readback freshness check below. The stage
    # runs after this line, so anything older than this timestamp is a
    # stale ``plan.json`` from a previous run and must NOT be surfaced
    # as this call's artifact (security review observation: stale
    # readback could mislead the caller into thinking the failed call
    # produced a current plan).
    import time as _time

    started_at = _time.time()

    result = await _run_sync_stage("plan", _plan, ns, PlanResult)

    # Readback gated on (a) successful exit AND (b) the file's mtime
    # being >= the moment we started — together they prove this call
    # actually produced the file.
    if not result.success:
        return result

    plan_path = output if output else None
    if plan_path is None:
        # Default path the CLI writes to is ./plan.json next to the contract.
        try:
            default_path = Path(contract).parent / "plan.json"
            if default_path.exists():
                plan_path = default_path
        except (TypeError, OSError):
            # ``contract`` may be a non-path-like or unreadable; the
            # artifact readback is best-effort and never blocks the
            # caller. The stage's exit code is the authoritative signal.
            logging.getLogger("fluid.engine.plan").debug(
                "could not derive default plan_path next to %s", contract, exc_info=True
            )
    if plan_path and Path(plan_path).exists():
        try:
            stat = Path(plan_path).stat()
            # Allow 1s slack — filesystem timestamps can lag the wall
            # clock by sub-second amounts depending on the filesystem.
            if stat.st_mtime + 1.0 < started_at:
                logging.getLogger("fluid.engine.plan").debug(
                    "skipping stale plan.json at %s (mtime older than call start)", plan_path
                )
            else:
                result.artifacts["plan"] = json.loads(Path(plan_path).read_text())
                result.artifacts["plan_path"] = str(plan_path)
        except (json.JSONDecodeError, OSError):
            # Stage wrote something at plan_path but it isn't valid
            # JSON or the file disappeared between exists() and read.
            # Surfaced as missing artifact, not an engine error.
            logging.getLogger("fluid.engine.plan").debug(
                "could not parse plan_path=%s", plan_path, exc_info=True
            )
    return result


async def apply(
    contract: Path | str,
    *,
    env: Optional[str] = None,
    dry_run: bool = True,
    yes: bool = False,
    rollback_strategy: Optional[str] = None,
    bundle: Optional[Path] = None,
    mode: Optional[str] = None,
    no_verify_plan_binding: bool = False,
    provider: Optional[str] = None,
    project: Optional[str] = None,
    region: Optional[str] = None,
) -> ApplyResult:
    """Run ``fluid apply`` in-process.

    Defaults to ``dry_run=True`` so a bare ``await engine.apply(...)``
    call is safe — same conservative default the CC backend uses for
    its durable apply endpoint.

    Required combinations
    ---------------------

    A non-dry-run call MUST explicitly opt out of the interactive prompt
    via ``yes=True``. The wrapped CLI calls ``input("Proceed? [y/N]: ")``
    when ``yes=False`` and the apply isn't dry-run; from an in-process
    consumer (CC backend, notebook, etc.) the prompt either never
    surfaces (stdout is redirected into the result buffer) or blocks
    on stdin forever. The engine refuses the combination with an
    :class:`EngineUsageError`. Callers that want an interactive flow
    should wrap their own confirmation around the call.

    Env safety
    ----------

    The wrapped ``cli.apply.run`` calls ``hydrate_dotenv(Path.cwd(), ...)``
    which writes the process CWD's dotenv into ``os.environ``. In a
    multi-tenant server this would leak credentials across tenants. The
    engine snapshots ``os.environ`` before the call and restores it on
    exit — safe under the process-wide stage lock, racy if that lock is
    bypassed (don't).

    Additional CLI flags (``--config-override``, ``--timeout``,
    ``--parallel-phases``, ``--notify``, ``--metrics-export``,
    ``--debug``, ``--workspace-dir``, ``--state-file``,
    ``--provider-config``, ``--report``, ``--build-id``, …) all default
    correctly via :func:`_build_namespace`. The engine surface
    intentionally narrows the typed kwargs to the ones the Command
    Center actually drives. Pass extras through a follow-up PR if a new
    use case needs them.
    """
    if not dry_run and not yes:
        raise EngineUsageError(
            "engine.apply(...) with dry_run=False requires yes=True. "
            "The in-process surface has no way to surface the CLI's "
            "interactive 'Proceed? [y/N]:' prompt. Pass yes=True when "
            "you're sure, or keep dry_run=True for a preview."
        )

    from fluid_build.cli import apply as _apply

    overrides: dict[str, Any] = {
        "contract": str(contract),
        "env": env,
        "dry_run": dry_run,
        "yes": yes,
        "rollback_strategy": rollback_strategy,
        "mode": mode,
        "no_verify_plan_binding": no_verify_plan_binding,
        "provider": provider,
        "project": project,
        "region": region,
    }
    if bundle is not None:
        overrides["bundle"] = str(bundle)
    ns = _build_namespace(_apply, overrides)
    return await _run_sync_stage("apply", _apply, ns, ApplyResult, env_snapshot=True)


async def diff(
    contract: Path | str,
    baseline: Path | str,
    *,
    state: Optional[Path | str] = None,
    out: Optional[Path | str] = None,
    exit_on_drift: bool = False,
) -> DiffResult:
    """Run ``fluid diff`` in-process.

    Per the CLI surface: ``contract`` is the candidate, ``baseline`` is
    what to compare against. Pre-namespace-fix engine called these
    ``contract_a`` / ``contract_b`` and passed them as ``other=`` —
    the CLI's argparse uses ``dest="baseline"`` so the previous shape
    failed with AttributeError.

    Drift mode reads ``--state`` against deployed resources; pass
    ``state=<path>`` to enable it.
    """
    from fluid_build.cli import diff as _diff

    overrides: dict[str, Any] = {
        "contract": str(contract),
        "baseline": str(baseline),
        "exit_on_drift": exit_on_drift,
    }
    if state is not None:
        overrides["state"] = str(state)
    if out is not None:
        overrides["out"] = str(out)
    ns = _build_namespace(_diff, overrides)
    result = await _run_sync_stage("diff", _diff, ns, DiffResult)
    # If the stage wrote to ``--out``, parse it back. Otherwise check
    # stdout for a JSON document (the CLI default prints to stdout).
    out_path = out if out is not None else None
    if out_path is not None and Path(out_path).exists():
        try:
            result.artifacts["diff"] = json.loads(Path(out_path).read_text())
            result.artifacts["diff_path"] = str(out_path)
        except (json.JSONDecodeError, OSError):
            logging.getLogger("fluid.engine.diff").debug(
                "could not parse diff out=%s", out_path, exc_info=True
            )
    elif result.stdout.strip():
        try:
            result.artifacts["diff"] = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Stage emitted non-JSON to stdout — keep the raw text in
            # result.stdout, leave artifacts['diff'] unset.
            logging.getLogger("fluid.engine.diff").debug(
                "diff stage stdout was not valid JSON", exc_info=True
            )
    return result


async def publish(
    contract: Path | str,
    *,
    catalog: Optional[str] = None,
    dry_run: bool = False,
    verify_only: bool = False,
    skip_health_check: bool = False,
    verbose: bool = False,
    endpoint_override: Optional[str] = None,
    config: Optional[Any] = None,
) -> PublishResult:
    """Run ``fluid publish`` in-process.

    Unlike the other stages, ``publish`` already exposes an async
    ``publish_contract`` function in the CLI module — we call it
    directly instead of going through ``asyncio.to_thread``.

    Config caching
    --------------

    When ``config`` is ``None`` the wrapper instantiates a fresh
    ``FluidConfig()`` per call. ``FluidConfig.__init__`` scans ``$HOME``,
    CWD, ``$FLUID_CONFIG``, and environment variables — non-trivial cost
    for tight publish loops. Callers that publish many contracts should
    construct one ``FluidConfig`` and pass it via this kwarg.

    Multi-tenant note
    -----------------

    ``FluidConfig`` resolves credentials from process-global ambient
    state (``$HOME`` / CWD / ``$FLUID_CONFIG`` / env). In a multi-tenant
    CC backend, the default behaviour publishes with the SERVER's
    identity, not a per-request operator identity. This is intended
    today (the CC's publish endpoint is admin-gated and the server
    credential is the canonical "publish on behalf of the org" identity),
    but it's worth surfacing here because the other stages have no such
    ambient state. If/when the CC introduces per-operator publish
    credentials, callers should pass an explicit ``config`` per request.

    Cancellation
    ------------

    Because ``publish_contract`` is natively async (no thread hop),
    ``CancelledError`` propagates cleanly through this wrapper IF
    ``publish_contract`` itself observes cancellation in its inner
    awaits. Other wrappers (which dispatch via ``asyncio.to_thread``)
    cannot be cancelled cleanly — see module docstring.
    """
    from fluid_build.cli import publish as _publish

    log = _logger("publish")

    if config is None:
        from fluid_build.config_manager import FluidConfig

        config = FluidConfig()

    # Note: publish does NOT acquire ``_STAGE_LOCK`` because it bypasses
    # the to_thread dispatch and ``publish_contract`` is natively async.
    # Concurrent publish calls in the same process would still hit the
    # stdout-redirect contamination issue, so we use a fd-scoped capture
    # via per-call buffers but document that concurrent publishes share
    # process-global stdout/stderr.
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    stage_logger = logging.getLogger(_publish.__name__)
    capture_handler = _BufferingHandler(stderr_buf)
    stage_logger.addHandler(capture_handler)
    try:
        with _STAGE_LOCK:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                upstream_result = await _publish.publish_contract(
                    contract_path=Path(contract),
                    catalog_name=catalog or "default",
                    config=config,
                    dry_run=dry_run,
                    verify_only=verify_only,
                    skip_health_check=skip_health_check,
                    verbose=verbose,
                    endpoint_override=endpoint_override,
                )
        success = bool(getattr(upstream_result, "success", False))
        return PublishResult(
            exit_code=0 if success else 1,
            artifacts={"publish": upstream_result},
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue(),
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        # Let these propagate — they're not stage failures, they're
        # process-level signals or async cancellation.
        raise
    except Exception as exc:  # noqa: BLE001 - upstream failure surfacing
        log.exception("publish failed")
        return PublishResult(
            exit_code=2,
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue() + f"\n{type(exc).__name__}: {exc}\n",
        )
    finally:
        stage_logger.removeHandler(capture_handler)


# ── Phase 1.1: remaining 11-stage commands ────────────────────────────


async def bundle(
    contract: Path | str | None = None,
    *,
    out: Optional[Path | str] = None,
    env: Optional[str] = None,
    output_format: Optional[str] = None,
    sign: bool = False,
    sign_key: Optional[str] = None,
    attest: bool = False,
) -> BundleResult:
    """Run ``fluid bundle`` in-process.

    Resolves $ref pointers and emits a single bundled contract (yaml/
    json/tgz). The ``tgz`` format is the canonical input to every
    downstream stage of the 11-stage pipeline; choose it for any
    pipeline-driving use. Set ``contract=None`` to let the stage
    auto-find ``contract.fluid.yaml`` in the CWD.
    """
    from fluid_build.cli import bundle as _bundle

    overrides: dict[str, Any] = {
        "env": env,
        "format": output_format,
        "sign": sign,
        "sign_key": sign_key,
        "attest": attest,
    }
    if contract is not None:
        overrides["contract"] = str(contract)
    if out is not None:
        overrides["out"] = str(out)
    ns = _build_namespace(_bundle, overrides)
    result = await _run_sync_stage("bundle", _bundle, ns, BundleResult)
    if out is not None and out != "-":
        result.artifacts["bundle_path"] = str(out)
    return result


async def verify(
    contract: Path | str,
    *,
    expose_id: Optional[str] = None,
    strict: bool = False,
    out: Optional[Path | str] = None,
    show_diffs: bool = False,
    env: Optional[str] = None,
) -> VerifyResult:
    """Run ``fluid verify`` in-process.

    Stage-9 of the 11-stage pipeline. Re-reads the deployed resources
    and compares against the contract's declared shape; surfaces drift.
    When ``out`` is supplied, the JSON drift report is parsed into
    ``result.artifacts['report']``.
    """
    from fluid_build.cli import verify as _verify

    overrides: dict[str, Any] = {
        "contract": str(contract),
        "expose_id": expose_id,
        "strict": strict,
        "show_diffs": show_diffs,
        "env": env,
    }
    if out is not None:
        overrides["out"] = str(out)
    ns = _build_namespace(_verify, overrides)
    result = await _run_sync_stage("verify", _verify, ns, VerifyResult)
    if out is not None and Path(out).exists():
        try:
            result.artifacts["report"] = json.loads(Path(out).read_text())
            result.artifacts["report_path"] = str(out)
        except (json.JSONDecodeError, OSError):
            logging.getLogger("fluid.engine.verify").debug(
                "could not parse verify report=%s", out, exc_info=True
            )
    return result


async def policy_apply(
    bindings: Path | str,
    *,
    mode: str = "check",
) -> PolicyApplyResult:
    """Run ``fluid policy-apply`` (alias of ``fluid policy apply``) in-process.

    Stage-8 of the 11-stage pipeline. Applies compiled IAM bindings
    (the output of ``fluid policy compile``). ``mode`` is ``"check"``
    (dry-run, the safe default) or ``"enforce"`` (write the bindings
    to the provider).
    """
    from fluid_build.cli import policy_apply as _policy_apply

    if mode not in ("check", "enforce"):
        raise ValueError(f"policy_apply mode must be 'check' or 'enforce', got {mode!r}")

    ns = _build_namespace(_policy_apply, {"bindings": str(bindings), "mode": mode})
    return await _run_sync_stage("policy_apply", _policy_apply, ns, PolicyApplyResult)


async def generate_artifacts(
    bundle: Path | str,
    *,
    out: Optional[Path | str] = None,
    emit: Optional[str] = None,
    manifest: Optional[Path | str] = None,
) -> GenerateArtifactsResult:
    """Run ``fluid generate artifacts`` in-process.

    Stage-3 of the 11-stage pipeline. Reads a Phase-2 bundle (.tgz) or
    a raw resolved contract (.yaml) and emits ODPS v4.1, ODPS-Bitol,
    ODCS per-port, OPDS, schedule DAGs, and compiled policy bindings
    into the output directory with a unified ``MANIFEST.json``.
    """
    from fluid_build.cli import generate_artifacts as _gen_artifacts

    out_dir = str(out) if out is not None else "dist/artifacts"
    manifest_path = str(manifest) if manifest is not None else str(Path(out_dir) / "MANIFEST.json")
    ns = _build_namespace(
        _gen_artifacts,
        {
            "bundle": str(bundle),
            "out": out_dir,
            "emit": emit,
            "manifest": manifest_path,
        },
    )
    result = await _run_sync_stage(
        "generate_artifacts", _gen_artifacts, ns, GenerateArtifactsResult
    )
    if Path(manifest_path).exists():
        try:
            result.artifacts["manifest"] = json.loads(Path(manifest_path).read_text())
            result.artifacts["manifest_path"] = manifest_path
        except (json.JSONDecodeError, OSError):
            logging.getLogger("fluid.engine.generate_artifacts").debug(
                "could not parse manifest=%s", manifest_path, exc_info=True
            )
    return result


async def validate_artifacts(
    artifacts_dir: Path | str,
    *,
    manifest: Optional[Path | str] = None,
    opa_policy_dir: str = "tests/policies",
    report: Optional[Path | str] = None,
    strict: bool = False,
    fail_fast: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    output_format: str = "text",
) -> ValidateArtifactsResult:
    """Run ``fluid validate-artifacts`` in-process.

    Stage-4 of the 11-stage pipeline. Re-verifies every file's SHA-256
    against ``MANIFEST.json`` (tamper gate), then runs per-format
    validators. Optional OPA conftest + dbt parse when those tools
    are on PATH.
    """
    from fluid_build.cli import validate_artifacts as _validate_artifacts

    overrides: dict[str, Any] = {
        "artifacts_dir": str(artifacts_dir),
        "opa_policy_dir": opa_policy_dir,
        "strict": strict,
        "fail_fast": fail_fast,
        "verbose": verbose,
        "quiet": quiet,
        "format": output_format,
    }
    if manifest is not None:
        overrides["manifest"] = str(manifest)
    if report is not None:
        overrides["report"] = str(report)
    ns = _build_namespace(_validate_artifacts, overrides)
    result = await _run_sync_stage(
        "validate_artifacts", _validate_artifacts, ns, ValidateArtifactsResult
    )
    if report is not None and Path(report).exists():
        try:
            result.artifacts["report"] = json.loads(Path(report).read_text())
            result.artifacts["report_path"] = str(report)
        except (json.JSONDecodeError, OSError):
            logging.getLogger("fluid.engine.validate_artifacts").debug(
                "could not parse report=%s", report, exc_info=True
            )
    return result


async def schedule_sync(
    *,
    scheduler: str,
    dags_dir: Path | str,
    destination: Optional[str] = None,
    environment_name: Optional[str] = None,
    location: Optional[str] = None,
    workspace: Optional[str] = None,
    env: str = "dev",
    dry_run: bool = True,
    timeout: int = 600,
    report: Optional[Path | str] = None,
    bundle_path: Optional[Path | str] = None,
    verify_signature: bool = False,
    verify_key: Optional[str] = None,
) -> ScheduleSyncResult:
    """Run ``fluid schedule-sync`` in-process.

    Stage-11 of the 11-stage pipeline. Pushes generated DAG files to a
    scheduler endpoint. ``dry_run`` defaults to ``True`` to match the
    safe-by-default ergonomics of the apply wrapper.
    """
    from fluid_build.cli import schedule_sync as _schedule_sync

    overrides: dict[str, Any] = {
        "scheduler": scheduler,
        "dags_dir": str(dags_dir),
        "destination": destination,
        "environment_name": environment_name,
        "location": location,
        "workspace": workspace,
        "env": env,
        "dry_run": dry_run,
        "timeout": timeout,
        "verify_signature": verify_signature,
        "verify_key": verify_key,
    }
    if report is not None:
        overrides["report"] = str(report)
    if bundle_path is not None:
        overrides["bundle"] = str(bundle_path)
    ns = _build_namespace(_schedule_sync, overrides)
    result = await _run_sync_stage("schedule_sync", _schedule_sync, ns, ScheduleSyncResult)
    if report is not None and Path(report).exists():
        try:
            result.artifacts["report"] = json.loads(Path(report).read_text())
            result.artifacts["report_path"] = str(report)
        except (json.JSONDecodeError, OSError):
            logging.getLogger("fluid.engine.schedule_sync").debug(
                "could not parse report=%s", report, exc_info=True
            )
    return result
