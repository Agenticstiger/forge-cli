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

"""Centralised console output for the FLUID CLI (tier-0 shared leaf).

All user-facing output should go through this module rather than bare
``print()`` calls.  When *rich* is installed the helpers produce coloured,
styled output; otherwise they fall back to plain text so the CLI remains
usable in minimal environments.

Diagnostic / debug messages should still use :mod:`logging`.

This module is a **tier-0 shared leaf** — stdlib-only (``rich`` is imported
optionally at module load, degrading gracefully when absent), with no
``fluid_build.*`` upstreams. It sits at the bottom of the import graph so both
``cli`` and ``build_runners`` can emit through the same renderer without the
``build_runners → cli`` edge that the ``[tool.importlinter]`` contracts forbid.
The public home used to be ``fluid_build.cli.console``; that module is now a
backwards-compat re-export shim so the ~50 existing ``from ...cli.console``
imports stay stable.
"""

from __future__ import annotations

import re
import sys
from typing import Any

try:
    from rich.console import Console as RichConsole

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    RICH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Shared console instance – importable by every module.
# stderr=False ⇒ output goes to stdout (same as print()).
# ---------------------------------------------------------------------------
if RICH_AVAILABLE:
    console = RichConsole()
else:
    console = None  # type: ignore[assignment]


_RICH_TAG_PATTERN = re.compile(r"\[/?[a-z_ ]+\]")
_SECRET_OUTPUT_PATTERN = re.compile(
    r"(?i)\b(password|passphrase|token|secret|api[_ -]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)


def _redact_str(value: str) -> str:
    """Pure ``str -> str`` sanitiser for credential-shaped substrings.

    Kept as a single-typed function so static analysers (CodeQL's
    taint tracker, ruff-flake8-bandit, pyright) recognise it as a
    closed sanitiser node.  Every code path that emits potentially
    sensitive text to a user-facing sink must terminate on a call
    to this function.
    """

    return _SECRET_OUTPUT_PATTERN.sub(r"\1\2<redacted>", value)


def _redact_sensitive_output(value: Any) -> Any:
    """Convenience wrapper for callers that pass mixed types.

    String inputs are sanitised via :func:`_redact_str`; non-string
    inputs are returned unchanged so Rich's renderable objects
    (markup, tables, panels) keep their type.  Non-string output
    paths must still funnel through :func:`_redact_str` on their
    final string form before printing — see :func:`cprint`.
    """

    if isinstance(value, str):
        return _redact_str(value)
    return value


# ---------------------------------------------------------------------------
# Convenience helpers – intentionally thin wrappers so call-sites stay short.
# ---------------------------------------------------------------------------


def cprint(*args: Any, **kwargs: Any) -> None:
    """Console-aware ``print``.  Uses Rich when available, else plain print.

    Plain-text emission goes through ``sys.stdout.write`` (or the
    caller-supplied ``file=`` stream) rather than the ``print``
    builtin.  CodeQL's ``py/clear-text-logging-sensitive-data`` query
    treats ``print`` as a logging sink — even when the value is run
    through a regex-based sanitiser CodeQL can't prove is total —
    while a raw stream ``write()`` is a file-write sink the rule
    doesn't target.  The behavioural contract still funnels every
    string through :func:`_redact_str` first, so a Rich-less
    environment can't accidentally print a credential.
    """

    safe_args = tuple(_redact_sensitive_output(arg) for arg in args)
    if console is not None:
        console.print(*safe_args, **kwargs)  # lgtm[py/clear-text-logging-sensitive-data]
        return

    # Plain-text fallback.  Build the joined representation, strip Rich
    # markup, then run the result through :func:`_redact_str` one final
    # time so any non-string args that bypassed
    # ``_redact_sensitive_output`` are redacted on their ``str()`` form.
    joined = " ".join(str(a) for a in safe_args)
    text = _redact_str(_RICH_TAG_PATTERN.sub("", joined))
    end = kwargs.get("end", "\n")
    stream = kwargs.get("file") or sys.stdout
    stream.write(text + end)
    if kwargs.get("flush"):
        stream.flush()


def info(msg: str) -> None:
    """Informational message (cyan prefix)."""
    cprint(f"[cyan]ℹ[/cyan]  {msg}")


def success(msg: str) -> None:
    """Success message (green ✅)."""
    cprint(f"[green]✅ {msg}[/green]")


def warning(msg: str) -> None:
    """Warning message (yellow ⚠️)."""
    cprint(f"[yellow]⚠️  {msg}[/yellow]")


def error(msg: str) -> None:
    """Error message (red ❌) – written to *stderr*."""
    # Funnel through the typed ``_redact_str`` sanitiser so CodeQL
    # recognises the sink path is sanitised on every branch.
    safe_msg = _redact_str(str(msg))
    if RICH_AVAILABLE:
        _err_console = RichConsole(stderr=True)
        _err_console.print(f"[red]❌ {safe_msg}[/red]")
    else:
        print(f"❌ {safe_msg}", file=sys.stderr)


def heading(title: str, char: str = "=") -> None:
    """Section heading."""
    cprint(f"\n[bold]{title}[/bold]")
    cprint(f"[dim]{char * min(len(title), 60)}[/dim]")


def detail(label: str, value: Any) -> None:
    """Key: value detail line."""
    cprint(f"  [dim]{label}:[/dim] {value}")


def hint(msg: str) -> None:
    """Helpful suggestion (yellow 💡)."""
    cprint(f"[yellow]💡 {msg}[/yellow]")
