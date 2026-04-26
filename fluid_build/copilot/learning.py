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

"""Continuous learning from operator edits (E16).

When an operator hand-edits a forged contract after a forge run,
those edits are pure operator labor today — no mechanism captures
the diff. World-class agentic systems learn from corrections so
the next run produces a contract closer to what the operator
wanted.

This module ships the diff-capture primitive. The flow:

1. After a successful forge, the contract is on disk.
2. Operator opens it, makes edits, saves.
3. Next time the operator runs `fluid forge`, this module's
   :func:`record_operator_edits` is called with the original
   contract path + the edited contract.
4. The diff is recorded in ``memory/semantic`` with a special
   ``edit:`` namespace prefix.
5. The next modeler run retrieves these edits and biases its
   prompt with "operator previously corrected X to Y for similar
   contracts."

v1.5 ships the primitive and the diff capture. v1.6 wires the
edit-aware retrieval into the modeler prompt.

Public surface:

* :class:`OperatorEdit` — typed diff record.
* :func:`compute_edits` — diff two contracts.
* :func:`record_operator_edits` — write the diff to memory/semantic.
* :func:`fetch_recent_edits` — read the diff log for prompt biasing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

EDIT_NAMESPACE = "memory/semantic"
EDIT_KEY_PREFIX = "operator_edit:"


@dataclass
class OperatorEdit:
    """One observed edit between original and edited contract.

    ``path`` is the dotted path to the field that changed
    (``metadata.domain``). ``before`` is the original value;
    ``after`` is what the operator changed it to. ``kind`` says
    what kind of change.
    """

    path: str
    kind: str  # "added" | "removed" | "modified"
    before: Any = None
    after: Any = None


def _flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten a nested dict / list into a dotted-path map."""
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_prefix = f"{prefix}.{k}" if prefix else k
            out.update(_flatten(v, child_prefix))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child_prefix = f"{prefix}[{i}]"
            out.update(_flatten(v, child_prefix))
    else:
        out[prefix] = obj
    return out


def compute_edits(
    *,
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> List[OperatorEdit]:
    """Diff two contract dicts and return the changes.

    Lightweight string-equality diff over the flattened paths.
    Doesn't try to be a Git-style smart diff — operators editing
    contracts in YAML produce structural changes that flatten
    cleanly. Returns an empty list when the two contracts are
    identical.
    """
    before_flat = _flatten(before)
    after_flat = _flatten(after)

    edits: List[OperatorEdit] = []
    all_paths = set(before_flat) | set(after_flat)
    for path in sorted(all_paths):
        bv = before_flat.get(path)
        av = after_flat.get(path)
        if path not in before_flat:
            edits.append(
                OperatorEdit(
                    path=path,
                    kind="added",
                    before=None,
                    after=av,
                )
            )
        elif path not in after_flat:
            edits.append(
                OperatorEdit(
                    path=path,
                    kind="removed",
                    before=bv,
                    after=None,
                )
            )
        elif bv != av:
            edits.append(
                OperatorEdit(
                    path=path,
                    kind="modified",
                    before=bv,
                    after=av,
                )
            )
    return edits


def record_operator_edits(
    *,
    store: Any,
    contract_name: str,
    edits: List[OperatorEdit],
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a list of edits to ``memory/semantic``.

    Best-effort: any store failure is logged at DEBUG and
    swallowed. Continuous learning is observability — a broken
    store must not block a forge.

    The record is keyed by ``operator_edit:{contract_name}:{ts}``
    so multiple edits on the same contract accumulate without
    overwriting; the modeler's RAG retrieval reads them all and
    aggregates.
    """
    if not edits:
        return
    if store is None:
        return
    try:
        import time as _time

        from fluid_build.copilot.scratchpad import RetrievalResult  # noqa: F401

        key = f"{EDIT_KEY_PREFIX}{contract_name}:{int(_time.time())}"
        payload = {
            "contract_name": contract_name,
            "edits": [
                {
                    "path": e.path,
                    "kind": e.kind,
                    "before": e.before,
                    "after": e.after,
                }
                for e in edits
            ],
            "context": context or {},
        }
        # Light wrapper around store.put — doesn't depend on a
        # specific backend's API.
        if hasattr(store, "put"):
            store.put(EDIT_NAMESPACE, key, payload)
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("record_operator_edits failed: %s", exc, exc_info=True)


def fetch_recent_edits(
    *,
    store: Any,
    contract_name: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Read recent operator edits for biasing the next forge.

    Used by the modeler's prompt builder (v1.6 wiring) to surface
    "the operator previously corrected ``metadata.domain`` from
    ``commerce`` to ``retail`` in N similar contracts" hints.

    Falls back to an empty list when the store doesn't support
    keyword-prefix queries (e.g. NullBackend).
    """
    if store is None:
        return []
    # Keys are prefixed ``operator_edit:<contract_name>:<ts>`` so
    # we use the store's ``query()`` (which lists records in a
    # namespace) and filter client-side by the ``contract_name``
    # field on the payload. ``search`` was misleading because
    # ``FileBackend.search`` does substring match on VALUES, not
    # KEYS — and the contract name is in the value either way.
    try:
        records = (
            store.query(EDIT_NAMESPACE, limit=max(limit * 4, 50)) if hasattr(store, "query") else []
        )
    except Exception:  # pragma: no cover — defensive
        return []
    out: List[Dict[str, Any]] = []
    for record in records or []:
        value = getattr(record, "value", None)
        if isinstance(value, dict) and value.get("contract_name") == contract_name:
            out.append(value)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "EDIT_NAMESPACE",
    "EDIT_KEY_PREFIX",
    "OperatorEdit",
    "compute_edits",
    "record_operator_edits",
    "fetch_recent_edits",
]
