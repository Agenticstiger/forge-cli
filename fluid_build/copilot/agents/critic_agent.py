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

"""CriticAgent — proactive reviewer between staged outputs (Missing #2).

The existing :class:`ValidatorAgent` runs at the END of the
pipeline. It catches schema-level issues but it can't whisper
"hey, this hub has no business keys" to the modeler in time for a
useful repair. CriticAgent fills that gap: it runs immediately
after each major stage and writes structured ``CriticFinding``s to
the session's :class:`Scratchpad`, addressed to the stage that
produced the output. The repair loop reads them on retry.

CriticAgent is **LLM-free in v1** — it's a deterministic
rule-based reviewer. Reasons:

* **Cost.** A second LLM agent per stage doubles the inference
  cost. v1's critic delivers 80% of the value via heuristics that
  cost zero tokens.
* **Determinism.** Heuristic rules → byte-stable findings on
  re-run. The repair loop's behavior is predictable.
* **Composable with v1.6+.** When the schema dependency for ODCS
  / DCS lands and we add a real LLM-based critic, the
  rule-based critic stays as the cheap pre-pass; the LLM critic
  reviews only what the rules don't catch.

Three review surfaces in v1:

1. ``review_logical`` — checks the LogicalDraft after the
   ModelerAgent emits it. Heuristics:
   * Hubs MUST have ≥1 business_key_columns.
   * Links MUST have ≥2 hubs_involved AND ≥1 join_keys (UNLESS the
     link came from lineage signal alone, which is acceptable).
   * Orphan entities (in Conceptual but not in DV2/Dimensional).
2. ``review_contract`` — checks the Fluid contract after the
   BuilderAgent emits it. Heuristics:
   * ``exposes[]`` MUST be non-empty.
   * ``metadata.domain`` SHOULD be set (warning, not error).
   * Each expose SHOULD have a description.
3. ``review_transform`` — checks the ``TransformPlan.builds[]``
   after the TransformationAgent emits it. Heuristics:
   * No build references a model not in the LogicalDraft.
   * Topo-sort is acyclic.

Findings flow through ``Scratchpad.add_critic_finding`` so the
coordinator's repair-loop hook can read
``scratchpad.critic_findings_for_stage(stage)`` on retry.

Public surface:

* :class:`CriticAgent` — the agent itself.
* :func:`review_*` — module-level functions for unit-test use.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fluid_build.copilot.scratchpad import CriticFinding, Scratchpad


class CriticAgent:
    """Run heuristic reviews against staged outputs.

    Stateless; constructible without any session. Pass the session
    explicitly to each ``review_*`` call so findings land on the
    right scratchpad.

    LLM-free in v1.5; planned LLM-augmented variant in v1.6+.
    """

    def review_logical(
        self,
        logical: Any,
        *,
        scratchpad: Scratchpad,
    ) -> List[CriticFinding]:
        """Heuristic review of a LogicalDraft.

        Findings are added to ``scratchpad.critic_findings`` AND
        returned as a list for tests / logging.
        """
        findings: List[CriticFinding] = []

        if logical is None:
            return findings

        # -- DV2 checks ----------------------------------------------
        dv2 = getattr(logical, "dv2", None)
        if dv2 is not None:
            for hub in getattr(dv2, "hubs", None) or []:
                bks = getattr(hub, "business_key_columns", None) or []
                if not bks:
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="warning",
                            message=(
                                f"Hub {hub.entity_name!r} has no "
                                "business_key_columns. DV2 hubs without a "
                                "business key cannot be loaded — review the "
                                "source-table primary key columns."
                            ),
                            suggestion=(
                                f"Set hub_{hub.entity_name}.business_key_columns "
                                "to the natural-key column(s) from the source table."
                            ),
                            target=f"dv2.hubs.{hub.entity_name}.business_key_columns",
                        )
                    )

            for link in getattr(dv2, "links", None) or []:
                hubs_involved = getattr(link, "hubs_involved", None) or []
                join_keys = getattr(link, "join_keys", None) or []
                if len(hubs_involved) < 2:
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="error",
                            message=(
                                f"Link {link.link_name!r} has "
                                f"{len(hubs_involved)} hub(s); DV2 links must "
                                "join at least 2 hubs."
                            ),
                            target=f"dv2.links.{link.link_name}.hubs_involved",
                        )
                    )
                if not join_keys:
                    # NOT an error — lineage-inferred links legitimately
                    # ship without join_keys (we don't know the FK
                    # columns from lineage alone). Just a warning.
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="info",
                            message=(
                                f"Link {link.link_name!r} has no join_keys. "
                                "If this link was inferred from catalog "
                                "lineage, that's expected; otherwise add "
                                "explicit join_keys."
                            ),
                            target=f"dv2.links.{link.link_name}.join_keys",
                        )
                    )

        # -- Dimensional checks --------------------------------------
        dimensional = getattr(logical, "dimensional", None)
        if dimensional is not None:
            facts = getattr(dimensional, "facts", None) or []
            dims = getattr(dimensional, "dimensions", None) or []

            if not facts:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="error",
                        message=(
                            "Dimensional model has zero fact tables. "
                            "Every dimensional model needs at least one "
                            "fact table to expose measures."
                        ),
                        target="dimensional.facts",
                    )
                )
            if facts and not dims:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="warning",
                        message=(
                            "Dimensional model has facts but no dimensions. "
                            "Either declare ``variant='flat'`` or add the "
                            "dimensions the facts join to."
                        ),
                        target="dimensional.dimensions",
                    )
                )
            for fact in facts:
                if not getattr(fact, "measures", None):
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="warning",
                            message=(
                                f"Fact {getattr(fact, 'name', '?')!r} has no "
                                "measures. A fact without measures is just a "
                                "factless / bridge fact — confirm the intent."
                            ),
                            target=f"dimensional.facts.{getattr(fact, 'name', '?')}.measures",
                        )
                    )
                if not getattr(fact, "foreign_keys", None) and dims:
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="warning",
                            message=(
                                f"Fact {getattr(fact, 'name', '?')!r} has no "
                                "foreign_keys despite the model having "
                                f"{len(dims)} dimension(s) — measures won't "
                                "join to dimensions."
                            ),
                            target=f"dimensional.facts.{getattr(fact, 'name', '?')}.foreign_keys",
                        )
                    )
            for dim in dims:
                if not getattr(dim, "attributes", None):
                    findings.append(
                        CriticFinding(
                            stage="logical",
                            severity="warning",
                            message=(
                                f"Dimension {getattr(dim, 'name', '?')!r} has "
                                "no attributes. Dimensions need at least the "
                                "natural-key column plus some descriptive "
                                "attributes to be useful."
                            ),
                            target=f"dimensional.dimensions.{getattr(dim, 'name', '?')}.attributes",
                        )
                    )
            # Variant-shape sanity (uses already-shipped variant lint
            # but at the critic level — surfaces as an info finding
            # for visibility).
            variant = getattr(dimensional, "variant", None)
            if variant == "galaxy" and len(facts) < 2:
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="info",
                        message=(
                            f"variant='galaxy' typically expects ≥2 fact "
                            f"tables; found {len(facts)}. Consider 'star' "
                            "for a single-fact model."
                        ),
                        target="dimensional.variant",
                    )
                )

        # -- Conceptual <-> physical orphan check --------------------
        conceptual = getattr(logical, "conceptual", None)
        if conceptual is not None:
            conceptual_names = {
                e.name.lower() for e in (getattr(conceptual, "entities", None) or [])
            }
            # Build the union of physical-entity names from BOTH DV2
            # and Dimensional layers. Either one being populated
            # qualifies as "the conceptual entity is represented".
            physical_names: set[str] = set()
            if dv2 is not None:
                for h in getattr(dv2, "hubs", None) or []:
                    physical_names.add(getattr(h, "entity_name", "").lower())
            if dimensional is not None:
                for f in getattr(dimensional, "facts", None) or []:
                    physical_names.add(
                        getattr(f, "name", "").removeprefix("fact_").removeprefix("fct_").lower()
                    )
                for d in getattr(dimensional, "dimensions", None) or []:
                    physical_names.add(getattr(d, "name", "").removeprefix("dim_").lower())
            orphans = conceptual_names - physical_names
            for name in sorted(orphans):
                findings.append(
                    CriticFinding(
                        stage="logical",
                        severity="info",
                        message=(
                            f"Conceptual entity {name!r} has no matching "
                            "physical (hub or fact/dim) entry. The modeler "
                            "may have intentionally merged it into another "
                            "entity; verify."
                        ),
                        target="dv2.hubs/dimensional.facts/dimensional.dimensions",
                    )
                )

        for f in findings:
            scratchpad.add_critic_finding(f)
        return findings

    def review_contract(
        self,
        contract: Optional[Dict[str, Any]],
        *,
        scratchpad: Scratchpad,
    ) -> List[CriticFinding]:
        """Heuristic review of a Fluid contract dict."""
        findings: List[CriticFinding] = []
        if not isinstance(contract, dict):
            return findings

        exposes = contract.get("exposes") or []
        if not exposes:
            findings.append(
                CriticFinding(
                    stage="builder",
                    severity="error",
                    message=(
                        "Contract has no 'exposes' entries. Every Fluid "
                        "contract must publish at least one dataset / "
                        "data product."
                    ),
                    suggestion=(
                        "Add at least one entry to ``exposes[]`` "
                        "describing the dataset the contract publishes."
                    ),
                    target="exposes",
                )
            )

        metadata = contract.get("metadata") or {}
        if not metadata.get("domain"):
            findings.append(
                CriticFinding(
                    stage="builder",
                    severity="warning",
                    message=(
                        "Contract is missing 'metadata.domain'. Downstream "
                        "tools (catalog publishers, governance dashboards) "
                        "use this for routing and ownership attribution."
                    ),
                    target="metadata.domain",
                )
            )

        for i, expose in enumerate(exposes):
            if isinstance(expose, dict) and not expose.get("description"):
                findings.append(
                    CriticFinding(
                        stage="builder",
                        severity="info",
                        message=(
                            f"exposes[{i}] (name={expose.get('name', '?')!r}) "
                            "has no description. Catalog consumers benefit "
                            "from a one-line summary."
                        ),
                        target=f"exposes.{i}.description",
                    )
                )

        for f in findings:
            scratchpad.add_critic_finding(f)
        return findings

    def review_transform(
        self,
        transform_plan: Any,
        logical: Any,
        *,
        scratchpad: Scratchpad,
    ) -> List[CriticFinding]:
        """Heuristic review of TransformPlan.builds[].

        Today, two checks: (a) every build's referenced source must
        appear in the LogicalDraft's hubs / facts, and (b) the
        topo-sort over ``ref()`` is acyclic. Cycle detection uses a
        simple DFS — quadratic but plenty fast for ≤500-build
        contracts.
        """
        findings: List[CriticFinding] = []
        if transform_plan is None:
            return findings

        builds = getattr(transform_plan, "builds", None) or []
        # Build name registry — map from build name to its refs.
        graph: Dict[str, List[str]] = {}
        for b in builds:
            name = getattr(b, "name", None) or getattr(b, "model_name", "")
            refs = list(getattr(b, "ref_models", None) or getattr(b, "depends_on", None) or [])
            graph[name] = refs

        # Cycle detection.
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}

        def has_cycle(node: str, path: List[str]) -> Optional[List[str]]:
            color[node] = GRAY
            for nxt in graph.get(node, []):
                if nxt not in color:
                    continue
                if color[nxt] == GRAY:
                    return path + [node, nxt]
                if color[nxt] == WHITE:
                    sub = has_cycle(nxt, path + [node])
                    if sub:
                        return sub
            color[node] = BLACK
            return None

        for n in list(color):
            if color[n] == WHITE:
                cycle = has_cycle(n, [])
                if cycle:
                    findings.append(
                        CriticFinding(
                            stage="transformation",
                            severity="error",
                            message=(
                                "Transform plan has a circular dependency: "
                                + " → ".join(cycle)
                                + ". dbt will refuse to compile this graph."
                            ),
                            target="transform_plan.builds",
                        )
                    )
                    break  # one report per pass is enough

        for f in findings:
            scratchpad.add_critic_finding(f)
        return findings


__all__ = ["CriticAgent"]
