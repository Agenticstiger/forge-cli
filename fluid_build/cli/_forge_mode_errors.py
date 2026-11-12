# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared error boundary for ``forge`` mode handlers.

The interactive ``run_*_mode`` entry points in ``forge_modes.py`` (AI
copilot / domain agent / guided) each wrapped their whole body in the
*same* control-flow skeleton::

    try:
        ...                       # body, with many success ``return`` points
    except KeyboardInterrupt:     # (ai + guided only)
        logger.info(<cancel msg>)
        return 130
    except Exception as exc:      # (all three)
        logger.exception(<fail label>)
        ...                       # render the failure on the console
        return 1

Only three things differed between handlers: the log label, whether
Ctrl-C is trapped (the domain-agent handler lets it propagate), and how
the failure is rendered on the console. This module hoists the identical
skeleton into one reusable context manager and lets each handler pass
those three parameters — so the exit codes (``130`` on cancel, ``1`` on
failure), the ``logger.exception``/``logger.info`` calls, and the
catch ordering live in exactly one place.

Borrow-before-build note: Click centralises CLI error handling through
``ClickException`` + a ``main()`` boundary that maps exceptions to exit
codes (https://click.palletsprojects.com/en/stable/exceptions/), and
Typer layers ``typer.Exit`` on top of the same idea. We mirror that
"one boundary owns the exception -> exit-code mapping" design, but as a
*context manager* rather than a function decorator: ``console`` is a
function-local in each handler, so a boundary created *inside* the
handler (after ``console`` exists) owns only the exception paths and
leaves every success-path ``return`` byte-identical. ``contextlib``
documents exactly this "trap the error / ensure cleanup" use of a
context manager (https://docs.python.org/3/library/contextlib.html).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple

# Render callback: receives ``(console, exc)`` and prints the
# handler-specific failure message. ``console`` may be ``None`` on the
# non-Rich / no-TTY path, so every callback must tolerate that.
RenderError = Callable[[Any, BaseException], None]


class ForgeModeErrorBoundary:
    """Context manager mapping mode-handler exceptions to forge exit codes.

    Usage inside a ``run_*_mode`` handler::

        console = console_factory() if console_factory else None
        with forge_mode_error_boundary(
            logger,
            console,
            fail_label="Guided mode failed",
            render_error=_render,
            cancel_label="Guided mode cancelled",
        ) as boundary:
            ...            # original body, unchanged, with its own returns
            return 0
        return boundary.exit_code

    Semantics, preserving the pre-refactor per-handler behaviour exactly:

    * ``KeyboardInterrupt`` is trapped **only** when ``cancel_label`` is
      set: ``logger.info(cancel_label)`` then exit code ``130``. When
      ``cancel_label`` is ``None`` the interrupt propagates unchanged
      (the domain-agent handler never caught Ctrl-C).
    * Any ``Exception`` is logged through :func:`log_forge_failure` (a crash
      as ``logger.exception(fail_label)``, a FLUID error with its traceback
      at DEBUG only), invokes ``render_error(console, exc)``, and yields exit
      code ``1``.
    * ``SystemExit`` / ``GeneratorExit`` and other non-``Exception``
      ``BaseException``\\ s propagate untouched — matching a bare
      ``except Exception`` clause, which never caught them either.

    The success path never touches ``exit_code``; the handler returns
    from *inside* the ``with`` block exactly as it did before, so no
    success-path return value or ordering changes.
    """

    def __init__(
        self,
        logger: logging.Logger,
        console: Any,
        *,
        fail_label: str,
        render_error: RenderError,
        cancel_label: Optional[str] = None,
    ) -> None:
        self._logger = logger
        self._console = console
        self._fail_label = fail_label
        self._render_error = render_error
        self._cancel_label = cancel_label
        # Populated only when the boundary swallowed an exception; the
        # handler reads it after the ``with`` block.
        self.exit_code: Optional[int] = None

    def __enter__(self) -> "ForgeModeErrorBoundary":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        # No exception: clean success path — nothing to do, don't suppress.
        if exc is None:
            return False

        # Ctrl-C: mirror ``except KeyboardInterrupt`` when the handler
        # opted in (``cancel_label`` set); otherwise let it propagate.
        if isinstance(exc, KeyboardInterrupt):
            if self._cancel_label is None:
                return False
            self._logger.info(self._cancel_label)
            self.exit_code = 130
            return True

        # Any other ``Exception`` maps to the shared failure path. Note
        # ``KeyboardInterrupt``/``SystemExit``/``GeneratorExit`` are
        # ``BaseException`` (not ``Exception``) and so fall through to
        # ``return False`` below — exactly as a bare ``except Exception``
        # would leave them uncaught.
        if isinstance(exc, Exception):
            # Without a console a renderer may print nothing, so the log line
            # is then the only place the failure shows.
            log_forge_failure(self._logger, self._fail_label, exc, shown=self._console is not None)
            self._render_error(self._console, exc)
            self.exit_code = 1
            return True

        return False


def forge_mode_error_boundary(
    logger: logging.Logger,
    console: Any,
    *,
    fail_label: str,
    render_error: RenderError,
    cancel_label: Optional[str] = None,
) -> ForgeModeErrorBoundary:
    """Build a :class:`ForgeModeErrorBoundary` (lowercase ``open()``-style entry point)."""
    return ForgeModeErrorBoundary(
        logger,
        console,
        fail_label=fail_label,
        render_error=render_error,
        cancel_label=cancel_label,
    )


# ---------------------------------------------------------------------------
# Why a forge run failed: ``log_forge_failure`` and the ``--agent`` event
# ---------------------------------------------------------------------------
#
# A mode handler catches its own failure, prints it and returns an exit code,
# so by the time ``forge.run`` writes ``forge.done`` the exception is gone.
# Each handler that swallows one hands it to :func:`log_forge_failure`, which
# notes it here; ``forge.run`` reads the note back and, under ``--agent``,
# writes it as one ``forge.error`` event. A ``ContextVar``, not a module global:
# the MCP ``forge_run`` runs forge in a worker thread, and a run reads only its
# own note.

_RUN_FAILURE: ContextVar[Optional[BaseException]] = ContextVar(
    "fluid_forge_run_failure", default=None
)

#: ``forge.error``'s ``code`` for an exception that is not a FLUID error.
UNEXPECTED_FAILURE_CODE = "forge_unexpected_error"


def note_forge_failure(exc: BaseException) -> None:
    """Remember *exc* as why this forge run is failing; a later failure replaces it."""
    _RUN_FAILURE.set(exc)


def take_forge_failure() -> Optional[BaseException]:
    """Return the failure noted for this run, and forget it."""
    exc = _RUN_FAILURE.get()
    _RUN_FAILURE.set(None)
    return exc


def is_typed_forge_error(exc: BaseException) -> bool:
    """Whether *exc* is a FLUID error, written for the user, rather than a crash.

    ``CLIError`` (``CopilotGenerationError``, ``ForgeError`` and the rest),
    ``FluidError``, ``FluidUserError`` and a missing optional extra each say what
    went wrong and, mostly, what to do next. Anything else is a bug.
    """
    from fluid_build._contract_loader import CLIError
    from fluid_build._errors import FluidUserError
    from fluid_build._optional import MissingOptionalDependency
    from fluid_build.errors import FluidError

    return isinstance(exc, (CLIError, FluidError, FluidUserError, MissingOptionalDependency))


def _failure_parts(exc: BaseException) -> Tuple[str, str, List[str], Optional[str]]:
    """``(code, message, suggestions, docs_url)`` for *exc*, not yet redacted."""
    import re

    from fluid_build._contract_loader import CLIError
    from fluid_build._errors import FluidUserError
    from fluid_build._optional import MissingOptionalDependency
    from fluid_build.errors import FluidError

    if isinstance(exc, MissingOptionalDependency):
        # ``str(exc)`` runs the message, the install line and any alternatives
        # together as paragraphs; the event keeps them apart.
        hints = [f"pip install 'data-product-forge[{exc.extra}]'"]
        if exc.alternatives:
            hints.append(exc.alternatives)
        message = f"{exc.feature} needs {exc.package}, which is not installed."
        return exc.event, message, hints, None
    if isinstance(exc, CLIError):
        return exc.event, str(exc.message), list(exc.suggestions or []), exc.docs_url
    if isinstance(exc, FluidUserError):
        return exc.code, exc.what, [exc.fix] if exc.fix else [], exc.doc or None
    if isinstance(exc, FluidError):
        # No event key on this family: the class name, snake_cased like one.
        code = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
        return code, exc.message, list(exc.suggestions or []), None
    return (
        UNEXPECTED_FAILURE_CODE,
        f"forge stopped on an unexpected {type(exc).__name__}; stderr has the detail",
        ["Run 'fluid doctor' to check the environment", "Re-run with --debug for the full log"],
        None,
    )


def describe_forge_failure(exc: BaseException) -> Dict[str, Any]:
    """``forge.error``'s fields for *exc*: ``code``, ``message``, ``suggestions``.

    The shape of ``fluid ai test --json``'s ``error`` object, plus ``docs_url``
    when the error catalog has one. The text is redacted as the console redacts
    it, since the events go to stdout directly, past the logging redactor. An
    exception that is not a FLUID error is named by its class (``exception``),
    never by its text: that can carry paths or provider detail, and the MCP
    ``forge_run`` returns these events to the IDE's model, where the copilot
    tools' typed-error rule (SECURITY_REVIEW I7) applies. stderr has the detail.
    """
    from fluid_build._console import _redact_error_str

    code, message, suggestions, docs_url = _failure_parts(exc)
    fields: Dict[str, Any] = {
        "code": code,
        "message": _redact_error_str(message),
        "suggestions": [_redact_error_str(s) for s in suggestions],
    }
    if docs_url:
        fields["docs_url"] = docs_url
    if not is_typed_forge_error(exc):
        fields["exception"] = type(exc).__name__
    return fields


def log_forge_failure(
    logger: logging.Logger, label: str, exc: BaseException, *, shown: bool = True
) -> None:
    """Log a failure that its caller prints for the user, and note it for ``--agent``.

    A FLUID error (:func:`is_typed_forge_error`) is an outcome with its own
    message and next steps, not a crash. It gets one ERROR record (*label* and
    its code) with no traceback, and the traceback at DEBUG. The ERROR record
    reaches ``--log-file`` but skips the console when the caller prints the
    message there itself (*shown*): the CLI's console handler drops a record
    marked ``fluid_console_shown``. Anything else keeps ``logger.exception``,
    whose traceback is the bug report.

    Call it while *exc* is being handled: in its ``except`` block, or in the
    ``__exit__`` of a context manager that it is unwinding.
    """
    note_forge_failure(exc)
    if not is_typed_forge_error(exc):
        logger.exception(label)
        return
    code = _failure_parts(exc)[0]
    logger.error("%s: %s", label, code, extra={"fluid_console_shown": shown})
    logger.debug("%s: %s", label, code, exc_info=exc)
