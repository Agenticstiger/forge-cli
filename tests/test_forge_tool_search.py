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

"""Pins for the opt-in tool-search / deferred-loading pattern.

Mirrors OpenAI's Agents-SDK ToolSearchTool + defer_loading: a large tool set is
advertised as a small CORE set (full schemas) plus name+namespace STUBS for the
rest, with one ``search_tools`` meta-tool the model calls to load a deferred
tool's full description + parameter schema on demand.

Covers:
  * the FLUID_FORGE_TOOL_SEARCH gate (transform is a pure no-op when unset);
  * apply_tool_search — core tools keep their full schema, deferred tools become
    lightweight stubs, and search_tools is injected;
  * the stub is genuinely smaller (its parameter schema is dropped);
  * search_tools dispatch — free-text query + namespace filter return the FULL
    (real-schema) defs;
  * the forge_copilot_tools integration (get_tool_definitions transform +
    dispatch_tool_call routing), incl. that a deferred tool still dispatches
    normally even though it was advertised as a stub.
"""

from __future__ import annotations

import json

from fluid_build.cli import forge_copilot_tools, forge_tool_search
from fluid_build.cli.forge_tool_search import (
    SEARCH_TOOL_NAME,
    apply_tool_search,
    dispatch_search_tool,
    is_enabled,
    is_search_tool,
)

_ON = {"FLUID_FORGE_TOOL_SEARCH": "1"}


def _fake_defs():
    """A representative slice: two core tools + two deferred tools."""
    return [
        {
            "name": "discover_workspace",
            "description": "Scan the workspace.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "validate_contract",
            "description": "Validate a contract.",
            "input_schema": {
                "type": "object",
                "properties": {"contract": {"type": "object"}},
                "required": ["contract"],
            },
        },
        {
            "name": "estimate_cost",
            "description": "Estimate the USD cost of a planned LLM call.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "x" * 200},
                    "model": {"type": "string", "description": "y" * 200},
                    "input_tokens": {"type": "integer"},
                    "output_tokens": {"type": "integer"},
                },
                "required": ["provider", "model"],
            },
        },
        {
            "name": "check_pii_classification",
            "description": "Look up a column's classification.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "z" * 200},
                    "column_name": {"type": "string"},
                },
                "required": ["product_id", "column_name"],
            },
        },
    ]


# ── gate ─────────────────────────────────────────────────────────────────────
class TestGate:
    def test_is_enabled_reads_env_flag(self):
        assert is_enabled({"FLUID_FORGE_TOOL_SEARCH": "1"}) is True
        assert is_enabled({"FLUID_FORGE_TOOL_SEARCH": "true"}) is True
        assert is_enabled({"FLUID_FORGE_TOOL_SEARCH": "0"}) is False
        assert is_enabled({}) is False

    def test_is_search_tool_requires_name_and_enabled(self):
        assert is_search_tool(SEARCH_TOOL_NAME, _ON) is True
        assert is_search_tool(SEARCH_TOOL_NAME, {}) is False
        assert is_search_tool("discover_workspace", _ON) is False

    def test_apply_is_noop_when_disabled(self):
        defs = _fake_defs()
        assert apply_tool_search(defs, env={}) == defs


# ── apply_tool_search ────────────────────────────────────────────────────────
class TestApply:
    def test_core_full_deferred_stubbed_search_injected(self):
        out = apply_tool_search(_fake_defs(), env=_ON)
        by_name = {d["name"]: d for d in out}

        # search_tools is injected.
        assert SEARCH_TOOL_NAME in by_name
        assert by_name[SEARCH_TOOL_NAME]["input_schema"]["properties"]  # has query/namespace

        # Core tools keep their full schema.
        assert by_name["discover_workspace"]["input_schema"] == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        assert "contract" in by_name["validate_contract"]["input_schema"]["properties"]

        # Deferred tools are present but stubbed: their parameter schema is
        # dropped (no per-field properties leak into the up-front listing).
        stub = by_name["estimate_cost"]
        assert stub["input_schema"].get("properties") in (None, {})
        assert "provider" not in json.dumps(stub["input_schema"])
        # The stub still names its namespace so the model knows what it is.
        assert "cost" in stub["description"].lower()

    def test_stub_is_smaller_than_full(self):
        full = {d["name"]: d for d in _fake_defs()}["check_pii_classification"]
        out = {d["name"]: d for d in apply_tool_search(_fake_defs(), env=_ON)}
        stub = out["check_pii_classification"]
        assert len(json.dumps(stub)) < len(json.dumps(full))

    def test_every_tool_still_advertised(self):
        # Deferred tools must remain callable, so they must still appear in the
        # advertised list (as stubs) — nothing is silently hidden.
        out_names = {d["name"] for d in apply_tool_search(_fake_defs(), env=_ON)}
        for name in (
            "discover_workspace",
            "validate_contract",
            "estimate_cost",
            "check_pii_classification",
            SEARCH_TOOL_NAME,
        ):
            assert name in out_names

    def test_core_override_via_env(self):
        env = {**_ON, "FLUID_FORGE_TOOL_SEARCH_CORE": "estimate_cost"}
        out = {d["name"]: d for d in apply_tool_search(_fake_defs(), env=env)}
        # estimate_cost is now core → full schema; discover_workspace is deferred.
        assert "provider" in out["estimate_cost"]["input_schema"]["properties"]
        assert out["discover_workspace"]["input_schema"].get("properties") in (None, {})


# ── search_tools dispatch ────────────────────────────────────────────────────
class TestSearchDispatch:
    def test_query_returns_full_defs(self):
        res = dispatch_search_tool({"query": "cost"}, all_definitions=_fake_defs(), env=_ON)
        names = {t["name"] for t in res["tools"]}
        assert "estimate_cost" in names
        # Full parameter schema is restored on search.
        est = next(t for t in res["tools"] if t["name"] == "estimate_cost")
        assert "provider" in est["input_schema"]["properties"]

    def test_namespace_filter(self):
        res = dispatch_search_tool({"namespace": "cost"}, all_definitions=_fake_defs(), env=_ON)
        assert {t["name"] for t in res["tools"]} == {"estimate_cost"}

    def test_empty_query_lists_deferred_namespaces(self):
        res = dispatch_search_tool({}, all_definitions=_fake_defs(), env=_ON)
        # With no query we surface the catalogue so the model can pick.
        assert "namespaces" in res

    def test_core_tools_not_returned_by_search(self):
        # Core tools already carry full schemas up front — searching for them
        # is redundant, so search returns only deferred matches.
        res = dispatch_search_tool({"query": "workspace"}, all_definitions=_fake_defs(), env=_ON)
        assert "discover_workspace" not in {t["name"] for t in res["tools"]}


# ── forge_copilot_tools integration ──────────────────────────────────────────
class TestIntegration:
    def test_disabled_leaves_definitions_unchanged(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_TOOL_SEARCH", raising=False)
        by_name = {d["name"]: d for d in forge_copilot_tools.get_tool_definitions()}
        assert SEARCH_TOOL_NAME not in by_name
        # estimate_cost carries its full schema.
        assert "provider" in by_name["estimate_cost"]["input_schema"]["properties"]

    def test_enabled_transforms_listing(self, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_TOOL_SEARCH", "1")
        by_name = {d["name"]: d for d in forge_copilot_tools.get_tool_definitions()}
        assert SEARCH_TOOL_NAME in by_name
        # A deferred tool is stubbed up front.
        assert by_name["estimate_cost"]["input_schema"].get("properties") in (None, {})
        # A core tool keeps its schema.
        assert "discover_workspace" in by_name

    def test_search_tools_routes_and_deferred_tool_still_dispatches(self, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_TOOL_SEARCH", "1")
        # search_tools routes and loads estimate_cost's full schema.
        res = forge_copilot_tools.dispatch_tool_call("search_tools", {"query": "cost"})
        assert "estimate_cost" in {t["name"] for t in res["tools"]}
        # The deferred tool itself still dispatches normally (advertised as a
        # stub, but dispatch resolves by name + validates via the real schema).
        out = forge_copilot_tools.dispatch_tool_call(
            "estimate_cost",
            {"provider": "openai", "model": "gpt-4.1-mini", "input_tokens": 10, "output_tokens": 5},
        )
        assert out["provider"] == "openai"
        assert "usd" in out
