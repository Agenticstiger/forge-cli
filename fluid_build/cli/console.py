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

"""Centralised console output for the FLUID CLI.

All user-facing output should go through this module rather than bare
``print()`` calls.  When *rich* is installed the helpers produce coloured,
styled output; otherwise they fall back to plain text so the CLI remains
usable in minimal environments.

Diagnostic / debug messages should still use :mod:`logging`.
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


def _redact_sensitive_output(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_OUTPUT_PATTERN.sub(r"\1\2<redacted>", value)
    return value


# ---------------------------------------------------------------------------
# Convenience helpers – intentionally thin wrappers so call-sites stay short.
# ---------------------------------------------------------------------------


def cprint(*args: Any, **kwargs: Any) -> None:
    """Console-aware ``print``.  Uses Rich when available, else plain print."""
    safe_args = tuple(_redact_sensitive_output(arg) for arg in args)
    if console is not None:
        console.print(*safe_args, **kwargs)  # lgtm[py/clear-text-logging-sensitive-data]
    else:
        # Strip Rich markup for plain output.
        text = " ".join(str(a) for a in safe_args)
        text = _RICH_TAG_PATTERN.sub("", text)
        print(  # lgtm[py/clear-text-logging-sensitive-data]
            text,
            **{k: v for k, v in kwargs.items() if k in ("end", "file", "flush")},
        )


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
    safe_msg = _redact_sensitive_output(msg)
    if RICH_AVAILABLE:
        _err_console = RichConsole(stderr=True)
        _err_console.print(
            f"[red]❌ {safe_msg}[/red]"
        )  # lgtm[py/clear-text-logging-sensitive-data]
    else:
        print(f"❌ {safe_msg}", file=sys.stderr)  # lgtm[py/clear-text-logging-sensitive-data]


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
