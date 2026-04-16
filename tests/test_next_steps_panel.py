# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.next_steps — post-success hint printer."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.next_steps import NEXT_STEPS, NextStep, print_next_steps


class TestNextStepsCatalog:
    def test_all_contexts_defined(self):
        for ctx in ("init", "forge", "forge-fragments", "demo", "bundle"):
            assert ctx in NEXT_STEPS, f"missing next-steps catalog: {ctx}"
            assert len(NEXT_STEPS[ctx]) >= 1

    def test_each_step_has_command_and_hint(self):
        for ctx, steps in NEXT_STEPS.items():
            for step in steps:
                assert isinstance(step, NextStep)
                assert step.command
                assert step.hint
                assert step.command.startswith("fluid ") or step.command.startswith("cat ")

    def test_init_catalog_points_at_status_first(self):
        """First step for init should be fluid status — the discoverability fix."""
        assert NEXT_STEPS["init"][0].command == "fluid status"

    def test_forge_catalog_points_at_status_first(self):
        assert NEXT_STEPS["forge"][0].command == "fluid status"

    def test_fragment_catalog_includes_bundle_check(self):
        commands = [step.command for step in NEXT_STEPS["forge-fragments"]]
        assert any("bundle --check" in cmd for cmd in commands)


class TestPrintNextStepsRendering:
    def test_no_console_falls_through_to_plain(self, capsys):
        print_next_steps("init", console=None)
        captured = capsys.readouterr()
        assert "Next steps" in captured.out
        assert "fluid status" in captured.out
        assert "fluid validate" in captured.out

    def test_with_console_prints_rich_panel(self):
        mock_console = MagicMock()
        print_next_steps("forge", console=mock_console)
        # Panel rendering calls console.print multiple times
        assert mock_console.print.called

    def test_unknown_context_is_a_noop(self, capsys):
        print_next_steps("nonexistent", console=None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestPrintNextStepsQuietMode:
    def test_quiet_args_suppresses_output(self, capsys):
        args = Namespace(quiet=True)
        print_next_steps("init", console=None, args=args)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_non_quiet_args_shows_output(self, capsys):
        args = Namespace(quiet=False)
        print_next_steps("init", console=None, args=args)
        captured = capsys.readouterr()
        assert "Next steps" in captured.out

    def test_explicit_quiet_parameter_wins(self, capsys):
        args = Namespace(quiet=False)
        print_next_steps("init", console=None, args=args, quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_args_without_quiet_attr_is_not_quiet(self, capsys):
        args = Namespace()
        print_next_steps("init", console=None, args=args)
        captured = capsys.readouterr()
        assert "Next steps" in captured.out


class TestPrintNextStepsSafety:
    def test_broken_console_never_raises(self):
        broken = MagicMock()
        broken.print.side_effect = RuntimeError("console dead")
        # Must not raise
        print_next_steps("init", console=broken)

    def test_all_contexts_are_printable(self, capsys):
        for ctx in NEXT_STEPS:
            print_next_steps(ctx, console=None)
        # No exceptions raised — good.  Output is cumulative but we
        # don't care about exact shape for this test.
        captured = capsys.readouterr()
        assert "Next steps" in captured.out
