# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""PostToolUse hook: validate a FLUID contract right after an agent edits it.

Claude Code runs this after every Edit or Write and hands it the tool call as JSON on
stdin; it does not put the file path into the command line. A file that is not a
contract is ignored. A contract that fails ``fluid validate`` exits 2, which Claude Code
shows to the agent; a valid contract exits 0 and prints nothing. A contract it could not
validate (no ``fluid`` found, or ``fluid validate`` ran out of time) exits 2 as well:
exit 1 would show only in the transcript, and the agent would take an unchecked contract
for a checked one.

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
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, TextIO

CONTRACT_SUFFIXES = (".fluid.yaml", ".fluid.yml", ".fluid.json")
#: Below the 90 s that hooks.json and scaffold-ide give the whole hook, so this hook's
#: own message reaches the agent before Claude Code stops it.
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


def find_fluid() -> Optional[str]:
    """The ``fluid`` console script: beside this interpreter first, then on PATH.

    Searched by hand rather than with ``shutil.which``, which on Windows looks in the
    working directory first, even with ``path=`` given. The working directory, and any
    relative PATH entry, is skipped: it is the project the agent writes to, so a
    ``fluid.cmd`` planted there must never run on its next edit. Console scripts are
    ``fluid.exe`` on Windows (pip and uv install them so), never ``.bat`` or ``.cmd``.
    """
    name = "fluid.exe" if os.name == "nt" else "fluid"
    here = os.path.realpath(os.getcwd())
    dirs = [str(Path(sys.executable).parent), *os.environ.get("PATH", "").split(os.pathsep)]
    for directory in dirs:
        if not directory or not os.path.isabs(directory):
            continue
        if os.path.realpath(directory) == here:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def validate_command(path: str) -> Optional[List[str]]:
    """``fluid validate`` for ``path``, or ``None`` when no ``fluid`` is found."""
    fluid = find_fluid()
    return None if fluid is None else [fluid, "validate", path, "--offline"]


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
            f"{shown} was not validated: the fluid CLI is not installed or not on PATH. "
            "Install it (pip install data-product-forge) or tell the user, then run: "
            f"fluid validate {shown} --offline",
            file=stderr,
        )
        return 2
    try:
        proc = subprocess.run(  # noqa: S603 - list argv, no shell
            command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        print(
            f"{shown} was not validated: fluid validate took more than {TIMEOUT_S}s. "
            f"Run it yourself: fluid validate {shown} --offline",
            file=stderr,
        )
        return 2
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
