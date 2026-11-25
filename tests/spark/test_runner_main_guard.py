# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner is run as a script (``spark-submit forge_spark_runner.py``), so its ``__main__``
guard runs when Python reaches it: every function the run calls is defined above it.

A function defined below the guard is a ``NameError`` in a real run and invisible to a test
that imports the module (live run, 2026-11-23: ``_check_maintenance`` sat below the guard).

Glue runs the script through ``runpy`` and marks a run FAILED on any ``SystemExit``, code 0
too (live run, 2026-11-25: the runner logged ``done``, then Glue ended the run
``FAILED: SystemExit: 0``). So a run that succeeds returns from the guard without exiting.
"""

from __future__ import annotations

import ast
import runpy
import sys

import pytest

from .conftest import RUNNER_PATH

pytestmark = pytest.mark.unit


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def test_nothing_is_defined_after_the_main_guard() -> None:
    body = ast.parse(RUNNER_PATH.read_text(encoding="utf-8")).body
    guards = [i for i, node in enumerate(body) if _is_main_guard(node)]
    assert len(guards) == 1, "the runner has one `if __name__ == '__main__'` guard"
    after = [type(n).__name__ for n in body[guards[0] + 1 :]]
    assert not after, (
        "top-level statements after the __main__ guard are not defined when a script run "
        f"calls them: {after}"
    )


def _run_guard(tmp_path, returns: int) -> None:
    """Run the runner's own ``__main__`` guard through ``runpy``, as Glue does, with ``main``
    patched to return ``returns``.

    The guard is taken verbatim from the runner. Running the whole file would define the real
    ``main`` over the patched one, and the real one starts Spark.
    """
    source = RUNNER_PATH.read_text(encoding="utf-8")
    guard = next(node for node in ast.parse(source).body if _is_main_guard(node))
    script = tmp_path / RUNNER_PATH.name
    script.write_text(ast.get_source_segment(source, guard) or "", encoding="utf-8")
    runpy.run_path(
        str(script), init_globals={"main": lambda: returns, "sys": sys}, run_name="__main__"
    )


def test_a_run_that_succeeds_raises_no_system_exit(tmp_path) -> None:
    _run_guard(tmp_path, 0)


def test_a_run_that_fails_exits_with_its_code(tmp_path) -> None:
    with pytest.raises(SystemExit) as raised:
        _run_guard(tmp_path, 2)
    assert raised.value.code == 2
