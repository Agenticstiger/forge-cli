# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The proof gate: ``fluid plan --tofu`` saves the OpenTofu plan, ``fluid apply``
applies exactly it.

Unit tests pin the binding (``forge/core/plan_digest.py``), the saved-plan
readers and the GitLab report (``iac/runner.py``). The end-to-end class runs the
real ``tofu`` offline: a stub plugin emits OpenTofu's built-in ``terraform_data``
resource, which needs no provider download, so the saved plan, its digest, a
tampered file and a stale state are all exercised against the real binary.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.plan_digest import (
    TOFU_PLAN_FIELD,
    PlanBindingError,
    inject_digests,
    sha256_bytes,
    tofu_plan_record,
    verify_plan_binding,
    verify_tofu_plan,
)
from fluid_build.iac import runner, target_identity

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_LOG = logging.getLogger("test.iac.saved_plan")


def _record(**over: Any) -> Dict[str, Any]:
    record = {"planFile": "plan.tfplan", "sha256": sha256_bytes(b"plan bytes")}
    record.update(over)
    return record


# ---------------------------------------------------------------------------
# The binding (plan_digest)
# ---------------------------------------------------------------------------


class TestBinding:
    def test_a_plan_without_a_record_binds_nothing(self):
        assert tofu_plan_record({"actions": []}) is None

    @pytest.mark.parametrize(
        "name", ["", ".", "..", "../plan.tfplan", "sub/plan.tfplan", "/abs/plan", "a\\b"]
    )
    def test_a_plan_file_that_is_not_a_bare_name_is_refused(self, name):
        with pytest.raises(PlanBindingError) as refused:
            tofu_plan_record({TOFU_PLAN_FIELD: _record(planFile=name)})
        assert refused.value.kind == "tofu-plan-invalid"

    @pytest.mark.parametrize("record", [{"planFile": "p.tfplan"}, "p.tfplan", _record(sha256="x")])
    def test_a_record_without_a_digest_is_refused(self, record):
        with pytest.raises(PlanBindingError) as refused:
            tofu_plan_record({TOFU_PLAN_FIELD: record})
        assert refused.value.kind == "tofu-plan-invalid"

    def test_the_bound_bytes_verify(self):
        verify_tofu_plan(_record(), b"plan bytes")

    def test_other_bytes_are_a_mismatch(self):
        with pytest.raises(PlanBindingError) as refused:
            verify_tofu_plan(_record(), b"plan bytes, edited")
        assert refused.value.kind == "tofu-plan-mismatch"
        assert "fluid plan --tofu" in str(refused.value)

    def test_a_missing_file_is_missing(self):
        with pytest.raises(PlanBindingError) as refused:
            verify_tofu_plan(_record(), None)
        assert refused.value.kind == "tofu-plan-missing"

    def test_plan_digest_covers_the_record(self):
        plan = inject_digests({"actions": [], TOFU_PLAN_FIELD: _record()})
        verify_plan_binding(plan)
        plan[TOFU_PLAN_FIELD]["sha256"] = sha256_bytes(b"another plan")
        with pytest.raises(PlanBindingError) as refused:
            verify_plan_binding(plan)
        assert refused.value.kind == "plan-tamper"


# ---------------------------------------------------------------------------
# Reading a saved plan back (runner)
# ---------------------------------------------------------------------------

_SHOWN = {
    "resource_changes": [
        {"address": "a.new", "type": "a", "mode": "managed", "change": {"actions": ["create"]}},
        {"address": "a.upd", "type": "a", "mode": "managed", "change": {"actions": ["update"]}},
        {"address": "a.gone", "type": "a", "mode": "managed", "change": {"actions": ["delete"]}},
        {
            "address": "b.repl",
            "type": "b",
            "mode": "managed",
            "change": {"actions": ["delete", "create"]},
        },
        {"address": "a.same", "type": "a", "mode": "managed", "change": {"actions": ["no-op"]}},
        {"address": "data.d.x", "type": "d", "mode": "data", "change": {"actions": ["read"]}},
    ]
}


class TestSavedPlanReaders:
    def test_counts_match_the_change_summary(self):
        # A replace is one add and one remove, as tofu's change_summary counts it.
        assert runner.saved_plan_changes(_SHOWN) == {"add": 2, "change": 1, "remove": 2}

    def test_removals_are_deletes_and_replaces(self):
        assert runner.saved_plan_removals(_SHOWN) == [("a.gone", "a"), ("b.repl", "b")]

    def test_an_empty_plan(self):
        assert runner.saved_plan_changes({}) == {"add": 0, "change": 0, "remove": 0}
        assert runner.gitlab_plan_report({}) == (
            '{\n  "create": 0,\n  "update": 0,\n  "delete": 0\n}\n'
        )

    def test_gitlab_report_is_what_gitlab_tofu_writes(self):
        expected = '{\n  "create": 2,\n  "update": 1,\n  "delete": 2\n}\n'
        assert runner.gitlab_plan_report(_SHOWN) == expected

    @pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
    def test_gitlab_report_is_byte_identical_to_jq(self):
        # plan_jq_filter from gitlab.com/components/opentofu src/gitlab-tofu.sh @ 9e5d830a.
        jq_filter = """
  (
    [.resource_changes[]?.change.actions?] | flatten
  ) | {
    "create":(map(select(.=="create")) | length),
    "update":(map(select(.=="update")) | length),
    "delete":(map(select(.=="delete")) | length)
  }
"""
        for shown in (_SHOWN, {}, {"resource_changes": [{"change": {}}]}):
            out = subprocess.run(
                ["jq", "-r", jq_filter],
                input=json.dumps(shown),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert runner.gitlab_plan_report(shown) == out


class TestStaleDetection:
    @pytest.mark.parametrize(
        "text",
        [
            '{"@level":"error","diagnostic":{"summary":"Saved plan is stale"}}',
            "Error: Saved plan does not match the given state\nfrom a different state lineage.",
        ],
    )
    def test_tofu_refusals_read_as_stale(self, text):
        assert engine._saved_plan_stale(runner.TofuResult("apply", 1, text, ""))

    def test_another_failure_is_not(self):
        assert not engine._saved_plan_stale(runner.TofuResult("apply", 1, "AccessDenied", ""))

    def test_the_error_says_how_on(self):
        err = engine._stale_plan_error(runner.TofuResult("apply", 1, "", ""))
        assert err.event == "apply_plan_digest_tofu_plan_stale"
        assert err.context["kind"] == "tofu-plan-stale"
        assert any("fluid plan --tofu" in hint for hint in err.suggestions)


# ---------------------------------------------------------------------------
# fluid plan --tofu refuses what fluid apply would not run through OpenTofu
# ---------------------------------------------------------------------------


class TestPlanTofuRouting:
    def test_a_native_engine_contract_is_refused(self, monkeypatch, tmp_path):
        from fluid_build.cli import plan as plan_cli

        monkeypatch.setattr(engine, "resolve_apply_engine", lambda *a, **k: "native")
        args = argparse.Namespace(out=str(tmp_path / "plan.json"))
        with pytest.raises(CLIError) as refused:
            plan_cli._plan_tofu({"id": "x"}, False, args, _LOG)
        assert refused.value.event == "plan_tofu_unsupported"

    def test_a_managed_streaming_contract_is_refused(self, tmp_path):
        from fluid_build.cli import plan as plan_cli

        args = argparse.Namespace(out=str(tmp_path / "plan.json"))
        with pytest.raises(CLIError) as refused:
            plan_cli._plan_tofu({"id": "x"}, True, args, _LOG)
        assert refused.value.event == "plan_tofu_unsupported"


# ---------------------------------------------------------------------------
# Brownfield adoption becomes import blocks in the saved plan
# ---------------------------------------------------------------------------


class TestAdoptionInTheSavedPlan:
    def test_imports_are_handed_back_and_planned_as_import_blocks(self, monkeypatch, tmp_path):
        from fluid_build.iac.importer import ImportBlock

        calls: Dict[str, List[Any]] = {"import": [], "rm": []}
        monkeypatch.setattr(runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(
            runner,
            "tofu_state_resources",
            lambda *a, **k: [{"address": "x.parent", "values": {"uid": "U1"}}],
        )

        def _import(workdir, address, rid, *, env=None):
            calls["import"].append((address, rid))
            return runner.TofuResult("import", 0, "", "")

        def _rm(workdir, addresses, *, env=None):
            calls["rm"].append(list(addresses))
            return runner.TofuResult("state-rm", 0, "", "")

        monkeypatch.setattr(runner, "tofu_import", _import)
        monkeypatch.setattr(runner, "tofu_state_rm", _rm)

        class _Plugin:
            def discover_imports(self, contract, actions=()):
                return [
                    ImportBlock(to="x.parent", id="P"),
                    ImportBlock(to="x.child", id="P/{}", id_from=("x.parent", "uid")),
                ]

        with engine._imports_kept_only_by_an_apply(str(tmp_path), {}, _LOG) as imported:
            adopted = engine._adopt_existing(
                _Plugin(), {}, [], str(tmp_path), {}, _LOG, imported.addresses
            )
        # Nothing stays in state: the plan, not the import, adopts them.
        assert calls["rm"] == [["x.parent", "x.child"]]
        assert [(b.to, b.id, b.id_from) for b in adopted] == [
            ("x.parent", "P", None),
            ("x.child", "P/U1", None),
        ]


# ---------------------------------------------------------------------------
# End to end against the real tofu, offline
# ---------------------------------------------------------------------------


class _StubPlugin:
    """Just enough of an IaC plugin for the engine: no imports, no hooks."""

    name = "aws"
    # As the AWS plugin's: tofu, and the target read, see the AWS_* family.
    credential_env_vars = ()
    credential_env_prefixes = ("AWS_",)

    def discover_imports(self, contract, actions=()):
        return []

    def provider_block(self):
        return {}


def _module(contract: Dict[str, Any], target: Any) -> str:
    """One built-in ``terraform_data`` per contract: its input is the description, and a
    changed description replaces it (``triggers_replace``), as a data-bearing resource is."""
    value = contract.get("description", "")
    document: Dict[str, Any] = {
        "resource": {"terraform_data": {"product": {"input": value, "triggers_replace": value}}}
    }
    if target.backend:
        document["terraform"] = {"backend": target.backend}
    return json.dumps(document, indent=2)


@pytest.fixture
def offline_tofu(monkeypatch, tmp_path):
    if runner.tofu_path() is None:
        pytest.skip("needs `tofu` on PATH")
    for name in ("FLUID_STATE_BACKEND", "OPENLINEAGE_URL", "TF_CLI_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine, "get_iac_plugin", lambda provider: _StubPlugin())
    monkeypatch.setattr(
        engine,
        "emit_module",
        lambda plugin, contract, target, logger: (_module(contract, target), []),
    )
    monkeypatch.setattr(engine, "write_dependency_lock", lambda plugin, workdir: None)
    monkeypatch.setattr(engine, "credential_overlay", lambda plugin, env: {})
    monkeypatch.setattr(engine, "credential_report", lambda plugin, env: ([], []))
    # The target the apply's credentials reach: one account, read without a network call.
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setattr(target_identity, "aws_caller_account", lambda env, region: (_ACCOUNT, ""))
    monkeypatch.chdir(tmp_path)
    return tmp_path


_ACCOUNT = "111111111111"


def _args(contract: str, **over: Any) -> argparse.Namespace:
    values: Dict[str, Any] = dict(
        contract=contract,
        env=None,
        provider="aws",
        workspace_dir=None,
        state_backend=None,
        dry_run=False,
        allow_data_loss=False,
        no_verify_plan_binding=False,
        mode=None,
    )
    values.update(over)
    return argparse.Namespace(**values)


def _plan(tmp: Path, contract: Dict[str, Any], name: str = "plan.json") -> Path:
    """What ``fluid plan --tofu`` writes: the record bound into a digested plan.json."""
    out = tmp / name
    record = engine.plan_with_opentofu(_args("c.fluid.yaml"), _LOG, contract, str(out))
    out.write_text(
        json.dumps(inject_digests({"contract": contract, "actions": [], TOFU_PLAN_FIELD: record})),
        encoding="utf-8",
    )
    return out


def _state_inputs(tmp: Path) -> List[Any]:
    state = tmp / ".fluid" / "iac" / "aws" / "p1" / "terraform.tfstate"
    if not state.exists():
        return []
    doc = json.loads(state.read_text(encoding="utf-8"))
    return [
        inst["attributes"]["input"]["value"]
        for res in doc.get("resources", [])
        for inst in res.get("instances", [])
    ]


class TestEndToEnd:
    def test_plan_writes_the_saved_plan_and_its_reports_owner_only(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        record = json.loads(out.read_text())[TOFU_PLAN_FIELD]
        assert record["changes"] == {"add": 1, "change": 0, "remove": 0}
        assert record["provider"] == "aws" and record["state"].startswith("local: ")
        for key in ("planFile", "showJson", "gitlabReport"):
            path = offline_tofu / record[key]
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, key
        assert sha256_bytes((offline_tofu / record["planFile"]).read_bytes()) == record["sha256"]
        assert json.loads((offline_tofu / record["gitlabReport"]).read_text()) == {
            "create": 1,
            "update": 0,
            "delete": 0,
        }
        assert json.loads((offline_tofu / record["showJson"]).read_text())["resource_changes"]
        # The plan wrote no state: nothing was applied.
        assert _state_inputs(offline_tofu) == []

    def test_apply_runs_exactly_the_saved_plan(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        planned: List[str] = []
        real_plan = runner.tofu_plan
        monkeypatch.setattr(
            runner, "tofu_plan", lambda *a, **k: planned.append("plan") or real_plan(*a, **k)
        )
        # The contract in the plan says v1; apply must not re-plan from anything else.
        assert engine.apply_via_opentofu(_args(str(out)), _LOG) == 0
        assert planned == []
        assert _state_inputs(offline_tofu) == ["v1"]

    def test_a_tampered_saved_plan_is_refused(self, offline_tofu):
        first = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        record = json.loads(first.read_text())[TOFU_PLAN_FIELD]
        # Swap in a plan made from another contract: a valid tofu plan, not the reviewed one.
        other = _plan(offline_tofu, {"id": "p1", "description": "evil"}, name="other.json")
        other_file = json.loads(other.read_text())[TOFU_PLAN_FIELD]["planFile"]
        shutil.copyfile(offline_tofu / other_file, offline_tofu / record["planFile"])
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(first)), _LOG)
        assert refused.value.event == "apply_plan_digest_tofu_plan_mismatch"
        assert _state_inputs(offline_tofu) == []

    def test_a_missing_saved_plan_is_refused_even_unverified(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        (offline_tofu / json.loads(out.read_text())[TOFU_PLAN_FIELD]["planFile"]).unlink()
        for skip in (False, True):
            with pytest.raises(CLIError) as refused:
                engine.apply_via_opentofu(_args(str(out), no_verify_plan_binding=skip), _LOG)
            assert refused.value.event == "apply_plan_digest_tofu_plan_missing"

    def test_an_edited_record_is_plan_tamper(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        plan = json.loads(out.read_text())
        plan[TOFU_PLAN_FIELD]["sha256"] = sha256_bytes(b"x")
        out.write_text(json.dumps(plan))
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(out)), _LOG)
        assert refused.value.event == "apply_plan_digest_plan_tamper"

    def test_a_plan_made_stale_by_another_apply_is_refused(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        # Another apply lands first and moves the state on.
        other = _plan(offline_tofu, {"id": "p1", "description": "v2"}, name="other.json")
        assert engine.apply_via_opentofu(_args(str(other)), _LOG) == 0
        ran: List[str] = []
        monkeypatch.setattr(engine, "_enforce_access", lambda *a, **k: ran.append("access"))
        monkeypatch.setattr(engine, "_prepare_destroys", lambda *a, **k: ran.append("prepare"))
        monkeypatch.setattr(runner, "tofu_apply", lambda *a, **k: ran.append("apply"))
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(out)), _LOG)
        assert refused.value.event == "apply_plan_digest_tofu_plan_stale"
        # Refused before any hook that acts outside OpenTofu, and before tofu apply.
        assert ran == []
        assert _state_inputs(offline_tofu) == ["v2"]

    def test_opentofu_still_refuses_a_stale_plan_without_the_recorded_version(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        other = _plan(offline_tofu, {"id": "p1", "description": "v2"}, name="other.json")
        assert engine.apply_via_opentofu(_args(str(other)), _LOG) == 0
        plan = json.loads(out.read_text())
        for key in ("stateSerial", "stateLineage"):
            plan[TOFU_PLAN_FIELD].pop(key)
        plan.pop("planDigest")
        out.write_text(json.dumps(inject_digests(plan, force=True)))
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(out)), _LOG)
        assert refused.value.event == "apply_plan_digest_tofu_plan_stale"

    def test_another_state_target_is_refused(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(
                _args(str(out), state_backend="s3://some-bucket/key.tfstate"), _LOG
            )
        assert refused.value.event == "apply_plan_digest_tofu_plan_target"

    def test_dry_run_applies_nothing(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        assert engine.apply_via_opentofu(_args(str(out), dry_run=True), _LOG) == 0
        assert _state_inputs(offline_tofu) == []

    def test_a_destroying_saved_plan_meets_the_data_loss_gate(self, offline_tofu):
        applied = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        assert engine.apply_via_opentofu(_args(str(applied)), _LOG) == 0
        # triggers_replace changed: a replace, so one remove.
        out = _plan(offline_tofu, {"id": "p1", "description": "v2"}, name="next.json")
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(out)), _LOG)
        assert refused.value.event == "opentofu_data_loss_gate"
        assert engine.apply_via_opentofu(_args(str(out), allow_data_loss=True), _LOG) == 0
        assert _state_inputs(offline_tofu) == ["v2"]

    def test_a_plan_without_a_saved_plan_keeps_todays_path(self, offline_tofu, monkeypatch):
        contract = {"id": "p1", "description": "v1"}
        out = offline_tofu / "plain.json"
        out.write_text(json.dumps(inject_digests({"contract": contract, "actions": []})))
        planned: List[str] = []
        real_plan = runner.tofu_plan
        monkeypatch.setattr(
            runner, "tofu_plan", lambda *a, **k: planned.append("plan") or real_plan(*a, **k)
        )
        assert engine.apply_via_opentofu(_args(str(out)), _LOG) == 0
        assert planned == ["plan"]
        assert _state_inputs(offline_tofu) == ["v1"]


class TestPlanAndApplyShareTheEngine:
    """Plan and apply build tofu's env with one function and init and plan through the
    runner's wrappers (``tofu_init`` adds the S3 native-lock arguments), so the plan
    holds the state lock and the saved plan is applied with the env it was made with."""

    def test_both_paths_use_the_one_env_builder_and_the_runner(self, offline_tofu, monkeypatch):
        calls: List[str] = []
        real_env, real_init, real_plan = engine._tofu_env, runner.tofu_init, runner.tofu_plan
        monkeypatch.setattr(
            engine, "_tofu_env", lambda *a, **k: calls.append("env") or real_env(*a, **k)
        )
        monkeypatch.setattr(
            runner, "tofu_init", lambda *a, **k: calls.append("init") or real_init(*a, **k)
        )
        monkeypatch.setattr(
            runner, "tofu_plan", lambda *a, **k: calls.append("plan") or real_plan(*a, **k)
        )
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        assert calls == ["env", "init", "plan"]
        calls.clear()
        assert engine.apply_via_opentofu(_args(str(out)), _LOG) == 0
        assert calls == ["env", "init"]


class TestNoUnreviewedPlan:
    def test_a_saved_plan_never_resumes_with_a_second_plan(self, monkeypatch):
        failed = runner.TofuResult("apply", 1, "", "boom")
        monkeypatch.setattr(runner, "tofu_apply", lambda *a, **k: failed)

        def _resume(*a, **k):
            raise AssertionError("a bound apply must not re-plan")

        monkeypatch.setattr(engine, "_resume_blocked_destroys", _resume)
        assert engine._apply_saved_plan(None, {}, [], "/wd", {}, resume=False) == (failed, 0)

    def test_an_adoption_left_in_state_stops_the_plan(self, monkeypatch, tmp_path):
        from fluid_build.iac.importer import ImportBlock

        monkeypatch.setattr(runner, "tofu_state_list", lambda *a, **k: ["x.kept"])
        eng = argparse.Namespace(workdir=str(tmp_path), env={})
        with pytest.raises(CLIError) as refused:
            engine._refuse_unreleased_imports(eng, [ImportBlock(to="x.kept", id="K")])
        assert refused.value.event == "plan_tofu_adoption_not_released"
        engine._refuse_unreleased_imports(eng, [ImportBlock(to="x.gone", id="G")])

    def test_plan_and_apply_emit_the_same_module_for_the_same_target(
        self, offline_tofu, monkeypatch
    ):
        # Every module of both paths comes from emit_module with the run's StateTarget, so
        # what the target adds (its backend; state and plan encryption) is the same in both.
        emitted: List[Any] = []
        real_emit = engine.emit_module

        def _emit(plugin, contract, target, logger):
            module = real_emit(plugin, contract, target, logger)
            emitted.append((target, module[0]))
            return module

        monkeypatch.setattr(engine, "emit_module", _emit)
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        assert engine.apply_via_opentofu(_args(str(out)), _LOG) == 0
        (plan_target, plan_module), (apply_target, apply_module) = emitted
        assert plan_target == apply_target
        assert plan_module == apply_module


# ---------------------------------------------------------------------------
# The saved plan applies on another runner, and only on the target it was made for
# ---------------------------------------------------------------------------


def _shared_backend(monkeypatch, state: Path) -> None:
    """Both runners keep state in one place, as an s3:// or gcs:// backend would: OpenTofu's
    ``local`` backend at an absolute path, so the proof runs offline."""
    real = engine.resolve_state_target

    def _resolve(args, contract, provider):
        from dataclasses import replace

        return replace(real(args, contract, provider), backend={"local": {"path": str(state)}})

    monkeypatch.setattr(engine, "resolve_state_target", _resolve)


def _copy_to(runner_dir: Path, plan: Path) -> Path:
    """What a CI hand-off carries: plan.json and the saved plan it binds, nothing else."""
    runner_dir.mkdir(parents=True)
    record = json.loads(plan.read_text())[TOFU_PLAN_FIELD]
    for name in (plan.name, record["planFile"]):
        shutil.copy2(plan.parent / name, runner_dir / name)
    return runner_dir / plan.name


def _shared_inputs(state: Path) -> List[Any]:
    doc = json.loads(state.read_text(encoding="utf-8"))
    return [
        inst["attributes"]["input"]["value"]
        for res in doc.get("resources", [])
        for inst in res.get("instances", [])
    ]


def _rebind(out: Path, edit) -> None:
    """Edit the saved-plan record and bind the plan again, as a re-signing editor would."""
    plan = json.loads(out.read_text())
    edit(plan[TOFU_PLAN_FIELD])
    plan.pop("planDigest")
    out.write_text(json.dumps(inject_digests(plan, force=True)))


class TestAnotherRunner:
    def test_the_record_names_the_target(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        target = json.loads(out.read_text())[TOFU_PLAN_FIELD]["target"]
        assert target["identity"] == {"account": _ACCOUNT, "region": "eu-west-1"}
        assert target["unread"] == {}
        assert target["backend"] == ""  # local state
        assert target["tofu"] == ".".join(str(p) for p in runner.tofu_version())

    @pytest.mark.parametrize("existing", [False, True], ids=["greenfield", "brownfield"])
    def test_a_plan_made_on_one_runner_applies_unchanged_on_another(
        self, offline_tofu, monkeypatch, existing
    ):
        state = offline_tofu / "shared" / "terraform.tfstate"
        state.parent.mkdir()
        _shared_backend(monkeypatch, state)
        runner_a = offline_tofu / "runner-a" / "checkout"
        runner_a.mkdir(parents=True)
        monkeypatch.chdir(runner_a)
        if existing:
            first = _plan(runner_a, {"id": "p1", "description": "v0"}, name="first.json")
            assert engine.apply_via_opentofu(_args(str(first)), _LOG) == 0
        out = _plan(runner_a, {"id": "p1", "description": "v1"})
        # Runner B: another machine, another checkout path, only the hand-off files.
        runner_b = offline_tofu / "runner-b" / "deep" / "other" / "checkout"
        handed = _copy_to(runner_b, out)
        monkeypatch.chdir(runner_b)
        planned: List[str] = []
        real_plan = runner.tofu_plan
        monkeypatch.setattr(
            runner, "tofu_plan", lambda *a, **k: planned.append("plan") or real_plan(*a, **k)
        )
        assert engine.apply_via_opentofu(_args(str(handed), allow_data_loss=existing), _LOG) == 0
        assert planned == []  # applied unchanged: no plan of its own
        assert _shared_inputs(state) == ["v1"]

    def test_local_state_does_not_travel_and_says_so(self, offline_tofu, monkeypatch):
        engine.apply_via_opentofu(
            _args(str(_plan(offline_tofu, {"id": "p1", "description": "v0"}, name="v0.json"))),
            _LOG,
        )
        handed = _copy_to(
            offline_tofu / "elsewhere", _plan(offline_tofu, {"id": "p1", "description": "v1"})
        )
        monkeypatch.chdir(handed.parent)
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(handed)), _LOG)
        assert refused.value.event == "apply_plan_digest_tofu_plan_state_missing"
        assert "local state stays in the workspace" in refused.value.context["error"]
        assert _state_inputs(offline_tofu) == ["v0"]


class TestTargetBinding:
    def _refused(self, out: Path, **over: Any) -> CLIError:
        with pytest.raises(CLIError) as refused:
            engine.apply_via_opentofu(_args(str(out), **over), _LOG)
        return refused.value

    def _nothing_ran(self, monkeypatch) -> List[str]:
        ran: List[str] = []
        for name in ("reconcile_state_key", "_enforce_access", "_prepare_destroys"):
            monkeypatch.setattr(engine, name, lambda *a, _n=name, **k: ran.append(_n))
        monkeypatch.setattr(runner, "tofu_apply", lambda *a, **k: ran.append("tofu apply"))
        return ran

    def test_another_account_is_refused_before_anything_runs(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        monkeypatch.setattr(
            target_identity, "aws_caller_account", lambda env, region: ("222222222222", "")
        )
        ran = self._nothing_ran(monkeypatch)
        refused = self._refused(out)
        assert refused.event == "apply_plan_digest_tofu_plan_target_mismatch"
        assert "111111111111" in refused.context["error"]
        assert "222222222222" in refused.context["error"]
        assert ran == []
        assert _state_inputs(offline_tofu) == []

    def test_another_region_is_refused(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        refused = self._refused(out)
        assert refused.event == "apply_plan_digest_tofu_plan_target_mismatch"
        assert "region" in refused.context["error"]

    def test_an_account_this_run_cannot_read_fails_closed(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        monkeypatch.setattr(
            target_identity, "aws_caller_account", lambda env, region: ("", "no credentials")
        )
        refused = self._refused(out)
        assert refused.event == "apply_plan_digest_tofu_plan_target_unverified"
        assert "no credentials" in refused.context["error"]

    def test_another_tofu_version_is_refused_before_tofu_sees_it(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        real = runner.tofu_version()
        monkeypatch.setattr(runner, "tofu_version", lambda: (real[0], real[1], real[2] + 1))
        refused = self._refused(out)
        assert refused.event == "apply_plan_digest_tofu_plan_toolchain"

    def test_another_provider_version_is_refused(self, offline_tofu, monkeypatch):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        monkeypatch.setattr(
            engine, "_locked_providers", lambda workdir: {"registry.opentofu.org/x/y": "9.9.9"}
        )
        refused = self._refused(out)
        assert refused.event == "apply_plan_digest_tofu_plan_toolchain"
        assert "registry.opentofu.org/x/y none -> 9.9.9" in refused.context["error"]

    @pytest.mark.parametrize(
        "edit, event",
        [
            (
                lambda r: r["target"].update(backend=sha256_bytes(b"another backend")),
                "apply_plan_digest_tofu_plan_target",
            ),
            (lambda r: r.pop("target"), "apply_plan_digest_tofu_plan_target_missing"),
        ],
        ids=["another-backend", "no-target"],
    )
    def test_a_record_for_another_or_no_target_is_refused(self, offline_tofu, edit, event):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        _rebind(out, edit)
        assert self._refused(out).event == event

    def test_an_edited_target_is_plan_tamper(self, offline_tofu):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        plan = json.loads(out.read_text())
        plan[TOFU_PLAN_FIELD]["target"]["identity"]["account"] = "222222222222"
        out.write_text(json.dumps(plan))
        assert self._refused(out).event == "apply_plan_digest_plan_tamper"

    def test_a_field_the_plan_could_not_read_is_not_compared(self, offline_tofu, monkeypatch):
        monkeypatch.setattr(
            target_identity, "aws_caller_account", lambda env, region: ("", "boto3 missing")
        )
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        record = json.loads(out.read_text())[TOFU_PLAN_FIELD]["target"]
        assert record["unread"] == {"account": "boto3 missing"}
        monkeypatch.setattr(
            target_identity, "aws_caller_account", lambda env, region: ("3" * 12, "")
        )
        assert engine.apply_via_opentofu(_args(str(out)), _LOG) == 0

    def test_the_escape_hatch_skips_it_loudly(self, offline_tofu, monkeypatch, caplog):
        out = _plan(offline_tofu, {"id": "p1", "description": "v1"})
        monkeypatch.setattr(
            target_identity, "aws_caller_account", lambda env, region: ("222222222222", "")
        )
        caplog.set_level(logging.WARNING)
        assert engine.apply_via_opentofu(_args(str(out), no_verify_plan_binding=True), _LOG) == 0
        assert "apply_tofu_plan_target_unverified_by_flag" in caplog.text
