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

"""Stage-level checkpoint store for the ``fluid forge`` agent pipeline.

Resumability for ``fluid forge`` — Ctrl-C mid-run, next invocation
resumes from the last completed stage. Single-process, sync-only,
JSON-only persistence under ``.fluid/agents/<run-id>/checkpoints/``.

Design — shape-compatible with LangGraph's ``BaseCheckpointSaver`` so
that a future ``command_center`` adapter can map this Protocol to
``langgraph-checkpoint-postgres`` in roughly 20 lines of glue. We DO
NOT import LangGraph (no new pip deps) — we mirror the method *names*
and their argument shape, but tailor the data model to our stage-level
needs (linear ordered stages, not graph-execution with per-channel
versions and pending writes).

Cross-checked against:

* LangGraph's ``BaseCheckpointSaver`` —
  https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/base/__init__.py
  Required methods: ``put``, ``put_writes``, ``get_tuple``, ``list``,
  ``delete_thread``. Our store maps these to ``put``, ``get``,
  ``list_stages`` / ``list_runs``, ``discard``. Divergences are
  documented in :class:`CheckpointStore` so adapter authors know what
  to wire.
* Apache Burr's ``BaseStatePersister`` —
  https://burr.apache.org/reference/persister/ — confirms the simpler
  save+load+list pattern is well-precedented for agent state stores
  where graph-execution semantics are overkill.

What this module does NOT do, by design:

* No async methods. LangGraph's ``BaseCheckpointSaver`` ships ``aget``,
  ``aput``, etc.; we keep this surface sync-only because the CLI is
  single-process. The async adapter lives downstream in
  ``command_center``.
* No graph topology / node primitives. This is a state store, not an
  executor. The coordinator owns the stage sequence.
* No locking. The contract is "one ``fluid`` process per workspace at a
  time" — multi-process resumability would require a real WAL and isn't
  worth the complexity for a CLI.
* No pickle. JSON-only by design: cross-version stability + no
  arbitrary-code-execution surface. Pydantic ``BaseModel`` round-trips
  via ``model_dump_json`` / ``model_validate_json``; dataclasses via
  ``asdict``; bare dicts/lists via ``json.dumps``.

Layout on disk::

    .fluid/agents/<run-id>/
        cost.json                    (owned by _preview_panel.py)
        reasoning.md                 (owned by _preview_panel.py)
        transcript.json              (owned by _preview_panel.py)
        checkpoints/
            manifest.json            (run-level summary)
            logical.json             (one file per completed stage)
            contract_forge.json
            …
            .paused                  (marker file — present when paused)

    .fluid/agents/.archived/<run-id>/  (after .discard())

Atomic writes mirror ``_preview_panel.py::_atomic_write`` — write to a
sibling tempfile, then ``os.replace`` (atomic on POSIX; on Windows
``Path.replace`` is also atomic per Python docs).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical stage sequence — single source of truth.
# ---------------------------------------------------------------------------

#: Canonical stage names used by the coordinator and the CLI list view.
#: The order matters: ``list_stages`` returns records in this order
#: regardless of the order they were written, and ``list_runs`` uses the
#: last entry to detect "fully complete" runs.
STAGE_NAMES: tuple[str, ...] = (
    "logical",
    "contract_forge",
    "builder",
    "readme",
    "transformation",
    "validator",
    "enrichment",
    "judge",
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class StageRecord:
    """One stage's checkpoint record.

    Shape-mirrors LangGraph's ``CheckpointTuple`` (run_id ~ thread_id,
    stage ~ checkpoint_id, payload ~ checkpoint.channel_values) but
    simplified to a flat dataclass with no pending-writes / parent-chain
    fields.
    """

    run_id: str
    stage: str  # one of STAGE_NAMES
    completed_at: str  # ISO 8601 UTC
    payload_kind: str  # "pydantic" | "json"
    payload: dict[str, Any]  # deserialised state (dict shape on read)
    cost_usd: float  # cost this stage spent
    contract_hash: Optional[str]  # sha256 of contract at this point


@dataclass
class RunSummary:
    """One run's high-level summary — used by ``fluid agents list``.

    Always read from the manifest file under ``checkpoints/`` so we
    don't have to parse every stage file just to list runs.
    """

    run_id: str
    started_at: str
    last_stage: Optional[str]
    completed_stages: tuple[str, ...]
    status: str  # "running" | "paused" | "complete" | "failed"
    total_cost_usd: float
    age_seconds: float
    workspace_root: str


class StaleContractError(Exception):
    """Raised by callers when resume detects the contract has changed
    since the checkpoint was written. The store DETECTS the mismatch
    (``get`` returns a record whose ``contract_hash`` differs from the
    caller's current hash); the caller decides whether to bail or
    retry. We expose the exception class here so every caller raises
    the same shape.
    """


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class JsonStageSerializer:
    """JSON-only serialiser for stage payloads.

    Three shapes handled:

    * **Pydantic ``BaseModel``** — ``model_dump_json()`` / ``model_validate_json()``.
      Round-trip preserves type info; caller passes ``expected_type`` to
      :meth:`deserialize`.
    * **Dataclass** — ``dataclasses.asdict`` → ``json.dumps``. Round-trip
      returns the plain dict shape unless the caller passes a dataclass
      ``expected_type`` (then we instantiate it from the dict).
    * **Plain dict / list / scalar** — ``json.dumps`` with ``default=str``
      so unexpected non-JSON types don't kill the write.

    Returns ``(kind, json_text)``. ``kind`` is one of ``"pydantic"``,
    ``"dataclass"``, ``"json"``.
    """

    @staticmethod
    def serialize(payload: Any) -> tuple[str, str]:
        """Encode ``payload`` to a JSON-text + kind tuple."""
        # Pydantic BaseModel — has ``model_dump_json``.
        if hasattr(payload, "model_dump_json") and callable(payload.model_dump_json):
            try:
                return "pydantic", payload.model_dump_json()
            except Exception:  # noqa: BLE001 — fall through to generic path
                LOG.debug("pydantic model_dump_json failed; falling through")
        # Dataclass instance — distinguish from generic dict so the
        # deserialiser can reconstruct the type when ``expected_type``
        # is supplied.
        if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
            return "dataclass", json.dumps(asdict(payload), sort_keys=True, default=str)
        # Generic JSON.
        return "json", json.dumps(payload, sort_keys=True, default=str)

    @staticmethod
    def deserialize(
        kind: str,
        text: str,
        expected_type: Optional[type] = None,
    ) -> Any:
        """Decode ``text`` back to its original shape.

        When ``expected_type`` is a Pydantic ``BaseModel`` subclass or a
        dataclass, we reconstruct an instance; otherwise we return the
        parsed JSON dict/list/scalar as-is. ``kind`` is the hint
        ``serialize`` returned and is the primary dispatch key.
        """
        if kind == "pydantic" and expected_type is not None:
            # Pydantic ``BaseModel.model_validate_json``.
            validator = getattr(expected_type, "model_validate_json", None)
            if callable(validator):
                return validator(text)
        if kind == "dataclass" and expected_type is not None:
            parsed = json.loads(text)
            if dataclasses.is_dataclass(expected_type):
                try:
                    return expected_type(**parsed)
                except TypeError:
                    # Field-mismatch (added/removed fields between
                    # versions) — return the raw dict so the caller can
                    # decide how to migrate.
                    return parsed
            return parsed
        # No expected type / plain JSON path.
        return json.loads(text)


# ---------------------------------------------------------------------------
# Atomic write helper — mirrors ``_preview_panel._atomic_write``.
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically via tempfile + ``os.replace``.

    Atomic on POSIX and Windows. Concurrent readers on the same file
    either see the previous bytes or the new bytes — never a half-
    written JSON document.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Protocol — the shape command_center will implement an adapter against.
# ---------------------------------------------------------------------------


@runtime_checkable
class CheckpointStore(Protocol):
    """LangGraph ``BaseCheckpointSaver``-shape interface.

    Method-name parity with LangGraph (``put`` / ``get`` / ``list`` /
    ``discard``) so a postgres-backed adapter in command_center can
    wrap each method 1-to-1. Argument names diverge intentionally:

    ===================  ==============================================
    Ours                 LangGraph
    ===================  ==============================================
    ``put(run_id,        ``put(config, checkpoint, metadata,
        stage,            new_versions)``
        payload, …)``    where ``config["thread_id"]`` maps to
                          our ``run_id`` and ``checkpoint["id"]``
                          maps to our ``stage``.
    ``get(run_id,        ``get_tuple(config)`` — single config arg
        stage)``          carrying both thread + checkpoint id.
    ``list_stages``      ``list(config)`` — flat iterator (we expose
                         the stage list separately from the run list
                         to keep both common queries O(stages-in-run)).
    ``list_runs``        no direct equivalent — LangGraph treats
                         threads as opaque ids. We expose this
                         because ``fluid agents list`` needs to
                         enumerate runs for resume-discoverability.
    ``discard(run_id)``  ``delete_thread(thread_id)``.
    ``skip_if_done``     no equivalent — convenience for the
                         coordinator's "skip already-completed
                         stage" loop pattern.
    ===================  ==============================================

    The Protocol is sync-only by design. An async sister Protocol
    (``aput`` / ``aget`` / …) belongs in ``command_center``, not here.
    """

    def put(
        self,
        run_id: str,
        stage: str,
        payload: Any,
        *,
        cost_usd: float = 0.0,
        contract_hash: Optional[str] = None,
    ) -> None:
        """Persist one stage's completion record."""
        ...

    def get(self, run_id: str, stage: str) -> Optional[StageRecord]:
        """Return the StageRecord for ``(run_id, stage)`` or ``None``."""
        ...

    def list_stages(self, run_id: str) -> list[StageRecord]:
        """Return completed StageRecords for this run, in canonical order."""
        ...

    def list_runs(
        self,
        *,
        workspace_root: Optional[Path] = None,
        only_incomplete: bool = False,
        since: Optional[datetime] = None,
    ) -> list[RunSummary]:
        """Enumerate known runs (sorted newest-first by ``started_at``)."""
        ...

    def discard(self, run_id: str) -> None:
        """Move the run dir to ``.fluid/agents/.archived/<run-id>/``."""
        ...

    @contextmanager
    def skip_if_done(self, run_id: str, stage: str) -> Iterator[Optional[StageRecord]]:
        """Context manager helper for the coordinator's resume pattern.

        Yields the existing :class:`StageRecord` when the stage is
        already done (caller skips its work and uses the payload),
        yields ``None`` when the stage hasn't run (caller does the
        work; on normal exit the manifest entry is bumped).
        """
        ...


# ---------------------------------------------------------------------------
# Null implementation — used when checkpointing is disabled.
# ---------------------------------------------------------------------------


class NullCheckpointStore:
    """No-op store. Every read returns ``None`` / empty; writes vanish.

    Used when ``FLUID_COPILOT_CHECKPOINT=0`` so callers don't have to
    branch on "is checkpointing enabled" — they just call the Protocol
    methods unconditionally.
    """

    def put(
        self,
        run_id: str,
        stage: str,
        payload: Any,
        *,
        cost_usd: float = 0.0,
        contract_hash: Optional[str] = None,
    ) -> None:
        return None

    def get(self, run_id: str, stage: str) -> Optional[StageRecord]:
        return None

    def list_stages(self, run_id: str) -> list[StageRecord]:
        return []

    def list_runs(
        self,
        *,
        workspace_root: Optional[Path] = None,
        only_incomplete: bool = False,
        since: Optional[datetime] = None,
    ) -> list[RunSummary]:
        return []

    def discard(self, run_id: str) -> None:
        return None

    @contextmanager
    def skip_if_done(self, run_id: str, stage: str) -> Iterator[Optional[StageRecord]]:
        yield None


# ---------------------------------------------------------------------------
# Default file-backed implementation.
# ---------------------------------------------------------------------------


_LAST_STAGE = STAGE_NAMES[-1]
_MANIFEST_FILENAME = "manifest.json"
_PAUSED_MARKER = ".paused"
_ARCHIVE_DIRNAME = ".archived"
_CHECKPOINTS_DIRNAME = "checkpoints"


class FileCheckpointStore:
    """File-backed default store.

    Layout::

        <workspace>/.fluid/agents/<run-id>/checkpoints/
            manifest.json
            <stage>.json    (one per stage in STAGE_NAMES)
            .paused         (marker — present when run is paused)

    All writes are atomic. The manifest carries enough information
    that ``list_runs`` doesn't have to parse every stage file just
    to render the resume picker.
    """

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self._workspace_root = (workspace_root or Path.cwd()).resolve()

    # ── path helpers ───────────────────────────────────────────────

    def _agents_root(self) -> Path:
        return self._workspace_root / ".fluid" / "agents"

    def _run_dir(self, run_id: str) -> Path:
        return self._agents_root() / run_id

    def _checkpoints_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / _CHECKPOINTS_DIRNAME

    def _manifest_path(self, run_id: str) -> Path:
        return self._checkpoints_dir(run_id) / _MANIFEST_FILENAME

    def _stage_path(self, run_id: str, stage: str) -> Path:
        return self._checkpoints_dir(run_id) / f"{stage}.json"

    def _paused_path(self, run_id: str) -> Path:
        return self._checkpoints_dir(run_id) / _PAUSED_MARKER

    # ── manifest read/write ────────────────────────────────────────

    def _read_manifest(self, run_id: str) -> dict[str, Any]:
        path = self._manifest_path(run_id)
        if not path.is_file():
            return {
                "run_id": run_id,
                "started_at": _utc_now_iso(),
                "completed_stages": [],
                "total_cost_usd": 0.0,
                "workspace_root": str(self._workspace_root),
                "status": "running",
            }
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A half-written manifest is recoverable — we have the
            # individual stage files; we just reconstruct it. Better
            # to lose the timestamp than the run.
            LOG.warning("manifest unreadable at %s (%s); reconstructing", path, exc)
            return {
                "run_id": run_id,
                "started_at": _utc_now_iso(),
                "completed_stages": [],
                "total_cost_usd": 0.0,
                "workspace_root": str(self._workspace_root),
                "status": "running",
            }

    def _write_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        _atomic_write_text(
            self._manifest_path(run_id),
            json.dumps(manifest, indent=2, sort_keys=True, default=str),
        )

    # ── Protocol surface ────────────────────────────────────────────

    def put(
        self,
        run_id: str,
        stage: str,
        payload: Any,
        *,
        cost_usd: float = 0.0,
        contract_hash: Optional[str] = None,
    ) -> None:
        """Persist one completed stage.

        Mirrors LangGraph ``BaseCheckpointSaver.put`` but flattened —
        we don't carry ``RunnableConfig`` / channel-versions /
        pending-writes because the CLI doesn't have a graph executor
        to feed those into.
        """
        if stage not in STAGE_NAMES:
            # Permissive — log + accept so a new stage added in
            # coordinator can land before this constant is bumped.
            LOG.info("checkpoint: unknown stage %r (not in STAGE_NAMES)", stage)

        kind, text = JsonStageSerializer.serialize(payload)
        record = {
            "run_id": run_id,
            "stage": stage,
            "completed_at": _utc_now_iso(),
            "payload_kind": kind,
            "payload_json": text,
            "cost_usd": float(cost_usd or 0.0),
            "contract_hash": contract_hash,
        }
        _atomic_write_text(
            self._stage_path(run_id, stage),
            json.dumps(record, indent=2, sort_keys=True, default=str),
        )
        # Bump manifest.
        manifest = self._read_manifest(run_id)
        completed = list(manifest.get("completed_stages") or [])
        if stage not in completed:
            completed.append(stage)
        manifest["completed_stages"] = completed
        manifest["last_stage"] = stage
        manifest["total_cost_usd"] = float(manifest.get("total_cost_usd") or 0.0) + float(
            cost_usd or 0.0
        )
        manifest["workspace_root"] = str(self._workspace_root)
        # "complete" once the final stage has landed; otherwise
        # "running". The .paused marker is a separate file the
        # coordinator drops on graceful pause.
        if stage == _LAST_STAGE or _LAST_STAGE in completed:
            manifest["status"] = "complete"
        else:
            manifest["status"] = manifest.get("status") or "running"
        self._write_manifest(run_id, manifest)

    def get(self, run_id: str, stage: str) -> Optional[StageRecord]:
        """Return the StageRecord for ``(run_id, stage)`` or ``None``."""
        path = self._stage_path(run_id, stage)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("checkpoint unreadable at %s (%s)", path, exc)
            return None
        try:
            payload = JsonStageSerializer.deserialize(
                raw.get("payload_kind", "json"),
                raw.get("payload_json", "null"),
                expected_type=None,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.warning("checkpoint payload undecodable at %s (%s)", path, exc)
            return None
        # Ensure payload is dict-shape on read for the StageRecord
        # contract — callers that need typed reconstruction re-run
        # ``JsonStageSerializer.deserialize`` with their expected type.
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return StageRecord(
            run_id=raw.get("run_id", run_id),
            stage=raw.get("stage", stage),
            completed_at=raw.get("completed_at", ""),
            payload_kind=raw.get("payload_kind", "json"),
            payload=payload,
            cost_usd=float(raw.get("cost_usd") or 0.0),
            contract_hash=raw.get("contract_hash"),
        )

    def list_stages(self, run_id: str) -> list[StageRecord]:
        """Return completed StageRecords in canonical order."""
        out: list[StageRecord] = []
        for stage in STAGE_NAMES:
            rec = self.get(run_id, stage)
            if rec is not None:
                out.append(rec)
        return out

    def list_runs(
        self,
        *,
        workspace_root: Optional[Path] = None,
        only_incomplete: bool = False,
        since: Optional[datetime] = None,
    ) -> list[RunSummary]:
        """Enumerate runs, sorted newest-first by ``started_at``."""
        target_root = workspace_root.resolve() if workspace_root else None
        agents_root = self._agents_root()
        if not agents_root.is_dir():
            return []
        out: list[RunSummary] = []
        now = datetime.now(timezone.utc)
        for child in agents_root.iterdir():
            if not child.is_dir():
                continue
            # Skip the archive bucket.
            if child.name == _ARCHIVE_DIRNAME:
                continue
            manifest_path = child / _CHECKPOINTS_DIRNAME / _MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            completed = tuple(manifest.get("completed_stages") or [])
            # Status precedence: an on-disk .paused marker overrides
            # "running" so the resume picker can flag paused runs
            # without re-walking every stage file.
            status = manifest.get("status") or "running"
            if (child / _CHECKPOINTS_DIRNAME / _PAUSED_MARKER).exists():
                status = "paused"
            started_at = str(manifest.get("started_at") or "")
            # ``only_incomplete`` filters to runs missing the final
            # stage. The "complete" status is the strong signal;
            # checking the stage list is the backup.
            if only_incomplete:
                if status == "complete" or _LAST_STAGE in completed:
                    continue
            # ``since`` filter — compare on ISO timestamp; tolerate
            # malformed dates by including the run.
            if since is not None and started_at:
                try:
                    started_dt = datetime.fromisoformat(started_at)
                    if started_dt < since:
                        continue
                except ValueError:
                    pass
            # workspace_root filter — accept either an exact match or
            # "run is inside this directory". Useful for nested
            # workspaces.
            run_ws = manifest.get("workspace_root") or str(self._workspace_root)
            if target_root is not None:
                try:
                    Path(run_ws).resolve().relative_to(target_root)
                except (ValueError, OSError):
                    continue
            # Compute an age in seconds for the listing UI.
            age_seconds = 0.0
            if started_at:
                try:
                    age_seconds = max(
                        0.0, (now - datetime.fromisoformat(started_at)).total_seconds()
                    )
                except ValueError:
                    pass
            out.append(
                RunSummary(
                    run_id=str(manifest.get("run_id") or child.name),
                    started_at=started_at,
                    last_stage=manifest.get("last_stage"),
                    completed_stages=completed,
                    status=status,
                    total_cost_usd=float(manifest.get("total_cost_usd") or 0.0),
                    age_seconds=age_seconds,
                    workspace_root=run_ws,
                )
            )
        # Newest-first.
        out.sort(key=lambda r: r.started_at, reverse=True)
        return out

    def discard(self, run_id: str) -> None:
        """Move the run dir to ``.fluid/agents/.archived/<run-id>/``.

        Reversibility — we don't delete. Operators who hit "discard"
        by accident can re-`mv` the directory back. The archive bucket
        is named with a leading dot so ``list_runs`` ignores it
        automatically.
        """
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            return
        archive_root = self._agents_root() / _ARCHIVE_DIRNAME
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / run_id
        # If a previous discard for the same run-id exists, fold the
        # incoming one into a timestamped sibling so we never lose
        # archived data.
        if dest.exists():
            dest = archive_root / f"{run_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        shutil.move(str(run_dir), str(dest))

    # ── pause / resume helpers (the .paused marker file) ───────────

    def mark_paused(self, run_id: str) -> None:
        """Drop the ``.paused`` marker file on graceful pause.

        Lets ``fluid agents list`` distinguish "killed mid-run" from
        "user explicitly paused" without parsing every stage file.
        """
        path = self._paused_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, _utc_now_iso())

    def clear_paused(self, run_id: str) -> None:
        """Remove the ``.paused`` marker, if present (resume entrypoint)."""
        try:
            self._paused_path(run_id).unlink(missing_ok=True)
        except OSError:
            pass

    # ── skip_if_done context manager ───────────────────────────────

    @contextmanager
    def skip_if_done(
        self,
        run_id: str,
        stage: str,
    ) -> Iterator[Optional[StageRecord]]:
        """Resume helper.

        Pattern in the coordinator::

            with store.skip_if_done(run_id, "logical") as rec:
                if rec is not None:
                    state.logical = rec.payload
                else:
                    state.logical = run_logical_agent(...)
                    store.put(run_id, "logical", state.logical,
                              cost_usd=delta, contract_hash=h)

        We yield the existing record (if any) but do NOT auto-persist
        on exit — explicit ``put`` calls keep the cost / hash
        plumbing straightforward and avoid silent writes.
        """
        existing = self.get(run_id, stage)
        yield existing


# ---------------------------------------------------------------------------
# Process-wide accessor with env-var dispatch.
# ---------------------------------------------------------------------------


_DEFAULT_SAVER: Optional[CheckpointStore] = None


def get_default_saver(workspace_root: Optional[Path] = None) -> CheckpointStore:
    """Return the process-wide :class:`CheckpointStore` singleton.

    Honours ``FLUID_COPILOT_CHECKPOINT``:

    * ``"0"`` / ``"false"`` / ``"off"`` / ``"no"`` → :class:`NullCheckpointStore`.
    * anything else (or unset) → :class:`FileCheckpointStore`.

    ``workspace_root`` is consulted on first call only; the singleton
    sticks for the rest of the process. Tests that need a different
    workspace per case should construct :class:`FileCheckpointStore`
    directly rather than going through this accessor.
    """
    global _DEFAULT_SAVER
    if _DEFAULT_SAVER is not None:
        return _DEFAULT_SAVER
    raw = os.environ.get("FLUID_COPILOT_CHECKPOINT", "").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        _DEFAULT_SAVER = NullCheckpointStore()
    else:
        _DEFAULT_SAVER = FileCheckpointStore(workspace_root=workspace_root)
    return _DEFAULT_SAVER


def reset_default_saver() -> None:
    """Test helper — drop the cached singleton so the next call rebuilds.

    Used by tests that mutate ``FLUID_COPILOT_CHECKPOINT`` and expect
    a fresh dispatch.
    """
    global _DEFAULT_SAVER
    _DEFAULT_SAVER = None


# ---------------------------------------------------------------------------
# LangGraph shape-compatibility smoke check (introspection only).
# ---------------------------------------------------------------------------


def langgraph_method_shape() -> dict[str, tuple[str, ...]]:
    """Return our Protocol's method-name → parameter-name tuples.

    Used by the test suite to assert shape parity with LangGraph's
    ``BaseCheckpointSaver``. We expose it as a function (not a constant)
    so introspection happens at test time rather than at import.
    """
    out: dict[str, tuple[str, ...]] = {}
    for name in ("put", "get", "list_stages", "list_runs", "discard"):
        method = getattr(CheckpointStore, name, None)
        if method is None:
            continue
        sig = inspect.signature(method)
        out[name] = tuple(p for p in sig.parameters.keys())
    return out


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "STAGE_NAMES",
    "StageRecord",
    "RunSummary",
    "StaleContractError",
    "JsonStageSerializer",
    "CheckpointStore",
    "FileCheckpointStore",
    "NullCheckpointStore",
    "get_default_saver",
    "reset_default_saver",
    "langgraph_method_shape",
]
