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
        # Match the full expected CDN path rather than a bare
        # ``jsdelivr.net`` substring — CodeQL flags bare-domain
        # substring checks as ``py/incomplete-url-substring-
        # sanitization`` (a URL like ``https://evil.com/jsdelivr.net/``
        # would satisfy the weaker check). The full path pins the
        # expected CDN origin + package without re-implementing a
        # URL parser just for a test assertion.
        assert "cdn.jsdelivr.net/npm/mermaid" in content

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
        # The JSON should contain the full action object — but with
        # HTML-safe unicode escapes on any <, >, & characters (the
        # JSON-in-HTML defence). ``"mode": "amend"`` is pure ASCII
        # alpha, so it's present verbatim.
        assert '"mode": "amend"' in content


# -----------------------------------------------------------------------------
# XSS regression guards — SECURITY-critical
#
# Pre-fix: the viz-plan renderer escaped only double-quotes on mermaid
# labels, leaving ``<``, ``>``, ``&`` unescaped. ``json.dumps`` by
# default doesn't escape HTML-relevant characters either. A contract
# author could set an action id / op to
# ``"safe_id</pre><script>alert(1)</script>"`` and the payload would
# break out of the ``<pre>`` wrapper + execute when an operator opened
# plan.html (HTML5 ``<pre>`` is ordinary flow content — child
# ``<script>`` tags execute; ``mermaid.securityLevel='strict'`` is a
# red herring because the browser's HTML tokenizer runs first).
#
# These tests lock the fix in. If any of them regress, the rendered
# HTML is XSS-vulnerable again.
# -----------------------------------------------------------------------------


class TestXssRegressionGuards:
    @pytest.fixture()
    def logger(self):
        return logging.getLogger("test_viz_plan")

    def _render(self, tmp_path, logger, actions):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"actions": actions}), encoding="utf-8")
        out = tmp_path / "plan.html"
        viz_plan.render_plan_html(str(plan), str(out), logger)
        return out.read_text()

    def test_malicious_action_id_does_not_produce_live_script_tag(self, tmp_path, logger):
        """The canonical XSS payload. A raw ``<script>alert(1)</script>``
        with a preceding ``</pre>`` closer MUST NOT survive into the
        rendered HTML in a browser-executable form.

        Fix: ``html.escape`` on op + id inside ``_mermaid_label`` +
        JSON-in-HTML unicode escape on the raw-JSON drill-down block.
        Both sinks must be disarmed — the mermaid label is the
        primary one, but the JSON-dump block is a secondary sink
        that would be independently exploitable without its own
        escape.
        """
        payload = "safe_id</pre><script>alert(1)</script>"
        content = self._render(
            tmp_path,
            logger,
            [{"op": "provisionDataset", "id": payload}],
        )
        # Core invariant: the browser-executable substring must NOT
        # appear anywhere in the rendered HTML.
        assert "<script>alert(1)</script>" not in content, (
            "XSS: raw <script>alert(1)</script> survived into HTML output. "
            "The ``_mermaid_label`` and/or JSON-dump sink failed to "
            "HTML-escape the action id. Check the ``html.escape`` call "
            "in _mermaid_label + the \\u003c/\\u003e replacement in "
            "render_plan_html."
        )
        # Secondary: the ``</pre>`` closer that would break out of
        # the surrounding element must also be gone.
        assert "</pre><script>" not in content, (
            "XSS: </pre><script> tag-breakout sequence survived into "
            "HTML. A browser would close the <pre>, then execute the "
            "following <script> as a real HTMLScriptElement."
        )

    def test_malicious_op_field_escaped_in_mermaid_label(self, tmp_path, logger):
        """Same class of attack, different field. ``op`` flows into
        the mermaid label alongside ``id`` — both must be escaped."""
        content = self._render(
            tmp_path,
            logger,
            [{"op": "evil</pre><script>x=1</script>", "id": "a1"}],
        )
        assert "<script>x=1</script>" not in content
        assert "</pre><script>" not in content

    def test_malicious_description_field_escaped_in_json_block(self, tmp_path, logger):
        """The raw-JSON drill-down block is a SECONDARY sink — it
        embeds the entire action dict. A malicious string in ANY
        field (description, params, etc.) would break out of the
        surrounding ``<pre>`` if not escaped. Test a field that
        doesn't appear in the mermaid label so this test isolates
        the JSON-sink defence."""
        content = self._render(
            tmp_path,
            logger,
            [
                {
                    "op": "harmless",
                    "id": "a1",
                    "description": "</pre><script>alert('json sink')</script>",
                }
            ],
        )
        assert "<script>alert(" not in content, (
            "XSS via JSON-dump sink: a description field containing a "
            "<script> tag was embedded as raw HTML inside the <pre> "
            "drill-down block. The render_plan_html function must "
            "\\uXXXX-escape < > & before template substitution."
        )
        # The JSON unicode escape should have rewritten these chars.
        # Confirm ``\u003c`` (< escape) appears in the rendered output
        # so we know the defence actually fired.
        assert "\\u003c" in content, (
            "expected JSON-in-HTML unicode escapes (\\u003c/\\u003e/"
            "\\u0026) on <, >, & — the escape pattern in "
            "render_plan_html didn't execute."
        )

    def test_ampersand_in_fields_escaped(self, tmp_path, logger):
        """``&`` must be escaped in both the mermaid label AND the
        JSON block. Without that, a payload like ``id=x&amp;`` could
        interact with other decoding paths downstream (URL encoders,
        email clients, etc.) in surprising ways.

        Fix uses ``html.escape(..., quote=True)`` on labels (turns
        ``&`` into ``&amp;``) and ``\\u0026`` on JSON.
        """
        content = self._render(
            tmp_path,
            logger,
            [{"op": "foo&bar", "id": "a&b"}],
        )
        # Raw ``&`` from op/id should NOT appear unescaped inside the
        # mermaid label. It should become ``&amp;``. (Note: the
        # ``&`` character elsewhere in the HTML template — e.g. CSS
        # selectors — is fine; we only check it's escaped where our
        # escape routine ran.)
        # The mermaid label block contains our escaped op + id.
        assert "foo&amp;bar" in content or "foo\\u0026bar" in content, (
            "& character in op field not escaped — check html.escape "
            "with quote=True in _mermaid_label."
        )

    def test_single_quote_in_op_escaped(self, tmp_path, logger):
        """``html.escape(..., quote=True)`` escapes ``'`` to ``&#x27;``.
        Without quote=True, an injected single quote could break out
        of a single-quoted HTML attribute context. Regression guard
        against accidentally flipping quote=False."""
        content = self._render(tmp_path, logger, [{"op": "f'unction", "id": "a1"}])
        # The literal apostrophe from op must be entity-encoded.
        # Inside the mermaid label area (double-quoted string), a
        # bare ``'`` would be fine for mermaid itself, but we want
        # defence-in-depth across contexts.
        assert "&#x27;" in content or "&#39;" in content

    def test_benign_payload_still_renders_correctly(self, tmp_path, logger):
        """The fix must NOT regress normal plans. A mundane
        ``provisionDataset`` action should render with its op and id
        visible (the escape turns nothing into nothing for
        alphanumeric inputs)."""
        content = self._render(
            tmp_path,
            logger,
            [
                {
                    "op": "provisionDataset",
                    "id": "silver.telco.subscriber360",
                    "mode": "amend",
                }
            ],
        )
        # Op + id both reachable in the output (not accidentally
        # entity-encoded so you can't read them).
        assert "provisionDataset" in content
        assert "silver.telco.subscriber360" in content

    def test_brnbsp_literal_preserved_in_mermaid_label(self, tmp_path, logger):
        """The safe literal ``<br/>`` separator that WE insert between
        op and id must remain as a mermaid line-break directive — NOT
        get escaped to ``&lt;br/&gt;``. Regression guard: a naive
        ``html.escape(full_label)`` would break this."""
        content = self._render(tmp_path, logger, [{"op": "a", "id": "b"}])
        # Our literal `<br/>` must be present verbatim inside the
        # label; mermaid treats it as a line break.
        assert "<br/>" in content
