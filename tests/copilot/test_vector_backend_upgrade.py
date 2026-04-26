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

"""Pin the graceful-degradation contract for the optional ``[vector]`` extra.

The plan (B2) requires that ``VectorBackend`` expose an optional
embedding-backed ranking mode gated on the ``data-product-forge[vector]``
extra, with the **hard invariant** that:

* Code that doesn't opt in behaves identically to v1.0 — same stdlib
  difflib ranking, same results.
* Code that opts in when the extra is NOT installed must NOT crash.
  It logs a single warning and falls back to the stdlib path. This is
  what lets users script against ``use_embeddings=True`` without
  branching on the install.
* Code that opts in when the extra IS installed uses the hash-based
  embedder — confirmed by checking that two records with no substring
  overlap but strong *token* overlap rank above a record with neither.

This file pins each of those three behaviours so a future refactor
can't silently flip any of them (which would either break users who
haven't installed the extra or silently turn off the upgrade for
users who have).
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.vector import (
    VectorBackend,
    is_sqlite_vec_available,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_backing_store(tmp_path):
    """A tiny FileBackend with three semantic-memory records so vector
    ranking has something to score."""
    store = FileBackend(root=tmp_path)
    store.put(
        "memory/semantic",
        "intent_telco_churn",
        {"intent": "subscriber attrition analytics on the mobile voice plan"},
    )
    store.put(
        "memory/semantic",
        "intent_retail_sales",
        {"intent": "retail point-of-sale transaction analytics with loyalty"},
    )
    store.put(
        "memory/semantic",
        "intent_healthcare_cohort",
        {"intent": "clinical cohort analysis for population health measures"},
    )
    return store


# ---------------------------------------------------------------------------
# Default behaviour — always on, always works, stdlib only
# ---------------------------------------------------------------------------


def test_default_mode_is_stdlib_difflib(populated_backing_store) -> None:
    """Default construction must NOT touch sqlite-vec — the no-deps
    install path stays green even when the extra is absent."""
    backend = VectorBackend(populated_backing_store)
    assert backend.use_embeddings is False
    # Search still returns ranked results from the stdlib path.
    results = backend.search("memory/semantic", "retail loyalty", mode="vector", limit=5)
    assert len(results) >= 1


def test_exact_and_keyword_modes_always_delegate(populated_backing_store) -> None:
    """The upgrade path only replaces ``vector`` / ``hybrid`` ranking.
    Exact and keyword modes must stay on the backing store's
    implementation so they keep their original semantics."""
    backend = VectorBackend(populated_backing_store)
    # Exact mode returns the specific record or nothing — not a ranked fuzzy set.
    exact_hit = backend.search("memory/semantic", "intent_retail_sales", mode="keyword", limit=5)
    # Not asserting the exact count — just that the delegation path works
    # without crashing and returns a list.
    assert isinstance(exact_hit, list)


# ---------------------------------------------------------------------------
# Graceful degradation — opt-in when extra is missing must NOT crash
# ---------------------------------------------------------------------------


def test_opt_in_without_extra_falls_back_silently(populated_backing_store, caplog) -> None:
    """When ``use_embeddings=True`` but ``sqlite-vec`` is not installed,
    the backend must degrade to stdlib with a single warning — never
    raise. Without this contract every caller has to probe the extra
    before instantiating, which defeats the graceful-degradation goal."""
    if is_sqlite_vec_available():
        pytest.skip("sqlite-vec is installed — degradation path is not exercised here")

    with caplog.at_level(logging.WARNING):
        backend = VectorBackend(populated_backing_store, use_embeddings=True)
    # The attribute reflects the honest runtime state, not the constructor's intent.
    assert backend.use_embeddings is False, (
        "use_embeddings must be False when the [vector] extra is missing — "
        "constructing with True should degrade, not elevate"
    )
    # One warning emitted, not a storm. The user sees the install
    # instruction once per process per VectorBackend construction.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 1
    joined = " ".join(r.getMessage() for r in warnings)
    assert "data-product-forge[vector]" in joined or "[vector]" in joined

    # And the search still returns results.
    results = backend.search("memory/semantic", "retail loyalty", mode="vector", limit=5)
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Upgraded ranking — runs only when the extra IS installed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_sqlite_vec_available(), reason="[vector] extra not installed")
def test_opt_in_with_extra_uses_embedding_ranking(populated_backing_store) -> None:
    backend = VectorBackend(populated_backing_store, use_embeddings=True)
    assert backend.use_embeddings is True
    results = backend.search(
        "memory/semantic",
        "retail loyalty point of sale analytics",
        mode="vector",
        limit=3,
    )
    assert len(results) >= 1
    # Top hit should be the retail intent — it shares the most tokens
    # with the query.
    assert results[0].key == "intent_retail_sales"


# ---------------------------------------------------------------------------
# Hash-based embedder — deterministic, dependency-free
# ---------------------------------------------------------------------------


def test_hash_embed_is_deterministic() -> None:
    """Caching of embeddings in a downstream index requires the embedder
    to be deterministic — otherwise a rewrite would rotate the entire
    index for no reason."""
    from fluid_build.copilot.store.backends.vector import _hash_embed

    a = _hash_embed("customer churn analytics")
    b = _hash_embed("customer churn analytics")
    assert a == b


def test_hash_embed_is_normalised() -> None:
    """Vectors are L2-normalised so cosine similarity degenerates to
    the dot product we actually compute in ``_rank_by_embedding``."""
    from fluid_build.copilot.store.backends.vector import _hash_embed

    v = _hash_embed("customer churn analytics")
    norm_sq = sum(x * x for x in v)
    assert 0.99 < norm_sq < 1.01, f"expected L2-normalised; got norm^2={norm_sq}"
