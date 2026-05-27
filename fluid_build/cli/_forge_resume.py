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

# ruff: noqa: T201 — interactive prompt module owns its own print() output.
"""Resume detection + prompt for ``fluid forge``.

This is the CLI-side wrapper around the resumability primitive at
:mod:`fluid_build.copilot.checkpoint`. It does three things:

* Auto-detects the most recent paused run in the current workspace.
* Renders the approved prompt:
  ``⏸ Found paused run from 12 min ago at stage 4/7 (builder · $0.04 spent). Continue, start fresh, or see details? [C/f/?]:``
* Returns the chosen ``run_id`` (or ``None`` to start fresh).

Defaults follow the spec:

* TTY + incomplete run + no flag → prompt, default = continue.
* Non-TTY + no env → return None (start fresh — predictable for CI).
* Non-TTY + ``FLUID_FORGE_AUTO_RESUME=1`` → return most-recent.
* ``--no-resume`` → return None (overrides everything).
* ``--resume <id>`` → return ``id`` (no prompt).
* ``--resume`` (no id) → return most-recent (no prompt — the explicit
  flag IS the user's confirmation).
* ``--or-fail`` → if no resumable run, exit 1.

Borrowed pattern: Claude Agent SDK's continue/resume distinction
(continue = most-recent-in-cwd; resume = specific id).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from fluid_build.cli import agents_cmd as _agents_cmd

LOG = logging.getLogger(__name__)

# Canonical stage names — kept in sync with the peer-agent
# fluid_build.copilot.checkpoint.STAGE_NAMES tuple. We define a local
# fallback so this module is importable even before the checkpoint
# primitive lands. The fallback list MUST match
# ``fluid_build.copilot.checkpoint.STAGE_NAMES`` byte-for-byte — see
# the pin test in tests/cli/test_forge_resume_flags.py for the guard.
DEFAULT_STAGE_NAMES: Tuple[str, ...] = (
    "logical",
    "contract_forge",
    "builder",
    "readme",
    "transformation",
    "validator",
    "enrichment",
    "judge",
)


def get_stage_names() -> Tuple[str, ...]:
    """Return the canonical STAGE_NAMES tuple, with local fallback."""
    try:
        from fluid_build.copilot.checkpoint import STAGE_NAMES  # type: ignore[import-not-found]

        return tuple(STAGE_NAMES)
    except Exception:  # noqa: BLE001
        return DEFAULT_STAGE_NAMES


# ---------------------------------------------------------------------------
# Auto-detect + prompt
# ---------------------------------------------------------------------------


class ResumeError(RuntimeError):
    """Raised when --or-fail finds no resumable run."""


def _is_interactive(input_fn: Optional[Callable[[str], str]] = None) -> bool:
    """True when we have a TTY to talk to."""
    if input_fn is not None:
        return True
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _find_resumable(workspace_root: Path) -> List[dict]:
    """Return the list of incomplete runs (paused/failed/running) for cwd."""
    runs = list(_agents_cmd.collect_runs(workspace_root))
    return [r for r in runs if r.get("status") in ("paused", "failed", "running")]


def maybe_prompt_resume(
    args: Any,
    *,
    workspace_root: Optional[Path] = None,
    input_fn: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """Return the run_id to resume, or None to start fresh.

    Behavior matrix (matches the spec docstring at module top):

    * ``--no-resume`` → None (skip detection)
    * ``--resume <id>`` (explicit) → that id
    * ``--resume`` (no id) → most-recent incomplete
    * no flag + TTY + incomplete found → prompt (default = continue)
    * no flag + non-TTY → None unless ``FLUID_FORGE_AUTO_RESUME=1``
    * ``--or-fail`` + no resumable → raise :class:`ResumeError`
    """
    workspace_root = Path(workspace_root or Path.cwd()).resolve()

    no_resume = bool(getattr(args, "no_resume", False))
    resume_arg = getattr(args, "resume", None)
    or_fail = bool(getattr(args, "or_fail", False))

    # --no-resume wins over everything else.
    if no_resume:
        if or_fail:
            raise ResumeError("--or-fail is incompatible with --no-resume")
        return None

    # Explicit --resume <id>: route to that id, no prompt.
    # H16 fix: a missing id is a HARD failure — never silently fall
    # back to fresh. Dropping the flag combined with --from-stage was
    # observed crashing downstream on an EOF reading the interview
    # prompt; the user had no signal that their request was ignored.
    if isinstance(resume_arg, str) and resume_arg:
        runs = _agents_cmd._resolve_run_id(workspace_root, resume_arg)
        if not runs:
            raise ResumeError(
                f"Run {resume_arg!r} not found in .fluid/agents/. "
                "Use 'fluid agents list' to see available runs."
            )
        if len(runs) > 1:
            raise ResumeError(
                f"Ambiguous --resume {resume_arg!r}; candidates: " + ", ".join(p.name for p in runs)
            )
        return runs[0].name

    # --resume with no value: take the most-recent incomplete.
    if resume_arg is None and _has_explicit_resume_flag(args):
        candidates = _find_resumable(workspace_root)
        if not candidates:
            if or_fail:
                raise ResumeError("No resumable run found (--or-fail)")
            print(
                "⚠ No paused / incomplete run found in this workspace.  Starting fresh.",
                file=sys.stderr,
            )
            return None
        return candidates[0]["run_id"]

    # Auto-detect path.
    candidates = _find_resumable(workspace_root)
    if not candidates:
        if or_fail:
            raise ResumeError("No resumable run found (--or-fail)")
        return None

    interactive = _is_interactive(input_fn)
    auto_resume_env = os.environ.get("FLUID_FORGE_AUTO_RESUME") == "1"

    if not interactive:
        if auto_resume_env:
            return candidates[0]["run_id"]
        return None

    # TTY path — prompt the user.
    return _prompt_for_resume(
        candidates,
        workspace_root=workspace_root,
        input_fn=input_fn or input,
    )


def _has_explicit_resume_flag(args: Any) -> bool:
    """True if ``--resume`` appeared on the command line, even bare.

    argparse with ``nargs='?'`` leaves the dest at ``default=None`` when
    the flag is absent but at the ``const`` value when bare. We choose
    a sentinel for ``const`` so we can distinguish "absent" from "bare".
    """
    return getattr(args, "_resume_explicit", False)


def _prompt_for_resume(
    candidates: List[dict],
    *,
    workspace_root: Path,
    input_fn: Callable[[str], str],
) -> Optional[str]:
    """Render the approved prompt and return the chosen run_id or None."""
    most_recent = candidates[0]
    while True:
        prompt = _build_prompt_string(most_recent)
        try:
            raw = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            # Treat Ctrl-C / EOF as "fresh" so the user is never locked
            # into a corrupted run. (Pressing Enter would route to
            # continue, which they can do explicitly if intended.)
            print("\n(aborted — starting fresh)", file=sys.stderr)
            return None
        ans = (raw or "").strip().lower()
        if ans in ("", "c", "continue", "y", "yes"):
            return most_recent["run_id"]
        if ans in ("f", "fresh", "n", "no", "new"):
            return None
        if ans in ("?", "h", "help", "d", "details"):
            _print_candidate_details(candidates, workspace_root=workspace_root)
            # Loop and re-prompt.
            continue
        # Unrecognised — be lenient and loop.
        print(
            "  (press Enter to continue, 'f' for fresh, '?' for details)",
            file=sys.stderr,
        )


def _build_prompt_string(run: dict) -> str:
    """Build the canonical resume prompt — the exact string approved by spec."""
    age = _format_age_human(run.get("age_seconds", 0.0) or 0.0)
    stages_completed = int(run.get("stages_completed") or 0)
    stages_total = int(run.get("stages_total") or 0)
    last_stage = run.get("last_stage") or "—"
    cost = run.get("total_usd")
    cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$0.00"

    if stages_total > 0:
        stage_str = f"stage {stages_completed}/{stages_total}"
    else:
        stage_str = "stage —"

    return (
        f"⏸ Found paused run from {age} ago at {stage_str} "
        f"({last_stage} · {cost_str} spent). "
        f"Continue, start fresh, or see details? [C/f/?]: "
    )


def _print_candidate_details(candidates: List[dict], *, workspace_root: Path) -> None:
    """Print the details block shown when the user picks ``?``."""
    print("\n  Candidates:", file=sys.stderr)
    for c in candidates[:5]:
        age = _format_age_human(c.get("age_seconds", 0.0) or 0.0)
        cost = c.get("total_usd")
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "—"
        stages = (
            f"{c.get('stages_completed', 0)}/{c.get('stages_total', 0)}"
            if c.get("stages_total")
            else "—"
        )
        print(
            f"   • {c['run_id']}  · {age} ago  · {c.get('status', '?')}  · "
            f"stages {stages}  · {cost_str}  · last: {c.get('last_stage') or '—'}",
            file=sys.stderr,
        )
    if len(candidates) > 5:
        print(f"   …and {len(candidates) - 5} more", file=sys.stderr)
    print(
        "\n  Full inspection: fluid agents show <run-id>" "\n  All runs:        fluid agents list",
        file=sys.stderr,
    )


def _format_age_human(seconds: float) -> str:
    """Human age — '12 min', '3 hours', '5 days'. Plural-aware."""
    if seconds < 60:
        n = int(seconds)
        return f"{n} sec" if n != 1 else "1 sec"
    if seconds < 3600:
        n = int(seconds // 60)
        return f"{n} min"
    if seconds < 86400:
        n = int(seconds // 3600)
        return f"{n} hour" + ("s" if n != 1 else "")
    n = int(seconds // 86400)
    return f"{n} day" + ("s" if n != 1 else "")


# ---------------------------------------------------------------------------
# Stage-name typo handling — Did-you-mean
# ---------------------------------------------------------------------------


def validate_from_stage(stage_name: Any) -> Tuple[bool, str]:
    """Check ``--from-stage`` argument; return (ok, message).

    On invalid stage: ``ok=False`` and a Did-you-mean error including
    every canonical stage name. Matches the kubernetes/git CLI shape.

    Defensive type-check: argparse passes ``None`` or ``str``; tests
    sometimes auto-attr a ``MagicMock`` onto the args namespace which
    used to crash deep in ``.lower()``. Fail loud at the boundary.
    """
    if not isinstance(stage_name, str):
        if stage_name is None or stage_name is False:
            return False, "--from-stage requires a stage name"
        return False, f"--from-stage must be a string; got {type(stage_name).__name__}"
    if not stage_name:
        return False, "--from-stage requires a stage name"
    stages = get_stage_names()
    if stage_name in stages:
        return True, ""
    # Did-you-mean — a single substring match is enough.
    candidates = [s for s in stages if stage_name.lower() in s.lower()]
    if not candidates:
        candidates = [s for s in stages if s.lower().startswith(stage_name[:3].lower())]
    hint = ""
    if candidates:
        hint = f"  Did you mean: {', '.join(candidates)}?\n"
    return False, (f"Unknown stage '{stage_name}'.\n" f"{hint}" f"  Valid: {', '.join(stages)}")


# ---------------------------------------------------------------------------
# Startup prune hint
# ---------------------------------------------------------------------------

# Singleton-per-process guard so the hint prints at most once even if
# forge re-enters its bootstrap.
_PRUNE_HINT_PRINTED = False


def _reset_prune_hint_flag() -> None:
    """Reset the per-process prune-hint guard.

    Test-only helper — pytest test pollution surfaces when the flag
    persists across tests in random-order runs. Tests that depend on
    the hint printing call this in setup; tests that don't care don't
    need it.
    """
    global _PRUNE_HINT_PRINTED
    _PRUNE_HINT_PRINTED = False


def maybe_print_prune_hint(
    workspace_root: Optional[Path] = None,
    *,
    threshold: int = 50,
    older_than_days: int = 30,
) -> bool:
    """Print the startup prune hint when conditions warrant.

    Triggers when:

    * More than ``threshold`` (default 50) run directories are older
      than ``older_than_days`` (default 30) days, AND
    * ``FLUID_FORGE_NO_PRUNE_HINT`` is not set.

    Returns True if a hint was printed (used by tests + the welcome
    scan to know whether to add a separator line).
    """
    global _PRUNE_HINT_PRINTED
    if _PRUNE_HINT_PRINTED:
        return False
    if os.environ.get("FLUID_FORGE_NO_PRUNE_HINT") == "1":
        return False

    workspace_root = Path(workspace_root or Path.cwd()).resolve()
    base = workspace_root / ".fluid" / "agents"
    if not base.is_dir():
        return False

    cutoff_seconds = older_than_days * 86400
    old_dirs: List[Path] = []
    total_bytes = 0
    import time as _time

    now = _time.time()
    try:
        for d in base.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                age = now - d.stat().st_mtime
            except OSError:
                continue
            if age < cutoff_seconds:
                continue
            old_dirs.append(d)
            try:
                total_bytes += _quick_dir_size(d)
            except OSError:
                continue
    except OSError:
        return False

    if len(old_dirs) < threshold:
        return False

    _PRUNE_HINT_PRINTED = True
    mb = max(1, total_bytes // (1024 * 1024))
    print(
        f"Tip: fluid agents prune --older-than {older_than_days}d "
        f"to reclaim {mb} MB ({len(old_dirs)} old runs).",
        file=sys.stderr,
    )
    return True


def reset_prune_hint_state() -> None:
    """For tests — clear the once-per-process guard."""
    global _PRUNE_HINT_PRINTED
    _PRUNE_HINT_PRINTED = False


def _quick_dir_size(path: Path) -> int:
    """Lighter than _agents_cmd._dir_size — only descends one level for the hint."""
    total = 0
    for p in path.iterdir():
        try:
            if p.is_file():
                total += p.stat().st_size
            elif p.is_dir():
                # One level deep is enough for an "approximate MB" hint.
                for q in p.iterdir():
                    try:
                        if q.is_file():
                            total += q.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


__all__ = [
    "DEFAULT_STAGE_NAMES",
    "ResumeError",
    "get_stage_names",
    "maybe_print_prune_hint",
    "maybe_prompt_resume",
    "reset_prune_hint_state",
    "validate_from_stage",
]
