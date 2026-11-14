# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""PostToolUse hook: validate a FLUID contract right after an agent edits it.

Claude Code runs this after every Edit or Write and hands it the tool call as JSON on
stdin; it does not put the file path into the command line. A file that is not a
contract is ignored. A contract that fails ``fluid validate`` exits 2, which Claude Code
shows to the agent, with each finding's code, JSON pointer and fix. A valid contract
exits 0: silently when it is clean, and with its warnings as ``additionalContext`` when
it has some (a placeholder email, a starter-template value), so the agent sees what
``--strict`` would fail on without being blocked on it. A contract it could not
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
#: Raw strings, so every range is written as escapes (ECMA-48: CSI parameter bytes
#: 0x30-0x3F, intermediate 0x20-0x2F, final 0x40-0x7E; Fe escapes 0x40-0x5F).
_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x1b[\x40-\x5f]?"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
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
    return None if fluid is None else [fluid, "validate", path, "--offline", "--format", "json"]


def _clip(text: str) -> str:
    lines = [line.rstrip() for line in printable(text).splitlines() if line.strip()]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... {len(lines) - MAX_LINES} more lines"]
    return "\n".join(lines)


def _report(stdout: str) -> Optional[dict]:
    """The JSON document ``fluid validate --format json`` printed, or ``None``."""
    start = stdout.find("{")
    try:
        document = json.loads(stdout[start:]) if start >= 0 else None
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _issue_lines(issue: dict) -> List[str]:
    """``- <code> at <pointer>: <message>``, then its fix unless the message ends with it."""
    message, fix = str(issue.get("message", "")), str(issue.get("fix") or "")
    where = issue.get("pointer") or "(the whole contract)"
    lines = [f"- {issue.get('code')} at {where}: {message}"]
    return lines + [f"  Fix: {fix}"] if fix and fix not in message else lines


def findings(report: dict, severities: tuple) -> List[str]:
    """The lines for the findings of ``severities``: code, pointer, message and fix.

    A ``fluid`` older than its ``issues`` list gives the messages alone.
    """
    issues = report.get("issues")
    if not isinstance(issues, list):
        keys = {"error": "errors", "warning": "warnings"}
        return [f"- {m}" for s in severities if s in keys for m in report.get(keys[s]) or []]
    lines: List[str] = []
    for issue in issues:
        if isinstance(issue, dict) and issue.get("severity") in severities:
            lines.extend(_issue_lines(issue))
    return lines


def main(
    stdin: TextIO = sys.stdin, stderr: TextIO = sys.stderr, stdout: TextIO = sys.stdout
) -> int:
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
    report = _report(proc.stdout)
    if proc.returncode == 0:
        warnings = findings(report, ("warning",)) if report else []
        if warnings:
            context = (
                f"fluid validate passed for {shown}, with warnings that fail --strict:\n"
                f"{_clip(chr(10).join(warnings))}\n"
                "Fix each one with what the request gives; never invent a value to "
                "silence a warning."
            )
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": context,
                    }
                },
                stdout,
            )
        return 0
    errors = findings(report, ("error", "warning")) if report else []
    shown_output = "\n".join(errors) if errors else proc.stdout + proc.stderr
    print(
        f"fluid validate failed for {shown}:\n{_clip(shown_output)}\n"
        f"Fix these errors, then run: fluid validate {shown} --offline",
        file=stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
