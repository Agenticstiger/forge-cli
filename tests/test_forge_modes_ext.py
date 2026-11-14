# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli.forge_modes."""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.forge_modes import run_template_mode

# ── helpers ────────────────────────────────────────────────────────────


def _args(**kwargs):
    defaults = {
        "non_interactive": True,
        "dry_run": False,
        "agent": None,
        "context": None,
        "template": None,
        "provider": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _logger():
    return logging.getLogger("test.forge_modes")


# ── run_template_mode ─────────────────────────────────────────────────


class TestRunTemplateMode:
    def test_missing_template_returns_one(self):
        args = _args(template="nonexistent", provider="local", non_interactive=True)

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        mock_registry.list_available.return_value = ["starter", "analytics"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
                console_factory=None,
            )
        assert result == 1

    def test_dry_run_returns_zero(self, tmp_path):
        args = _args(template="starter", provider="local", dry_run=True, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(
            name="starter", description="Starter template"
        )
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template
        mock_registry.list_available.return_value = ["starter"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_successful_template_creation(self, tmp_path):
        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(
            name="starter", description="Starter template"
        )
        mock_template.generate_contract.return_value = {}
        mock_template.generate_structure.return_value = {}
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template
        mock_registry.list_available.return_value = ["starter"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_exception_returns_one(self):
        args = _args(template="starter", provider="local", non_interactive=True)

        # get_target_directory_fn raising causes generic exception handler to catch it
        result = run_template_mode(
            args,
            _logger(),
            get_target_directory_fn=MagicMock(side_effect=RuntimeError("oops")),
            console_factory=None,
        )
        assert result == 1


# ── run_template_mode – additional branches ───────────────────────────


class TestRunTemplateModeAdditional:
    """Cover lines 318-379, 382-397."""

    def test_template_creates_directory_structure(self, tmp_path):
        """Lines 365-379: generate_structure returns dir entries."""
        from fluid_build.cli.forge_modes import run_template_mode

        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(name="starter", description="A starter")
        mock_template.generate_contract.return_value = {}
        # One directory entry and one file entry
        mock_template.generate_structure.return_value = {
            "src/": None,
            "src/main.py": "print('hello')",
        }
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_template_readme_attribute_error_is_swallowed(self, tmp_path):
        """Lines 376-378: AttributeError on _create_readme is handled."""
        from fluid_build.cli.forge_modes import run_template_mode

        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(name="starter", description="Starter")
        mock_template.generate_contract.return_value = {}
        mock_template.generate_structure.return_value = {}
        mock_template._create_readme.side_effect = AttributeError("no readme")
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0


# ── print_interview_phase ──────────────────────────────────────────────


class TestPrintInterviewPhase:
    """Regression tests for the interview phase breadcrumb helper."""

    def test_prints_breadcrumb_with_rich(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        console = MagicMock()
        with patch("fluid_build.cli.forge_ui.RICH_AVAILABLE", True):
            print_interview_phase(console, phase=1, total=3, label="Understanding your project")
        console.print.assert_called()
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "Phase 1/3" in printed
        assert "Understanding your project" in printed

    def test_prints_plain_breadcrumb_without_rich(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        console = MagicMock()
        with patch("fluid_build.cli.forge_ui.RICH_AVAILABLE", False):
            print_interview_phase(console, phase=2, total=3, label="Clarifying details")
        console.print.assert_called()
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "Phase 2/3" in printed
        assert "Clarifying details" in printed

    def test_no_console_is_noop(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        # Should not raise even if console is None.
        print_interview_phase(None, phase=1, total=3, label="x")
