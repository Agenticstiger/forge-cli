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
