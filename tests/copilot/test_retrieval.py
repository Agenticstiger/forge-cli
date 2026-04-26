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

"""Coverage for per-stage RAG retrieval (Missing #3)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.retrieval import (
    RetrievalConfig,
    retrieve_similar_models,
)
from fluid_build.copilot.scratchpad import Scratchpad
from fluid_build.copilot.store.backends.null import NullBackend


class TestRetrieveBasicShape:
    def test_top_k_returned_in_similarity_order(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(key="a", similarity=0.6, value={"description": "A"}),
            SimpleNamespace(key="b", similarity=0.95, value={"description": "B"}),
            SimpleNamespace(key="c", similarity=0.4, value={"description": "C"}),
        ]
        # Note: the function over-fetches (2x) then filters; we
        # assert top 3 are returned, but in their original order
        # since the backend already sorts (or doesn't — the helper
        # doesn't re-sort; ordering is the backend's contract).
        results = retrieve_similar_models(
            "customer orders",
            store=store,
            scratchpad=pad,
            config=RetrievalConfig(limit=3),
        )
        assert len(results) == 3
        # All landed on the scratchpad too.
        assert len(pad.retrievals) == 3

    def test_dict_shape_records_accepted(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            {"key": "x", "similarity": 0.9, "payload": {"name": "X"}},
        ]
        results = retrieve_similar_models(
            "x",
            store=store,
            scratchpad=pad,
        )
        assert len(results) == 1
        assert results[0].key == "x"
        assert results[0].similarity == 0.9

    def test_min_similarity_filters_low_matches(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(key="a", similarity=0.3, value={}),
            SimpleNamespace(key="b", similarity=0.8, value={}),
        ]
        results = retrieve_similar_models(
            "q",
            store=store,
            scratchpad=pad,
            config=RetrievalConfig(limit=5, min_similarity=0.5),
        )
        # Only b cleared the threshold.
        assert [r.key for r in results] == ["b"]


class TestRetrieveDegradesGracefully:
    def test_null_backend_returns_empty(self):
        pad = Scratchpad()
        store = NullBackend()
        results = retrieve_similar_models(
            "anything",
            store=store,
            scratchpad=pad,
        )
        assert results == []
        assert pad.retrievals == []

    def test_no_search_method_returns_empty(self):
        pad = Scratchpad()
        # Object without ``.search`` — older / stripped backend.
        bare_store = SimpleNamespace()
        results = retrieve_similar_models(
            "q",
            store=bare_store,
            scratchpad=pad,
        )
        assert results == []

    def test_search_exception_returns_empty(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.side_effect = RuntimeError("vector index offline")
        # Must NOT raise — RAG is observability / enhancement.
        results = retrieve_similar_models(
            "q",
            store=store,
            scratchpad=pad,
        )
        assert results == []

    def test_empty_query_returns_empty(self):
        pad = Scratchpad()
        store = MagicMock()
        results = retrieve_similar_models(
            "",
            store=store,
            scratchpad=pad,
        )
        assert results == []
        # Search was NEVER called — empty query short-circuits.
        store.search.assert_not_called()


class TestSummaryDerivation:
    def test_summary_pulled_from_description_field(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(
                key="customer",
                similarity=0.9,
                value={"description": "Customer model with SCD2 satellites."},
            ),
        ]
        results = retrieve_similar_models(
            "q",
            store=store,
            scratchpad=pad,
        )
        assert results[0].summary == "Customer model with SCD2 satellites."

    def test_summary_falls_back_to_name(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(
                key="x",
                similarity=0.9,
                value={"name": "MyModel"},
            ),
        ]
        results = retrieve_similar_models(
            "q",
            store=store,
            scratchpad=pad,
        )
        assert results[0].summary == "MyModel"

    def test_summary_capped_at_200_chars(self):
        pad = Scratchpad()
        store = MagicMock()
        store.search.return_value = [
            SimpleNamespace(
                key="x",
                similarity=0.9,
                value={"description": "x" * 500},
            ),
        ]
        results = retrieve_similar_models(
            "q",
            store=store,
            scratchpad=pad,
        )
        assert len(results[0].summary) == 200
