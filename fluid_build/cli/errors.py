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

"""Actionable errors — every user-facing error names the fix.

UX audit F6: "Error messages don't teach."  The typed exceptions raised
by the init/forge artifact code tell you *what* failed (``RefResolutionError``,
``LockfileMismatch``, ``CIStateMismatch``, ``FragmentShapeMismatch``), not
*what to do about it*.

Slice UX-B introduces :class:`ActionableError`, a base exception class
that carries a ``fix`` string and an optional ``docs_url`` alongside its
message.  Every top-level command catches it at its outer boundary and
prints a single Rich panel with both the symptom and the fix, then exits
non-zero.  Example printed output:

    ❌ LockfileMismatch: fragments/builds/main.yaml has been edited but
       contract.lock.yaml is out of date.
       Fix: run 'fluid bundle' and commit the updated contract.bundled.yaml
            and contract.lock.yaml.
       Docs: https://fluid-build.dev/docs/fragments#lockfile

The idea — and the shape — is borrowed from ``cargo``, ``rustc``, and
``git`` which all wrap their errors in a "how to recover" line.  Users
coming from those tools expect it; users who haven't seen it before
discover an out without having to read docs.

Migration strategy
------------------

Slice UX-B lands the base class, the pretty-printer, and a small
``raise_actionable`` helper.  It migrates the most-commonly-hit error
sites (ref resolution, YAML parse failures inside forge scaffolding)
to the new class.  Remaining ``raise RuntimeError / ValueError`` sites
stay unchanged — they continue to produce plain error output — and can
be converted one at a time as users discover them.  This keeps the
surface of this slice small and reversible.
"""

from __future__ import annotations

__all__ = [
    "ActionableError",
    "FragmentShapeError",
    "LockfileMismatchError",
    "CIStateMismatchError",
    "RefResolutionActionableError",
    "format_actionable_error",
    "print_actionable_error",
    "handle_actionable_error",
]

import logging
import sys
from typing import Any, Optional

LOG = logging.getLogger("fluid.cli.errors")


class ActionableError(Exception):
    """Exception that carries a message + a ``fix:`` hint + optional docs URL.

    Every user-facing raise site in the CLI should prefer this class
    over bare ``RuntimeError`` / ``ValueError``.  The top-level command
    handler catches it, prints a formatted panel, and exits non-zero —
    so callers get actionable output for free without adding a
    try/except at every raise site.

    Parameters
    ----------
    message:
        Human-readable description of what went wrong.  Keep it to one
        sentence — anything longer belongs in the fix line or the docs.
    fix:
        One-line description of the exact command or action the user
        should take next.  Should start with a verb ("Run …", "Delete
        …", "Check …").  This is the whole point of the class.
    docs_url:
        Optional URL to a specific docs section with more context.
    """

    #: Short machine-readable tag printed alongside the error name.
    #: Subclasses override this to give users a stable identifier they
    #: can search in docs or issue trackers.
    code: str = "fluid.error"

    def __init__(
        self,
        message: str,
        *,
        fix: str,
        docs_url: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.docs_url = docs_url

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Concrete subclasses used by the CLI
# ---------------------------------------------------------------------------


class FragmentShapeError(ActionableError):
    """Raised when a contract fragment has the wrong YAML shape.

    Example: a fragment referenced as a sequence element (``builds: -
    $ref: ...``) has a non-mapping root.  The loader cannot splice it
    into the parent contract.
    """

    code = "fluid.fragment.shape"


class LockfileMismatchError(ActionableError):
    """Raised when ``contract.lock.yaml`` is out of date.

    Surfaced by ``fluid bundle --check`` (future slice F2).  The fix
    is always "run `fluid bundle` and commit the updated files".
    """

    code = "fluid.lockfile.mismatch"


class CIStateMismatchError(ActionableError):
    """Raised when a CI file has drifted from its recorded sha256.

    Surfaced by ``_scaffold_ci_pipeline`` when re-running forge on a
    product with hand-edited CI files.  The fix is either to delete
    the edits or to regenerate explicitly.
    """

    code = "fluid.ci.drift"


class RefResolutionActionableError(ActionableError):
    """Wraps the loader's ``RefResolutionError`` with an actionable hint.

    The loader's ``RefResolutionError`` carries only the failing path.
    When raised from a user-facing command (forge / validate / compile),
    callers can convert it to this class to attach a fix line.
    """

    code = "fluid.ref.resolution"


# ---------------------------------------------------------------------------
# Rendering + top-level handler
# ---------------------------------------------------------------------------


def format_actionable_error(exc: ActionableError) -> str:
    """Return a multi-line plain-text rendering of *exc*.

    Used both by the plain-output fallback and by tests that want to
    assert the exact rendered shape without depending on Rich.
    """
    name = type(exc).__name__
    code = getattr(type(exc), "code", "fluid.error")
    lines = [
        f"❌ {name}: {exc.message}",
        f"   Fix: {exc.fix}",
    ]
    if exc.docs_url:
        lines.append(f"   Docs: {exc.docs_url}")
    lines.append(f"   Code: {code}")
    return "\n".join(lines)


def print_actionable_error(
    exc: ActionableError,
    *,
    console: Any = None,
) -> None:
    """Print *exc* to *console* (Rich panel) or fall back to plain text.

    The function never raises — a failure in the printer path is
    printed to stderr as plain text so the user still sees the error.
    """
    try:
        if console is not None:
            try:
                from rich.panel import Panel
                from rich.text import Text

                name = type(exc).__name__
                code = getattr(type(exc), "code", "fluid.error")
                body = Text()
                body.append(exc.message + "\n", style="bold red")
                body.append("\nFix: ", style="dim")
                body.append(exc.fix + "\n", style="bold")
                if exc.docs_url:
                    body.append("Docs: ", style="dim")
                    body.append(f"{exc.docs_url}\n", style="blue underline")
                body.append("Code: ", style="dim")
                body.append(f"{code}", style="dim")

                console.print()
                console.print(
                    Panel(
                        body,
                        title=f"[bold red]❌ {name}[/bold red]",
                        border_style="red",
                        padding=(1, 2),
                    )
                )
                console.print()
                return
            except Exception:  # noqa: BLE001 — fall through to plain
                pass

        # Plain fallback
        print(format_actionable_error(exc), file=sys.stderr)
    except Exception:  # noqa: BLE001 — never raise from the printer
        try:
            print(f"ERROR: {exc.message}", file=sys.stderr)
            print(f"Fix: {exc.fix}", file=sys.stderr)
        except Exception:  # pragma: no cover — truly last resort
            pass


def handle_actionable_error(
    exc: ActionableError,
    *,
    console: Any = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Top-level boundary handler.

    Intended usage in any top-level command::

        try:
            ...
        except ActionableError as exc:
            return handle_actionable_error(exc, console=console, logger=logger)

    Logs the error at DEBUG (so ``--verbose`` captures full context),
    prints the actionable panel, and returns exit code 2 (the same
    code argparse uses for invalid usage — a distinct signal from
    exit 1, which the CLI already uses for generic failures).
    """
    log = logger or LOG
    # Python's LogRecord reserves 'message' as a built-in field; passing it
    # via ``extra`` raises KeyError.  Rename to ``error_message`` which is
    # safe and still searchable in structured log backends.
    log.debug(
        "actionable_error",
        extra={
            "code": type(exc).code,
            "error_message": exc.message,
            "fix": exc.fix,
        },
    )
    print_actionable_error(exc, console=console)
    return 2
