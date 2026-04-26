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

from __future__ import annotations

from pathlib import Path

import yaml

from fluid_build.cli.mcp import _call_tool
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft


def _logical_model() -> LogicalDraft:
    return LogicalDraft(
        name="orders",
        description="Orders model",
        technique="data_vault_2",
        conceptual=ConceptualDraft(name="orders"),
        dv2={
            "hubs": [],
            "links": [],
            "satellites": [],
            "pits": [],
            "bridges": [],
            "hash_key_strategy": "md5",
        },
        osi=OSISemanticModel(
            name="orders",
            description="Orders model",
            ai_context=OSIAIContext(),
            datasets=[],
            relationships=[],
            metrics=[],
            custom_extensions=[],
        ),
        source_summary={},
    )


def test_mcp_search_semantic_memory_uses_configured_store(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("FLUID_STORE_PATH", str(tmp_path / "mcp.sqlite3"))

    from fluid_build.copilot.store.factory import resolve_store

    store = resolve_store(workspace_root=tmp_path)
    store.put("memory/semantic", "orders", {"description": "orders revenue semantic model"})

    result = _call_tool(
        "search_semantic_memory",
        {"query": "revenue", "mode": "keyword", "limit": 5},
        read_only=True,
    )
    assert result["results"][0]["description"] == "orders revenue semantic model"


def test_mcp_regenerate_physical_writes_contract(tmp_path: Path):
    logical_path = tmp_path / "orders.model.json"
    contract_path = tmp_path / "orders.fluid.yaml"
    logical_path.write_text(
        _logical_model().model_dump_json(indent=2, by_alias=True), encoding="utf-8"
    )

    result = _call_tool(
        "regenerate_physical",
        {"path": str(logical_path), "engine": "sql", "contract_path": str(contract_path)},
        read_only=False,
    )
    assert Path(result["contract_path"]).exists()
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["labels"]["modelSidecar"] == "orders.fluid.yaml.model.json"
