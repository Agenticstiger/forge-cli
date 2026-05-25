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

* Five commands covered: ``validate``, ``plan``, ``apply``, ``diff``,
  ``publish``. These are the stages present in the released PyPI
  package (``fluid-forge==0.7.9``) and the highest-leverage targets
  for in-process consumers. The other 6 stages (``bundle``,
  ``generate``, ``generate-artifacts``, ``validate-artifacts``,
  ``policy-apply``, ``verify``, ``schedule-sync``) will land in Phase
  1.1 once they ship to PyPI.

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
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "EngineError",
    "StageResult",
    "ValidateResult",
    "PlanResult",
    "ApplyResult",
    "DiffResult",
    "PublishResult",
    "validate",
    "plan",
    "apply",
    "diff",
    "publish",
]


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
        Verbatim captured CLI output. Useful for surfacing in audit
        logs, UIs, or for diagnosing failures.
    """

    exit_code: int
    artifacts: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def raise_for_status(self) -> None:
        """Raise ``EngineError`` if the stage failed. Mirrors the
        ``requests.Response.raise_for_status`` ergonomics."""
        if not self.success:
            stage = getattr(self, "_stage_name", "unknown")
            raise EngineError(stage, self)


@dataclass
class ValidateResult(StageResult):
    """Result of :func:`validate`. ``artifacts["report"]`` is the
    validation report dict when output_format="json"."""

    _stage_name: str = "validate"


@dataclass
class PlanResult(StageResult):
    """Result of :func:`plan`. ``artifacts["plan"]`` is the parsed
    ``plan.json`` body when one was written."""

    _stage_name: str = "plan"


@dataclass
class ApplyResult(StageResult):
    """Result of :func:`apply`."""

    _stage_name: str = "apply"


@dataclass
class DiffResult(StageResult):
    """Result of :func:`diff`."""

    _stage_name: str = "diff"


@dataclass
class PublishResult(StageResult):
    """Result of :func:`publish`. Publish already has an upstream
    async surface; this wrapper does not add a thread hop."""

    _stage_name: str = "publish"


class EngineError(RuntimeError):
    """A stage exited non-zero. Carries the :class:`StageResult`.

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


# ── Internal: argparse.Namespace builder + stage runner ───────────────


def _logger(stage: str) -> logging.Logger:
    """Per-stage logger. Records go to the engine's own namespace so
    consumers can filter without affecting the CLI's own log topics."""
    log = logging.getLogger(f"fluid.engine.{stage}")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


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
) -> StageResult:
    """Run a sync ``cli/<stage>.run(args, logger) -> int`` in a worker
    thread, capturing stdout/stderr/exit-code.

    The stage modules expect to print to real stdout/stderr; we
    redirect those into in-memory buffers so the engine surface
    stays quiet by default and consumers get the captured output as
    part of the result.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    log = _logger(stage_name)

    def _invoke() -> int:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            return stage_module.run(ns, log)

    exit_code = await asyncio.to_thread(_invoke)
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

    The full set of CLI flags (``--config-override``, ``--timeout``,
    ``--parallel-phases``, ``--notify``, ``--metrics-export``,
    ``--debug``, ``--workspace-dir``, ``--state-file``,
    ``--provider-config``, ``--report``, ``--build-id``, …) are
    accepted via :func:`_build_namespace`'s defaults — the engine
    surface intentionally narrows the typed kwargs to the ones the
    Command Center actually drives. Pass extras through a follow-up
    PR if a new use case needs them.
    """
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
    return await _run_sync_stage("apply", _apply, ns, ApplyResult)


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
) -> PublishResult:
    """Run ``fluid publish`` in-process.

    Unlike the other stages, ``publish`` already exposes an async
    ``publish_contract`` function in the CLI module — we call it
    directly instead of going through ``asyncio.to_thread``.

    Multi-tenant note
    -----------------

    The wrapper instantiates a fresh ``FluidConfig()`` on every call.
    ``FluidConfig.__init__`` resolves credentials from ``$HOME``,
    the CWD, ``$FLUID_CONFIG``, and environment variables — meaning
    in a multi-tenant CC backend, ``publish`` picks up the SERVER's
    identity, not a per-request operator identity. This is the
    intended behaviour today (the CC's publish endpoint is admin-
    gated and the server credential is the canonical "publish on
    behalf of the org" identity), but it's worth surfacing here
    because the other stages have no such ambient state. If/when the
    CC introduces per-operator publish credentials, this is the line
    that needs to change.
    """
    from fluid_build.cli import publish as _publish
    from fluid_build.config_manager import FluidConfig

    log = _logger("publish")
    config = FluidConfig()

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
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
    except Exception as exc:  # pragma: no cover - upstream failure surfacing
        log.exception("publish failed")
        return PublishResult(
            exit_code=2,
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue() + f"\n{type(exc).__name__}: {exc}\n",
        )
