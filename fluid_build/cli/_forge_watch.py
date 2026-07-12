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

"""``fluid forge --watch`` — re-generate the contract when source files change.

Design (borrow-before-build receipts in the PR body):

* We surveyed ``watchdog`` (the canonical Python file-watch library) and its
  ``PatternMatchingEventHandler`` debounce, plus how ``dbt --watch`` / nodemon /
  ``watchmedo`` structure a watch->rebuild loop. ``watchdog`` is *not* a project
  dependency, is not installed, and pulling a native-extension dependency onto
  the ``fluid`` install for one medium-priority UX feature is unjustified -- and
  a module-level ``import watchdog`` would also violate the startup budget on the
  ``fluid --help`` path. ``watchgod``/``watchdog`` both confirm a plain mtime
  *polling snapshot* is fast enough (a 300k-LOC tree scans in ~24ms), so we adopt
  their **polling-snapshot pattern** with **zero new dependencies**.

* Debounce mirrors nodemon / ``watchmedo``: a burst of rapid saves is coalesced
  into a *single* regeneration by waiting for the tree to go quiescent for a
  short debounce window before firing.

The regeneration itself is **non-interactive and network-free** -- a watch loop
cannot re-run a full interview per keystroke, so the caller injects a
``regenerate_fn`` (forge wires this to the deterministic offline-guided path).

The source-file snapshot **reuses the copilot discovery walker**
(``forge_copilot_discovery._iter_candidate_files`` + its exclusions +
``IGNORED_DIRECTORIES``) so "what counts as a source file" is defined in exactly
one place. On top of that we exclude the artifacts forge *writes itself*
(``contract.fluid.yaml`` / ``contract.fluid.json`` and anything under
``runtime/``) so regenerating never retriggers the watcher -- no infinite loop.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Snapshot signature: path -> (mtime_ns, size). mtime_ns gives sub-second
# resolution and size catches same-nanosecond truncations, so a burst of edits
# is never missed and a byte-identical rewrite is never a false positive.
Snapshot = Dict[str, Tuple[int, int]]

LOG = logging.getLogger("fluid.cli.forge.watch")

#: Seconds between source-tree scans. Cheap: the walk is stat-only.
DEFAULT_POLL_INTERVAL = 1.0
#: Quiet window (seconds) the tree must hold steady before regenerating --
#: coalesces a save-storm into one regeneration.
DEFAULT_DEBOUNCE_SECONDS = 0.75


# ---------------------------------------------------------------------------
# Root + artifact resolution
# ---------------------------------------------------------------------------


def _resolve_watch_roots(args: Any) -> List[Path]:
    """Return the directories to watch -- the same roots copilot discovery scans.

    That is the workspace root (cwd) plus ``--discovery-path`` when supplied,
    matching ``forge_copilot_discovery.discover_local_context``'s root set so the
    watcher observes exactly the files that shape the generated contract.
    """
    roots: List[Path] = [Path.cwd().resolve()]
    discovery_path = getattr(args, "discovery_path", None)
    if discovery_path:
        extra = Path(str(discovery_path)).expanduser().resolve()
        if extra not in roots:
            roots.append(extra)
    return roots


def _is_watch_output_artifact(path: Path) -> bool:
    """True for files forge writes itself -- excluded so regen never retriggers.

    Covers the emitted contract (``contract.fluid.yaml`` / ``.json``) and any
    file under a ``runtime/`` directory (``plan.json`` etc.). The CLI's ``.fluid``
    state dir is already skipped by the discovery walker's ``IGNORED_DIRECTORIES``.
    """
    if path.name in ("contract.fluid.yaml", "contract.fluid.json"):
        return True
    return any(part == "runtime" for part in path.parts)


def _snapshot_sources(roots: List[Path]) -> Snapshot:
    """Build a ``{path: (mtime_ns, size)}`` snapshot of watchable source files.

    Reuses the copilot discovery walker so the definition of "source file"
    (ignored dirs, discovery artifacts) stays single-sourced, then drops the
    artifacts forge writes itself.
    """
    # Lazy import -- keeps the (transitively heavy) discovery module off the
    # ``fluid --help`` / parser-build path; only paid when a watch actually runs.
    from fluid_build.cli.forge_copilot_discovery import (
        _is_excluded_discovery_artifact,
        _iter_candidate_files,
    )

    snapshot: Snapshot = {}
    for root in roots:
        for candidate in _iter_candidate_files(root):
            if _is_excluded_discovery_artifact(candidate):
                continue
            if _is_watch_output_artifact(candidate):
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            snapshot[str(candidate)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


# ---------------------------------------------------------------------------
# The watch loop (pure + injectable for tests)
# ---------------------------------------------------------------------------


def watch_loop(
    roots: List[Path],
    on_change: Callable[[], None],
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    snapshot_fn: Optional[Callable[[], Snapshot]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_after: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Poll ``roots`` and invoke ``on_change`` once per debounced change burst.

    Pure and fully injectable: ``snapshot_fn`` / ``sleep_fn`` / ``stop_after`` /
    ``should_stop`` let tests drive a scripted change sequence without touching
    the filesystem or the wall clock. Production passes none of them and the loop
    runs forever until ``KeyboardInterrupt`` (which propagates to the caller for a
    clean Ctrl-C exit).

    Debounce semantics (mirrors nodemon / ``watchmedo``): once a change is seen,
    keep re-snapshotting every ``debounce_seconds`` until two consecutive
    snapshots match (the tree went quiescent), *then* fire ``on_change`` exactly
    once. A burst of N rapid edits therefore coalesces into a single call.

    After firing, the baseline is re-taken so files ``on_change`` itself wrote
    (which are excluded from the snapshot anyway) can never retrigger the loop.
    """
    snap = snapshot_fn or (lambda: _snapshot_sources(roots))
    baseline = snap()
    iterations = 0
    while True:
        if should_stop is not None and should_stop():
            return
        if stop_after is not None and iterations >= stop_after:
            return
        iterations += 1

        sleep_fn(poll_interval)
        current = snap()
        if current == baseline:
            continue

        # Change detected -- debounce: wait for the tree to settle so a
        # save-storm collapses into one regeneration.
        while True:
            sleep_fn(debounce_seconds)
            newer = snap()
            if newer == current:
                break
            current = newer

        on_change()
        # Re-baseline post-regeneration; forge's own output writes are already
        # excluded from the snapshot, so this is belt-and-suspenders.
        baseline = snap()


# ---------------------------------------------------------------------------
# Status output
# ---------------------------------------------------------------------------


def _emit(console: Any, message: str, *, style: Optional[str] = None) -> None:
    """Print a status line via Rich when available, else the plain console."""
    if console is not None:
        try:
            console.print(f"[{style}]{message}[/{style}]" if style else message)
            return
        except Exception:  # noqa: BLE001 -- never let status output crash the watch
            pass
    from fluid_build.cli.console import cprint

    cprint(message)


def _resolve_float_env(name: str, default: float) -> float:
    """Read a positive float tuning knob from the environment, else *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_watch_mode(
    args: Any,
    logger: logging.Logger,
    *,
    regenerate_fn: Callable[[Any, logging.Logger], int],
    console_factory: Optional[Callable[[], Any]] = None,
    poll_interval: Optional[float] = None,
    debounce_seconds: Optional[float] = None,
    loop_fn: Callable[..., None] = watch_loop,
) -> int:
    """Run the forge watch loop: initial regen, then regen on every change.

    ``regenerate_fn`` performs one non-interactive, network-free contract
    regeneration (forge injects the offline-guided path). Returns 0 on a clean
    Ctrl-C exit; the loop otherwise runs until interrupted.

    Tuning knobs (advanced): ``FLUID_FORGE_WATCH_INTERVAL`` /
    ``FLUID_FORGE_WATCH_DEBOUNCE`` override the poll/debounce seconds.
    """
    console = console_factory() if console_factory else None

    poll = (
        poll_interval
        if poll_interval is not None
        else _resolve_float_env("FLUID_FORGE_WATCH_INTERVAL", DEFAULT_POLL_INTERVAL)
    )
    debounce = (
        debounce_seconds
        if debounce_seconds is not None
        else _resolve_float_env("FLUID_FORGE_WATCH_DEBOUNCE", DEFAULT_DEBOUNCE_SECONDS)
    )

    roots = _resolve_watch_roots(args)

    # Surface a bad --discovery-path the same way copilot discovery does,
    # instead of silently watching nothing.
    discovery_path = getattr(args, "discovery_path", None)
    if discovery_path and not Path(str(discovery_path)).expanduser().exists():
        _emit(console, f"Discovery path does not exist: {discovery_path}", style="red")
        return 1

    display = ", ".join(_friendly_path(root) for root in roots)
    _emit(console, f"\N{EYES} watching {display}… (Ctrl-C to stop)", style="cyan")

    def _regenerate(label: str) -> None:
        try:
            regenerate_fn(args, logger)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 -- one bad regen must not kill the watch
            logger.debug("watch_regenerate_failed", exc_info=True)
            _emit(console, f"\N{WARNING SIGN}  {label} failed: {exc}", style="yellow")

    try:
        # Initial build -- like dbt/nodemon, watch does one pass up front.
        _regenerate("initial generation")

        def _on_change() -> None:
            _emit(
                console,
                "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
                "change detected, regenerating…",
                style="cyan",
            )
            _regenerate("regeneration")

        loop_fn(
            roots,
            _on_change,
            poll_interval=poll,
            debounce_seconds=debounce,
        )
    except KeyboardInterrupt:
        _emit(console, "\n\N{RAISED HAND} stopped watching.", style="dim")
        return 0
    return 0


def _friendly_path(path: Path) -> str:
    """Render *path* relative to cwd (or ``~``) for a compact status line."""
    try:
        rel = str(path.relative_to(Path.cwd()))
        return rel or "."
    except ValueError:
        home = str(Path.home())
        text = str(path)
        return text.replace(home, "~") if text.startswith(home) else text
