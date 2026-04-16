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

"""Extended tests for fluid_build.cli.viz_graph — uncovered functions."""

import argparse
import json
import logging
import subprocess
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.viz_graph import (
    GraphBuilder,
    GraphConfig,
    GraphMetrics,
    _build_contract_dot,
    _check_graphviz_installation,
    _create_html_wrapper,
    _get_file_size,
    _prepare_output_directory,
    _read_plan,
    _shell_open,
    _validate_input_file,
    _write_output,
    run,
)

logger = logging.getLogger("test_viz_graph_ext")

# ── Minimal contract fixture used across many tests ──────────────────────────

MINIMAL_CONTRACT = {
    "id": "test_product",
    "name": "Test Product",
    "domain": "engineering",
    "metadata": {"layer": "gold"},
    "consumes": [{"id": "src_orders", "ref": "orders_raw"}],
    "exposes": [{"id": "out_orders", "type": "table", "location": {"format": "delta"}}],
}

MINIMAL_PLAN = {
    "actions": [
        {"op": "create_table", "dataset": "ds1", "table": "t1"},
        {"op": "insert", "dst": "ds1.t1"},
        {"op": "validate", "name": "quality_check"},
    ]
}


# ── _build_contract_dot ───────────────────────────────────────────────────────


class TestBuildContractDot:
    def test_returns_digraph_string(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert dot.startswith("digraph G {")
        assert dot.endswith("}")

    def test_product_node_present(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "product_test_product" in dot
        assert "cluster_product" in dot

    def test_consumes_cluster_present(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "cluster_consumes" in dot
        assert "consume_orders_raw" in dot

    def test_exposes_cluster_present(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "cluster_exposes" in dot
        assert "expose_out_orders" in dot

    def test_collapse_consumes(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=True,
            collapse_exposes=False,
            plan=None,
        )
        assert "consumes_agg" in dot

    def test_collapse_exposes(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=True,
            plan=None,
        )
        assert "exposes_agg" in dot

    def test_legend_included_when_requested(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=True,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "cluster_legend" in dot
        assert "key_product" in dot
        assert "key_consume" in dot
        assert "key_action" in dot
        assert "key_expose" in dot

    def test_custom_title_used(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title="My Custom Title",
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "My Custom Title" in dot

    def test_plan_cluster_present(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=MINIMAL_PLAN,
        )
        assert "cluster_plan" in dot
        assert "action_0_create_table" in dot

    def test_plan_with_name_action(self):
        plan = {"actions": [{"op": "validate", "name": "quality_check"}]}
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=plan,
        )
        assert "quality_check" in dot

    def test_plan_with_dst_action(self):
        plan = {"actions": [{"op": "copy", "dst": "target_table"}]}
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=plan,
        )
        assert "target_table" in dot

    def test_light_theme(self):
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="light",
            rankdir="TB",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "rankdir=TB" in dot
        assert "#ffffff" in dot  # light theme bg

    def test_empty_contract_minimal(self):
        contract = {"id": "bare"}
        dot = _build_contract_dot(
            contract,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan=None,
        )
        assert "product_bare" in dot

    def test_plan_linked_to_exposes(self):
        """Last plan action should link to expose nodes."""
        dot = _build_contract_dot(
            MINIMAL_CONTRACT,
            theme="dark",
            rankdir="LR",
            title=None,
            legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            plan={"actions": [{"op": "run"}]},
        )
        # expose node should be linked from last action
        assert "action_0_run" in dot
        assert "expose_out_orders" in dot


# ── _validate_input_file ──────────────────────────────────────────────────────


class TestValidateInputFile:
    def test_valid_file_returns_path(self, tmp_path):
        f = tmp_path / "contract.yaml"
        f.write_text("id: test")
        with patch("fluid_build.cli.viz_graph.validate_input_file", return_value=f):
            result = _validate_input_file(str(f))
        assert result == f

    def test_not_found_raises_file_not_found(self, tmp_path):
        missing = str(tmp_path / "missing.yaml")
        with patch(
            "fluid_build.cli.viz_graph.validate_input_file",
            side_effect=Exception("File not found: missing"),
        ):
            with pytest.raises(FileNotFoundError):
                _validate_input_file(missing)

    def test_permission_error_raised(self, tmp_path):
        f = tmp_path / "restricted.yaml"
        f.write_text("id: x")
        with patch(
            "fluid_build.cli.viz_graph.validate_input_file",
            side_effect=Exception("permission denied"),
        ):
            with pytest.raises(PermissionError):
                _validate_input_file(str(f))

    def test_extension_error_raises_value_error(self, tmp_path):
        f = tmp_path / "bad.bin"
        f.write_bytes(b"\x00")
        with patch(
            "fluid_build.cli.viz_graph.validate_input_file",
            side_effect=Exception("invalid extension for path"),
        ):
            with pytest.raises(ValueError):
                _validate_input_file(str(f))

    def test_generic_error_raises_value_error(self, tmp_path):
        f = tmp_path / "contract.yaml"
        f.write_text("id: x")
        with patch(
            "fluid_build.cli.viz_graph.validate_input_file",
            side_effect=Exception("something else went wrong"),
        ):
            with pytest.raises(ValueError):
                _validate_input_file(str(f))


# ── _prepare_output_directory ─────────────────────────────────────────────────


class TestPrepareOutputDirectory:
    def test_returns_path_when_new_file(self, tmp_path):
        out = tmp_path / "out" / "graph.svg"
        with patch("fluid_build.cli.viz_graph.validate_output_file", return_value=out):
            result = _prepare_output_directory(str(out), force_overwrite=False)
        assert result == out

    def test_raises_file_exists_when_not_forced(self, tmp_path):
        out = tmp_path / "graph.svg"
        out.write_text("existing")
        with patch("fluid_build.cli.viz_graph.validate_output_file", return_value=out):
            with pytest.raises(ValueError, match="already exists"):
                _prepare_output_directory(str(out), force_overwrite=False)

    def test_allows_overwrite_when_forced(self, tmp_path):
        out = tmp_path / "graph.svg"
        out.write_text("existing")
        with patch("fluid_build.cli.viz_graph.validate_output_file", return_value=out):
            result = _prepare_output_directory(str(out), force_overwrite=True)
        assert result == out

    def test_permission_error_propagated(self, tmp_path):
        out = tmp_path / "graph.svg"
        with patch(
            "fluid_build.cli.viz_graph.validate_output_file",
            side_effect=Exception("permission denied writing"),
        ):
            with pytest.raises(PermissionError):
                _prepare_output_directory(str(out))

    def test_forbidden_path_raises_value_error(self, tmp_path):
        out = tmp_path / "graph.svg"
        with patch(
            "fluid_build.cli.viz_graph.validate_output_file",
            side_effect=Exception("forbidden path detected"),
        ):
            with pytest.raises(ValueError):
                _prepare_output_directory(str(out))


# ── _check_graphviz_installation ──────────────────────────────────────────────


class TestCheckGraphvizInstallation:
    def test_returns_false_when_dot_not_found(self):
        with patch("fluid_build.cli.viz_graph.shutil.which", return_value=None):
            available, version = _check_graphviz_installation()
        assert available is False
        assert version is None

    def test_returns_true_with_version_when_available(self):
        mock_result = Mock()
        mock_result.stderr = "dot - graphviz version 2.50.0"
        with patch("fluid_build.cli.viz_graph.shutil.which", return_value="/usr/bin/dot"):
            with patch("fluid_build.cli.viz_graph.subprocess.run", return_value=mock_result):
                available, version = _check_graphviz_installation()
        assert available is True
        assert "graphviz" in version.lower()

    def test_returns_true_unknown_version_when_no_stderr(self):
        mock_result = Mock()
        mock_result.stderr = ""
        with patch("fluid_build.cli.viz_graph.shutil.which", return_value="/usr/bin/dot"):
            with patch("fluid_build.cli.viz_graph.subprocess.run", return_value=mock_result):
                available, version = _check_graphviz_installation()
        assert available is True
        assert version == "Unknown version"

    def test_returns_false_on_exception(self):
        with patch("fluid_build.cli.viz_graph.shutil.which", return_value="/usr/bin/dot"):
            with patch(
                "fluid_build.cli.viz_graph.subprocess.run",
                side_effect=OSError("exec failed"),
            ):
                available, version = _check_graphviz_installation()
        assert available is False
        assert version is None


# ── _shell_open ───────────────────────────────────────────────────────────────


class TestShellOpen:
    def test_invalid_file_logs_warning(self, tmp_path):
        missing = tmp_path / "missing.svg"
        test_logger = MagicMock()
        with patch(
            "fluid_build.cli.viz_graph.validate_input_file",
            side_effect=Exception("not found"),
        ):
            _shell_open(missing, test_logger)
        # Should warn, not raise
        test_logger.warning.assert_called()  # logs a warning on error

    def test_opens_on_darwin(self, tmp_path):
        f = tmp_path / "graph.svg"
        f.write_text("<svg/>")
        test_logger = logging.getLogger("test")
        mock_result = Mock()
        mock_result.returncode = 0
        with patch("fluid_build.cli.viz_graph.validate_input_file", return_value=f):
            with patch("fluid_build.cli.viz_graph.platform.system", return_value="Darwin"):
                with patch("fluid_build.cli.viz_graph.subprocess.run", return_value=mock_result):
                    _shell_open(f, test_logger)

    def test_opens_on_linux(self, tmp_path):
        f = tmp_path / "graph.svg"
        f.write_text("<svg/>")
        test_logger = logging.getLogger("test")
        mock_result = Mock()
        mock_result.returncode = 0
        with patch("fluid_build.cli.viz_graph.validate_input_file", return_value=f):
            with patch("fluid_build.cli.viz_graph.platform.system", return_value="Linux"):
                with patch("fluid_build.cli.viz_graph.subprocess.run", return_value=mock_result):
                    _shell_open(f, test_logger)

    def test_timeout_does_not_raise(self, tmp_path):
        f = tmp_path / "graph.svg"
        f.write_text("<svg/>")
        test_logger = MagicMock()
        with patch("fluid_build.cli.viz_graph.validate_input_file", return_value=f):
            with patch("fluid_build.cli.viz_graph.platform.system", return_value="Linux"):
                with patch(
                    "fluid_build.cli.viz_graph.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["xdg-open"], 10),
                ):
                    _shell_open(f, test_logger)  # should not raise

    def test_called_process_error_does_not_raise(self, tmp_path):
        f = tmp_path / "graph.svg"
        f.write_text("<svg/>")
        test_logger = MagicMock()
        with patch("fluid_build.cli.viz_graph.validate_input_file", return_value=f):
            with patch("fluid_build.cli.viz_graph.platform.system", return_value="Linux"):
                with patch(
                    "fluid_build.cli.viz_graph.subprocess.run",
                    side_effect=subprocess.CalledProcessError(1, "xdg-open"),
                ):
                    _shell_open(f, test_logger)  # should not raise


# ── _get_file_size ────────────────────────────────────────────────────────────


class TestGetFileSize:
    def test_returns_size_for_existing_file(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello")
        size = _get_file_size(f)
        assert size == 5

    def test_returns_none_for_missing_file(self, tmp_path):
        missing = tmp_path / "ghost.txt"
        assert _get_file_size(missing) is None

    def test_accepts_string_path(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_bytes(b"abc")
        assert _get_file_size(str(f)) == 3


# ── _read_plan ────────────────────────────────────────────────────────────────


class TestReadPlan:
    def test_returns_none_when_no_path(self):
        assert _read_plan(None) is None

    def test_parses_valid_json(self, tmp_path):
        plan_file = tmp_path / "plan.json"
        plan_data = {"actions": [{"op": "create"}]}
        plan_file.write_text(json.dumps(plan_data))
        with patch(
            "fluid_build.cli.viz_graph.read_file_secure",
            return_value=json.dumps(plan_data),
        ):
            result = _read_plan(str(plan_file))
        assert result == plan_data

    def test_returns_none_on_error(self, tmp_path):
        with patch(
            "fluid_build.cli.viz_graph.read_file_secure",
            side_effect=Exception("read error"),
        ):
            result = _read_plan("/nonexistent/plan.json")
        assert result is None


# ── _write_output ─────────────────────────────────────────────────────────────


class TestWriteOutput:
    def _make_config(self, tmp_path, fmt="dot", force=True, quiet=True, open_done=False):
        out = tmp_path / f"graph.{fmt}"
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text("id: x")
        with patch.object(GraphConfig, "validate"):
            cfg = GraphConfig.__new__(GraphConfig)
            cfg.contract_path = str(contract_file)
            cfg.output_path = str(out)
            cfg.format = fmt
            cfg.environment = None
            cfg.plan_path = None
            cfg.theme = "dark"
            cfg.rankdir = "LR"
            cfg.title = None
            cfg.show_legend = False
            cfg.collapse_consumes = False
            cfg.collapse_exposes = False
            cfg.show_metadata = True
            cfg.show_descriptions = False
            cfg.max_label_length = 50
            cfg.open_when_done = open_done
            cfg.force_overwrite = force
            cfg.quiet = quiet
            cfg.custom_theme_path = None
            cfg.graphviz_args = []
        return cfg

    def test_writes_dot_file_directly(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="dot")
        metrics = GraphMetrics()
        dot = "digraph G { A -> B }"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            return_value=tmp_path / "graph.dot",
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(True, "2.50"),
            ):
                with patch("fluid_build.cli.security.write_file_secure") as mock_write:
                    _write_output(dot, cfg, metrics, logger)
        mock_write.assert_called_once()

    def test_falls_back_to_dot_when_graphviz_unavailable(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="svg")
        metrics = GraphMetrics()
        dot = "digraph G { A -> B }"
        out_path = tmp_path / "graph.svg"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            return_value=out_path,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(False, None),
            ):
                with patch("fluid_build.cli.security.write_file_secure") as mock_write:
                    _write_output(dot, cfg, metrics, logger)
        mock_write.assert_called_once()

    def test_raises_cli_error_on_output_prepare_failure(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="dot")
        metrics = GraphMetrics()
        dot = "digraph G {}"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            side_effect=ValueError("bad path"),
        ):
            with pytest.raises(CLIError) as exc_info:
                _write_output(dot, cfg, metrics, logger)
        assert exc_info.value.exit_code == 1

    def test_graphviz_timeout_raises_cli_error(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="svg")
        metrics = GraphMetrics()
        dot = "digraph G {}"
        out_path = tmp_path / "graph.svg"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            return_value=out_path,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(True, "2.50"),
            ):
                with patch(
                    "fluid_build.cli.viz_graph.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["dot"], 30),
                ):
                    with pytest.raises(CLIError):
                        _write_output(dot, cfg, metrics, logger)

    def test_graphviz_called_process_error_writes_dot_fallback(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="svg")
        metrics = GraphMetrics()
        dot = "digraph G {}"
        out_path = tmp_path / "graph.svg"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            return_value=out_path,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(True, "2.50"),
            ):
                err = subprocess.CalledProcessError(1, "dot", stderr="error")
                with patch("fluid_build.cli.viz_graph.subprocess.run", side_effect=err):
                    with patch("fluid_build.cli.security.write_file_secure") as mock_write:
                        _write_output(dot, cfg, metrics, logger)
        mock_write.assert_called_once()

    def test_opens_when_done(self, tmp_path):
        cfg = self._make_config(tmp_path, fmt="dot", open_done=True)
        metrics = GraphMetrics()
        dot = "digraph G {}"
        out_path = tmp_path / "graph.dot"

        with patch(
            "fluid_build.cli.viz_graph._prepare_output_directory",
            return_value=out_path,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(True, "2.50"),
            ):
                with patch("fluid_build.cli.security.write_file_secure"):
                    with patch("fluid_build.cli.viz_graph._shell_open") as mock_open:
                        _write_output(dot, cfg, metrics, logger)
        mock_open.assert_called_once()


# ── _create_html_wrapper ──────────────────────────────────────────────────────


class TestCreateHtmlWrapper:
    def _make_config_stub(self, show_metadata=True, theme="dark", rankdir="LR"):
        cfg = Mock()
        cfg.show_metadata = show_metadata
        cfg.theme = theme
        cfg.rankdir = rankdir
        return cfg

    def test_returns_valid_html(self):
        cfg = self._make_config_stub()
        metrics = GraphMetrics()
        metrics.mark_load_complete()
        metrics.mark_render_complete()
        html = _create_html_wrapper("<svg/>", cfg, metrics)
        assert "<!DOCTYPE html>" in html
        assert "<svg/>" in html

    def test_metadata_section_included(self):
        cfg = self._make_config_stub(show_metadata=True)
        metrics = GraphMetrics()
        metrics.node_count = 4
        metrics.edge_count = 3
        html = _create_html_wrapper("<svg/>", cfg, metrics)
        assert "Generation Info" in html
        assert "Nodes" in html

    def test_no_metadata_section_when_disabled(self):
        cfg = self._make_config_stub(show_metadata=False)
        metrics = GraphMetrics()
        html = _create_html_wrapper("<svg/>", cfg, metrics)
        assert "Generation Info" not in html

    def test_theme_colors_in_html(self):
        cfg = self._make_config_stub(theme="dark")
        metrics = GraphMetrics()
        html = _create_html_wrapper("<svg/>", cfg, metrics)
        assert "#0B1020" in html  # dark bg


# ── GraphConfig validation ────────────────────────────────────────────────────


class TestGraphConfigValidation:
    def test_invalid_format_raises(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        with pytest.raises(ValueError, match="Invalid format"):
            GraphConfig(contract_path=str(f), format="pdf")

    def test_invalid_theme_raises(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        with pytest.raises(ValueError, match="Invalid theme"):
            GraphConfig(contract_path=str(f), theme="neon")

    def test_invalid_rankdir_raises(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        with pytest.raises(ValueError, match="Invalid rankdir"):
            GraphConfig(contract_path=str(f), rankdir="XY")

    def test_missing_contract_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            GraphConfig(contract_path=str(tmp_path / "missing.yaml"))

    def test_max_label_length_too_small_raises(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        with pytest.raises(ValueError, match="max_label_length"):
            GraphConfig(contract_path=str(f), max_label_length=5)

    def test_valid_config_created(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        cfg = GraphConfig(contract_path=str(f))
        assert cfg.format == "svg"
        assert cfg.theme == "dark"


# ── GraphBuilder ──────────────────────────────────────────────────────────────


class TestGraphBuilder:
    def _make_builder(self, tmp_path, **kwargs):
        f = tmp_path / "c.yaml"
        f.write_text("id: x")
        cfg = GraphConfig(contract_path=str(f), **kwargs)
        metrics = GraphMetrics()
        return GraphBuilder(cfg, metrics, logger)

    def test_build_dot_minimal(self, tmp_path):
        builder = self._make_builder(tmp_path)
        dot = builder.build_dot(MINIMAL_CONTRACT)
        assert "digraph G {" in dot
        assert "product_test_product" in dot

    def test_build_dot_with_plan(self, tmp_path):
        builder = self._make_builder(tmp_path)
        dot = builder.build_dot(MINIMAL_CONTRACT, plan=MINIMAL_PLAN)
        assert "cluster_plan" in dot
        assert "action_0_create_table" in dot

    def test_metrics_updated_after_build(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder.build_dot(MINIMAL_CONTRACT)
        assert builder.metrics.node_count >= 1
        assert builder.metrics.cluster_count >= 1

    def test_collapse_consumes_option(self, tmp_path):
        builder = self._make_builder(tmp_path, collapse_consumes=True)
        dot = builder.build_dot(MINIMAL_CONTRACT)
        assert "consumes_agg" in dot

    def test_collapse_exposes_option(self, tmp_path):
        builder = self._make_builder(tmp_path, collapse_exposes=True)
        dot = builder.build_dot(MINIMAL_CONTRACT)
        assert "exposes_agg" in dot

    def test_show_descriptions(self, tmp_path):
        builder = self._make_builder(tmp_path, show_descriptions=True)
        contract = {
            "id": "p1",
            "consumes": [{"id": "src", "ref": "raw", "description": "raw source data"}],
            "exposes": [],
        }
        dot = builder.build_dot(contract)
        assert "raw source data" in dot

    def test_no_consumes_no_cluster(self, tmp_path):
        builder = self._make_builder(tmp_path)
        contract = {"id": "p1", "exposes": []}
        dot = builder.build_dot(contract)
        assert "cluster_consumes" not in dot

    def test_no_exposes_no_cluster(self, tmp_path):
        builder = self._make_builder(tmp_path)
        contract = {"id": "p1", "consumes": []}
        dot = builder.build_dot(contract)
        assert "cluster_exposes" not in dot


# ── run() ─────────────────────────────────────────────────────────────────────


class TestRun:
    def _make_args(self, tmp_path, **overrides):
        contract = tmp_path / "contract.yaml"
        contract.write_text("id: test_product\nname: Test\ndomain: eng\n")
        out = tmp_path / "out.dot"
        ns = argparse.Namespace(
            contract=str(contract),
            output_path=str(out),
            format="dot",
            env=None,
            plan=None,
            theme="dark",
            rankdir="LR",
            title=None,
            show_legend=False,
            collapse_consumes=False,
            collapse_exposes=False,
            hide_metadata=False,
            show_descriptions=False,
            max_label_length=50,
            open_when_done=False,
            force_overwrite=True,
            quiet=True,
            custom_theme_path=None,
            graphviz_args=[],
            debug=False,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def test_run_returns_zero_on_success(self, tmp_path):
        args = self._make_args(tmp_path)
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch("fluid_build.cli.viz_graph._write_output"):
                result = run(args, logger)
        assert result == 0

    def test_run_file_not_found_raises_cli_error(self, tmp_path):
        args = self._make_args(tmp_path)
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            side_effect=FileNotFoundError("contract not found"),
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value.exit_code == 2

    def test_run_value_error_raises_cli_error(self, tmp_path):
        args = self._make_args(tmp_path)
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch(
                "fluid_build.cli.viz_graph._write_output",
                side_effect=ValueError("bad format"),
            ):
                with pytest.raises(CLIError) as exc_info:
                    run(args, logger)
        assert exc_info.value.exit_code == 2

    def test_run_unexpected_error_raises_cli_error(self, tmp_path):
        args = self._make_args(tmp_path)
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value.exit_code == 1

    def test_run_with_debug_saves_files(self, tmp_path):
        args = self._make_args(tmp_path, debug=True)
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch("fluid_build.cli.viz_graph._write_output"):
                result = run(args, logger)
        assert result == 0
        debug_dir = tmp_path / "debug"
        assert debug_dir.exists()
        dot_files = list(debug_dir.glob("*.dot"))
        assert len(dot_files) == 1

    def test_run_with_plan_path(self, tmp_path):
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(json.dumps(MINIMAL_PLAN))
        args = self._make_args(tmp_path, plan=str(plan_file))
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch("fluid_build.cli.viz_graph._read_plan", return_value=MINIMAL_PLAN):
                with patch("fluid_build.cli.viz_graph._write_output"):
                    result = run(args, logger)
        assert result == 0

    def test_run_graphviz_not_available_warns(self, tmp_path):
        args = self._make_args(tmp_path, format="svg")
        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(False, None),
            ):
                with patch("fluid_build.cli.viz_graph._write_output"):
                    result = run(args, logger)
        assert result == 0


# ── _run_provider_actions_viz ─────────────────────────────────────────────────


class TestRunProviderActionsViz:
    """Tests for the _run_provider_actions_viz function."""

    def _make_args(self, tmp_path, fmt="svg"):
        contract = tmp_path / "contract.yaml"
        contract.write_text("id: test\n")
        return argparse.Namespace(
            contract=str(contract),
            env=None,
            format=fmt,
            output_path=str(tmp_path / f"out.{fmt}"),
            open_when_done=False,
        )

    def test_import_error_returns_one(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path)

        # Temporarily remove the module so the relative import raises ImportError
        module_key = "fluid_build.cli.viz_provider_actions"
        saved = sys.modules.pop(module_key, None)
        try:
            with patch(
                "fluid_build.cli.viz_graph.load_contract_with_overlay",
                return_value=MINIMAL_CONTRACT,
            ):
                with patch.dict("sys.modules", {module_key: None}):
                    result = _run_provider_actions_viz(args, logger)
        finally:
            if saved is not None:
                sys.modules[module_key] = saved
        assert result == 1

    def test_no_provider_actions_returns_one(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path)
        mock_pav = MagicMock()
        mock_pav.add_provider_actions_to_viz.return_value = None
        mock_pav.visualize_provider_actions_dot.return_value = "digraph {}"
        mock_pav.visualize_provider_actions_html.return_value = "<html/>"

        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch.dict(
                "sys.modules",
                {"fluid_build.cli.viz_provider_actions": mock_pav},
            ):
                result = _run_provider_actions_viz(args, logger)
        assert result == 1

    def test_dot_format_writes_dot_file(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path, fmt="dot")
        mock_pav = MagicMock()
        mock_pav.add_provider_actions_to_viz.return_value = (
            ["action1"],
            {"action1": []},
        )
        mock_pav.visualize_provider_actions_dot.return_value = "digraph {}"
        mock_pav.visualize_provider_actions_html.return_value = "<html/>"

        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch.dict(
                "sys.modules",
                {"fluid_build.cli.viz_provider_actions": mock_pav},
            ):
                result = _run_provider_actions_viz(args, logger)
        assert result == 0

    def test_html_format_writes_html_file(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path, fmt="html")
        mock_pav = MagicMock()
        mock_pav.add_provider_actions_to_viz.return_value = (
            ["action1"],
            {"action1": []},
        )
        mock_pav.visualize_provider_actions_dot.return_value = "digraph {}"
        mock_pav.visualize_provider_actions_html.return_value = "<html/>"

        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch.dict(
                "sys.modules",
                {"fluid_build.cli.viz_provider_actions": mock_pav},
            ):
                result = _run_provider_actions_viz(args, logger)
        assert result == 0

    def test_svg_format_with_graphviz_available(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path, fmt="svg")
        mock_pav = MagicMock()
        mock_pav.add_provider_actions_to_viz.return_value = (
            ["action1"],
            {"action1": []},
        )
        mock_pav.visualize_provider_actions_dot.return_value = "digraph {}"

        mock_run = Mock()
        mock_run.returncode = 0

        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(True, "2.50"),
            ):
                with patch("fluid_build.cli.viz_graph.subprocess.run", return_value=mock_run):
                    with patch.dict(
                        "sys.modules",
                        {"fluid_build.cli.viz_provider_actions": mock_pav},
                    ):
                        result = _run_provider_actions_viz(args, logger)
        assert result == 0

    def test_svg_format_graphviz_not_available_writes_dot(self, tmp_path):
        from fluid_build.cli.viz_graph import _run_provider_actions_viz

        args = self._make_args(tmp_path, fmt="svg")
        mock_pav = MagicMock()
        mock_pav.add_provider_actions_to_viz.return_value = (
            ["action1"],
            {"action1": []},
        )
        mock_pav.visualize_provider_actions_dot.return_value = "digraph {}"

        with patch(
            "fluid_build.cli.viz_graph.load_contract_with_overlay",
            return_value=MINIMAL_CONTRACT,
        ):
            with patch(
                "fluid_build.cli.viz_graph._check_graphviz_installation",
                return_value=(False, None),
            ):
                with patch.dict(
                    "sys.modules",
                    {"fluid_build.cli.viz_provider_actions": mock_pav},
                ):
                    result = _run_provider_actions_viz(args, logger)
        assert result == 1
