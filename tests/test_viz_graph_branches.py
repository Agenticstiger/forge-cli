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

"""Branch coverage tests for viz_graph.py."""

import argparse
import logging
from unittest.mock import patch

import pytest

# ---- GraphConfig ----


class TestGraphConfig:
    def test_create_valid(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        cfg = GraphConfig(contract_path=str(contract))
        assert cfg.format == "svg"
        assert cfg.theme == "dark"
        assert cfg.rankdir == "LR"

    def test_invalid_format(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(ValueError, match="Invalid format"):
            GraphConfig(contract_path=str(contract), format="pdf")

    def test_invalid_theme(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(ValueError, match="Invalid theme"):
            GraphConfig(contract_path=str(contract), theme="neon")

    def test_invalid_rankdir(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(ValueError, match="Invalid rankdir"):
            GraphConfig(contract_path=str(contract), rankdir="XY")

    def test_missing_contract(self):
        from fluid_build.cli.viz_graph import GraphConfig

        with pytest.raises(FileNotFoundError):
            GraphConfig(contract_path="/nonexistent/contract.yaml")

    def test_missing_plan(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(FileNotFoundError):
            GraphConfig(contract_path=str(contract), plan_path="/nonexistent/plan.json")

    def test_short_label_length(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(ValueError, match="max_label_length"):
            GraphConfig(contract_path=str(contract), max_label_length=5)

    @pytest.mark.parametrize("fmt", ["dot", "svg", "png", "html"])
    def test_valid_formats(self, fmt, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        cfg = GraphConfig(contract_path=str(contract), format=fmt)
        assert cfg.format == fmt

    @pytest.mark.parametrize("theme", ["dark", "light", "minimal", "blueprint"])
    def test_valid_themes(self, theme, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        cfg = GraphConfig(contract_path=str(contract), theme=theme)
        assert cfg.theme == theme

    @pytest.mark.parametrize("rankdir", ["LR", "TB", "RL", "BT"])
    def test_valid_rankdirs(self, rankdir, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        cfg = GraphConfig(contract_path=str(contract), rankdir=rankdir)
        assert cfg.rankdir == rankdir

    def test_custom_theme_missing(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        with pytest.raises(FileNotFoundError):
            GraphConfig(
                contract_path=str(contract), theme="custom", custom_theme_path="/missing/theme.json"
            )

    def test_custom_theme_valid(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        theme_file = tmp_path / "theme.json"
        theme_file.write_text('{"bg": "#000"}')
        cfg = GraphConfig(
            contract_path=str(contract), theme="custom", custom_theme_path=str(theme_file)
        )
        assert cfg.custom_theme_path == str(theme_file)

    def test_all_options(self, tmp_path):
        from fluid_build.cli.viz_graph import GraphConfig

        contract = tmp_path / "contract.yaml"
        contract.write_text("name: test")
        cfg = GraphConfig(
            contract_path=str(contract),
            output_path="out.svg",
            format="svg",
            theme="light",
            rankdir="TB",
            title="My Graph",
            show_legend=True,
            collapse_consumes=True,
            collapse_exposes=True,
            show_metadata=False,
            show_descriptions=True,
            max_label_length=30,
            open_when_done=True,
            force_overwrite=True,
            quiet=True,
            graphviz_args=["-Gdpi=300"],
        )
        assert cfg.title == "My Graph"
        assert cfg.show_legend is True
        assert cfg.collapse_consumes is True


# ---- GraphMetrics ----


class TestGraphMetrics:
    def test_create(self):
        from fluid_build.cli.viz_graph import GraphMetrics

        metrics = GraphMetrics()
        assert metrics.node_count == 0

    def test_mark_load(self):
        from fluid_build.cli.viz_graph import GraphMetrics

        metrics = GraphMetrics()
        metrics.mark_load_complete()
        assert metrics.load_time is not None
        assert metrics.load_time >= 0

    def test_mark_render(self):
        from fluid_build.cli.viz_graph import GraphMetrics

        metrics = GraphMetrics()
        metrics.mark_load_complete()
        metrics.mark_render_complete()
        assert metrics.render_time is not None
        assert metrics.total_time is not None

    def test_to_dict(self):
        from fluid_build.cli.viz_graph import GraphMetrics

        metrics = GraphMetrics()
        metrics.node_count = 5
        metrics.edge_count = 3
        metrics.mark_load_complete()
        metrics.mark_render_complete()
        d = metrics.to_dict()
        assert d["node_count"] == 5
        assert d["edge_count"] == 3
        assert "load_time_ms" in d
        assert "render_time_ms" in d

    def test_to_dict_no_times(self):
        from fluid_build.cli.viz_graph import GraphMetrics

        metrics = GraphMetrics()
        d = metrics.to_dict()
        assert d["load_time_ms"] == 0
        assert d["total_time_ms"] == 0


# ---- Themes ----


class TestThemes:
    def test_themes_exist(self):
        from fluid_build.cli.viz_graph import THEMES

        assert "dark" in THEMES
        assert "light" in THEMES
        assert "minimal" in THEMES
        assert "blueprint" in THEMES

    def test_theme_keys(self):
        from fluid_build.cli.viz_graph import THEMES

        required_keys = ["bg", "fg", "edge", "font", "product_fill", "product_border"]
        for theme_name, theme in THEMES.items():
            for key in required_keys:
                assert key in theme, f"Missing {key} in {theme_name}"


# ---- Register ----


class TestRegister:
    def test_register(self):
        from fluid_build.cli.viz_graph import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

    def test_register_args(self):
        from fluid_build.cli.viz_graph import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["viz-graph", "test.yaml", "--format", "png", "--theme", "light"])
        assert args.format == "png"
        assert args.theme == "light"


# ---- Run function ----


class TestRunFunction:
    @patch("fluid_build.cli.viz_graph._write_output")
    @patch("fluid_build.cli.viz_graph.load_contract_with_overlay")
    def test_run_dot_format(self, mock_load, _mock_write, tmp_path):
        from fluid_build.cli.viz_graph import run

        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text("name: test")
        mock_load.return_value = {
            "name": "test",
            "id": "test-id",
            "version": "1.0",
            "exposes": [],
            "consumes": [],
        }
        output = tmp_path / "output" / "out.dot"
        args = argparse.Namespace(
            contract=str(contract_file),
            output=str(output),
            format="dot",
            env=None,
            plan=None,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            show_metadata=True,
            show_descriptions=False,
            max_label_length=50,
            open=False,
            force=True,
            quiet=True,
            custom_theme=None,
            graphviz_args=None,
        )
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch(
        "fluid_build.cli.viz_graph.load_contract_with_overlay",
        side_effect=FileNotFoundError("nope"),
    )
    def test_run_missing_contract(self, _mock_load, tmp_path):
        from fluid_build.cli._common import CLIError
        from fluid_build.cli.viz_graph import run

        args = argparse.Namespace(
            contract="/nonexistent",
            output="out.dot",
            format="dot",
            env=None,
            plan=None,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            show_metadata=True,
            show_descriptions=False,
            max_label_length=50,
            open=False,
            force=True,
            quiet=True,
            custom_theme=None,
            graphviz_args=None,
        )
        with pytest.raises(CLIError):
            run(args, logging.getLogger("test"))
