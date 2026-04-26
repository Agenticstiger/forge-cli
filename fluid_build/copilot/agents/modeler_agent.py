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

"""Modeler agent: Conceptual + Logical stages.

.. note::

    **Internal-composition agent.** :class:`ModelerAgent` is composed by
    :class:`fluid_build.copilot.agents.logical_agent.LogicalAgent` and
    :class:`fluid_build.copilot.agents.conceptual_agent.ConceptualAgent`
    — those are the recommended entry points for v1.5+ code.

    ``ModelerAgent`` remains in the public API for v1.3 orchestrators
    that drive the modeler directly. New code should call
    :class:`fluid_build.copilot.agents.coordinator.StageCoordinator`
    methods (``from_intent`` / ``from_tables`` / ``from_catalog``)
    instead.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from pydantic import ValidationError

LOG = logging.getLogger("fluid.copilot.modeler")

from fluid_build.copilot.agents.base import BaseStageAgent, StageSession
from fluid_build.copilot.agents.errors import AgentExecutionError
from fluid_build.copilot.prompts.loader import load_prompt_text
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    EntityRelationship,
    FactTable,
    FieldDefinition,
    HubDefinition,
    JoinKeyDetail,
    LinkDefinition,
    SatelliteDefinition,
    recommend_dimensional_variant,
)
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSIDataset,
    OSIDimension,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSIRelationship,
    OSISemanticModel,
    osi_dialect_from_source_type,
)
from fluid_build.copilot.schemas.stage_outputs import (
    ConceptualDraft,
    ConceptualEntity,
    ConceptualRelationship,
    LogicalDraft,
)
from fluid_build.forge_datamodel.from_ddl.parser import TableDefinition

_MAX_SCHEMA_REPAIR_ATTEMPTS = 1


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


class ModelerAgent(BaseStageAgent):
    """Modeler stage. Uses heuristics by default and LLMs when configured."""

    def __init__(self) -> None:
        super().__init__(stage="modeler", tier="deep")

    def from_tables(
        self,
        session: StageSession,
        *,
        name: str,
        tables: Sequence[TableDefinition],
        technique: str,
        source_type: Optional[str] = None,
    ) -> LogicalDraft:
        # Item 1 — register tools the LLM modeler can invoke
        # mid-call. Today the heuristic path doesn't use them,
        # but the registry lands on the session so:
        #
        # * v1.6+ LLM modeler will pass ``session.tool_registry``
        #   to the provider's ``build_tool_request`` and run a
        #   multi-turn tool-use loop.
        # * Audit / telemetry observers see WHICH tools the
        #   modeler had available even on heuristic runs.
        _ensure_tool_registry(session)
        # Item 3 — emit a plan-then-execute sketch BEFORE the
        # actual modeling work. Lets the critic / operator review
        # the intent ("I will create N hubs from these tables, link
        # via these FKs") without paying for the full output. The
        # plan lands on ``scratchpad.raw["plan:logical"]`` so
        # downstream agents (CriticAgent, audit observers) can
        # read it.
        _record_logical_plan_from_tables(
            session=session,
            name=name,
            tables=tables,
            technique=technique,
        )
        if session.llm_config is not None:
            try:
                return self._llm_from_tables(
                    session, name=name, tables=tables, technique=technique, source_type=source_type
                )
            except Exception as exc:  # noqa: BLE001 — fallback is deliberate
                # Heuristic fallback is the right safety net for keyless /
                # transient-failure paths, but a silent swallow hides
                # real LLM bugs (malformed structured output, 4xx auth,
                # prompt regressions). Log the cause at WARNING so the
                # operator can tell "ran heuristics because no LLM"
                # apart from "ran heuristics because Gemini returned
                # garbage."
                if session.require_llm:
                    if isinstance(exc, AgentExecutionError):
                        raise
                    LOG.warning(
                        "ModelerAgent.from_tables: LLM path failed (%s: %s) — strict mode refuses heuristic fallback",
                        type(exc).__name__,
                        exc,
                    )
                    raise AgentExecutionError(
                        "LLM execution is required for this run; refusing heuristic fallback."
                    ) from exc
                LOG.warning(
                    "ModelerAgent.from_tables: LLM path failed (%s: %s) — falling back to heuristics",
                    type(exc).__name__,
                    exc,
                )
                session.record_fallback(
                    stage="modeler",
                    reason="llm_failed",
                    error_type=type(exc).__name__,
                )
        conceptual = self._conceptual_from_tables(name=name, tables=tables)
        result = self._logical_from_tables(
            name=name,
            conceptual=conceptual,
            tables=tables,
            technique=technique,
            source_type=source_type,
        )
        result = self._ensure_minimum_coverage(result=result, session=session, technique=technique)
        result = self._merge_inferred_table_relationships(
            result, tables=tables, technique=technique
        )
        result = self._ensure_semantic_coverage(result, source_type=source_type)
        # Item 4 — annotate modeler outputs with confidence +
        # provenance so downstream agents (Critic, Validator) and
        # the receipt writer see WHERE each claim came from and
        # how strong it is.
        _annotate_logical_from_tables(
            session=session,
            logical=result,
            tables=tables,
            source_type=source_type,
        )
        return result

    def from_intent(
        self,
        session: StageSession,
        *,
        intent: BusinessIntent,
        technique: str,
    ) -> LogicalDraft:
        # Item 1 — tool registry (see from_tables for rationale).
        _ensure_tool_registry(session)
        # Item 3 — plan-then-execute sketch.
        _record_logical_plan_from_intent(
            session=session,
            intent=intent,
            technique=technique,
        )
        if session.llm_config is not None:
            try:
                return self._llm_from_intent(session, intent=intent, technique=technique)
            except Exception as exc:  # noqa: BLE001 — fallback is deliberate
                if session.require_llm:
                    if isinstance(exc, AgentExecutionError):
                        raise
                    LOG.warning(
                        "ModelerAgent.from_intent: LLM path failed (%s: %s) — strict mode refuses heuristic fallback",
                        type(exc).__name__,
                        exc,
                    )
                    raise AgentExecutionError(
                        "LLM execution is required for this run; refusing heuristic fallback."
                    ) from exc
                LOG.warning(
                    "ModelerAgent.from_intent: LLM path failed (%s: %s) — falling back to heuristics",
                    type(exc).__name__,
                    exc,
                )
                session.record_fallback(
                    stage="modeler",
                    reason="llm_failed",
                    error_type=type(exc).__name__,
                )
        conceptual = self._conceptual_from_intent(intent)
        result = self._logical_from_intent(
            intent=intent, conceptual=conceptual, technique=technique
        )
        result = self._ensure_minimum_coverage(result=result, session=session, technique=technique)
        return self._ensure_semantic_coverage(result)

    def _llm_from_tables(
        self,
        session: StageSession,
        *,
        name: str,
        tables: Sequence[TableDefinition],
        technique: str,
        source_type: Optional[str],
    ) -> LogicalDraft:
        fragments = [
            load_prompt_text("fragments/conceptual.yaml"),
            load_prompt_text(
                "fragments/dv2.yaml"
                if technique == "data_vault_2"
                else "fragments/dimensional.yaml"
            ),
        ]
        semantic_query = self._build_semantic_query_from_tables(
            name=name, tables=tables, technique=technique
        )
        prior_context = self._retrieve_prior_similar_models(session, query=semantic_query)
        user_prompt_payload: Dict[str, Any] = {
            "name": name,
            "technique": technique,
            "source_type": source_type,
            "schema_constraints": self._logical_schema_constraints(technique),
            "tables": [
                {
                    "name": table.name,
                    "primary_keys": table.primary_keys,
                    "columns": [
                        {
                            "name": column.name,
                            "logical_type": column.logical_type,
                            "nullable": column.nullable,
                            "primary_key": column.primary_key,
                        }
                        for column in table.columns
                    ],
                }
                for table in tables
            ],
        }
        if prior_context:
            user_prompt_payload["prior_similar_models"] = prior_context
        # Item 1 — deterministic tool research phase. Dispatches
        # registered tools to gather context the LLM would
        # otherwise have to ask for. Results land in
        # ``prior_research`` for the next prompt to reference.
        research = _run_tool_research_phase(
            session,
            name=name,
            tables=tables,
        )
        if research:
            user_prompt_payload["prior_research"] = research
        # Item 4 — operator edits from prior runs of similar
        # contracts. Read what the operator hand-corrected last
        # time (via ``fluid forge data-model learn``) and inject
        # the diffs so the modeler biases toward operator
        # preferences.
        _inject_operator_corrections(
            session,
            payload=user_prompt_payload,
            contract_name=name,
        )
        # Sprint #2 + #3 — inject scratchpad signals into the
        # prompt so retries (after validator findings) and
        # subsequent runs (after critic findings) actually steer
        # the LLM toward fixing the problems. Without this, retries
        # see the same prompt and the LLM has no reason to produce
        # a different answer.
        _inject_scratchpad_signals(
            session,
            payload=user_prompt_payload,
            target_stages=("logical", "modeler"),
        )
        user_prompt = json.dumps(user_prompt_payload, indent=2)
        result = self._call_logical_with_schema_repair(
            session,
            system_prompt="\n".join(fragments),
            user_prompt=user_prompt,
            params={"technique": technique, "source_type": source_type, "source_kind": "ddl"},
            technique=technique,
        )
        result = self._ensure_minimum_coverage(result=result, session=session, technique=technique)
        result = self._merge_inferred_table_relationships(
            result, tables=tables, technique=technique
        )
        return self._ensure_semantic_coverage(result, source_type=source_type)

    def _llm_from_intent(
        self, session: StageSession, *, intent: BusinessIntent, technique: str
    ) -> LogicalDraft:
        fragments = [
            load_prompt_text("fragments/conceptual.yaml"),
            load_prompt_text(
                "fragments/dv2.yaml"
                if technique == "data_vault_2"
                else "fragments/dimensional.yaml"
            ),
        ]
        semantic_query = self._build_semantic_query_from_intent(intent=intent, technique=technique)
        prior_context = self._retrieve_prior_similar_models(session, query=semantic_query)
        intent_payload = json.loads(intent.model_dump_json())
        intent_payload["schema_constraints"] = self._logical_schema_constraints(technique)
        if prior_context:
            intent_payload["prior_similar_models"] = prior_context
        # Item 1 — tool research phase (same wiring as tables path).
        research = _run_tool_research_phase(
            session,
            name=getattr(intent, "name", "model"),
            intent=intent,
        )
        if research:
            intent_payload["prior_research"] = research
        # Item 4 — operator-edit retrieval (same wiring as the
        # tables path).
        _inject_operator_corrections(
            session,
            payload=intent_payload,
            contract_name=getattr(intent, "name", "model"),
        )
        # Sprint #2 + #3 — same scratchpad-signal injection as the
        # tables path. Critic findings + validator stage feedback
        # land in the prompt so retries can act on them.
        _inject_scratchpad_signals(
            session,
            payload=intent_payload,
            target_stages=("logical", "modeler"),
        )
        result = self._call_logical_with_schema_repair(
            session,
            system_prompt="\n".join(fragments),
            user_prompt=json.dumps(intent_payload, indent=2),
            params={"technique": technique, "source_kind": "intent"},
            technique=technique,
        )
        result = self._deterministic_intent_backbone(
            provider_result=result,
            intent=intent,
            technique=technique,
        )
        result = self._ensure_minimum_coverage(result=result, session=session, technique=technique)
        return self._ensure_semantic_coverage(result)

    def _deterministic_intent_backbone(
        self,
        *,
        provider_result: LogicalDraft,
        intent: BusinessIntent,
        technique: str,
    ) -> LogicalDraft:
        """Anchor intent-sourced physical models in deterministic code.

        Strict provider runs still require the LLM to return a valid
        LogicalDraft, so provider setup and structured-output behavior are
        genuinely tested. The repeatable physical backbone, however, comes
        from the typed intent parser plus industry skeleton repair. That
        keeps harmless provider wording variance from changing contracts
        and dbt SQL between runs.
        """

        conceptual = provider_result.conceptual or self._conceptual_from_intent(intent)
        deterministic = self._logical_from_intent(
            intent=intent,
            conceptual=conceptual,
            technique=technique,
        )
        deterministic.description = provider_result.description or deterministic.description
        deterministic.review_notes = list(provider_result.review_notes or [])
        deterministic.source_summary.update(provider_result.source_summary or {})
        deterministic.source_summary["source_kind"] = "intent"
        deterministic.source_summary["logical_backbone"] = "deterministic_intent"
        return deterministic

    def _call_logical_with_schema_repair(
        self,
        session: StageSession,
        *,
        system_prompt: str,
        user_prompt: str,
        params: Dict[str, Any],
        technique: str,
    ) -> LogicalDraft:
        try:
            return self.call(
                session,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                output_schema=LogicalDraft,
                params=params,
                retry_schema_errors=False,
            )
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error: Exception = exc

        for attempt in range(1, _MAX_SCHEMA_REPAIR_ATTEMPTS + 1):
            session.record_repair(
                stage="modeler",
                reason="schema_validation_failed",
                error_type=type(last_error).__name__,
                detail=self._schema_error_detail(last_error),
            )
            LOG.warning(
                "ModelerAgent: LogicalDraft schema validation failed (%s: %s) — requesting repair attempt %d/%d",
                type(last_error).__name__,
                last_error,
                attempt,
                _MAX_SCHEMA_REPAIR_ATTEMPTS,
            )
            try:
                return self.call(
                    session,
                    system_prompt=system_prompt,
                    user_prompt=self._build_schema_repair_prompt(
                        original_user_prompt=user_prompt,
                        error=last_error,
                        technique=technique,
                    ),
                    output_schema=LogicalDraft,
                    params={**params, "schema_repair_attempt": attempt},
                    retry_schema_errors=False,
                )
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc

        raise AgentExecutionError(
            "LLM schema repair failed; refusing invalid LogicalDraft output."
        ) from last_error

    @staticmethod
    def _schema_error_detail(error: Exception) -> str:
        error_text = str(error).strip()
        if not error_text:
            return type(error).__name__
        if len(error_text) > 500:
            return error_text[:500] + "...[truncated]"
        return error_text

    def _build_schema_repair_prompt(
        self,
        *,
        original_user_prompt: str,
        error: Exception,
        technique: str,
    ) -> str:
        error_text = str(error)
        if len(error_text) > 4000:
            error_text = error_text[:4000] + "\n...[truncated]"
        return "\n\n".join(
            [
                "The previous response failed strict LogicalDraft validation.",
                "Return one complete corrected LogicalDraft JSON object only. Do not include markdown fences or commentary.",
                f"Modeling technique must remain: {technique}.",
                "Repair rules:",
                "- Preserve the user's original business intent.",
                "- For data_vault_2, every dv2.links[].join_keys[] item must be an object with table1, column1, table2, column2, and optional reasoning. Never emit a bare string join key.",
                "- For dimensional, populate dimensional only and leave dv2 null or omitted.",
                "- For time dimensions, use grain day/week/month/quarter/year/hour/minute; if the source is second/sub-second, use minute.",
                "- Ensure OSI entities, dimensions, measures, and metrics are sufficient for contract validation.",
                "Validation errors:",
                error_text,
                "Original request payload:",
                original_user_prompt,
            ]
        )

    @staticmethod
    def _logical_schema_constraints(technique: str) -> Dict[str, Any]:
        if technique == "data_vault_2":
            return {
                "technique": "data_vault_2",
                "dv2_required": True,
                "dimensional": "null",
                "dv2.links[].join_keys[]": {
                    "type": "object",
                    "required": ["table1", "column1", "table2", "column2"],
                    "optional": ["reasoning"],
                    "forbidden_shapes": [
                        "string",
                        "array_of_strings",
                        "array_tuple",
                    ],
                    "example": {
                        "table1": "orders",
                        "column1": "customer_id",
                        "table2": "customers",
                        "column2": "customer_id",
                        "reasoning": "Orders reference customers by customer_id.",
                    },
                },
                "dv2.links[].relationships[]": {
                    "type": "object",
                    "required": ["source_entity", "target_entity", "relationship_type"],
                    "default_relationship_type": "association",
                    "allowed_relationship_type_values": [
                        "association",
                        "one_to_one",
                        "one_to_many",
                        "many_to_one",
                        "many_to_many",
                    ],
                },
            }
        if technique == "dimensional":
            return {
                "technique": "dimensional",
                "dimensional_required": True,
                "dv2": "null",
            }
        return {"technique": technique}

    # ------------------------------------------------------------------
    # Semantic-memory retrieval — seeds the LLM prompt on cache miss with
    # top-3 structurally similar prior forged models. Fully read-only and
    # fully optional: if the store has no ``memory/semantic`` records or
    # the backend doesn't support vector search, we pass an empty
    # context and the prompt degenerates to its v1.0 shape — no
    # regression, just no uplift.
    # ------------------------------------------------------------------
    _SEMANTIC_RETRIEVAL_LIMIT = 3
    _SEMANTIC_NAMESPACE = "memory/semantic"

    def _retrieve_prior_similar_models(
        self, session: StageSession, *, query: str
    ) -> List[Dict[str, Any]]:
        """Fetch up to 3 prior semantic-memory records whose content
        overlaps the new intent / DDL.

        Implementation delegates to the canonical
        :func:`fluid_build.copilot.retrieval.retrieve_similar_models`
        so there's exactly one retrieval code path. Returns the
        payload shape the modeler's prompt expects (list of
        ``{"key": ..., "value": ...}`` dicts) — the canonical
        function ALSO writes ``RetrievalResult`` rows to the
        session scratchpad so other agents and observers can read
        retrievals from one place.
        """
        if not query:
            return []
        store = getattr(session, "store", None)
        if store is None:
            return []
        try:
            from fluid_build.copilot.retrieval import (
                RetrievalConfig,
                retrieve_similar_models,
            )
            from fluid_build.copilot.store.backends.vector import VectorBackend

            ranker = store if isinstance(store, VectorBackend) else VectorBackend(store)
            results = retrieve_similar_models(
                query,
                store=ranker,
                scratchpad=session.get_scratchpad(),
                config=RetrievalConfig(
                    limit=self._SEMANTIC_RETRIEVAL_LIMIT,
                    namespace=self._SEMANTIC_NAMESPACE,
                    mode="vector",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            LOG.debug(
                "ModelerAgent: semantic-memory retrieval failed (%s: %s) — skipping",
                type(exc).__name__,
                exc,
            )
            return []
        payloads: List[Dict[str, Any]] = [
            {"key": result.key, "value": result.payload} for result in results
        ]
        if payloads:
            LOG.info(
                "ModelerAgent: seeded prompt with %d prior similar model(s) from %s",
                len(payloads),
                self._SEMANTIC_NAMESPACE,
            )
        return payloads

    @staticmethod
    def _build_semantic_query_from_intent(*, intent: BusinessIntent, technique: str) -> str:
        """Derive a token-rich query string from a BusinessIntent. Tokens
        drive both the stdlib difflib path and the hash-based embedder
        in VectorBackend, so the more descriptive tokens we pack in
        (technique, industry, metric names, entity names) the better
        the ranking."""
        parts: List[str] = [technique]
        data_product = getattr(intent, "data_product", None)
        if data_product is not None:
            for attr in ("name", "domain", "description"):
                value = getattr(data_product, attr, None)
                if isinstance(value, str) and value:
                    parts.append(value)
        business_context = getattr(intent, "business_context", None)
        if business_context is not None:
            value = getattr(business_context, "description", None)
            if isinstance(value, str) and value:
                parts.append(value)
        metrics = getattr(intent, "metrics", None) or []
        for metric in metrics:
            name = getattr(metric, "name", None)
            if isinstance(name, str) and name:
                parts.append(name)
        return " ".join(parts)

    @staticmethod
    def _build_semantic_query_from_tables(
        *, name: str, tables: Sequence[TableDefinition], technique: str
    ) -> str:
        """Derive a query from DDL-path inputs — table names + column
        names carry the domain semantics sqlglot extracts."""
        parts: List[str] = [technique, name]
        for table in tables:
            parts.append(table.name)
            for column in table.columns:
                parts.append(column.name)
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Defense-in-depth: repair semantic gaps from industry-pack skeletons.
    # ------------------------------------------------------------------
    def _ensure_minimum_coverage(
        self,
        *,
        result: LogicalDraft,
        session: StageSession,
        technique: str,
    ) -> LogicalDraft:
        """Backfill and repair LLM output from the industry-pack skeleton.

        The first generation remains the modeler's primary answer, but the
        industry skeleton is now a bounded semantic repair pass rather than
        display-only warning text. Vacuous output is replaced wholesale; a
        non-vacuous draft is merged by canonical table name so missing
        essentials are appended without overwriting the LLM's own entities.
        """
        pack = session.industry_pack
        if pack is None:
            return result

        if technique == "dimensional":
            dim = result.dimensional
            skeleton = pack.seed_dimensional_skeleton
            if skeleton is None:
                return result
            if dim is not None and dim.facts and len(dim.dimensions) >= 2:
                result.dimensional = _merge_dimensional_skeleton(dim, skeleton)
                return result
            LOG.warning(
                "ModelerAgent: LLM returned vacuous dimensional model "
                "(facts=%d, dimensions=%d) — seeding from %s industry pack",
                0 if dim is None else len(dim.facts),
                0 if dim is None else len(dim.dimensions),
                pack.name,
            )
            result.dimensional = skeleton.model_copy(deep=True)
            return result

        if technique == "data_vault_2":
            dv2 = result.dv2
            skeleton = pack.seed_dv2_skeleton
            if skeleton is None:
                return result
            if dv2 is not None and dv2.hubs:
                result.dv2 = _merge_dv2_skeleton(dv2, skeleton)
                return result
            LOG.warning(
                "ModelerAgent: LLM returned vacuous DV2 model (hubs=%d) — "
                "seeding from %s industry pack",
                0 if dv2 is None else len(dv2.hubs),
                pack.name,
            )
            result.dv2 = skeleton.model_copy(deep=True)
            return result

        return result

    def _ensure_semantic_coverage(
        self,
        result: LogicalDraft,
        *,
        source_type: Optional[str] = None,
    ) -> LogicalDraft:
        """Backfill OSI datasets and metrics from the chosen physical IR.

        LLMs occasionally produce a good dimensional/DV2 model but leave
        ``osi.datasets`` or ``osi.metrics`` empty. That is schema-valid but
        not release-grade: contracts then validate with thin semantics and BI
        generators have little to work with. This deterministic repair keeps
        the LLM's modeling choices while making the semantic sidecar useful.
        """
        if not result.osi.description:
            result.osi.description = result.description or f"Semantic model for {result.name}"
        if not result.osi.ai_context.instructions:
            result.osi.ai_context.instructions = (
                f"Use {result.name} for governed analytics and transformation generation."
            )

        if not result.osi.datasets:
            if result.technique == "dimensional" and result.dimensional is not None:
                result.osi.datasets = self._osi_datasets_from_dimensional(result.dimensional)
            elif result.technique == "data_vault_2" and result.dv2 is not None:
                result.osi.datasets = self._osi_datasets_from_dv2(
                    result.dv2, source_type=source_type
                )

        if not result.osi.metrics:
            if result.technique == "dimensional" and result.dimensional is not None:
                result.osi.metrics = self._osi_metrics_from_dimensional(result.dimensional)
            elif result.technique == "data_vault_2" and result.dv2 is not None:
                result.osi.metrics = self._osi_metrics_from_dv2(result.dv2)

        return result

    def _osi_datasets_from_dimensional(self, model: DimensionalModel) -> List[OSIDataset]:
        if not model.facts:
            return []
        datasets: List[OSIDataset] = []
        fact = model.facts[0]
        fact_key = f"{_slug(fact.name).removeprefix('fact_').removeprefix('fct_')}_id"
        fields_by_name: Dict[str, OSIField] = {
            fact_key: self._osi_field(fact_key, "STRING"),
        }
        for column in fact.foreign_keys + fact.degenerate_dimensions:
            fields_by_name.setdefault(column, self._osi_field(column, "STRING"))
        for measure in fact.measures:
            fields_by_name.setdefault(
                measure.name,
                self._osi_field(
                    measure.name,
                    measure.data_type,
                    description=measure.description,
                ),
            )
        if not fact.measures:
            fields_by_name.setdefault("record_count", self._osi_field("record_count", "INTEGER"))

        datasets.append(
            OSIDataset(
                name=fact.name,
                source=fact.name,
                primary_key=[fact_key],
                fields=list(fields_by_name.values()),
            )
        )
        for dimension in model.dimensions:
            key_columns = list(dimension.natural_keys)
            if dimension.surrogate_key:
                key_columns.insert(0, dimension.surrogate_key)
            if not key_columns:
                key_columns = [f"{_slug(dimension.name).removeprefix('dim_')}_id"]
            fields = [self._osi_field(key, "STRING") for key in dict.fromkeys(key_columns)]
            fields.extend(
                self._osi_field(
                    attr.name,
                    attr.data_type,
                    description=attr.description,
                )
                for attr in dimension.attributes
            )
            datasets.append(
                OSIDataset(
                    name=dimension.name,
                    source=dimension.name,
                    primary_key=key_columns[:1],
                    fields=fields,
                )
            )
        return datasets

    def _osi_metrics_from_dimensional(self, model: DimensionalModel) -> List[OSIMetric]:
        if not model.facts:
            return []
        metrics: List[OSIMetric] = []
        fact = model.facts[0]
        for measure in fact.measures:
            metrics.append(
                OSIMetric(
                    name=_slug(measure.name),
                    description=measure.description or f"Sum of {measure.name}.",
                    expression=OSIExpression(
                        dialects=[
                            OSIExpressionDialect(
                                dialect="ANSI_SQL",
                                expression=f"SUM({measure.name})",
                            )
                        ]
                    ),
                )
            )
        if not metrics:
            metrics.append(self._record_count_metric(fact.name))
        return metrics

    def _osi_datasets_from_dv2(
        self,
        model: DV2Model,
        *,
        source_type: Optional[str] = None,
    ) -> List[OSIDataset]:
        datasets: List[OSIDataset] = []
        dialect = osi_dialect_from_source_type(source_type)
        satellites_by_parent: Dict[str, List[SatelliteDefinition]] = {}
        for satellite in model.satellites:
            satellites_by_parent.setdefault(satellite.parent_hub, []).append(satellite)
        for hub in model.hubs:
            keys = list(hub.business_key_columns) or [f"{_slug(hub.entity_name)}_id"]
            fields = [self._osi_field(key, "STRING", dialect=dialect) for key in keys]
            for satellite in satellites_by_parent.get(hub.hub_table_name, []):
                fields.extend(
                    self._osi_field(attr, "STRING", dialect=dialect)
                    for attr in satellite.attributes
                )
            datasets.append(
                OSIDataset(
                    name=hub.hub_table_name,
                    source=(
                        hub.mapped_source_tables[0]
                        if hub.mapped_source_tables
                        else hub.hub_table_name
                    ),
                    primary_key=keys[:1],
                    fields=fields,
                )
            )
        return datasets

    def _osi_metrics_from_dv2(self, model: DV2Model) -> List[OSIMetric]:
        dataset_name = model.hubs[0].hub_table_name if model.hubs else "vault"
        return [self._record_count_metric(dataset_name)]

    def _record_count_metric(self, dataset_name: str) -> OSIMetric:
        metric_name = f"{_slug(dataset_name)}_record_count"
        return OSIMetric(
            name=metric_name,
            description=f"Count of records in {dataset_name}.",
            expression=OSIExpression(
                dialects=[OSIExpressionDialect(dialect="ANSI_SQL", expression="COUNT(*)")]
            ),
        )

    def _osi_field(
        self,
        name: str,
        data_type: str,
        *,
        description: Optional[str] = None,
        dialect: str = "ANSI_SQL",
    ) -> OSIField:
        lower = name.lower()
        is_time = any(token in lower for token in ("date", "time", "timestamp", "day"))
        return OSIField(
            name=name,
            description=description,
            data_type=data_type or "STRING",
            expression=OSIExpression(
                dialects=[OSIExpressionDialect(dialect=dialect, expression=name)]
            ),
            dimension=OSIDimension(is_time=True, grain="day") if is_time else None,
        )

    def _merge_inferred_table_relationships(
        self,
        result: LogicalDraft,
        *,
        tables: Sequence[TableDefinition],
        technique: str,
    ) -> LogicalDraft:
        """Repair DDL-path DV2 drafts with deterministic FK-style links."""
        if technique != "data_vault_2" or result.dv2 is None:
            return result
        relationships = self._relationships_from_tables(tables)
        if not relationships:
            return result

        existing_hubs = {hub.hub_table_name for hub in result.dv2.hubs}
        table_by_name = {table.name: table for table in tables}
        for rel in relationships:
            for entity in (rel["source_entity"], rel["target_entity"]):
                hub_name = f"hub_{_slug(entity)}"
                if hub_name in existing_hubs:
                    continue
                table = table_by_name.get(entity)
                keys = []
                if table is not None:
                    keys = (
                        list(table.primary_keys)
                        or [
                            column.name
                            for column in table.columns
                            if column.name.lower().endswith("_id")
                        ][:1]
                    )
                result.dv2.hubs.append(
                    HubDefinition(
                        entity_name=entity,
                        hub_table_name=hub_name,
                        business_key_columns=keys or [f"{_slug(entity)}_id"],
                        mapped_source_tables=[entity],
                        description=f"Hub inferred from source table {entity}.",
                    )
                )
                existing_hubs.add(hub_name)

        links_by_name = {link.link_table_name: link for link in result.dv2.links}
        for rel in relationships:
            link_table_name = f"lnk_{_slug(rel['source_entity'])}_{_slug(rel['target_entity'])}"
            link = links_by_name.get(link_table_name)
            if link is None:
                link = LinkDefinition(
                    link_name=f"{_slug(rel['source_entity'])}_{_slug(rel['target_entity'])}",
                    link_table_name=link_table_name,
                    hubs_involved=[
                        f"hub_{_slug(rel['source_entity'])}",
                        f"hub_{_slug(rel['target_entity'])}",
                    ],
                )
                result.dv2.links.append(link)
                links_by_name[link_table_name] = link
            _merge_relationship_into_link(link, rel)

            _merge_relationship_into_osi(result.osi.relationships, rel)
        return result

    def _conceptual_from_tables(
        self, *, name: str, tables: Sequence[TableDefinition]
    ) -> ConceptualDraft:
        relationships = self._relationships_from_tables(tables)
        return ConceptualDraft(
            name=name,
            ai_context=OSIAIContext(
                instructions=f"Use {name} for operational analytics and governed transformation generation.",
                synonyms=[name.replace("_", " "), name.title()],
            ),
            entities=[
                ConceptualEntity(
                    name=table.name,
                    description=f"Entity inferred from source table `{table.name}`.",
                    source_names=[table.name],
                )
                for table in tables
            ],
            relationships=[
                ConceptualRelationship(
                    source_entity=rel["source_entity"],
                    target_entity=rel["target_entity"],
                    description=rel["reasoning"],
                    cardinality=rel["relationship_type"],
                )
                for rel in relationships
            ],
        )

    def _logical_from_tables(
        self,
        *,
        name: str,
        conceptual: ConceptualDraft,
        tables: Sequence[TableDefinition],
        technique: str,
        source_type: Optional[str],
    ) -> LogicalDraft:
        relationships = self._relationships_from_tables(tables)
        osi = self._osi_from_tables(
            name=name, tables=tables, relationships=relationships, source_type=source_type
        )
        if technique == "data_vault_2":
            dv2 = self._dv2_from_tables(tables, relationships)
            return LogicalDraft(
                name=name,
                description=f"Logical DV2 draft for {name}",
                technique="data_vault_2",
                conceptual=conceptual,
                dv2=dv2,
                osi=osi,
                source_summary={"source_kind": "ddl", "table_count": len(tables)},
            )
        dimensional = self._dimensional_from_tables(name=name, tables=tables)
        return LogicalDraft(
            name=name,
            description=f"Logical dimensional draft for {name}",
            technique="dimensional",
            conceptual=conceptual,
            dimensional=dimensional,
            osi=osi,
            source_summary={"source_kind": "ddl", "table_count": len(tables)},
        )

    def _conceptual_from_intent(self, intent: BusinessIntent) -> ConceptualDraft:
        entities = []
        seen = set()
        if intent.grain and intent.grain.entity:
            seen.add(intent.grain.entity)
            entities.append(
                ConceptualEntity(
                    name=intent.grain.entity,
                    description=intent.grain.description
                    or "Primary grain entity from business intent.",
                    source_names=[intent.grain.entity],
                )
            )
        for entity_name in intent.dimensions.entities:
            if entity_name in seen:
                continue
            seen.add(entity_name)
            entities.append(
                ConceptualEntity(
                    name=entity_name,
                    description=f"Dimension entity inferred from the business intent for {entity_name}.",
                    source_names=[entity_name],
                )
            )
        return ConceptualDraft(
            name=intent.data_product.name,
            description=intent.data_product.description,
            ai_context=OSIAIContext(
                instructions=intent.business_context.problem_statement,
                synonyms=[intent.data_product.name, intent.data_product.domain],
                examples=intent.consumption.use_cases,
            ),
            entities=entities,
        )

    def _logical_from_intent(
        self,
        *,
        intent: BusinessIntent,
        conceptual: ConceptualDraft,
        technique: str,
    ) -> LogicalDraft:
        osi = self._osi_from_intent(intent)
        if technique == "data_vault_2":
            dv2 = self._dv2_from_intent(intent)
            return LogicalDraft(
                name=intent.data_product.name,
                description=intent.data_product.description,
                technique="data_vault_2",
                conceptual=conceptual,
                dv2=dv2,
                osi=osi,
                source_summary={"source_kind": "intent", "metric_count": len(intent.metrics)},
            )
        dimensional = self._dimensional_from_intent(intent)
        return LogicalDraft(
            name=intent.data_product.name,
            description=intent.data_product.description,
            technique="dimensional",
            conceptual=conceptual,
            dimensional=dimensional,
            osi=osi,
            source_summary={"source_kind": "intent", "metric_count": len(intent.metrics)},
        )

    def _relationships_from_tables(self, tables: Sequence[TableDefinition]) -> List[Dict[str, str]]:
        table_names = {table.name: table for table in tables}
        relationships: List[Dict[str, str]] = []
        for table in tables:
            own_identity_key = f"{_slug(table.name)}_id"
            for column in table.columns:
                if not column.name.lower().endswith("_id"):
                    continue
                if column.name.lower() == own_identity_key and column.name in table.primary_keys:
                    continue
                target, target_column = self._target_table_for_key(
                    column.name,
                    table_names.values(),
                    current_table=table.name,
                )
                if not target:
                    continue
                relationships.append(
                    {
                        "source_entity": table.name,
                        "target_entity": target,
                        "relationship_type": "many_to_one",
                        "join_condition": f"{table.name}.{column.name} = {target}.{target_column}",
                        "reasoning": (
                            f"Foreign-key style column `{column.name}` suggests "
                            f"a relationship from {table.name} to {target}."
                        ),
                    }
                )
        deduped: List[Dict[str, str]] = []
        seen = set()
        for rel in relationships:
            key = (rel["source_entity"], rel["target_entity"], rel["join_condition"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(rel)
        return deduped

    def _target_table_for_key(
        self, column_name: str, tables: Iterable[TableDefinition], *, current_table: str
    ) -> tuple[Optional[str], str]:
        column_lower = column_name.lower()
        stem = column_lower[:-3]
        best: tuple[int, Optional[str], str] = (0, None, column_name)
        for table in tables:
            if table.name == current_table:
                continue
            table_slug = _slug(table.name)
            pk_names = [pk for pk in table.primary_keys]
            pk_lower = {pk.lower(): pk for pk in pk_names}
            target_column = pk_lower.get(column_lower) or (pk_names[0] if pk_names else column_name)

            score = 0
            if column_lower in pk_lower:
                score += 8
            if stem == table_slug or stem.rstrip("s") == table_slug.rstrip("s"):
                score += 6
            if table_slug in stem or stem in table_slug:
                score += 3
            if target_column.lower().endswith("_id"):
                score += 1
            if score > best[0]:
                best = (score, table.name, target_column)
        return (best[1], best[2]) if best[0] >= 4 else (None, column_name)

    def _osi_from_tables(
        self,
        *,
        name: str,
        tables: Sequence[TableDefinition],
        relationships: Sequence[Dict[str, str]],
        source_type: Optional[str],
    ) -> OSISemanticModel:
        datasets = []
        for table in tables:
            fields = []
            for column in table.columns:
                fields.append(
                    OSIField(
                        name=column.name,
                        data_type=column.logical_type,
                        expression=OSIExpression(
                            dialects=[
                                OSIExpressionDialect(
                                    dialect=osi_dialect_from_source_type(source_type),
                                    expression=column.name,
                                )
                            ]
                        ),
                    )
                )
            datasets.append(
                OSIDataset(
                    name=table.name,
                    source=table.name,
                    primary_key=list(table.primary_keys),
                    fields=fields,
                )
            )
        osi_relationships: List[OSIRelationship] = []
        for rel in relationships:
            _merge_relationship_into_osi(osi_relationships, rel)
        return OSISemanticModel(
            name=name,
            description=f"Semantic model for {name}",
            ai_context=OSIAIContext(
                instructions=f"Use {name} to generate transformation logic and analytics-ready semantic metadata.",
                synonyms=[name.replace("_", " "), name.title()],
            ),
            datasets=datasets,
            relationships=osi_relationships,
        )

    def _osi_from_intent(self, intent: BusinessIntent) -> OSISemanticModel:
        datasets = []
        grain_entity = intent.grain.entity if intent.grain else intent.data_product.name
        datasets.append(
            OSIDataset(
                name=_slug(grain_entity),
                source=_slug(grain_entity),
                primary_key=[f"{_slug(grain_entity)}_id"],
                fields=[
                    OSIField(
                        name=f"{_slug(grain_entity)}_id",
                        data_type="STRING",
                        expression=OSIExpression(
                            dialects=[
                                OSIExpressionDialect(
                                    dialect="ANSI_SQL", expression=f"{_slug(grain_entity)}_id"
                                )
                            ]
                        ),
                    )
                ],
            )
        )
        for entity in intent.dimensions.entities:
            datasets.append(
                OSIDataset(
                    name=_slug(entity),
                    source=_slug(entity),
                    primary_key=[f"{_slug(entity)}_id"],
                    fields=[
                        OSIField(
                            name=f"{_slug(entity)}_id",
                            data_type="STRING",
                            expression=OSIExpression(
                                dialects=[
                                    OSIExpressionDialect(
                                        dialect="ANSI_SQL", expression=f"{_slug(entity)}_id"
                                    )
                                ]
                            ),
                        )
                    ],
                )
            )
        metrics = [
            OSIMetric(
                name=_slug(metric.name),
                description=metric.description,
                expression=OSIExpression(
                    dialects=[
                        OSIExpressionDialect(
                            dialect="ANSI_SQL",
                            expression=f"SUM({_slug(metric.name)})",
                        )
                    ]
                ),
            )
            for metric in intent.metrics
        ]
        return OSISemanticModel(
            name=intent.data_product.name,
            description=intent.data_product.description,
            ai_context=OSIAIContext(
                instructions=intent.business_context.problem_statement,
                synonyms=[intent.data_product.name, intent.data_product.domain],
                examples=intent.consumption.use_cases,
            ),
            datasets=datasets,
            metrics=metrics,
        )

    def _dv2_from_tables(
        self, tables: Sequence[TableDefinition], relationships: Sequence[Dict[str, str]]
    ) -> DV2Model:
        hubs = []
        satellites = []
        for table in tables:
            keys = _business_keys_for_table(table)
            hubs.append(
                HubDefinition(
                    entity_name=table.name,
                    hub_table_name=f"hub_{_slug(table.name)}",
                    business_key_columns=keys,
                    mapped_source_tables=[table.name],
                    description=f"Hub derived from source table {table.name}.",
                )
            )
            non_key_columns = [column.name for column in table.columns if column.name not in keys]
            if non_key_columns:
                satellites.append(
                    SatelliteDefinition(
                        entity_name=table.name,
                        satellite_table_name=f"sat_{_slug(table.name)}_details",
                        parent_hub=f"hub_{_slug(table.name)}",
                        attributes=non_key_columns,
                        mapped_source_tables=[table.name],
                    )
                )

        links = []
        links_by_name: Dict[str, LinkDefinition] = {}
        for rel in relationships:
            source = f"hub_{_slug(rel['source_entity'])}"
            target = f"hub_{_slug(rel['target_entity'])}"
            link_table_name = f"lnk_{_slug(rel['source_entity'])}_{_slug(rel['target_entity'])}"
            link = links_by_name.get(link_table_name)
            if link is None:
                link = LinkDefinition(
                    link_name=f"{_slug(rel['source_entity'])}_{_slug(rel['target_entity'])}",
                    link_table_name=link_table_name,
                    hubs_involved=[source, target],
                )
                links.append(link)
                links_by_name[link_table_name] = link
            _merge_relationship_into_link(link, rel)
        return DV2Model(hubs=hubs, links=links, satellites=satellites)

    def _dv2_from_intent(self, intent: BusinessIntent) -> DV2Model:
        entities = []
        if intent.grain:
            entities.append(intent.grain.entity)
        entities.extend(intent.dimensions.entities)
        deduped = []
        for entity in entities:
            if entity and entity not in deduped:
                deduped.append(entity)
        hubs = [
            HubDefinition(
                entity_name=entity,
                hub_table_name=f"hub_{_slug(entity)}",
                business_key_columns=[f"{_slug(entity)}_id"],
                mapped_source_tables=[_slug(entity)],
            )
            for entity in deduped
        ]
        satellites = [
            SatelliteDefinition(
                entity_name=entity,
                satellite_table_name=f"sat_{_slug(entity)}_details",
                parent_hub=f"hub_{_slug(entity)}",
                attributes=list(intent.dimensions.attributes) or ["description"],
                mapped_source_tables=[_slug(entity)],
            )
            for entity in deduped
        ]
        links = []
        if intent.grain and intent.dimensions.entities:
            grain_slug = _slug(intent.grain.entity)
            for dim in intent.dimensions.entities:
                if _slug(dim) == grain_slug:
                    continue
                links.append(
                    LinkDefinition(
                        link_name=f"{_slug(intent.grain.entity)}_{_slug(dim)}",
                        link_table_name=f"lnk_{_slug(intent.grain.entity)}_{_slug(dim)}",
                        hubs_involved=[f"hub_{_slug(intent.grain.entity)}", f"hub_{_slug(dim)}"],
                    )
                )
        return DV2Model(hubs=hubs, links=links, satellites=satellites)

    def _dimensional_from_tables(
        self, *, name: str, tables: Sequence[TableDefinition]
    ) -> DimensionalModel:
        if not tables:
            return DimensionalModel(grain_statement=f"Facts at the {name} grain.")
        fact_table = max(tables, key=lambda table: len(table.columns))
        dimensions = [table for table in tables if table.name != fact_table.name]
        fact = FactTable(
            name=f"fact_{_slug(fact_table.name)}",
            grain_statement=f"One row per {_slug(fact_table.name)} event.",
            measures=[
                FieldDefinition(name=column.name, data_type=column.logical_type)
                for column in fact_table.columns
                if any(
                    token in column.logical_type.upper()
                    for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT")
                )
            ],
            foreign_keys=[
                column.name for column in fact_table.columns if column.name.endswith("_id")
            ],
        )
        dims = [
            DimensionTable(
                name=f"dim_{_slug(table.name)}",
                attributes=[
                    FieldDefinition(name=column.name, data_type=column.logical_type)
                    for column in table.columns
                ],
                surrogate_key=f"{_slug(table.name)}_sk",
                natural_keys=list(table.primary_keys),
            )
            for table in dimensions
        ]
        return DimensionalModel(
            facts=[fact],
            dimensions=dims,
            conformed_dimensions=[dimension.name for dimension in dims],
            grain_statement=fact.grain_statement,
        )

    def _dimensional_from_intent(self, intent: BusinessIntent) -> DimensionalModel:
        grain_entity = intent.grain.entity if intent.grain else intent.data_product.name
        grain_statement = (
            intent.grain.description.strip()
            if intent.grain and intent.grain.description.strip()
            else f"One row per {_slug(grain_entity)}."
        )
        fact = FactTable(
            name=f"fact_{_slug(grain_entity)}",
            grain_statement=grain_statement,
            measures=[
                FieldDefinition(
                    name=_slug(metric.name), data_type="NUMERIC", description=metric.description
                )
                for metric in intent.metrics
            ],
            foreign_keys=[f"{_slug(entity)}_id" for entity in intent.dimensions.entities],
        )
        dimensions = [
            DimensionTable(
                name=f"dim_{_slug(entity)}",
                attributes=[
                    FieldDefinition(name=attr, data_type="STRING")
                    for attr in (intent.dimensions.attributes or [f"{_slug(entity)}_name"])
                ],
                surrogate_key=f"{_slug(entity)}_sk",
                natural_keys=[f"{_slug(entity)}_id"],
                slowly_changing_type=(
                    intent.modeling.scd_policy_default if intent.modeling else None
                ),
            )
            for entity in intent.dimensions.entities
        ]
        return DimensionalModel(
            facts=[fact],
            dimensions=dimensions,
            conformed_dimensions=[dimension.name for dimension in dimensions],
            grain_statement=fact.grain_statement,
        )


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
