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
    cprint,
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
        with patch("fluid_build.cli.console.console", None):
            cprint("[bold]bold text[/bold]")
            captured = capsys.readouterr()
            assert "bold text" in captured.out
            assert "[bold]" not in captured.out

    def test_empty_call(self, capsys):
        cprint()
        # Should not crash


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
