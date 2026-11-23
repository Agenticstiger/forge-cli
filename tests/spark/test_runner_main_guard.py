# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner is run as a script (``spark-submit forge_spark_runner.py``), so its ``__main__``
guard runs when Python reaches it: every function the run calls is defined above it.

A function defined below the guard is a ``NameError`` in a real run and invisible to a test
that imports the module (live run, 2026-11-23: ``_check_maintenance`` sat below the guard).
"""

from __future__ import annotations

import ast

import pytest

from .conftest import RUNNER_PATH

pytestmark = pytest.mark.unit


def test_nothing_is_defined_after_the_main_guard() -> None:
    body = ast.parse(RUNNER_PATH.read_text(encoding="utf-8")).body
    guards = [
        i
        for i, node in enumerate(body)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    ]
    assert len(guards) == 1, "the runner has one `if __name__ == '__main__'` guard"
    after = [type(n).__name__ for n in body[guards[0] + 1 :]]
    assert not after, (
        "top-level statements after the __main__ guard are not defined when a script run "
        f"calls them: {after}"
    )
