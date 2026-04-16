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

"""Tests for GraphBuilder DOT generation methods in cli/viz_graph.py."""

import logging
from unittest.mock import patch

import pytest

from fluid_build.cli.viz_graph import GraphBuilder, GraphConfig, GraphMetrics


def _make_config(**overrides):
    """Create GraphConfig bypassing file validation."""
    with patch.object(GraphConfig, "__post_init__"):
        cfg = GraphConfig(contract_path="/fake/contract.yaml", **overrides)
    return cfg


@pytest.fixture
def config():
    return _make_config()


@pytest.fixture
def builder(config):
    metrics = GraphMetrics()
    return GraphBuilder(config, metrics, logging.getLogger("test"))


@pytest.fixture
def contract():
    return {
        "id": "btc-tracker",
        "name": "Bitcoin Price Tracker",
        "domain": "finance",
        "metadata": {"layer": "gold"},
        "consumes": [
            {"id": "raw-prices", "ref": "coinbase-api"},
            {"id": "exchange-rates", "ref": "ecb-api"},
        ],
        "exposes": [
            {"id": "daily_prices", "type": "table", "location": {"format": "parquet"}},
            {"id": "alerts", "type": "view"},
        ],
    }


class TestBuildProductCluster:
    def test_basic_cluster(self, builder, contract):
        lines = builder._build_product_cluster(contract, "prod_node")
        joined = "\n".join(lines)
        assert "subgraph cluster_product" in joined
        assert "prod_node" in joined
        assert "Bitcoin Price Tracker" in joined

    def test_metadata_tags(self, builder, contract):
        builder.config.show_metadata = True
        lines = builder._build_product_cluster(contract, "prod_node")
        joined = "\n".join(lines)
        assert "Domain: finance" in joined
        assert "Layer: gold" in joined

    def test_no_metadata_tags(self, builder, contract):
        builder.config.show_metadata = False
        lines = builder._build_product_cluster(contract, "prod_node")
        joined = "\n".join(lines)
        assert "Domain:" not in joined

    def test_missing_fields_defaults(self, builder):
        lines = builder._build_product_cluster({}, "node")
        joined = "\n".join(lines)
        assert "product" in joined  # default id


class TestBuildConsumesCluster:
    def test_empty_consumes(self, builder):
        lines, nodes = builder._build_consumes_cluster([], "prod")
        assert lines == []
        assert nodes == []

    def test_with_consumes(self, builder, contract):
        lines, nodes = builder._build_consumes_cluster(contract["consumes"], "prod")
        joined = "\n".join(lines)
        assert "subgraph cluster_consumes" in joined
        assert len(nodes) == 2
        assert builder.metrics.node_count == 2
        assert builder.metrics.edge_count == 2

    def test_collapsed_consumes(self, builder, contract):
        builder.config.collapse_consumes = True
        lines, nodes = builder._build_consumes_cluster(contract["consumes"], "prod")
        "\n".join(lines)
        assert len(nodes) == 1
        assert nodes[0][0] == "consumes_agg"


class TestBuildExposesCluster:
    def test_empty_exposes(self, builder):
        lines, nodes = builder._build_exposes_cluster([], "prod")
        assert lines == []
        assert nodes == []

    def test_with_exposes(self, builder, contract):
        lines, nodes = builder._build_exposes_cluster(contract["exposes"], "prod")
        joined = "\n".join(lines)
        assert "subgraph cluster_exposes" in joined
        assert len(nodes) == 2
        assert "daily_prices" in joined
        assert "parquet" in joined

    def test_collapsed_exposes(self, builder, contract):
        builder.config.collapse_exposes = True
        lines, nodes = builder._build_exposes_cluster(contract["exposes"], "prod")
        assert len(nodes) == 1
        assert nodes[0][0] == "exposes_agg"

    def test_expose_with_description(self, builder):
        builder.config.show_descriptions = True
        exposes = [{"id": "out", "type": "table", "description": "Output table"}]
        lines, nodes = builder._build_exposes_cluster(exposes, "prod")
        joined = "\n".join(lines)
        assert "Output table" in joined


class TestBuildDot:
    def test_basic_dot(self, builder, contract):
        dot = builder.build_dot(contract)
        assert dot.startswith("digraph G {")
        assert dot.strip().endswith("}")
        assert "Bitcoin Price Tracker" in dot
        assert "cluster_product" in dot
        assert "cluster_consumes" in dot
        assert "cluster_exposes" in dot

    def test_dot_without_consumes_or_exposes(self, builder):
        dot = builder.build_dot({"id": "simple", "name": "Simple"})
        assert "digraph G {" in dot
        assert "cluster_consumes" not in dot
        assert "cluster_exposes" not in dot

    def test_dot_with_plan(self, builder, contract):
        plan = {
            "actions": [
                {"op": "create_table", "dataset": "ds", "table": "t1"},
                {"op": "load_data", "name": "loader"},
            ]
        }
        dot = builder.build_dot(contract, plan=plan)
        assert "cluster_plan" in dot
        assert "create_table" in dot
        assert "load_data" in dot

    def test_metrics_updated(self, builder, contract):
        builder.build_dot(contract)
        assert builder.metrics.node_count >= 3  # product + 2 consume or expose
        assert builder.metrics.cluster_count >= 3

    def test_rankdir(self):
        config = _make_config(rankdir="LR")
        b = GraphBuilder(config, GraphMetrics(), logging.getLogger("test"))
        dot = b.build_dot({"id": "x"})
        assert "rankdir=LR" in dot

    def test_custom_title(self):
        config = _make_config(title="My Custom Title")
        b = GraphBuilder(config, GraphMetrics(), logging.getLogger("test"))
        dot = b.build_dot({"id": "x"})
        assert "My Custom Title" in dot


class TestBuildPlanCluster:
    def test_empty_actions(self, builder):
        lines = builder._build_plan_cluster([], "prod", [])
        assert lines == []

    def test_actions_with_dataset_table(self, builder):
        actions = [{"op": "create_table", "dataset": "ds", "table": "t1"}]
        lines = builder._build_plan_cluster(actions, "prod", [])
        joined = "\n".join(lines)
        assert "cluster_plan" in joined
        assert "ds.t1" in joined

    def test_sequential_edges(self, builder):
        actions = [{"op": "a"}, {"op": "b"}, {"op": "c"}]
        lines = builder._build_plan_cluster(actions, "prod", [])
        joined = "\n".join(lines)
        # Should have edges between consecutive actions
        assert "->" in joined
        # product -> first action
        assert "prod ->" in joined

    def test_last_action_to_exposes(self, builder):
        actions = [{"op": "load"}]
        expose_nodes = [("exp_1", "output")]
        lines = builder._build_plan_cluster(actions, "prod", expose_nodes)
        joined = "\n".join(lines)
        assert "exp_1" in joined
