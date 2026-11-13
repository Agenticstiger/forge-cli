# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the MCP server advertises for its catalog tools, and how it reads a contract.

The advertised ``inputSchema`` must carry the curated descriptions and enums,
and a contract the tools read must stay inside ``--readable-paths``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build._mcp_compat import attr as _mcp_attr
from fluid_build.cli.mcp.dispatch import _contract_file

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


def test_list_source_adapters_reports_the_catalog_sources_only():
    """``list_source_adapters`` lists the 7 catalogs the catalog tools read. JDBC databases
    are not listed: no MCP tool reads them since ``forge_from_source`` was dropped."""
    from fluid_build.cli.mcp import _list_source_adapters

    adapters = _list_source_adapters()
    assert {entry["name"] for entry in adapters} == {
        "snowflake",
        "unity",
        "bigquery",
        "dataplex",
        "glue",
        "datahub",
        "datamesh_manager",
    }
    assert {entry["kind"] for entry in adapters} == {"catalog"}


def test_a_fragment_outside_the_readable_paths_is_refused(tmp_path: Path, monkeypatch):
    """A workspace ref_root may widen the ref root past --readable-paths: a contract the
    policy lets the tool read must not compose, and echo, a file the policy does not."""
    from fluid_build.cli import mcp as mcp_mod
    from fluid_build.cli.mcp.policy import McpPolicy

    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "s.yaml").write_text("api_key: SECRET-1\n", encoding="utf-8")
    (tmp_path / "fluid.workspace.yaml").write_text("ref_root: .\n", encoding="utf-8")
    product = tmp_path / "products" / "a"
    product.mkdir(parents=True)
    contract = product / "c.yaml"
    split = {**SAMPLE_CONTRACT, "description": {"$ref": "../../secret/s.yaml"}}
    contract.write_text(yaml.safe_dump(split), encoding="utf-8")
    policy = McpPolicy(readable_paths=(product.resolve(),), writable_paths=(product.resolve(),))
    monkeypatch.setattr(mcp_mod, "_policy", lambda: policy)

    # Refused before the file is opened (the ref root is capped to the readable path).
    from fluid_build.util.ref_confinement import RefResolutionError

    with pytest.raises((PermissionError, RefResolutionError)) as err:
        _contract_file(str(contract))
    assert "SECRET-1" not in str(err.value)
    # Inside the readable root it composes, as before.
    wide = McpPolicy(readable_paths=(tmp_path.resolve(),), writable_paths=(tmp_path.resolve(),))
    monkeypatch.setattr(mcp_mod, "_policy", lambda: wide)
    assert _contract_file(str(contract))["description"] == {
        "api_key": "SECRET-1"  # pragma: allowlist secret
    }


def test_a_ref_outside_the_readable_paths_fails_before_the_file_is_read(
    tmp_path: Path, monkeypatch
):
    """A composition that fails must not quote a file outside --readable-paths either: the
    ref root is capped to the readable path before any fragment is opened."""
    from fluid_build.cli import mcp as mcp_mod
    from fluid_build.cli.mcp.policy import McpPolicy

    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "app.yaml").write_text(
        "db: x\npassword=TOPSECRET-1: x: y\n", encoding="utf-8"  # pragma: allowlist secret
    )
    (tmp_path / "fluid.workspace.yaml").write_text("ref_root: .\n", encoding="utf-8")
    product = tmp_path / "products" / "a"
    product.mkdir(parents=True)
    contract = product / "c.yaml"
    split = {**SAMPLE_CONTRACT, "description": {"$ref": "../../secret/app.yaml"}}
    contract.write_text(yaml.safe_dump(split), encoding="utf-8")
    policy = McpPolicy(readable_paths=(product.resolve(),), writable_paths=(product.resolve(),))
    monkeypatch.setattr(mcp_mod, "_policy", lambda: policy)

    with pytest.raises(Exception) as err:
        _contract_file(str(contract))
    assert "TOPSECRET" not in str(err.value) and "escapes the ref root" in str(err.value)
