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

"""V1.5 Gap 5 — auto-detect industry pack from catalog tags.

The matcher reads catalog tags / domain values and picks the
matching industry pack name when one is configured. Tests the
three resolution paths:

1. Direct industry tag (``industry: telco``) — wins when present.
2. Direct industry tag with synonym (``industry: telecom``) — alias
   resolution within :data:`INDUSTRY_DOMAIN_HINTS`.
3. Indirect domain tag (``domain: party``) — domain word maps to
   industry via the hint table.

Plus the aggregator that turns a list of :class:`CatalogTable`
objects into a single industry vote (plurality wins).

Negative paths:

* Empty / missing tags → ``None`` (caller falls back to the legacy
  ``--industry`` flag behaviour).
* Unrecognised tag values → ``None``.
* Tag values that overlap two industries → caller's responsibility,
  but the plurality aggregator picks the winner deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.copilot.industry.compiler import (
    INDUSTRY_DOMAIN_HINTS,
    detect_industry_from_catalog_tables,
    match_industry_from_catalog_tags,
    match_industry_from_domain,
)

# ----------------------------------------------------------------------
# match_industry_from_domain — substring matching against hint set
# ----------------------------------------------------------------------


class TestMatchIndustryFromDomain:
    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("party", "telecommunications"),
            ("subscriber", "telecommunications"),
            ("customer-360", "telecommunications"),
            ("billing_event", "telecommunications"),
            ("telco", "telecommunications"),
            ("telecom", "telecommunications"),
            ("patient", "healthcare"),
            ("claim", "healthcare"),
            ("encounter_summary", "healthcare"),
            ("transaction", "finance"),
            ("account", "finance"),
            ("iso20022_payment", "finance"),
            ("store", "retail"),
            ("sku", "retail"),
            ("ecommerce", "retail"),
        ],
    )
    def test_known_domain_maps_to_industry(self, domain, expected):
        assert match_industry_from_domain(domain) == expected

    @pytest.mark.parametrize("domain", ["", None, "wibble", "unrelated", "  "])
    def test_unknown_or_empty_returns_none(self, domain):
        assert match_industry_from_domain(domain) is None

    def test_case_insensitive(self):
        assert match_industry_from_domain("PARTY") == "telecommunications"
        assert match_industry_from_domain("Patient") == "healthcare"


# ----------------------------------------------------------------------
# match_industry_from_catalog_tags — full priority chain
# ----------------------------------------------------------------------


class TestMatchIndustryFromCatalogTags:
    def test_direct_industry_tag_wins(self):
        """``industry: telecommunications`` is the strongest signal."""
        assert (
            match_industry_from_catalog_tags({"industry": "telecommunications"})
            == "telecommunications"
        )

    def test_industry_alias_resolves(self):
        """``industry: telco`` → ``telecommunications`` via the alias path."""
        assert match_industry_from_catalog_tags({"industry": "telco"}) == "telecommunications"

    def test_industry_abbreviation_resolves(self):
        """``industry: telecom`` resolves through the hint set
        (the hint set deliberately includes industry-name
        synonyms alongside domain words)."""
        assert match_industry_from_catalog_tags({"industry": "telecom"}) == "telecommunications"
        assert match_industry_from_catalog_tags({"industry": "fintech"}) == "finance"
        assert match_industry_from_catalog_tags({"industry": "ecommerce"}) == "retail"

    def test_vertical_tag_treated_as_industry(self):
        assert match_industry_from_catalog_tags({"vertical": "retail"}) == "retail"

    def test_domain_tag_indirect_match(self):
        """When no ``industry:`` tag, ``domain:`` is the secondary
        signal — works for both Snowflake (``domain``) and dbt
        Mesh (``business_domain``) conventions."""
        assert match_industry_from_catalog_tags({"domain": "party"}) == "telecommunications"
        assert match_industry_from_catalog_tags({"business_domain": "claim"}) == "healthcare"
        assert match_industry_from_catalog_tags({"data_domain": "transaction"}) == "finance"
        assert match_industry_from_catalog_tags({"subject_area": "store"}) == "retail"

    def test_industry_tag_beats_domain_tag(self):
        """When both fire, ``industry:`` wins — that's the more
        intentional metadata."""
        result = match_industry_from_catalog_tags(
            {
                "industry": "healthcare",
                "domain": "store",  # would map to retail — overridden
            }
        )
        assert result == "healthcare"

    @pytest.mark.parametrize(
        "tags",
        [
            {},
            None,
            {"unrelated": "value"},
            {"team": "analytics-eng"},  # owner tag, not industry/domain
        ],
    )
    def test_no_match_returns_none(self, tags):
        assert match_industry_from_catalog_tags(tags) is None

    def test_case_insensitive_keys(self):
        """Catalog tags often have different casing conventions —
        ``DOMAIN: party`` should match the same as ``domain: party``."""
        assert match_industry_from_catalog_tags({"DOMAIN": "party"}) == "telecommunications"
        assert match_industry_from_catalog_tags({"Industry": "retail"}) == "retail"


# ----------------------------------------------------------------------
# detect_industry_from_catalog_tables — plurality vote
# ----------------------------------------------------------------------


def _table(tags: dict) -> SimpleNamespace:
    """Minimal ``CatalogTable``-shaped stub for the aggregator."""
    return SimpleNamespace(tags=tags)


class TestDetectIndustryFromCatalogTables:
    def test_unanimous_telco_tables(self):
        tables = [
            _table({"domain": "party"}),
            _table({"domain": "subscriber"}),
            _table({"domain": "service"}),
        ]
        assert detect_industry_from_catalog_tables(tables) == "telecommunications"

    def test_plurality_winner(self):
        """Mixed votes — plurality wins."""
        tables = [
            _table({"domain": "patient"}),  # healthcare
            _table({"domain": "claim"}),  # healthcare
            _table({"domain": "subscriber"}),  # telecommunications
        ]
        assert detect_industry_from_catalog_tables(tables) == "healthcare"

    def test_empty_list_returns_none(self):
        assert detect_industry_from_catalog_tables([]) is None

    def test_no_tagged_tables_returns_none(self):
        tables = [_table({}), _table({"foo": "bar"})]
        assert detect_industry_from_catalog_tables(tables) is None

    def test_partial_tagging_still_picks_winner(self):
        """Realistic scenario: 5 tables, only 2 have domain tags.
        Aggregator picks based on the tagged tables only."""
        tables = [
            _table({"domain": "store"}),  # retail
            _table({"domain": "sku"}),  # retail
            _table({}),  # no tags — abstains
            _table({"foo": "bar"}),  # unrelated tags — abstains
            _table({}),  # no tags — abstains
        ]
        assert detect_industry_from_catalog_tables(tables) == "retail"


# ----------------------------------------------------------------------
# Hint table sanity — every shipping pack has hints
# ----------------------------------------------------------------------


def test_every_shipping_pack_has_hints():
    """The four shipping industry packs must each have a matching
    entry in the hint table — otherwise the auto-detect for that
    industry would never fire even when catalogs are well-tagged."""
    expected = {"telecommunications", "healthcare", "finance", "retail"}
    assert set(INDUSTRY_DOMAIN_HINTS) == expected


def test_hint_sets_are_non_empty():
    """A hint set with zero entries is a misconfiguration — the
    industry would auto-detect for everything (matching empty)
    or nothing (matching never)."""
    for industry, hints in INDUSTRY_DOMAIN_HINTS.items():
        assert hints, f"{industry} has no hint entries"
        assert all(isinstance(h, str) and h for h in hints), f"{industry} has invalid hint entries"
