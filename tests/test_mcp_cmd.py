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


# ---------------------------------------------------------------------------
# read_logical_model — typed-tool-error invariant on the MCP-SERVER path.
#
# Issue #392 hardened only one of two mirrored implementations:
# ``forge_copilot_tools._dispatch_read_logical_model`` (in-process toolkit).
# ``fluid mcp serve`` routes through ``cli/mcp/dispatch.py``, which called
# ``LogicalDraft.model_validate_json(path.read_text(...))`` bare — so a missing
# file round-tripped the absolute host path to the LLM, and a wrong-shape or
# invalid-JSON sidecar round-tripped a truncated echo of the FILE CONTENTS
# (pydantic's ``input_value=...``). Verified over the real stdio wire.
# ---------------------------------------------------------------------------


def test_read_logical_model_missing_file_returns_typed_error(tmp_path: Path):
    result = _call_tool("read_logical_model", {"path": str(tmp_path / "nope.json")}, read_only=True)

    assert result["error"] == "FileNotFoundError"
    assert "see server logs" in result["message"]
    assert "Errno" not in result["message"]
    assert str(tmp_path) not in result["message"]


def test_read_logical_model_invalid_json_does_not_echo_file_contents(tmp_path: Path):
    sidecar = tmp_path / "notjson.model.json"
    sidecar.write_text("not json {{{ PWD_ECHO_SENTINEL_ABC\n", encoding="utf-8")

    result = _call_tool("read_logical_model", {"path": str(sidecar)}, read_only=True)

    assert result["error"] == "ValidationError"
    assert "see server logs" in result["message"]
    assert "PWD_ECHO_SENTINEL_ABC" not in result["message"]


def test_read_logical_model_wrong_shape_does_not_echo_file_contents(tmp_path: Path):
    sidecar = tmp_path / "bad.model.json"
    sidecar.write_text(
        '{"entities": [{"name": 12}], "secret": "PWD_ECHO_SENTINEL_ABC"}', encoding="utf-8"
    )

    result = _call_tool("read_logical_model", {"path": str(sidecar)}, read_only=True)

    assert result["error"] == "ValidationError"
    assert "PWD_ECHO_SENTINEL_ABC" not in result["message"]
    assert "input_value" not in result["message"]


def test_the_mutating_sidecar_tools_share_the_sanitised_reader(tmp_path: Path):
    """update_entity / add_relationship / regenerate_physical read the same
    sidecar the same way; the sanitising must not be read-path-only."""
    import pytest

    from fluid_build.cli.mcp.dispatch import LogicalDraftError

    sidecar = tmp_path / "bad.model.json"
    sidecar.write_text('{"nope": "PWD_ECHO_SENTINEL_ABC"}', encoding="utf-8")

    for name, arguments in (
        ("update_entity", {"path": str(sidecar), "entity": "x", "updates": {}}),
        ("add_relationship", {"path": str(sidecar), "relationship": {}}),
        ("regenerate_physical", {"path": str(sidecar)}),
    ):
        with pytest.raises(LogicalDraftError) as excinfo:
            _call_tool(name, arguments, read_only=False)
        assert "PWD_ECHO_SENTINEL_ABC" not in str(excinfo.value), name
