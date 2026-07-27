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

"""Tests for fluid_build.cli.viz_graph — pure helpers, GraphMetrics, themes."""

import time

from fluid_build.cli.viz_graph import (
    THEMES,
    GraphMetrics,
    _build_mesh_dot,
    _escape_label,
    _get_theme_value,
    _safe_id,
)

# ── _safe_id ──


class TestSafeId:
    def test_hyphens(self):
        assert _safe_id("my-node") == "my_node"

    def test_dots(self):
        assert _safe_id("a.b.c") == "a_b_c"

    def test_slashes(self):
        assert _safe_id("src/main") == "src_main"

    def test_spaces(self):
        assert _safe_id("node name") == "node_name"

    def test_colons(self):
        assert _safe_id("db:table") == "db_table"

    def test_at_sign(self):
        assert _safe_id("user@host") == "user_host"

    def test_combined(self):
        assert _safe_id("a-b.c/d e:f@g#h%i&j") == "a_b_c_d_e_f_g_h_i_j"

    def test_no_changes(self):
        assert _safe_id("simple") == "simple"

    def test_double_quote_becomes_underscore(self):
        # Node IDs are interpolated UNQUOTED, so a surviving quote ends the
        # identifier and everything after it is parsed as graph structure.
        assert '"' not in _safe_id('x" fillcolor="red" q="')

    def test_backslash_becomes_underscore(self):
        assert "\\" not in _safe_id("a\\b")

    def test_dot_syntax_characters_become_underscores(self):
        # Anything DOT reads as syntax: brackets, braces, semicolons, equals,
        # commas, angle brackets (HTML-like labels), and the edge operator.
        assert _safe_id("a[b]{c};d=e,f<g>h") == "a_b__c__d_e_f_g_h"

    def test_non_ascii_letters_are_preserved(self):
        # DOT permits \\200-\\377 in an ID, so international names should not
        # degrade into underscores.
        assert _safe_id("commandes_café") == "commandes_café"

    def test_injected_id_yields_no_extra_graph_structure(self):
        # Regression for the concrete payload: rendered via graphviz 12 this
        # produced 10 nodes instead of 5 before the allowlist landed.
        out = _safe_id('consume_x" fillcolor="red" label="SPOOFED" q="')
        assert out == "consume_x__fillcolor__red__label__SPOOFED__q__"


# ── _escape_label ──


class TestEscapeLabel:
    def test_quotes(self):
        assert _escape_label('say "hello"') == 'say \\"hello\\"'

    def test_newline(self):
        assert _escape_label("line1\nline2") == "line1\\nline2"

    def test_carriage_return_removed(self):
        assert "\r" not in _escape_label("a\rb")

    def test_tab_to_space(self):
        assert _escape_label("a\tb") == "a b"

    def test_truncation(self):
        result = _escape_label("a" * 50, max_length=20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_no_truncation_when_short(self):
        assert _escape_label("short", max_length=20) == "short"

    def test_no_max_length(self):
        long_str = "x" * 200
        assert _escape_label(long_str) == long_str

    def test_backslash_is_doubled(self):
        assert _escape_label("a\\b") == "a\\\\b"

    def test_trailing_backslash_does_not_swallow_the_closing_quote(self):
        # label="X\\" would leave the DOT string unterminated: graphviz failed
        # the entire render with `syntax error in line N`, so any contract with a
        # trailing backslash produced no graph at all.
        escaped = _escape_label("orders\\")
        assert escaped == "orders\\\\"
        assert not escaped.endswith("\\\\" + '"')
        # Emitted into the real template, the string closes.
        line = f'  n1 [label="{escaped}"];'
        assert line.count('"') == 2

    def test_backslash_escaped_before_quote(self):
        # Order matters: escaping quotes first would let the backslash pass
        # double the backslash of each \\", turning an escaped quote back into a
        # literal backslash plus a string terminator.
        assert _escape_label('a"b') == 'a\\"b'

    def test_real_newline_still_becomes_the_dot_escape(self):
        # Callers compose multi-line labels with a real newline and rely on this
        # conversion; it must survive the backslash pass un-doubled.
        assert _escape_label("top\nbottom") == "top\\nbottom"


# ── _get_theme_value ──


class TestGetThemeValue:
    def test_dark_theme(self):
        assert _get_theme_value("dark", "bg") == "#0B1020"

    def test_light_theme(self):
        assert _get_theme_value("light", "fg") == "#111827"

    def test_unknown_theme_falls_back_to_dark(self):
        assert _get_theme_value("nonexistent", "bg") == "#0B1020"

    def test_custom_theme_overrides(self):
        custom = {"bg": "#FF0000"}
        assert _get_theme_value("dark", "bg", custom_theme=custom) == "#FF0000"

    def test_custom_theme_fallback(self):
        custom = {"bg": "#FF0000"}
        # Key not in custom, falls back to theme
        assert _get_theme_value("dark", "fg", custom_theme=custom) == "#E5E7EB"


# ── THEMES ──


class TestThemes:
    def test_all_themes_have_required_keys(self):
        required_keys = {"bg", "fg", "edge", "font", "product_fill", "product_border"}
        for name, theme in THEMES.items():
            for key in required_keys:
                assert key in theme, f"Theme '{name}' missing key '{key}'"

    def test_dark_theme_exists(self):
        assert "dark" in THEMES

    def test_light_theme_exists(self):
        assert "light" in THEMES


# ── GraphMetrics ──


class TestGraphMetrics:
    def test_defaults(self):
        m = GraphMetrics()
        assert m.node_count == 0
        assert m.edge_count == 0
        assert m.cluster_count == 0
        assert m.total_time is None

    def test_mark_load_complete(self):
        m = GraphMetrics()
        time.sleep(0.01)
        m.mark_load_complete()
        assert m.load_time is not None
        assert m.load_time > 0

    def test_mark_render_complete(self):
        m = GraphMetrics()
        time.sleep(0.01)
        m.mark_load_complete()
        time.sleep(0.01)
        m.mark_render_complete()
        assert m.render_time is not None
        assert m.total_time is not None
        assert m.total_time > 0

    def test_to_dict(self):
        m = GraphMetrics()
        m.node_count = 5
        m.edge_count = 3
        m.cluster_count = 2
        m.mark_load_complete()
        m.mark_render_complete()
        d = m.to_dict()
        assert d["node_count"] == 5
        assert d["edge_count"] == 3
        assert d["cluster_count"] == 2
        assert "load_time_ms" in d
        assert "render_time_ms" in d
        assert "total_time_ms" in d

    def test_to_dict_no_render(self):
        m = GraphMetrics()
        d = m.to_dict()
        assert d["load_time_ms"] == 0
        assert d["total_time_ms"] == 0


# ── _build_mesh_dot (contract-authored text reaches DOT source) ──


class TestBuildMeshDotEscaping:
    """The mesh graph is rendered from contract-authored labels and IDs.

    Neither was escaped: a product could close the label string and inject a
    second ``label`` attribute, which DOT honours (last one wins), so it
    displayed a name of its choosing in a graph a reviewer reads. Verified
    against graphviz 12 before and after — the payload below rendered
    ``>SPOOFED<`` in the SVG and now does not.
    """

    @staticmethod
    def _node_line(dot: str) -> str:
        return next(
            line
            for line in dot.splitlines()
            if "[" in line and "node [" not in line and "edge [" not in line
        )

    def test_label_quote_cannot_inject_a_second_attribute(self):
        node = {"id": "p1", "label": 'X" fillcolor="red" label="SPOOFED', "layer": "silver"}
        line = self._node_line(_build_mesh_dot({node["id"]: node}, [], rankdir="LR"))
        # Count only UNESCAPED quotes: the payload's text survives as escaped
        # label content (harmless), so a substring check for `fillcolor=` would
        # match that text and prove nothing. Six delimiters are expected — the ID,
        # the label value, and the fillcolor value.
        assert line.replace('\\"', "").count('"') == 6
        # The payload is still there, as inert text rather than syntax.
        assert '\\"' in line

    def test_trailing_backslash_in_id_does_not_break_the_render(self):
        node = {"id": "p1\\", "label": "Orders", "layer": "silver"}
        line = self._node_line(_build_mesh_dot({node["id"]: node}, [], rankdir="LR"))
        # `"p1\"` would leave the ID string unterminated (graphviz: syntax error).
        assert line.startswith('  "p1\\\\"')

    def test_edge_label_is_escaped(self):
        node = {"id": "p1", "label": "Orders", "layer": "silver"}
        dot = _build_mesh_dot(
            {node["id"]: node}, [("p1", "p1", 'e" color="red" label="X')], rankdir="LR"
        )
        edge = next(line for line in dot.splitlines() if "->" in line)
        assert 'color="red"' not in edge

    def test_clean_input_is_unchanged(self):
        node = {"id": "p1", "label": "Orders", "layer": "silver"}
        line = self._node_line(_build_mesh_dot({node["id"]: node}, [], rankdir="LR"))
        assert 'label="Orders\\np1"' in line
