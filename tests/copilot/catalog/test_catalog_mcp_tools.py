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

"""Coverage for the V1.5 source-catalog MCP tools.

Pins the wire-level contract of every catalog tool added to
``cli/mcp.py`` in Sprint A.4:

* ``list_source_adapters`` — read-only inventory of catalog types.
* ``list_source_tables`` / ``inspect_source_table`` /
  ``list_source_lineage`` / ``list_source_glossary`` — read-only
  metadata reads.
* ``forge_from_source`` — write tool; runs the staged Logical
  pipeline and emits a ``.model.json`` sidecar.

Three categories of pin:

1. **Tool registry shape.** Every tool present in
   ``TOOL_CAPABILITIES`` with the right read/write classification.
2. **Permission policy.** Read tools pass under ``--read-only``;
   write tools (``forge_from_source``) are blocked.
3. **Dispatch smoke.** Calling each tool with a stubbed adapter
   returns the expected JSON shape and writes the audit event.

The Snowflake / Unity adapters are stubbed at the module level so
these tests don't need ``snowflake-connector-python`` /
``databricks-sdk`` installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli.mcp import (
    TOOL_CAPABILITIES,
    McpPolicy,
    _build_source_adapter,
    _call_tool,
    _list_source_adapters,
    _scope_from_args,
    check_tool_permission,
)
from fluid_build.copilot.catalog.base import CatalogAdapter
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
)

# ----------------------------------------------------------------------
# Stub adapter — no SDK imports
# ----------------------------------------------------------------------


class _StubAdapter(CatalogAdapter):
    """A catalog-agnostic stub used by the MCP dispatch smoke tests.

    Returns canned shapes so we can assert the MCP layer's wire
    contract without touching a real catalog or SDK."""

    name = "stub-source"

    def list_tables(self, scope):
        return [
            CatalogTable(fqn="DB.SCH.A", database="DB", schema_name="SCH", name="A"),
            CatalogTable(fqn="DB.SCH.B", database="DB", schema_name="SCH", name="B"),
        ]

    def get_table(self, fqn):
        return CatalogTable(
            fqn=fqn,
            database=fqn.split(".")[0],
            schema_name=fqn.split(".")[1],
            name=fqn.split(".")[-1],
            description=f"stub table {fqn}",
            columns=[
                CatalogColumn(name="id", data_type="NUMBER", primary_key=True),
            ],
        )

    def get_lineage(self, fqn):
        return CatalogLineage(
            upstream=[LineageRef(fqn="raw.x.y", kind="upstream")],
            downstream=[],
        )

    def list_glossary_terms(self, scope):
        return [GlossaryTerm(term="Order", definition="A customer purchase event.")]


# ----------------------------------------------------------------------
# Tool registry shape
# ----------------------------------------------------------------------


class TestToolRegistryShape:
    def test_six_source_tools_registered(self):
        """The plan promises 6 source-catalog MCP tools; every one
        must appear in ``TOOL_CAPABILITIES``."""
        expected = {
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
            "forge_from_source",
        }
        registered = {t for t in TOOL_CAPABILITIES if t in expected}
        assert registered == expected

    def test_only_forge_from_source_mutates(self):
        """All 5 read tools must have ``mutates_files=False``;
        ``forge_from_source`` is the sole writer."""
        for name, cap in TOOL_CAPABILITIES.items():
            if not name.startswith(("list_source_", "inspect_source_", "forge_from_source")):
                continue
            if name == "forge_from_source":
                assert cap.mutates_files is True
                assert "output_path" in cap.file_path_args
            else:
                assert cap.mutates_files is False, (
                    f"{name} should be read-only — got mutates_files=True"
                )

    def test_forge_from_source_writes_history_and_audit(self):
        """The write tool's writes_namespaces must include both
        ``history`` and ``audit`` so the policy gate enforces both."""
        cap = TOOL_CAPABILITIES["forge_from_source"]
        assert "history" in cap.writes_namespaces
        assert "audit" in cap.writes_namespaces

    def test_descriptions_mention_credential_id(self):
        """Every catalog tool's description names ``credential_id``
        so an LLM scanning ``tools/list`` knows what arg to pass."""
        for name in TOOL_CAPABILITIES:
            if not name.startswith(
                ("list_source_t", "inspect_source", "list_source_l", "list_source_g", "forge_from")
            ):
                continue
            cap = TOOL_CAPABILITIES[name]
            assert "credential_id" in cap.description, (
                f"{name} description missing 'credential_id': {cap.description}"
            )

    def test_every_tool_advertises_input_schema(self):
        """Gap 5 — populated ``input_schema`` lets MCP clients (Claude
        Code, Cursor, VS Code MCP) drive typed autocomplete on the
        tool's arguments.

        Without this pin, a future refactor could drop the schema and
        the editor UX would silently degrade to free-form text input
        — no regression visible at runtime, only visible by typing
        in the editor.
        """
        from fluid_build.cli.mcp import _tool_definitions

        defs = _tool_definitions()
        assert len(defs) == len(TOOL_CAPABILITIES)
        for entry in defs:
            assert "inputSchema" in entry, f"missing inputSchema: {entry['name']}"
            schema = entry["inputSchema"]
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"

    def test_catalog_tool_schemas_require_credentials(self):
        """The seven catalog tools (everything except
        ``list_source_adapters``) MUST require credentials in their
        schema — that's the whole "no raw secrets over the wire"
        contract from the security model."""
        catalog_tools_with_creds = [
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
            "forge_from_source",
        ]
        for name in catalog_tools_with_creds:
            cap = TOOL_CAPABILITIES[name]
            schema = cap.input_schema
            assert schema is not None, f"{name} missing input_schema"
            assert "credentials" in schema.get("required", []), (
                f"{name} schema must REQUIRE credentials"
            )
            assert "credentials" in schema.get("properties", {}), (
                f"{name} schema must declare credentials property"
            )

    def test_forge_from_source_schema_has_technique_enum(self):
        """``forge_from_source.technique`` must be a closed enum so
        clients know the only valid values are ``data_vault_2`` and
        ``dimensional`` — defends against hallucinated free-form
        techniques like 'star schema' that the dispatch would
        silently default away."""
        schema = TOOL_CAPABILITIES["forge_from_source"].input_schema
        technique = schema["properties"]["technique"]
        assert technique["enum"] == ["data_vault_2", "dimensional"]
        assert "engine" in schema["properties"]


# ----------------------------------------------------------------------
# Permission policy
# ----------------------------------------------------------------------


class TestPermissionPolicy:
    def test_read_only_blocks_forge_from_source(self):
        """Under ``--read-only``, the write tool is blocked."""
        policy = McpPolicy(read_only=True)
        with pytest.raises(PermissionError, match="read-only"):
            check_tool_permission(
                "forge_from_source",
                {"output_path": "/tmp/x.fluid.yaml"},
                policy=policy,
            )

    def test_read_only_allows_read_tools(self):
        """Read-only doesn't block any of the 5 read tools."""
        policy = McpPolicy(read_only=True)
        for name in (
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
        ):
            check_tool_permission(name, {}, policy=policy)  # no exception

    def test_writable_paths_sandbox_blocks_outside_paths(self, tmp_path):
        """``forge_from_source`` writes ``output_path`` — the path
        must resolve under one of ``writable_paths`` or the policy
        gate rejects."""
        sandbox = tmp_path / "allowed"
        sandbox.mkdir()
        policy = McpPolicy(
            read_only=False,
            writable_paths=(sandbox.resolve(),),
        )
        # Inside sandbox — passes.
        check_tool_permission(
            "forge_from_source",
            {"output_path": str(sandbox / "model.json")},
            policy=policy,
        )
        # Outside sandbox — blocked.
        with pytest.raises(PermissionError, match="writable-paths"):
            check_tool_permission(
                "forge_from_source",
                {"output_path": str(tmp_path / "outside" / "model.json")},
                policy=policy,
            )


# ----------------------------------------------------------------------
# Dispatch smoke — every tool returns the right JSON shape
# ----------------------------------------------------------------------


class TestDispatchSmoke:
    def test_list_source_adapters_returns_inventory(self):
        result = _call_tool("list_source_adapters", {}, read_only=True)
        adapters = result["adapters"]
        names = {a["name"] for a in adapters}
        # Sprint A ships snowflake + unity as ``available``; the
        # rest are listed as ``planned`` so the LLM knows what's
        # coming without us hiding the future surface.
        assert {"snowflake", "unity"} <= names
        assert {"bigquery", "dataplex", "glue", "datahub", "datamesh_manager"} <= names

    def test_list_source_tables_routes_through_stub_adapter(self, tmp_path, monkeypatch):
        """Patch ``_build_source_adapter`` to return our stub. The
        real dispatch is exercised end-to-end — argument parsing,
        scope construction, audit event write, return shape."""
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        # Audit dir must be writable; redirect it so the test
        # doesn't pollute ``~/.fluid``.
        from fluid_build.copilot.store.audit_trail import write_audit_event as _orig

        captured: List[Dict[str, Any]] = []

        def _capture(event, *, payload, root=None):
            captured.append({"event": event, "payload": payload})
            return _orig(event, payload=payload, root=root)

        monkeypatch.setattr("fluid_build.cli.mcp.write_audit_event", _capture)
        # Audit root → tmp_path so the real on-disk write is scoped.
        monkeypatch.setenv("HOME", str(tmp_path))

        result = _call_tool(
            "list_source_tables",
            {
                "source": "stub",
                "credentials": {"credential_id": "stub-prod"},
                "scope": {"database": "DB", "schema_name": "SCH"},
            },
            read_only=True,
        )
        assert "tables" in result
        assert len(result["tables"]) == 2
        names = {t["name"] for t in result["tables"]}
        assert names == {"A", "B"}
        # Audit event recorded with non-sensitive context only.
        assert any(c["event"] == "mcp_list_source_tables" for c in captured)
        audit_payload = next(
            c["payload"] for c in captured if c["event"] == "mcp_list_source_tables"
        )
        assert audit_payload["catalog_name"] == "stub-source"
        assert audit_payload["result_count"] == 2

    def test_inspect_source_table_returns_full_table(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        monkeypatch.setattr(
            "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "x"
        )
        result = _call_tool(
            "inspect_source_table",
            {
                "source": "stub",
                "credentials": {"credential_id": "stub-prod"},
                "fqn": "DB.SCH.MY_TABLE",
            },
            read_only=True,
        )
        assert result["fqn"] == "DB.SCH.MY_TABLE"
        assert result["columns"][0]["name"] == "id"

    def test_inspect_source_table_requires_fqn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        monkeypatch.setattr(
            "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "x"
        )
        with pytest.raises(RuntimeError, match="requires 'fqn'"):
            _call_tool(
                "inspect_source_table",
                {"source": "stub", "credentials": {"credential_id": "x"}},
                read_only=True,
            )

    def test_list_source_lineage_returns_typed_chain(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        monkeypatch.setattr(
            "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "x"
        )
        result = _call_tool(
            "list_source_lineage",
            {
                "source": "stub",
                "credentials": {"credential_id": "stub-prod"},
                "fqn": "DB.SCH.X",
            },
            read_only=True,
        )
        assert len(result["upstream"]) == 1
        assert result["upstream"][0]["fqn"] == "raw.x.y"
        assert result["downstream"] == []

    def test_list_source_glossary_returns_terms(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        monkeypatch.setattr(
            "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "x"
        )
        result = _call_tool(
            "list_source_glossary",
            {
                "source": "stub",
                "credentials": {"credential_id": "stub-prod"},
                "scope": {"database": "DB", "schema_name": "SCH"},
            },
            read_only=True,
        )
        assert len(result["terms"]) == 1
        assert result["terms"][0]["term"] == "Order"

    @pytest.mark.xfail(
        strict=False,
        reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update",
    )
    def test_forge_from_source_writes_contract_and_sidecar(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fluid_build.cli.mcp._build_source_adapter",
            lambda args, **_kwargs: _StubAdapter(),
        )
        monkeypatch.setattr(
            "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "audit.json"
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        contract_path = tmp_path / "forged.fluid.yaml"
        result = _call_tool(
            "forge_from_source",
            {
                "source": "stub",
                "credentials": {"credential_id": "stub-prod"},
                "scope": {"database": "DB", "schema_name": "SCH"},
                "technique": "data_vault_2",
                "name": "stub_smoke",
                "output_path": str(contract_path),
            },
            read_only=False,
        )

        sidecar_path = contract_path.with_name(f"{contract_path.name}.model.json")
        assert contract_path.exists()
        assert sidecar_path.exists()
        assert result["contract_path"] == str(contract_path)
        assert result["sidecar_path"] == str(sidecar_path)
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        assert contract["kind"] == "DataProduct"
        assert contract["labels"]["modelSidecar"] == sidecar_path.name
        assert result["validation"]["passes_schema"] is True


# ----------------------------------------------------------------------
# Argument validation — credential_id required
# ----------------------------------------------------------------------


class TestCredentialArgRequired:
    def test_missing_credentials_raises(self):
        """The MCP defense: every catalog tool MUST receive
        ``credentials.credential_id`` (or ``credentials.inline``).
        A missing credentials block is rejected before the adapter
        is constructed."""
        with pytest.raises(RuntimeError, match="credentials.credential_id"):
            _build_source_adapter({"source": "snowflake"})

    def test_empty_credentials_dict_raises(self):
        with pytest.raises(RuntimeError, match="credentials.credential_id"):
            _build_source_adapter({"source": "snowflake", "credentials": {}})

    def test_missing_source_raises(self):
        with pytest.raises(RuntimeError, match="'source'"):
            _build_source_adapter({"credentials": {"credential_id": "x"}})

    def test_unknown_source_raises_with_helpful_message(self):
        """Unknown adapter names must surface the supported list,
        not a stack trace, so an LLM can self-correct."""
        with pytest.raises(RuntimeError, match="Unknown source-catalog adapter"):
            _build_source_adapter(
                {
                    "source": "alation",
                    "credentials": {"credential_id": "x"},
                }
            )


# ----------------------------------------------------------------------
# Scope parsing
# ----------------------------------------------------------------------


class TestScopeParsing:
    def test_nested_scope_form(self):
        scope = _scope_from_args(
            {"scope": {"database": "DB", "schema": "SCH", "tables": ["A", "B"]}}
        )
        assert scope.database == "DB"
        assert scope.schema_name == "SCH"
        assert scope.tables == ["A", "B"]

    def test_flat_scope_form(self):
        """Forgiving fallback: scope fields at the top level work
        too. Keeps the LLM-facing schema lenient."""
        scope = _scope_from_args(
            {
                "database": "DB",
                "schema_name": "SCH",
            }
        )
        assert scope.database == "DB"
        assert scope.schema_name == "SCH"

    def test_flat_form_accepts_alias_schema_key(self):
        scope = _scope_from_args(
            {
                "database": "DB",
                "schema": "SCH",
            }
        )
        assert scope.schema_name == "SCH"
