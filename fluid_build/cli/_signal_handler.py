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

"""SIGINT handler for ``fluid forge`` — Ctrl-C → save + exit 130.

The handler is **deliberately minimal**: it sets a `.paused` JSON
marker under ``.fluid/agents/<run-id>/`` (the file ``fluid agents list``
reads to surface "paused" status) and prints a single resume hint line.
The handler is idempotent — a second SIGINT is a no-op rather than a
crash, mirroring the safe-shutdown pattern documented in the Python
``signal`` module docs.

The mark-paused write lands directly in the handler because:

* the data is small (a single ~200-byte JSON marker),
* it's idempotent (writes to a fixed path, no append),
* and skipping it on the rare race-condition crash is acceptable — the
  user still sees the resume hint and the existing on-disk artifact
  stack (cost.json / reasoning.md / transcript.json) is unaffected.

Borrowed: Python signal module's documented "minimal work in handler,
set a flag, do the heavy work in the main loop" pattern. The flag
itself lives in the marker file rather than in process memory so that
even ``os._exit`` paths don't lose it.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger(__name__)

# SIGINT exit code per POSIX: 128 + signal_number; SIGINT == 2 → 130.
SIGINT_EXIT_CODE = 130

# Global re-entrance guard. Module-level so a second SIGINT inside the
# same process can early-out cleanly even if the handler ran via
# threading.Thread on a worker.
_HANDLER_FIRED = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_paused_marker(
    run_dir: Path,
    *,
    current_stage: int = 0,
    stages_total: int = 0,
    stage_name: str = "",
    cost_so_far: float = 0.0,
    extra: Optional[dict] = None,
) -> Path:
    """Write ``.paused`` JSON marker under ``run_dir``.

    Returns the marker path. Idempotent — overwrites whatever was there
    last time. Best-effort: silently swallows OSError so the SIGINT
    handler never crashes the process en route to exit.
    """
    marker = run_dir / ".paused"
    payload = {
        "paused_at": _now_iso(),
        "current_stage": int(current_stage),
        "stages_total": int(stages_total),
        "stages_completed": max(0, int(current_stage) - 1),
        "last_stage": stage_name,
        "cost_so_far_usd": float(cost_so_far),
    }
    if extra:
        payload.update(extra)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(".paused.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(marker)
    except OSError as exc:  # noqa: BLE001 — never crash in a signal handler
        LOG.debug("paused_marker_write_failed: %s", exc)
    return marker


def _default_resume_hint(
    *,
    run_id: str,
    current_stage: int,
    stages_total: int,
    stage_name: str,
    age_seconds: float,
    cost_so_far: float,
) -> str:
    """Build the multi-line resume hint printed to stderr on Ctrl-C."""
    age_str = _format_age(age_seconds)
    stages_str = f"stage {current_stage}/{stages_total}" if stages_total else "running"
    stage_detail = f" ({stage_name})" if stage_name else ""
    return (
        f"\n⏸  Paused at {stages_str}{stage_detail} · {age_str} in · "
        f"${cost_so_far:.2f} spent\n"
        f"   Resume:   fluid forge          (next invocation here auto-detects)\n"
        f"   Discard:  fluid agents prune --run-id {run_id}\n"
    )


def install_pause_handler(
    *,
    run_id: str,
    run_dir: Path,
    get_state: Callable[[], dict],
    saver: Any = None,
    exit_fn: Optional[Callable[[int], None]] = None,
) -> Callable[[int, Any], None]:
    """Install a SIGINT handler that writes ``.paused`` and exits 130.

    Parameters
    ----------
    run_id
        The run id (so the resume / discard hints can reference it).
    run_dir
        Directory under which to write the ``.paused`` marker — usually
        ``<target_dir>/.fluid/agents/<run_id>``.
    get_state
        Callable returning a dict with the latest progress snapshot.
        Keys read: ``current_stage`` (int), ``stages_total`` (int),
        ``stage_name`` (str), ``age_seconds`` (float), ``cost_so_far``
        (float). Anything missing defaults to 0/"". Called inside the
        handler, so keep it cheap (no IO, no locks).
    saver
        Optional :class:`CheckpointStore`. If present and exposes
        ``mark_paused(run_id)``, we call it first. The local
        ``.paused`` marker is **always** written — it's the authoritative
        source for ``fluid agents list``'s "paused" status.
    exit_fn
        Override ``sys.exit`` for tests. Defaults to ``sys.exit``.

    Returns the installed handler so callers can re-install it manually
    if needed (or detach via ``signal.signal(signal.SIGINT, signal.SIG_DFL)``).
    """
    _exit = exit_fn or sys.exit

    def _on_sigint(signum: int, frame: Any) -> None:
        # Re-entrance guard: a second Ctrl-C is a no-op (but still exits).
        if _HANDLER_FIRED.is_set():
            try:
                _exit(SIGINT_EXIT_CODE)
            except SystemExit:
                raise
            return
        _HANDLER_FIRED.set()

        # 1. Snapshot the progress (cheap dict read).
        try:
            state = get_state() or {}
        except Exception as exc:  # noqa: BLE001
            LOG.debug("paused_state_snapshot_failed: %s", exc)
            state = {}

        current_stage = int(state.get("current_stage", 0) or 0)
        stages_total = int(state.get("stages_total", 0) or 0)
        stage_name = str(state.get("stage_name", "") or "")
        age_seconds = float(state.get("age_seconds", 0.0) or 0.0)
        cost_so_far = float(state.get("cost_so_far", 0.0) or 0.0)

        # 2. Tell the saver (if it has the optional mark_paused method).
        if saver is not None and hasattr(saver, "mark_paused"):
            try:
                saver.mark_paused(run_id)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("saver_mark_paused_failed: %s", exc)

        # 3. Write the local .paused marker — the always-reliable source.
        try:
            write_paused_marker(
                run_dir,
                current_stage=current_stage,
                stages_total=stages_total,
                stage_name=stage_name,
                cost_so_far=cost_so_far,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.debug("paused_marker_failed: %s", exc)

        # 4. Print the resume hint to stderr (so progress prints on
        # stdout don't get intermixed with the resume hint).
        try:
            sys.stderr.write(
                _default_resume_hint(
                    run_id=run_id,
                    current_stage=current_stage,
                    stages_total=stages_total,
                    stage_name=stage_name,
                    age_seconds=age_seconds,
                    cost_so_far=cost_so_far,
                )
            )
            sys.stderr.flush()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("paused_hint_print_failed: %s", exc)

        # 5. Exit 130 (POSIX convention for SIGINT-terminated processes).
        try:
            _exit(SIGINT_EXIT_CODE)
        except SystemExit:
            raise

    signal.signal(signal.SIGINT, _on_sigint)
    return _on_sigint


def reset_handler_state() -> None:
    """Reset the re-entrance guard. For tests only."""
    _HANDLER_FIRED.clear()


def _format_age(seconds: float) -> str:
    """Compact age — '12m', '3h', '5d'. Mirrors agents_cmd."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


__all__ = [
    "SIGINT_EXIT_CODE",
    "install_pause_handler",
    "reset_handler_state",
    "write_paused_marker",
]
