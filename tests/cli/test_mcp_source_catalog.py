# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid mcp serve``'s ``source_catalog`` tool and what came with it.

* one tool, with ``kind``, replaces the five source tools, and each kind's
  missing argument is named before any adapter is built;
* ``response_format``: ``concise`` (the default) and ``detailed``;
* a result over the inline budget comes back as an envelope plus a
  ``resource_link`` that ``resources/read`` serves (and, over stdio, a file);
* the server's ``instructions``;
* an unknown name in ``--allow-tools`` / ``--deny-tools`` stops the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build._mcp_compat import attr
from fluid_build.cli.mcp import artifacts
from fluid_build.cli.mcp.dispatch import _MISSING_ARGUMENT_HINTS, McpToolUsageError, _call_tool
from fluid_build.cli.mcp.models import (
    RETIRED_TOOL_HINTS,
    SERVER_INSTRUCTIONS,
    SOURCE_CATALOG_KINDS,
    TOOL_CAPABILITIES,
)
from fluid_build.cli.mcp.policy import _build_policy_from_args, tool_name_refusal
from fluid_build.copilot.catalog.base import CatalogAdapter
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogLineage,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
)

_CREDS = {"credential_id": "stub-prod"}
_FIVE = (
    "list_source_adapters",
    "list_source_tables",
    "inspect_source_table",
    "list_source_lineage",
    "list_source_glossary",
)


class _StubAdapter(CatalogAdapter):
    """Canned metadata with the fields ``concise`` drops: vendor payloads,
    empty values and false key flags."""

    name = "stub-source"

    def list_tables(self, scope):
        return [
            CatalogTable(
                fqn=f"DB.SCH.T{i}",
                database="DB",
                schema_name="SCH",
                name=f"T{i}",
                description=f"Table {i}",
                owner="data-team",
                catalog_specific={"raw": "x" * 40},
            )
            for i in range(2)
        ]

    def get_table(self, fqn):
        return CatalogTable(
            fqn=fqn,
            database="DB",
            schema_name="SCH",
            name=fqn.rsplit(".", 1)[-1],
            description="",
            owner="data-team",
            tags={"pii": "", "tier": "gold"},
            columns=[
                CatalogColumn(name="id", data_type="NUMBER", nullable=False, primary_key=True),
                CatalogColumn(
                    name="email",
                    data_type="VARCHAR",
                    description="",
                    catalog_specific={"comment": "raw"},
                ),
            ],
            catalog_specific={"vendor": {"retention": 7}},
        )

    def get_lineage(self, fqn):
        return CatalogLineage(
            upstream=[LineageRef(fqn="RAW.S.A", kind="upstream", transformation_type="view")],
            downstream=[LineageRef(fqn="MART.S.B", kind="downstream")],
        )

    def list_glossary_terms(self, scope):
        return [GlossaryTerm(term="Order", definition="A purchase.", catalog_specific={"id": 9})]


@pytest.fixture
def built(monkeypatch, tmp_path):
    """Stub the adapter builder (recording each call) and the audit writer."""
    calls = []

    def _build(arguments, **_kwargs):
        calls.append(arguments)
        return _StubAdapter()

    monkeypatch.setattr("fluid_build.cli.mcp._build_source_adapter", _build)
    monkeypatch.setattr(
        "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "audit.json"
    )
    return calls


@pytest.fixture
def store(monkeypatch):
    """A fresh artifact store, with files off unless a test turns them on."""
    fresh = artifacts.ArtifactStore()
    monkeypatch.setattr(artifacts, "_STORE", fresh)
    monkeypatch.setattr(artifacts, "_WRITE_FILES", False)
    yield fresh
    fresh.close()


def _call(**arguments):
    return _call_tool("source_catalog", arguments, read_only=True)


# ---------------------------------------------------------------------------
# One tool
# ---------------------------------------------------------------------------


def test_one_source_tool_replaces_the_five():
    assert "source_catalog" in TOOL_CAPABILITIES
    assert not set(_FIVE) & set(TOOL_CAPABILITIES)
    assert set(_FIVE) <= set(RETIRED_TOOL_HINTS)
    schema = TOOL_CAPABILITIES["source_catalog"].input_schema
    assert schema["required"] == ["kind"]
    assert schema["properties"]["kind"]["enum"] == list(SOURCE_CATALOG_KINDS)
    assert schema["properties"]["response_format"]["default"] == "concise"


def test_kind_adapters_needs_nothing_else_and_builds_no_adapter(built):
    result = _call(kind="adapters")
    assert result["kind"] == "adapters"
    assert {"snowflake", "unity", "bigquery"} <= {a["name"] for a in result["adapters"]}
    assert built == []


@pytest.mark.parametrize(
    "kind, arguments, needs",
    [
        ("tables", {}, ["source", "credentials", "scope"]),
        ("tables", {"source": "snowflake"}, ["credentials", "scope"]),
        ("tables", {"source": "snowflake", "credentials": _CREDS}, ["scope"]),
        ("table", {"source": "snowflake", "credentials": _CREDS}, ["fqn"]),
        ("lineage", {"source": "snowflake", "credentials": _CREDS, "fqn": "  "}, ["fqn"]),
        ("glossary", {"credentials": _CREDS}, ["source"]),
    ],
    ids=[
        "tables-all",
        "tables-credentials",
        "tables-scope",
        "table-fqn",
        "lineage-blank-fqn",
        "glossary-source",
    ],
)
def test_a_missing_argument_is_named_before_any_adapter_is_built(built, kind, arguments, needs):
    with pytest.raises(McpToolUsageError) as exc:
        _call(kind=kind, **arguments)
    message = str(exc.value)
    assert message.startswith(f"source_catalog kind={kind} needs ")
    for key, hint in _MISSING_ARGUMENT_HINTS.items():
        assert (hint in message) == (key in needs), key
    assert built == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"kind": "columns"}, "kind must be one of adapters, tables, table, lineage, glossary"),
        ({"kind": "adapters", "response_format": "full"}, "response_format must be one of"),
    ],
    ids=["kind", "response_format"],
)
def test_an_unknown_kind_or_format_is_a_usage_error(built, arguments, fragment):
    with pytest.raises(McpToolUsageError, match=fragment):
        _call(**arguments)


# ---------------------------------------------------------------------------
# response_format
# ---------------------------------------------------------------------------

_SNOWFLAKE = {"source": "snowflake", "credentials": _CREDS}
_SCOPE = {"scope": {"database": "DB", "schema": "SCH"}}


def test_an_empty_scope_object_is_a_scope(built):
    """``scope: {}`` passes, as it did for ``list_source_tables``: Data Mesh
    Manager lists every domain on an empty scope; other adapters refuse."""
    assert _call(kind="tables", scope={}, **_SNOWFLAKE)["kind"] == "tables"


def test_concise_tables_are_fqn_name_and_description(built):
    concise = _call(kind="tables", **_SNOWFLAKE, **_SCOPE)
    assert concise == {
        "kind": "tables",
        "tables": [
            {"fqn": "DB.SCH.T0", "name": "T0", "description": "Table 0"},
            {"fqn": "DB.SCH.T1", "name": "T1", "description": "Table 1"},
        ],
    }
    detailed = _call(kind="tables", response_format="detailed", **_SNOWFLAKE, **_SCOPE)
    assert detailed["tables"] == [
        t.model_dump(mode="json", by_alias=True) for t in _StubAdapter().list_tables(None)
    ]


def test_concise_table_drops_vendor_payloads_empty_values_and_false_key_flags(built):
    table = _call(kind="table", fqn="DB.SCH.ORDERS", **_SNOWFLAKE)["table"]
    assert "catalog_specific" not in json.dumps(table)
    assert "description" not in table  # empty string
    assert table["tags"] == {"pii": "", "tier": "gold"}  # a key-only tag is still a tag
    id_column, email_column = table["columns"]
    assert id_column["primary_key"] is True
    assert id_column["nullable"] is False  # a false flag that means something stays
    assert "primary_key" not in email_column and "partition_key" not in email_column
    assert email_column["nullable"] is True
    assert "description" not in email_column

    detailed = _call(kind="table", fqn="DB.SCH.ORDERS", response_format="detailed", **_SNOWFLAKE)
    expected = _StubAdapter().get_table("DB.SCH.ORDERS").model_dump(mode="json", by_alias=True)
    assert detailed == {"kind": "table", "table": expected}


def test_concise_lineage_is_the_fqns_on_each_side(built):
    concise = _call(kind="lineage", fqn="DB.SCH.ORDERS", **_SNOWFLAKE)
    assert concise == {
        "kind": "lineage",
        "lineage": {"upstream": ["RAW.S.A"], "downstream": ["MART.S.B"]},
    }
    detailed = _call(kind="lineage", fqn="DB.SCH.ORDERS", response_format="detailed", **_SNOWFLAKE)
    assert detailed["lineage"]["upstream"][0]["transformation_type"] == "view"


def test_concise_glossary_terms_are_pruned(built):
    concise = _call(kind="glossary", **_SNOWFLAKE)
    assert concise == {
        "kind": "glossary",
        "terms": [{"term": "Order", "definition": "A purchase."}],
    }
    detailed = _call(kind="glossary", response_format="detailed", **_SNOWFLAKE)
    assert detailed["terms"][0]["catalog_specific"] == {"id": 9}


# ---------------------------------------------------------------------------
# Large results
# ---------------------------------------------------------------------------


def _large_result():
    return {"kind": "tables", "tables": [{"fqn": f"DB.SCH.T{i}"} for i in range(50)]}


def test_a_result_within_the_budget_is_returned_as_it_is(store):
    result = {"kind": "adapters", "adapters": []}
    assert artifacts.offload_if_large(result, tool="source_catalog") is result


def test_a_large_result_becomes_an_envelope_and_a_resource_link(store, monkeypatch):
    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, "10")
    result = _large_result()
    out = artifacts.offload_if_large(result, tool="source_catalog", hint="Ask for less.")

    text_block, link = out.content
    envelope = json.loads(text_block.text)
    assert attr(out, "structured_content", "structuredContent") == {"result": envelope}
    assert attr(out, "is_error", "isError") is False
    assert envelope["offloaded"] is True
    assert envelope["tool"] == "source_catalog"
    assert envelope["inline_token_limit"] == 10
    assert envelope["summary"] == {"kind": "tables", "counts": {"tables": 50}}
    assert envelope["next"].endswith("Ask for less.")
    assert "path" not in envelope

    assert link.type == "resource_link"
    assert str(link.uri) == envelope["resource"]
    assert envelope["resource"].startswith(artifacts.URI_PREFIX)
    assert attr(link, "mime_type", "mimeType") == "application/json"
    full = artifacts.read(envelope["resource"][len(artifacts.URI_PREFIX) :])
    assert json.loads(full) == result
    assert link.size == envelope["bytes"] == len(full.encode("utf-8"))


def test_over_stdio_the_envelope_also_names_a_private_file(store, monkeypatch):
    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, "10")
    monkeypatch.setattr(artifacts, "_WRITE_FILES", True)
    out = artifacts.offload_if_large(_large_result(), tool="source_catalog")
    envelope = attr(out, "structured_content", "structuredContent")["result"]
    path = Path(envelope["path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    artifact_id = envelope["resource"][len(artifacts.URI_PREFIX) :]
    assert path.read_text(encoding="utf-8") == artifacts.read(artifact_id)
    store.close()
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "raw, limit, warns",
    [("0", 0, False), ("2500", 2500, False), ("many", 6000, True), ("-5", 6000, True)],
)
def test_the_inline_budget_setting(monkeypatch, caplog, raw, limit, warns):
    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, raw)
    with caplog.at_level("WARNING", logger=artifacts.__name__):
        assert artifacts.inline_token_limit() == limit
    assert (artifacts.INLINE_TOKEN_LIMIT_ENV in caplog.text) == warns


def test_a_zero_budget_sends_everything_inline(store, monkeypatch):
    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, "0")
    result = _large_result()
    assert artifacts.offload_if_large(result, tool="source_catalog") is result


def test_the_store_keeps_only_the_newest_results(tmp_path):
    by_count = artifacts.ArtifactStore(max_artifacts=2)
    try:
        first, first_path = by_count.put("a" * 10, write_file=True)
        second, _ = by_count.put("b" * 10, write_file=True)
        third, _ = by_count.put("c" * 10, write_file=True)
        assert by_count.get(first) is None
        assert not os.path.exists(first_path)
        assert by_count.get(second) == "b" * 10 and by_count.get(third) == "c" * 10
    finally:
        by_count.close()
    by_bytes = artifacts.ArtifactStore(max_total_bytes=10)
    older, _ = by_bytes.put("x" * 8, write_file=False)
    newer, _ = by_bytes.put("y" * 8, write_file=False)
    assert by_bytes.get(older) is None and by_bytes.get(newer) == "y" * 8


def test_reading_an_unknown_result_says_what_to_do(store):
    with pytest.raises(LookupError, match="Call the tool again"):
        artifacts.read("0" * 32)


def test_the_budget_counts_the_indented_text_the_call_would_send(store, monkeypatch):
    """The SDK sends a dict result as JSON indented by 2, so a result whose
    compact form fits can still be over the budget."""
    result = _large_result()
    compact = artifacts.estimate_tokens(json.dumps(result, separators=(",", ":")))
    import pydantic_core

    indented = -(-len(pydantic_core.to_json(result, indent=2)) // 4)
    assert compact < indented
    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, str(compact))
    out = artifacts.offload_if_large(result, tool="source_catalog")
    envelope = attr(out, "structured_content", "structuredContent")["result"]
    assert envelope["estimated_tokens"] == indented


def test_a_large_result_is_read_back_through_the_sdk(built, store, monkeypatch):
    """A real MCP session on the installed SDK generation: the tool call
    returns the link, and ``resources/read`` on it returns the full result."""
    from fluid_build._mcp_compat import lowlevel_server, open_inmemory_session
    from fluid_build.cli.mcp import policy as policy_module

    monkeypatch.setenv(artifacts.INLINE_TOKEN_LIMIT_ENV, "20")
    policy = _build_policy_from_args(SimpleNamespace())
    monkeypatch.setattr(policy_module, "_current_policy", policy)
    app = policy_module._build_fastmcp_app(policy)

    async def scenario():
        async with open_inmemory_session(lowlevel_server(app)) as session:
            result = await session.call_tool(
                "source_catalog",
                {"kind": "tables", "source": "snowflake", "credentials": _CREDS, **_SCOPE},
            )
            blocks = {block.type: block for block in result.content}
            read = await session.read_resource(blocks["resource_link"].uri)
            return json.loads(blocks["text"].text), blocks["resource_link"], read

    envelope, link, read = asyncio.run(scenario())
    assert envelope["offloaded"] is True
    assert envelope["resource"] == str(link.uri)
    full = json.loads(read.contents[0].text)
    assert full["kind"] == "tables"
    assert [t["fqn"] for t in full["tables"]] == ["DB.SCH.T0", "DB.SCH.T1"]


def test_a_scope_that_names_no_tables_reaches_the_adapter_through_the_sdk(built, monkeypatch):
    """Through the SDK the scope arrives as a ``ScopeArg``; dumped with its
    unset fields as ``None`` it failed ``CatalogScope`` validation, so a
    listing with an ordinary scope failed (on main too, as ``list_source_tables``)."""
    from fluid_build._mcp_compat import lowlevel_server, open_inmemory_session
    from fluid_build.cli.mcp import policy as policy_module

    policy = _build_policy_from_args(SimpleNamespace())
    monkeypatch.setattr(policy_module, "_current_policy", policy)
    app = policy_module._build_fastmcp_app(policy)

    async def scenario():
        async with open_inmemory_session(lowlevel_server(app)) as session:
            return await session.call_tool(
                "source_catalog",
                {"kind": "tables", "source": "snowflake", "credentials": _CREDS, **_SCOPE},
            )

    result = asyncio.run(scenario())
    assert attr(result, "is_error", "isError") is False
    assert [t["fqn"] for t in json.loads(result.content[0].text)["tables"]] == [
        "DB.SCH.T0",
        "DB.SCH.T1",
    ]
    assert built[0]["scope"] == {"database": "DB", "schema": "SCH"}


def test_an_expired_result_says_why_through_the_sdk(store, monkeypatch):
    """On SDK 2.x only a ResourceError keeps its message; any other exception
    reaches the client as "Error creating resource from template"."""
    from fluid_build._mcp_compat import lowlevel_server, open_inmemory_session
    from fluid_build.cli.mcp import policy as policy_module

    policy = _build_policy_from_args(SimpleNamespace())
    monkeypatch.setattr(policy_module, "_current_policy", policy)
    app = policy_module._build_fastmcp_app(policy)

    async def scenario():
        async with open_inmemory_session(lowlevel_server(app)) as session:
            try:
                await session.read_resource(artifacts.URI_PREFIX + "0" * 32)
            except Exception as exc:  # noqa: BLE001 - the SDK's error type differs by generation
                return str(exc)
            return "no error"

    message = asyncio.run(scenario())
    assert "Call the tool again" in message


# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------


def test_the_server_says_when_to_reach_for_its_tools():
    from fluid_build._mcp_compat import lowlevel_server
    from fluid_build.cli.mcp.server import _get_mcp_app

    options = lowlevel_server(_get_mcp_app()).create_initialization_options()
    assert options.instructions == SERVER_INSTRUCTIONS
    # Claude Code cut instructions at 2,048 characters before v2.1.296.
    assert len(SERVER_INSTRUCTIONS) <= 2048
    assert SERVER_INSTRUCTIONS.startswith("FLUID data products")


# ---------------------------------------------------------------------------
# Unknown tool names
# ---------------------------------------------------------------------------


def test_a_renamed_tool_in_the_deny_list_is_refused_with_its_new_name():
    policy = _build_policy_from_args(SimpleNamespace(deny_tools="list_source_tables,nope"))
    refusal = tool_name_refusal(policy)
    assert "--deny-tools names no such tool: list_source_tables, nope." in refusal
    assert "  list_source_tables is now source_catalog with kind=tables." in refusal
    assert "  --deny-tools takes source_catalog whole, with every kind it reads." in refusal
    assert "nope is" not in refusal
    assert "  Tools: " + ", ".join(sorted(TOOL_CAPABILITIES)) + "." in refusal


def test_a_removed_tool_in_the_allow_list_is_refused():
    policy = _build_policy_from_args(SimpleNamespace(allow_tools="forge_run"))
    refusal = tool_name_refusal(policy)
    assert "--allow-tools names no such tool: forge_run." in refusal
    assert "  forge_run was removed; no tool replaces it." in refusal
    assert "takes source_catalog whole" not in refusal


def test_known_tool_names_pass():
    policy = _build_policy_from_args(
        SimpleNamespace(allow_tools="source_catalog,validate_contract", deny_tools="update_entity")
    )
    assert tool_name_refusal(policy) is None


def test_fluid_mcp_serve_stops_on_an_unknown_tool_name(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "mcp",
            "serve",
            "--deny-tools",
            "list_source_tables",
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2
    assert "fluid mcp serve: --deny-tools names no such tool: list_source_tables." in proc.stderr
    assert "list_source_tables is now source_catalog with kind=tables." in proc.stderr
    assert proc.stdout == ""


# ---------------------------------------------------------------------------
# Which transport names a file (the wiring in cli.py)
# ---------------------------------------------------------------------------


def _server_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
    env.update({artifacts.INLINE_TOKEN_LIMIT_ENV: "20", **extra})
    return env


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stderr.read()!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError("server did not listen in time")


@pytest.mark.slow
def test_over_stdio_an_offloaded_result_also_names_a_file(tmp_path):
    import time

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fluid_build.cli", "mcp", "serve", "--read-only"],
        env=_server_env(),
        cwd=str(tmp_path),
    )

    async def scenario():
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("source_catalog", {"kind": "adapters"})
                envelope = json.loads(result.content[0].text)
                text = Path(envelope["path"]).read_text(encoding="utf-8")
                read = await session.read_resource(envelope["resource"])
                return envelope, text, read.contents[0].text

    envelope, file_text, resource_text = asyncio.run(scenario())
    assert envelope["offloaded"] is True
    assert file_text == resource_text
    directory = Path(envelope["path"]).parent
    deadline = time.monotonic() + 15
    while directory.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not directory.exists(), "a server that exits normally removes its directory"


@pytest.mark.slow
def test_over_http_an_offloaded_result_names_no_file(tmp_path):
    import secrets
    import signal

    from fluid_build._mcp_compat import open_streamable_http_session

    token, port = secrets.token_hex(32), _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "fluid_build.cli", "mcp", "serve", "--transport", "http"]
        + ["--port", str(port)],
        cwd=tmp_path,
        env=_server_env(FLUID_MCP_AUTH_TOKEN=token),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, proc)

        async def scenario():
            async with open_streamable_http_session(
                f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {token}"}
            ) as session:
                result = await session.call_tool("source_catalog", {"kind": "adapters"})
                return json.loads(result.content[0].text)

        envelope = asyncio.run(scenario())
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert envelope["offloaded"] is True
    assert envelope["resource"].startswith(artifacts.URI_PREFIX)
    assert "path" not in envelope


# ---------------------------------------------------------------------------
# An adapter's refusal reaches the client in the adapter's words
# ---------------------------------------------------------------------------


def _refusing_builder(monkeypatch, tmp_path, *, at_build=None, at_read=None):
    """Stub the builder so the adapter refuses while it is built or while it reads."""

    class _Refusing(_StubAdapter):
        def list_tables(self, scope):
            raise at_read

    def _build(arguments, **_kwargs):
        if at_build is not None:
            raise at_build
        return _Refusing()

    monkeypatch.setattr("fluid_build.cli.mcp._build_source_adapter", _build)
    monkeypatch.setattr(
        "fluid_build.cli.mcp.write_audit_event", lambda *a, **kw: tmp_path / "audit.json"
    )


def test_an_adapters_own_refusal_is_passed_on_without_the_sdks_text(monkeypatch, tmp_path):
    from fluid_build.copilot.catalog.base import CatalogConfigError

    _refusing_builder(
        monkeypatch,
        tmp_path,
        at_read=CatalogConfigError(
            "Snowflake CatalogScope requires 'database'.",
            suggestions=["Pass scope.database='YOUR_DB'."],
            original_error=RuntimeError("driver said: token=abc123"),
        ),
    )
    with pytest.raises(McpToolUsageError) as exc:
        _call(kind="tables", scope={}, **_SNOWFLAKE)
    message = str(exc.value)
    assert message == (
        "Snowflake CatalogScope requires 'database'. Next: Pass scope.database='YOUR_DB'."
    )
    assert "abc123" not in message


def test_an_unknown_credential_id_is_passed_on(monkeypatch, tmp_path):
    from fluid_build.copilot.catalog.credentials import CredentialNotFoundError

    _refusing_builder(
        monkeypatch,
        tmp_path,
        at_build=CredentialNotFoundError(
            "No credentials resolved for source 'nope'.",
            suggestions=["Run `fluid ai setup --source snowflake --name nope`."],
        ),
    )
    with pytest.raises(McpToolUsageError, match="No credentials resolved for source 'nope'"):
        _call(kind="tables", **_SNOWFLAKE, **_SCOPE)


@pytest.mark.parametrize("phase", ["build", "read-connection", "read-permission"])
def test_other_catalog_failures_stay_server_side(monkeypatch, tmp_path, phase):
    """A credential validation error can quote input values, and a connection
    error or a permission denial carries the SDK's or the catalog's own text:
    none is turned into a client message."""
    from fluid_build.copilot.catalog.base import (
        CatalogConfigError,
        CatalogConnectionError,
        CatalogPermissionError,
    )

    if phase == "build":
        error = CatalogConfigError("Stored credentials for source 'x' failed validation: ...")
        _refusing_builder(monkeypatch, tmp_path, at_build=error)
    elif phase == "read-connection":
        error = CatalogConnectionError("Snowflake connection failed: driver text")
        _refusing_builder(monkeypatch, tmp_path, at_read=error)
    else:
        error = CatalogPermissionError("Catalog denied a metadata read on X: arn:aws:iam::1:role/r")
        _refusing_builder(monkeypatch, tmp_path, at_read=error)
    with pytest.raises(type(error)) as exc:
        _call(kind="tables", **_SNOWFLAKE, **_SCOPE)
    assert not isinstance(exc.value, McpToolUsageError)


# ---------------------------------------------------------------------------
# Odds and ends the review found
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments, hint",
    [
        ({"kind": "tables"}, "For less, name the tables you need in scope.tables."),
        (
            {"kind": "tables", "response_format": "detailed"},
            "For less, name the tables you need in scope.tables or ask for "
            "response_format=concise.",
        ),
        (
            {"kind": "glossary", "response_format": "detailed"},
            "For less, ask for response_format=concise.",
        ),
        ({"kind": "lineage"}, ""),
        ({"kind": "tables", "scope": {"tables": ["ORDERS"]}}, ""),
    ],
)
def test_the_offload_hint_fits_the_call(arguments, hint):
    from fluid_build.cli.mcp.server import _offload_hint

    assert _offload_hint("source_catalog", arguments) == hint
    assert _offload_hint("read_logical_model", arguments) == ""


def test_the_source_enum_and_the_dispatch_table_name_the_same_catalogs():
    from fluid_build.cli.mcp.dispatch import _SOURCE_ADAPTERS
    from fluid_build.cli.mcp.models import _CATALOG_SOURCE_LIST

    assert set(_CATALOG_SOURCE_LIST) == set(_SOURCE_ADAPTERS)


def test_over_http_a_large_result_is_serialized_on_the_worker_thread(monkeypatch):
    """Offloading serializes the whole result; over HTTP it runs where the
    tool ran, so the event loop keeps serving other clients."""
    import threading

    from fluid_build.cli.mcp import dispatch as dispatch_module
    from fluid_build.cli.mcp import policy as policy_module
    from fluid_build.cli.mcp import server as server_module

    seen = {}

    def _record(result, *, tool, hint):
        seen["thread"] = threading.current_thread().name
        return result

    monkeypatch.setattr(artifacts, "offload_if_large", _record)
    monkeypatch.setattr(dispatch_module, "_call_tool", lambda *a, **kw: {"kind": "adapters"})
    monkeypatch.setattr(
        policy_module, "_current_policy", _build_policy_from_args(SimpleNamespace())
    )
    monkeypatch.setattr(server_module, "_TOOLS_OFF_LOOP", True)
    result = asyncio.run(server_module._dispatch_sync_tool("source_catalog", {"kind": "adapters"}))
    assert result == {"kind": "adapters"}
    assert seen["thread"].startswith("fluid-mcp-tool")
