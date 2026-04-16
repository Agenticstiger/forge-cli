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

"""
Unit tests for fluid_build.cli.viz_provider_actions — 0% coverage module.
Targets all public functions: visualize_provider_actions_dot,
visualize_provider_actions_html, _render_action_item, and
add_provider_actions_to_viz.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.viz_provider_actions import (
    _render_action_item,
    add_provider_actions_to_viz,
    visualize_provider_actions_dot,
    visualize_provider_actions_html,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_ACTIONS = [
    {
        "action_id": "act-001",
        "action_type": "create",
        "provider": "gcp",
        "resource": "my_table",
    },
    {
        "action_id": "act-002",
        "action_type": "update",
        "provider": "gcp",
        "resource": "other_table",
    },
]

MULTI_PROVIDER_ACTIONS = [
    {
        "action_id": "act-gcp-1",
        "action_type": "create",
        "provider": "gcp",
        "resource": "bq_table",
    },
    {
        "action_id": "act-aws-1",
        "action_type": "query",
        "provider": "aws",
        "resource": "s3_bucket",
    },
    {
        "action_id": "act-sf-1",
        "action_type": "transform",
        "provider": "snowflake",
        "resource": "sf_table",
    },
]

EMPTY_DEPS: dict = {}


# ---------------------------------------------------------------------------
# Tests: visualize_provider_actions_dot
# ---------------------------------------------------------------------------


class TestVisualizeProviderActionsDot:
    def test_returns_string(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert isinstance(result, str)

    def test_contains_digraph_header(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "digraph ProviderActions {" in result

    def test_contains_closing_brace(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert result.strip().endswith("}")

    def test_contains_action_ids(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert '"act-001"' in result
        assert '"act-002"' in result

    def test_contains_provider_subgraph(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "subgraph cluster_gcp" in result

    def test_provider_label_uppercased(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert 'label="GCP"' in result

    def test_contains_rankdir(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "rankdir=TB" in result

    def test_multiple_providers_create_multiple_subgraphs(self):
        result = visualize_provider_actions_dot(MULTI_PROVIDER_ACTIONS, EMPTY_DEPS)
        assert "subgraph cluster_gcp" in result
        assert "subgraph cluster_aws" in result
        assert "subgraph cluster_snowflake" in result

    def test_dependency_edges_rendered(self):
        deps = {"act-002": ["act-001"]}
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, deps)
        assert '"act-001" -> "act-002"' in result
        assert 'label="depends"' in result

    def test_sequential_order_edges_rendered_when_no_dependency(self):
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert '"act-001" -> "act-002"' in result
        assert 'label="then"' in result

    def test_order_edge_present_even_when_dependency_exists(self):
        # Both dependency and order edges are emitted (source does not deduplicate).
        deps = {"act-002": ["act-001"]}
        result = visualize_provider_actions_dot(SIMPLE_ACTIONS, deps)
        # The edge appears twice: once as dependency, once as "then" order edge.
        assert result.count('"act-001" -> "act-002"') == 2

    def test_execution_time_in_label_when_present(self):
        actions = [
            {
                "action_id": "act-t",
                "action_type": "create",
                "provider": "gcp",
                "resource": "tbl",
                "execution_time": 3.5,
            }
        ]
        result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
        assert "(3.5s)" in result

    def test_fallback_color_for_unknown_provider(self):
        actions = [
            {
                "action_id": "act-x",
                "action_type": "create",
                "provider": "custom_cloud",
                "resource": "tbl",
            }
        ]
        result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
        # Unknown provider uses fallback colour #607D8B
        assert "#607D8B" in result

    def test_known_provider_colors_used(self):
        for provider, color in [
            ("aws", "#FF9900"),
            ("azure", "#0078D4"),
            ("databricks", "#FF3621"),
            ("airflow", "#017CEE"),
        ]:
            actions = [
                {
                    "action_id": f"act-{provider}",
                    "action_type": "create",
                    "provider": provider,
                    "resource": "x",
                }
            ]
            result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
            assert color in result, f"Expected colour {color} for provider {provider}"

    def test_action_type_color_applied(self):
        for atype, color in [
            ("delete", "#EA4335"),
            ("query", "#4285F4"),
            ("schedule", "#F538A0"),
            ("monitor", "#00ACC1"),
        ]:
            actions = [
                {
                    "action_id": f"act-{atype}",
                    "action_type": atype,
                    "provider": "gcp",
                    "resource": "r",
                }
            ]
            result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
            assert color in result, f"Expected colour {color} for action type {atype}"

    def test_empty_actions_list(self):
        result = visualize_provider_actions_dot([], EMPTY_DEPS)
        assert "digraph ProviderActions {" in result

    def test_action_with_id_key_fallback(self):
        """Actions using 'id' key instead of 'action_id'."""
        actions = [{"id": "fallback-id", "type": "create", "provider": "gcp", "resource": "t"}]
        result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
        assert '"fallback-id"' in result

    def test_action_resource_from_params(self):
        """resource falls back to params.table when 'resource' key absent."""
        actions = [
            {
                "action_id": "act-p",
                "action_type": "create",
                "provider": "gcp",
                "params": {"table": "param_table"},
            }
        ]
        result = visualize_provider_actions_dot(actions, EMPTY_DEPS)
        assert "param_table" in result

    def test_multiple_dependencies_per_action(self):
        actions = [
            {"action_id": "a", "action_type": "create", "provider": "gcp", "resource": "x"},
            {"action_id": "b", "action_type": "create", "provider": "gcp", "resource": "y"},
            {"action_id": "c", "action_type": "create", "provider": "gcp", "resource": "z"},
        ]
        deps = {"c": ["a", "b"]}
        result = visualize_provider_actions_dot(actions, deps)
        assert '"a" -> "c"' in result
        assert '"b" -> "c"' in result


# ---------------------------------------------------------------------------
# Tests: visualize_provider_actions_html
# ---------------------------------------------------------------------------


class TestVisualizeProviderActionsHtml:
    def test_returns_string(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert isinstance(result, str)

    def test_html_doctype_present(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "<!DOCTYPE html>" in result

    def test_contains_dot_content(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "digraph ProviderActions" in result

    def test_stat_total_actions(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        # 2 actions — value appears in the stat block
        assert '<div class="stat-value">2</div>' in result

    def test_stat_providers_count(self):
        result = visualize_provider_actions_html(MULTI_PROVIDER_ACTIONS, EMPTY_DEPS)
        # 3 unique providers
        assert '<div class="stat-value">3</div>' in result

    def test_stat_dependencies_count(self):
        deps = {"act-002": ["act-001"]}
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, deps)
        # 1 dependency edge
        assert '<div class="stat-value">1</div>' in result

    def test_action_items_rendered(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "act-001" in result
        assert "act-002" in result

    def test_empty_actions_produces_valid_html(self):
        result = visualize_provider_actions_html([], EMPTY_DEPS)
        assert "<html>" in result
        assert "</html>" in result

    def test_graphviz_link_present(self):
        result = visualize_provider_actions_html(SIMPLE_ACTIONS, EMPTY_DEPS)
        assert "GraphvizOnline" in result


# ---------------------------------------------------------------------------
# Tests: _render_action_item
# ---------------------------------------------------------------------------


class TestRenderActionItem:
    def test_returns_html_string(self):
        action = {"action_id": "x1", "action_type": "create", "provider": "gcp"}
        html = _render_action_item(action, {})
        assert isinstance(html, str)
        assert "x1" in html

    def test_action_type_shown(self):
        action = {"action_id": "x2", "action_type": "delete", "provider": "aws"}
        html = _render_action_item(action, {})
        assert "delete" in html

    def test_provider_shown(self):
        action = {"action_id": "x3", "action_type": "query", "provider": "snowflake"}
        html = _render_action_item(action, {})
        assert "snowflake" in html

    def test_dependencies_shown_when_present(self):
        action = {"action_id": "x4", "action_type": "create", "provider": "gcp"}
        deps = {"x4": ["x1", "x3"]}
        html = _render_action_item(action, deps)
        assert "Depends on" in html
        assert "x1" in html
        assert "x3" in html

    def test_no_dependency_section_when_empty(self):
        action = {"action_id": "x5", "action_type": "create", "provider": "gcp"}
        html = _render_action_item(action, {})
        assert "Depends on" not in html

    def test_fallback_id_key(self):
        action = {"id": "fallback", "type": "update", "provider": "azure"}
        html = _render_action_item(action, {})
        assert "fallback" in html

    def test_unknown_keys_use_unknown_label(self):
        action = {}
        html = _render_action_item(action, {})
        assert "unknown" in html


# ---------------------------------------------------------------------------
# Tests: add_provider_actions_to_viz
# ---------------------------------------------------------------------------


class TestAddProviderActionsToViz:
    def _make_logger(self):
        return MagicMock(spec=logging.Logger)

    def test_returns_none_when_import_fails(self):
        logger = self._make_logger()
        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": None}):
            result = add_provider_actions_to_viz({}, logger)
        # ImportError path should return None
        assert result is None

    def test_returns_none_when_actions_list_empty(self):
        logger = self._make_logger()
        mock_parser = MagicMock()
        mock_parser.parse.return_value = []
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            # Re-import inside the patch scope so the function picks up the mock
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({"some": "contract"}, logger)
        assert result is None

    def test_returns_actions_and_deps_tuple(self):
        logger = self._make_logger()

        mock_action = MagicMock()
        mock_action.action_id = "a1"
        mock_action.action_type = MagicMock()
        mock_action.action_type.value = "create"
        mock_action.provider = "gcp"
        mock_action.params = {"table": "my_table"}
        mock_action.depends_on = []

        mock_parser = MagicMock()
        mock_parser.parse.return_value = [mock_action]
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({"data": "contract"}, logger)

        assert result is not None
        actions_dicts, dependencies = result
        assert len(actions_dicts) == 1
        assert actions_dicts[0]["action_id"] == "a1"
        assert actions_dicts[0]["provider"] == "gcp"
        assert actions_dicts[0]["resource"] == "my_table"
        assert isinstance(dependencies, dict)

    def test_dependencies_populated_from_depends_on(self):
        logger = self._make_logger()

        action_a = MagicMock()
        action_a.action_id = "a"
        action_a.action_type = MagicMock()
        action_a.action_type.value = "create"
        action_a.provider = "gcp"
        action_a.params = {}
        action_a.depends_on = []

        action_b = MagicMock()
        action_b.action_id = "b"
        action_b.action_type = MagicMock()
        action_b.action_type.value = "update"
        action_b.provider = "gcp"
        action_b.params = {}
        action_b.depends_on = ["a"]

        mock_parser = MagicMock()
        mock_parser.parse.return_value = [action_a, action_b]
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({}, logger)

        assert result is not None
        _, dependencies = result
        assert dependencies["b"] == ["a"]

    def test_returns_none_on_unexpected_exception(self):
        logger = self._make_logger()

        mock_parser = MagicMock()
        mock_parser.parse.side_effect = RuntimeError("boom")
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({}, logger)

        assert result is None
        logger.error.assert_called_once()

    def test_resource_falls_back_to_bucket(self):
        logger = self._make_logger()

        mock_action = MagicMock()
        mock_action.action_id = "b1"
        mock_action.action_type = MagicMock()
        mock_action.action_type.value = "create"
        mock_action.provider = "aws"
        mock_action.params = {"bucket": "my_bucket"}
        mock_action.depends_on = []

        mock_parser = MagicMock()
        mock_parser.parse.return_value = [mock_action]
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({}, logger)

        assert result is not None
        actions_dicts, _ = result
        assert actions_dicts[0]["resource"] == "my_bucket"

    def test_resource_falls_back_to_dataset(self):
        logger = self._make_logger()

        mock_action = MagicMock()
        mock_action.action_id = "d1"
        mock_action.action_type = MagicMock()
        mock_action.action_type.value = "create"
        mock_action.provider = "gcp"
        mock_action.params = {"dataset": "my_dataset"}
        mock_action.depends_on = []

        mock_parser = MagicMock()
        mock_parser.parse.return_value = [mock_action]
        mock_parser_cls = MagicMock(return_value=mock_parser)

        fake_module = MagicMock()
        fake_module.ProviderActionParser = mock_parser_cls

        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": fake_module}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            result = fn({}, logger)

        assert result is not None
        actions_dicts, _ = result
        assert actions_dicts[0]["resource"] == "my_dataset"

    def test_debug_logged_on_import_error(self):
        logger = self._make_logger()
        with patch.dict("sys.modules", {"fluid_build.forge.core.provider_actions": None}):
            from fluid_build.cli.viz_provider_actions import add_provider_actions_to_viz as fn

            fn({}, logger)
        logger.debug.assert_called_once()
