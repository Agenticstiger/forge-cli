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

"""Inter-agent shared scratchpad / blackboard (Missing #1).

The agentic-native pattern: agents in a multi-stage pipeline often
need to share intermediate state (entities the modeler discovered,
relationships the conceptual stage flagged, dialect drift the
critic noticed). Today, agents pass full ``LogicalDraft`` objects
through Pydantic — every agent re-serialises the whole tree even
when it only cares about a subset.

A scratchpad is a structured, in-memory shared dict scoped to one
forge run. Slots are typed via Pydantic; agents read / write only
the slot(s) they care about. Three benefits:

1. **No full-tree re-serialisation.** Builder reads
   ``scratchpad["entities"]`` directly instead of walking a 2k-line
   LogicalDraft.
2. **Cross-stage signal capture.** Critic agent (Missing #2) writes
   ``scratchpad["critic_findings"]``; Builder picks them up on
   retry without changing the pipeline contract.
3. **Inspectable in tests.** Hermetic tests assert on scratchpad
   slots instead of stubbing the whole modeler.

The scratchpad is **not** a replacement for the staged Pydantic
schemas (LogicalDraft / PhysicalDraft / etc.). Those remain the
authoritative outputs. The scratchpad is for *intermediate
signals* the schemas don't model — feedback loops, cross-stage
hints, RAG-retrieved context.

Design decisions:

* **Per-session, not per-process.** Lives on
  :class:`fluid_build.copilot.agents.base.StageSession`. Every
  forge run gets a fresh scratchpad; no leakage between runs.
* **Typed slots via Pydantic.** Each well-known slot has a
  Pydantic model so accessors are type-checked. Free-form keys
  are accepted via ``set_raw`` / ``get_raw`` for ad-hoc use.
* **Thread-safe writes.** Parallel-physical fanout writes from
  three threads concurrently; reads are lock-free (pre-existing
  values never mutate, only append).

Public surface:

* :class:`Scratchpad` — the per-session container.
* :class:`CriticFinding` — typed slot for Critic agent output.
* :class:`RetrievalResult` — typed slot for RAG retrievals.
* :class:`StageFeedback` — typed slot for stage-to-stage signals.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class CriticFinding:
    """One observation from a critic-style agent.

    ``stage`` names which staged agent's output the critic was
    reviewing (``"logical"`` / ``"builder"`` / etc.). ``severity``
    follows the ``ValidationFinding`` convention (``error`` blocks
    progression; ``warning`` is informational).

    ``suggestion`` carries the next-action the critic recommends —
    the Builder agent uses this on retry to bias its prompt.
    """

    stage: str
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str = ""
    target: str = ""
    """Optional dotted-path to the field the finding applies to
    (e.g. ``"dv2.hubs.hub_customer.business_key_columns"``)."""


@dataclass
class RetrievalResult:
    """One semantic-memory retrieval relevant to the current stage.

    Powers Missing #3 (per-stage RAG). When the modeler runs against
    a new intent, it can pull the top-k most similar past forged
    models from ``memory/semantic`` and surface them here so the
    next stage's prompt sees prior wins.
    """

    namespace: str
    """Store namespace the result came from (e.g. ``memory/semantic``)."""

    key: str
    """Identifier within the namespace."""

    similarity: float
    """Match score in [0, 1]; higher is more similar."""

    summary: str = ""
    """One-line description for inclusion in downstream prompts."""

    payload: Dict[str, Any] = field(default_factory=dict)
    """Optional structured copy of the retrieved record."""


@dataclass
class StageFeedback:
    """Structured signal one stage emits for a downstream stage.

    Powers Missing #4 (structured feedback loops). When the validator
    finds a contract issue, it writes a ``StageFeedback`` with
    ``target_stage="builder"`` and the structured findings; on
    repair-loop rerun, the Builder picks it up and biases its
    prompt accordingly. No more "re-run with the same prompt and
    hope for a different answer."
    """

    source_stage: str
    """Stage that emitted the feedback (``"validator"``, ``"critic"``…)."""

    target_stage: str
    """Stage that should consume the feedback on next run
    (``"builder"`` / ``"logical"`` / …)."""

    summary: str
    """Short human-readable description of what the consumer
    should change."""

    structured: Dict[str, Any] = field(default_factory=dict)
    """Machine-readable details — typed if the consumer agrees on
    a shape, free-form otherwise."""


@dataclass
class Scratchpad:
    """Per-session shared state container.

    Construct one Scratchpad per forge run; pass via
    :class:`StageSession.scratchpad`. Agents read their slots,
    write their slots, and ignore the rest.

    The class is dataclass-shaped so it inspects cleanly in pytest
    output and pickles for the audit trail. The internal lock
    protects the lists; reads of immutable types
    (``RetrievalResult``, ``CriticFinding``, ``StageFeedback``) are
    safe without locking.
    """

    critic_findings: List[CriticFinding] = field(default_factory=list)
    retrievals: List[RetrievalResult] = field(default_factory=list)
    feedback: List[StageFeedback] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    """Free-form slots for ad-hoc inter-agent state. Used sparingly
    — well-known patterns get their own typed slot."""

    annotations: Any = None
    """Per-claim confidence + provenance log (E11 + E12).

    Lazily constructed via :meth:`get_annotations` so older code
    paths that don't care don't pay the import cost. Agents that
    DO emit confidence scores or provenance attach annotations
    here so downstream consumers (Critic, Validator, audit trail,
    cost summary) read from one place."""

    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def get_annotations(self):
        """Return the per-session :class:`AnnotationLog`, lazy.

        Lazy so importers that never use confidence / provenance
        don't pay the module-load cost on every forge run."""
        if self.annotations is None:
            from fluid_build.copilot.confidence import AnnotationLog

            self.annotations = AnnotationLog()
        return self.annotations

    # --- Critic ------------------------------------------------------

    def add_critic_finding(self, finding: CriticFinding) -> None:
        """Append a critic finding. Thread-safe."""
        with self._lock:
            self.critic_findings.append(finding)

    def critic_findings_for_stage(self, stage: str) -> List[CriticFinding]:
        """Return critic findings whose ``stage`` matches ``stage``.

        Used by an agent on rerun to read what the critic said about
        its previous output. Read-only snapshot — caller mutating
        the returned list does NOT affect the scratchpad."""
        return [f for f in self.critic_findings if f.stage == stage]

    # --- Retrievals --------------------------------------------------

    def add_retrieval(self, result: RetrievalResult) -> None:
        """Append a RAG retrieval to the scratchpad."""
        with self._lock:
            self.retrievals.append(result)

    def top_retrievals(
        self,
        *,
        limit: int = 5,
        namespace: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """Return the top-``limit`` retrievals by similarity.

        Optional ``namespace`` filter so e.g. the modeler can
        request "top-5 from memory/semantic" without seeing
        retrievals the catalog adapter cached."""
        candidates = (
            self.retrievals
            if namespace is None
            else [r for r in self.retrievals if r.namespace == namespace]
        )
        return sorted(candidates, key=lambda r: r.similarity, reverse=True)[:limit]

    # --- Feedback ----------------------------------------------------

    def add_feedback(self, feedback: StageFeedback) -> None:
        """Append a stage-to-stage feedback message."""
        with self._lock:
            self.feedback.append(feedback)

    def feedback_for_stage(self, target_stage: str) -> List[StageFeedback]:
        """Return feedback messages addressed to ``target_stage``."""
        return [f for f in self.feedback if f.target_stage == target_stage]

    # --- Raw ad-hoc slots --------------------------------------------

    def set_raw(self, key: str, value: Any) -> None:
        """Set an ad-hoc free-form slot."""
        with self._lock:
            self.raw[key] = value

    def get_raw(self, key: str, default: Any = None) -> Any:
        """Read an ad-hoc free-form slot."""
        return self.raw.get(key, default)


__all__ = [
    "Scratchpad",
    "CriticFinding",
    "RetrievalResult",
    "StageFeedback",
]
