# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Run the real ``fluid`` CLI inside the test process.

In process is faster than a subprocess and lets a test patch what the command
calls. The pattern is the one ``click.testing.CliRunner`` uses (call the entry
point, catch ``SystemExit``, read the captured streams), with the capture left to
the caller: pass pytest's ``capfd`` (or ``capsys``), or anything else whose
``readouterr()`` returns ``(out, err)``.

::

    def test_validate(capfd):
        code, out, err = run_fluid(capfd, "validate", "contract.fluid.yaml")
        assert code == 0
"""

from __future__ import annotations

import sys
from typing import Any, Tuple

__all__ = ["run_fluid"]


def run_fluid(capture: Any, *argv: str) -> Tuple[int, str, str]:
    """Run ``fluid <argv...>`` in this process: ``(exit code, stdout, stderr)``.

    ``capture`` is anything with a ``readouterr()`` that returns ``(out, err)``,
    such as pytest's ``capfd`` or ``capsys``. The exit code is what the shell
    would see for ``sys.exit(main(argv))``: a command that returns, or calls
    ``sys.exit`` with, ``None`` gives 0, an integer gives itself, and anything
    else is written to stderr and gives 1. A ``KeyboardInterrupt`` or any other
    exception propagates, so a crash fails the test with its own traceback.
    """
    from fluid_build.cli import main

    try:
        code = _exit_code(main(list(argv)))
    except SystemExit as exc:  # argparse, and commands that exit directly
        code = _exit_code(exc.code)
    out, err = capture.readouterr()
    return code, out, err


def _exit_code(value: Any) -> int:
    """The status the interpreter reports for ``sys.exit(value)``."""
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)
    sys.stderr.write(f"{value}\n")
    return 1
