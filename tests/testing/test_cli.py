# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.testing.cli.run_fluid``: exit codes and captured output."""

from __future__ import annotations

from typing import List, Tuple

import pytest

import fluid_build.cli as cli
from fluid_build.testing.cli import run_fluid

pytestmark = pytest.mark.unit


class _Capture:
    """The smallest capture ``run_fluid`` accepts: ``readouterr()`` returning ``(out, err)``."""

    def __init__(self, out: str = "", err: str = "") -> None:
        self.out, self.err = out, err

    def readouterr(self) -> Tuple[str, str]:
        return self.out, self.err


_UNSET = object()


def _patch_main(monkeypatch, *, returns=None, exits=_UNSET, raises=None) -> List[List[str]]:
    """Replace ``fluid_build.cli.main``: return ``returns``, or exit with ``exits``, or raise."""
    calls: List[List[str]] = []

    def main(argv):
        calls.append(argv)
        if raises is not None:
            raise raises
        if exits is not _UNSET:
            raise SystemExit(exits)
        return returns

    monkeypatch.setattr(cli, "main", main)
    return calls


def test_it_passes_the_arguments_as_a_list_and_returns_the_exit_code_and_both_streams(monkeypatch):
    calls = _patch_main(monkeypatch, returns=7)
    assert run_fluid(_Capture("the out", "the err"), "plan", "c.yaml", "--out", "p.json") == (
        7,
        "the out",
        "the err",
    )
    assert calls == [["plan", "c.yaml", "--out", "p.json"]]


@pytest.mark.parametrize(
    "returns, exits, code",
    [
        (None, _UNSET, 0),  # a command that returns nothing succeeded
        (0, _UNSET, 0),
        (True, _UNSET, 1),
        (None, 3, 3),
        (None, 0, 0),
        (None, None, 0),  # sys.exit() and sys.exit(None) report success
    ],
)
def test_the_code_is_what_the_shell_would_see(monkeypatch, returns, exits, code):
    _patch_main(monkeypatch, returns=returns, exits=exits)
    assert run_fluid(_Capture())[0] == code


def test_a_message_given_to_exit_goes_to_stderr_and_the_code_is_one(monkeypatch, capsys):
    _patch_main(monkeypatch, exits="fatal: no contract")
    code, out, err = run_fluid(capsys)
    assert (code, out) == (1, "") and err == "fatal: no contract\n"


@pytest.mark.parametrize("error", [RuntimeError("crash"), KeyboardInterrupt()])
def test_a_crash_is_not_swallowed(monkeypatch, error):
    _patch_main(monkeypatch, raises=error)
    with pytest.raises(type(error)):
        run_fluid(_Capture())


def test_the_real_cli_prints_its_help_and_exits_zero(capfd):
    code, out, err = run_fluid(capfd, "--help")
    assert code == 0 and err == ""
    assert "validate" in out and "plan" in out  # the command list


def test_the_real_cli_refuses_an_unknown_command_with_argparses_status_two(capfd):
    code, out, err = run_fluid(capfd, "no-such-command")
    assert code == 2 and "no-such-command" in err
