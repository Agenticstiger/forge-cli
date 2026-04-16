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
