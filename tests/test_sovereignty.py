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

    def test_unmapped_region_in_same_jurisdiction_does_not_block(self):
        """``eu-south-1`` (Milan) is a real EU region absent from the map."""
        ok, messages = validate_sovereignty(self._contract("eu-west-1", "eu-south-1"))
        errors, warnings = self._counts(messages)
        assert ok is True
        assert errors == 0
        assert warnings == 1  # surfaced, not silently swallowed
        assert "no known jurisdiction" in "".join(messages)

    def test_verdict_is_independent_of_expose_order(self):
        forward = validate_sovereignty(self._contract("eu-west-1", "eu-south-1"))
        reverse = validate_sovereignty(self._contract("eu-south-1", "eu-west-1"))
        assert forward[0] == reverse[0]
        assert self._counts(forward[1]) == self._counts(reverse[1])

    def test_two_unmapped_regions_are_not_treated_as_the_same_jurisdiction(self):
        """``None == None`` used to read as "same jurisdiction" and pass clean."""
        ok, messages = validate_sovereignty(self._contract("me-central-1", "us-gov-west-1"))
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
