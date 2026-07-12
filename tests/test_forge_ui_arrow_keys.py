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

"""Arrow-key selection behavior for ``forge_ui.ask_numbered_choice``.

The interactive path is only reached on a real TTY; these tests drive
``_arrow_key_select`` directly with a scripted ``readchar.readkey`` so the
navigation logic is covered without a terminal, and exercise the
``_arrow_keys_enabled`` gate + numbered fallback around it.
"""

from __future__ import annotations

import io
from unittest import mock

import pytest

from fluid_build.cli import forge_ui

OPTIONS = [("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]


def _console():
    from rich.console import Console

    # force_terminal so Rich Live renders its control sequences into the
    # StringIO exactly as it would on a terminal, without needing a real tty.
    return Console(file=io.StringIO(), force_terminal=True)


def _scripted_readkey(keys):
    """Return a fake ``readkey`` that yields *keys* one call at a time."""
    it = iter(keys)

    def _fake():
        return next(it)

    return _fake


def _run_select(keys, *, default_idx=0, options=OPTIONS):
    with mock.patch("readchar.readkey", _scripted_readkey(keys)):
        return forge_ui._arrow_key_select(_console(), "Pick one:", options, default_idx)


# ---------------------------------------------------------------------------
# _arrow_key_select — navigation
# ---------------------------------------------------------------------------


def test_down_then_enter_selects_next():
    from readchar import key

    assert _run_select([key.DOWN, key.ENTER]) == "b"


def test_two_downs_selects_third():
    from readchar import key

    assert _run_select([key.DOWN, key.DOWN, key.ENTER]) == "c"


def test_enter_immediately_takes_default():
    from readchar import key

    assert _run_select([key.ENTER], default_idx=1) == "b"


def test_up_wraps_from_top_to_bottom():
    from readchar import key

    assert _run_select([key.UP, key.ENTER], default_idx=0) == "c"


def test_down_wraps_from_bottom_to_top():
    from readchar import key

    assert _run_select([key.DOWN, key.ENTER], default_idx=2) == "a"


def test_vim_keys_navigate():
    from readchar import key

    # j down, j down, k up -> lands on index 1
    assert _run_select(["j", "j", "k", key.ENTER]) == "b"


def test_digit_moves_cursor_then_enter_confirms():
    from readchar import key

    # "3" moves the cursor to the third option; Enter confirms it. The
    # trailing Enter is consumed here (does NOT leak to a next prompt).
    assert _run_select(["3", key.ENTER]) == "c"


def test_digit_alone_does_not_commit():
    from readchar import key

    # A bare "3" only moves; a subsequent arrow can still change the choice
    # before Enter — proving the digit did not commit.
    assert _run_select(["3", key.UP, key.ENTER]) == "b"


def test_out_of_range_digit_is_ignored():
    from readchar import key

    # "9" is out of range (only 3 options) -> ignored, Enter takes default
    assert _run_select(["9", key.ENTER], default_idx=0) == "a"


def test_ctrl_c_propagates_as_keyboardinterrupt():
    from readchar import key

    with pytest.raises(KeyboardInterrupt):
        _run_select([key.CTRL_C])


def test_default_idx_out_of_bounds_is_clamped():
    from readchar import key

    # default_idx beyond the list clamps to the last option
    assert _run_select([key.ENTER], default_idx=99) == "c"


def test_selection_confirmation_line_is_printed():
    from readchar import key

    console = _console()
    with mock.patch("readchar.readkey", _scripted_readkey([key.DOWN, key.ENTER])):
        forge_ui._arrow_key_select(console, "Pick one:", OPTIONS, 0)
    out = console.file.getvalue()
    # collapsed confirmation shows the chosen label
    assert "Banana" in out


def test_label_with_brackets_does_not_break_render():
    from readchar import key

    # A label containing "[...]" must not be parsed as Rich markup (which
    # would raise MarkupError and, via the caller's except, spuriously drop
    # back to the numbered prompt). It should render + return cleanly.
    bracket_opts = [("x", "model[8b]"), ("y", "plain")]
    console = _console()
    with mock.patch("readchar.readkey", _scripted_readkey([key.ENTER])):
        result = forge_ui._arrow_key_select(console, "Pick [a] model:", bracket_opts, 0)
    assert result == "x"
    assert "model[8b]" in console.file.getvalue()


# ---------------------------------------------------------------------------
# _arrow_keys_enabled — the gate
# ---------------------------------------------------------------------------


def test_gate_false_without_console():
    assert forge_ui._arrow_keys_enabled(None) is False


def test_gate_false_under_pytest_env():
    # PYTEST_CURRENT_TEST is set while this test runs -> gate must decline,
    # which is exactly what keeps the numbered path deterministic in CI.
    assert forge_ui._arrow_keys_enabled(_console()) is False


def test_gate_false_on_explicit_optout(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("FLUID_FORGE_NO_ARROW_KEYS", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert forge_ui._arrow_keys_enabled(_console()) is False


def test_gate_true_when_tty_and_readchar_present(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("FLUID_FORGE_NO_ARROW_KEYS", raising=False)
    monkeypatch.delenv("FLUID_NO_ARROW_KEYS", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert forge_ui._arrow_keys_enabled(_console()) is True


def test_gate_false_when_stdout_not_tty(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert forge_ui._arrow_keys_enabled(_console()) is False


# ---------------------------------------------------------------------------
# ask_numbered_choice — numbered fallback preserved when gate is closed
# ---------------------------------------------------------------------------


def test_numbered_fallback_used_when_gate_closed(monkeypatch):
    # Under pytest the gate is closed, so this must use the numbered path.
    # Feed the Rich prompt a "2" and confirm the second value returns.
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *a, **k: "2"))
    result = forge_ui.ask_numbered_choice(_console(), "Pick one:", OPTIONS, default=1)
    assert result == "b"


def test_numbered_fallback_default_on_enter(monkeypatch):
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *a, **k: "3"))
    result = forge_ui.ask_numbered_choice(_console(), "Pick one:", OPTIONS, default=1)
    assert result == "c"


def test_empty_options_returns_empty_string():
    assert forge_ui.ask_numbered_choice(_console(), "Pick one:", []) == ""
