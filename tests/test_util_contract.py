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

"""Tests for util/contract.py — version-agnostic field adapters and normalization."""

import logging

import pytest

from fluid_build.util.contract import (
    consumes_to_canonical_ports,
    get_build_engine,
    get_builds,
    get_consume_id,
    get_consume_ref,
    get_contract_version,
    get_expose_binding,
    get_expose_contract,
    get_expose_id,
    get_expose_kind,
    get_expose_location,
    get_owner,
    get_primary_build,
    slugify_identifier,
)


# ── get_expose_id ───────────────────────────────────────────────────
class TestGetExposeId:
    def test_expose_id_057(self):
        assert get_expose_id({"exposeId": "e1"}) == "e1"

    def test_expose_id_040(self):
        assert get_expose_id({"id": "e2"}) == "e2"

    def test_prefers_expose_id(self):
        assert get_expose_id({"exposeId": "new", "id": "old"}) == "new"

    def test_empty(self):
        assert get_expose_id({}) is None


# ── get_expose_kind ─────────────────────────────────────────────────
class TestGetExposeKind:
    def test_kind_057(self):
        assert get_expose_kind({"kind": "table"}) == "table"

    def test_type_040(self):
        assert get_expose_kind({"type": "view"}) == "view"

    def test_prefers_kind(self):
        assert get_expose_kind({"kind": "table", "type": "view"}) == "table"

    def test_empty(self):
        assert get_expose_kind({}) is None


# ── get_expose_binding ──────────────────────────────────────────────
class TestGetExposeBinding:
    def test_binding_dict(self):
        b = get_expose_binding({"binding": {"provider": "aws"}})
        assert b == {"provider": "aws"}

    def test_location_string_fallback(self):
        b = get_expose_binding({"location": "s3://bucket/path"})
        assert b == {"location": "s3://bucket/path"}

    def test_empty(self):
        assert get_expose_binding({}) is None

    def test_location_non_string_ignored(self):
        assert get_expose_binding({"location": 42}) is None


# ── get_expose_location ─────────────────────────────────────────────
class TestGetExposeLocation:
    def test_from_binding(self):
        loc = get_expose_location({"binding": {"location": "s3://b"}})
        assert loc == "s3://b"

    def test_from_direct_location(self):
        loc = get_expose_location({"location": "gs://b"})
        assert loc == "gs://b"

    def test_empty(self):
        assert get_expose_location({}) is None


# ── get_builds ──────────────────────────────────────────────────────
class TestGetBuilds:
    def test_builds_array(self):
        assert get_builds({"builds": [{"engine": "dbt"}]}) == [{"engine": "dbt"}]

    def test_single_build_wrapped(self):
        assert get_builds({"build": {"engine": "spark"}}) == [{"engine": "spark"}]

    def test_empty(self):
        assert get_builds({}) == []

    def test_builds_non_list_ignored(self):
        assert get_builds({"builds": "invalid"}) == []


# ── get_primary_build ───────────────────────────────────────────────
class TestGetPrimaryBuild:
    def test_returns_first(self):
        assert get_primary_build({"builds": [{"engine": "a"}, {"engine": "b"}]}) == {"engine": "a"}

    def test_none_when_empty(self):
        assert get_primary_build({}) is None


# ── get_build_engine ────────────────────────────────────────────────
class TestGetBuildEngine:
    def test_engine(self):
        assert get_build_engine({"engine": "dbt"}) == "dbt"

    def test_type_fallback(self):
        assert get_build_engine({"type": "spark"}) == "spark"

    def test_empty(self):
        assert get_build_engine({}) is None


# ── simple getters ──────────────────────────────────────────────────
class TestSimpleGetters:
    def test_contract_version(self):
        assert get_contract_version({"fluidVersion": "0.5.7"}) == "0.5.7"

    def test_contract_version_empty(self):
        assert get_contract_version({}) is None

    def test_expose_contract(self):
        assert get_expose_contract({"contract": {"dq": []}}) == {"dq": []}


# ── get_consume_id ──────────────────────────────────────────────────
class TestGetConsumeId:
    def test_prefers_expose_id(self):
        assert get_consume_id({"exposeId": "new", "id": "old"}) == "new"

    def test_legacy_id_040(self):
        assert get_consume_id({"id": "legacy"}) == "legacy"

    def test_modern_exposeId_072(self):
        assert get_consume_id({"exposeId": "modern"}) == "modern"

    def test_empty(self):
        assert get_consume_id({}) is None


# ── get_consume_ref ─────────────────────────────────────────────────
class TestGetConsumeRef:
    def test_prefers_product_id(self):
        assert get_consume_ref({"productId": "p1", "ref": "r1"}) == "p1"

    def test_legacy_ref_040(self):
        assert get_consume_ref({"ref": "legacy"}) == "legacy"

    def test_modern_product_id_072(self):
        assert get_consume_ref({"productId": "silver.hr.people_v2"}) == "silver.hr.people_v2"

    def test_empty(self):
        assert get_consume_ref({}) is None


# ── get_owner ───────────────────────────────────────────────────────
class TestGetOwner:
    def test_metadata_owner_072_shape(self):
        """0.7.2 canonical location: metadata.owner."""
        contract = {"metadata": {"owner": {"team": "data-eng", "email": "e@x.com"}}}
        assert get_owner(contract) == {"team": "data-eng", "email": "e@x.com"}

    def test_top_level_owner_legacy(self):
        """Legacy contracts may carry a top-level ``owner`` block — still readable."""
        contract = {"owner": {"team": "legacy"}}
        assert get_owner(contract) == {"team": "legacy"}

    def test_prefers_metadata_owner_when_both_present(self):
        """0.7.2 mandates ``metadata.owner`` and forbids top-level ``owner``
        (via ``additionalProperties: false`` at the document root). When both
        are present (only possible in malformed / partially-migrated
        contracts), the canonical location wins."""
        contract = {
            "owner": {"team": "top-legacy"},
            "metadata": {"owner": {"team": "meta-canonical"}},
        }
        assert get_owner(contract) == {"team": "meta-canonical"}

    def test_empty_metadata_owner_falls_back_to_top_level(self):
        """Empty mapping at metadata.owner must not short-circuit — fall
        back to the legacy top-level location."""
        contract = {"owner": {"team": "top"}, "metadata": {"owner": {}}}
        assert get_owner(contract) == {"team": "top"}

    def test_no_owner_returns_empty_mapping(self):
        assert get_owner({}) == {}

    def test_non_mapping_owner_is_ignored(self):
        contract = {"owner": "not-a-dict", "metadata": {"owner": {"team": "ok"}}}
        assert get_owner(contract) == {"team": "ok"}

    def test_non_mapping_metadata_owner_falls_back_to_top_level(self):
        contract = {"owner": {"team": "top"}, "metadata": {"owner": "nope"}}
        assert get_owner(contract) == {"team": "top"}


# ── consumes_to_canonical_ports ─────────────────────────────────────
class TestConsumesToCanonicalPorts:
    def test_empty_consumes(self):
        assert consumes_to_canonical_ports({}) == []
        assert consumes_to_canonical_ports({"consumes": []}) == []

    def test_non_list_consumes_returns_empty(self):
        """A dict or scalar under 'consumes' must not crash — return empty."""
        assert consumes_to_canonical_ports({"consumes": {"x": 1}}) == []
        assert consumes_to_canonical_ports({"consumes": "bad"}) == []

    def test_modern_072_shape(self):
        contract = {
            "consumes": [
                {
                    "productId": "silver.hr.people_v2",
                    "exposeId": "people_snapshot",
                    "purpose": "Join employees into the 360 view.",
                }
            ]
        }
        ports = consumes_to_canonical_ports(contract)
        assert len(ports) == 1
        p = ports[0]
        assert p["id"] == "people_snapshot"
        assert p["name"] == "people_snapshot"
        assert p["description"] == "Join employees into the 360 view."
        assert p["reference"] == "silver.hr.people_v2"
        assert p["version"] == "1"
        # Fields not explicitly set on 0.7.2 consumeRef must stay None/empty.
        assert p["contract_id"] is None
        assert p["required"] is None
        assert p["kind"] is None
        assert p["constraints"] is None
        # 0.7.2-canonical optional fields: absent on this minimal entry.
        assert p["version_constraint"] is None
        assert p["qos_expectations"] is None
        assert p["required_policies"] is None
        assert p["tags"] is None
        assert p["labels"] is None

    def test_072_canonical_optional_fields_preserved(self):
        """Every optional field the 0.7.2 ``consumeRef`` schema permits
        (``versionConstraint``, ``qosExpectations``, ``requiredPolicies``,
        ``tags``, ``labels``) must be surfaced in the canonical view so
        providers can forward them without re-parsing the raw contract."""
        contract = {
            "consumes": [
                {
                    "productId": "silver.finance.ledger_v3",
                    "exposeId": "ledger",
                    "purpose": "Source of truth for reconciliation.",
                    "versionConstraint": ">=3.2.0 <4.0.0",
                    "qosExpectations": {
                        "freshnessMax": "PT15M",
                        "minCompleteness": 0.99,
                    },
                    "requiredPolicies": ["pii-mask", "region-eu"],
                    "tags": ["finance", "gold"],
                    "labels": {"criticality": "tier-1"},
                }
            ]
        }
        p = consumes_to_canonical_ports(contract)[0]
        assert p["version_constraint"] == ">=3.2.0 <4.0.0"
        assert p["qos_expectations"] == {
            "freshnessMax": "PT15M",
            "minCompleteness": 0.99,
        }
        assert p["required_policies"] == ["pii-mask", "region-eu"]
        assert p["tags"] == ["finance", "gold"]
        assert p["labels"] == {"criticality": "tier-1"}

    def test_072_canonical_fields_reject_wrong_types(self):
        """Non-mapping ``qosExpectations``/``labels``, non-list ``tags``/
        ``requiredPolicies``, and empty collections all degrade to ``None``
        so downstream ``if port[...]:`` checks work predictably."""
        contract = {
            "consumes": [
                {
                    "productId": "x.y",
                    "exposeId": "z",
                    "qosExpectations": "not-a-dict",
                    "requiredPolicies": "not-a-list",
                    "tags": {},
                    "labels": [],
                    "versionConstraint": "",
                }
            ]
        }
        p = consumes_to_canonical_ports(contract)[0]
        assert p["qos_expectations"] is None
        assert p["required_policies"] is None
        assert p["tags"] is None
        assert p["labels"] is None
        assert p["version_constraint"] is None

    def test_072_canonical_fields_are_defensive_copies(self):
        """Mutating the returned canonical port must not mutate the source
        contract — providers frequently rewrite tags/labels locally."""
        src_tags = ["a", "b"]
        src_labels = {"k": "v"}
        contract = {
            "consumes": [
                {
                    "productId": "x.y",
                    "exposeId": "z",
                    "tags": src_tags,
                    "labels": src_labels,
                }
            ]
        }
        p = consumes_to_canonical_ports(contract)[0]
        p["tags"].append("c")
        p["labels"]["k2"] = "v2"
        # Source unchanged
        assert src_tags == ["a", "b"]
        assert src_labels == {"k": "v"}

    def test_legacy_040_shape(self):
        contract = {"consumes": [{"id": "people", "ref": "bronze.people"}]}
        ports = consumes_to_canonical_ports(contract)
        assert ports[0]["id"] == "people"
        assert ports[0]["reference"] == "bronze.people"

    def test_extension_fields_preserved_when_set(self):
        """Contracts that pre-date strict 0.7.2 additionalProperties=false may
        carry ``contractId``/``required``/``kind``/``constraints``; the canonical
        form preserves them so downstream providers can optionally honor them."""
        contract = {
            "consumes": [
                {
                    "productId": "x.y",
                    "exposeId": "z",
                    "contractId": "x.y.contract.v1",
                    "required": False,
                    "kind": "data",
                    "constraints": {"min_rows": 1000},
                }
            ]
        }
        p = consumes_to_canonical_ports(contract)[0]
        assert p["contract_id"] == "x.y.contract.v1"
        assert p["required"] is False
        assert p["kind"] == "data"
        assert p["constraints"] == {"min_rows": 1000}

    def test_source_system_extension_preserved_when_set(self):
        contract = {
            "consumes": [
                {
                    "productId": "x.y",
                    "exposeId": "z",
                    "sourceSystem": "bss-crm",
                }
            ]
        }
        p = consumes_to_canonical_ports(contract)[0]
        assert p["source_system_id"] == "bss-crm"

    def test_contract_id_alternate_spellings(self):
        """Both ``contractId`` and ``contract_id`` are accepted on read."""
        assert (
            consumes_to_canonical_ports(
                {"consumes": [{"productId": "x", "exposeId": "y", "contract_id": "snake"}]}
            )[0]["contract_id"]
            == "snake"
        )

    def test_explicit_none_required_treated_as_unset(self):
        """``required: None`` explicitly set must behave like omitted."""
        p = consumes_to_canonical_ports({"consumes": [{"exposeId": "x", "required": None}]})[0]
        assert p["required"] is None

    def test_purpose_empty_falls_back_to_description(self):
        """Empty ``purpose`` should defer to ``description`` rather than
        shadow it with an empty string."""
        p = consumes_to_canonical_ports(
            {"consumes": [{"exposeId": "x", "purpose": "", "description": "desc"}]}
        )[0]
        assert p["description"] == "desc"

    def test_default_version_override(self):
        p = consumes_to_canonical_ports({"consumes": [{"exposeId": "x"}]}, default_version="2")[0]
        assert p["version"] == "2"

    def test_skips_non_mapping_entries_with_warning(self, caplog):
        contract = {"consumes": ["not-a-mapping", 42, None, {"exposeId": "ok"}]}
        logger = logging.getLogger("util_contract_test_skip_nonmap")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            ports = consumes_to_canonical_ports(contract, logger=logger)
        assert [p["id"] for p in ports] == ["ok"]
        warnings = [r for r in caplog.records if "expected mapping" in r.getMessage()]
        assert len(warnings) == 3

    def test_skips_entries_missing_id_with_warning(self, caplog):
        contract = {
            "consumes": [
                {"productId": "only-ref"},  # no exposeId/id
                {"exposeId": "keep", "productId": "x"},
            ]
        }
        logger = logging.getLogger("util_contract_test_skip_missing_id")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            ports = consumes_to_canonical_ports(contract, logger=logger)
        assert [p["id"] for p in ports] == ["keep"]
        missing_id = [r for r in caplog.records if "missing required" in r.getMessage()]
        assert len(missing_id) == 1

    def test_skip_without_logger_does_not_crash(self):
        """Providers that don't pass a logger must still get a graceful
        skip (no AttributeError, no unconditional log call)."""
        ports = consumes_to_canonical_ports({"consumes": ["bad", {"exposeId": "ok"}]})
        assert [p["id"] for p in ports] == ["ok"]


# ── slugify_identifier ──────────────────────────────────────────────
class TestSlugifyIdentifier:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("My Project", "my-project"),
            ("my_project", "my-project"),
            ("  Spaced Out  ", "spaced-out"),
            ("UPPER", "upper"),
            ("multi   spaces", "multi-spaces"),
            ("already-a-slug", "already-a-slug"),
            ("a.b.c", "a-b-c"),
            ("foo!@#$bar", "foo-bar"),
            # single alnum character — valid 0.7.2 identifier per pattern
            ("A", "a"),
        ],
    )
    def test_common_cases(self, raw, expected):
        assert slugify_identifier(raw) == expected

    def test_leading_digit_gets_x_prefix(self):
        """0.7.2 identifier pattern requires alnum/underscore at start. A
        purely numeric project name must be rewritten to stay valid."""
        assert slugify_identifier("123abc") == "x-123abc"
        assert slugify_identifier("42") == "x-42"

    def test_trailing_dashes_stripped(self):
        assert slugify_identifier("project---") == "project"

    def test_leading_dashes_stripped(self):
        assert slugify_identifier("---project") == "project"

    def test_non_ascii_collapses_to_fallback(self):
        """Non-ASCII characters are stripped, then the empty result falls
        back to the caller-supplied default."""
        assert slugify_identifier("こんにちは") == "project"
        assert slugify_identifier("こんにちは", fallback="my-app") == "my-app"

    def test_numeric_fallback_gets_leading_digit_guard(self):
        """The leading-digit guard must apply to the fallback too, so
        ``slugify_identifier('', fallback='123')`` returns a valid FLUID
        identifier (not ``'123'``, which violates the identifier pattern)."""
        assert slugify_identifier("", fallback="123") == "x-123"
        assert slugify_identifier("こんにちは", fallback="42") == "x-42"

    def test_dirty_fallback_is_slug_cleaned(self):
        """The fallback itself is slug-cleaned — a caller passing
        ``'My App!'`` as a fallback must not bypass the sanitizer."""
        assert slugify_identifier("", fallback="My App!") == "my-app"

    def test_empty_fallback_returns_sentinel(self):
        """If both input and fallback collapse to empty, the function
        returns the single-character sentinel ``'x'`` — guaranteed valid
        under the 0.7.2 identifier pattern."""
        assert slugify_identifier("", fallback="") == "x"
        assert slugify_identifier("!!!", fallback="---") == "x"

    def test_empty_input_returns_fallback(self):
        assert slugify_identifier("") == "project"
        assert slugify_identifier("   ") == "project"

    def test_none_input_returns_fallback(self):
        assert slugify_identifier(None) == "project"  # type: ignore[arg-type]

    def test_punctuation_only_returns_fallback(self):
        assert slugify_identifier("!!!") == "project"
        assert slugify_identifier("---") == "project"

    def test_result_matches_fluid_identifier_pattern(self):
        """Every slug this function produces (for non-fallback input) must
        satisfy the FLUID 0.7.2 identifier regex so it's safe to embed in
        a contract ``id``."""
        import re

        fluid_identifier = re.compile(r"^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$")
        samples = [
            "My Project",
            "my_project",
            "multi   spaces",
            "already-a-slug",
            "foo!@#$bar",
            "A",
            "123abc",
            "project---",
        ]
        for raw in samples:
            slug = slugify_identifier(raw)
            assert fluid_identifier.match(slug), f"slug={slug!r} from raw={raw!r}"
