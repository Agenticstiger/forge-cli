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

"""MCP tools — score_contract_quality + enrich_contract_suggestions.

These tools let command_center (and any MCP client — Claude Code,
Cursor, Kiro) invoke the JudgeAgent and the deterministic enrichment
pass remotely, without bloating the CLI surface. The CLI itself stays
lightweight; heavy use cases go through MCP.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build._mcp_compat import attr as _mcp_attr
from fluid_build.cli.mcp import (
    TOOL_CAPABILITIES,
    _call_tool,
    _dispatch_enrich_contract_suggestions,
    _dispatch_score_contract_quality,
    _resolve_contract_argument,
)
from fluid_build.copilot.agents.judge_agent import AxisScore, JudgeAgent, JudgeResult

SAMPLE_CONTRACT = {
    "fluidVersion": "0.7.3",
    "kind": "DataProduct",
    "id": "x.y.sample",
    "name": "sample",
    "domain": "x",
    "metadata": {"layer": "Silver", "productType": "ADP", "refreshCadence": "hourly"},
    "builds": [{"id": "b", "engine": "dbt"}],
    "exposes": [
        {
            "exposeId": "sample_curated",
            "kind": "table",
            "binding": {"platform": "snowflake", "format": "table"},
            "contract": {
                "schema": [
                    {"name": "id", "type": "BIGINT", "primary_key": True},
                    {"name": "amount", "type": "DECIMAL", "min": 0, "max": 1000},
                ]
            },
        }
    ],
}


def _mock_judge_result(total: int = 24) -> JudgeResult:
    return JudgeResult(
        axes={
            axis: AxisScore(score=4, reasoning="r", suggestions=["s"]) for axis in JudgeAgent.AXES
        },
        total=total,
        model="claude-haiku-4-5",
    )


# ---------------------------------------------------------------------------
# Capability registration
# ---------------------------------------------------------------------------


def test_score_contract_quality_capability_registered():
    cap = TOOL_CAPABILITIES["score_contract_quality"]
    assert cap.name == "score_contract_quality"
    assert cap.mutates_files is False
    assert "contract_path" in cap.read_path_args
    assert cap.input_schema is not None
    assert "contract_path" in cap.input_schema["properties"]
    assert "contract" in cap.input_schema["properties"]
    assert "include_artifacts" in cap.input_schema["properties"]


def test_enrich_contract_suggestions_capability_registered():
    cap = TOOL_CAPABILITIES["enrich_contract_suggestions"]
    assert cap.name == "enrich_contract_suggestions"
    assert cap.mutates_files is False
    assert "contract_path" in cap.read_path_args


# ---------------------------------------------------------------------------
# Argument resolution
# ---------------------------------------------------------------------------


def test_resolve_contract_from_inline_dict():
    contract = _resolve_contract_argument({"contract": SAMPLE_CONTRACT})
    assert contract["id"] == "x.y.sample"


def test_resolve_contract_from_path(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(SAMPLE_CONTRACT), encoding="utf-8")
    contract = _resolve_contract_argument({"contract_path": str(p)})
    assert contract["id"] == "x.y.sample"


def test_resolve_contract_path_wins_over_inline(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"fluidVersion": "0.7.3", "id": "from-path"}), encoding="utf-8")
    contract = _resolve_contract_argument(
        {"contract_path": str(p), "contract": {"id": "from-inline"}}
    )
    assert contract["id"] == "from-path"


def test_resolve_contract_requires_one_arg():
    with pytest.raises(RuntimeError, match="contract_path"):
        _resolve_contract_argument({})


# ---------------------------------------------------------------------------
# score_contract_quality dispatch
# ---------------------------------------------------------------------------


def test_score_contract_quality_returns_full_scorecard():
    with patch.object(JudgeAgent, "judge", return_value=_mock_judge_result(total=24)):
        result = _dispatch_score_contract_quality({"contract": SAMPLE_CONTRACT})
    assert result["total"] == 24
    assert result["max_total"] == 30
    assert result["model"] == "claude-haiku-4-5"
    assert set(result["axes"].keys()) == set(JudgeAgent.AXES)
    assert all(isinstance(s, int) for s in result["axes"].values())
    # Axis reasoning + suggestions are first-class so command_center
    # can render them directly without re-running the judge.
    assert all(
        JudgeAgent.AXES[0] in d for d in (result["axis_reasoning"], result["axis_suggestions"])
    )


def test_score_contract_quality_passes_artifacts_when_requested():
    with (
        patch.object(JudgeAgent, "judge", return_value=_mock_judge_result()) as mock_judge,
        patch(
            "fluid_build.copilot.enrichment.enrich_contract",
            return_value={
                "provider": "snowflake",
                "dbt_tests": [],
                "freshness": {},
                "physical_layout": [],
            },
        ),
    ):
        _dispatch_score_contract_quality({"contract": SAMPLE_CONTRACT, "include_artifacts": True})
    # The judge was called WITH non-None build_artifacts.
    call_kwargs = mock_judge.call_args.kwargs
    assert call_kwargs["build_artifacts"] is not None


def test_score_contract_quality_passes_none_when_not_requested():
    with patch.object(JudgeAgent, "judge", return_value=_mock_judge_result()) as mock_judge:
        _dispatch_score_contract_quality({"contract": SAMPLE_CONTRACT})
    call_kwargs = mock_judge.call_args.kwargs
    assert call_kwargs["build_artifacts"] is None


def test_score_contract_quality_does_not_write_files(tmp_path: Path):
    p = tmp_path / "c.yaml"
    original_text = yaml.safe_dump(SAMPLE_CONTRACT)
    p.write_text(original_text, encoding="utf-8")
    with patch.object(JudgeAgent, "judge", return_value=_mock_judge_result()):
        _dispatch_score_contract_quality({"contract_path": str(p)})
    # File on disk is byte-identical.
    assert p.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# enrich_contract_suggestions dispatch
# ---------------------------------------------------------------------------


def test_enrich_contract_suggestions_returns_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _dispatch_enrich_contract_suggestions({"contract": SAMPLE_CONTRACT})
    assert result["enabled"] is True
    art = result["artifacts"]
    assert art["provider"] == "snowflake"
    assert art["refresh_cadence"] == "hourly"


def test_enrich_contract_suggestions_returns_disabled_when_env_off(monkeypatch):
    monkeypatch.setenv("FLUID_COPILOT_ENRICHMENT", "0")
    result = _dispatch_enrich_contract_suggestions({"contract": SAMPLE_CONTRACT})
    assert result["enabled"] is False
    assert result["artifacts"] is None


def test_enrich_contract_suggestions_does_not_write_files(tmp_path):
    p = tmp_path / "c.yaml"
    original_text = yaml.safe_dump(SAMPLE_CONTRACT)
    p.write_text(original_text, encoding="utf-8")
    _dispatch_enrich_contract_suggestions({"contract_path": str(p)})
    assert p.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# _call_tool router
# ---------------------------------------------------------------------------


def test_call_tool_routes_score_contract_quality():
    with patch.object(JudgeAgent, "judge", return_value=_mock_judge_result(total=22)):
        result = _call_tool(
            "score_contract_quality",
            {"contract": SAMPLE_CONTRACT},
            read_only=True,
        )
    assert result["total"] == 22


def test_call_tool_routes_enrich_contract_suggestions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _call_tool(
        "enrich_contract_suggestions",
        {"contract": SAMPLE_CONTRACT},
        read_only=True,
    )
    assert result["enabled"] is True


# ---------------------------------------------------------------------------
# FastMCP-advertised inputSchema (H4 regression)
#
# Before the H4 refactor, ``TOOL_CAPABILITIES[*].input_schema`` was dead
# code on the MCP wire — FastMCP derived the inputSchema from the bare
# function signature, dropping every curated description + enum. These
# tests pin the post-refactor behaviour: descriptions + enums + nested
# Pydantic envelopes reach the client via ``tools/list``.
# ---------------------------------------------------------------------------


def _get_advertised_input_schema(tool_name: str) -> dict:
    """Resolve the inputSchema that FastMCP will publish in ``tools/list``.

    Uses ``_mcp_app.list_tools()`` so we hit the SAME code path the SDK
    invokes for a live MCP client — not the curated registry directly.
    """
    import asyncio

    from fluid_build.cli.mcp import _get_mcp_app

    async def _go() -> dict:
        tools = await _get_mcp_app().list_tools()
        for tool in tools:
            if tool.name == tool_name:
                return _mcp_attr(tool, "input_schema", "inputSchema")  # type: ignore[no-any-return]
        raise AssertionError(f"Tool {tool_name!r} not advertised")

    return asyncio.run(_go())


def test_advertised_schema_list_source_tables_carries_source_enum():
    """The seven curated catalog sources must surface as a JSON Schema enum
    so Claude Code / Cursor / Kiro can autocomplete the value.

    Before H4 this advertised ``{type: string}`` with no enum — the
    curated ``TOOL_CAPABILITIES["list_source_tables"].input_schema``
    never reached the wire.
    """
    schema = _get_advertised_input_schema("list_source_tables")
    source_prop = schema["properties"]["source"]
    assert source_prop["type"] == "string"
    assert set(source_prop["enum"]) == {
        "snowflake",
        "unity",
        "bigquery",
        "dataplex",
        "glue",
        "datahub",
        "datamesh_manager",
    }
    # Description must reach the client too.
    assert "dispatch" in source_prop["description"].lower()


def test_advertised_schema_list_source_tables_carries_credentials_envelope():
    """The credentials envelope must reach the client as a nested object
    schema with ``credential_id`` documented — not a bare ``{type: object}``.
    """
    schema = _get_advertised_input_schema("list_source_tables")
    # FastMCP refs the BaseModel via $defs; resolve it.
    cred_ref = schema["properties"]["credentials"]["$ref"]
    cred_name = cred_ref.split("/")[-1]
    cred_def = schema["$defs"][cred_name]
    assert "credential_id" in cred_def["properties"]
    # The description names the sources.yaml lookup chain.
    cred_id_desc = cred_def["properties"]["credential_id"]["description"]
    assert "sources.yaml" in cred_id_desc
    # Top-level credentials prop must carry the curated description.
    assert "credential_id" in schema["properties"]["credentials"]["description"]


def test_advertised_schema_list_source_tables_carries_scope_envelope():
    """The scope envelope must surface every documented field."""
    schema = _get_advertised_input_schema("list_source_tables")
    scope_ref = schema["properties"]["scope"]["$ref"]
    scope_name = scope_ref.split("/")[-1]
    scope_def = schema["$defs"][scope_name]
    # ``database`` + ``schema`` + ``catalog`` + ``tables`` all present.
    assert "database" in scope_def["properties"]
    assert "schema" in scope_def["properties"]
    assert "catalog" in scope_def["properties"]
    assert "tables" in scope_def["properties"]
    # Strict-by-default — curated registry pins ``additionalProperties: false``.
    assert scope_def["additionalProperties"] is False


def test_advertised_schema_score_contract_quality_carries_descriptions():
    """Curated descriptions for ``contract_path`` / ``contract`` /
    ``include_artifacts`` must reach the client."""
    schema = _get_advertised_input_schema("score_contract_quality")
    props = schema["properties"]
    assert "contract.fluid.yaml" in props["contract_path"]["description"]
    assert "Inline contract" in props["contract"]["description"]
    assert "enrichment" in props["include_artifacts"]["description"]


def test_advertised_schema_forge_from_source_includes_jdbc_sources():
    """H19 regression — JDBC sources (postgres, postgresql, mysql, sqlite)
    must appear in ``forge_from_source``'s source enum so MCP clients can
    autocomplete them and the dispatcher can route them to the JDBC path."""
    schema = _get_advertised_input_schema("forge_from_source")
    source_prop = schema["properties"]["source"]
    assert {"postgres", "postgresql", "mysql", "sqlite"}.issubset(set(source_prop["enum"]))
    # All seven catalog sources still present.
    assert {"snowflake", "unity", "bigquery"}.issubset(set(source_prop["enum"]))
    # ``uri`` param must be documented (used by JDBC path).
    assert "uri" in schema["properties"]
    assert "JDBC" in schema["properties"]["uri"]["description"]


def test_advertised_schema_inspect_source_table_carries_fqn_description():
    """Curated FQN description must reach the client for every catalog tool
    that takes a ``fqn`` argument."""
    schema = _get_advertised_input_schema("inspect_source_table")
    fqn_desc = schema["properties"]["fqn"]["description"]
    # Curated description names each catalog format. At least one must
    # survive the trip.
    assert "Snowflake" in fqn_desc or "snowflake" in fqn_desc.lower()


def test_curated_registry_matches_advertised_schema_for_source_tools():
    """TOOL_CAPABILITIES[*].input_schema and the FastMCP-advertised schema
    MUST agree on the source enum for every catalog-only tool. This
    catches drift when adapters are added to one surface but not the
    other.
    """
    from fluid_build.cli.mcp import TOOL_CAPABILITIES

    for tool_name in (
        "list_source_tables",
        "inspect_source_table",
        "list_source_lineage",
        "list_source_glossary",
    ):
        curated = TOOL_CAPABILITIES[tool_name].input_schema
        curated_enum = set(curated["properties"]["source"]["enum"])
        advertised = _get_advertised_input_schema(tool_name)
        advertised_enum = set(advertised["properties"]["source"]["enum"])
        assert curated_enum == advertised_enum, (
            f"Schema drift in {tool_name}: curated={curated_enum} " f"advertised={advertised_enum}"
        )


def test_forge_from_source_curated_enum_includes_jdbc():
    """The curated registry must list JDBC sources alongside catalog
    sources — otherwise the legacy ``_tool_definitions()`` path (still
    consumed by tests + downstream tooling) reports stale capabilities.
    """
    from fluid_build.cli.mcp import TOOL_CAPABILITIES

    curated_enum = set(
        TOOL_CAPABILITIES["forge_from_source"].input_schema["properties"]["source"]["enum"]
    )
    assert {"postgres", "postgresql", "mysql", "sqlite"}.issubset(curated_enum)


def test_list_source_adapters_reports_eleven_sources():
    """``list_source_adapters`` must enumerate all 11 sources (7 catalog +
    4 JDBC). Before H19 only 7 were returned, masking JDBC support."""
    from fluid_build.cli.mcp import _list_source_adapters

    adapters = _list_source_adapters()
    names = {entry["name"] for entry in adapters}
    assert names == {
        "snowflake",
        "unity",
        "bigquery",
        "dataplex",
        "glue",
        "datahub",
        "datamesh_manager",
        "postgres",
        "postgresql",
        "mysql",
        "sqlite",
    }
    # Kind discriminator must split catalog vs jdbc cleanly.
    kinds = {entry["name"]: entry["kind"] for entry in adapters}
    assert kinds["postgres"] == "jdbc"
    assert kinds["snowflake"] == "catalog"
