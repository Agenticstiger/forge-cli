# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pin the consumer MCP server's policy + tool-derivation rules."""

from __future__ import annotations

import pytest

from fluid_build.output_ports.mcp.policy import OutputPortPolicy
from fluid_build.output_ports.mcp.tools import (
    OUTPUT_PORT_TOOL_CAPABILITIES,
    check_tool_permission,
    derive_advertised_tools,
)

from ._fixtures import make_expose

# ---------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------


def test_tool_capabilities_have_input_schemas():
    for name, cap in OUTPUT_PORT_TOOL_CAPABILITIES.items():
        assert cap.input_schema is not None, name
        assert cap.input_schema.get("type") == "object"
        assert "properties" in cap.input_schema


def test_query_sql_marked_as_sql_allowlist_required():
    assert OUTPUT_PORT_TOOL_CAPABILITIES["query_sql"].requires_sql_allowlist is True
    assert OUTPUT_PORT_TOOL_CAPABILITIES["query"].requires_sql_allowlist is False


# ---------------------------------------------------------------------
# Tool advertisement
# ---------------------------------------------------------------------


def test_query_sql_hidden_when_allow_sql_off():
    expose = make_expose(
        semantics={
            "measures": [{"name": "customer_count", "agg": "count", "expr": "customer_id"}],
        }
    )
    advertised = derive_advertised_tools(expose=expose, allow_free_form_sql=False)
    names = {tool["name"] for tool in advertised}
    assert "query_sql" not in names
    assert {"describe", "sample", "query"} <= names


def test_query_sql_visible_when_allow_sql_on():
    expose = make_expose()
    advertised = derive_advertised_tools(expose=expose, allow_free_form_sql=True)
    assert "query_sql" in {tool["name"] for tool in advertised}


def test_denied_tools_dropped_from_advertisement():
    expose = make_expose(
        semantics={
            "measures": [{"name": "customer_count", "agg": "count", "expr": "customer_id"}],
        }
    )
    advertised = derive_advertised_tools(
        expose=expose, allow_free_form_sql=True, extra_denied=("query_sql", "sample")
    )
    names = {tool["name"] for tool in advertised}
    assert "query_sql" not in names
    assert "sample" not in names
    assert "describe" in names
    assert "query" in names


def test_query_hidden_when_expose_lacks_semantics():
    """When the expose has no semantics block, the ``query`` tool is
    dropped from the advertisement entirely (not merely re-described
    as 'UNAVAILABLE'). LLM agents won't try to call a tool that
    isn't there, which avoids retry loops."""
    expose = make_expose()  # no semantics
    advertised = derive_advertised_tools(expose=expose, allow_free_form_sql=False)
    names = {tool["name"] for tool in advertised}
    assert "query" not in names
    assert {"describe", "sample"} <= names


def test_query_visible_when_expose_has_semantics():
    expose = make_expose(
        semantics={
            "measures": [{"name": "customer_count", "agg": "count", "expr": "customer_id"}],
        }
    )
    advertised = derive_advertised_tools(expose=expose, allow_free_form_sql=False)
    assert "query" in {tool["name"] for tool in advertised}


# ---------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------


def test_unknown_tool_raises_runtime_error():
    with pytest.raises(RuntimeError, match="Unknown tool"):
        check_tool_permission(
            "made_up", allowed_tools=None, denied_tools=(), allow_free_form_sql=True
        )


def test_denied_tool_rejected_even_when_in_allowlist():
    with pytest.raises(PermissionError, match="deny list"):
        check_tool_permission(
            "describe",
            allowed_tools=("describe", "sample"),
            denied_tools=("describe",),
            allow_free_form_sql=False,
        )


def test_unallowed_tool_rejected():
    with pytest.raises(PermissionError, match="not in allowlist"):
        check_tool_permission(
            "sample",
            allowed_tools=("describe",),
            denied_tools=(),
            allow_free_form_sql=False,
        )


def test_query_sql_requires_allow_flag():
    with pytest.raises(PermissionError, match="--allow-sql"):
        check_tool_permission(
            "query_sql",
            allowed_tools=None,
            denied_tools=(),
            allow_free_form_sql=False,
        )


# ---------------------------------------------------------------------
# OutputPortPolicy defaults
# ---------------------------------------------------------------------


def test_default_policy_is_conservative():
    policy = OutputPortPolicy()
    assert policy.read_only is True
    assert policy.allow_free_form_sql is False
    assert policy.max_sample_rows == 100
    assert policy.allowed_tools is None
    assert policy.denied_tools == ()


def test_policy_is_tool_allowed_logic():
    policy = OutputPortPolicy(allowed_tools=("describe", "sample"), denied_tools=("sample",))
    assert policy.is_tool_allowed("describe") is True
    assert policy.is_tool_allowed("sample") is False  # denied beats allowed
    assert policy.is_tool_allowed("query") is False  # not in allowlist
