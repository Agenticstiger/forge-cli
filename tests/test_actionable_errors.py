# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.errors — actionable error class."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.errors import (
    ActionableError,
    CIStateMismatchError,
    FragmentShapeError,
    LockfileMismatchError,
    RefResolutionActionableError,
    format_actionable_error,
    handle_actionable_error,
    print_actionable_error,
)


class TestActionableErrorConstruction:
    def test_minimum_fields(self):
        exc = ActionableError("something broke", fix="try that instead")
        assert exc.message == "something broke"
        assert exc.fix == "try that instead"
        assert exc.docs_url is None
        assert exc.code == "fluid.error"
        assert str(exc) == "something broke"

    def test_with_docs_url(self):
        exc = ActionableError(
            "broken",
            fix="run foo",
            docs_url="https://example.com/docs",
        )
        assert exc.docs_url == "https://example.com/docs"

    def test_subclass_code_is_distinct(self):
        assert FragmentShapeError.code == "fluid.fragment.shape"
        assert LockfileMismatchError.code == "fluid.lockfile.mismatch"
        assert CIStateMismatchError.code == "fluid.ci.drift"
        assert RefResolutionActionableError.code == "fluid.ref.resolution"
        # All distinct
        codes = {
            FragmentShapeError.code,
            LockfileMismatchError.code,
            CIStateMismatchError.code,
            RefResolutionActionableError.code,
        }
        assert len(codes) == 4

    def test_is_exception_and_raises_cleanly(self):
        with pytest.raises(ActionableError) as info:
            raise FragmentShapeError(
                "root was a list, expected a mapping",
                fix="check the fragment file at fragments/x.yaml",
            )
        assert info.value.message == "root was a list, expected a mapping"
        assert info.value.fix == "check the fragment file at fragments/x.yaml"


class TestFormatActionableError:
    def test_renders_symptom_fix_and_code(self):
        exc = ActionableError("thing broke", fix="do the other thing")
        rendered = format_actionable_error(exc)
        assert "ActionableError" in rendered
        assert "thing broke" in rendered
        assert "Fix: do the other thing" in rendered
        assert "fluid.error" in rendered

    def test_includes_docs_url_when_set(self):
        exc = ActionableError(
            "broken",
            fix="run foo",
            docs_url="https://example.com/fix",
        )
        rendered = format_actionable_error(exc)
        assert "Docs: https://example.com/fix" in rendered

    def test_omits_docs_url_when_none(self):
        exc = ActionableError("broken", fix="run foo")
        rendered = format_actionable_error(exc)
        assert "Docs:" not in rendered

    def test_subclass_name_in_output(self):
        exc = LockfileMismatchError(
            "lockfile is out of date",
            fix="run fluid bundle",
        )
        rendered = format_actionable_error(exc)
        assert "LockfileMismatchError" in rendered
        assert "fluid.lockfile.mismatch" in rendered


class TestPrintActionableError:
    def test_plain_fallback_writes_to_stderr(self, capsys):
        exc = ActionableError("plain mode", fix="check your config")
        print_actionable_error(exc, console=None)
        captured = capsys.readouterr()
        assert "plain mode" in captured.err
        assert "Fix: check your config" in captured.err

    def test_rich_panel_renders_when_console_available(self):
        mock_console = MagicMock()
        exc = ActionableError("rich mode", fix="run it")
        print_actionable_error(exc, console=mock_console)
        # Console.print was called multiple times (blank line, panel, blank line)
        assert mock_console.print.called

    def test_never_raises_on_console_error(self, capsys):
        """Console that throws on every call must not break the printer."""
        broken_console = MagicMock()
        broken_console.print.side_effect = RuntimeError("console dead")
        exc = ActionableError("broken", fix="fix it")
        # Must not raise
        print_actionable_error(exc, console=broken_console)
        # Plain fallback should have written to stderr
        captured = capsys.readouterr()
        assert "broken" in captured.err


class TestHandleActionableError:
    def test_returns_exit_code_two(self):
        exc = ActionableError("test", fix="do the thing")
        rc = handle_actionable_error(exc, console=None)
        assert rc == 2

    def test_logs_at_debug_level(self):
        logger = logging.getLogger("test.actionable.handler")
        with MagicMock() as mock_logger_wrapper:
            # Replace debug with a spy
            mock_logger_wrapper.debug = MagicMock()
            handle_actionable_error(
                ActionableError("test", fix="fix"),
                console=None,
                logger=mock_logger_wrapper,
            )
            mock_logger_wrapper.debug.assert_called_once()

    def test_logs_include_code_message_and_fix(self):
        mock_logger = MagicMock()
        exc = CIStateMismatchError(
            "drift detected",
            fix="delete the CI files",
        )
        handle_actionable_error(exc, console=None, logger=mock_logger)
        call_args = mock_logger.debug.call_args
        extra = call_args.kwargs.get("extra", {}) if call_args else {}
        assert extra.get("code") == "fluid.ci.drift"
        # Note: the field is named error_message (not message) to avoid
        # colliding with LogRecord's reserved attributes.
        assert extra.get("error_message") == "drift detected"
        assert extra.get("fix") == "delete the CI files"


# ---------------------------------------------------------------------------
# Integration: forge_contract_factory prints ActionableError on validation fail
# ---------------------------------------------------------------------------


class TestContractFactoryIntegration:
    def test_create_and_validate_prints_actionable_on_failure(self, tmp_path: Path, capsys):
        from fluid_build.cli.forge_contract_factory import (
            create_and_validate_contract,
        )

        logger = logging.getLogger("test.integration")

        # A contract missing required top-level keys will fail
        # validate_contract_file().
        invalid = {"not_a_contract": True}
        result = create_and_validate_contract(invalid, tmp_path, logger)
        assert result is None  # backward compat: returns None on failure

        # Plain output should contain the actionable error shape
        captured = capsys.readouterr()
        # The error panel goes to stderr (plain path) or to the console
        # (rich path). Either way, a "Fix:" line must appear somewhere.
        combined = captured.out + captured.err
        assert "Fix:" in combined or "fix:" in combined.lower()
