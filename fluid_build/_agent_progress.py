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

"""Provider-agnostic "thinking" UX for staged LLM calls (tier-0 shared leaf).

This module is a **tier-0 shared leaf** — stdlib-only (``rich`` is imported
lazily inside the render path, degrading to a silent no-op when absent), with
no ``fluid_build.*`` upstreams. It sits below both ``cli`` and ``copilot`` so
``copilot.agents.base`` can wrap its staged LLM calls in the status panel
without importing anything under ``cli`` (the ``copilot -> cli`` edge that the
``[tool.importlinter]`` contracts forbid). The public home used to be
``fluid_build.cli.progress``; that module is now a backwards-compat re-export
shim.

The staged pipeline issues several LLM requests in sequence (modeler →
builder → readme → transformation → validator). Each request can take
tens of seconds; without feedback the CLI looks hung and users
disengage.

:class:`AgentStatus` wraps a single LLM call in a ``rich.Live`` panel
showing which agent is active, which stage it's on, and elapsed time.
It is deliberately *provider-agnostic* — we do **not** try to stream raw
JSON or surface provider-native thinking blocks (those differ per
provider and often aren't exposed). The goal is engagement, not
transparency into model internals.

The panel degrades gracefully:

* no ``rich`` installed → silent no-op
* non-TTY stdout (CI, piping to a file) → silent no-op
* ``FLUID_QUIET=1`` or ``FLUID_NO_TUI=1`` → silent no-op
* ``args.quiet`` propagated by the caller → silent no-op

Usage (wrapping the provider call in ``BaseStageAgent``)::

    with AgentStatus(stage="logical", agent="LogicalAgent",
                     provider="gemini", model="gemini-2.5-pro"):
        result = self._call_once(...)

The context manager returns silently when disabled so callers can wrap
unconditionally.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import AbstractContextManager
from typing import Any, Optional


def _status_disabled() -> bool:
    """Return True when the status panel must not render.

    Checks env vars and TTY-ness. Callers can also pass
    ``enabled=False`` to :class:`AgentStatus` to force-disable (used
    when ``args.quiet`` is set).
    """
    if os.environ.get("FLUID_QUIET") == "1":
        return True
    if os.environ.get("FLUID_NO_TUI") == "1":
        return True
    if os.environ.get("FLUID_NONINTERACTIVE") == "1":
        return True
    if not sys.stdout.isatty():
        return True
    return False


class AgentStatus(AbstractContextManager):
    """Context manager that shows a live "thinking" panel while an LLM call is in flight.

    Parameters
    ----------
    stage:
        Stage name — e.g. ``"logical"``, ``"builder"``. Shown as the
        first column in the panel.
    agent:
        Agent class name — e.g. ``"LogicalAgent"``. Shown second.
    provider:
        Provider name — e.g. ``"gemini"``, ``"openai"``. Shown for
        debugging surface; provider-agnostic otherwise.
    model:
        Model id — e.g. ``"gemini-2.5-pro"``.
    enabled:
        Explicit override. ``None`` (default) means "use env/TTY
        detection via :func:`_status_disabled`". ``False`` forces
        silent mode regardless of env/TTY.
    """

    def __init__(
        self,
        *,
        stage: str,
        agent: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.stage = stage
        self.agent = agent
        self.provider = provider or "?"
        self.model = model or "?"
        self._explicit_enabled = enabled
        self._live: Any = None
        self._started_at = 0.0

    # ------------------------------------------------------------------
    # enablement
    # ------------------------------------------------------------------

    def _is_active(self) -> bool:
        if self._explicit_enabled is False:
            return False
        if self._explicit_enabled is True:
            # Still honor env-level kill switches so CI runs stay clean.
            return not (
                os.environ.get("FLUID_QUIET") == "1" or os.environ.get("FLUID_NO_TUI") == "1"
            )
        return not _status_disabled()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _render(self) -> Any:
        """Build the panel body for the current elapsed time."""
        elapsed = time.time() - self._started_at
        dots = "." * (int(elapsed) % 4)
        from rich.text import Text  # local import — optional dep

        body = Text()
        body.append(f"  {self.agent}", style="bold cyan")
        body.append(f"  ·  {self.stage}", style="dim")
        body.append(f"  ·  {self.provider}/{self.model}", style="dim")
        body.append(f"  ·  thinking{dots}", style="magenta")
        body.append(f"   {elapsed:4.1f}s", style="dim")
        return body

    def _tick(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._render(), refresh=True)
            except Exception:  # noqa: BLE001
                # Never let the status panel break the underlying call.
                pass

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "AgentStatus":
        self._started_at = time.time()
        if not self._is_active():
            return self
        try:
            from rich.live import Live  # local import — optional dep

            self._live = Live(
                self._render(),
                refresh_per_second=4,
                transient=True,
            )
            self._live.__enter__()
        except Exception:  # noqa: BLE001
            # rich not installed or terminal can't host Live — fall back
            # silently. The stage work continues either way.
            self._live = None
        return self

    def tick(self) -> None:
        """Bump the elapsed-time display; safe no-op when disabled."""
        self._tick()

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._live is not None:
            try:
                self._live.__exit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001
                pass
            self._live = None
        # Never swallow the caller's exception.
        return None
