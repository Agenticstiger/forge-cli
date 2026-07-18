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

"""Mission run directory — manifest, scorecards, and cost receipts.

Layout, deliberately mirroring the ``.fluid/agents/<run-id>/``
convention the preview panel and ``FileCheckpointStore`` already use::

    <workspace>/.fluid/missions/<run-id>/
        manifest.json             run-level state (the resume pointer)
        scorecard.json            latest VERIFY result (digest-bound)
        cycles/<n>/scorecard.json per-cycle scorecard
        cycles/<n>/cost.json      per-cycle RunCostTracker receipt
        cycles/<n>/plan.json      the planner's step list

**No new store primitive.** The manifest is the same *shape*
``FileCheckpointStore`` writes (``run_id`` / ``started_at`` /
``status`` / ``total_cost_usd`` / ``workspace_root``) and reuses that
module's atomic writer and timestamp helper rather than growing a
parallel implementation. Mission-specific fields
(``mission``/``mission_goal``/``criteria_status``/``pause_reason``/
``mission_spec_sha256``/``contract_path``) are strictly **additive
optional** keys.

``status`` stays inside the documented literal set
(``running|paused|complete|failed``) — "stalled", "budget",
"timeout" and "iterations" are :data:`PAUSE_REASONS` values carried on
a ``paused`` status, so any consumer that switches on status keeps
working (RFC-deep-agents.md, "Checkpointing").
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Reuse the checkpoint module's writer/clock rather than re-deriving
# them: one atomic-write definition and one timestamp format across
# ``.fluid/agents/`` and ``.fluid/missions/``.
from fluid_build.copilot.checkpoint import _atomic_write_text, _utc_now_iso

LOG = logging.getLogger("fluid.copilot.missions.store")

MISSIONS_DIRNAME = "missions"
MANIFEST_FILENAME = "manifest.json"
SCORECARD_FILENAME = "scorecard.json"

#: The documented status literals — mission runs never invent new ones.
RUN_STATUSES = frozenset({"running", "paused", "complete", "failed"})

#: Why a ``paused`` run stopped. Carried in ``pause_reason``.
PAUSE_REASONS = frozenset({"stalled", "budget", "timeout", "iterations", "gate_rejected"})

#: A run id is an opaque **single path segment**, never a path fragment.
#:
#: This is a security boundary, not cosmetics. ``run_dir`` joins the id onto
#: ``missions_root``, and ``resume`` takes the id from a *workspace-resident*
#: ``manifest.json`` — attacker-controlled content in a cloned repo. Without
#: this gate a manifest declaring ``{"run_id": "../../../../x"}`` (or an
#: absolute ``"/tmp/x"``, which ``pathlib`` resolves by discarding the left
#: operand entirely) escapes the workspace and the mission trust model, which
#: pins the *spec* file's hash and says nothing about run manifests.
#: Mirrors ``spec.py::_MISSION_NAME_RE``.
_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class MissionRunIdError(ValueError):
    """A run id was not a safe single path segment."""


def missions_root(workspace_root: Path) -> Path:
    """``<workspace>/.fluid/missions``."""
    return Path(workspace_root).resolve() / ".fluid" / MISSIONS_DIRNAME


class MissionRunStore:
    """File-backed mission run directory. All writes atomic."""

    def __init__(self, workspace_root: Path, run_id: str) -> None:
        # Validate here, not at the call sites: this is the single chokepoint
        # every entry point (fresh run AND resume) funnels through.
        run_id = str(run_id)
        if not _RUN_ID_RE.match(run_id):
            raise MissionRunIdError(
                f"invalid mission run id {run_id!r} — expected a single path "
                "segment matching [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            )
        self.workspace_root = Path(workspace_root).resolve()
        self.run_id = run_id

    # ── paths ──────────────────────────────────────────────────────

    @property
    def run_dir(self) -> Path:
        root = missions_root(self.workspace_root)
        candidate = (root / self.run_id).resolve()
        # Defence in depth: the regex above already forbids separators and
        # ``..``; this asserts containment against symlink/normalisation
        # surprises before any write lands.
        if not candidate.is_relative_to(root.resolve()):
            raise MissionRunIdError(
                f"mission run dir {candidate} escapes the workspace missions root {root}"
            )
        return candidate

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / MANIFEST_FILENAME

    @property
    def scorecard_path(self) -> Path:
        return self.run_dir / SCORECARD_FILENAME

    def cycle_dir(self, cycle: int) -> Path:
        return self.run_dir / "cycles" / str(int(cycle))

    # ── manifest ───────────────────────────────────────────────────

    def read_manifest(self) -> Dict[str, Any]:
        """Read the manifest, or a fresh ``running`` skeleton."""
        path = self.manifest_path
        if not path.is_file():
            return self._skeleton()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Same posture as FileCheckpointStore: a half-written
            # manifest loses metadata, never the run.
            LOG.warning(
                "mission_manifest_unreadable",
                extra={"path": str(path), "error": type(exc).__name__},
            )
            return self._skeleton()
        if not isinstance(payload, dict):
            return self._skeleton()
        return payload

    def _skeleton(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": _utc_now_iso(),
            "status": "running",
            "total_cost_usd": 0.0,
            "workspace_root": str(self.workspace_root),
        }

    def write_manifest(self, manifest: Dict[str, Any]) -> None:
        status = str(manifest.get("status") or "running")
        if status not in RUN_STATUSES:  # pragma: no cover — guarded by callers
            raise ValueError(
                f"mission manifest status {status!r} is outside the documented set "
                f"{sorted(RUN_STATUSES)}; use pause_reason for the detail."
            )
        reason = manifest.get("pause_reason")
        if reason is not None and str(reason) not in PAUSE_REASONS:  # pragma: no cover
            raise ValueError(f"unknown pause_reason {reason!r}; expected {sorted(PAUSE_REASONS)}")
        _atomic_write_text(
            self.manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True, default=str),
        )

    def update_manifest(self, **fields: Any) -> Dict[str, Any]:
        """Merge *fields* into the manifest and persist it."""
        manifest = self.read_manifest()
        manifest.update(fields)
        manifest.setdefault("run_id", self.run_id)
        manifest.setdefault("started_at", _utc_now_iso())
        manifest["workspace_root"] = str(self.workspace_root)
        manifest["updated_at"] = _utc_now_iso()
        self.write_manifest(manifest)
        return manifest

    # ── scorecards ─────────────────────────────────────────────────

    def write_scorecard(self, scorecard_dict: Dict[str, Any], *, cycle: int) -> Path:
        """Persist a scorecard for *cycle* and refresh the latest pointer.

        The payload is already digest-bound — ``contract_sha256`` is the
        canonical hash of the exact contract VERIFY read — so a consumer
        can mark it STALE the moment the on-disk contract diverges.
        """
        payload = dict(scorecard_dict)
        payload["cycle"] = int(cycle)
        payload["run_id"] = self.run_id
        payload["verified_at"] = _utc_now_iso()
        blob = json.dumps(payload, indent=2, sort_keys=True, default=str)
        cycle_path = self.cycle_dir(cycle) / SCORECARD_FILENAME
        _atomic_write_text(cycle_path, blob)
        _atomic_write_text(self.scorecard_path, blob)
        return cycle_path

    def read_scorecard(self) -> Optional[Dict[str, Any]]:
        if not self.scorecard_path.is_file():
            return None
        try:
            payload = json.loads(self.scorecard_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def write_plan(self, plan_payload: Dict[str, Any], *, cycle: int) -> Path:
        path = self.cycle_dir(cycle) / "plan.json"
        _atomic_write_text(path, json.dumps(plan_payload, indent=2, sort_keys=True, default=str))
        return path

    # ── cost receipts ──────────────────────────────────────────────

    def receipt_paths(self) -> List[Path]:
        """Every per-cycle ``cost.json`` written by this run so far."""
        cycles = self.run_dir / "cycles"
        if not cycles.is_dir():
            return []
        return sorted(cycles.glob("*/cost.json"))

    def spend_from_receipts(self) -> float:
        """Re-sum spend from the on-disk receipts.

        Budgets are cumulative across pause/resume, so the authority is
        the receipts on disk — never a mutable in-memory total that a
        crash or a fresh process would silently reset to zero.
        """
        total = 0.0
        for path in self.receipt_paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            value = payload.get("total_usd") if isinstance(payload, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
        return round(total, 6)


def list_mission_runs(workspace_root: Path) -> List[Dict[str, Any]]:
    """Manifests of every mission run in the workspace, newest first."""
    root = missions_root(workspace_root)
    if not root.is_dir():
        return []
    runs: List[Dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            # The DIRECTORY NAME is authoritative — a manifest must not be able
            # to declare its own identity and thereby point the store somewhere
            # else. (Was ``setdefault``, which let workspace-resident content
            # win over the directory it lives in.)
            payload["run_id"] = child.name
            runs.append(payload)
    runs.sort(key=lambda m: str(m.get("started_at") or ""), reverse=True)
    return runs


def find_resumable_run(
    workspace_root: Path, *, mission: str, run_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Newest resumable (``running`` / ``paused``) run for *mission*.

    A finished mission never lingers as resumable — the runner writes an
    explicit ``complete``/``failed`` status at termination precisely so
    this lookup skips it.
    """
    for manifest in list_mission_runs(workspace_root):
        if run_id is not None and str(manifest.get("run_id")) != run_id:
            continue
        if mission and str(manifest.get("mission") or "") != mission:
            continue
        if str(manifest.get("status") or "") in ("running", "paused"):
            return manifest
    return None


__all__ = [
    "MANIFEST_FILENAME",
    "MISSIONS_DIRNAME",
    "PAUSE_REASONS",
    "RUN_STATUSES",
    "MissionRunStore",
    "find_resumable_run",
    "list_mission_runs",
    "missions_root",
]
