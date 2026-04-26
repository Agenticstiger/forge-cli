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

"""Auto-write compiled semantic models into ``memory/semantic`` on a
successful forge (D7).

Until D7, ``memory/semantic`` was **read-only** from ModelerAgent's
perspective: it would retrieve prior forged models to inform a new
generation, but the namespace was never populated automatically — the
only writers were the tests seeding fixtures and human users calling
``session.store.put(...)`` by hand. That gap broke the "learning loop":
a user's first real forge produced no retrievable signal for their
second, so the benefit of vector-indexed retrieval only materialized
when users remembered to back-fill the store themselves.

D7 closes the loop by hooking the coordinator's successful-forge
return point. When a ``LogicalDraft`` is produced, a slim JSON payload
summarising its semantic shape is written under
``memory/semantic/<slug>.<hash>`` so subsequent runs — in the same
workspace, or on a teammate's machine if the store is synced — can
retrieve it via the same ``VectorBackend`` token-hash ranker the
modeler already uses.

The write is **strictly opt-in** for two reasons:

* **Privacy / tenancy.** The payload includes OSI descriptions, entity
  names, business terms, and dataset metadata. These persist under
  ``~/.fluid/store/memory/semantic/`` on disk. Consultants and shared
  dev boxes that forge models for multiple unrelated tenants should
  *not* silently accumulate cross-tenant semantic signal. Flipping the
  gate explicitly (via ``FLUID_COPILOT_SEMANTIC_MEMORY=1``) makes the
  consent step visible in every environment.
* **Predictability.** Prior to D7, the coordinator was a pure function
  from inputs to a ``CoordinatorResult`` — no side effects on the
  session's store beyond the LLM cache the agents already hit.
  Turning on auto-write changes that contract; gating it behind an
  env var keeps v1.0 semantics intact unless the user opts in.

Errors in the write path are **swallowed with a warning log**: the
forge has already succeeded by the time this module runs, so a
disk-full / permission-denied / backend-offline failure on the store
must not retroactively turn a green forge red. The warning surfaces
through standard ops telemetry, so a misconfigured backend remains
visible without poisoning the return value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.copilot.store.base import Store

_log = logging.getLogger(__name__)

_ENV_VAR = "FLUID_COPILOT_SEMANTIC_MEMORY"
"""Environment variable that opts in to semantic-memory auto-writes.

Default is **off** — the forge-cli v1.0 contract is "retrieval-only
against ``memory/semantic``"; D7 preserves that default and requires an
explicit opt-in to change it. Any of the tokens ``1`` / ``true`` /
``yes`` / ``on`` (case-insensitive, surrounding whitespace allowed)
enables writes; everything else — unset, empty, ``0``, ``false``,
garbage — leaves the default ``read-only`` behavior in place."""

_ENABLE_TOKENS = frozenset({"1", "true", "yes", "on"})

_NAMESPACE = "memory/semantic"
"""Must match ``ModelerAgent._SEMANTIC_NAMESPACE`` so the writer and
the reader target the same drawer. If either side drifts, the other
silently stops finding records — so the constant is exposed here for
tests and any future shared consumer."""

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
"""Strip everything non-alphanumeric when slugging a model name.

Keeps keys filesystem-safe (``FileBackend`` writes them as
``<key>.json`` under ``memory/semantic/``) and avoids surprises from
users who forge a model called ``Customer Orders (V2)`` — the slug
becomes ``customer_orders_v2``, not a path-escape minefield."""


def auto_semantic_write_enabled() -> bool:
    """Return ``True`` iff semantic-memory auto-write is opted in.

    Pure function, no I/O beyond ``os.environ`` access. Reads the
    variable fresh on every call so tests using ``monkeypatch.setenv``
    and users who flip the flag between forge invocations both see the
    new value immediately — caching the value would break both paths.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _ENABLE_TOKENS


def _slug(name: str) -> str:
    """File-system-safe, stable, short slug for the key prefix.

    The slug is user-facing — it shows up in ``fluid memory show``
    output and in ``ls ~/.fluid/store/memory/semantic/`` — so we keep
    it readable rather than opaque. Adjacent non-alphanumerics collapse
    to a single underscore, leading/trailing underscores are stripped,
    and an empty result falls back to ``unnamed`` so the key never
    degenerates to a bare dot.
    """
    s = _SLUG_PATTERN.sub("_", name.lower()).strip("_")
    return s or "unnamed"


def _content_hash(payload: Dict[str, Any]) -> str:
    """Short, content-addressable digest for the key suffix.

    ``json.dumps(..., sort_keys=True)`` canonicalizes the payload so
    Pydantic-level field-order churn doesn't cause spurious key drift
    between otherwise-identical forges. Sixteen hex chars
    (64 bits of entropy) is plenty for a single workspace's semantic
    namespace — collision probability is dominated by workload size,
    not the hash cutoff.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _build_payload(logical: LogicalDraft) -> Dict[str, Any]:
    """Shape the persisted value for VectorBackend's token-hash ranker.

    Field selection balances two forces:

    * **Recall** — the payload must carry the text the modeler's
      retrieval query (built from ``intent.domain``, ``intent.description``,
      ``technique``, entity / dataset names) would plausibly hit. That
      means carrying at least the compiled OSI, which holds the
      richest natural-language signal (``ai_context.instructions``,
      ``ai_context.synonyms``, dataset / metric descriptions).
    * **Replayability** — enough structure that when a future run
      retrieves this record, the modeler can decide whether it's
      relevant (``technique`` check) and what to reuse from it
      (entity names, metric patterns). Raw OSI gives the modeler
      everything it produced in the successful run.

    We deliberately do **not** persist ``dv2`` / ``dimensional`` /
    ``conceptual`` — those are physical-shape artifacts orthogonal to
    the semantic reuse this namespace is designed for. Keeping them out
    shrinks the payload by an order of magnitude on large models.
    """
    return {
        "name": logical.name,
        "technique": logical.technique,
        "description": logical.description,
        "osi": logical.osi.model_dump(mode="json"),
    }


def write_semantic_record(
    store: Optional[Store],
    logical: LogicalDraft,
    *,
    source_type: Optional[str] = None,
) -> Optional[str]:
    """Persist ``logical`` under ``memory/semantic/<slug>.<hash>``.

    Returns the key that was written, or ``None`` when the write was
    skipped (store missing, opt-in disabled, or a swallowed error).
    **Never raises** — the caller has already produced a successful
    forge result; a broken store must not retroactively poison that
    result.

    The ``source_type`` hint (``"intent"`` / ``"ddl"`` / ``"tables"``)
    goes into ``metadata`` for provenance / debugging only — it does
    not participate in retrieval ranking, which operates on the
    ``value`` blob.
    """
    if store is None:
        return None
    if not auto_semantic_write_enabled():
        return None

    try:
        payload = _build_payload(logical)
        key = f"{_slug(logical.name)}.{_content_hash(payload)}"
        metadata = {
            "source_type": source_type,
            "technique": logical.technique,
            "written_by": "coordinator.auto_semantic_write",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        store.put(_NAMESPACE, key, payload, ttl=None, metadata=metadata)
        return key
    except Exception as exc:  # pragma: no cover — defensive swallow
        _log.warning(
            "fluid.copilot.semantic_write.failed: %s (forge unaffected)",
            exc,
        )
        return None


__all__ = [
    "_ENV_VAR",
    "_NAMESPACE",
    "auto_semantic_write_enabled",
    "write_semantic_record",
]
