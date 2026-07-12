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

"""Behaviour-preservation tests for the shared forge mode error boundary.

Two layers:

* Unit tests for :func:`forge_mode_error_boundary` that pin the
  exception -> exit-code mapping in isolation (Ctrl-C -> 130, any other
  ``Exception`` -> 1, ``SystemExit`` / no-``cancel_label`` ``KeyboardInterrupt``
  propagate, clean exit leaves ``exit_code`` untouched).

* Characterization tests that drive each real ``run_*_mode`` handler into
  every branch its *pre-refactor* try/except owned and assert the exit
  code, the ``logger`` label, and the **byte-identical** console message.
  The golden strings below are copied verbatim from the handlers as they
  stood before the boundary extraction, so any drift fails loudly.
"""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli._forge_mode_errors import (
    ForgeModeErrorBoundary,
    forge_mode_error_boundary,
)
from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
from fluid_build.cli.forge_modes import (
    run_ai_copilot_mode,
    run_domain_agent_mode,
    run_guided_mode,
)

# Golden console strings — verbatim from the pre-refactor except blocks.
GOLDEN_AI_GENERIC = "[red]AI Copilot failed: boom[/red]"
GOLDEN_AI_COPILOT_ERR = "[red]❌ AI Copilot failed: friendly message[/red]"
GOLDEN_AI_TIP = (
    "[yellow]Tip: Run 'fluid ai setup' to configure your LLM provider,[/yellow]\n"
    "[yellow]or use 'fluid forge --blank' / guided mode without AI.[/yellow]"
)
GOLDEN_DOMAIN_GENERIC = "[red]❌ Domain agent failed: boom[/red]"
GOLDEN_GUIDED_GENERIC = "[red]Guided mode failed: boom[/red]"


def _printed(console: MagicMock) -> str:
    """Concatenate every positional arg passed to ``console.print``."""
    return "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)


# ── unit: the boundary in isolation ───────────────────────────────────


class TestForgeModeErrorBoundary:
    def test_keyboard_interrupt_maps_to_130_and_logs_cancel(self):
        logger = MagicMock()
        console = MagicMock()
        rendered = []

        def handler() -> int:
            with forge_mode_error_boundary(
                logger,
                console,
                fail_label="X failed",
                render_error=lambda c, e: rendered.append(e),
                cancel_label="X cancelled",
            ) as boundary:
                raise KeyboardInterrupt()
            return boundary.exit_code

        assert handler() == 130
        logger.info.assert_called_once_with("X cancelled")
        logger.exception.assert_not_called()
        # Ctrl-C never routes through the failure renderer.
        assert rendered == []

    def test_keyboard_interrupt_propagates_without_cancel_label(self):
        logger = MagicMock()

        def handler() -> int:
            with forge_mode_error_boundary(
                logger,
                None,
                fail_label="X failed",
                render_error=lambda c, e: None,
            ) as boundary:
                raise KeyboardInterrupt()
            return boundary.exit_code

        with pytest.raises(KeyboardInterrupt):
            handler()
        logger.info.assert_not_called()

    def test_exception_maps_to_1_logs_and_renders(self):
        logger = MagicMock()
        console = MagicMock()
        seen = {}
        exc = ValueError("boom")

        def render(c, e):
            seen["console"] = c
            seen["exc"] = e

        def handler() -> int:
            with forge_mode_error_boundary(
                logger,
                console,
                fail_label="X failed",
                render_error=render,
                cancel_label="X cancelled",
            ) as boundary:
                raise exc
            return boundary.exit_code

        assert handler() == 1
        logger.exception.assert_called_once_with("X failed")
        logger.info.assert_not_called()
        assert seen["console"] is console
        assert seen["exc"] is exc

    def test_system_exit_propagates(self):
        logger = MagicMock()

        def handler() -> int:
            with forge_mode_error_boundary(
                logger,
                None,
                fail_label="X failed",
                render_error=lambda c, e: None,
                cancel_label="X cancelled",
            ) as boundary:
                raise SystemExit(2)
            return boundary.exit_code

        with pytest.raises(SystemExit):
            handler()
        logger.exception.assert_not_called()

    def test_clean_exit_leaves_exit_code_none(self):
        logger = MagicMock()
        captured = {}

        def handler() -> int:
            with forge_mode_error_boundary(
                logger,
                None,
                fail_label="X failed",
                render_error=lambda c, e: None,
                cancel_label="X cancelled",
            ) as boundary:
                captured["boundary"] = boundary
                return 0
            return boundary.exit_code  # pragma: no cover - unreachable

        assert handler() == 0
        assert captured["boundary"].exit_code is None
        logger.exception.assert_not_called()
        logger.info.assert_not_called()

    def test_factory_returns_boundary_instance(self):
        boundary = forge_mode_error_boundary(
            MagicMock(),
            None,
            fail_label="X",
            render_error=lambda c, e: None,
        )
        assert isinstance(boundary, ForgeModeErrorBoundary)
        assert boundary.exit_code is None


# ── characterization: run_guided_mode ─────────────────────────────────


class TestGuidedModeErrorParity:
    def _args(self):
        return argparse.Namespace(non_interactive=True, dry_run=False)

    def test_exception_returns_1_with_golden_message(self):
        console = MagicMock()
        logger = MagicMock()
        with patch(
            "fluid_build.cli.forge_modes._guided_non_interactive_defaults",
            side_effect=RuntimeError("boom"),
        ):
            result = run_guided_mode(
                self._args(),
                logger,
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/p")),
                console_factory=lambda: console,
            )
        assert result == 1
        logger.exception.assert_any_call("Guided mode failed")
        assert GOLDEN_GUIDED_GENERIC in _printed(console)

    def test_keyboard_interrupt_returns_130(self):
        console = MagicMock()
        logger = MagicMock()
        with patch(
            "fluid_build.cli.forge_modes._guided_non_interactive_defaults",
            side_effect=KeyboardInterrupt(),
        ):
            result = run_guided_mode(
                self._args(),
                logger,
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/p")),
                console_factory=lambda: console,
            )
        assert result == 130
        logger.info.assert_any_call("Guided mode cancelled")


# ── characterization: run_domain_agent_mode ───────────────────────────


class TestDomainAgentModeErrorParity:
    def _args(self):
        return argparse.Namespace(
            non_interactive=True, dry_run=False, agent="finance", context=None
        )

    def _kwargs(self, ai_agents, console):
        return dict(
            ai_agents=ai_agents,
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(return_value={}),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/p")),
            context_error_cls=Exception,
            console_factory=lambda: console,
        )

    def test_exception_returns_1_with_golden_message(self):
        console = MagicMock()
        logger = MagicMock()
        ai_agents = {"finance": MagicMock(side_effect=RuntimeError("boom"))}
        result = run_domain_agent_mode(self._args(), logger, **self._kwargs(ai_agents, console))
        assert result == 1
        logger.exception.assert_any_call("Domain agent mode failed")
        assert GOLDEN_DOMAIN_GENERIC in _printed(console)

    def test_keyboard_interrupt_propagates(self):
        # Pre-refactor the domain handler had no ``except KeyboardInterrupt``;
        # the interrupt must still propagate (no cancel_label on its boundary).
        console = MagicMock()
        logger = MagicMock()
        ai_agents = {"finance": MagicMock(side_effect=KeyboardInterrupt())}
        with pytest.raises(KeyboardInterrupt):
            run_domain_agent_mode(self._args(), logger, **self._kwargs(ai_agents, console))


# ── characterization: run_ai_copilot_mode ─────────────────────────────


class TestAiCopilotModeErrorParity:
    def _args(self):
        return argparse.Namespace(non_interactive=True, dry_run=False)

    def _kwargs(self, copilot_class, console):
        return dict(
            copilot_class=copilot_class,
            get_cli_arg_fn=lambda a, name, default=None: default,
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/p")),
            context_error_cls=Exception,
            build_interview_summary_fn=MagicMock(return_value={}),
            console_factory=lambda: console,
        )

    def test_generic_exception_returns_1_with_golden_message(self):
        console = MagicMock()
        logger = MagicMock()
        copilot_class = MagicMock(side_effect=RuntimeError("boom"))
        result = run_ai_copilot_mode(self._args(), logger, **self._kwargs(copilot_class, console))
        assert result == 1
        logger.exception.assert_any_call("AI Copilot mode failed")
        printed = _printed(console)
        assert GOLDEN_AI_GENERIC in printed
        # No api-key hint for a non-key error.
        assert "fluid ai setup" not in printed

    def test_key_error_exception_prints_setup_tip(self):
        console = MagicMock()
        logger = MagicMock()
        copilot_class = MagicMock(side_effect=RuntimeError("missing api_key for provider"))
        result = run_ai_copilot_mode(self._args(), logger, **self._kwargs(copilot_class, console))
        assert result == 1
        assert GOLDEN_AI_TIP in _printed(console)

    def test_copilot_generation_error_shows_friendly_message(self):
        console = MagicMock()
        logger = MagicMock()
        copilot_class = MagicMock(
            side_effect=CopilotGenerationError(
                "internal_error_code",
                "friendly message",
                suggestions=["try this"],
            )
        )
        result = run_ai_copilot_mode(self._args(), logger, **self._kwargs(copilot_class, console))
        assert result == 1
        printed = _printed(console)
        assert GOLDEN_AI_COPILOT_ERR in printed
        assert "[dim]• try this[/dim]" in printed
        # The internal error code must never surface to the user.
        assert "internal_error_code" not in printed

    def test_copilot_generation_error_without_console_uses_plain_fallback(self):
        logger = MagicMock()
        copilot_class = MagicMock(
            side_effect=CopilotGenerationError(
                "internal_error_code",
                "friendly message",
                suggestions=["try this"],
            )
        )
        with (
            patch("fluid_build.cli.forge_modes.console_error") as mock_err,
            patch("fluid_build.cli.forge_modes.cprint") as mock_cprint,
        ):
            result = run_ai_copilot_mode(
                self._args(),
                logger,
                **self._kwargs(copilot_class, None),
            )
        assert result == 1
        mock_err.assert_any_call("AI Copilot failed: friendly message")
        mock_cprint.assert_any_call("  • try this")

    def test_keyboard_interrupt_returns_130(self):
        console = MagicMock()
        logger = MagicMock()
        copilot_class = MagicMock(side_effect=KeyboardInterrupt())
        result = run_ai_copilot_mode(self._args(), logger, **self._kwargs(copilot_class, console))
        assert result == 130
        logger.info.assert_any_call("AI Copilot cancelled by user")
