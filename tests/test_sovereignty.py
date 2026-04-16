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
