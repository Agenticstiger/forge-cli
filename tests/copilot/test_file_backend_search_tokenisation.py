# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tokenised keyword search on FileBackend — regression for
`fluid memory search` returning 0 hits on natural-language queries.

Pre-fix, FileBackend.search did strict substring matching on the
serialised JSON. A user querying ``"telco customer 360"`` against a
stored record named ``telco_customer_360`` got nothing — the
underscores broke the substring. ModelerAgent's prompt loader runs
the same retrieve_similar_models codepath, so the bug also silently
degraded prompt enrichment for normal data-modeling runs.

Fix: split both sides on word boundaries (alphanumerics only,
underscores / dashes / dots / slashes are separators) and require
ALL tokens to be present. Word-order-insensitive, case-insensitive,
and underscore/space-insensitive — which matches what users mean
when they type a free-text query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.copilot.store.backends.file import FileBackend


@pytest.fixture
def populated_backend(tmp_path: Path) -> FileBackend:
    backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
    backend.put(
        "memory/semantic",
        "telco_customer_360.abc",
        {
            "name": "telco_customer_360",
            "description": "Customer 360 analytics with usage + billing facts",
            "technique": "dimensional",
        },
        ttl=None,
        metadata={"technique": "dimensional"},
    )
    backend.put(
        "memory/semantic",
        "retail_loyalty.def",
        {
            "name": "retail_loyalty",
            "description": "Loyalty program point ledger + redemption tracking",
            "technique": "data_vault_2",
        },
        ttl=None,
        metadata={"technique": "data_vault_2"},
    )
    backend.put(
        "memory/semantic",
        "finance_revenue.ghi",
        {
            "name": "finance_revenue",
            "description": "Daily revenue rollup by account_segment",
            "technique": "dimensional",
        },
        ttl=None,
        metadata={"technique": "dimensional"},
    )
    return backend


class TestFileBackendKeywordSearch:
    """Pin the multi-word/underscore-spanning query shapes that the
    pre-fix substring path missed."""

    @pytest.mark.parametrize(
        "query,expected_keys",
        [
            # Exact key fragments work pre- and post-fix.
            ("telco_customer_360", {"telco_customer_360.abc"}),
            # Multi-word query spanning underscores — the regression case.
            ("telco customer", {"telco_customer_360.abc"}),
            ("telco customer 360", {"telco_customer_360.abc"}),
            ("customer 360", {"telco_customer_360.abc"}),
            # Word-order-insensitive.
            ("360 customer", {"telco_customer_360.abc"}),
            # Case-insensitive.
            ("TELCO CUSTOMER", {"telco_customer_360.abc"}),
            # Hits multiple records via shared tokens.
            ("dimensional", {"telco_customer_360.abc", "finance_revenue.ghi"}),
            # Underscore in stored value matches space in query.
            (
                "account segment",
                {"finance_revenue.ghi"},
            ),
            # Tokens from description.
            ("loyalty point", {"retail_loyalty.def"}),
            ("point redemption", {"retail_loyalty.def"}),
        ],
    )
    def test_tokenised_match_finds_record(
        self, populated_backend: FileBackend, query: str, expected_keys: set
    ) -> None:
        results = populated_backend.search("memory/semantic", query, mode="hybrid", limit=10)
        actual_keys = {r.key for r in results}
        assert (
            actual_keys == expected_keys
        ), f"query={query!r}: expected {expected_keys}, got {actual_keys}"

    def test_unmatched_token_filters_out(self, populated_backend: FileBackend) -> None:
        """All tokens must be present (AND semantics) — even one missing
        token excludes the record."""
        # 'telco' matches the first record, but 'snowflake' is in NO record
        results = populated_backend.search(
            "memory/semantic", "telco snowflake", mode="hybrid", limit=10
        )
        assert results == [], "AND-match: one missing token must exclude the record"

    def test_empty_query_returns_empty(self, populated_backend: FileBackend) -> None:
        results = populated_backend.search("memory/semantic", "", mode="hybrid", limit=10)
        assert results == []

    def test_exact_mode_is_key_lookup(self, populated_backend: FileBackend) -> None:
        """exact mode must still behave as a key lookup, not a keyword
        search — that's the pre-existing contract."""
        # Exact key found
        results = populated_backend.search(
            "memory/semantic", "telco_customer_360.abc", mode="exact"
        )
        assert len(results) == 1
        # Non-key string returns empty (even though substring matches)
        results = populated_backend.search("memory/semantic", "telco customer", mode="exact")
        assert results == []

    def test_limit_is_respected(self, populated_backend: FileBackend) -> None:
        # 'dimensional' matches 2 records; limit=1 returns only 1
        results = populated_backend.search("memory/semantic", "dimensional", mode="hybrid", limit=1)
        assert len(results) == 1
