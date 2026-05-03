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

"""Pre-write preview — the centerpiece UX surface for ``fluid forge``.

Lands in front of every authoring path (forge --ai, forge --blank,
init --template, forge --refine, from_data_products) and answers
three questions before a single byte hits the user's repo:

* **What** is about to land — the file list with sizes;
* **What did it cost** — total USD, tokens, wall-clock seconds;
* **What did the agent decide** — pointers to ``reasoning.md``,
  ``transcript.json``, ``cost.json``, and ``forge-receipt.json``
  under ``.fluid/agents/<run-id>/``.

Invariants this module delivers:

* **I1** Authoring is interruptible — every artifact is written
  BEFORE the confirmation prompt. Ctrl-C at the prompt loses nothing
  on disk; hitting ``n`` cleans up the run directory.
* **I4** Cost is visible before it's spent — the dollar+token line is
  rendered next to the file list; ``--yes`` does not skip rendering,
  only the prompt.
* **I5** Every decision is reproducible — the four artifacts under
  ``.fluid/agents/<run-id>/`` plus ``.fluid/forge-receipt.json``
  capture enough context for a year-from-now replay.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

LOG = logging.getLogger(__name__)


def _redact(text: Any) -> Any:
    """Pass strings through the central secret redactor; leave other types untouched.

    Defensive — the secret redactor is the single source of truth for
    what shapes count as a secret. We use it on transcript.json /
    reasoning.md content so an LLM tool-call that received a URL with
    embedded credentials does not write those credentials to disk.
    """
    if not isinstance(text, str):
        return text
    try:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return redact_secret_text(text)
    except Exception:  # noqa: BLE001 — never block writes on the redactor
        return text


def _redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside JSON-shaped data."""
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_obj(v) for v in obj)
    return obj


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Compact, sortable, human-readable run id.

    Format: ``YYYYMMDD-HHMMSS-<6 hex>``. Sorts in chronological order
    so listings under ``.fluid/agents/`` make sense at a glance.
    """
    import secrets

    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{now}-{secrets.token_hex(3)}"


def run_dir_for(target_dir: str | Path, run_id: str) -> Path:
    """Return the per-run artifact directory: ``<target>/.fluid/agents/<run_id>``."""
    return Path(target_dir) / ".fluid" / "agents" / run_id


# ---------------------------------------------------------------------------
# Records that ride alongside the panel
# ---------------------------------------------------------------------------


@dataclass
class PendingFile:
    """One file the panel intends to write."""

    relpath: str
    """Path relative to ``target_dir``. Forward-slashes only, no drive prefix."""

    content: str
    """Full body to be written. Sized at preview time so the panel can
    show kilobytes without re-reading later."""

    size_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.size_bytes:
            self.size_bytes = len(self.content.encode("utf-8"))


@dataclass
class CostSnapshot:
    """Frozen cost view rendered into the panel and persisted as ``cost.json``."""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_usd: Optional[float] = None
    wall_clock_seconds: float = 0.0
    unknown_models: List[str] = field(default_factory=list)
    cumulative_usd: Optional[float] = None
    """Cumulative USD across the whole process (vs ``total_usd`` for this run)."""

    @classmethod
    def empty(cls) -> "CostSnapshot":
        return cls()

    @property
    def has_data(self) -> bool:
        return bool(self.total_tokens or self.total_usd)


@dataclass
class ReceiptDecision:
    """One row in the forge receipt — what the user (or agent) decided."""

    key: str
    value: Any
    source: str = "user"  # user / inferred / default / agent
    rationale: str = ""


@dataclass
class PreviewPanel:
    """All data the pre-write preview needs to render.

    Constructed by the runtime *before* the prompt; immutable from the
    user's perspective once :meth:`render` runs. The artifact stack
    under ``.fluid/agents/<run-id>/`` is materialised by
    :meth:`persist_artifacts`.
    """

    run_id: str
    target_dir: Path
    files: List[PendingFile] = field(default_factory=list)
    cost: CostSnapshot = field(default_factory=CostSnapshot.empty)
    decisions: List[ReceiptDecision] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    tools_called: List[str] = field(default_factory=list)
    reasoning_markdown: str = ""
    transcript: List[Mapping[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ----- artifact paths ---------------------------------------------

    @property
    def run_dir(self) -> Path:
        return run_dir_for(self.target_dir, self.run_id)

    @property
    def receipt_path(self) -> Path:
        return Path(self.target_dir) / ".fluid" / "forge-receipt.json"

    # ----- mutators (used by the runtime as the run progresses) -------

    def add_file(self, relpath: str, content: str) -> None:
        self.files.append(PendingFile(relpath=relpath, content=content))

    def add_decision(
        self, key: str, value: Any, *, source: str = "user", rationale: str = ""
    ) -> None:
        self.decisions.append(
            ReceiptDecision(key=key, value=value, source=source, rationale=rationale)
        )

    def add_assumption(self, text: str) -> None:
        self.assumptions.append(text)

    def add_tool_call(self, tool_name: str) -> None:
        self.tools_called.append(tool_name)

    def append_reasoning(self, chunk: str) -> None:
        if not chunk:
            return
        # Redact obvious secret shapes (bearer tokens, API keys, JWTs).
        # The on-disk reasoning.md persists across runs so this guard
        # prevents an LLM that quoted a credential back from leaking
        # it to the workspace.
        self.reasoning_markdown += _redact(chunk)
        if not self.reasoning_markdown.endswith("\n"):
            self.reasoning_markdown += "\n"

    def append_transcript(self, event: Mapping[str, Any]) -> None:
        # Recursively redact strings — tool inputs commonly carry URIs
        # that include embedded credentials in the userinfo segment.
        self.transcript.append(_redact_obj(dict(event)))

    # ----- artifact materialisation -----------------------------------

    def persist_artifacts(self) -> None:
        """Write every artifact under ``.fluid/agents/<run-id>/``.

        Always writes; safe to call multiple times. Per-iteration callers
        should invoke this on every agent loop tick so Ctrl-C anywhere
        leaves a recoverable record on disk (invariant **I1**).

        Uses ``default=str`` for cost.json/transcript.json so unexpected
        non-JSON types (e.g. MagicMock objects from mocked-provider test
        runs) round-trip as their string representation rather than
        crashing the run.
        """
        run_dir = self.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        # cost.json — the dollar/token receipt
        _atomic_write(
            run_dir / "cost.json",
            json.dumps(asdict(self.cost), indent=2, sort_keys=True, default=str),
        )
        # reasoning.md — what the agent thought through
        _atomic_write(run_dir / "reasoning.md", self.reasoning_markdown or "")
        # transcript.json — every event the runtime emitted
        _atomic_write(
            run_dir / "transcript.json",
            json.dumps(list(self.transcript), indent=2, sort_keys=False, default=str),
        )

    def write_receipt(self) -> None:
        """Write ``.fluid/forge-receipt.json`` — the durable decision log.

        Receipt contents persist across runs; later authoring paths
        consume this file to avoid re-asking what the user already
        answered (invariant **I3**).
        """
        receipt = {
            "run_id": self.run_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "files_written": [{"path": f.relpath, "size_bytes": f.size_bytes} for f in self.files],
            "decisions": [asdict(d) for d in self.decisions],
            "assumptions": list(self.assumptions),
            "tools_called": list(self.tools_called),
            "cost": asdict(self.cost),
            **(dict(self.extra) if self.extra else {}),
        }
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.receipt_path, json.dumps(receipt, indent=2, sort_keys=True))

    def commit_files(self) -> List[Path]:
        """Materialise every pending file under ``target_dir``.

        Returns the list of absolute paths written. Existing files are
        overwritten — the runtime is responsible for asking earlier in
        the flow if that's not what the user wants.
        """
        written: List[Path] = []
        for pending in self.files:
            dest = (Path(self.target_dir) / pending.relpath).resolve()
            target_root = Path(self.target_dir).resolve()
            try:
                dest.relative_to(target_root)
            except ValueError as exc:
                raise PreviewError(
                    f"Refusing to write outside target directory: {pending.relpath!r}"
                ) from exc
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dest, pending.content)
            written.append(dest)
        return written

    def cleanup_run_dir(self) -> None:
        """Remove the per-run artifact directory.

        Called when the user rejects the preview (``n`` at the prompt).
        ``.fluid/agents/<run-id>/`` is the only thing we wrote BEFORE
        the prompt; cleaning it up keeps "rejected" runs from leaking.
        """
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir, ignore_errors=True)


class PreviewError(RuntimeError):
    """Raised when the preview cannot be safely materialised."""


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically.

    Writes to a sibling tempfile and renames into place so a crash
    mid-write never leaves a half-written ``cost.json``/``transcript.json``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Cost capture — bridge to the upstream tracker
# ---------------------------------------------------------------------------


def capture_cost_snapshot(
    *,
    provider: Any = "",
    model: Any = "",
    started_at: float,
) -> CostSnapshot:
    """Pull a fresh :class:`CostSnapshot` from the process-wide tracker.

    Falls back to zeroed counters if the cost subsystem isn't available
    (e.g. tests that don't import ``fluid_build.copilot.cost``). Coerces
    ``provider`` / ``model`` to strings so callers passing through
    untyped provenance dicts don't break JSON serialisation later.
    """
    snap = CostSnapshot(
        provider=str(provider or ""),
        model=str(model or ""),
        wall_clock_seconds=max(0.0, time.time() - started_at),
    )
    try:
        from fluid_build.copilot.cost import get_run_tracker

        breakdown = get_run_tracker().breakdown()
    except Exception as exc:  # noqa: BLE001 — preview must never crash auth flow
        LOG.debug("preview_panel: cost tracker unavailable: %s", exc)
        return snap

    snap.input_tokens = int(getattr(breakdown, "total_input_tokens", 0) or 0)
    snap.output_tokens = int(getattr(breakdown, "total_output_tokens", 0) or 0)
    snap.total_tokens = snap.input_tokens + snap.output_tokens
    total_usd = getattr(breakdown, "total_usd", None)
    snap.total_usd = float(total_usd) if total_usd is not None else None
    unknown = getattr(breakdown, "unknown_models", None) or []
    snap.unknown_models = list(unknown)
    return snap


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 100:
        return f"{kb:.1f} kB"
    return f"{int(kb)} kB"


def _format_cost_line(cost: CostSnapshot) -> str:
    parts: List[str] = []
    if cost.total_usd is not None:
        parts.append(f"${cost.total_usd:.4f}")
    elif cost.total_tokens or cost.unknown_models:
        parts.append("$? (price not in catalog)")
    else:
        parts.append("$0.0000")
    if cost.total_tokens:
        if cost.total_tokens >= 1000:
            parts.append(f"{cost.total_tokens / 1000:.1f}K tokens")
        else:
            parts.append(f"{cost.total_tokens} tokens")
    if cost.wall_clock_seconds > 0:
        parts.append(f"{cost.wall_clock_seconds:.1f}s")
    return " · ".join(parts)


def _format_provider_line(cost: CostSnapshot) -> str:
    bits = [b for b in (cost.provider, cost.model) if b]
    return " · ".join(bits)


def render(panel: PreviewPanel, *, console: Optional[Any] = None) -> None:
    """Render the preview panel.

    Uses ``rich`` when available; falls back to plain text otherwise so
    the function is safe to call in CI/quiet environments.
    """
    try:
        from rich.console import Console
        from rich.panel import Panel as RichPanel
        from rich.table import Table
        from rich.text import Text
    except Exception:  # noqa: BLE001 — rich is optional
        _render_plain(panel)
        return

    out = console or Console()

    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left")
    table.add_column(justify="right", style="cyan")
    for f in panel.files:
        table.add_row(f.relpath, _format_size(f.size_bytes))
    if not panel.files:
        table.add_row("(no files queued)", "")

    body = Text.assemble(
        ("Would write the following:\n", "bold"),
    )
    body.append(table.__rich_console__ and "" or "")  # no-op for type checker
    cost_line = _format_cost_line(panel.cost)
    provider_line = _format_provider_line(panel.cost)

    out.print(
        RichPanel(
            table,
            title=f"[bold]Preview · run {panel.run_id}[/bold]",
            border_style="cyan",
        )
    )
    out.print(f"[bold]Cost:[/bold] {cost_line}")
    if provider_line:
        out.print(f"[dim]Provider:[/dim] {provider_line}")
    if panel.cost.unknown_models:
        out.print(
            "[yellow]One or more models are not in the price catalog; the dollar "
            "figure above is an estimate.[/yellow]"
        )
    out.print(
        f"[dim]Artifacts:[/dim] {panel.run_dir}/  " "(reasoning.md, transcript.json, cost.json)"
    )
    if panel.assumptions:
        out.print("[dim]Assumptions:[/dim]")
        for a in panel.assumptions[:5]:
            out.print(f"  · {a}")


def _render_plain(panel: PreviewPanel) -> None:
    print(f"\n=== Preview · run {panel.run_id} ===")
    print("Would write the following:")
    for f in panel.files:
        print(f"  {f.relpath}    ({_format_size(f.size_bytes)})")
    if not panel.files:
        print("  (no files queued)")
    print(f"Cost: {_format_cost_line(panel.cost)}")
    provider_line = _format_provider_line(panel.cost)
    if provider_line:
        print(f"Provider: {provider_line}")
    if panel.cost.unknown_models:
        print("Note: one or more models are not in the price catalog; figure is an estimate.")
    print(f"Artifacts: {panel.run_dir}/  " "(reasoning.md, transcript.json, cost.json)")
    if panel.assumptions:
        print("Assumptions:")
        for a in panel.assumptions[:5]:
            print(f"  - {a}")


# ---------------------------------------------------------------------------
# Confirmation flow
# ---------------------------------------------------------------------------


def confirm(
    panel: PreviewPanel,
    *,
    auto_yes: bool = False,
    input_fn: Optional[Any] = None,
) -> bool:
    """Render the panel and ask the user to confirm.

    Returns ``True`` to proceed, ``False`` to abort.

    With ``auto_yes=True`` the prompt is skipped but the panel is still
    rendered — invariant **I4** says cost is always visible, even on
    ``--yes``. The caller is responsible for hooking ``--yes`` to this
    flag (don't bypass ``confirm`` itself).

    When stdin is not a TTY (CI, cron, pipes, tests), the prompt is
    skipped automatically — there is no interactive user to answer it
    and blocking on stdin would hang the run. The panel is still
    rendered so cost stays visible.
    """
    render(panel)
    if auto_yes:
        return True
    if input_fn is None:
        try:
            import sys

            if not sys.stdin.isatty():
                return True
        except Exception:  # noqa: BLE001
            return True
    fn = input_fn or input
    try:
        ans = (
            fn(
                "\nProceed? [Y/n]   "
                "(use --yes to skip · --refine to iterate · :show-work to stream) "
            )
            .strip()
            .lower()
        )
    except (KeyboardInterrupt, EOFError):
        return False
    except OSError:
        # stdin captured / unavailable (pytest, daemon, etc.)
        return True
    if not ans:
        return True
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Completion ritual — what the user sees AFTER confirming
# ---------------------------------------------------------------------------


def render_completion(
    panel: PreviewPanel,
    *,
    next_steps: Sequence[str] = (
        "fluid validate",
        "fluid plan",
        "fluid apply --dry-run",
        "fluid forge --refine",
    ),
    help_url: str = "https://forge.fluid.dev",
    console: Optional[Any] = None,
) -> None:
    """Print the success ritual after the user accepts the preview.

    The user just trusted us with their repo — give them a clear
    confirmation, the next-step ladder, and where to get help. This
    is the final beat of the 90-second walk.
    """
    try:
        from rich.console import Console
        from rich.panel import Panel as RichPanel
    except Exception:  # noqa: BLE001
        print(f"\n✓ Forged · run {panel.run_id}")
        print(f"  Files: {len(panel.files)}")
        print("  Next:  " + " · ".join(next_steps))
        print(f"  Help:  fluid doctor  ·  {help_url}")
        return

    out = console or Console()
    body = (
        f"[bold green]✓ Forged · run {panel.run_id}[/bold green]\n"
        f"  Files: {len(panel.files)}\n"
        f"  Next:  {' · '.join(next_steps)}\n"
        f"  Help:  fluid doctor  ·  {help_url}"
    )
    out.print(RichPanel(body, border_style="green"))


__all__ = [
    "CostSnapshot",
    "PendingFile",
    "PreviewError",
    "PreviewPanel",
    "ReceiptDecision",
    "capture_cost_snapshot",
    "confirm",
    "new_run_id",
    "render",
    "render_completion",
    "run_dir_for",
]
