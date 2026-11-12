# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
GOLDEN_AI_GENERIC = "AI Copilot failed: boom"
GOLDEN_AI_COPILOT_ERR = "❌ AI Copilot failed: friendly message"
GOLDEN_AI_TIP = (
    "[yellow]Tip: Run 'fluid ai setup' to configure your LLM provider,[/yellow]\n"
    "[yellow]or use 'fluid forge --blank' / guided mode without AI.[/yellow]"
)
GOLDEN_DOMAIN_GENERIC = "❌ Domain agent failed: boom"
GOLDEN_GUIDED_GENERIC = "Guided mode failed: boom"


def _printed(console: MagicMock) -> str:
    """Concatenate every positional arg passed to ``console.print``."""
    return "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)


def _style_of(console: MagicMock, needle: str) -> str:
    """The Rich style of the printed ``Text`` containing ``needle``.

    The messages print as ``Text`` (not markup), so the colour is a ``Text`` style,
    not a ``[red]`` tag in the string the golden strings above compare.
    """
    for call in console.print.call_args_list:
        if call.args and needle in str(call.args[0]):
            return str(getattr(call.args[0], "style", ""))
    raise AssertionError(f"nothing printed containing {needle!r}")


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
        assert _style_of(console, GOLDEN_AI_GENERIC) == "red"
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
        assert _style_of(console, GOLDEN_AI_COPILOT_ERR) == "red"
        assert _style_of(console, "• try this") == "dim"
        assert "• try this" in printed
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
            patch("fluid_build.cli.forge_modes.cprint_literal") as mock_cprint,
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


# ── a failure is logged once, noted for --agent, described as data ────


def _raised(exc: Exception) -> Exception:
    """*exc* with a traceback, as a handler meets it."""
    try:
        raise exc
    except Exception as caught:  # noqa: BLE001
        return caught


def _fail(exc: Exception) -> None:
    """Raise *exc*; called inside a ``with`` so the code after the block stays reachable."""
    raise exc


def _handled(logger: logging.Logger, label: str, exc: Exception, **kwargs) -> None:
    """Call ``log_forge_failure`` from the ``except`` block, as handlers do."""
    from fluid_build.cli._forge_mode_errors import log_forge_failure

    try:
        raise exc
    except Exception as caught:  # noqa: BLE001
        log_forge_failure(logger, label, caught, **kwargs)


class TestLogForgeFailure:
    @pytest.fixture(autouse=True)
    def _forget_the_note(self):
        """Drop what a test noted, and the traceback frames it holds, after the test."""
        from fluid_build.cli._forge_mode_errors import take_forge_failure

        yield
        take_forge_failure()

    def test_a_fluid_error_has_no_traceback_above_debug(self, caplog):
        logger = logging.getLogger("test.forge.failure")
        exc = CopilotGenerationError("copilot_no_llm_provider", "No LLM provider configured.")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            _handled(logger, "AI Copilot minimal flow failed", exc)

        error, debug = caplog.records
        assert (error.levelno, error.getMessage()) == (
            logging.ERROR,
            "AI Copilot minimal flow failed: copilot_no_llm_provider",
        )
        assert error.exc_info is None
        # The caller prints the message; the record is for --log-file only.
        assert error.fluid_console_shown is True
        assert debug.levelno == logging.DEBUG and debug.exc_info[1] is exc

    def test_a_fluid_error_nobody_printed_stays_on_the_console(self, caplog):
        logger = logging.getLogger("test.forge.failure")
        exc = CopilotGenerationError("copilot_x", "x")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            _handled(logger, "Agent loop failed", exc, shown=False)
        assert caplog.records[0].fluid_console_shown is False

    def test_a_crash_keeps_its_traceback(self, caplog):
        logger = logging.getLogger("test.forge.failure")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            _handled(logger, "AI Copilot minimal flow crashed", KeyError("boom"))

        (record,) = caplog.records
        assert record.levelno == logging.ERROR
        assert isinstance(record.exc_info[1], KeyError)

    def test_the_failure_is_noted_for_the_run(self):
        from fluid_build.cli._forge_mode_errors import take_forge_failure

        exc = ValueError("boom")
        _handled(MagicMock(), "X failed", exc)
        assert take_forge_failure() is exc
        assert take_forge_failure() is None  # read once

    def test_the_boundary_logs_a_fluid_error_without_a_traceback(self):
        from fluid_build.cli._forge_mode_errors import take_forge_failure

        logger = MagicMock()
        exc = CopilotGenerationError("internal_error_code", "friendly message")

        with forge_mode_error_boundary(
            logger, MagicMock(), fail_label="X failed", render_error=lambda c, e: None
        ) as boundary:
            _fail(exc)

        assert boundary.exit_code == 1
        logger.exception.assert_not_called()
        logger.error.assert_called_once_with(
            "%s: %s", "X failed", "internal_error_code", extra={"fluid_console_shown": True}
        )
        assert take_forge_failure() is exc


class TestDescribeForgeFailure:
    def _describe(self, exc):
        from fluid_build.cli._forge_mode_errors import describe_forge_failure

        return describe_forge_failure(_raised(exc))

    def test_a_copilot_error_carries_its_code_message_and_next_steps(self):
        fields = self._describe(
            CopilotGenerationError("copilot_no_llm_provider", "No provider.", suggestions=["a"])
        )
        assert fields["code"] == "copilot_no_llm_provider"
        assert fields["message"] == "No provider."
        assert fields["suggestions"] == ["a"]
        assert fields["docs_url"].startswith("https://")
        assert "exception" not in fields

    def test_a_missing_extra_names_its_install_line(self):
        from fluid_build._optional import MissingOptionalDependency

        fields = self._describe(MissingOptionalDependency("mcp", "mcp", "fluid mcp serve"))
        assert fields == {
            "code": "mcp_sdk_unavailable",
            "message": "fluid mcp serve needs mcp, which is not installed.",
            "suggestions": ["pip install 'data-product-forge[mcp]'"],
        }

    def test_a_fluid_error_gets_a_snake_case_code(self):
        from fluid_build.errors import ConfigurationError

        fields = self._describe(ConfigurationError("bad config", suggestions=["fix it"]))
        assert (fields["code"], fields["message"], fields["suggestions"]) == (
            "configuration_error",
            "bad config",
            ["fix it"],
        )

    def test_a_typed_user_error_maps_what_fix_and_doc(self):
        from fluid_build._errors import FluidUserError

        fields = self._describe(
            FluidUserError(what="w", why="y", fix="f", doc="https://d", code="ERR_X")
        )
        assert fields == {
            "code": "ERR_X",
            "message": "w",
            "suggestions": ["f"],
            "docs_url": "https://d",
        }

    def test_a_crash_is_named_by_its_class_never_its_text(self):
        fields = self._describe(OSError("/home/alice/.ssh/id_rsa: permission denied"))
        assert fields["code"] == "forge_unexpected_error"
        assert fields["exception"] == "OSError"
        assert "alice" not in repr(fields)

    def test_secrets_in_the_text_are_redacted(self):
        leaked = "ghp_" + "a" * 36  # pragma: allowlist secret
        fields = self._describe(
            CopilotGenerationError("c", f"token {leaked} refused", suggestions=[f"drop {leaked}"])
        )
        assert leaked not in repr(fields)


# ── the handlers that swallow a failure note it ───────────────────────


class TestHandlersNoteTheirFailure:
    def test_the_agent_loop_notes_a_typed_failure_without_a_traceback(
        self, tmp_path, caplog, monkeypatch
    ):
        from fluid_build.cli._forge_mode_errors import take_forge_failure
        from fluid_build.cli._template_mode import _create_project_agent_loop

        error = CopilotGenerationError("copilot_no_llm_provider", "No LLM provider configured.")

        def _resolve(*args, **kwargs):
            raise error

        monkeypatch.setattr(
            "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config", _resolve
        )
        logger = logging.getLogger("test.forge.agent_loop")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            ok = _create_project_agent_loop(
                target_dir=tmp_path,
                context={},
                copilot_options={},
                copilot=None,
                dry_run=False,
                logger=logger,
                console=None,
            )

        assert ok is False
        assert take_forge_failure() is error
        assert not [r for r in caplog.records if r.exc_info and r.levelno > logging.DEBUG]
        # Nothing printed it (no console), so its log line stays on the console.
        assert caplog.records[0].fluid_console_shown is False

    @pytest.mark.parametrize(
        "exc",
        [CopilotGenerationError("copilot_x", "typed"), OSError("disk full")],
        ids=["typed", "crash"],
    )
    def test_template_mode_notes_its_failure(self, exc):
        from fluid_build.cli._forge_mode_errors import take_forge_failure
        from fluid_build.cli._template_mode import run_template_mode

        def _target(args, default_name):
            raise exc

        args = argparse.Namespace(non_interactive=True, template="starter", provider=None)
        rc = run_template_mode(
            args, MagicMock(), get_target_directory_fn=_target, console_factory=None
        )

        assert rc == 1
        assert take_forge_failure() is exc


def test_forge_prints_a_typed_user_errors_fix(tmp_path, capsys, monkeypatch):
    """``_run_main`` prints what the ``--agent`` event carries, a FluidUserError's fix too."""
    from fluid_build._errors import FluidUserError
    from fluid_build.cli import forge as forge_mod

    def _fail(args, logger):
        raise FluidUserError(what="The target is locked.", why="w", fix="Unlock it first.", doc="")

    monkeypatch.chdir(tmp_path)
    for var in ("FLUID_FORGE_NO_PICKER", "FLUID_FORGE_NO_WELCOME", "FLUID_FORGE_NO_PREVIEW"):
        monkeypatch.setenv(var, "1")
    monkeypatch.setattr(forge_mod, "run_ai_copilot_mode", _fail)
    parser = argparse.ArgumentParser()
    forge_mod.register(parser.add_subparsers())
    args = parser.parse_args(
        ["forge", "--non-interactive", "--llm-provider", "openai", "-d", str(tmp_path / "p")]
    )

    assert forge_mod.run(args, logging.getLogger("test")) == 1

    printed = capsys.readouterr()
    assert "Forge failed: The target is locked." in printed.out
    assert "Unlock it first." in printed.out
