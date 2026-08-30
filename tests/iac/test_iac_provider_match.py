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

"""Tests for ``iac.provider_match`` — cloud detection + the --provider gate."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import load_contract_with_overlay
from fluid_build.iac import IAC_PLUGINS, get_iac_plugin, provider_match

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


class TestCanonicalCloud:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("aws", "aws"),
            ("S3", "aws"),
            ("Athena", "aws"),
            ("google", "gcp"),
            ("BigQuery", "gcp"),
            ("snowflake", "snowflake"),
            ("confluent", "confluent"),
            ("duckdb", "local"),
            ("azure", ""),
            ("", ""),
            (None, ""),
            (42, ""),
        ],
    )
    def test_normalises_documented_spellings(self, token, expected):
        assert provider_match.canonical_cloud(token) == expected


class TestDetectCloudDeclarations:
    def test_reports_where_the_cloud_was_declared(self):
        contract = {"exposes": [{"binding": {"platform": "bigquery"}}]}
        assert provider_match.detect_cloud_declarations(contract) == [
            ("gcp", "exposes[0].binding.platform")
        ]

    def test_top_level_binding(self):
        contract = {"binding": {"provider": "snowflake"}}
        assert provider_match.detect_cloud_declarations(contract) == [
            ("snowflake", "binding.provider")
        ]

    def test_builds_runtime_platform(self):
        contract = {"builds": [{"execution": {"runtime": {"platform": "aws"}}}]}
        assert provider_match.detect_cloud_declarations(contract) == [
            ("aws", "builds[0].execution.runtime.platform")
        ]

    def test_region_fallback_only_when_nothing_stronger(self):
        assert provider_match.detect_clouds({"binding": {"region": "eu-west-2"}}) == ["aws"]
        # A GCP region is not an AWS region and must not trigger the fallback.
        assert provider_match.detect_clouds({"binding": {"region": "us-central1"}}) == []
        # An explicit platform always wins over the region hint.
        contract = {"binding": {"platform": "gcp", "region": "eu-west-2"}}
        assert provider_match.detect_clouds(contract) == ["gcp"]

    def test_deduplicates_on_first_declaration(self):
        contract = {
            "exposes": [
                {"binding": {"platform": "aws"}},
                {"binding": {"platform": "s3"}},
            ]
        }
        assert provider_match.detect_cloud_declarations(contract) == [
            ("aws", "exposes[0].binding.platform")
        ]

    def test_tolerates_malformed_entries(self):
        contract = {"exposes": [None, {}, {"binding": None}], "builds": ["nope", None]}
        assert provider_match.detect_cloud_declarations(contract) == []


class TestCheckProviderMatchesContract:
    def test_auto_and_empty_are_noops(self):
        contract = {"binding": {"provider": "aws"}}
        provider_match.check_provider_matches_contract(contract, "auto")
        provider_match.check_provider_matches_contract(contract, "")
        provider_match.check_provider_matches_contract(contract, None)

    def test_undetectable_contract_is_a_noop(self):
        # `generate_iac_no_provider` tells the operator to pass --provider
        # here. There is nothing to contradict, so the gate must not fire.
        provider_match.check_provider_matches_contract({}, "gcp")
        provider_match.check_provider_matches_contract({"exposes": []}, "snowflake")

    def test_matching_provider_passes(self):
        provider_match.check_provider_matches_contract({"binding": {"provider": "s3"}}, "aws")

    def test_mismatch_raises_with_structured_facts(self):
        contract = {"exposes": [{"binding": {"platform": "aws"}}]}
        with pytest.raises(provider_match.ProviderBindingMismatch) as exc:
            provider_match.check_provider_matches_contract(contract, "gcp")
        assert exc.value.requested == "gcp"
        assert exc.value.detected == ["aws"]
        assert exc.value.source == "exposes[0].binding.platform"

    def test_ambiguous_contract_accepts_either_declared_cloud(self):
        contract = {
            "exposes": [
                {"binding": {"platform": "gcp"}},
                {"binding": {"platform": "aws"}},
            ]
        }
        provider_match.check_provider_matches_contract(contract, "gcp")
        provider_match.check_provider_matches_contract(contract, "aws")
        with pytest.raises(provider_match.ProviderBindingMismatch):
            provider_match.check_provider_matches_contract(contract, "snowflake")


class TestGateAgreesWithTheEmitters:
    """The gate and the emitters must agree, checked in both directions.

    PR #475's discipline: if the gate rejects where the emitter would have
    emitted, it blocks a working contract; if it passes where the emitter
    emits nothing, it waves through the silent no-op it exists to catch.
    Every example contract is run against every plugin.
    """

    @staticmethod
    def _contracts():
        # Every shipped example, whatever it targets. The set is walked
        # rather than listed so a newly added example is covered the day it
        # lands, which is the point of an agreement test.
        return sorted(_EXAMPLES.rglob("*.fluid.yaml"))

    # The only contract x plugin pairs where a *mismatched* plugin still
    # emits something. All three are the same shape: an S3-bound expose is
    # structurally compatible with the GCP emitter, which happily produces a
    # google_storage_bucket named after the S3 bucket and carrying
    # `location: us-east-1` — an AWS region, which is not a valid GCS
    # location. They are why the gate cannot be a zero-resource check alone,
    # and why it belongs on the provider/binding pair rather than the output.
    _WRONG_CLOUD_EMITS = {
        ("aws-glue-data-lake/contract-etl-job.fluid.yaml", "gcp"),
        ("aws-glue-data-lake/contract-iceberg.fluid.yaml", "gcp"),
        ("aws-iceberg-lakehouse/contract.fluid.yaml", "gcp"),
    }

    def _mismatched_pairs_that_emit(self):
        logger = logging.getLogger("test")
        found = set()
        for path in self._contracts():
            try:
                contract = load_contract_with_overlay(str(path), None, logger)
            except Exception:  # noqa: BLE001 — unparseable fixtures are not our subject
                continue
            for name in sorted(IAC_PLUGINS):
                try:
                    provider_match.check_provider_matches_contract(contract, name)
                except provider_match.ProviderBindingMismatch:
                    plugin = get_iac_plugin(name)
                    try:
                        emitted = sum(len(v) for v in plugin.emit(contract, []).values())
                    except Exception:  # noqa: BLE001 — an emitter that throws emits nothing
                        emitted = 0
                    if emitted:
                        found.add((str(path.relative_to(_EXAMPLES)), name))
        return found

    def test_rejected_pairs_are_empty_or_a_known_wrong_cloud_emit(self):
        """No false positives: a rejected pair emits nothing, or wrong output.

        A new entry here means some emitter started cross-emitting for a
        cloud the contract does not declare — worth a look, not a silent
        pass. A disappearing entry means an emitter got stricter, and the
        pin should shrink with it.
        """
        assert self._mismatched_pairs_that_emit() == self._WRONG_CLOUD_EMITS

    def test_gate_accepts_every_pair_that_emits(self):
        """No false negatives on the accept side: whatever emits must pass."""
        logger = logging.getLogger("test")
        for path in self._contracts():
            try:
                contract = load_contract_with_overlay(str(path), None, logger)
            except Exception:  # noqa: BLE001
                continue
            for name in sorted(IAC_PLUGINS):
                plugin = get_iac_plugin(name)
                try:
                    emitted = sum(len(v) for v in plugin.emit(contract, []).values())
                except Exception:  # noqa: BLE001
                    continue
                if not emitted:
                    continue
                try:
                    provider_match.check_provider_matches_contract(contract, name)
                except provider_match.ProviderBindingMismatch as exc:
                    # Only the known shape-compatible GCP case may land here.
                    assert name == "gcp", (
                        f"{path.relative_to(_EXAMPLES)} emits {emitted} {name} "
                        f"resources but the gate rejects it: {exc}"
                    )


class TestGenerateIacWrappersStillDelegate:
    """The private names kept for back-compat resolve through the shared table."""

    def test_wrappers_match_the_shared_implementation(self):
        contract = {"exposes": [{"binding": {"platform": "bigquery"}}]}
        assert generate_iac._detect_clouds(contract) == provider_match.detect_clouds(contract)
        assert generate_iac._canonical_cloud("s3") == "aws"
        assert generate_iac._candidate_regions({"binding": {"region": "eu-west-2"}}) == [
            "eu-west-2"
        ]
