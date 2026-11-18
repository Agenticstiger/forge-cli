# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A test that leaves the process in another working directory fails, alone.

``tests/conftest.py`` notes the working directory before each test's fixtures run
and checks it after their teardown. A leak used to fail an unrelated test later on
the same worker: the MCP output port's readable paths default to the working
directory, and the quickstart tests were refused their own example data. Each case
runs a small suite through pytest itself, with the check in its conftest.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = [pytest.mark.unit]
pytest_plugins = ["pytester"]

#: The inner suite's conftest: the check under test, and an autouse fixture that
#: asks for monkeypatch, as the suite's own do, so monkeypatch is torn down last.
_CONFTEST = """
import pytest

from tests.conftest import pytest_runtest_setup, pytest_runtest_teardown  # noqa: F401


@pytest.fixture(autouse=True)
def _asks_for_monkeypatch(monkeypatch):
    yield
"""

#: The inner suite's last test, which must start where the run did.
_NEXT = """

def test_next():
    assert os.getcwd() == START
"""


def _run(pytester: pytest.Pytester, body: str) -> pytest.RunResult:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_inner="import os\n\nSTART = os.getcwd()\n" + body + _NEXT)
    return pytester.runpytest("-p", "no:randomly", "-p", "no:cacheprovider", "-p", "no:asyncio")


def test_a_test_that_stays_put_passes(pytester: pytest.Pytester) -> None:
    result = _run(pytester, "\n\ndef test_stays(tmp_path):\n    (tmp_path / 'x').touch()\n")
    result.assert_outcomes(passed=2)


_MOVES_AWAY = """

def test_moves(tmp_path):
    os.chdir(tmp_path)
"""

#: The leak this check was written for: a fixture moves and moves back, and
#: monkeypatch, torn down after it, moves back into the fixture's directory.
_MONKEYPATCH_UNDER_A_MOVING_FIXTURE = """
import pytest


@pytest.fixture
def moved(tmp_path):
    os.chdir(tmp_path)
    try:
        yield
    finally:
        os.chdir(START)


def test_moves_with_monkeypatch(moved, monkeypatch, tmp_path):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
"""

#: A teardown error is the report, and the next test still starts right.
_A_TEARDOWN_THAT_RAISES = """
import pytest


@pytest.fixture
def breaks():
    yield
    raise RuntimeError("teardown boom")


def test_moves_and_breaks(breaks, tmp_path):
    os.chdir(tmp_path)
"""

_REMOVES_WHERE_IT_STANDS = """

def test_removes_where_it_stands(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    os.chdir(gone)
    gone.rmdir()
"""


@pytest.mark.parametrize(
    ("body", "reported"),
    [
        pytest.param(
            _MOVES_AWAY,
            "*test_inner.py::test_moves left the working directory at *, not * where it started*",
            id="moves-away",
        ),
        pytest.param(
            _MONKEYPATCH_UNDER_A_MOVING_FIXTURE,
            "*test_moves_with_monkeypatch left the working directory at *",
            id="monkeypatch-under-a-moving-fixture",
        ),
        pytest.param(
            _A_TEARDOWN_THAT_RAISES, "*RuntimeError: teardown boom*", id="teardown-raises"
        ),
        pytest.param(
            _REMOVES_WHERE_IT_STANDS,
            "*left the working directory at a directory that no longer exists*",
            id="removed-directory",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows keeps the working directory"
            ),
        ),
    ],
)
def test_only_the_test_that_moved_fails_and_the_next_starts_where_the_run_did(
    pytester: pytest.Pytester, body: str, reported: str
) -> None:
    result = _run(pytester, body)
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines([reported])
