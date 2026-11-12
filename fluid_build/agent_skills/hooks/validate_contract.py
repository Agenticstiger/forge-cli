# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""PostToolUse hook: validate a FLUID contract right after an agent edits it.

Claude Code runs this after every Edit or Write and hands it the tool call as JSON on
stdin; it does not put the file path into the command line. A file that is not a
contract is ignored. A contract that fails ``fluid validate`` exits 2, which Claude Code
shows to the agent; a valid contract exits 0 and prints nothing.

It always runs as a script, by path: the plugin hook as
``python3 "${CLAUDE_PLUGIN_ROOT}/hooks/validate_contract.py"``, and the hook that
``fluid scaffold-ide --target claude-code`` writes as ``<python> <this file>``. It then
runs the ``fluid`` console script. Never ``python -m``: that puts the working
directory, the project the agent writes to, first on ``sys.path``, so a ``json.py``
the agent planted there would run on its next edit. A script and a console script put
only their own directory there.

Standard library only, so any ``python3`` can run it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, TextIO

CONTRACT_SUFFIXES = (".fluid.yaml", ".fluid.yml", ".fluid.json")
TIMEOUT_S = 60
MAX_LINES = 40
#: Terminal control sequences: OSC (title, clipboard, links), CSI, the other ESC pairs,
#: then any C0 or C1 control character but tab and newline, and the Unicode bidi
#: controls that reorder how text reads (Trojan Source, CVE-2021-42574).
_CONTROL = re.compile(
    "\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)?"
    "|\x1b\\[[0-?]*[ -/]*[@-~]"
    "|\x1b[@-_]?"
    "|[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def printable(text: str) -> str:
    """``text`` without anything a terminal or the agent would read as a control code."""
    return _CONTROL.sub("", text)


def contract_path(payload: Any) -> Optional[str]:
    """The edited file's absolute path when it is a FLUID contract, else ``None``."""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if isinstance(path, str) and path.endswith(CONTRACT_SUFFIXES):
        # Absolute, so a name starting with "-" can never read as an option.
        return os.path.abspath(path)
    return None


def validate_command(path: str) -> Optional[List[str]]:
    """``fluid validate`` for ``path``: the console script beside this interpreter, else PATH."""
    beside = shutil.which("fluid", path=str(Path(sys.executable).parent))
    fluid = beside or shutil.which("fluid")
    if not fluid:
        return None
    return [fluid, "validate", path, "--offline"]


def _clip(text: str) -> str:
    lines = [line.rstrip() for line in printable(text).splitlines() if line.strip()]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... {len(lines) - MAX_LINES} more lines"]
    return "\n".join(lines)


def main(stdin: TextIO = sys.stdin, stderr: TextIO = sys.stderr) -> int:
    try:
        payload = json.load(stdin)
    except ValueError:
        return 0  # not a tool call this hook understands; never block the agent on it
    path = contract_path(payload)
    if path is None:
        return 0
    command = validate_command(path)
    shown = printable(path)
    if command is None:
        print(
            "The FLUID contract was not validated: fluid is not installed or not on PATH "
            "(pip install data-product-forge).",
            file=stderr,
        )
        return 1
    try:
        proc = subprocess.run(  # noqa: S603 - list argv, no shell
            command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        print(
            f"fluid validate took more than {TIMEOUT_S}s on {shown}; run it yourself.", file=stderr
        )
        return 1
    if proc.returncode == 0:
        return 0
    print(
        f"fluid validate failed for {shown}:\n{_clip(proc.stdout + proc.stderr)}\n"
        f"Fix these errors, then run: fluid validate {shown} --offline",
        file=stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
