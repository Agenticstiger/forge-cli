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

"""ADP auto-replay on upstream reprocess (Phase-3 #13).

When an SDP reprocesses (its cursor is rewound to an earlier value),
every downstream ADP / CDP that ``consumes[]`` from it has stale
output: data the downstream materialised from the SDP's old slice
no longer matches what's there now. The mesh has two states:

* **Quiet drift** — operators don't know the downstream is stale
  until a verify run catches it weeks later. Bad.
* **Loud drift** — the moment the SDP cursor goes backward, every
  product that consumes it gets marked dirty. ``fluid status`` shows
  the dirty list immediately. Operators run ``fluid apply --replay``
  on the dirty products. Good.

This module is the loud-drift implementation:

1. :func:`detect_cursor_rewind` — compares old vs new cursor values
   for an SDP build, returns True when ``new < old`` (reprocess).
2. :func:`mark_downstream_dirty` — walks the workspace's contracts,
   finds every product whose ``consumes[]`` references the rewound
   SDP, writes a ``replay-pending`` marker into each downstream's
   ``.fluid/<product>/runtime/`` directory.
3. :func:`list_dirty_products` — reads the markers for ``fluid status``.
4. :func:`clear_dirty_marker` — called by ``fluid apply --replay`` on
   completion so the marker doesn't persist past the fix.

The markers are JSON files under
``.fluid/<product_id>/runtime/replay-pending.json`` with shape::

    {
      "upstream_product_id": "bronze.crm.customers",
      "upstream_build_id": "main_build",
      "upstream_stream": "default",
      "old_cursor_value": "2026-04-30T00:00:00Z",
      "new_cursor_value": "2026-04-15T00:00:00Z",
      "detected_at": "2026-05-02T12:30:00Z",
      "reason": "upstream cursor rewound 15 days"
    }

The build runners call :func:`detect_cursor_rewind` +
:func:`mark_downstream_dirty` after every successful cursor update.
The cost is one workspace walk per write; for typical workspaces
(<100 products) this is sub-millisecond.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

LOG = logging.getLogger("fluid.build_runners.replay")

REPLAY_MARKER_FILENAME = "replay-pending.json"


def _utc_now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_cursor_value_smaller(old_value: Any, new_value: Any) -> bool:
    """True when ``new`` is meaningfully less than ``old`` (rewind)."""
    if old_value is None or new_value is None:
        return False
    # Try numeric comparison first (offset-based cursors).
    try:
        return float(new_value) < float(old_value)
    except (TypeError, ValueError):
        pass
    # ISO-8601 timestamps compare lexicographically.
    if isinstance(old_value, str) and isinstance(new_value, str):
        return new_value < old_value
    # Same-type fallback.
    try:
        return new_value < old_value  # type: ignore[operator]
    except TypeError:
        return False


def detect_cursor_rewind(
    *,
    old_cursor_value: Any,
    new_cursor_value: Any,
) -> bool:
    """True when a build runner is about to write a cursor that's
    earlier than the previous one.

    Used pre-write so the runner can capture the OLD value, decide
    whether to flip downstream products dirty, then commit the new
    cursor. Reads cleanly even when the cursor is a string timestamp,
    a numeric offset, or a Kafka-style int.
    """
    return _is_cursor_value_smaller(old_cursor_value, new_cursor_value)


def find_downstream_products(
    workspace_root: Path,
    upstream_product_id: str,
) -> List[Dict[str, Any]]:
    """Walk the workspace for products whose ``consumes[]`` lists
    ``upstream_product_id``.

    Returns one dict per match: ``{"product_id": str, "contract_path":
    Path, "consumes_index": int}`` so callers can attribute markers
    to specific consumes rows. Errors during a single contract read
    do NOT abort the walk — broken contracts get logged + skipped.
    """
    matches: List[Dict[str, Any]] = []
    for path in sorted(workspace_root.rglob("*.fluid.yaml")):
        try:
            with path.open(encoding="utf-8") as f:
                contract = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            LOG.debug("downstream_walk_skip: path=%s error=%s", path, exc)
            continue
        consumes = contract.get("consumes")
        if not isinstance(consumes, list):
            continue
        product_id = contract.get("id") or ""
        for idx, consume in enumerate(consumes):
            if not isinstance(consume, Mapping):
                continue
            ref = consume.get("productId")
            if ref == upstream_product_id:
                matches.append(
                    {
                        "product_id": str(product_id),
                        "contract_path": path,
                        "consumes_index": idx,
                    }
                )
                break  # one marker per downstream product, even with multiple consumes rows
    return matches


def _marker_path(workspace_root: Path, product_id: str) -> Path:
    """Resolve the per-product replay-pending marker path."""
    return workspace_root / ".fluid" / product_id / "runtime" / REPLAY_MARKER_FILENAME


def mark_downstream_dirty(
    *,
    workspace_root: Path,
    upstream_product_id: str,
    upstream_build_id: str,
    upstream_stream: str,
    old_cursor_value: Any,
    new_cursor_value: Any,
    reason: Optional[str] = None,
) -> List[str]:
    """Write replay-pending markers for every downstream product that
    consumes ``upstream_product_id``.

    Returns the list of product_ids marked dirty, so the caller can
    log / surface the count. Best-effort writes — a failure on one
    product doesn't block the others.

    The marker payload includes both old and new cursor values so the
    operator can decide whether the rewind is small (one batch
    re-processed) or large (full historical replay) and adjust their
    response accordingly.
    """
    detected_at = _utc_now_iso()
    if not reason:
        reason = f"upstream cursor rewound from {old_cursor_value!r} to {new_cursor_value!r}"
    marked: List[str] = []
    for match in find_downstream_products(workspace_root, upstream_product_id):
        product_id = match["product_id"]
        if not product_id:
            continue
        marker_path = _marker_path(workspace_root, product_id)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "upstream_product_id": upstream_product_id,
            "upstream_build_id": upstream_build_id,
            "upstream_stream": upstream_stream,
            "old_cursor_value": old_cursor_value,
            "new_cursor_value": new_cursor_value,
            "detected_at": detected_at,
            "reason": reason,
        }
        try:
            marker_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            marked.append(product_id)
            LOG.info(
                "replay_marker_written: product=%s upstream=%s old=%s new=%s",
                product_id,
                upstream_product_id,
                old_cursor_value,
                new_cursor_value,
            )
        except OSError as exc:  # pragma: no cover — defensive
            LOG.warning("replay_marker_write_failed: product=%s error=%s", product_id, exc)
    return marked


def list_dirty_products(workspace_root: Path) -> List[Dict[str, Any]]:
    """Return one entry per dirty product (replay-pending marker present).

    Each entry: ``{"product_id": str, "marker": <full marker dict>}``.
    Used by ``fluid status`` to surface the pending-replay list.
    """
    fluid_dir = workspace_root / ".fluid"
    if not fluid_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for product_dir in sorted(fluid_dir.iterdir()):
        if not product_dir.is_dir():
            continue
        marker_path = product_dir / "runtime" / REPLAY_MARKER_FILENAME
        if not marker_path.is_file():
            continue
        try:
            with marker_path.open(encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.debug("dirty_marker_read_skip: path=%s error=%s", marker_path, exc)
            continue
        out.append({"product_id": product_dir.name, "marker": marker})
    return out


def clear_dirty_marker(workspace_root: Path, product_id: str) -> bool:
    """Delete the replay-pending marker for ``product_id``.

    Called by ``fluid apply --replay`` on successful completion so
    the marker doesn't persist past the fix. Returns True when a
    marker was deleted; False when no marker existed (idempotent).
    """
    marker_path = _marker_path(workspace_root, product_id)
    if not marker_path.is_file():
        return False
    try:
        marker_path.unlink()
        LOG.info("replay_marker_cleared: product=%s", product_id)
        return True
    except OSError as exc:  # pragma: no cover
        LOG.warning("replay_marker_clear_failed: product=%s error=%s", product_id, exc)
        return False


__all__ = [
    "REPLAY_MARKER_FILENAME",
    "clear_dirty_marker",
    "detect_cursor_rewind",
    "find_downstream_products",
    "list_dirty_products",
    "mark_downstream_dirty",
]
