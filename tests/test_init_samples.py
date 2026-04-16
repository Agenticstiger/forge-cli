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

"""Tests for fluid_build.cli.init_samples."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fluid_build.cli.init_samples import init_samples, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager_mock(tmp_path: Path):
    """Return a MagicMock that mimics SampleDataManager."""
    manager = MagicMock()
    # generate_all returns a dict of name -> filepath
    dummy_file = str(tmp_path / "customers.csv")
    Path(dummy_file).write_text("id,name\n1,Alice")
    manager.generate_all.return_value = {"customers": dummy_file}

    # Individual dataset generators
    manager.customer_gen.generate.return_value = [{"customer_id": "CUST000001"}]
    manager.order_gen.generate.return_value = [{"order_id": "ORD000001"}]
    manager.event_gen.generate.return_value = [{"event_id": "EVT000001"}]
    manager.timeseries_gen.generate_metrics.return_value = [{"metric": "cpu"}]
    manager.timeseries_gen.generate_sensor_data.return_value = [{"sensor": "S1"}]
    manager._write_dataset.side_effect = lambda data, name, fmt: str(tmp_path / f"{name}.{fmt}")
    manager.get_summary.return_value = {
        "output_directory": str(tmp_path),
        "datasets": [{"name": "customers", "size_kb": 1.2}],
    }
    return manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitSamplesCommand:
    """Tests for the init_samples Click command."""

    def test_generate_all_default_options(self, tmp_path):
        """Default invocation generates all datasets and exits 0."""
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "customers.csv")
        Path(dummy).write_text("id\n1")
        manager.generate_all.return_value = {"customers": dummy}

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(init_samples, ["--output-dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        manager.generate_all.assert_called_once_with(format="csv")

    def test_quiet_flag_suppresses_output(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(init_samples, ["--output-dir", str(tmp_path), "--quiet"])

        assert result.exit_code == 0
        assert result.output == ""

    def test_json_format_option_passed_to_manager(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--format", "json"],
            )

        assert result.exit_code == 0
        manager.generate_all.assert_called_once_with(format="json")

    def test_specific_dataset_customers_only(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "customers.csv")
        Path(dummy).write_text("id\n1")
        manager._write_dataset.return_value = dummy

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--datasets", "customers"],
            )

        assert result.exit_code == 0, result.output
        manager.customer_gen.generate.assert_called_once()
        manager.generate_all.assert_not_called()

    def test_specific_dataset_events(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "events.csv")
        Path(dummy).write_text("id\n1")
        manager._write_dataset.return_value = dummy

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--datasets", "events"],
            )

        assert result.exit_code == 0, result.output
        manager.event_gen.generate.assert_called_once()

    def test_specific_dataset_metrics(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "metrics.csv")
        Path(dummy).write_text("ts,val\n1,2")
        manager._write_dataset.return_value = dummy

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--datasets", "metrics"],
            )

        assert result.exit_code == 0, result.output
        manager.timeseries_gen.generate_metrics.assert_called_once()

    def test_specific_dataset_sensor_readings(self, tmp_path):
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "sensor_readings.csv")
        Path(dummy).write_text("ts,val\n1,2")
        manager._write_dataset.return_value = dummy

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--datasets", "sensor_readings"],
            )

        assert result.exit_code == 0, result.output
        manager.timeseries_gen.generate_sensor_data.assert_called_once()

    def test_error_in_generate_exits_nonzero(self, tmp_path):
        runner = CliRunner()
        manager = MagicMock()
        manager.generate_all.side_effect = RuntimeError("disk full")

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(init_samples, ["--output-dir", str(tmp_path)])

        assert result.exit_code != 0

    def test_orders_without_customers_uses_fallback_ids(self, tmp_path):
        """Requesting orders without customers generates fallback customer IDs."""
        runner = CliRunner()
        manager = _make_manager_mock(tmp_path)
        dummy = str(tmp_path / "orders.csv")
        Path(dummy).write_text("id\n1")
        manager._write_dataset.return_value = dummy

        with patch("fluid_build.cli.init_samples.SampleDataManager", return_value=manager):
            result = runner.invoke(
                init_samples,
                ["--output-dir", str(tmp_path), "--datasets", "orders"],
            )

        assert result.exit_code == 0, result.output
        manager.order_gen.generate.assert_called_once()
        # Verify customer_ids were generated as fallback list
        call_kwargs = manager.order_gen.generate.call_args
        cids = call_kwargs[1]["customer_ids"] if call_kwargs[1] else call_kwargs[0][1]
        assert cids[0].startswith("CUST")


# ---------------------------------------------------------------------------
# Tests for register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_command_to_group(self):
        import click

        @click.group()
        def cli():
            pass

        register(cli)
        assert "init-samples" in cli.commands
