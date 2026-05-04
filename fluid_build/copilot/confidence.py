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

"""Confidence scoring + per-claim provenance primitives (E11 + E12).

Each agent's structured output today is binary — here's the
answer. World-class agentic systems carry per-claim confidence
and provenance so downstream agents can act differentially
(low-confidence claim → ask critic to verify; high-confidence
claim → trust it; unsourced claim → flag for human review).

This module ships two typed primitives:

* :class:`ClaimProvenance` — where a claim came from (catalog tag,
  intent sentence, prior model retrieval, modeler synthesis, …).
* :class:`Confidence` — float in [0, 1] with a level label and
  the rationale.

They're attached to specific Pydantic outputs via the
``annotations`` dict (free-form; agents that don't care don't
populate it). Critic / Validator / dashboard observers read the
annotations and act accordingly.

The primitives are deliberately **detached** from the existing
Pydantic schemas so they can be applied to any output without
schema-version churn. Agents emit their normal output then
attach annotations as a side-channel via the scratchpad.

Public surface:

* :class:`ClaimProvenance` — typed source attribution.
* :class:`Confidence` — float + level + rationale.
* :class:`Annotation` — claim → confidence + provenance binding.
* :class:`AnnotationLog` — per-output annotation collection.
* :func:`confidence_level` — float → label helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ConfidenceLevel = Literal["high", "medium", "low", "unknown"]
"""Coarse-grained label corresponding to a confidence score.

Levels are derived from the float via :func:`confidence_level`:

* ``high``     — score ≥ 0.80
* ``medium``   — 0.50 ≤ score < 0.80
* ``low``      — 0.0 < score < 0.50
* ``unknown``  — score is None / 0.0 / NaN
"""


def confidence_level(score: Optional[float]) -> ConfidenceLevel:
    """Map a confidence float to a level label."""
    if score is None:
        return "unknown"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s != s:  # NaN
        return "unknown"
    if s <= 0.0:
        return "unknown"
    if s >= 0.80:
        return "high"
    if s >= 0.50:
        return "medium"
    return "low"


ProvenanceKind = Literal[
    "catalog_tag",
    "catalog_description",
    "catalog_lineage",
    "catalog_classification",
    "intent_field",
    "ddl_constraint",
    "industry_skeleton",
    "memory_semantic",
    "memory_episodic",
    "modeler_synthesis",
    "critic_correction",
    "operator_edit",
]
"""Closed enum of where a claim can originate.

* ``catalog_*`` — pulled directly from a catalog adapter (highest trust).
* ``intent_field`` — a intent.yaml field literal.
* ``ddl_constraint`` — a DDL primary key / foreign key.
* ``industry_skeleton`` — an industry pack's seed model.
* ``memory_*`` — past forged models retrieved via RAG.
* ``modeler_synthesis`` — the LLM modeler invented this (lowest trust on its own).
* ``critic_correction`` — the critic told the modeler to change this.
* ``operator_edit`` — a human edited the contract after the forge.
"""


@dataclass
class ClaimProvenance:
    """Where a single claim in the output came from.

    ``kind`` says what category of source. ``ref`` is a free-form
    locator the source uses (e.g. ``"snowflake://DEMO_DB.SEEDED.ORDERS#owner_team_tag"``
    for catalog signal, or ``"intent.yaml:metadata.domain"`` for intent
    fields). ``snippet`` is the actual source text — kept short
    (200 chars) so the audit trail doesn't bloat.
    """

    kind: ProvenanceKind
    ref: str = ""
    snippet: str = ""


@dataclass
class Confidence:
    """One claim's confidence score + rationale.

    ``score`` is in [0, 1]; helper :func:`confidence_level` maps to
    a label. ``rationale`` is the agent's one-line explanation of
    why this score (e.g. "exact catalog tag match" → 0.95;
    "synthesized from heuristics" → 0.40).
    """

    score: float
    rationale: str = ""

    @property
    def level(self) -> ConfidenceLevel:
        return confidence_level(self.score)


@dataclass
class Annotation:
    """One claim's full annotation: confidence + provenance.

    ``claim_path`` is a dotted path identifying the field this
    annotation describes (e.g. ``"dv2.hubs.hub_customer.business_key_columns"``
    or ``"metadata.owner.team"``). Multiple annotations can point
    at the same path (e.g. confidence + multiple provenance
    sources).
    """

    claim_path: str
    confidence: Optional[Confidence] = None
    provenance: List[ClaimProvenance] = field(default_factory=list)

    def add_provenance(self, prov: ClaimProvenance) -> None:
        self.provenance.append(prov)


@dataclass
class AnnotationLog:
    """Collection of annotations for one staged output.

    Lives on the session scratchpad so any agent can read /
    extend it. Annotations are keyed by ``claim_path`` so multiple
    writes against the same path merge rather than duplicate.
    """

    by_path: Dict[str, Annotation] = field(default_factory=dict)

    def annotate(
        self,
        claim_path: str,
        *,
        confidence: Optional[Confidence] = None,
        provenance: Optional[ClaimProvenance] = None,
    ) -> Annotation:
        """Create-or-update the annotation for ``claim_path``.

        Subsequent calls with the same path:
        * Replace ``confidence`` if the new score is higher
          (best-evidence wins).
        * Append the new provenance entry to the list.
        """
        existing = self.by_path.get(claim_path)
        if existing is None:
            existing = Annotation(claim_path=claim_path)
            self.by_path[claim_path] = existing
        if confidence is not None:
            if existing.confidence is None or confidence.score > existing.confidence.score:
                existing.confidence = confidence
        if provenance is not None:
            existing.add_provenance(provenance)
        return existing

    def summary(self) -> Dict[str, Any]:
        """Aggregate counters for the cost summary footer."""
        levels = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        prov_kinds: Dict[str, int] = {}
        for ann in self.by_path.values():
            level = ann.confidence.level if ann.confidence else "unknown"
            levels[level] += 1
            for p in ann.provenance:
                prov_kinds[p.kind] = prov_kinds.get(p.kind, 0) + 1
        return {
            "annotation_count": len(self.by_path),
            "confidence_levels": levels,
            "provenance_kinds": prov_kinds,
        }


__all__ = [
    "Annotation",
    "AnnotationLog",
    "ClaimProvenance",
    "Confidence",
    "ConfidenceLevel",
    "ProvenanceKind",
    "confidence_level",
]
