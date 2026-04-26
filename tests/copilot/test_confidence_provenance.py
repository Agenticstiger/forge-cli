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

"""Coverage for confidence scores + per-claim provenance (E11 + E12)."""

from __future__ import annotations

import pytest

from fluid_build.copilot.confidence import (
    Annotation,
    AnnotationLog,
    ClaimProvenance,
    Confidence,
    confidence_level,
)
from fluid_build.copilot.scratchpad import Scratchpad


class TestConfidenceLevel:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (1.0, "high"),
            (0.95, "high"),
            (0.80, "high"),
            (0.79, "medium"),
            (0.50, "medium"),
            (0.49, "low"),
            (0.01, "low"),
            (0.0, "unknown"),
            (None, "unknown"),
            (-0.1, "unknown"),
        ],
    )
    def test_level_buckets(self, score, expected):
        assert confidence_level(score) == expected

    def test_garbage_input_returns_unknown(self):
        assert confidence_level("not-a-float") == "unknown"  # type: ignore[arg-type]


class TestConfidence:
    def test_level_property_uses_score(self):
        c = Confidence(score=0.92, rationale="exact catalog tag match")
        assert c.level == "high"
        assert c.rationale == "exact catalog tag match"

    def test_low_score_low_level(self):
        c = Confidence(score=0.30)
        assert c.level == "low"


class TestAnnotation:
    def test_add_provenance_appends(self):
        ann = Annotation(claim_path="metadata.owner.team")
        ann.add_provenance(
            ClaimProvenance(
                kind="catalog_tag",
                ref="snowflake://owner_team",
                snippet="data-eng",
            )
        )
        ann.add_provenance(
            ClaimProvenance(
                kind="intent_field",
                ref="intent.yaml:owner",
                snippet="data-eng",
            )
        )
        assert len(ann.provenance) == 2
        assert ann.provenance[0].kind == "catalog_tag"
        assert ann.provenance[1].kind == "intent_field"


class TestAnnotationLog:
    def test_annotate_creates_new_entry(self):
        log = AnnotationLog()
        ann = log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.95, rationale="catalog tag"),
            provenance=ClaimProvenance(
                kind="catalog_tag",
                ref="snowflake://domain_tag",
                snippet="commerce",
            ),
        )
        assert ann.claim_path == "metadata.domain"
        assert ann.confidence.score == 0.95
        assert len(ann.provenance) == 1

    def test_annotate_existing_replaces_confidence_if_higher(self):
        log = AnnotationLog()
        log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.40, rationale="modeler synthesis"),
        )
        # Higher score wins.
        log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.95, rationale="catalog tag"),
        )
        assert log.by_path["metadata.domain"].confidence.score == 0.95

    def test_annotate_existing_keeps_higher_confidence(self):
        log = AnnotationLog()
        log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.95, rationale="catalog tag"),
        )
        # Lower score does NOT replace.
        log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.40, rationale="modeler synthesis"),
        )
        assert log.by_path["metadata.domain"].confidence.score == 0.95

    def test_annotate_appends_provenance_across_calls(self):
        log = AnnotationLog()
        log.annotate(
            "metadata.domain",
            provenance=ClaimProvenance(kind="catalog_tag", ref="x"),
        )
        log.annotate(
            "metadata.domain",
            provenance=ClaimProvenance(kind="intent_field", ref="y"),
        )
        ann = log.by_path["metadata.domain"]
        assert len(ann.provenance) == 2

    def test_summary_aggregates_counters(self):
        log = AnnotationLog()
        log.annotate(
            "a",
            confidence=Confidence(score=0.9),
            provenance=ClaimProvenance(kind="catalog_tag", ref="x"),
        )
        log.annotate(
            "b",
            confidence=Confidence(score=0.6),
            provenance=ClaimProvenance(kind="intent_field", ref="y"),
        )
        log.annotate(
            "c",
            confidence=Confidence(score=0.3),
        )
        summary = log.summary()
        assert summary["annotation_count"] == 3
        assert summary["confidence_levels"]["high"] == 1
        assert summary["confidence_levels"]["medium"] == 1
        assert summary["confidence_levels"]["low"] == 1
        assert summary["provenance_kinds"]["catalog_tag"] == 1
        assert summary["provenance_kinds"]["intent_field"] == 1


class TestScratchpadIntegration:
    def test_get_annotations_lazy_create(self):
        pad = Scratchpad()
        # Not created yet.
        assert pad.annotations is None
        log = pad.get_annotations()
        assert isinstance(log, AnnotationLog)
        # Idempotent — same instance on subsequent calls.
        assert pad.get_annotations() is log

    def test_annotations_survive_across_agents(self):
        """Two simulated agents both attach annotations; both
        survive on the same scratchpad's log."""
        pad = Scratchpad()
        # Agent 1 (modeler) attaches a confidence.
        pad.get_annotations().annotate(
            "dv2.hubs.hub_customer.business_key_columns",
            confidence=Confidence(score=0.85, rationale="exact PK match"),
            provenance=ClaimProvenance(
                kind="ddl_constraint",
                ref="raw.customers#PK",
                snippet="customer_id",
            ),
        )
        # Agent 2 (critic) attaches an additional provenance.
        pad.get_annotations().annotate(
            "dv2.hubs.hub_customer.business_key_columns",
            provenance=ClaimProvenance(
                kind="critic_correction",
                ref="hub_customer/v2",
                snippet="confirmed by critic review",
            ),
        )
        ann = pad.get_annotations().by_path["dv2.hubs.hub_customer.business_key_columns"]
        assert ann.confidence.score == 0.85
        assert len(ann.provenance) == 2
