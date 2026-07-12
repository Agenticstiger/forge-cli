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
from types import TracebackType
from typing import Any, Callable, Optional

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
    * Any ``Exception`` logs ``logger.exception(fail_label)``, invokes
      ``render_error(console, exc)``, and yields exit code ``1``.
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
            self._logger.exception(self._fail_label)
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
