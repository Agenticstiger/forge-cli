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

"""Per-stage semantic retrieval helper.

Single canonical entry point for ``memory/semantic`` retrieval.
Used by :meth:`ModelerAgent._retrieve_prior_similar_models` so
the rest of the pipeline (CriticAgent, BuilderAgent, observers)
gets retrievals in the session scratchpad consistently.

Before this module was unified, the modeler had its own private
``_retrieve_prior_similar_models`` AND this public function
existed in parallel — the public function never ran in
production. Now ``_retrieve_prior_similar_models`` delegates here
so there's exactly one code path.

Public surface:

* :func:`retrieve_similar_models` — top-k from
  ``memory/semantic`` for a given query string. Writes results to
  the session scratchpad as :class:`RetrievalResult` entries.
* :class:`RetrievalConfig` — thresholds + limits, configurable via
  ``StageSession.capability_matrix["rag"]``.

The function is **safe to call against any backend** — when the
store is ``NullBackend`` or ``FileBackend`` (no vector index), the
search falls back to keyword / exact mode and may return empty.
That's expected: RAG is an enhancement, not a hard requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from fluid_build.copilot.scratchpad import RetrievalResult, Scratchpad

_log = logging.getLogger(__name__)


@dataclass
class RetrievalConfig:
    """Runtime config for the RAG retrieval helper.

    Defaults match the v1.5 plan ("top-3 similar prior models");
    operators can tune via ``StageSession.capability_matrix``::

        session.capability_matrix["rag"] = {
            "limit": 5,
            "min_similarity": 0.5,
            "namespace": "memory/semantic",
        }
    """

    limit: int = 3
    """Max number of retrievals returned."""

    min_similarity: float = 0.0
    """Filter out matches below this threshold. ``0.0`` means
    "return whatever the store gives me" — useful when the store
    is ``FileBackend`` and similarity is always 0 (exact match
    only). Vector backends produce real similarity scores so the
    threshold becomes meaningful."""

    namespace: str = "memory/semantic"
    """Store namespace to search."""

    mode: str = "hybrid"
    """Retrieval mode hint (``"exact" | "keyword" | "vector" |
    "hybrid"``). ``hybrid`` is the most forgiving — backends that
    don't support hybrid silently degrade to exact / keyword."""


def retrieve_similar_models(
    query: str,
    *,
    store: Any,
    scratchpad: Scratchpad,
    config: Optional[RetrievalConfig] = None,
) -> List[RetrievalResult]:
    """Pull top-k similar prior records from ``memory/semantic``.

    Parameters
    ----------
    query:
        Free-text query — typically a one-line summary of the
        current intent / DDL / catalog scope. The LLM in the staged
        modeler prompt sees the retrieved records, so this query
        should be specific enough that lexical / vector search can
        find truly similar past models, not just any record.
    store:
        Any :class:`Store` implementation. ``NullBackend`` returns
        an empty list — RAG is a capability, not a requirement.
    scratchpad:
        Per-session scratchpad. Each retrieval is written via
        :meth:`Scratchpad.add_retrieval` so the modeler prompt
        loader can pick them up.
    config:
        Optional :class:`RetrievalConfig`. Defaults to ``limit=3``,
        ``namespace="memory/semantic"``, ``mode="hybrid"``.

    Returns
    -------
    list of :class:`RetrievalResult`
        The same records added to the scratchpad, in similarity-
        descending order.

    The function is best-effort: any error in the store path is
    logged at DEBUG and produces an empty result. RAG must NEVER
    block a forge — degraded retrieval just means a colder prompt.
    """
    cfg = config or RetrievalConfig()

    if not query or store is None:
        return []

    # Some Store impls don't have ``.search`` (older NullBackend
    # variants) — degrade gracefully.
    search_fn = getattr(store, "search", None)
    if not callable(search_fn):
        return []

    try:
        records = search_fn(
            cfg.namespace,
            query,
            mode=cfg.mode,
            limit=max(1, cfg.limit * 2),  # over-fetch then filter
        )
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug(
            "retrieve_similar_models: search failed in namespace %r: %s",
            cfg.namespace,
            exc,
            exc_info=True,
        )
        return []

    out: List[RetrievalResult] = []
    for record in records or []:
        # Each backend returns slightly different shapes; we accept:
        # * a Pydantic-like object with ``.key`` / ``.value`` /
        #   ``.metadata`` (FileBackend, SqliteBackend).
        # * a dict with ``key`` + ``score`` + ``payload``
        #   (VectorBackend).
        key = (
            getattr(record, "key", None)
            or (record.get("key") if isinstance(record, dict) else None)
            or ""
        )
        score = float(
            getattr(record, "similarity", None)
            or getattr(record, "score", None)
            or (
                record.get("similarity") or record.get("score")
                if isinstance(record, dict)
                else None
            )
            or 0.0
        )
        if score < cfg.min_similarity:
            continue
        payload = (
            getattr(record, "value", None)
            or (record.get("payload") or record.get("value") if isinstance(record, dict) else None)
            or {}
        )
        # Build a one-line summary from common fields if the backend
        # didn't supply one — improves the downstream prompt's
        # readability.
        summary = ""
        if isinstance(payload, dict):
            summary = (
                payload.get("description") or payload.get("name") or payload.get("intent") or ""
            )
        result = RetrievalResult(
            namespace=cfg.namespace,
            key=str(key),
            similarity=score,
            summary=str(summary)[:200],  # cap for prompt-token safety
            payload=payload if isinstance(payload, dict) else {},
        )
        out.append(result)
        if len(out) >= cfg.limit:
            break

    for r in out:
        scratchpad.add_retrieval(r)
    return out


__all__ = [
    "RetrievalConfig",
    "retrieve_similar_models",
]
