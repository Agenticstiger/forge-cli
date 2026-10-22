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

"""Tests for fluid_build.cli.console — centralised output helpers."""

from unittest.mock import patch

from fluid_build.cli.console import (
    _redact_str,
    cprint,
    cprint_literal,
    detail,
    error,
    heading,
    hint,
    info,
    success,
    warning,
)


class TestCprint:
    def test_plain_text(self, capsys):
        cprint("hello world")
        captured = capsys.readouterr()
        assert "hello" in captured.out

    def test_strips_markup_when_no_rich(self, capsys):
        # ``cprint`` reads the module-global ``console`` sentinel of its
        # *defining* module, which is now the tier-0 leaf
        # ``fluid_build._console`` (``cli.console`` is a re-export shim).
        # Patch the canonical home so the Rich-off fallback is exercised.
        with patch("fluid_build._console.console", None):
            cprint("[bold]bold text[/bold]")
            captured = capsys.readouterr()
            assert "bold text" in captured.out
            assert "[bold]" not in captured.out

    def test_empty_call(self, capsys):
        cprint()
        # Should not crash

    def test_redacts_secret_like_values(self) -> None:
        assert _redact_str("token=live-token password:super-secret") == (
            "token=<redacted> password:<redacted>"
        )


class TestHelpers:
    def test_info(self, capsys):
        info("test message")
        captured = capsys.readouterr()
        assert "test message" in captured.out

    def test_success(self, capsys):
        success("done")
        captured = capsys.readouterr()
        assert "done" in captured.out

    def test_warning(self, capsys):
        warning("be careful")
        captured = capsys.readouterr()
        assert "be careful" in captured.out

    def test_error(self, capsys):
        error("something broke")
        captured = capsys.readouterr()
        assert "something broke" in captured.err

    def test_error_prints_markup_shaped_text_literally(self, capsys):
        # The ``[red]`` wrapper made a ``[/]`` raise MarkupError and ate ``[orders]``.
        message = "[/] exposes[orders] ^[a-z0-9]+$ [bold]x[/bold] C:\\work\\"
        error(message)
        assert f"❌ {message}\n" in capsys.readouterr().err

    def test_error_still_redacts_secrets(self, capsys):
        error("connect failed [/] password=hunter2")
        err = capsys.readouterr().err
        assert "password=<redacted>" in err
        assert "hunter2" not in err

    def test_error_spells_out_terminal_control_characters(self, capsys, monkeypatch):
        # A terminal: Rich writes its own colour codes, but none from the message.
        monkeypatch.setenv("FORCE_COLOR", "1")
        error("A\x1b[2J\x1b]8;;http://evil.example/\x1b\\B \x9b31m\r\nnext\rover")
        err = capsys.readouterr().err
        assert "\x1b[2J" not in err and "\x1b]8;;" not in err and "\x9b" not in err
        assert "A\\x1b[2J\\x1b]8;;http://evil.example/\\x1b\\B \\x9b31m" in err
        # A CRLF still breaks the line; a lone CR, which would print over it, is spelled.
        assert "\r" not in err and "next\\x0dover" in err

    def test_error_keeps_a_crlf_line_break(self, capsys):
        error("Validation failed\r\nsee line 2")
        assert capsys.readouterr().err == "❌ Validation failed\nsee line 2\n"
        with patch("fluid_build._console.RICH_AVAILABLE", False):
            error("Validation failed\r\nsee line 2")
        assert capsys.readouterr().err == "❌ Validation failed\nsee line 2\n"

    def test_error_without_rich_spells_out_control_characters(self, capsys):
        with patch("fluid_build._console.RICH_AVAILABLE", False):
            error("deploy failed\r✅ all checks passed \x1b[2J")
        assert capsys.readouterr().err == "❌ deploy failed\\x0d✅ all checks passed \\x1b[2J\n"

    def test_error_redacts_a_secret_beside_a_control_character(self, capsys):
        # A spelled control ends in a hex digit, which hid the key from the pattern.
        error("PATH=/usr/bin\x00PASSWORD=hunter2\x00HOME=/root; token\x1f=abc123")
        err = capsys.readouterr().err
        assert "hunter2" not in err and "abc123" not in err
        assert "\\x00PASSWORD=<redacted>" in err

    def test_heading(self, capsys):
        heading("My Section")
        captured = capsys.readouterr()
        assert "My Section" in captured.out

    def test_detail(self, capsys):
        detail("Status", "OK")
        captured = capsys.readouterr()
        assert "Status" in captured.out
        assert "OK" in captured.out

    def test_hint(self, capsys):
        hint("try this")
        captured = capsys.readouterr()
        assert "try this" in captured.out


class TestCprintLiteral:
    def test_prints_data_as_written(self, capsys):
        # Markup, emoji codes (``:x:``, the ``:b:`` in ``fe80::a:b:c``) and controls.
        cprint_literal("exposes[orders] [/] :x: fe80::a:b:c C:\\work\\ \x1b[2J")
        out = capsys.readouterr().out
        assert "exposes[orders] [/] :x: fe80::a:b:c C:\\work\\ \\x1b[2J\n" in out

    def test_still_redacts(self, capsys):
        cprint_literal("dsn failed: user=bob\x00password=hunter2")
        assert "hunter2" not in capsys.readouterr().out

    def test_keeps_brackets_without_rich(self, capsys):
        with patch("fluid_build._console.console", None):
            cprint_literal("exposes[orders] [bold]x[/bold]")
        assert capsys.readouterr().out == "exposes[orders] [bold]x[/bold]\n"
