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

"""Tests for the OpenTofu apply engine (``fluid apply --engine opentofu``)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_GCP_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "bitcoin-price-api-imperative-part-a"
    / "contract-bigquery.fluid.yaml"
)


class TestDataLossGate:
    def test_destructive_plan_blocked_by_default(self):
        assert engine._data_loss_blocked({"add": 0, "change": 0, "remove": 2}, False) is True

    def test_destructive_plan_allowed_with_flag(self):
        assert engine._data_loss_blocked({"add": 0, "change": 0, "remove": 2}, True) is False

    def test_additive_plan_never_blocked(self):
        assert engine._data_loss_blocked({"add": 5, "change": 1, "remove": 0}, False) is False

    def test_a_revoked_grant_or_policy_tag_is_not_counted(self):
        removals = [
            ("google_bigquery_dataset_iam_member.a", "google_bigquery_dataset_iam_member"),
            ("google_data_catalog_policy_tag.t", "google_data_catalog_policy_tag"),
            ("aws_lakeformation_permissions.p", "aws_lakeformation_permissions"),
        ]
        counted, revoked = engine._data_bearing_changes(
            {"add": 0, "change": 0, "remove": 3}, removals
        )
        assert counted["remove"] == 0 and len(revoked) == 3
        assert engine._data_loss_blocked(counted, False) is False

    def test_a_table_or_a_key_grant_is_still_counted(self):
        removals = [
            ("google_bigquery_table.t", "google_bigquery_table"),
            ("google_kms_crypto_key_iam_member.k", "google_kms_crypto_key_iam_member"),
        ]
        counted, revoked = engine._data_bearing_changes(
            {"add": 1, "change": 0, "remove": 2}, removals
        )
        assert counted["remove"] == 2 and revoked == []


class TestReconcileWithState:
    """The engine's side of the GCP plugin's one-time access-list reconciliation."""

    class _Plugin:
        def __init__(self, reports):
            self.reports = reports

        def reconcile_state(self, module, state):
            module["patched"] = True
            return self.reports

    def test_a_patched_module_is_written_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [{"x": 1}])
        path = tmp_path / "main.tf.json"
        path.write_text("{}", encoding="utf-8")
        reports = [{"dataset": "sales", "revoked": ["READER group:gone@x.com"], "blocked": False}]
        got = engine._reconcile_with_state(
            self._Plugin(reports), path, str(tmp_path), {}, logging.getLogger("test")
        )
        assert got == reports
        assert json.loads(path.read_text(encoding="utf-8")) == {"patched": True}

    def test_a_list_that_cannot_be_narrowed_refuses_the_apply(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [{"x": 1}])
        path = tmp_path / "main.tf.json"
        path.write_text("{}", encoding="utf-8")
        reports = [{"dataset": "sales", "revoked": ["READER group:gone@x.com"], "blocked": True}]
        with pytest.raises(CLIError) as refused:
            engine._reconcile_with_state(
                self._Plugin(reports), path, str(tmp_path), {}, logging.getLogger("test")
            )
        assert refused.value.event == "opentofu_dataset_access_unreconciled"
        assert "group:gone@x.com" in refused.value.context["error"]
        assert path.read_text(encoding="utf-8") == "{}"

    def test_no_state_is_a_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
        path = tmp_path / "main.tf.json"
        path.write_text("{}", encoding="utf-8")
        plugin = self._Plugin([{"dataset": "d", "revoked": [], "blocked": True}])
        assert (
            engine._reconcile_with_state(plugin, path, str(tmp_path), {}, logging.getLogger("t"))
            == []
        )


class TestLoadContract:
    def test_loads_yaml_contract(self):
        args = argparse.Namespace(contract=str(_GCP_CONTRACT), env=None)
        contract = engine._load_contract(args, logging.getLogger("test"))
        assert contract.get("exposes")

    def test_loads_json_plan_contract(self, tmp_path):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"contract": {"id": "x", "exposes": []}}))
        args = argparse.Namespace(contract=str(plan), env=None)
        assert engine._load_contract(args, logging.getLogger("test")) == {"id": "x", "exposes": []}

    def test_json_plan_without_contract_errors(self, tmp_path):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"actions": []}))
        args = argparse.Namespace(contract=str(plan), env=None)
        with pytest.raises(CLIError):
            engine._load_contract(args, logging.getLogger("test"))


class TestApplyViaOpentofuGuards:
    def test_missing_tofu_binary_errors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_path", lambda: None)
        args = argparse.Namespace(
            contract=str(_GCP_CONTRACT),
            env=None,
            provider=None,
            workspace_dir=tmp_path,
            state_backend=None,
            dry_run=True,
            allow_data_loss=False,
        )
        with pytest.raises(CLIError) as exc:
            engine.apply_via_opentofu(args, logging.getLogger("test"))
        assert "tofu" in str(exc.value).lower()


class TestResolveApplyEngine:
    """Engine resolution is automatic and per-provider — no user switch."""

    def test_resolves_opentofu_for_cutover_provider(self):
        # GCP is cut over — a GCP contract resolves to the OpenTofu engine.
        args = argparse.Namespace(contract=str(_GCP_CONTRACT), env=None, provider=None)
        assert engine.resolve_apply_engine(args, logging.getLogger("test")) == "opentofu"

    def test_unclassifiable_contract_falls_back_to_native(self):
        args = argparse.Namespace(contract="/no/such/contract.yaml", env=None, provider=None)
        assert engine.resolve_apply_engine(args, logging.getLogger("test")) == "native"

    def test_provider_mismatch_propagates_instead_of_falling_back(self):
        # `fluid apply --provider gcp` on a non-GCP contract used to route
        # through the OpenTofu engine, emit a resource-free module and report
        # "tofu plan: +0 ~0 -0" with exit 0. The mismatch must surface — and
        # must NOT be swallowed by the broad "cannot classify" fallback, which
        # would silently route the same wrong target to the native engine.
        args = argparse.Namespace(contract=str(_GCP_CONTRACT), env=None, provider="snowflake")
        with pytest.raises(CLIError) as exc:
            engine.resolve_apply_engine(args, logging.getLogger("test"))
        assert exc.value.event == "generate_iac_provider_mismatch"

    def test_matching_provider_override_still_resolves(self):
        args = argparse.Namespace(contract=str(_GCP_CONTRACT), env=None, provider="gcp")
        assert engine.resolve_apply_engine(args, logging.getLogger("test")) == "opentofu"


class TestBrownfieldAdoption:
    """`_adopt_existing` tofu-imports pre-existing resources so `tofu apply`
    reconciles brownfield infra instead of failing 'already exists'."""

    def test_imports_only_candidates_not_already_in_state(self, monkeypatch):
        from fluid_build.iac.importer import ImportBlock

        imported: list = []
        monkeypatch.setattr(
            engine.runner, "tofu_state_list", lambda *a, **k: ["snowflake_database.d"]
        )

        def _fake_import(workdir, addr, rid, *, env=None):
            imported.append((addr, rid))
            return engine.runner.TofuResult("import", 0, "", "")

        monkeypatch.setattr(engine.runner, "tofu_import", _fake_import)

        class _Plugin:
            def discover_imports(self, contract, actions=()):
                return [
                    ImportBlock(to="snowflake_database.d", id="D"),  # in state → skip
                    ImportBlock(to="snowflake_schema.s", id='"D"."S"'),  # → import
                ]

        engine._adopt_existing(_Plugin(), {}, [], "/wd", {}, logging.getLogger("test"))
        assert imported == [("snowflake_schema.s", '"D"."S"')]

    def test_no_candidates_skips_state_query(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            engine.runner, "tofu_state_list", lambda *a, **k: calls.append("listed") or []
        )

        class _Plugin:
            def discover_imports(self, contract, actions=()):
                return []

        engine._adopt_existing(_Plugin(), {}, [], "/wd", {}, logging.getLogger("test"))
        assert calls == []

    def test_failed_import_is_tolerated(self, monkeypatch):
        from fluid_build.iac.importer import ImportBlock

        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(
            engine.runner,
            "tofu_import",
            lambda *a, **k: engine.runner.TofuResult("import", 1, "", "non-existent object"),
        )

        class _Plugin:
            def discover_imports(self, contract, actions=()):
                return [ImportBlock(to="snowflake_table.t", id='"D"."S"."T"')]

        # A non-existent resource fails to import — must not raise; `tofu
        # apply` then creates it.
        engine._adopt_existing(_Plugin(), {}, [], "/wd", {}, logging.getLogger("test"))


class TestImportsKeptOnlyByAnApply:
    """What a run imported is handed back (``tofu state rm``) unless its apply
    starts: a run that applies nothing leaves the state as it found it."""

    @pytest.fixture
    def tofu(self, monkeypatch):
        calls: dict = {"import": [], "state_rm": [], "apply": 0, "plan_events": []}
        for name in ("FLUID_STATE_BACKEND", "OPENLINEAGE_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
        monkeypatch.setattr(engine.runner, "require_tofu_version", lambda: None)

        def ok(command, events=None):
            return engine.runner.TofuResult(command, 0, "", "", events=events or [])

        def imported(workdir, address, resource_id, *, env=None):
            calls["import"].append(address)
            return ok("import")

        def state_rm(workdir, addresses, *, env=None):
            calls["state_rm"].append(list(addresses))
            return ok("state-rm")

        def applied(*a, **k):
            calls["apply"] += 1
            return ok("apply")

        monkeypatch.setattr(engine.runner, "tofu_init", lambda *a, **k: ok("init"))
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
        monkeypatch.setattr(engine.runner, "tofu_import", imported)
        monkeypatch.setattr(engine.runner, "tofu_state_rm", state_rm)
        monkeypatch.setattr(
            engine.runner, "tofu_plan", lambda *a, **k: ok("plan", calls["plan_events"])
        )
        monkeypatch.setattr(engine.runner, "tofu_apply", applied)
        return calls

    @staticmethod
    def _apply(tmp_path, *, dry_run=False):
        args = argparse.Namespace(
            contract=str(_GCP_CONTRACT),
            env=None,
            provider=None,
            workspace_dir=tmp_path,
            state_backend=None,
            dry_run=dry_run,
            allow_data_loss=False,
        )
        return engine.apply_via_opentofu(args, logging.getLogger("test"))

    def test_a_dry_run_hands_back_what_it_imported(self, tofu, tmp_path, capfd):
        assert self._apply(tmp_path, dry_run=True) == 0
        assert tofu["import"], "the contract has resources to adopt"
        assert tofu["state_rm"] == [tofu["import"]]
        assert "released the" in capfd.readouterr().out

    def test_a_refused_run_hands_back_what_it_imported(self, tofu, tmp_path):
        tofu["plan_events"][:] = [
            {
                "type": "planned_change",
                "change": {
                    "action": "delete",
                    "resource": {
                        "addr": "google_bigquery_table.t",
                        "resource_type": "google_bigquery_table",
                    },
                },
            },
            {"type": "change_summary", "changes": {"add": 0, "change": 0, "remove": 1}},
        ]
        with pytest.raises(CLIError) as exc:
            self._apply(tmp_path)
        assert exc.value.event == "opentofu_data_loss_gate"
        assert tofu["state_rm"] == [tofu["import"]] and tofu["apply"] == 0

    def test_an_apply_keeps_what_it_imported(self, tofu, tmp_path):
        assert self._apply(tmp_path) == 0
        assert tofu["import"] and tofu["apply"] == 1
        assert tofu["state_rm"] == []

    def test_a_release_that_fails_says_how_to_finish_it(self, tofu, tmp_path, monkeypatch, capfd):
        monkeypatch.setattr(
            engine.runner,
            "tofu_state_rm",
            lambda *a, **k: engine.runner.TofuResult("state-rm", 1, "", "state locked"),
        )
        assert self._apply(tmp_path, dry_run=True) == 0
        out = capfd.readouterr().out
        assert "stay in state, though nothing was applied" in out
        assert " state rm " in out

    def test_an_import_the_strict_check_stops_still_hands_back_the_earlier_ones(self, monkeypatch):
        from fluid_build.iac.importer import ImportBlock

        removed: list = []
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        results = iter(
            [
                engine.runner.TofuResult("import", 0, "", ""),
                engine.runner.TofuResult("import", 1, "", "permission denied"),
            ]
        )
        monkeypatch.setattr(engine.runner, "tofu_import", lambda *a, **k: next(results))
        monkeypatch.setattr(
            engine.runner,
            "tofu_state_rm",
            lambda workdir, addresses, *, env=None: removed.append(list(addresses))
            or engine.runner.TofuResult("state-rm", 0, "", ""),
        )

        class _Plugin:
            strict_adoption = True

            def discover_imports(self, contract, actions=()):
                return [ImportBlock(to="x_schema.s", id="s"), ImportBlock(to="x_table.t", id="t")]

        log = logging.getLogger("test")
        with pytest.raises(CLIError):
            with engine._imports_kept_only_by_an_apply("/wd", {}, log) as held:
                engine._adopt_existing(_Plugin(), {}, [], "/wd", {}, log, held.addresses)
        assert removed == [["x_schema.s"]]


class TestPerContractState:
    """Each contract applies into its own ``tofu`` workdir + state."""

    def test_workdir_includes_contract_id_segment(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
        # Skip the version-floor probe (subprocess fork against a fake path).
        monkeypatch.setattr(engine.runner, "require_tofu_version", lambda: None)
        monkeypatch.setattr(
            engine.runner, "tofu_init", lambda *a, **k: engine.runner.TofuResult("init", 0, "", "")
        )
        monkeypatch.setattr(
            engine.runner,
            "tofu_plan",
            lambda *a, **k: engine.runner.TofuResult("plan", 0, "", "", events=[]),
        )
        # ``_adopt_existing`` now finds candidates (GCP discover_imports
        # is no longer a stub); short-circuit the state-list + import
        # subprocess shells so this test keeps focusing on the workdir
        # layout invariant it owns.
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        # The GCP plugin's one-time access-list reconciliation reads the state too.
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
        monkeypatch.setattr(
            engine.runner,
            "tofu_import",
            lambda *a, **k: engine.runner.TofuResult("import", 1, "", "stub"),
        )
        args = argparse.Namespace(
            contract=str(_GCP_CONTRACT),
            env=None,
            provider=None,
            workspace_dir=tmp_path,
            state_backend=None,
            dry_run=True,
            allow_data_loss=False,
        )
        assert engine.apply_via_opentofu(args, logging.getLogger("test")) == 0
        modules = list((tmp_path / ".fluid" / "iac" / "gcp").glob("*/main.tf.json"))
        assert len(modules) == 1  # emitted under .fluid/iac/gcp/<contract-id>/
        assert modules[0].parent.parent.name == "gcp"
