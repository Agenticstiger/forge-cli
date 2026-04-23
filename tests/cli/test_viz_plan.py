# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the mermaid-uplifted ``fluid plan --html`` rendering.

The pre-uplift template was a bare ``<pre>`` of JSON. The new template
emits a mermaid.js ``graph TD`` block with:

- One node per action (label = op + id, two-line format)
- Colour class per action mode (amend / replace / skipped / unknown)
- Edges derived from ``depends_on`` / ``dependsOn`` arrays on each
  action; fallback to sequential chaining when no explicit deps exist
- Legend panel showing the four mode colours
- Raw-JSON drill-down below the graph for operators who need the
  actual values

Tests cover:

1. Empty plans render an "(no actions)" placeholder (not a crash).
2. Single-action plans render one node + no edges.
3. Multi-action plans with explicit depends_on render the right edges.
4. Plans without any depends_on fall back to sequential chaining.
5. Node labels quote-escape any double quotes in op/id (mermaid safety).
6. Action-mode classes map correctly (replace → ``:::replace``, etc.).
7. The HTML carries the ``securityLevel: 'strict'`` mermaid init so
   arbitrary action-id strings can't inject script.
8. ``render_plan_html`` writes to the output path and reports
   ``viz_plan_ok`` — the existing contract preserved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import viz_plan

# -----------------------------------------------------------------------------
# _mermaid_node_id + _mermaid_label — sanitization
# -----------------------------------------------------------------------------


class TestMermaidNodeId:
    def test_basic_id_sanitized(self):
        """Dots + dashes + slashes are replaced with underscores so the
        resulting string is a valid mermaid node id."""
        assert (
            viz_plan._mermaid_node_id("silver.telco.subscriber360_v1", 0)
            == "n0_silver_telco_subscriber360_v1"
        )

    def test_fallback_id_when_absent(self):
        """Missing id produces a predictable ``action_<idx>`` form."""
        assert viz_plan._mermaid_node_id("", 5) == "n5_action_5"

    def test_collision_safety_via_index(self):
        """Two actions sharing the same id must still produce distinct
        node ids — positional index is always prepended."""
        a = viz_plan._mermaid_node_id("dup", 3)
        b = viz_plan._mermaid_node_id("dup", 7)
        assert a != b
        assert a == "n3_dup"
        assert b == "n7_dup"

    def test_shell_meta_stripped(self):
        """Exotic characters collapse to underscores; no mermaid-parser
        confusion."""
        assert viz_plan._mermaid_node_id("weird$id;with meta", 1) == "n1_weird_id_with_meta"


class TestMermaidLabel:
    def test_op_and_id_rendered(self):
        action = {"op": "provisionDataset", "id": "a1"}
        label = viz_plan._mermaid_label(action, 0)
        assert "provisionDataset" in label
        assert "a1" in label
        assert label.startswith('"') and label.endswith('"')
        assert "<br/>" in label

    def test_falls_back_to_action_type(self):
        """When ``op`` is absent but ``action_type`` is present, use
        that (plans from the 0.7.1-era emitter carry action_type)."""
        action = {"action_type": "grantAccess", "id": "g1"}
        label = viz_plan._mermaid_label(action, 0)
        assert "grantAccess" in label

    def test_escapes_double_quotes_in_id(self):
        """Double-quotes in the action id must be escaped so the
        mermaid parser doesn't choke on the label. A malicious
        action id with a closing quote could otherwise break out of
        the label and inject."""
        action = {"op": "weird", "id": 'a"id'}
        label = viz_plan._mermaid_label(action, 0)
        # The escaped form replaces " with ' in our implementation.
        assert '"' not in label.replace('"', "", 2)  # only the outer quotes

    def test_escapes_double_quotes_in_op(self):
        action = {"op": 'sneaky"op', "id": "a1"}
        label = viz_plan._mermaid_label(action, 0)
        # Outer quotes are OK; no inner unescaped quotes.
        stripped = label[1:-1]  # remove outer quotes
        assert '"' not in stripped


# -----------------------------------------------------------------------------
# _mermaid_class_for — mode → CSS class
# -----------------------------------------------------------------------------


class TestMermaidClassFor:
    def test_amend_modes(self):
        for mode in ["amend", "amend-and-build", "create-only"]:
            assert viz_plan._mermaid_class_for({"mode": mode}) == "amend"

    def test_replace_mode(self):
        assert viz_plan._mermaid_class_for({"mode": "replace"}) == "replace"

    def test_replace_detected_from_op(self):
        """Some plans don't carry mode; detect ``replace`` from the
        op string as a fallback so destructive actions still stand
        out in the graph."""
        assert viz_plan._mermaid_class_for({"op": "replace_table"}) == "replace"

    def test_skipped_status(self):
        assert viz_plan._mermaid_class_for({"status": "skipped"}) == "skipped"

    def test_unknown_default(self):
        assert viz_plan._mermaid_class_for({}) == "unknown"


# -----------------------------------------------------------------------------
# _build_mermaid_graph
# -----------------------------------------------------------------------------


class TestBuildMermaidGraph:
    def test_empty_actions_emits_placeholder(self):
        """No actions must not crash; render a placeholder node so
        the resulting HTML still loads in a browser."""
        out = viz_plan._build_mermaid_graph([])
        assert "graph TD" in out
        assert "(no actions)" in out

    def test_single_action_one_node(self):
        actions = [{"op": "bundle", "id": "a1"}]
        out = viz_plan._build_mermaid_graph(actions)
        assert "graph TD" in out
        # Exactly one node declaration (not counting classDefs).
        node_lines = [
            ln for ln in out.splitlines() if ln.strip().startswith("n0_") and "-->" not in ln
        ]
        assert len(node_lines) == 1
        # No edges for a single node.
        assert "-->" not in out

    def test_explicit_depends_on_produces_edges(self):
        actions = [
            {"op": "bundle", "id": "a0"},
            {"op": "validate", "id": "a1", "depends_on": ["a0"]},
            {"op": "plan", "id": "a2", "depends_on": ["a1"]},
        ]
        out = viz_plan._build_mermaid_graph(actions)
        # Two edges expected: a0→a1 and a1→a2.
        edges = [ln for ln in out.splitlines() if "-->" in ln]
        assert len(edges) == 2
        assert "n0_a0 --> n1_a1" in out
        assert "n1_a1 --> n2_a2" in out

    def test_depends_on_alias_also_works(self):
        """Some older plan emitters use ``dependsOn`` (camelCase)
        instead of ``depends_on``. Both are honoured."""
        actions = [
            {"op": "bundle", "id": "a0"},
            {"op": "plan", "id": "a1", "dependsOn": ["a0"]},
        ]
        out = viz_plan._build_mermaid_graph(actions)
        assert "n0_a0 --> n1_a1" in out

    def test_fallback_sequential_chain_when_no_deps(self):
        """Plans without explicit ``depends_on`` are chained sequentially
        so the graph is still connected. Otherwise a bare list of
        actions would render as disconnected islands, which is noisy."""
        actions = [{"op": "x", "id": f"a{i}"} for i in range(3)]
        out = viz_plan._build_mermaid_graph(actions)
        edges = [ln for ln in out.splitlines() if "-->" in ln]
        assert len(edges) == 2
        assert "n0_a0 --> n1_a1" in out
        assert "n1_a1 --> n2_a2" in out

    def test_classdef_present_for_all_modes(self):
        """The four classDef directives must appear so the CSS
        classes referenced by ``:::amend`` etc. actually bind to
        colours."""
        out = viz_plan._build_mermaid_graph([{"op": "x", "id": "a1"}])
        for cls in ["amend", "replace", "skipped", "unknown"]:
            assert f"classDef {cls}" in out

    def test_replace_mode_gets_replace_class(self):
        actions = [{"op": "replace_table", "id": "r1", "mode": "replace"}]
        out = viz_plan._build_mermaid_graph(actions)
        assert ":::replace" in out

    def test_amend_mode_gets_amend_class(self):
        actions = [{"op": "x", "id": "a1", "mode": "amend"}]
        out = viz_plan._build_mermaid_graph(actions)
        assert ":::amend" in out

    def test_orphan_dependency_ref_skipped(self):
        """If ``depends_on`` references an id that doesn't exist in
        the actions list, the edge is silently skipped rather than
        producing a broken node-pair reference that mermaid would
        render as a phantom node."""
        actions = [
            {"op": "x", "id": "a0", "depends_on": ["nonexistent"]},
        ]
        out = viz_plan._build_mermaid_graph(actions)
        # No edges; just the one node.
        assert "-->" not in out


# -----------------------------------------------------------------------------
# render_plan_html — end-to-end
# -----------------------------------------------------------------------------


class TestRenderPlanHtml:
    @pytest.fixture()
    def logger(self):
        return logging.getLogger("test_viz_plan")

    def test_writes_html_to_disk(self, tmp_path, logger):
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps({"actions": [{"op": "bundle", "id": "a0"}]}),
            encoding="utf-8",
        )
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        assert out.exists()
        content = out.read_text()
        assert "<!doctype html>" in content.lower()
        assert "FLUID Plan" in content

    def test_mermaid_script_loaded(self, tmp_path, logger):
        """Regression guard: the mermaid CDN script must appear.
        Without it the ``<pre class="mermaid">`` blocks render as
        plain text."""
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"actions": []}), encoding="utf-8")
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        content = out.read_text()
        assert "mermaid" in content.lower()
        assert "jsdelivr.net" in content  # CDN source

    def test_security_level_strict_set(self, tmp_path, logger):
        """The mermaid.initialize call must include
        ``securityLevel: 'strict'`` so a malicious action id / op
        string can't smuggle <script> tags into a label and run
        when the plan is opened."""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps({"actions": [{"op": "x", "id": "<script>alert(1)</script>"}]}),
            encoding="utf-8",
        )
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        content = out.read_text()
        assert "'strict'" in content or '"strict"' in content

    def test_mermaid_block_contains_all_actions(self, tmp_path, logger):
        """Every action id must appear in the mermaid body so the
        graph shows the full plan."""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "actions": [
                        {"op": "bundle", "id": "a0"},
                        {"op": "validate", "id": "a1", "depends_on": ["a0"]},
                        {"op": "plan", "id": "a2", "depends_on": ["a1"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        content = out.read_text()
        # Inside <pre class="mermaid">...</pre> we expect all three ids.
        assert "a0" in content
        assert "a1" in content
        assert "a2" in content
        # And the edges:
        assert "--> n1_a1" in content
        assert "--> n2_a2" in content

    def test_legend_block_present(self, tmp_path, logger):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"actions": []}), encoding="utf-8")
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        content = out.read_text()
        # Legend mentions all four mode colours.
        for mode in ["amend", "replace", "skipped", "unknown"]:
            assert mode in content

    def test_raw_json_drilldown_present(self, tmp_path, logger):
        """Graph is for overview; raw JSON block beneath it is for
        "what's the exact field value" operator inspection. Both
        must ship in every emit."""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps({"actions": [{"op": "x", "id": "only", "mode": "amend"}]}),
            encoding="utf-8",
        )
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        content = out.read_text()
        # The JSON should contain the full action object.
        assert '"mode": "amend"' in content
