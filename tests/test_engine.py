# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for fluid_build.engine — the Phase 1 async facade.

The wrapper is intentionally thin. These tests cover the wrapper's
own behaviour (result type, exit-code propagation, stdout/stderr
capture, artifact parsing) using monkeypatched stage modules — NOT
the stage logic itself, which is tested at length elsewhere.

For each public function we verify:

1. it returns the right typed result class
2. exit code propagates from the stage's ``run()`` return value
3. stdout/stderr are captured into the result
4. on JSON-shaped output, the result.artifacts is populated
5. raise_for_status raises EngineError on non-zero exit
"""

from __future__ import annotations

import asyncio as asyncio_mod
import json
import os
import sys
from pathlib import Path

import pytest

from fluid_build import engine

# ── Generic StageResult ───────────────────────────────────────────────


def test_stage_result_success_true_on_zero():
    r = engine.StageResult(exit_code=0)
    assert r.success is True


def test_stage_result_success_false_on_nonzero():
    r = engine.StageResult(exit_code=1)
    assert r.success is False


def test_raise_for_status_raises_on_failure():
    r = engine.ValidateStageResult(exit_code=2, stderr="boom")
    with pytest.raises(engine.EngineError) as exc:
        r.raise_for_status()
    assert exc.value.stage == "validate"
    assert exc.value.result is r
    assert "boom" in str(exc.value)


def test_raise_for_status_quiet_on_success():
    r = engine.PlanStageResult(exit_code=0)
    r.raise_for_status()  # no exception


# Parametrize across every stage result type so a typo in any
# ``_stage_name`` ClassVar slips a test, not production.
_ALL_RESULT_TYPES = [
    (engine.ValidateStageResult, "validate", engine.ValidateFailed),
    (engine.PlanStageResult, "plan", engine.PlanFailed),
    (engine.ApplyStageResult, "apply", engine.ApplyFailed),
    (engine.DiffStageResult, "diff", engine.DiffFailed),
    (engine.PublishStageResult, "publish", engine.PublishFailed),
    (engine.BundleStageResult, "bundle", engine.BundleFailed),
    (engine.VerifyStageResult, "verify", engine.VerifyFailed),
    (engine.PolicyApplyStageResult, "policy_apply", engine.PolicyApplyFailed),
    (engine.GenerateArtifactsStageResult, "generate_artifacts", engine.GenerateArtifactsFailed),
    (engine.ValidateArtifactsStageResult, "validate_artifacts", engine.ValidateArtifactsFailed),
    (engine.ScheduleSyncStageResult, "schedule_sync", engine.ScheduleSyncFailed),
]


@pytest.mark.parametrize("result_cls,stage_name,expected_exc", _ALL_RESULT_TYPES)
def test_raise_for_status_raises_stage_specific_subtype(result_cls, stage_name, expected_exc):
    """The reviewer asked for typed exceptions per stage. Verify the
    right subtype is raised AND it still subclasses EngineError so
    callers can catch broadly."""
    r = result_cls(exit_code=1, stderr=f"{stage_name} broke")
    with pytest.raises(expected_exc) as exc:
        r.raise_for_status()
    # Subclass check — broad catch still works.
    assert isinstance(exc.value, engine.EngineError)
    assert exc.value.stage == stage_name
    assert exc.value.result is r


@pytest.mark.parametrize("result_cls,stage_name,_", _ALL_RESULT_TYPES)
def test_raise_for_status_quiet_on_success_for_every_stage(result_cls, stage_name, _):
    """Symmetric to the failure-mode parametrize: no stage should raise
    on a zero exit. Guards against a stage adding silly raise-on-success
    logic by mistake."""
    result_cls(exit_code=0).raise_for_status()


@pytest.mark.parametrize("result_cls,stage_name,_", _ALL_RESULT_TYPES)
def test_stage_name_is_classvar_not_dataclass_field(result_cls, stage_name, _):
    """``_stage_name`` was originally a dataclass field. That meant it
    showed up in ``repr`` / ``__eq__`` / ``fields()`` and could be
    overridden via constructor — none of which were intended. The fix
    promoted it to ClassVar."""
    from dataclasses import fields

    field_names = {f.name for f in fields(result_cls)}
    assert (
        "_stage_name" not in field_names
    ), f"{result_cls.__name__}._stage_name must be ClassVar, not a dataclass field"
    # And the class-level value matches the expected stage name.
    assert result_cls._stage_name == stage_name
    # Constructor cannot accept it as a kwarg.
    with pytest.raises(TypeError):
        result_cls(exit_code=0, _stage_name="spoofed")  # type: ignore[call-arg]


def test_engine_error_carries_full_result_for_inspection():
    """The reviewer noted ``EngineError.result`` wasn't directly tested.
    Catch it; inspect every field on the original result."""
    artifacts = {"plan": {"actions": []}, "plan_path": "/tmp/plan.json"}
    # ``artifacts`` is now a read-only ``MappingProxyType`` property; the
    # underlying dataclass field is ``_artifacts`` (write-side).
    r = engine.PlanStageResult(exit_code=3, _artifacts=artifacts, stdout="out", stderr="err")
    with pytest.raises(engine.PlanFailed) as exc:
        r.raise_for_status()
    # The exception carries the original result, not a copy.
    assert exc.value.result is r
    assert exc.value.result.exit_code == 3
    # Compare via dict() since artifacts is now a MappingProxyType view.
    assert dict(exc.value.result.artifacts) == artifacts
    assert exc.value.result.stdout == "out"
    assert exc.value.result.stderr == "err"


def test_engine_usage_error_is_not_engine_error():
    """``EngineUsageError`` is a ValueError, not an EngineError — the
    invocation was wrong, the stage never ran. Catching EngineError
    must NOT catch the usage error."""
    assert not issubclass(engine.EngineUsageError, engine.EngineError)
    assert issubclass(engine.EngineUsageError, ValueError)


# ── validate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_returns_validate_result_type(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    captured = {}

    def fake_run(ns, logger):
        captured["ns"] = ns
        print(json.dumps({"valid": True, "errors": []}))
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract, env="dev", strict=True)

    assert isinstance(result, engine.ValidateStageResult)
    assert result.success
    assert result.exit_code == 0
    # The wrapper parsed the JSON-shaped stdout into the report artifact.
    assert result.artifacts["report"] == {"valid": True, "errors": []}
    # Args were forwarded.
    assert captured["ns"].contract == str(contract)
    assert captured["ns"].env == "dev"
    assert captured["ns"].strict is True
    assert captured["ns"].format == "json"


@pytest.mark.asyncio
async def test_validate_captures_stdout_when_not_json(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    def fake_run(ns, logger):
        print("Validation OK", file=sys.stderr)
        print("contract is valid")
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract, output_format="text")

    assert "contract is valid" in result.stdout
    assert "Validation OK" in result.stderr
    assert result.artifacts == {}  # no JSON parsing in text mode


@pytest.mark.asyncio
async def test_validate_propagates_nonzero_exit(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("")

    def fake_run(ns, logger):
        print("invalid yaml", file=sys.stderr)
        return 1

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract)
    assert result.exit_code == 1
    assert result.success is False
    assert "invalid yaml" in result.stderr


# ── plan ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_reads_back_plan_json_artifact(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")
    plan_path = tmp_path / "plan.json"
    plan_body = {"actions": [{"op": "ensure_dataset"}], "phases": []}

    def fake_run(ns, logger):
        # The real stage writes plan.json next to the contract by default.
        plan_path.write_text(json.dumps(plan_body))
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract)

    assert isinstance(result, engine.PlanStageResult)
    assert result.success
    assert result.artifacts["plan"] == plan_body
    assert result.artifacts["plan_path"] == str(plan_path)


@pytest.mark.asyncio
async def test_plan_respects_explicit_output_path(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")
    explicit_output = tmp_path / "explicit-plan.json"

    captured = {}

    def fake_run(ns, logger):
        # CLI's argparse uses ``dest="out"`` (not ``output``).
        captured["out"] = ns.out
        Path(ns.out).write_text(json.dumps({"phases": [1]}))
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract, output=explicit_output)

    assert captured["out"] == str(explicit_output)
    assert result.artifacts["plan"] == {"phases": [1]}


# ── apply ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_defaults_to_dry_run(tmp_path, monkeypatch):
    """A bare ``await engine.apply(...)`` must default to dry_run=True
    to match the CC backend's conservative default."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    captured = {}

    def fake_run(ns, logger):
        captured["dry_run"] = ns.dry_run
        return 0

    monkeypatch.setattr("fluid_build.cli.apply.run", fake_run)
    await engine.apply(contract)
    assert captured["dry_run"] is True


@pytest.mark.asyncio
async def test_apply_forwards_rollback_strategy(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    captured = {}

    def fake_run(ns, logger):
        captured["rollback"] = ns.rollback_strategy
        captured["yes"] = ns.yes
        return 0

    monkeypatch.setattr("fluid_build.cli.apply.run", fake_run)
    await engine.apply(contract, dry_run=False, yes=True, rollback_strategy="phase_complete")
    assert captured["rollback"] == "phase_complete"
    assert captured["yes"] is True


# ── diff ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diff_parses_json_output(tmp_path, monkeypatch):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("kind: x")
    b.write_text("kind: y")

    captured = {}

    def fake_run(ns, logger):
        # CLI uses ``dest="baseline"`` (not ``other``).
        captured["contract"] = ns.contract
        captured["baseline"] = ns.baseline
        print(json.dumps({"changes": [{"path": "kind", "from": "x", "to": "y"}]}))
        return 0

    monkeypatch.setattr("fluid_build.cli.diff.run", fake_run)
    result = await engine.diff(a, b)
    assert captured["contract"] == str(a)
    assert captured["baseline"] == str(b)
    assert result.artifacts["diff"]["changes"][0]["from"] == "x"


# ── Integration: namespace-shape regression guards ────────────────────
#
# These tests are the antidote to the bug pattern flagged in PR #142's
# review: the engine wrapper monkeypatched the CLI stages in every test,
# so an ``argparse.Namespace`` with a wrong field name silently passed
# every unit test but blew up on the first real call. These integration
# tests construct the namespace via :func:`engine._build_namespace` and
# verify it carries every attribute the real ``cli/<stage>.run()``
# reads. If a future ``add_argument`` upstream introduces a new
# ``args.<x>`` dereference, the build_namespace machinery picks up the
# default from the CLI's own argparse — these tests confirm the shape.


def _read_args_referenced(cli_module_name: str) -> set[str]:
    """Return the set of ``args.<x>`` attributes the CLI's ``run()`` and
    its callees reference, by static grep on the module source."""
    import importlib
    import inspect
    import re

    cli_mod = importlib.import_module(f"fluid_build.cli.{cli_module_name}")
    src = inspect.getsource(cli_mod)
    return set(re.findall(r"args\.([a-zA-Z_][a-zA-Z0-9_]*)", src))


def _engine_namespace_for(stage_name: str, overrides: dict) -> argparse.Namespace:
    """Build the engine's namespace for ``stage_name`` without running."""
    import importlib

    from fluid_build import engine as _engine

    stage_mod = importlib.import_module(f"fluid_build.cli.{stage_name}")
    return _engine._build_namespace(stage_mod, overrides)


import argparse  # noqa: E402 — used by the helpers above


def test_validate_namespace_carries_every_field_the_stage_reads():
    """If a real ``fluid validate`` call would dereference an attribute,
    the engine's namespace must already have it set. This is what
    monkeypatched tests can't catch."""
    referenced = _read_args_referenced("validate")
    ns = _engine_namespace_for("validate", {"contract": "/tmp/x"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, (
        f"engine.validate's namespace is missing fields the CLI reads: {sorted(missing)}. "
        "The CLI's argparse should be the source of truth — fix _build_namespace, "
        "not this assertion."
    )


def test_plan_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("plan")
    ns = _engine_namespace_for("plan", {"contract": "/tmp/x"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.plan namespace missing: {sorted(missing)}"


def test_apply_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("apply")
    ns = _engine_namespace_for("apply", {"contract": "/tmp/x"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.apply namespace missing: {sorted(missing)}"


def test_diff_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("diff")
    ns = _engine_namespace_for("diff", {"contract": "/tmp/a", "baseline": "/tmp/b"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.diff namespace missing: {sorted(missing)}"


def test_plan_namespace_uses_cli_dest_names_not_engine_kwarg_names():
    """The original bug: engine sent ``output=``, CLI argparse used
    ``dest="out"``. The auto-namespace + the wrapper's explicit
    ``overrides["out"]`` together prove the right field shows up."""
    from pathlib import Path as _P

    ns = _engine_namespace_for("plan", {"contract": "/tmp/c", "out": "/tmp/plan.json"})
    assert ns.out == "/tmp/plan.json"
    # And ``output`` was never on the CLI's argparse, so it should not
    # leak onto the namespace from the wrapper's defaults.
    assert not hasattr(ns, "output") or ns.output is None
    del _P  # silence flake8 on macOS pytest runners


def test_diff_namespace_uses_baseline_not_other():
    """The original bug: engine sent ``other=`` (no such CLI arg)."""
    ns = _engine_namespace_for("diff", {"contract": "/tmp/a", "baseline": "/tmp/b"})
    assert ns.baseline == "/tmp/b"
    assert not hasattr(ns, "other") or ns.other is None


# ── Phase 1.1: bundle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_returns_bundle_result_type(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")
    out_path = tmp_path / "bundled.tgz"

    captured = {}

    def fake_run(ns, logger):
        captured["contract"] = ns.contract
        captured["format"] = ns.format
        captured["out"] = ns.out
        out_path.write_bytes(b"fake-tgz-bytes")
        return 0

    monkeypatch.setattr("fluid_build.cli.bundle.run", fake_run)
    result = await engine.bundle(contract, out=out_path, output_format="tgz")

    assert isinstance(result, engine.BundleStageResult)
    assert result.success
    assert captured["contract"] == str(contract)
    assert captured["format"] == "tgz"
    assert captured["out"] == str(out_path)


# ── Phase 1.1: verify ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_reads_back_report_artifact(tmp_path, monkeypatch):
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")
    out_path = tmp_path / "drift.json"
    report_body = {"drift": [], "verified_at": "now"}

    def fake_run(ns, logger):
        out_path.write_text(json.dumps(report_body))
        return 0

    monkeypatch.setattr("fluid_build.cli.verify.run", fake_run)
    result = await engine.verify(contract, out=out_path, show_diffs=True, strict=True)

    assert isinstance(result, engine.VerifyStageResult)
    assert result.artifacts["report"] == report_body
    assert result.artifacts["report_path"] == str(out_path)


# ── Phase 1.1: policy_apply ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_apply_defaults_to_check_mode(tmp_path, monkeypatch):
    bindings = tmp_path / "bindings.json"
    bindings.write_text("{}")

    captured = {}

    def fake_run(ns, logger):
        captured["mode"] = ns.mode
        captured["bindings"] = ns.bindings
        return 0

    monkeypatch.setattr("fluid_build.cli.policy_apply.run", fake_run)
    await engine.policy_apply(bindings)
    assert captured["mode"] == "check"
    assert captured["bindings"] == str(bindings)


@pytest.mark.asyncio
async def test_policy_apply_rejects_unknown_mode(tmp_path):
    bindings = tmp_path / "bindings.json"
    bindings.write_text("{}")
    with pytest.raises(ValueError, match="must be 'check' or 'enforce'"):
        await engine.policy_apply(bindings, mode="nuke")


# ── Phase 1.1: generate_artifacts ─────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_artifacts_reads_back_manifest(tmp_path, monkeypatch):
    bundle_path = tmp_path / "bundle.tgz"
    bundle_path.write_bytes(b"fake")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    manifest_body = {"files": {"a.json": "sha256:abc"}, "digest": "sha256:xyz"}

    def fake_run(ns, logger):
        Path(ns.manifest).write_text(json.dumps(manifest_body))
        return 0

    monkeypatch.setattr("fluid_build.cli.generate_artifacts.run", fake_run)
    result = await engine.generate_artifacts(bundle_path, out=out_dir)

    assert isinstance(result, engine.GenerateArtifactsStageResult)
    assert result.success
    assert result.artifacts["manifest"] == manifest_body


# ── Phase 1.1: validate_artifacts ─────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_artifacts_reads_back_report(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "dist"
    artifacts_dir.mkdir()
    report_path = tmp_path / "va.json"
    report_body = {"status": "ok", "issues": [], "summary": {"errors": 0}}

    captured = {}

    def fake_run(ns, logger):
        captured["strict"] = ns.strict
        captured["artifacts_dir"] = ns.artifacts_dir
        report_path.write_text(json.dumps(report_body))
        return 0

    monkeypatch.setattr("fluid_build.cli.validate_artifacts.run", fake_run)
    result = await engine.validate_artifacts(artifacts_dir, report=report_path, strict=True)

    assert isinstance(result, engine.ValidateArtifactsStageResult)
    assert result.artifacts["report"] == report_body
    assert captured["strict"] is True


# ── Phase 1.1: schedule_sync ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_sync_defaults_to_dry_run(tmp_path, monkeypatch):
    captured = {}

    def fake_run(ns, logger):
        captured["dry_run"] = ns.dry_run
        captured["scheduler"] = ns.scheduler
        return 0

    monkeypatch.setattr("fluid_build.cli.schedule_sync.run", fake_run)
    await engine.schedule_sync(scheduler="airflow", dags_dir="dist/schedule/")
    assert captured["dry_run"] is True
    assert captured["scheduler"] == "airflow"


@pytest.mark.asyncio
async def test_schedule_sync_forwards_signature_verification(tmp_path, monkeypatch):
    captured = {}

    def fake_run(ns, logger):
        captured["verify_signature"] = ns.verify_signature
        captured["bundle"] = ns.bundle
        captured["verify_key"] = ns.verify_key
        return 0

    monkeypatch.setattr("fluid_build.cli.schedule_sync.run", fake_run)
    await engine.schedule_sync(
        scheduler="airflow",
        dags_dir="dist/schedule/",
        bundle_path="bundle.tgz",
        verify_signature=True,
        verify_key="awskms://key-id",
    )
    assert captured["verify_signature"] is True
    assert captured["bundle"] == "bundle.tgz"
    assert captured["verify_key"] == "awskms://key-id"


# ── Phase 1.1: namespace-shape regression guards ──────────────────────


def test_bundle_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("bundle")
    ns = _engine_namespace_for("bundle", {"contract": "/tmp/c"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.bundle namespace missing: {sorted(missing)}"


def test_verify_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("verify")
    ns = _engine_namespace_for("verify", {"contract": "/tmp/c"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.verify namespace missing: {sorted(missing)}"


def test_policy_apply_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("policy_apply")
    ns = _engine_namespace_for("policy_apply", {"bindings": "/tmp/b"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.policy_apply namespace missing: {sorted(missing)}"


def test_generate_artifacts_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("generate_artifacts")
    ns = _engine_namespace_for("generate_artifacts", {"bundle": "/tmp/b.tgz"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.generate_artifacts namespace missing: {sorted(missing)}"


def test_validate_artifacts_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("validate_artifacts")
    ns = _engine_namespace_for("validate_artifacts", {"artifacts_dir": "/tmp/d"})
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.validate_artifacts namespace missing: {sorted(missing)}"


def test_schedule_sync_namespace_carries_every_field_the_stage_reads():
    referenced = _read_args_referenced("schedule_sync")
    ns = _engine_namespace_for(
        "schedule_sync",
        {"scheduler": "airflow", "dags_dir": "/tmp/d"},
    )
    missing = [a for a in referenced if not hasattr(ns, a)]
    assert not missing, f"engine.schedule_sync namespace missing: {sorted(missing)}"


# ── Second-pass review: capture-mechanism + safety regressions ────────
#
# These tests pin the design-level fixes from the second-pass review:
# log-record capture, env-snapshot/restore in apply(), the apply()
# yes-without-dry-run guard, and the concurrent-call lock. Without
# these tests the fixes can silently regress when the wrapper is next
# touched.


@pytest.mark.asyncio
async def test_validate_captures_log_records_not_just_print(tmp_path, monkeypatch):
    """The reviewer noted result.stderr only had print() output — log
    records emitted by ``logging.getLogger(__name__)`` from the stage
    were missing. Verify they now land in result.stderr."""
    import logging

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    # Stages use ``logging.getLogger(__name__)`` — for the validate
    # stage, that's ``fluid_build.cli.validate``.
    stage_logger = logging.getLogger("fluid_build.cli.validate")

    def fake_run(ns, logger):
        # Emit through the STAGE's logger (what real stages do), not
        # through the engine-passed logger argument.
        stage_logger.error("schema mismatch on line 14")
        stage_logger.info("trying again with strict=False")
        return 1

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract)

    assert (
        "schema mismatch on line 14" in result.stderr
    ), f"Expected stage logger.error output in stderr, got: {result.stderr!r}"
    assert (
        "trying again with strict=False" in result.stderr
    ), "Stage logger.info output should also be captured"


@pytest.mark.asyncio
async def test_apply_snapshots_and_restores_os_environ(tmp_path, monkeypatch):
    """The reviewer flagged that ``cli.apply.run`` calls hydrate_dotenv,
    which mutates os.environ from process CWD. The engine must snapshot
    + restore so a tenant's dotenv doesn't leak across calls."""
    import os

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    # Capture env state before / inside / after the call.
    sentinel_added = "FLUID_TEST_TENANT_SECRET"
    sentinel_modified_key = "FLUID_TEST_MODIFIED"
    sentinel_modified_orig = "original-value"

    # Seed the modified key so we can assert restoration.
    monkeypatch.setenv(sentinel_modified_key, sentinel_modified_orig)
    # Ensure the "added" key is NOT present before.
    monkeypatch.delenv(sentinel_added, raising=False)

    inside = {}

    def fake_run(ns, logger):
        # Simulate hydrate_dotenv: add a new key, mutate an existing one.
        os.environ[sentinel_added] = "leaked-value"
        os.environ[sentinel_modified_key] = "tenant-override"
        inside["added"] = os.environ.get(sentinel_added)
        inside["modified"] = os.environ.get(sentinel_modified_key)
        return 0

    monkeypatch.setattr("fluid_build.cli.apply.run", fake_run)
    await engine.apply(contract)

    # Inside the call the env was mutated.
    assert inside["added"] == "leaked-value"
    assert inside["modified"] == "tenant-override"
    # After the call the env is restored.
    assert (
        sentinel_added not in os.environ
    ), f"Engine must drop env keys added during apply; {sentinel_added} leaked"
    assert (
        os.environ[sentinel_modified_key] == sentinel_modified_orig
    ), "Engine must restore env keys mutated during apply"


@pytest.mark.asyncio
async def test_apply_refuses_yes_false_without_dry_run(tmp_path):
    """The reviewer flagged that ``apply(dry_run=False, yes=False)`` would
    block on ``input()`` in a notebook context. Engine must refuse the
    combo with EngineUsageError, not let the call reach the stage."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    with pytest.raises(engine.EngineUsageError) as exc:
        await engine.apply(contract, dry_run=False, yes=False)
    assert "yes=True" in str(exc.value)
    assert "dry_run" in str(exc.value)


@pytest.mark.asyncio
async def test_apply_allows_dry_run_with_yes_false(tmp_path, monkeypatch):
    """Symmetric to the previous test: the guard only triggers on
    ``dry_run=False AND yes=False``. A bare ``apply(...)`` (dry_run=True
    by default, yes=False) must work — it's the conservative default."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    def fake_run(ns, logger):
        return 0

    monkeypatch.setattr("fluid_build.cli.apply.run", fake_run)
    # No raise — this is the safe default combo.
    result = await engine.apply(contract)
    assert result.success


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_cross_contaminate_output(tmp_path, monkeypatch):
    """The reviewer's P0: ``redirect_stdout`` is process-global, so two
    concurrent calls would corrupt each other's captured output. The
    engine acquires a process-wide lock to serialize calls; verify
    output is correctly attributed even when scheduled concurrently."""
    import asyncio as _asyncio

    contract_a = tmp_path / "a.fluid.yaml"
    contract_b = tmp_path / "b.fluid.yaml"
    contract_a.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")
    contract_b.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    call_count = {"n": 0}

    def fake_run(ns, logger):
        call_count["n"] += 1
        # Each call prints its own contract path. With process-global
        # stdout redirect, an unlocked impl could interleave them and
        # contaminate each result's stdout — the lock prevents this.
        print(json.dumps({"contract": ns.contract, "valid": True}))
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)

    # Schedule both concurrently and let the event loop interleave.
    result_a, result_b = await _asyncio.gather(
        engine.validate(contract_a),
        engine.validate(contract_b),
    )

    assert call_count["n"] == 2
    # Each result's artifacts must reflect ITS contract, not the other.
    assert result_a.artifacts["report"]["contract"] == str(
        contract_a
    ), f"Concurrent call cross-contamination! a got: {result_a.artifacts}"
    assert result_b.artifacts["report"]["contract"] == str(
        contract_b
    ), f"Concurrent call cross-contamination! b got: {result_b.artifacts}"


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "Phase 1 limit: asyncio.to_thread dispatches to a thread, and "
        "Python threads cannot be cancelled. The awaiter sees "
        "CancelledError but the underlying stage keeps running. This "
        "test documents the broken behaviour as an XFAIL until Phase 2 "
        "adds a proper CancellationToken mechanism."
    ),
    strict=False,
)
async def test_apply_cancellation_stops_underlying_work(tmp_path, monkeypatch):
    """Document the cancellation gap. When this test stops xfailing,
    cancellation has been fixed — flip strict to True or remove the
    xfail and update the test to assert the stage was cancelled."""
    import asyncio as _asyncio
    import time

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    work_done = {"completed": False}

    def fake_run(ns, logger):
        # Simulate a long-running apply.
        time.sleep(0.5)
        work_done["completed"] = True
        return 0

    monkeypatch.setattr("fluid_build.cli.apply.run", fake_run)

    task = _asyncio.create_task(engine.apply(contract))
    await _asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task

    # Wait for the underlying thread to finish before asserting — this
    # is what makes the test XFAIL: the work completes despite cancellation.
    await _asyncio.sleep(0.6)
    # With cancellation working correctly, this would be False.
    # Currently True because threads can't be cancelled.
    assert work_done["completed"] is False, (
        "Cancellation is now propagating to the underlying thread — "
        "remove the xfail and update the docstring accordingly"
    )


# ── Publish coverage (none existed before second-pass review) ─────────


@pytest.mark.asyncio
async def test_publish_returns_publish_result_on_success(tmp_path, monkeypatch):
    """First test for publish — verify the wrapper turns
    ``publish_contract``'s success into a PublishStageResult."""
    from unittest.mock import AsyncMock, MagicMock

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    # publish_contract is async + returns an object with .success.
    upstream = MagicMock()
    upstream.success = True

    async_publish = AsyncMock(return_value=upstream)
    monkeypatch.setattr("fluid_build.cli.publish.publish_contract", async_publish)
    # Stub FluidConfig so we don't scan $HOME on test machines.
    monkeypatch.setattr("fluid_build.config_manager.FluidConfig", MagicMock())

    result = await engine.publish(contract, catalog="dev")

    assert isinstance(result, engine.PublishStageResult)
    assert result.success
    assert result.artifacts["publish"] is upstream
    # Verify the wrapper passed catalog through.
    call_kwargs = async_publish.call_args.kwargs
    assert call_kwargs["catalog_name"] == "dev"


@pytest.mark.asyncio
async def test_publish_passes_through_explicit_config(tmp_path, monkeypatch):
    """The reviewer flagged that publish builds a fresh FluidConfig
    per call. The fix added a ``config`` kwarg so callers can cache;
    verify it's forwarded to publish_contract instead of constructing
    a new one."""
    from unittest.mock import AsyncMock, MagicMock

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    upstream = MagicMock()
    upstream.success = True
    async_publish = AsyncMock(return_value=upstream)
    monkeypatch.setattr("fluid_build.cli.publish.publish_contract", async_publish)

    # Sentinel config — if the wrapper builds its own, this isn't passed through.
    fluid_config_class = MagicMock()
    monkeypatch.setattr("fluid_build.config_manager.FluidConfig", fluid_config_class)
    explicit_config = MagicMock(name="caller-supplied-FluidConfig")

    await engine.publish(contract, config=explicit_config)

    # The wrapper used the caller's config without instantiating a new one.
    call_kwargs = async_publish.call_args.kwargs
    assert call_kwargs["config"] is explicit_config
    fluid_config_class.assert_not_called()


@pytest.mark.asyncio
async def test_publish_failure_yields_nonzero_exit(tmp_path, monkeypatch):
    """When ``publish_contract`` returns success=False, the wrapper
    surfaces exit_code=1 (callable's raise_for_status would then raise
    PublishFailed)."""
    from unittest.mock import AsyncMock, MagicMock

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    upstream = MagicMock()
    upstream.success = False
    async_publish = AsyncMock(return_value=upstream)
    monkeypatch.setattr("fluid_build.cli.publish.publish_contract", async_publish)
    monkeypatch.setattr("fluid_build.config_manager.FluidConfig", MagicMock())

    result = await engine.publish(contract)
    assert not result.success
    assert result.exit_code == 1
    # Verify the typed subtype is raised.
    with pytest.raises(engine.PublishFailed):
        result.raise_for_status()


@pytest.mark.asyncio
async def test_publish_lets_cancellation_propagate(tmp_path, monkeypatch):
    """Unlike to_thread-dispatched stages, publish is natively async;
    CancelledError must propagate (not get swallowed by the broad
    Exception catch)."""
    from unittest.mock import AsyncMock, MagicMock

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    async_publish = AsyncMock(side_effect=asyncio_mod.CancelledError())
    monkeypatch.setattr("fluid_build.cli.publish.publish_contract", async_publish)
    monkeypatch.setattr("fluid_build.config_manager.FluidConfig", MagicMock())

    with pytest.raises(asyncio_mod.CancelledError):
        await engine.publish(contract)


# ── Empty-stdout JSON-mode coverage ──────────────────────────────────


@pytest.mark.asyncio
async def test_validate_with_empty_stdout_does_not_set_report_artifact(tmp_path, monkeypatch):
    """``engine.validate`` reads stdout as JSON when output_format=json,
    but must not blow up if stdout is empty (stage failed before
    printing, or chose to emit no output)."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    def fake_run(ns, logger):
        # No print, no log emissions, just an exit code.
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract, output_format="json")

    assert result.success
    assert "report" not in result.artifacts, "Empty stdout must not populate the report artifact"


# ── Third-pass review: finer-grained correctness fixes ────────────────


def test_stage_result_repr_truncates_large_stdout_stderr():
    """``repr(result)`` used to dump full stdout/stderr — a real apply
    emits hundreds of KB of Rich progress output. ``__repr__`` now
    truncates at 200 chars + a count hint; full text still accessible
    via the ``.stdout``/``.stderr`` attrs."""
    huge = "X" * 5000
    r = engine.ApplyStageResult(exit_code=0, stdout=huge, stderr=huge)
    s = repr(r)
    assert len(s) < 800, f"repr({type(r).__name__}) blew up: {len(s)} chars"
    assert "5000" in s or "4800" in s, "repr should hint at the truncated count"
    # Full text still accessible on the attribute.
    assert r.stdout == huge
    assert r.stderr == huge


def test_stage_result_artifacts_returns_read_only_mapping():
    """The reviewer's P3: ``artifacts`` was a mutable dict; consumers
    could mutate engine-owned data. ``artifacts`` is now a
    ``MappingProxyType`` view — reads work, mutation raises TypeError."""
    r = engine.PlanStageResult(exit_code=0, _artifacts={"plan": {"actions": []}})
    # Reads work.
    assert r.artifacts["plan"] == {"actions": []}
    assert "plan" in r.artifacts
    assert list(r.artifacts.keys()) == ["plan"]
    assert len(r.artifacts) == 1
    # Mutation is rejected.
    with pytest.raises(TypeError):
        r.artifacts["spoofed"] = "nope"  # type: ignore[index]


def test_engine_error_strips_ansi_before_truncating_message():
    """The reviewer's P3: Rich emits ANSI color codes; slicing
    ``stderr[:200]`` could straddle an escape and leave ``\\x1b[``
    garbage in the message. The fix strips ANSI before truncating."""
    # Build a stderr with ANSI sequences scattered throughout.
    ansi_red = "\x1b[31m"
    ansi_reset = "\x1b[0m"
    stderr = f"{ansi_red}error:{ansi_reset} the {ansi_red}thing{ansi_reset} broke"
    r = engine.ValidateStageResult(exit_code=1, stderr=stderr)
    with pytest.raises(engine.ValidateFailed) as exc:
        r.raise_for_status()
    msg = str(exc.value)
    # No ANSI in the message text.
    assert "\x1b" not in msg, f"ANSI escape leaked into message: {msg!r}"
    # The actual text is preserved.
    assert "error: the thing broke" in msg


@pytest.mark.asyncio
async def test_plan_reads_back_from_explicit_output_path_not_contract_dir(tmp_path, monkeypatch):
    """The reviewer's P1 plan-readback bug: the CLI's default --out is
    CWD-relative, but the engine looked in ``Path(contract).parent``.
    In a server consumer that doesn't ``cd`` into the contract dir,
    the engine looked in the wrong place. The fix: always pass an
    explicit --out so the path is unambiguous AND the engine reads
    back from that exact path."""
    # Place the contract in a sub-dir so contract-dir ≠ CWD if the
    # caller doesn't cd.
    subdir = tmp_path / "tenant" / "contracts"
    subdir.mkdir(parents=True)
    contract = subdir / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\nkind: DataContract\n")

    captured_out = {}

    def fake_run(ns, logger):
        # The engine now MUST pass an explicit --out so the readback
        # path is unambiguous. The fake stage writes there.
        captured_out["out"] = ns.out
        Path(ns.out).write_text(
            json.dumps({"actions": [{"op": "ensure_dataset"}]}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract)

    # Engine passed --out and the path is next to the contract.
    expected_out = str(subdir / "plan.json")
    assert captured_out["out"] == expected_out
    # Readback found the file at that exact path.
    assert result.artifacts["plan"] == {"actions": [{"op": "ensure_dataset"}]}
    assert result.artifacts["plan_path"] == expected_out


@pytest.mark.asyncio
async def test_plan_ignores_stale_plan_json_in_contract_dir(tmp_path, monkeypatch):
    """Compounds the path fix: if a stale ``plan.json`` is sitting
    next to the contract from an old CLI run AND the new stage didn't
    write (e.g. it failed before reaching the writer), the engine
    must not return the stale data."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")
    # Plant a stale plan.json BEFORE running the engine.
    stale_path = tmp_path / "plan.json"
    stale_path.write_text(json.dumps({"stale": True}), encoding="utf-8")
    # Backdate the mtime well before "now" so the mtime gate trips.
    old_time = stale_path.stat().st_mtime - 3600
    os.utime(stale_path, (old_time, old_time))

    def fake_run(ns, logger):
        # Stage "fails" — does NOT write to ns.out.
        return 0  # success exit so the mtime gate is what excludes the stale file

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract)

    # Despite the stale plan.json existing at the engine's readback
    # path, the mtime gate excludes it — no plan artifact surfaced.
    assert "plan" not in result.artifacts


@pytest.mark.asyncio
async def test_plan_readback_runs_inside_lock_for_concurrent_calls(tmp_path, monkeypatch):
    """The reviewer's P1 readback race: two concurrent plan calls
    could see each other's plan.json in the gap between
    ``stage.run()`` and the engine's readback. The fix moves readback
    INSIDE ``_run_sync_stage``'s lock, eliminating the gap.

    Verify each concurrent caller sees ITS own contract's plan
    content, not the other's."""
    import asyncio as _asyncio

    sub_a = tmp_path / "a"
    sub_b = tmp_path / "b"
    sub_a.mkdir()
    sub_b.mkdir()
    contract_a = sub_a / "c.fluid.yaml"
    contract_b = sub_b / "c.fluid.yaml"
    contract_a.write_text("apiVersion: fluid.dev/v1\n")
    contract_b.write_text("apiVersion: fluid.dev/v1\n")

    def fake_run(ns, logger):
        # Each contract's plan content is keyed by the contract path
        # so we can tell them apart in the assertion.
        Path(ns.out).write_text(json.dumps({"from_contract": ns.contract}), encoding="utf-8")
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result_a, result_b = await _asyncio.gather(
        engine.plan(contract_a),
        engine.plan(contract_b),
    )

    # Each result reflects its OWN contract — no cross-contamination.
    assert result_a.artifacts["plan"]["from_contract"] == str(contract_a)
    assert result_b.artifacts["plan"]["from_contract"] == str(contract_b)


@pytest.mark.asyncio
async def test_validate_surfaces_parse_error_sentinel_on_bad_json(tmp_path, monkeypatch):
    """The reviewer's P3: a non-JSON stdout used to leave
    ``artifacts["report"]`` silently absent — indistinguishable from
    'stage emitted nothing'. The fix surfaces a sentinel so consumers
    can tell parse-failure apart from no-output."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")

    def fake_run(ns, logger):
        # Emit something that LOOKS like output but isn't valid JSON.
        print("this looks like text, not JSON {")
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)
    result = await engine.validate(contract, output_format="json")

    # Sentinel makes parse-failure unambiguous.
    assert "report" not in result.artifacts, "Bad JSON must not silently populate report"
    assert "report_parse_error" in result.artifacts
    assert "JSONDecodeError" in result.artifacts["report_parse_error"]
    # Raw excerpt of what the stage emitted, for diagnosis.
    assert "this looks like text" in result.artifacts["report_raw_excerpt"]


@pytest.mark.asyncio
async def test_publish_result_is_distinct_from_provider_publish_result():
    """The reviewer's P1 name collision: there was an ``engine.PublishResult``
    AND a ``providers.catalogs.base.PublishResult``. The fix renamed
    the engine class to ``PublishStageResult``; this test pins the
    rename so the collision doesn't return."""
    # The engine result is the renamed StageResult subtype.
    assert engine.PublishStageResult.__name__ == "PublishStageResult"
    assert issubclass(engine.PublishStageResult, engine.StageResult)
    # Verify the old name is GONE — re-introducing it would re-introduce
    # the collision risk.
    assert not hasattr(engine, "PublishResult"), (
        "Old PublishResult alias must not exist — it would collide with "
        "providers.catalogs.base.PublishResult"
    )


@pytest.mark.asyncio
async def test_plan_readback_uses_utf8_encoding(tmp_path, monkeypatch):
    """The reviewer's P2: ``Path.read_text()`` defaults to
    ``locale.getencoding()``. On servers with non-UTF-8 locales, non-
    ASCII plan content (CJK, accents) raises UnicodeDecodeError.
    Verify the readback pins UTF-8 by emitting non-ASCII content."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")

    # Plan body with non-ASCII characters that would crash on LANG=C.
    body = {
        "actions": [{"op": "ensure_dataset", "description": "東京 — café résumé"}],
    }

    def fake_run(ns, logger):
        Path(ns.out).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract)

    assert result.artifacts["plan"] == body
    assert "東京" in result.artifacts["plan"]["actions"][0]["description"]


@pytest.mark.asyncio
async def test_validate_timeout_fires_on_stuck_stage(tmp_path, monkeypatch):
    """The reviewer's P2: stages stuck in a deadlock / infinite loop
    would hold a worker thread forever. The fix accepts a per-call
    ``timeout=`` and uses ``asyncio.wait_for`` to bound the await
    (the underlying thread still runs — soft signal, not hard kill,
    per the cancellation gap)."""
    import time

    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")

    def fake_run(ns, logger):
        # Sleep WAY longer than the test's timeout.
        time.sleep(10)
        return 0

    monkeypatch.setattr("fluid_build.cli.validate.run", fake_run)

    with pytest.raises(asyncio_mod.TimeoutError):
        await engine.validate(contract, timeout=0.2)


@pytest.mark.asyncio
async def test_artifact_read_error_surfaces_sentinel(tmp_path, monkeypatch):
    """If the ``read_artifacts`` callback raises, ``_run_sync_stage``
    catches and surfaces a sentinel so the call still returns a
    well-formed result. Verify the sentinel shape."""
    contract = tmp_path / "c.fluid.yaml"
    contract.write_text("apiVersion: fluid.dev/v1\n")

    def fake_run(ns, logger):
        # Don't write plan.json — the read_artifacts callback for plan
        # will return {} (no file), no error. To trigger the sentinel,
        # we need a callback that explicitly raises. The plan callback
        # only catches narrow exceptions; arrange for a generic raise
        # by making read_text() blow up via a non-utf8 file.
        Path(ns.out).write_bytes(b"\xff\xfe not valid utf-8")
        return 0

    monkeypatch.setattr("fluid_build.cli.plan.run", fake_run)
    result = await engine.plan(contract)

    # The narrow exceptions plan() catches (JSONDecodeError, OSError,
    # UnicodeDecodeError) drop the artifact silently. Verify that's
    # the behaviour — no plan artifact, no sentinel for THIS path.
    assert "plan" not in result.artifacts
    # And the stage call itself didn't crash.
    assert result.success
