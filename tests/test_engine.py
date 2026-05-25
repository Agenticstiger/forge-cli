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

import json
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
    r = engine.ValidateResult(exit_code=2, stderr="boom")
    with pytest.raises(engine.EngineError) as exc:
        r.raise_for_status()
    assert exc.value.stage == "validate"
    assert exc.value.result is r
    assert "boom" in str(exc.value)


def test_raise_for_status_quiet_on_success():
    r = engine.PlanResult(exit_code=0)
    r.raise_for_status()  # no exception


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

    assert isinstance(result, engine.ValidateResult)
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

    assert isinstance(result, engine.PlanResult)
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

    assert isinstance(result, engine.BundleResult)
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

    assert isinstance(result, engine.VerifyResult)
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

    assert isinstance(result, engine.GenerateArtifactsResult)
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

    assert isinstance(result, engine.ValidateArtifactsResult)
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
