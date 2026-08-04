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

"""Tests for fluid_build.policy.sovereignty — data sovereignty validation."""

import pytest

from fluid_build.policy.sovereignty import (
    EnforcementMode,
    SovereigntyValidator,
    SovereigntyViolation,
    get_region_jurisdiction,
    validate_sovereignty,
)


class TestEnforcementMode:
    def test_values(self):
        assert EnforcementMode.STRICT.value == "strict"
        assert EnforcementMode.ADVISORY.value == "advisory"
        assert EnforcementMode.AUDIT.value == "audit"


class TestSovereigntyViolation:
    def test_defaults(self):
        v = SovereigntyViolation(severity="error", message="bad")
        assert v.expose_id is None
        assert v.region_found is None
        assert v.region_expected is None
        assert v.suggestion is None

    def test_full(self):
        v = SovereigntyViolation(
            severity="warning",
            message="m",
            expose_id="e1",
            region_found="us-east-1",
            region_expected=["eu-west-1"],
            suggestion="move it",
        )
        assert v.region_found == "us-east-1"


class TestSovereigntyValidator:
    def _contract(self, sovereignty=None, exposes=None):
        c = {}
        if sovereignty is not None:
            c["sovereignty"] = sovereignty
        if exposes is not None:
            c["exposes"] = exposes
        return c

    def test_no_sovereignty_always_valid(self):
        ok, violations = SovereigntyValidator().validate(self._contract())
        assert ok is True
        assert violations == []

    def test_empty_sovereignty_always_valid(self):
        ok, violations = SovereigntyValidator().validate(self._contract(sovereignty={}))
        assert ok is True

    def test_denied_region_always_error(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"deniedRegions": ["us-east-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "us-east-1"}}}],
            )
        )
        assert any(v.severity == "error" for v in violations)

    def test_allowed_region_pass(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"allowedRegions": ["eu-west-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "eu-west-1"}}}],
            )
        )
        errors = [v for v in violations if v.severity == "error"]
        assert errors == []

    def test_region_not_in_allowed_strict_blocks(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"enforcementMode": "strict", "allowedRegions": ["eu-west-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "us-east-1"}}}],
            )
        )
        assert ok is False
        assert any(v.severity == "error" for v in violations)

    def test_region_not_in_allowed_advisory_passes(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"enforcementMode": "advisory", "allowedRegions": ["eu-west-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "us-east-1"}}}],
            )
        )
        assert ok is True  # advisory = allow deployment
        assert len(violations) > 0

    def test_audit_mode_passes(self):
        ok, _ = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"enforcementMode": "audit", "allowedRegions": ["eu-west-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "us-east-1"}}}],
            )
        )
        assert ok is True

    def test_jurisdiction_mismatch_warning(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"jurisdiction": "EU"},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "us-east-1"}}}],
            )
        )
        assert any(
            v.severity == "warning" and "jurisdiction" in v.message.lower() for v in violations
        )

    def test_jurisdiction_match_no_violation(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"jurisdiction": "EU"},
                exposes=[{"exposeId": "e1", "binding": {"location": {"region": "eu-west-1"}}}],
            )
        )
        assert violations == []

    def test_cross_border_transfer_prohibited(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"dataResidency": True, "crossBorderTransfer": False},
                exposes=[
                    {"exposeId": "e1", "binding": {"location": {"region": "eu-west-1"}}},
                    {"exposeId": "e2", "binding": {"location": {"region": "us-east-1"}}},
                ],
            )
        )
        assert any("cross-border" in v.message.lower() for v in violations)

    def test_no_region_skips_validation(self):
        ok, violations = SovereigntyValidator().validate(
            self._contract(
                sovereignty={"allowedRegions": ["eu-west-1"]},
                exposes=[{"exposeId": "e1", "binding": {"location": {}}}],
            )
        )
        assert violations == []


class TestConvenienceFunctions:
    def test_validate_sovereignty_no_policy(self):
        ok, messages = validate_sovereignty({})
        assert ok is True
        assert messages == []

    def test_validate_sovereignty_with_violation(self):
        ok, messages = validate_sovereignty(
            {
                "sovereignty": {"enforcementMode": "strict", "deniedRegions": ["us-east-1"]},
                "exposes": [{"exposeId": "x", "binding": {"location": {"region": "us-east-1"}}}],
            }
        )
        assert len(messages) > 0

    def test_get_region_jurisdiction_known(self):
        assert get_region_jurisdiction("eu-west-1") == "EU"
        assert get_region_jurisdiction("us-east-1") == "US"

    def test_get_region_jurisdiction_unknown(self):
        assert get_region_jurisdiction("mars-central-1") == "Unknown"


class TestSchemaDefaultAlignment:
    """The engine's runtime defaults must equal the schema's declared ones.

    They did not: every one was the permissive inverse of what the schema
    advertises (``enforcementMode`` advisory-not-strict, ``dataResidency``
    False-not-True, ``crossBorderTransfer`` True-not-False). A contract that
    declared a sovereignty policy and relied on the documented defaults was
    therefore evaluated under the weakest possible settings — a GDPR contract
    with exposes in two jurisdictions returned a clean pass.
    """

    @staticmethod
    def _schema_defaults():
        import json
        from pathlib import Path

        from fluid_build.schema_manager import SchemaManager

        version = SchemaManager.latest_bundled_version()
        path = (
            Path(__file__).resolve().parents[1]
            / "fluid_build"
            / "schemas"
            / f"fluid-schema-{version}.json"
        )
        schema = json.loads(path.read_text(encoding="utf-8"))
        defs = schema.get("$defs") or schema.get("definitions") or {}
        return {
            name: spec["default"]
            for name, spec in defs["sovereignty"].get("properties", {}).items()
            if "default" in spec
        }

    def test_engine_defaults_match_the_bundled_schema(self):
        from fluid_build.policy import sovereignty as sov

        declared = self._schema_defaults()
        assert declared["enforcementMode"] == sov.DEFAULT_ENFORCEMENT_MODE
        assert declared["dataResidency"] == sov.DEFAULT_DATA_RESIDENCY
        assert declared["crossBorderTransfer"] == sov.DEFAULT_CROSS_BORDER_TRANSFER

    def test_omitted_enforcement_mode_defaults_to_strict(self):
        """No ``enforcementMode`` must block, not warn."""
        ok, messages = validate_sovereignty(
            {
                "sovereignty": {
                    "jurisdiction": "EU",
                    "allowedRegions": ["eu-central-1", "eu-west-1"],
                },
                "exposes": [{"exposeId": "x", "binding": {"location": {"region": "us-east-1"}}}],
            }
        )
        assert ok is False
        assert any("❌" in m for m in messages)

    def test_omitted_residency_keys_still_catch_cross_border_transfer(self):
        """The reproduced bypass: two jurisdictions used to return a clean pass."""
        ok, messages = validate_sovereignty(
            {
                "sovereignty": {
                    "enforcementMode": "strict",
                    "allowedRegions": ["eu-west-1", "us-east-1"],
                },
                "exposes": [
                    {"exposeId": "eu", "binding": {"location": {"region": "eu-west-1"}}},
                    {"exposeId": "us", "binding": {"location": {"region": "us-east-1"}}},
                ],
            }
        )
        assert ok is False
        assert any("Cross-border" in m for m in messages)

    def test_explicit_permissive_values_are_still_honoured(self):
        """Defaulting strict must not override an author's explicit opt-out."""
        ok, messages = validate_sovereignty(
            {
                "sovereignty": {
                    "enforcementMode": "advisory",
                    "allowedRegions": ["eu-west-1", "us-east-1"],
                    "dataResidency": False,
                    "crossBorderTransfer": True,
                },
                "exposes": [
                    {"exposeId": "eu", "binding": {"location": {"region": "eu-west-1"}}},
                    {"exposeId": "us", "binding": {"location": {"region": "us-east-1"}}},
                ],
            }
        )
        assert ok is True
        assert not any("❌" in m for m in messages)


class TestCrossBorderUnknownJurisdiction:
    """Check 4 must not conflate "no baseline yet" with "jurisdiction unknown".

    ``REGION_JURISDICTION_MAP`` holds ~31 entries and will always lag the
    clouds, so an unmapped region is a common, real state. It used to be
    represented by the same ``None`` as the not-yet-set baseline, which made
    the check simultaneously over-sensitive (two EU regions, one unmapped,
    were reported as a cross-border transfer), under-sensitive (two unmapped
    regions in genuinely different jurisdictions compared equal and passed),
    and order-dependent (the same pair passed or failed depending on which
    expose came first).

    These only became reachable by default once the engine's defaults were
    aligned with the schema (``dataResidency`` true / ``crossBorderTransfer``
    false), which is what arms Check 4.
    """

    # Deliberately synthetic. These tests exercise the *unknown-jurisdiction*
    # path, so they need regions guaranteed to stay off the map — using real
    # ones (eu-south-1, me-central-1) meant the tests broke the moment the
    # table was widened to cover them, which is the opposite of what they are
    # for. ``test_placeholders_really_are_unmapped`` keeps them honest.
    UNMAPPED_A = "zz-unmapped-1"
    UNMAPPED_B = "zz-unmapped-2"

    @staticmethod
    def _contract(*regions, mode="strict"):
        return {
            "sovereignty": {"enforcementMode": mode},
            "exposes": [
                {"exposeId": f"e{i}", "binding": {"location": {"region": r}}}
                for i, r in enumerate(regions)
            ],
        }

    @staticmethod
    def _counts(messages):
        return (
            sum(1 for m in messages if "❌" in m),
            sum(1 for m in messages if "⚠️" in m),
        )

    def test_placeholders_really_are_unmapped(self):
        """Guard the premise of the tests below."""
        from fluid_build.policy.sovereignty import region_jurisdiction_map

        for region in (self.UNMAPPED_A, self.UNMAPPED_B):
            assert region not in region_jurisdiction_map()

    def test_unmapped_region_alongside_a_mapped_one_does_not_block(self):
        """A region the table does not cover must not read as a foreign one."""
        ok, messages = validate_sovereignty(self._contract("eu-west-1", self.UNMAPPED_A))
        errors, warnings = self._counts(messages)
        assert ok is True
        assert errors == 0
        assert warnings == 1  # surfaced, not silently swallowed
        assert "no known jurisdiction" in "".join(messages)

    def test_verdict_is_independent_of_expose_order(self):
        forward = validate_sovereignty(self._contract("eu-west-1", self.UNMAPPED_A))
        reverse = validate_sovereignty(self._contract(self.UNMAPPED_A, "eu-west-1"))
        assert forward[0] == reverse[0]
        assert self._counts(forward[1]) == self._counts(reverse[1])

    def test_two_unmapped_regions_are_not_treated_as_the_same_jurisdiction(self):
        """``None == None`` used to read as "same jurisdiction" and pass clean."""
        ok, messages = validate_sovereignty(self._contract(self.UNMAPPED_A, self.UNMAPPED_B))
        errors, warnings = self._counts(messages)
        assert errors == 0  # we genuinely cannot tell, so we must not assert a violation
        assert warnings == 2  # but we must not claim it is clean either
        assert ok is True

    def test_genuine_cross_border_still_blocks(self):
        ok, messages = validate_sovereignty(self._contract("eu-west-1", "us-east-1"))
        errors, _ = self._counts(messages)
        assert ok is False
        assert errors == 1  # exactly one — the check used to fire once per expose
        assert "EU and US" in "".join(messages)

    def test_single_and_uniform_jurisdictions_stay_clean(self):
        for regions in (("eu-west-1",), ("eu-west-1", "eu-central-1")):
            ok, messages = validate_sovereignty(self._contract(*regions))
            assert ok is True, regions
            assert self._counts(messages) == (0, 0), regions

    def test_region_less_exposes_are_skipped(self):
        contract = self._contract("eu-west-1")
        contract["exposes"].append({"exposeId": "no_region", "binding": {"location": {}}})
        ok, messages = validate_sovereignty(contract)
        assert ok is True
        assert self._counts(messages) == (0, 0)


class TestRegionJurisdictionMap:
    """The map is factual data, and a wrong entry is a silent governance bug.

    Two entries were wrong in the permissive direction: ``eu-west-2`` and
    ``europe-west2`` are both **London**, and both were mapped to ``EU``. A
    product declaring EU-only residency and binding to London reported clean.
    """

    def test_london_is_not_the_eu(self):
        for region in ("eu-west-2", "europe-west2", "uksouth"):
            assert get_region_jurisdiction(region) == "UK", region

    def test_zurich_is_not_the_eu(self):
        for region in ("eu-central-2", "europe-west6", "switzerlandnorth"):
            assert get_region_jurisdiction(region) == "CH", region

    def test_regions_are_derived_not_hand_maintained(self):
        """The volatile half of the data comes from sources that update
        themselves — botocore's shipped endpoints.json for AWS, the vendored
        dgl/cloud-regions csv for GCP/Azure. A hand-kept region list goes stale
        silently, and a stale entry here is a governance bug."""
        from fluid_build.policy.sovereignty import region_jurisdiction_map

        table = region_jurisdiction_map()
        # Spans all three clouds without any of them being typed out in source.
        assert table["eu-west-1"] == "EU"  # AWS
        assert table["europe-west1"] == "EU"  # GCP
        assert table["northeurope"] == "EU"  # Azure
        assert len(table) > 100

    def test_vendored_region_data_ships(self):
        """A wheel without these csvs resolves no GCP or Azure region at all."""
        from fluid_build.policy.sovereignty import _VENDORED_REGION_DATA

        for provider in ("aws", "gcp", "azure"):
            assert (_VENDORED_REGION_DATA / f"{provider}.csv").exists(), provider

    def test_botocore_supplies_regions_the_vendored_csv_lacks(self):
        """Why AWS prefers botocore: the csv was 14 regions behind when
        vendored, missing the AWS European Sovereign Cloud among others."""
        pytest.importorskip("botocore")
        from fluid_build.policy.sovereignty import _load_botocore_aws, _load_vendored

        assert set(_load_botocore_aws()) - set(_load_vendored("aws"))
        assert get_region_jurisdiction("eusc-de-east-1") == "EU"

    def test_eu_member_state_regions_are_eu(self):
        for region in (
            "eu-west-1",  # Ireland
            "eu-west-3",  # Paris
            "eu-central-1",  # Frankfurt
            "eu-south-1",  # Milan
            "eu-south-2",  # Spain
            "eu-north-1",  # Stockholm
            "europe-west1",  # Belgium
            "europe-west4",  # Netherlands
            "europe-central2",  # Warsaw
            "europe-southwest1",  # Madrid
        ):
            assert get_region_jurisdiction(region) == "EU", region

    def test_an_eu_only_product_in_london_is_reported(self):
        """The end-to-end consequence of the corrected mapping."""
        ok, messages = validate_sovereignty(
            {
                "sovereignty": {"jurisdiction": "EU", "enforcementMode": "strict"},
                "exposes": [{"exposeId": "x", "binding": {"location": {"region": "eu-west-2"}}}],
            }
        )
        assert any("eu-west-2" in m for m in messages)
        assert any("UK" in m for m in messages)

    def test_the_aws_provider_and_the_policy_engine_agree(self):
        """One table, not two.

        These were maintained separately and had drifted apart on 16 regions —
        `eu-west-2` was "EU" in one and "UK" in the other, every `ap-*` was a
        single "APAC" in one and per-country in the other. A governance control
        that answers differently depending on which stage asks is worse than
        one that is merely incomplete.
        """
        from fluid_build.policy.sovereignty import region_jurisdiction_map
        from fluid_build.providers.aws.util.sovereignty import REGION_JURISDICTIONS

        canonical = region_jurisdiction_map()
        disagreements = {
            r: (REGION_JURISDICTIONS[r], canonical[r])
            for r in set(REGION_JURISDICTIONS) & set(canonical)
            if REGION_JURISDICTIONS[r] != canonical[r]
        }
        assert disagreements == {}

    def test_unknown_regions_are_still_a_first_class_answer(self):
        """Deriving the table does not remove the need for the unknown path —
        a region can post-date both the installed SDK and the vendored csv."""
        assert get_region_jurisdiction("mars-central-1") == "Unknown"

    def test_eea_is_distinguished_from_the_eu(self):
        """Norway is in the EEA, so GDPR applies — but a contract asking for
        `jurisdiction: EU` has not asked for Norway."""
        assert get_region_jurisdiction("norwayeast") == "EEA"


class TestFallbackWithoutBotocore:
    """The csv-only path — what an install without boto3 actually gets.

    This class exists because CI caught what local testing missed: the dev venv
    had botocore, so every AWS region resolved through it and the vendored-csv
    fallback was never exercised. `eu-south-2` then resolved to EU locally and
    `Unknown` in CI, because the upstream row for it is literally
    ``eu-south-2,"EU () - ???",,,,``.

    A governance table that is complete only when an optional dependency
    happens to be installed is not a governance table.
    """

    @pytest.fixture(autouse=True)
    def _clear_memo(self):
        """The resolved table is memoised process-wide, so a table built with
        botocore stubbed out must never survive into another test."""
        from fluid_build.policy.sovereignty import region_jurisdiction_map

        region_jurisdiction_map.cache_clear()
        yield
        region_jurisdiction_map.cache_clear()

    @staticmethod
    def _csv_only_table(monkeypatch):
        from fluid_build.policy import sovereignty as sov

        monkeypatch.setattr(sov, "_load_botocore_aws", dict)
        sov.region_jurisdiction_map.cache_clear()
        return sov.region_jurisdiction_map()

    def test_incomplete_upstream_rows_are_corrected(self, monkeypatch):
        table = self._csv_only_table(monkeypatch)
        assert table.get("eu-south-2") == "EU"  # AWS "Europe (Spain)"
        assert table.get("us-gov-east-1") == "US-GOV"
        assert table.get("us-gov-west-1") == "US-GOV"

    def test_the_corrections_only_fill_gaps(self, monkeypatch):
        """A correction must never contradict the data it patches.

        If upstream later fills one of these rows in, the correction becomes a
        silent override of real data — so assert the rows really are empty.
        """
        import csv

        from fluid_build.policy.sovereignty import (
            _VENDORED_REGION_DATA,
            SovereigntyValidator,
        )

        with (_VENDORED_REGION_DATA / "aws.csv").open(encoding="utf-8", newline="") as handle:
            rows = {
                r["region"]: (r.get("country_tld") or "").strip() for r in csv.DictReader(handle)
            }
        for region in SovereigntyValidator.VENDORED_CORRECTIONS:
            assert not rows.get(
                region
            ), f"upstream now supplies a country for {region}; drop the correction"

    def test_the_three_clouds_still_resolve_without_botocore(self, monkeypatch):
        table = self._csv_only_table(monkeypatch)
        assert table.get("eu-west-2") == "UK"  # AWS, from the csv
        assert table.get("europe-west1") == "EU"  # GCP
        assert table.get("northeurope") == "EU"  # Azure
        assert len(table) > 100

    def test_botocore_and_the_csv_never_disagree(self):
        """Where both have an answer they must give the same one, or the
        verdict would depend on whether boto3 happens to be installed."""
        pytest.importorskip("botocore")
        from fluid_build.policy.sovereignty import (
            SovereigntyValidator,
            _load_botocore_aws,
            _load_vendored,
        )

        csv_table = _load_vendored("aws")
        csv_table.update(SovereigntyValidator.VENDORED_CORRECTIONS)
        boto_table = _load_botocore_aws()
        conflicts = {
            r: (csv_table[r], boto_table[r])
            for r in set(csv_table) & set(boto_table)
            if csv_table[r] != boto_table[r]
        }
        assert conflicts == {}
