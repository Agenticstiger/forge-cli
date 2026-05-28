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

"""Stale-checkpoint detector for ``fluid forge`` resume.

When the user pauses a run, edits the contract on disk, then tries
to resume, the cached stage payloads no longer match the
on-disk truth. We compare the contract on disk's canonical hash
against the hash stamped on the last completed stage; a mismatch
means "resume would produce a contract divergent from the file the
operator has open in their editor".

The CLI surfaces this as a one-line prompt:

    The contract has changed since the checkpoint was taken
    (stage: contract_forge). Discard the checkpoint and start fresh?

This module is the detector only — the CLI owns the prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from fluid_build.copilot.checkpoint import CheckpointStore

_log = logging.getLogger(__name__)


def _canonical_hash(contract: Dict[str, Any]) -> str:
    """Canonical sha256 of a contract dict — same recipe as
    :meth:`StageCoordinator._hash_contract` so both sides compare apples
    to apples."""
    try:
        blob = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
    except Exception:  # pragma: no cover — defensive
        blob = json.dumps(contract, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_contract_from_disk(path: Path) -> Optional[Dict[str, Any]]:
    """Read a contract.fluid.yaml off disk. Returns ``None`` when the
    file doesn't exist, is unreadable, or doesn't parse as YAML.

    Stale-detection treats a missing / unreadable file the same as a
    fresh run — the operator's "discard" prompt is the right escape
    hatch either way."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.debug("stale-check: could not read %s (%s)", path, exc)
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        _log.debug("stale-check: could not parse %s (%s)", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def detect_stale_contract(
    saver: CheckpointStore,
    run_id: str,
    current_contract_path: Optional[Path],
) -> Tuple[bool, Optional[str]]:
    """Detect whether the contract on disk has drifted from the cursor.

    Returns ``(is_stale, summary)``.

    Logic:

    1. Walk the checkpoint records in canonical stage order. Use the
       LAST record that carries a ``contract_hash`` as the reference
       (later stages always re-stamp the same hash, but earlier ones
       like ``logical`` legitimately have ``None``).
    2. If there are no records OR none carry a ``contract_hash``,
       return ``(False, None)`` — nothing to compare against, fresh
       run.
    3. Read the contract from ``current_contract_path``. If the path
       is ``None`` or the file doesn't exist, return ``(False, None)``
       — nothing on disk to compare.
    4. Compute the canonical hash and compare. Mismatch → stale.

    The summary intentionally does NOT diff fields — that's
    expensive and the operator already has the file open. A clear
    "contract changed since checkpoint at stage X" is the contract.
    """
    summary = saver.list_stages(run_id) if hasattr(saver, "list_stages") else None
    if not summary:
        return (False, None)
    # ``list_stages`` returns either a ``RunSummary`` (peer's primitive)
    # or a ``list[StageRecord]`` (lighter primitives). Handle both
    # without picking a side — duck-type the ``.stages`` attribute.
    if hasattr(summary, "stages"):
        stages = list(summary.stages)
    else:
        stages = list(summary)
    if not stages:
        return (False, None)
    # Find the most-recent record that carries a non-None hash.
    reference: Optional[Tuple[str, str]] = None
    for record in stages:
        h = getattr(record, "contract_hash", None)
        if h:
            reference = (getattr(record, "stage", "?"), h)
    if reference is None:
        return (False, None)
    if current_contract_path is None:
        return (False, None)
    on_disk = _load_contract_from_disk(Path(current_contract_path))
    if on_disk is None:
        return (False, None)
    current_hash = _canonical_hash(on_disk)
    reference_stage, reference_hash = reference
    if current_hash == reference_hash:
        return (False, None)
    summary_msg = (
        f"contract changed since checkpoint at stage {reference_stage} "
        f"(cursor sha256={reference_hash[:12]}..., "
        f"on-disk sha256={current_hash[:12]}...)"
    )
    return (True, summary_msg)


__all__ = ["detect_stale_contract"]
