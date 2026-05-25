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
    """A stage exited non-zero. Carries the :class:`StageResult`."""

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

    ns = argparse.Namespace(
        contract=str(contract),
        env=env,
        schema_version=schema_version,
        min_version=min_version,
        max_version=max_version,
        strict=strict,
        cache_dir=cache_dir,
        format=output_format,
        verbose=verbose,
        show_schema=show_schema,
        list_versions=list_versions,
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
    fluid_version: Optional[str] = None,
    cost_estimate: bool = False,
    no_verify_digest: bool = False,
) -> PlanResult:
    """Run ``fluid plan`` in-process.

    Returns a :class:`PlanResult`. If a ``plan.json`` is written
    (default behavior unless suppressed by stage flags), it is read
    back and surfaced as ``result.artifacts["plan"]``.
    """
    from fluid_build.cli import plan as _plan

    ns = argparse.Namespace(
        contract=str(contract),
        env=env,
        provider=provider,
        project=project,
        region=region,
        output=str(output) if output else None,
        fluid_version=fluid_version,
        cost_estimate=cost_estimate,
        no_verify_digest=no_verify_digest,
    )
    result = await _run_sync_stage("plan", _plan, ns, PlanResult)
    # Best-effort read of plan.json if the stage wrote one.
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
    plan_json: Optional[Path] = None,
    mode: Optional[str] = None,
    no_verify_plan_binding: bool = False,
) -> ApplyResult:
    """Run ``fluid apply`` in-process.

    Defaults to ``dry_run=True`` so a bare ``await engine.apply(...)``
    call is safe — same conservative default the CC backend uses for
    its durable apply endpoint.
    """
    from fluid_build.cli import apply as _apply

    ns = argparse.Namespace(
        contract=str(contract),
        env=env,
        dry_run=dry_run,
        yes=yes,
        rollback_strategy=rollback_strategy,
        bundle=str(bundle) if bundle else None,
        plan=str(plan_json) if plan_json else None,
        mode=mode,
        no_verify_plan_binding=no_verify_plan_binding,
    )
    return await _run_sync_stage("apply", _apply, ns, ApplyResult)


async def diff(
    contract_a: Path | str,
    contract_b: Path | str,
    *,
    output_format: str = "json",
) -> DiffResult:
    """Run ``fluid diff`` in-process to compare two contracts."""
    from fluid_build.cli import diff as _diff

    ns = argparse.Namespace(
        contract=str(contract_a),
        other=str(contract_b),
        format=output_format,
    )
    result = await _run_sync_stage("diff", _diff, ns, DiffResult)
    if output_format == "json" and result.stdout.strip():
        try:
            result.artifacts["diff"] = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Stage emitted non-JSON despite format=json — keep the raw
            # text in result.stdout, leave artifacts['diff'] unset.
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
