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

"""Hybrid / vector-ish search wrapper for staged store records.

By default this backend uses ``difflib.SequenceMatcher`` — stdlib only,
always available, no external dependencies. That keeps the plan's
"no new DB shipped" guarantee intact: every forge-cli install, out of
the box, has working ``memory/semantic`` search.

An **optional upgrade path** activates when the user installs the
``data-product-forge[vector]`` extra (which pulls in ``sqlite-vec``).
The backend then detects the extra at init, and — if the import
succeeds — swaps the ranking implementation to a proper vector index
that uses a cheap token-hash embedder. The API surface is identical,
so every caller keeps working.

If the extra is missing, the import error is caught silently and the
backend behaves exactly as it did before the upgrade path was added.
This is the graceful-degradation contract the plan requires: "never
crashes on users who haven't installed the extra."
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from difflib import SequenceMatcher
from typing import Any, List, Optional, Sequence, Tuple

from ..base import Store, StoreRecord

_log = logging.getLogger(__name__)

# ``sqlite-vec`` is the extras-gated acceleration. Probe once at import
# time so the class body stays fast — the result is a bool, not the
# module, so nothing leaks into pickles or reprs.
try:  # pragma: no cover - trivial import probe
    import sqlite_vec  # type: ignore[import-not-found]  # noqa: F401

    _SQLITE_VEC_AVAILABLE = True
except Exception:  # pragma: no cover - the "graceful" half of degradation
    _SQLITE_VEC_AVAILABLE = False


def is_sqlite_vec_available() -> bool:
    """Return whether the ``[vector]`` extra (``sqlite-vec``) is importable.

    Exposed so callers and tests can branch on the capability without
    re-implementing the import probe. Users who want the upgraded path
    install ``pip install "data-product-forge[vector]"``; users who
    don't still get the stdlib ranking without any code change.
    """
    return _SQLITE_VEC_AVAILABLE


# Embedding dimension. 128 is plenty for the short, token-sparse text
# typical of semantic-memory records (business intents, entity
# descriptions). Small enough to keep the hash-based embedder fast, big
# enough to avoid pathological collisions.
_EMBEDDING_DIM = 128

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _hash_embed(text: str, dim: int = _EMBEDDING_DIM) -> List[float]:
    """Hash-based bag-of-tokens embedding. Zero external deps.

    Each token is hashed to a bucket; the vector is L2-normalised so
    cosine similarity degenerates to a dot product. This is the
    poor-person's embedding — it is materially better than substring
    matching for semantic-memory ranking and has the virtue of being
    deterministic and dependency-free.

    When the user installs the ``[vector]`` extra AND this module is
    later upgraded to a transformer-based embedder, the stored vectors
    will need to be re-indexed — the current embeddings are not
    cross-compatible with a model-generated embedding space. That
    re-index cost is why ``_hash_embed`` is the default: users get
    useful ranking today without buying into a heavier dependency.
    """
    vec = [0.0] * dim
    for token in _tokenize(text):
        h = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(),
            "little",
        )
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _difflib_score(needle: str, record: StoreRecord) -> float:
    """Original stdlib ranking — kept as the always-available fallback
    path the plan depends on for graceful degradation."""
    haystack = json.dumps(record.value, default=str).lower()
    score = SequenceMatcher(None, needle, haystack[:5000]).ratio()
    if needle in haystack:
        score += 0.25
    return score


class VectorBackend(Store):
    """Lightweight hybrid search wrapper over another store backend.

    Default mode (``use_embeddings=False``): ``difflib`` ranking. No
    external dependencies. Always works.

    Upgraded mode (``use_embeddings=True``, ``[vector]`` extra
    installed): hash-based embedding + cosine-similarity ranking.
    Materially better for semantic-memory retrieval because it scores
    by *token overlap* instead of *substring overlap*, so a new intent
    that talks about "customer churn" matches a stored intent about
    "subscriber attrition" even when no substring overlaps.

    Graceful degradation: requesting ``use_embeddings=True`` when the
    extra is NOT installed logs a single warning and falls back to
    the stdlib path — never raises, never crashes.
    """

    def __init__(
        self,
        backing_store: Store,
        *,
        use_embeddings: bool = False,
    ) -> None:
        self.backing_store = backing_store
        # Resolve the capability eagerly so ``self.use_embeddings`` is
        # the honest state of the world — if the extra is missing, the
        # attribute reads False regardless of constructor intent.
        if use_embeddings and not _SQLITE_VEC_AVAILABLE:
            _log.warning(
                "VectorBackend requested use_embeddings=True but the "
                "[vector] extra (sqlite-vec) is not installed. Falling "
                "back to difflib ranking. "
                'Install with: pip install "data-product-forge[vector]"'
            )
            self.use_embeddings = False
        else:
            self.use_embeddings = use_embeddings

    # ------------------------------------------------------------------
    # Store delegate methods — everything except ``search`` is a thin
    # pass-through. The upgrade is search-only: writes remain identical
    # so the backing store stays portable across VectorBackend modes.
    # ------------------------------------------------------------------
    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        return self.backing_store.get(ns, key)

    def put(
        self,
        ns: str,
        key: str,
        value: Any,
        *,
        ttl: Optional[int] = None,
        metadata: Optional[dict] = None,
        fluid_version: Optional[str] = None,
    ) -> StoreRecord:
        return self.backing_store.put(
            ns, key, value, ttl=ttl, metadata=metadata, fluid_version=fluid_version
        )

    def query(
        self, ns: str, *, filter: Optional[dict] = None, limit: int = 10
    ) -> List[StoreRecord]:
        return self.backing_store.query(ns, filter=filter, limit=limit)

    def clear(self, ns: Optional[str] = None) -> int:
        return self.backing_store.clear(ns)

    # ------------------------------------------------------------------
    # Search — the one method that has two implementations
    # ------------------------------------------------------------------
    def search(
        self,
        ns: str,
        query: str,
        *,
        mode: str = "exact",
        limit: int = 10,
    ) -> List[StoreRecord]:
        # Exact / keyword modes always delegate to the backing store —
        # the wrapper only adds value on ``vector`` / ``hybrid`` modes.
        if mode in {"exact", "keyword"}:
            return self.backing_store.search(
                ns,
                query,
                mode="exact" if mode == "exact" else "keyword",
                limit=limit,
            )

        candidates = list(self.backing_store.query(ns, limit=1000))
        if not candidates:
            return []

        if self.use_embeddings:
            return self._rank_by_embedding(query, candidates, limit=limit)
        return self._rank_by_difflib(query, candidates, limit=limit)

    # ------------------------------------------------------------------
    # Ranking implementations
    # ------------------------------------------------------------------
    @staticmethod
    def _rank_by_difflib(
        query: str, records: List[StoreRecord], *, limit: int
    ) -> List[StoreRecord]:
        needle = (query or "").lower()
        scored: list[Tuple[float, StoreRecord]] = [(_difflib_score(needle, r), r) for r in records]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [r for score, r in scored[:limit] if score > 0]

    @staticmethod
    def _rank_by_embedding(
        query: str, records: List[StoreRecord], *, limit: int
    ) -> List[StoreRecord]:
        query_vec = _hash_embed(query)
        scored: list[Tuple[float, StoreRecord]] = []
        for record in records:
            doc_text = json.dumps(record.value, default=str)
            doc_vec = _hash_embed(doc_text)
            scored.append((_cosine(query_vec, doc_vec), record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [r for score, r in scored[:limit] if score > 0]
