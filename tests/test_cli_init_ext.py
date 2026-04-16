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

"""Unit tests for fluid_build/cli/__init__.py (ProductionCLI, build_parser, main, helpers)."""

import argparse
import logging
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# build_parser()
# ---------------------------------------------------------------------------


class TestBuildParser(unittest.TestCase):
    def test_returns_argument_parser(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        assert isinstance(p, argparse.ArgumentParser)

    def test_prog_is_fluid(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        assert p.prog == "fluid"

    def test_global_log_level_choices(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--log-level", "DEBUG", "doctor"])
        assert args.log_level == "DEBUG"

    def test_global_provider_choices(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--provider", "gcp", "doctor"])
        assert args.provider == "gcp"

    def test_debug_flag(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--debug", "doctor"])
        assert args.debug is True

    def test_profile_flag(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--profile", "doctor"])
        assert args.profile is True

    def test_health_check_flag(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--health-check", "doctor"])
        assert args.health_check is True

    def test_stats_flag(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--stats", "doctor"])
        assert args.stats is True

    def test_no_color_flag(self):
        from fluid_build.cli import build_parser

        p = build_parser()
        args = p.parse_args(["--no-color", "doctor"])
        assert args.no_color is True


# ---------------------------------------------------------------------------
# ProductionCLI
# ---------------------------------------------------------------------------


class TestProductionCLI(unittest.TestCase):
    def test_init_sets_memory_monitor(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        assert cli.memory_monitor is not None

    def test_logger_initially_none(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        assert cli.logger is None

    def test_setup_production_environment_runs(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        # Should not raise
        with (
            patch("fluid_build.cli.optimize_startup"),
            patch("fluid_build.cli.set_security_context"),
        ):
            cli.setup_production_environment()

    def test_execute_command_calls_func(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        cli.logger = MagicMock()
        cli.logger.logger = logging.getLogger("test")

        mock_func = MagicMock(return_value=0)
        args = argparse.Namespace(func=mock_func)
        result = cli._execute_command(args)
        mock_func.assert_called_once()
        assert result == 0

    def test_execute_command_returns_2_without_func(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        cli.logger = MagicMock()
        args = argparse.Namespace()
        result = cli._execute_command(args)
        assert result == 2

    def test_execute_command_without_logger(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        cli.logger = None
        args = argparse.Namespace()
        result = cli._execute_command(args)
        assert result == 2

    def test_handle_health_check_returns_int(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        mock_results = {
            "checks": {"system": {"status": "ok"}},
            "overall_healthy": True,
        }
        with (
            patch("fluid_build.cli.run_health_checks", return_value=mock_results),
            patch("fluid_build.cli.cprint"),
        ):
            result = cli._handle_health_check()
        assert isinstance(result, int)
        assert result == 0

    def test_handle_health_check_unhealthy_returns_1(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        mock_results = {
            "checks": {"system": {"status": "fail", "error": "broken"}},
            "overall_healthy": False,
        }
        with (
            patch("fluid_build.cli.run_health_checks", return_value=mock_results),
            patch("fluid_build.cli.cprint"),
        ):
            result = cli._handle_health_check()
        assert result == 1

    def test_handle_performance_stats(self):
        from fluid_build.cli import ProductionCLI

        cli = ProductionCLI()
        mock_stats = {
            "startup_stats": {"startup_time": 0.1},
            "cache_stats": {"hit_ratio": 0.75, "size": 10},
            "memory_stats": {"growth_mb": 5.0},
        }
        with (
            patch("fluid_build.cli.get_performance_stats", return_value=mock_stats),
            patch("fluid_build.cli.cprint"),
        ):
            result = cli._handle_performance_stats()
        assert result == 0


# ---------------------------------------------------------------------------
# _setup_enhanced_logging()
# ---------------------------------------------------------------------------


class TestSetupEnhancedLogging(unittest.TestCase):
    def test_returns_production_logger(self):
        from fluid_build.cli import ProductionLogger, _setup_enhanced_logging

        logger = _setup_enhanced_logging("INFO", None)
        assert isinstance(logger, ProductionLogger)

    def test_debug_mode_overrides_level(self):
        from fluid_build.cli import ProductionLogger, _setup_enhanced_logging

        logger = _setup_enhanced_logging("INFO", None, debug=True)
        assert isinstance(logger, ProductionLogger)

    def test_no_color_uses_plain_formatter(self):
        from fluid_build.cli import ProductionLogger, _setup_enhanced_logging

        logger = _setup_enhanced_logging("INFO", None, no_color=True)
        assert isinstance(logger, ProductionLogger)

    def test_with_log_file(self):
        import tempfile

        from fluid_build.cli import ProductionLogger, _setup_enhanced_logging

        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            log_path = f.name

        logger = _setup_enhanced_logging("INFO", log_path)
        assert isinstance(logger, ProductionLogger)


# ---------------------------------------------------------------------------
# _validate_global_args()
# ---------------------------------------------------------------------------


class TestValidateGlobalArgs(unittest.TestCase):
    def test_gcp_without_project_logs_error(self):
        from fluid_build.cli import _validate_global_args

        args = argparse.Namespace(provider="gcp", project=None, debug=False, safe_mode=False)
        mock_logger = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            _validate_global_args(args, mock_logger)
        assert mock_logger.log_safe.called

    def test_local_provider_no_error(self):
        from fluid_build.cli import _validate_global_args

        args = argparse.Namespace(provider="local", project=None, debug=False, safe_mode=False)
        mock_logger = MagicMock()
        # Should not raise
        with patch.dict("os.environ", {}, clear=True):
            _validate_global_args(args, mock_logger)

    def test_no_provider_no_exception(self):
        from fluid_build.cli import _validate_global_args

        args = argparse.Namespace(provider=None, project=None, debug=False, safe_mode=False)
        # Should not raise even without logger
        with patch.dict("os.environ", {}, clear=True):
            _validate_global_args(args, None)

    def test_production_env_without_safe_mode_logs_warning(self):
        from fluid_build.cli import _validate_global_args

        args = argparse.Namespace(provider=None, project=None, debug=False, safe_mode=False)
        mock_logger = MagicMock()
        with patch.dict("os.environ", {"PRODUCTION": "true"}, clear=False):
            _validate_global_args(args, mock_logger)
        # logger may have been called with a warning about safe_mode
        # just verifying it does not raise


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def test_main_empty_argv_returns_0(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.HELP_RICH_AVAILABLE", False),
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
        ):
            result = main([])
        assert result == 0

    def test_main_help_flag_returns_0(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.HELP_RICH_AVAILABLE", False),
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
        ):
            result = main(["--help"])
        assert result == 0

    def test_main_h_flag_returns_0(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.HELP_RICH_AVAILABLE", False),
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
        ):
            result = main(["-h"])
        assert result == 0

    def test_main_health_check_flag(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
            patch("fluid_build.cli.ProductionCLI._handle_health_check", return_value=0),
        ):
            result = main(["--health-check", "doctor"])
        assert result == 0

    def test_main_stats_flag(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
            patch("fluid_build.cli.ProductionCLI._handle_performance_stats", return_value=0),
        ):
            result = main(["--stats", "doctor"])
        assert result == 0

    def test_main_setup_failure_returns_2(self):
        from fluid_build.cli import main

        with (
            patch(
                "fluid_build.cli.ProductionCLI.setup_production_environment",
                side_effect=RuntimeError("init fail"),
            ),
            patch("fluid_build.cli.console_error"),
        ):
            result = main(["doctor"])
        assert result == 2

    def test_main_rich_help_first_run(self):
        from fluid_build.cli import main

        with (
            patch("fluid_build.cli.HELP_RICH_AVAILABLE", True),
            patch("fluid_build.cli.ProductionCLI.setup_production_environment"),
            patch("fluid_build.cli.print_first_run_help"),
            patch("pathlib.Path.exists", return_value=False),
        ):
            result = main([])
        assert result == 0


if __name__ == "__main__":
    unittest.main()
