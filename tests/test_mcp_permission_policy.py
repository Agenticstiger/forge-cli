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

"""Coverage for ``fluid_build.cli.mcp`` permission model.

The MCP stdio server exposes seven tools. Before v1.5 the only access
control was a single ``--read-only`` flag that blocked every mutating
tool uniformly. These tests pin the richer three-layer model that
hardens the server against prompt-injection from upstream agents:

1. **Tool allow/deny list** — only explicitly permitted tools are
   callable; denied tools are also hidden from ``tools/list``.
2. **Read-only gate** — preserves the legacy ``--read-only`` semantics.
3. **Sandboxes** — ``--writable-paths`` constrains filesystem writes;
   ``--writable-namespaces`` constrains store writes.

The ``_build_policy_from_args`` tests verify the argparse wiring so the
CLI surface matches the policy dataclass end to end.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from fluid_build.cli.mcp import (
    DEFAULT_WRITABLE_NAMESPACES,
    TOOL_CAPABILITIES,
    McpPolicy,
    _build_policy_from_args,
    _filter_visible_tools,
    _path_is_writable,
    _tool_definitions,
    check_tool_permission,
)

# ----------------------------------------------------------------------
# Tool capability registry
# ----------------------------------------------------------------------


class TestToolCapabilityRegistry:
    def test_v1_logical_pipeline_tools_registered(self):
        """Pin the original 7 logical-pipeline tools as a *subset*.

        The registry has grown beyond the original 7 (V1.5 added 6
        source-catalog tools: list_source_adapters, list_source_tables,
        inspect_source_table, list_source_lineage, list_source_glossary,
        forge_from_source). The original set must continue to exist
        — any rename / removal would break the LLM-agentic
        contract Claude Code / Cursor depend on.
        """
        original_seven = {
            "read_logical_model",
            "update_entity",
            "add_relationship",
            "regenerate_physical",
            "validate_contract",
            "diff_models",
            "search_semantic_memory",
        }
        assert original_seven.issubset(set(TOOL_CAPABILITIES.keys()))

    def test_v15_source_catalog_tools_registered(self):
        """V1.5 adds six source-catalog tools, all named ``*_source_*``
        to disambiguate from the existing publish-catalog role."""
        v15_six = {
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
            "forge_from_source",
        }
        assert v15_six.issubset(set(TOOL_CAPABILITIES.keys()))

    def test_read_only_tools_are_nonmutating(self):
        """Every read tool must declare no mutation and no namespace writes
        — otherwise the read-only gate would unexpectedly block them."""
        for name in (
            "read_logical_model",
            "validate_contract",
            "diff_models",
            "search_semantic_memory",
        ):
            cap = TOOL_CAPABILITIES[name]
            assert cap.mutates_files is False
            assert cap.writes_namespaces == ()

    def test_mutating_tools_always_write_history_and_audit(self):
        """Audit + history snapshotting is non-negotiable for mutations:
        an operator who blocks those namespaces must also block the tool,
        not silently run it without a forensic trail."""
        for name in ("update_entity", "add_relationship", "regenerate_physical"):
            cap = TOOL_CAPABILITIES[name]
            assert cap.mutates_files is True
            assert "history" in cap.writes_namespaces
            assert "audit" in cap.writes_namespaces

    def test_regenerate_physical_declares_both_path_args(self):
        """regenerate_physical accepts both ``path`` (logical sidecar in)
        and ``contract_path`` (Fluid contract out) — both must be
        declared so the sandbox check catches both."""
        cap = TOOL_CAPABILITIES["regenerate_physical"]
        assert set(cap.file_path_args) == {"path", "contract_path"}

    def test_search_semantic_memory_declares_read_namespace(self):
        cap = TOOL_CAPABILITIES["search_semantic_memory"]
        assert cap.reads_namespaces == ("memory/semantic",)

    def test_tool_definitions_derived_from_registry(self):
        """tools/list output must be backed by TOOL_CAPABILITIES so there
        is one source of truth — adding a tool shouldn't require editing
        two places."""
        tools = _tool_definitions()
        assert [t["name"] for t in tools] == list(TOOL_CAPABILITIES.keys())
        for tool in tools:
            cap = TOOL_CAPABILITIES[tool["name"]]
            assert tool["description"] == cap.description


# ----------------------------------------------------------------------
# _path_is_writable helper
# ----------------------------------------------------------------------


class TestPathIsWritable:
    def test_direct_child_allowed(self, tmp_path: Path):
        assert _path_is_writable(tmp_path / "x.json", (tmp_path,)) is True

    def test_nested_child_allowed(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "c.json"
        assert _path_is_writable(nested, (tmp_path,)) is True

    def test_sibling_denied(self, tmp_path: Path):
        other = tmp_path.parent / "elsewhere" / "x.json"
        assert _path_is_writable(other, (tmp_path,)) is False

    def test_empty_roots_always_denied(self, tmp_path: Path):
        assert _path_is_writable(tmp_path / "x.json", ()) is False

    def test_multiple_roots_any_match_allows(self, tmp_path: Path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        target = root_b / "file.json"
        assert _path_is_writable(target, (root_a, root_b)) is True


# ----------------------------------------------------------------------
# Read-only gate
# ----------------------------------------------------------------------


class TestReadOnlyGate:
    def test_read_only_allows_read_tools(self, tmp_path: Path):
        policy = McpPolicy(
            read_only=True,
            readable_paths=(tmp_path,),
            writable_paths=(tmp_path,),
        )
        # No raise
        check_tool_permission(
            "read_logical_model", {"path": str(tmp_path / "x.json")}, policy=policy
        )
        check_tool_permission(
            "validate_contract",
            {"logical_path": str(tmp_path / "logical.json")},
            policy=policy,
        )
        check_tool_permission(
            "diff_models",
            {"old": str(tmp_path / "a.json"), "new": str(tmp_path / "b.json")},
            policy=policy,
        )

    def test_read_only_denies_update_entity(self, tmp_path: Path):
        policy = McpPolicy(read_only=True, writable_paths=(tmp_path,))
        with pytest.raises(PermissionError, match="read-only"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )

    def test_read_only_denies_regenerate(self, tmp_path: Path):
        policy = McpPolicy(read_only=True, writable_paths=(tmp_path,))
        with pytest.raises(PermissionError, match="read-only"):
            check_tool_permission(
                "regenerate_physical",
                {"path": str(tmp_path / "m.json"), "contract_path": str(tmp_path / "c.yaml")},
                policy=policy,
            )


# ----------------------------------------------------------------------
# Tool allowlist / denylist
# ----------------------------------------------------------------------


class TestToolAllowlist:
    def test_allowlist_admits_listed_tool(self, tmp_path: Path):
        policy = McpPolicy(
            allowed_tools=("read_logical_model",),
            readable_paths=(tmp_path,),
            writable_paths=(tmp_path,),
        )
        check_tool_permission(
            "read_logical_model",
            {"path": str(tmp_path / "x.json")},
            policy=policy,
        )

    def test_allowlist_rejects_omitted_tool(self, tmp_path: Path):
        policy = McpPolicy(
            allowed_tools=("read_logical_model",),
            writable_paths=(tmp_path,),
        )
        with pytest.raises(PermissionError, match="not in allowlist"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )

    def test_allowlist_none_permits_all(self, tmp_path: Path):
        policy = McpPolicy(
            allowed_tools=None,
            readable_paths=(tmp_path,),
            writable_paths=(tmp_path,),
        )
        check_tool_permission(
            "read_logical_model",
            {"path": str(tmp_path / "x.json")},
            policy=policy,
        )
        check_tool_permission(
            "update_entity",
            {"path": str(tmp_path / "x.json"), "entity": "a"},
            policy=policy,
        )

    def test_empty_allowlist_rejects_everything(self, tmp_path: Path):
        """An explicit empty tuple differs from None: it means 'no tools
        allowed'. Useful for an inspection-only deployment."""
        policy = McpPolicy(allowed_tools=(), writable_paths=(tmp_path,))
        with pytest.raises(PermissionError, match="not in allowlist"):
            check_tool_permission(
                "read_logical_model",
                {"path": str(tmp_path / "x.json")},
                policy=policy,
            )

    def test_denylist_wins_over_allowlist(self, tmp_path: Path):
        """Belt-and-braces: a tool appearing in both lists is denied.
        This prevents subtle config errors from becoming security holes."""
        policy = McpPolicy(
            allowed_tools=("update_entity",),
            denied_tools=("update_entity",),
            writable_paths=(tmp_path,),
        )
        with pytest.raises(PermissionError, match="not in allowlist"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )


# ----------------------------------------------------------------------
# Filesystem sandbox
# ----------------------------------------------------------------------


class TestFileSandbox:
    def test_write_inside_sandbox_allowed(self, tmp_path: Path):
        policy = McpPolicy(writable_paths=(tmp_path,))
        check_tool_permission(
            "update_entity",
            {"path": str(tmp_path / "x.json"), "entity": "a"},
            policy=policy,
        )

    def test_write_outside_sandbox_denied(self, tmp_path: Path):
        sandbox = tmp_path / "inside"
        sandbox.mkdir()
        policy = McpPolicy(writable_paths=(sandbox,))
        outside = tmp_path / "outside" / "x.json"
        with pytest.raises(PermissionError, match="outside --writable-paths"):
            check_tool_permission(
                "update_entity",
                {"path": str(outside), "entity": "a"},
                policy=policy,
            )

    def test_parent_traversal_resolves_and_is_caught(self, tmp_path: Path):
        """A client trying ``sandbox/../outside.json`` must have that
        path resolve() before the sandbox check, so the traversal is
        caught rather than silently allowed."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        policy = McpPolicy(writable_paths=(sandbox,))
        traversal = sandbox / ".." / "outside.json"
        with pytest.raises(PermissionError):
            check_tool_permission(
                "update_entity",
                {"path": str(traversal), "entity": "a"},
                policy=policy,
            )

    def test_no_writable_paths_blocks_all_writes(self, tmp_path: Path):
        """writable_paths=() must deny every write attempt — even when
        the tool itself is allowlisted."""
        policy = McpPolicy(writable_paths=())
        with pytest.raises(PermissionError, match="outside --writable-paths"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )

    def test_regenerate_physical_checks_both_args(self, tmp_path: Path):
        """regenerate_physical takes ``path`` + ``contract_path``. If
        only one of the two is outside the sandbox, the tool is denied."""
        sandbox = tmp_path / "ok"
        sandbox.mkdir()
        policy = McpPolicy(writable_paths=(sandbox,))
        with pytest.raises(PermissionError, match="outside --writable-paths"):
            check_tool_permission(
                "regenerate_physical",
                {
                    "path": str(sandbox / "m.json"),
                    "contract_path": "/tmp/elsewhere/c.yaml",
                },
                policy=policy,
            )

    def test_missing_path_arg_is_skipped(self, tmp_path: Path):
        """A tool call lacking the declared file_path argument must
        not crash the permission layer — the tool body will raise for
        the missing-required-arg, which is its concern, not ours."""
        policy = McpPolicy(writable_paths=(tmp_path,))
        # No ``path`` key. Should not raise at the permission layer.
        check_tool_permission(
            "update_entity",
            {"entity": "a"},
            policy=policy,
        )

    def test_read_tool_inside_sandbox_allowed(self, tmp_path: Path):
        policy = McpPolicy(readable_paths=(tmp_path,), writable_paths=(tmp_path,))
        check_tool_permission(
            "read_logical_model",
            {"path": str(tmp_path / "x.json")},
            policy=policy,
        )

    def test_read_outside_sandbox_denied(self, tmp_path: Path):
        sandbox = tmp_path / "inside"
        sandbox.mkdir()
        outside = tmp_path / "outside" / "x.json"
        policy = McpPolicy(readable_paths=(sandbox,), writable_paths=(sandbox,))
        with pytest.raises(PermissionError, match="outside --readable-paths"):
            check_tool_permission(
                "read_logical_model",
                {"path": str(outside)},
                policy=policy,
            )

    def test_read_parent_traversal_resolves_and_is_caught(self, tmp_path: Path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        traversal = sandbox / ".." / "outside.json"
        policy = McpPolicy(readable_paths=(sandbox,), writable_paths=(sandbox,))
        with pytest.raises(PermissionError, match="outside --readable-paths"):
            check_tool_permission(
                "read_logical_model",
                {"path": str(traversal)},
                policy=policy,
            )

    def test_validate_contract_checks_both_read_paths(self, tmp_path: Path):
        sandbox = tmp_path / "ok"
        sandbox.mkdir()
        policy = McpPolicy(readable_paths=(sandbox,), writable_paths=(sandbox,))
        with pytest.raises(PermissionError, match="outside --readable-paths"):
            check_tool_permission(
                "validate_contract",
                {
                    "logical_path": str(sandbox / "logical.json"),
                    "contract_path": str(tmp_path / "outside" / "contract.yaml"),
                },
                policy=policy,
            )

    def test_no_readable_paths_blocks_path_reads(self, tmp_path: Path):
        policy = McpPolicy(readable_paths=(), writable_paths=(tmp_path,))
        with pytest.raises(PermissionError, match="outside --readable-paths"):
            check_tool_permission(
                "read_logical_model",
                {"path": str(tmp_path / "x.json")},
                policy=policy,
            )

    def test_read_tool_missing_path_arg_is_skipped(self, tmp_path: Path):
        policy = McpPolicy(readable_paths=(), writable_paths=(tmp_path,))
        # The tool body will raise for missing required input; permission
        # should only gate populated path arguments.
        check_tool_permission(
            "read_logical_model",
            {},
            policy=policy,
        )


# ----------------------------------------------------------------------
# Namespace allowlist
# ----------------------------------------------------------------------


class TestNamespaceAllowlist:
    def test_default_namespaces_include_history_and_audit(self):
        assert set(DEFAULT_WRITABLE_NAMESPACES) >= {"history", "audit"}

    def test_default_namespaces_permit_mutating_tools(self, tmp_path: Path):
        """With no explicit --writable-namespaces, mutating tools must
        still be runnable. The defaults cover exactly what they need."""
        policy = McpPolicy(writable_paths=(tmp_path,))
        check_tool_permission(
            "update_entity",
            {"path": str(tmp_path / "x.json"), "entity": "a"},
            policy=policy,
        )

    def test_missing_audit_blocks_write_tool(self, tmp_path: Path):
        policy = McpPolicy(
            writable_paths=(tmp_path,),
            writable_namespaces=("history",),  # audit missing
        )
        with pytest.raises(PermissionError, match="writes namespace 'audit'"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )

    def test_missing_history_blocks_write_tool(self, tmp_path: Path):
        policy = McpPolicy(
            writable_paths=(tmp_path,),
            writable_namespaces=("audit",),  # history missing
        )
        with pytest.raises(PermissionError, match="writes namespace 'history'"):
            check_tool_permission(
                "update_entity",
                {"path": str(tmp_path / "x.json"), "entity": "a"},
                policy=policy,
            )


# ----------------------------------------------------------------------
# Unknown tool
# ----------------------------------------------------------------------


class TestUnknownTool:
    def test_unknown_tool_raises_runtime_error(self):
        """Unknown tools are a programming error, not an auth failure.
        Distinguishing ``RuntimeError`` from ``PermissionError`` makes
        the server's error payload accurate (-32000 vs -32001)."""
        policy = McpPolicy(writable_paths=(Path("/tmp"),))
        with pytest.raises(RuntimeError, match="Unknown tool"):
            check_tool_permission("nonexistent", {}, policy=policy)


# ----------------------------------------------------------------------
# tools/list visibility filter
# ----------------------------------------------------------------------


class TestVisibleTools:
    def test_read_only_hides_mutating_tools(self):
        policy = McpPolicy(read_only=True)
        visible = _filter_visible_tools(_tool_definitions(), policy)
        names = {t["name"] for t in visible}
        assert "read_logical_model" in names
        assert "validate_contract" in names
        assert "diff_models" in names
        assert "search_semantic_memory" in names
        # Mutating tools gone:
        assert "update_entity" not in names
        assert "add_relationship" not in names
        assert "regenerate_physical" not in names

    def test_allowlist_restricts_visibility(self):
        policy = McpPolicy(allowed_tools=("read_logical_model",))
        visible = _filter_visible_tools(_tool_definitions(), policy)
        assert [t["name"] for t in visible] == ["read_logical_model"]

    def test_denylist_hides_single_tool(self):
        policy = McpPolicy(denied_tools=("update_entity",))
        visible = _filter_visible_tools(_tool_definitions(), policy)
        names = {t["name"] for t in visible}
        assert "update_entity" not in names
        # Everything else still present.
        assert "add_relationship" in names
        assert "read_logical_model" in names

    def test_default_policy_shows_all_tools(self):
        """Bare ``McpPolicy()`` with no restrictions must still expose the
        full registry — otherwise upgraders would see tools disappear."""
        visible = _filter_visible_tools(
            _tool_definitions(), McpPolicy(writable_paths=(Path.cwd(),))
        )
        assert len(visible) == len(TOOL_CAPABILITIES)


# ----------------------------------------------------------------------
# CLI → policy translation
# ----------------------------------------------------------------------


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class TestPolicyFromArgs:
    def test_defaults_map_cleanly(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = _ns(
            read_only=False,
            allow_tools=None,
            deny_tools=None,
            readable_paths=None,
            writable_paths=None,
            writable_namespaces=None,
        )
        policy = _build_policy_from_args(args)
        assert policy.read_only is False
        assert policy.allowed_tools is None
        assert policy.denied_tools == ()
        # Default writable_paths = cwd, resolved.
        assert policy.readable_paths == (tmp_path.resolve(),)
        assert policy.writable_paths == (tmp_path.resolve(),)
        assert policy.writable_namespaces == DEFAULT_WRITABLE_NAMESPACES

    def test_read_only_flag_propagates(self):
        args = _ns(
            read_only=True,
            allow_tools=None,
            deny_tools=None,
            readable_paths=None,
            writable_paths=None,
            writable_namespaces=None,
        )
        assert _build_policy_from_args(args).read_only is True

    def test_csv_lists_parsed(self, tmp_path: Path):
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_a.mkdir()
        path_b.mkdir()
        args = _ns(
            read_only=False,
            allow_tools="read_logical_model,diff_models",
            deny_tools="update_entity",
            readable_paths=f"{path_a},{path_b}",
            writable_paths=f"{path_a},{path_b}",
            writable_namespaces="history,audit,memory/semantic",
        )
        policy = _build_policy_from_args(args)
        assert policy.allowed_tools == ("read_logical_model", "diff_models")
        assert policy.denied_tools == ("update_entity",)
        assert policy.readable_paths == (path_a.resolve(), path_b.resolve())
        assert policy.writable_paths == (path_a.resolve(), path_b.resolve())
        assert policy.writable_namespaces == ("history", "audit", "memory/semantic")

    def test_whitespace_and_empty_entries_stripped(self, tmp_path: Path):
        monkeypatch_cwd = tmp_path.resolve()
        args = _ns(
            read_only=False,
            allow_tools="  read_logical_model , , validate_contract ,",
            deny_tools=None,
            readable_paths=None,
            writable_paths=None,
            writable_namespaces=None,
        )
        policy = _build_policy_from_args(args)
        assert policy.allowed_tools == ("read_logical_model", "validate_contract")
        # Sanity: the path tuples default to cwd.
        assert len(policy.readable_paths) == 1
        assert len(policy.writable_paths) == 1
