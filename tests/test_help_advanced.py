# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.help_advanced — progressive --help."""

from __future__ import annotations

import argparse
import sys
from unittest.mock import patch

from fluid_build.cli.help_advanced import (
    ADVANCED_FLAG,
    is_advanced_help_requested,
    mark_advanced,
)


class TestIsAdvancedHelpRequested:
    def test_default_argv_is_sys_argv(self):
        with patch.object(sys, "argv", ["fluid", "init", "--help"]):
            assert is_advanced_help_requested() is False
        with patch.object(sys, "argv", ["fluid", "init", "--help", "--advanced"]):
            assert is_advanced_help_requested() is True

    def test_explicit_argv_parameter(self):
        assert is_advanced_help_requested([]) is False
        assert is_advanced_help_requested(["init", "--help"]) is False
        assert is_advanced_help_requested(["init", "--advanced", "--help"]) is True

    def test_partial_match_is_not_detected(self):
        """--advanced-something should NOT match --advanced."""
        assert is_advanced_help_requested(["init", "--advanced-mode"]) is False
        assert is_advanced_help_requested(["init", "--advancedfoo"]) is False


class TestMarkAdvanced:
    def test_returns_action_for_chaining(self):
        parser = argparse.ArgumentParser()
        action = parser.add_argument("--foo", help="foo help")

        result = mark_advanced(action)
        assert result is action

    def test_hides_help_when_advanced_not_requested(self):
        with patch.object(sys, "argv", ["fluid", "init", "--help"]):
            parser = argparse.ArgumentParser()
            action = parser.add_argument("--foo", help="foo help")
            mark_advanced(action)
            assert action.help == argparse.SUPPRESS

    def test_keeps_help_when_advanced_requested(self):
        with patch.object(sys, "argv", ["fluid", "init", "--help", "--advanced"]):
            parser = argparse.ArgumentParser()
            action = parser.add_argument("--foo", help="foo help")
            mark_advanced(action)
            assert action.help == "foo help"

    def test_advanced_flag_constant(self):
        assert ADVANCED_FLAG == "--advanced"


class TestInitParserAdvancedIntegration:
    """Slice UX-D wires mark_advanced into init.py; verify the effect."""

    def test_default_help_omits_use_case_flag(self):
        """--use-case should be hidden from the default help output."""
        with patch.object(sys, "argv", ["fluid", "init", "--help"]):
            # Re-import init so mark_advanced reads the patched argv.
            import importlib
            import fluid_build.cli.init as init_module

            importlib.reload(init_module)

            parser = argparse.ArgumentParser()
            sp = parser.add_subparsers()
            init_module.register(sp)

            help_text = parser.format_help() + sp.choices["init"].format_help()
            # The advanced flag should not appear in default help.
            assert "--use-case" not in help_text
            # Visible basics should still be there.
            assert "--yes" in help_text
            assert "--dry-run" in help_text

    def test_advanced_help_includes_use_case_flag(self):
        """--use-case should appear when --advanced is in argv."""
        with patch.object(sys, "argv", ["fluid", "init", "--help", "--advanced"]):
            import importlib
            import fluid_build.cli.init as init_module

            importlib.reload(init_module)

            parser = argparse.ArgumentParser()
            sp = parser.add_subparsers()
            init_module.register(sp)

            help_text = sp.choices["init"].format_help()
            assert "--use-case" in help_text
            assert "--no-run" in help_text
            assert "--no-dag" in help_text

    def test_advanced_flag_does_not_break_parsing(self):
        """Hidden flags must still parse at the command line."""
        with patch.object(sys, "argv", ["fluid", "init", "--help"]):
            import importlib
            import fluid_build.cli.init as init_module

            importlib.reload(init_module)

            parser = argparse.ArgumentParser()
            sp = parser.add_subparsers()
            init_module.register(sp)

            # Even though --use-case is hidden, passing it should still work.
            args = parser.parse_args(
                ["init", "--use-case", "analytics", "--yes"]
            )
            assert args.use_case == "analytics"
            assert args.yes is True
