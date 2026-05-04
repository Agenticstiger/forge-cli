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

"""Standalone helpers for the modeler agent.

Physical extraction from ``modeler_agent.py`` — the ``ModelerAgent``
class itself stays in the original module (its methods carry shared
``self`` state that doesn't extract cleanly), but the ~680 LOC of
free-function helpers (DV2 / dimensional skeleton merging,
relationship inference, scratchpad annotation, tool-research phase,
operator correction injection) live here so the class file is
focused on the agent's main flow.

``modeler_agent.py`` re-imports every public symbol at module top so
existing test patches that target
``fluid_build.copilot.agents.modeler_agent._merge_dv2_skeleton`` (etc.)
still resolve via the module's namespace.
"""

from __future__ import annotations

# Stdlib
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

# Re-import every type / helper the original file imported. The
# helpers below reference these — keep them at top-level so the
# extracted bodies see the same identifiers they did inline.
from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DV2Model,
    EntityRelationship,
    JoinKeyDetail,
    LinkDefinition,
    recommend_dimensional_variant,
)
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.copilot.schemas.osi import (
    OSIRelationship,
)
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
)
from fluid_build.forge_datamodel.from_ddl.parser import TableDefinition

LOG = logging.getLogger("fluid.copilot.modeler")


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return value or "model"


def _business_keys_for_table(table: TableDefinition) -> List[str]:
    """Choose stable DV2 business keys from real source columns."""

    if table.primary_keys:
        return list(table.primary_keys)
    id_like = [
        column.name
        for column in table.columns
        if column.name.strip().lower().endswith(("_id", "_key"))
    ]
    if id_like:
        return id_like[:1]
    if table.columns:
        return [table.columns[0].name]
    return [f"{_slug(table.name)}_id"]


def _merge_dv2_skeleton(emitted: DV2Model, skeleton: DV2Model) -> DV2Model:
    repaired = emitted.model_copy(deep=True)
    existing_hubs = {hub.hub_table_name for hub in repaired.hubs}
    existing_links = {link.link_table_name for link in repaired.links}
    existing_satellites = {sat.satellite_table_name for sat in repaired.satellites}

    for hub in skeleton.hubs:
        if hub.hub_table_name not in existing_hubs:
            repaired.hubs.append(hub.model_copy(deep=True))
            existing_hubs.add(hub.hub_table_name)
    for link in skeleton.links:
        if link.link_table_name not in existing_links:
            repaired.links.append(link.model_copy(deep=True))
            existing_links.add(link.link_table_name)
    for satellite in skeleton.satellites:
        if satellite.satellite_table_name not in existing_satellites:
            repaired.satellites.append(satellite.model_copy(deep=True))
            existing_satellites.add(satellite.satellite_table_name)
    return repaired


def _merge_dimensional_skeleton(
    emitted: DimensionalModel, skeleton: DimensionalModel
) -> DimensionalModel:
    repaired = emitted.model_copy(deep=True)
    existing_facts = {fact.name for fact in repaired.facts}
    existing_dimensions = {dimension.name for dimension in repaired.dimensions}

    for fact in skeleton.facts:
        if fact.name not in existing_facts:
            repaired.facts.append(fact.model_copy(deep=True))
            existing_facts.add(fact.name)
    for dimension in skeleton.dimensions:
        if dimension.name not in existing_dimensions:
            repaired.dimensions.append(dimension.model_copy(deep=True))
            existing_dimensions.add(dimension.name)

    repaired.conformed_dimensions = list(
        dict.fromkeys(
            repaired.conformed_dimensions + [name for name in skeleton.conformed_dimensions if name]
        )
    )
    repaired.degenerate_dims = list(
        dict.fromkeys(
            repaired.degenerate_dims + [name for name in skeleton.degenerate_dims if name]
        )
    )
    repaired.slowly_changing = {**skeleton.slowly_changing, **repaired.slowly_changing}
    if not repaired.grain_statement and skeleton.grain_statement:
        repaired.grain_statement = skeleton.grain_statement
    repaired.variant = recommend_dimensional_variant(repaired)
    return repaired


def _split_join_columns(join_condition: str) -> tuple[str, str]:
    left, _, right = join_condition.partition("=")
    return (
        left.split(".")[-1].strip().strip('"'),
        right.split(".")[-1].strip().strip('"'),
    )


def _append_unique(values: List[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _merge_relationship_into_link(link: LinkDefinition, rel: Dict[str, str]) -> None:
    left_col, right_col = _split_join_columns(rel["join_condition"])
    join_key = JoinKeyDetail(
        table1=rel["source_entity"],
        column1=left_col,
        table2=rel["target_entity"],
        column2=right_col,
        reasoning=rel["reasoning"],
    )
    join_signature = (
        join_key.table1,
        join_key.column1,
        join_key.table2,
        join_key.column2,
    )
    if not any(
        (existing.table1, existing.column1, existing.table2, existing.column2) == join_signature
        for existing in link.join_keys
    ):
        link.join_keys.append(join_key)

    relationship = EntityRelationship(
        source_entity=rel["source_entity"],
        target_entity=rel["target_entity"],
        relationship_type=rel["relationship_type"],
        join_condition=rel["join_condition"],
        reasoning=rel["reasoning"],
    )
    relationship_signature = (
        relationship.source_entity,
        relationship.target_entity,
        relationship.join_condition,
    )
    if not any(
        (
            existing.source_entity,
            existing.target_entity,
            existing.join_condition,
        )
        == relationship_signature
        for existing in link.relationships
    ):
        link.relationships.append(relationship)


def _merge_relationship_into_osi(
    relationships: List[OSIRelationship],
    rel: Dict[str, str],
) -> None:
    left_col, right_col = _split_join_columns(rel["join_condition"])
    osi_name = f"{rel['source_entity']}_to_{rel['target_entity']}"
    for existing in relationships:
        if existing.name != osi_name:
            continue
        _append_unique(existing.from_columns, left_col)
        _append_unique(existing.to_columns, right_col)
        if not existing.description:
            existing.description = rel["reasoning"]
        return
    relationships.append(
        OSIRelationship(
            name=osi_name,
            from_=rel["source_entity"],
            to=rel["target_entity"],
            from_columns=[left_col],
            to_columns=[right_col],
            description=rel["reasoning"],
        )
    )


# ── Helpers below the class ────────────────────────────────────────


def _run_tool_research_phase(
    session: StageSession,
    *,
    name: str,
    tables: Optional[Sequence[TableDefinition]] = None,
    intent: Optional[BusinessIntent] = None,
) -> List[Dict[str, Any]]:
    """Item 1 — deterministically dispatch tools from
    ``session.tool_registry`` to gather context BEFORE the LLM
    call.

    For every modeler invocation:

    * Always call ``search_semantic_memory`` (when registered)
      with the contract name + intent description as the query —
      surfaces prior forges similar to this one.
    * When tables are provided and ``inspect_table`` is
      registered, call it for the first 5 tables to enrich the
      prompt with deeper per-table metadata than what the
      modeler's input already carried.

    Returns a list of invocation summaries for inclusion in
    ``user_prompt_payload["prior_research"]``. The full
    :class:`ToolInvocation` records also accumulate on
    ``registry.invocations`` so the audit trail captures every
    tool call.

    LLM-driven tool dispatch (where the model decides which tool
    to call mid-draft) is v1.6+ work. This helper fires the
    deterministic baseline so the registry is genuinely invoked
    on every modeler run.
    """
    registry = getattr(session, "tool_registry", None)
    if registry is None:
        return []

    summaries: List[Dict[str, Any]] = []

    # 1. Semantic memory search (always when registered).
    if "search_semantic_memory" in getattr(registry, "tools", {}):
        try:
            query = name
            if intent is not None:
                query = " ".join(
                    filter(
                        None,
                        [
                            name,
                            getattr(intent, "domain", "") or "",
                            getattr(intent, "description", "") or "",
                        ],
                    )
                )
            registry.invoke(
                "search_semantic_memory",
                {
                    "query": query,
                    "limit": 3,
                },
            )
            inv = registry.invocations[-1]
            summaries.append(
                {
                    "tool": "search_semantic_memory",
                    "success": inv.success,
                    "result_count": (len(inv.result) if isinstance(inv.result, list) else 0),
                }
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # 2. Per-table inspection (when adapter + tool registered).
    if "inspect_table" in getattr(registry, "tools", {}) and tables:
        for table in list(tables)[:5]:
            try:
                registry.invoke("inspect_table", {"fqn": table.name})
                inv = registry.invocations[-1]
                summaries.append(
                    {
                        "tool": "inspect_table",
                        "table": table.name,
                        "success": inv.success,
                    }
                )
            except Exception:  # pragma: no cover — defensive
                pass

    return summaries


def _ensure_tool_registry(session: StageSession) -> None:
    """Item 1 — attach a default :class:`ToolRegistry` to the
    session so the v1.6+ LLM modeler's tool-use loop has tools
    to pick from.

    Best-effort: missing inputs (no catalog adapter on the
    session, no store) just produce a smaller registry. Already-
    set ``session.tool_registry`` is preserved so callers can
    inject custom registries before invoking the modeler.
    """
    if getattr(session, "tool_registry", None) is not None:
        return
    try:
        from fluid_build.copilot.agent_tools import (
            build_default_tool_registry,
        )

        registry = build_default_tool_registry(
            catalog_adapter=getattr(session, "catalog_adapter", None),
            store=getattr(session, "store", None),
        )
        # Attribute set lazily to avoid bloating the dataclass; a
        # future v1.6 ``StageSession`` field will make this
        # first-class.
        session.tool_registry = registry  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive
        pass


def _annotate_logical_from_tables(
    *,
    session: StageSession,
    logical: LogicalDraft,
    tables: Sequence[TableDefinition],
    source_type: Optional[str],
) -> None:
    """Item 4 — attach :class:`Confidence` + :class:`ClaimProvenance`
    to every entity / relationship the modeler emitted.

    Trust calibration:

    * Hub / Fact / Dimension whose ``mapped_source_tables`` matches
      a real input table → ``confidence=0.90`` (strong DDL-backed).
    * Hub whose business_key_columns came from a primary_key on
      the input → ``confidence=0.95``.
    * Synthesised entities (no input table match) →
      ``confidence=0.40`` so the critic / operator sees them as
      "modeler invention, verify."

    Provenance kinds:

    * ``ddl_constraint`` — DDL primary / foreign key.
    * ``catalog_description`` — catalog metadata when source_type
      indicates catalog origin.
    * ``modeler_synthesis`` — the modeler invented this without a
      direct source.
    """
    try:
        from fluid_build.copilot.confidence import (
            ClaimProvenance,
            Confidence,
        )

        annotations = session.get_scratchpad().get_annotations()
        table_pk_index = {t.name.lower(): tuple(t.primary_keys or []) for t in tables}

        # DV2 hubs.
        dv2 = getattr(logical, "dv2", None)
        if dv2 is not None:
            for hub in getattr(dv2, "hubs", None) or []:
                claim_path = f"dv2.hubs.{hub.entity_name}.business_key_columns"
                # Score by how well the hub matches an input table.
                bks = tuple(hub.business_key_columns or [])
                for src in hub.mapped_source_tables or []:
                    pks = table_pk_index.get(src.lower(), ())
                    if pks and tuple(pks) == bks:
                        annotations.annotate(
                            claim_path,
                            confidence=Confidence(
                                score=0.95,
                                rationale="exact PK match on input table",
                            ),
                            provenance=ClaimProvenance(
                                kind="ddl_constraint",
                                ref=f"input:{src}#PK",
                                snippet=", ".join(pks),
                            ),
                        )
                        break
                else:
                    if hub.mapped_source_tables:
                        annotations.annotate(
                            claim_path,
                            confidence=Confidence(
                                score=0.70,
                                rationale="mapped to input table; PK overlap unclear",
                            ),
                            provenance=ClaimProvenance(
                                kind="ddl_constraint",
                                ref=f"input:{','.join(hub.mapped_source_tables or [])}",
                            ),
                        )
                    else:
                        annotations.annotate(
                            claim_path,
                            confidence=Confidence(
                                score=0.40,
                                rationale="modeler synthesis with no input mapping",
                            ),
                            provenance=ClaimProvenance(
                                kind="modeler_synthesis",
                                ref=hub.entity_name,
                            ),
                        )

        # Dimensional facts / dimensions.
        dimensional = getattr(logical, "dimensional", None)
        if dimensional is not None:
            for fact in getattr(dimensional, "facts", None) or []:
                claim_path = f"dimensional.facts.{fact.name}.measures"
                annotations.annotate(
                    claim_path,
                    confidence=Confidence(
                        score=0.80 if fact.measures else 0.40,
                        rationale=(
                            "fact has measures from input"
                            if fact.measures
                            else "fact emitted with no measures (synthesis)"
                        ),
                    ),
                    provenance=ClaimProvenance(
                        kind=("ddl_constraint" if fact.measures else "modeler_synthesis"),
                        ref=fact.name,
                    ),
                )
            for dim in getattr(dimensional, "dimensions", None) or []:
                claim_path = f"dimensional.dimensions.{dim.name}.attributes"
                annotations.annotate(
                    claim_path,
                    confidence=Confidence(
                        score=0.80 if getattr(dim, "attributes", None) else 0.40,
                        rationale=(
                            "dim has attributes"
                            if getattr(dim, "attributes", None)
                            else "dim emitted with no attributes"
                        ),
                    ),
                    provenance=ClaimProvenance(
                        kind=(
                            "ddl_constraint"
                            if getattr(dim, "attributes", None)
                            else "modeler_synthesis"
                        ),
                        ref=dim.name,
                    ),
                )

        # OSI metadata claims (domain / owner) carry source_type
        # so we know whether the catalog or the intent was the
        # provenance.
        prov_kind = (
            "catalog_description" if source_type and source_type != "ddl" else "ddl_constraint"
        )
        annotations.annotate(
            "metadata.source_kind",
            confidence=Confidence(score=1.0, rationale="known from input type"),
            provenance=ClaimProvenance(
                kind=prov_kind,
                ref=source_type or "ddl",
            ),
        )
    except Exception:  # pragma: no cover — defensive
        pass


def _record_logical_plan_from_tables(
    *,
    session: StageSession,
    name: str,
    tables: Sequence[TableDefinition],
    technique: str,
) -> None:
    """Item 3 — synthesize a :class:`StagePlan` from the table list
    and write it to the scratchpad.

    The plan is HEURISTIC — one ``create_hub`` step per table,
    one ``create_link`` step per pair of tables that share an FK
    column. The LLM modeler may produce a different shape; the
    plan exists so the critic can review the intent BEFORE the
    full Pydantic output is committed."""
    try:
        from fluid_build.copilot.planning import (
            PlanStep,
            StagePlan,
            record_plan,
        )

        steps: List[Any] = []
        for table in tables:
            steps.append(
                PlanStep(
                    kind=("create_hub" if technique == "data_vault_2" else "create_fact"),
                    target=(
                        f"hub_{table.name}" if technique == "data_vault_2" else f"fact_{table.name}"
                    ),
                    rationale=f"derived from input table {table.name!r}",
                    inputs=[table.name],
                )
            )
        # Cross-table inferred links: any two tables that mention
        # the same column name in their column lists.
        if technique == "data_vault_2":
            seen_pairs: set = set()
            for i, ta in enumerate(tables):
                for tb in tables[i + 1 :]:
                    common = {c.name.lower() for c in (ta.columns or [])} & {
                        c.name.lower() for c in (tb.columns or [])
                    }
                    if not common or (ta.name, tb.name) in seen_pairs:
                        continue
                    seen_pairs.add((ta.name, tb.name))
                    steps.append(
                        PlanStep(
                            kind="create_link",
                            target=f"lnk_{ta.name}_{tb.name}",
                            rationale=(
                                f"inferred FK overlap on column(s) "
                                f"{sorted(common)} between {ta.name!r} and "
                                f"{tb.name!r}"
                            ),
                            inputs=[ta.name, tb.name],
                        )
                    )
        plan = StagePlan(
            stage="logical",
            summary=(
                f"Forge {len(tables)} input table(s) into a " f"{technique} model named {name!r}."
            ),
            steps=steps,
            inputs_used=[t.name for t in tables],
        )
        record_plan(plan, scratchpad=session.get_scratchpad())
    except Exception:  # pragma: no cover — defensive
        pass


def _record_logical_plan_from_intent(
    *,
    session: StageSession,
    intent: BusinessIntent,
    technique: str,
) -> None:
    """Item 3 — synthesize a plan from the intent's headline fields."""
    try:
        from fluid_build.copilot.planning import (
            PlanStep,
            StagePlan,
            record_plan,
        )

        product_name = getattr(intent, "name", "model")
        domain = getattr(intent, "domain", "") or ""
        # Step 1 — create the central entity from the intent.
        steps: List[Any] = [
            PlanStep(
                kind=("create_hub" if technique == "data_vault_2" else "create_fact"),
                target=(
                    f"hub_{product_name}" if technique == "data_vault_2" else f"fact_{product_name}"
                ),
                rationale=f"central entity for the {product_name!r} data product",
                inputs=["intent.name"],
            ),
        ]
        # Steps from declared dimensions / metrics.
        dims = getattr(intent, "dimensions", None)
        if dims is not None:
            for entity in getattr(dims, "entities", []) or []:
                ent_name = getattr(entity, "name", "")
                if not ent_name:
                    continue
                steps.append(
                    PlanStep(
                        kind=("create_hub" if technique == "data_vault_2" else "create_dimension"),
                        target=(
                            f"hub_{ent_name}" if technique == "data_vault_2" else f"dim_{ent_name}"
                        ),
                        rationale=f"declared dimension {ent_name!r} from the intent",
                        inputs=[f"intent.dimensions.entities.{ent_name}"],
                    )
                )
        metrics = getattr(intent, "metrics", None)
        if metrics is not None:
            for metric in getattr(metrics, "list", []) or []:
                metric_name = getattr(metric, "name", "")
                if not metric_name:
                    continue
                steps.append(
                    PlanStep(
                        kind="add_metric",
                        target=metric_name,
                        rationale=f"declared metric {metric_name!r} from the intent",
                        inputs=[f"intent.metrics.{metric_name}"],
                    )
                )
        plan = StagePlan(
            stage="logical",
            summary=(
                f"Forge data product {product_name!r}"
                + (f" in domain {domain!r}" if domain else "")
                + f" using technique={technique}."
            ),
            steps=steps,
            inputs_used=["intent.name", "intent.domain", "intent.dimensions", "intent.metrics"],
        )
        record_plan(plan, scratchpad=session.get_scratchpad())
    except Exception:  # pragma: no cover — defensive
        pass


def _inject_operator_corrections(
    session: StageSession,
    *,
    payload: Dict[str, Any],
    contract_name: str,
) -> None:
    """Item 4 — pull operator edits captured by ``fluid forge
    data-model learn`` and inject the top-3 corrections per record
    into the modeler prompt as ``operator_corrections``.

    The corrections take the form::

        [
          {"summary": "Previously, the operator changed
                       metadata.domain from 'commerce' to 'retail'.",
           "kind": "modified"},
          ...
        ]

    Best-effort: any error in the store path returns without
    populating the field. The LLM modeler treats this as a "soft
    bias" — the absence of guidance never breaks the forge.
    """
    if not contract_name:
        return
    try:
        from fluid_build.copilot.learning import fetch_recent_edits

        records = fetch_recent_edits(
            store=session.store,
            contract_name=contract_name,
            limit=5,
        )
        corrections: List[Dict[str, Any]] = []
        for record in records or []:
            for edit in (record.get("edits") or [])[:3]:
                corrections.append(
                    {
                        "summary": (
                            f"Previously, the operator {edit.get('kind')} "
                            f"{edit.get('path')!r}"
                            + (
                                f" from {edit.get('before')!r} to {edit.get('after')!r}"
                                if edit.get("kind") == "modified"
                                else ""
                            )
                        ),
                        "kind": edit.get("kind", ""),
                        "path": edit.get("path", ""),
                    }
                )
        if corrections:
            payload["operator_corrections"] = corrections
    except Exception:  # pragma: no cover — defensive
        pass


def _inject_scratchpad_signals(
    session: StageSession,
    *,
    payload: Dict[str, Any],
    target_stages: tuple[str, ...],
) -> None:
    """Append scratchpad signals (critic findings + validator
    feedback) to a modeler-prompt payload IN PLACE.

    Reads ``CriticFinding`` rows whose ``stage`` is in
    ``target_stages`` and ``StageFeedback`` rows whose
    ``target_stage`` is in ``target_stages``, then writes them
    into ``payload["critic_findings"]`` and
    ``payload["validator_feedback"]`` so the LLM sees them in the
    next call.

    The injection is **safe to call from a fresh session** —
    ``get_scratchpad`` lazy-creates the scratchpad if it doesn't
    yet exist and returns an empty pad on the first run, so
    nothing lands in the payload. On a repair retry, the previous
    validator's findings are already on the scratchpad and DO
    land — exactly the path that was missing before this wiring.
    """
    try:
        scratchpad = session.get_scratchpad()
    except Exception:  # pragma: no cover — defensive
        return

    critic_payload: List[Dict[str, Any]] = []
    for stage in target_stages:
        for finding in scratchpad.critic_findings_for_stage(stage):
            critic_payload.append(
                {
                    "stage": finding.stage,
                    "severity": finding.severity,
                    "message": finding.message,
                    "suggestion": finding.suggestion,
                    "target": finding.target,
                }
            )
    if critic_payload:
        payload["critic_findings"] = critic_payload

    feedback_payload: List[Dict[str, Any]] = []
    for stage in target_stages:
        for feedback in scratchpad.feedback_for_stage(stage):
            feedback_payload.append(
                {
                    "source_stage": feedback.source_stage,
                    "summary": feedback.summary,
                    "structured": feedback.structured,
                }
            )
    if feedback_payload:
        payload["validator_feedback"] = feedback_payload
