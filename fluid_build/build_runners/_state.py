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

"""File-backed state store + single-flight lock.

Layout::

    .fluid/runs/<product-id>/<build-id>/
      cursors/<stream>.json
      watermarks/<stream>.json
      runs/<run-id>.json
      lock                     ← live lock file (PID + lease until)

All writes are atomic (temp + rename). Cursor / watermark / run-record
files are JSON; structure is documented in fluid_build.api.state.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, ContextManager, Dict, List, Optional

from fluid_build.api.state import Cursor, RunLock, StateStore, Watermark
from fluid_build.observability.secret_redactor import redact_value

from ._acquisition_common import utc_now_iso

DEFAULT_LEASE_SECONDS = 900  # 15 min


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _to_dict(obj: Any) -> Dict[str, Any]:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"cannot serialize {type(obj).__name__}")


# Re-export from the typed-error catalog: same symbol, single class
# identity. Existing imports of ``LockHeldError`` keep resolving here.
from fluid_build._errors import LockHeldError  # noqa: E402,F401


class FileStateStore(StateStore):
    """Local filesystem implementation of ``api.StateStore``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── path helpers ─────────────────────────────────────────────────────
    def _confine(self, candidate: Path) -> Path:
        """Assert ``candidate`` stays inside the state-store root after resolution.

        Belt-and-suspenders defence in depth: the runtime chokepoint
        (``build_runners.base.run_builds_from_args``) already validates
        ``contract.id`` / ``build.id`` against the shared identifier
        grammar before any path is created, so neither ``product_id`` nor
        ``build_id`` can carry ``..`` / a separator by the time they reach
        here. This guard is the second wall: if a future caller ever
        constructs a ``FileStateStore`` path from an unvalidated component
        (``product_id``, ``build_id``, ``stream``, ``run_id``), an escape
        is rejected here instead of writing outside the workspace.
        """
        root_resolved = self.root.resolve()
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            from fluid_build.build_runners._ids import IdentifierViolation

            raise IdentifierViolation(
                f"state path {resolved} escapes the state-store root {root_resolved}"
            ) from exc
        return resolved

    def _build_dir(self, product_id: str, build_id: str) -> Path:
        return self._confine(self.root / "runs" / product_id / build_id)

    def _cursor_path(self, product_id: str, build_id: str, stream: str) -> Path:
        return self._confine(self._build_dir(product_id, build_id) / "cursors" / f"{stream}.json")

    def _watermark_path(self, product_id: str, build_id: str, stream: str) -> Path:
        return self._confine(
            self._build_dir(product_id, build_id) / "watermarks" / f"{stream}.json"
        )

    def _run_record_path(self, product_id: str, build_id: str, run_id: str) -> Path:
        return self._confine(self._build_dir(product_id, build_id) / "runs" / f"{run_id}.json")

    def _lock_path(self, scope: str, resource_id: str) -> Path:
        # SECURITY (path traversal): ``scope`` / ``resource_id`` are
        # interpolated into the lock filename, so a value with a path
        # separator or ``..`` would escape the workspace. Route through the
        # same ``_confine`` backstop as every sibling helper
        # (``_build_dir`` / ``_cursor_path`` / ``_watermark_path`` /
        # ``_run_record_path``) so an unvalidated component is rejected here.
        return self._confine(self.root / "locks" / f"{scope}__{resource_id}.lock")

    # ── cursor / watermark ───────────────────────────────────────────────
    def get_cursor(self, product_id: str, build_id: str, stream: str) -> Optional[Cursor]:
        d = _read_json(self._cursor_path(product_id, build_id, stream))
        if d is None:
            return None
        return Cursor(stream=d["stream"], value=d["value"], updated_at=d["updated_at"])

    def set_cursor(self, product_id: str, build_id: str, cursor: Cursor) -> None:
        # Capture the previous cursor value BEFORE writing the new one
        # so we can detect a rewind (reprocess) and mark every
        # downstream product that ``consumes[]`` from this product
        # dirty. See ``build_runners._replay`` for the marker contract.
        prev = self.get_cursor(product_id, build_id, cursor.stream)
        _atomic_write_json(
            self._cursor_path(product_id, build_id, cursor.stream),
            _to_dict(cursor),
        )

        if prev is None:
            return

        try:
            from ._replay import detect_cursor_rewind, mark_downstream_dirty
        except Exception:  # pragma: no cover — defensive
            return

        if not detect_cursor_rewind(old_cursor_value=prev.value, new_cursor_value=cursor.value):
            return

        # Workspace root is the parent of ``.fluid/runs/...``. The
        # state store is rooted at ``.fluid/runs``; walk up two to
        # land on the workspace dir that holds ``*.fluid.yaml``.
        workspace_root = self.root.parent.parent if self.root.name == "runs" else self.root.parent
        try:
            mark_downstream_dirty(
                workspace_root=workspace_root,
                upstream_product_id=product_id,
                upstream_build_id=build_id,
                upstream_stream=cursor.stream,
                old_cursor_value=prev.value,
                new_cursor_value=cursor.value,
            )
        except Exception as exc:  # pragma: no cover — defensive
            # Marking dirty must never block the cursor write itself.
            # Log + continue so the runner can finish its commit.
            import logging as _lg

            _lg.getLogger("fluid.build_runners._state").debug(
                "replay_mark_failed: product=%s error=%s", product_id, exc
            )

    def get_watermark(self, product_id: str, build_id: str, stream: str) -> Optional[Watermark]:
        d = _read_json(self._watermark_path(product_id, build_id, stream))
        if d is None:
            return None
        return Watermark(
            stream=d["stream"], kind=d["kind"], value=d["value"], updated_at=d["updated_at"]
        )

    def set_watermark(self, product_id: str, build_id: str, watermark: Watermark) -> None:
        _atomic_write_json(
            self._watermark_path(product_id, build_id, watermark.stream), _to_dict(watermark)
        )

    # ── run record ───────────────────────────────────────────────────────
    def write_run_record(self, product_id: str, build_id: str, run_record: Dict[str, Any]) -> None:
        run_id = run_record["run_id"]
        # Defence-in-depth: run records carry runner facets (e.g. a Kafka
        # Connect task-failure ``trace``) that can embed connector config —
        # database passwords, S3 keys, ``sasl.jaas.config``. This record is
        # written via ``json.dumps`` and never flows through the logging
        # ``SecretRedactingFilter``, so redact here — the single chokepoint
        # every runner's run-record write funnels through — before it lands
        # on disk. ``redact_value`` recurses through the dict/list/str shape
        # and is idempotent, so re-redacting an already-clean record is safe.
        _atomic_write_json(
            self._run_record_path(product_id, build_id, run_id),
            redact_value(run_record),
        )

    def read_run_record(
        self, product_id: str, build_id: str, run_id: str
    ) -> Optional[Dict[str, Any]]:
        return _read_json(self._run_record_path(product_id, build_id, run_id))

    def read_run_record_strict(
        self,
        product_id: str,
        build_id: str,
        run_id: str,
        *,
        retention_horizon: str = "P30D",
    ) -> Dict[str, Any]:
        """Read a run record or raise ``StaleReplayError`` when missing.

        Replay paths (``fluid apply --replay --run-id X``) need to fail
        loudly when the requested run is past retention so the user gets
        the five-field Panel instead of a silent empty manifest. The
        retention_horizon string is included in the error so the user
        knows how to extend retention if they want to keep replaying old
        runs.
        """
        rec = self.read_run_record(product_id, build_id, run_id)
        if rec is not None:
            return rec
        from fluid_build._errors import StaleReplayError

        raise StaleReplayError.for_run(run_id=run_id, retention_horizon=retention_horizon)

    def list_runs(self, product_id: str, build_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        runs_dir = self._build_dir(product_id, build_id) / "runs"
        if not runs_dir.exists():
            return []
        records = []
        for p in sorted(runs_dir.glob("*.json"), reverse=True):
            d = _read_json(p)
            if d is not None:
                records.append(d)
            if len(records) >= limit:
                break
        return records

    # ── lock ─────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def acquire_lock(
        self,
        scope: str,
        resource_id: str,
        timeout_seconds: int = DEFAULT_LEASE_SECONDS,
        on_contended: str = "abort",
    ) -> ContextManager[RunLock]:
        path = self._lock_path(scope, resource_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        lease_until = now + timeout_seconds

        existing = _read_json(path)
        if existing is not None:
            existing_lease = float(existing.get("lease_until", 0.0))
            if now < existing_lease:
                # Still held.
                if on_contended == "abort":
                    raise LockHeldError.for_resource(
                        holder=str(existing.get("holder") or "?"),
                        scope=scope,
                        resource_id=resource_id,
                    )
                elif on_contended == "queue":
                    # Simple busy-wait; production-quality version uses fs notify.
                    while now < existing_lease:
                        time.sleep(0.5)
                        now = time.time()
                        existing = _read_json(path)
                        if existing is None:
                            break
                        existing_lease = float(existing.get("lease_until", 0.0))
                # else "replace": fall through and overwrite.

        holder = f"pid-{os.getpid()}"
        record = {
            "holder": holder,
            "scope": scope,
            "resource_id": resource_id,
            "acquired_at": now,
            "acquired_at_iso": utc_now_iso(),
            "lease_until": lease_until,
            "lease_until_iso": utc_now_iso(),
            "lease_seconds": timeout_seconds,
        }
        _atomic_write_json(path, record)
        lock = RunLock(
            holder=holder,
            acquired_at=record["acquired_at_iso"],
            lease_seconds=timeout_seconds,
            scope=scope,
            resource_id=resource_id,
        )
        try:
            yield lock
        finally:
            try:
                # Only remove if we still hold it.
                cur = _read_json(path)
                if cur is not None and cur.get("holder") == holder:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
