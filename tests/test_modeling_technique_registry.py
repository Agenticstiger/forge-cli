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

"""Tests for the pluggable modeling-technique registry (issue #248).

``--modeling-technique`` used to be a closed enum dispatched via a hardcoded
``if technique == "data_vault_2" ... else dimensional``. The
``copilot.modeling_techniques`` registry makes it pluggable
(``fluid_build.modeling_techniques`` entry points) and adds two source-aligned
built-ins:

* ``flat`` — 1:1, one expose per source table, no vault/dimensional reshaping.
* ``custom`` — consume a user-supplied logical model verbatim.

Tests use the same ``FakeEntryPoint`` monkeypatch idiom as
``tests/test_source_adapter_registry.py`` / ``tests/test_cli_plugin_hooks.py``.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from fluid_build.copilot import modeling_techniques as MT
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator
from fluid_build.schema_manager import FluidSchemaManager

_BUILTINS = {"data_vault_2", "dimensional", "flat", "custom"}


def _osi(datasets: List[dict]) -> dict:
    return {"name": "m", "description": "", "ai_context": {}, "datasets": datasets}


def _flat_logical(n_datasets: int = 2) -> LogicalDraft:
    datasets = [
        {
            "name": f"t{i}",
            "description": f"table {i}",
            "primary_key": [f"t{i}_id"],
            "fields": [
                {"name": f"t{i}_id", "data_type": "integer"},
                {"name": "label", "data_type": "varchar"},
            ],
        }
        for i in range(n_datasets)
    ]
    return LogicalDraft.model_validate({"name": "m", "technique": "flat", "osi": _osi(datasets)})


class _FakeEntryPoint:
    def __init__(self, name: str, load_value: Any) -> None:
        self.name = name
        self._load_value = load_value

    def load(self) -> Any:
        if isinstance(self._load_value, BaseException):
            raise self._load_value
        return self._load_value


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: List[_FakeEntryPoint]) -> None:
    import importlib.metadata as md

    class _EPs(list):
        def select(self, group=None):
            return list(eps) if group == MT.EP_GROUP else []

        def get(self, group, default=None):
            return list(eps) if group == MT.EP_GROUP else (default or [])

    def fake_entry_points(*args, **kwargs):
        group = kwargs.get("group")
        if group is not None:
            return list(eps) if group == MT.EP_GROUP else []
        return _EPs()

    monkeypatch.setattr(md, "entry_points", fake_entry_points)


@pytest.fixture(autouse=True)
def _reset_registry():
    MT.discover_modeling_techniques(force=True)
    yield
    MT.discover_modeling_techniques(force=True)


# ── registry ────────────────────────────────────────────────────────────
def test_builtins_present():
    assert set(MT.list_modeling_techniques()) == _BUILTINS


def test_alias_normalization():
    assert MT.normalize_technique("data-vault-2") == "data_vault_2"
    assert MT.normalize_technique("kimball") == "dimensional"
    assert MT.normalize_technique("source-aligned") == "flat"
    assert MT.normalize_technique(None) is None
    assert MT.normalize_technique("nope") == "nope"  # unknown passes through


def test_technique_flags():
    assert MT.get_modeling_technique("custom").requires_logical_model is True
    assert MT.get_modeling_technique("flat").uses_llm is False
    assert MT.get_modeling_technique("data_vault_2").uses_llm is True
    assert MT.get_modeling_technique("data_vault_2").branch == "dv2"
    assert MT.get_modeling_technique("flat").branch is None


def test_plugin_discovered(monkeypatch):
    anchor = MT.ModelingTechnique(name="anchor", branch=None, description="Anchor modeling")
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("anchor", anchor)])
    MT.discover_modeling_techniques(force=True)
    assert "anchor" in MT.list_modeling_techniques()
    assert MT.get_modeling_technique("anchor").origin == "entrypoint"


def test_plugin_cannot_shadow_builtin(monkeypatch):
    evil = MT.ModelingTechnique(name="data_vault_2", branch="dimensional")
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("data_vault_2", evil)])
    MT.discover_modeling_techniques(force=True)
    assert MT.get_modeling_technique("data_vault_2").branch == "dv2"  # built-in kept


def test_broken_and_wrongtype_plugins_skipped(monkeypatch):
    _patch_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("boom", RuntimeError("load failed")),
            _FakeEntryPoint("notatech", object()),
        ],
    )
    MT.discover_modeling_techniques(force=True)  # must not raise
    assert set(MT.list_modeling_techniques()) == _BUILTINS  # neither plugin admitted


def test_discovery_failure_is_fail_open(monkeypatch):
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    MT.discover_modeling_techniques(force=True)
    assert set(MT.list_modeling_techniques()) == _BUILTINS


# ── LogicalDraft shape validation ────────────────────────────────────────
def test_flat_and_custom_drafts_validate():
    assert _flat_logical().technique == "flat"
    custom = LogicalDraft.model_validate({"name": "m", "technique": "custom", "osi": _osi([])})
    assert custom.technique == "custom"


def test_branch_shape_still_enforced():
    with pytest.raises(Exception):
        LogicalDraft.model_validate({"name": "m", "technique": "data_vault_2", "osi": _osi([])})
    with pytest.raises(Exception):
        # flat must not carry a dv2 branch
        LogicalDraft.model_validate(
            {
                "name": "m",
                "technique": "flat",
                "osi": _osi([]),
                "dv2": {"hubs": [{"name": "h", "business_keys": ["k"], "source_tables": ["t"]}]},
            }
        )


# ── flat emission: 1:1 exposes ────────────────────────────────────────────
def test_flat_emits_one_expose_per_dataset():
    contract = build_contract_from_logical(_flat_logical(3), build_engine="dbt")
    assert [e["exposeId"] for e in contract["exposes"]] == ["t0", "t1", "t2"]
    res = FluidSchemaManager().validate_contract(
        contract, schema_version=contract["fluidVersion"], offline_only=True
    )
    assert res.is_valid, [getattr(e, "message", str(e)) for e in res.errors]


# ── validator relaxation: source-aligned exposes aren't forced into BI semantics
def test_flat_relaxes_semantic_coverage_but_dimensional_does_not():
    flat = _flat_logical(2)
    contract = build_contract_from_logical(flat, build_engine="dbt")
    # flat: sparse semantics are allowed (warnings, not errors)
    report_flat = FluidContractValidator().validate(logical=flat, contract=contract)
    assert report_flat.passes_schema, [str(i) for i in report_flat.issues if i.severity == "error"]
    # dimensional logical over the SAME (sparse) contract: coverage is enforced
    dim = flat.model_copy(update={"technique": "dimensional"})
    report_dim = FluidContractValidator().validate(logical=dim, contract=contract)
    assert not report_dim.passes_schema
