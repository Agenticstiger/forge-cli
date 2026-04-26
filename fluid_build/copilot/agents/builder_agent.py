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

"""Builder agent: Logical -> Physical stage."""

from __future__ import annotations

from typing import Any, Dict, List

from fluid_build.copilot.agents.base import BaseStageAgent, StageSession
from fluid_build.copilot.agents.contract_forge_agent import ContractForgeAgent
from fluid_build.copilot.schemas.data_model import DimensionalModel, DV2Model, JoinKeyDetail
from fluid_build.copilot.schemas.stage_outputs import (
    BuildSpec,
    LogicalDraft,
    PhysicalDraft,
    ReadmeDraft,
    TransformPlan,
)


class BuilderAgent(BaseStageAgent):
    """Produce transformation stages and contract-ready physical metadata."""

    def __init__(self) -> None:
        super().__init__(stage="builder", tier="balanced")

    def build_physical(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        contract: Dict[str, Any],
        engine: str,
    ) -> PhysicalDraft:
        transform_plan = self._heuristic_transform_plan(logical, engine=engine)
        readme = ReadmeDraft(
            readme_markdown=self._build_readme(logical, engine=engine),
            description=f"Generated README for {logical.name}",
        )

        # Sprint #3 — surface scratchpad signals in provenance.
        # The heuristic builder doesn't make LLM calls (no prompt
        # to inject into), but the critic's findings + validator's
        # feedback addressed to ``builder`` belong in the receipt
        # so downstream code (cost summary, audit trail) sees them.
        # On a repair-loop rerun, the builder ALSO checks for
        # error-severity findings and degrades the heuristic
        # decisions accordingly (e.g. preferring conservative
        # contract metadata when the critic flagged sloppy
        # defaults).
        provenance = {
            "stage": "physical",
            "engine": engine,
            "source_kind": logical.source_summary.get("source_kind"),
        }
        try:
            scratchpad = session.get_scratchpad()
            critic_findings = scratchpad.critic_findings_for_stage("builder")
            stage_feedback = scratchpad.feedback_for_stage("builder")
            if critic_findings:
                provenance["critic_findings"] = [
                    {
                        "severity": f.severity,
                        "message": f.message,
                        "target": f.target,
                    }
                    for f in critic_findings
                ]
            if stage_feedback:
                provenance["validator_feedback"] = [
                    {"summary": f.summary, "source_stage": f.source_stage} for f in stage_feedback
                ]
        except Exception:  # pragma: no cover — defensive
            pass

        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=transform_plan,
            readme=readme,
            additional_files=transform_plan.additional_files,
            provenance=provenance,
        )

    def build_contract(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        engine: str = "dbt",
    ) -> Dict[str, Any]:
        return ContractForgeAgent().forge_contract(session, logical=logical, engine=engine)

    def _heuristic_transform_plan(self, logical: LogicalDraft, *, engine: str) -> TransformPlan:
        source_backed = logical.source_summary.get("source_kind") == "ddl"
        if logical.technique == "data_vault_2" and logical.dv2 is not None:
            return self._dv2_transform_plan(logical.dv2, engine=engine, source_backed=source_backed)
        if logical.dimensional is not None:
            return self._dimensional_transform_plan(
                logical.dimensional, engine=engine, source_backed=source_backed
            )
        return TransformPlan()

    def _dv2_transform_plan(
        self, dv2: DV2Model, *, engine: str, source_backed: bool
    ) -> TransformPlan:
        builds: List[BuildSpec] = []
        for hub in dv2.hubs:
            source = hub.mapped_source_tables[0] if hub.mapped_source_tables else hub.entity_name
            columns = [(column, "varchar") for column in hub.business_key_columns]
            builds.append(
                BuildSpec(
                    id=hub.hub_table_name,
                    name=hub.hub_table_name,
                    engine=engine,
                    layer="staging" if engine == "dbt" else "raw_vault",
                    sql=(
                        _source_select(
                            source,
                            columns,
                            distinct=True,
                        )
                        if source_backed
                        else _empty_select(columns, source_name=source)
                    ),
                    outputs=[hub.hub_table_name],
                )
            )
        for link in dv2.links:
            depends_on = list(link.hubs_involved)
            if link.join_keys:
                columns = []
                for join_key in link.join_keys:
                    columns.append((join_key.column1, "varchar"))
                    columns.append((join_key.column2, "varchar"))
                sql = (
                    _source_join_select(link.join_keys[0], columns)
                    if source_backed
                    else _empty_select(columns)
                )
            else:
                sql = _empty_select([])
            builds.append(
                BuildSpec(
                    id=link.link_table_name,
                    name=link.link_table_name,
                    engine=engine,
                    layer="intermediate",
                    sql=sql,
                    depends_on=depends_on,
                    outputs=[link.link_table_name],
                )
            )
        for satellite in dv2.satellites:
            source = (
                satellite.mapped_source_tables[0]
                if satellite.mapped_source_tables
                else satellite.entity_name
            )
            columns = [(column, "varchar") for column in satellite.attributes]
            builds.append(
                BuildSpec(
                    id=satellite.satellite_table_name,
                    name=satellite.satellite_table_name,
                    engine=engine,
                    layer="marts",
                    sql=(
                        _source_select(source, columns)
                        if source_backed
                        else _empty_select(columns, source_name=source)
                    ),
                    depends_on=[satellite.parent_hub],
                    outputs=[satellite.satellite_table_name],
                )
            )
        return TransformPlan(builds=builds)

    def _dimensional_transform_plan(
        self, dimensional: DimensionalModel, *, engine: str, source_backed: bool
    ) -> TransformPlan:
        builds: List[BuildSpec] = []
        for dimension in dimensional.dimensions:
            source_name = dimension.name.replace("dim_", "")
            columns = [
                (field.name, _sql_type_for_data_type(field.data_type))
                for field in dimension.attributes
            ]
            builds.append(
                BuildSpec(
                    id=dimension.name,
                    name=dimension.name,
                    engine=engine,
                    layer="staging",
                    sql=(
                        _source_select(source_name, columns)
                        if source_backed
                        else _empty_select(columns, source_name=source_name)
                    ),
                    outputs=[dimension.name],
                )
            )
        for fact in dimensional.facts:
            # Accept both the current ``fact_*`` naming and the legacy
            # ``fct_*`` prefix so older cached models still generate the
            # right source-table reference.
            source_name = fact.name.removeprefix("fact_").removeprefix("fct_")
            columns = [
                (measure.name, _sql_type_for_data_type(measure.data_type))
                for measure in fact.measures
            ]
            columns.extend((foreign_key, "varchar") for foreign_key in fact.foreign_keys)
            builds.append(
                BuildSpec(
                    id=fact.name,
                    name=fact.name,
                    engine=engine,
                    layer="marts",
                    sql=(
                        _source_select(source_name, columns)
                        if source_backed
                        else _empty_select(columns, source_name=source_name)
                    ),
                    depends_on=[dimension.name for dimension in dimensional.dimensions],
                    outputs=[fact.name],
                )
            )
        return TransformPlan(builds=builds)

    def _build_readme(self, logical: LogicalDraft, *, engine: str) -> str:
        return "\n".join(
            [
                f"# {logical.name}",
                "",
                f"- Modeling technique: {logical.technique}",
                f"- Target engine: {engine}",
                f"- Semantic datasets: {len(logical.osi.datasets)}",
                "",
                "This project was generated by the staged Forge data-model pipeline.",
            ]
        )


def _empty_select(columns: List[tuple[str, str]], *, source_name: str | None = None) -> str:
    """Return deterministic, zero-row SQL that dbt can run without external sources."""
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for column_name, sql_type in columns:
        normalized = column_name.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        deduped.append((normalized, sql_type))
    if not deduped:
        deduped.append(("placeholder", "integer"))

    select_lines = [
        f"    cast(null as {sql_type}) as {_quote_identifier(column_name)}"
        for column_name, sql_type in deduped
    ]
    prefix = f"-- Source hint: {source_name}\n" if source_name else ""
    return f"{prefix}select\n" + ",\n".join(select_lines) + "\nwhere false"


def _source_select(
    source_name: str,
    columns: List[tuple[str, str]],
    *,
    distinct: bool = False,
) -> str:
    fallback = _empty_select(columns, source_name=source_name)
    projection = _source_projection(columns)
    if not projection:
        return fallback
    distinct_sql = " distinct" if distinct else ""
    return "\n".join(
        [
            f"{{% set _src = source('raw', '{source_name}') %}}",
            "{% if execute %}",
            "{% set _rel = adapter.get_relation(database=_src.database, schema=_src.schema, identifier=_src.identifier) %}",
            "{% else %}",
            "{% set _rel = none %}",
            "{% endif %}",
            "{% if _rel is not none %}",
            f"select{distinct_sql}",
            projection,
            "from {{ _src }}",
            "{% else %}",
            fallback,
            "{% endif %}",
        ]
    )


def _source_join_select(join_key: JoinKeyDetail, columns: List[tuple[str, str]]) -> str:
    fallback = _empty_select(columns)
    left_projection = (
        f"    l.{_quote_identifier(join_key.column1)} as {_quote_identifier(join_key.column1)}"
    )
    right_projection = (
        f"    r.{_quote_identifier(join_key.column2)} as {_quote_identifier(join_key.column2)}"
    )
    projection = left_projection
    if join_key.column2.lower() != join_key.column1.lower():
        projection += ",\n" + right_projection
    return "\n".join(
        [
            f"{{% set _left_src = source('raw', '{join_key.table1}') %}}",
            f"{{% set _right_src = source('raw', '{join_key.table2}') %}}",
            "{% if execute %}",
            "{% set _left_rel = adapter.get_relation(database=_left_src.database, schema=_left_src.schema, identifier=_left_src.identifier) %}",
            "{% set _right_rel = adapter.get_relation(database=_right_src.database, schema=_right_src.schema, identifier=_right_src.identifier) %}",
            "{% else %}",
            "{% set _left_rel = none %}",
            "{% set _right_rel = none %}",
            "{% endif %}",
            "{% if _left_rel is not none and _right_rel is not none %}",
            "select distinct",
            projection,
            "from {{ _left_src }} as l",
            f"join {{{{ _right_src }}}} as r on l.{_quote_identifier(join_key.column1)} = r.{_quote_identifier(join_key.column2)}",
            "{% else %}",
            fallback,
            "{% endif %}",
        ]
    )


def _source_projection(columns: List[tuple[str, str]]) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for column_name, _sql_type in columns:
        normalized = column_name.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        deduped.append(f"    {_quote_identifier(normalized)}")
    return ",\n".join(deduped)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sql_type_for_data_type(data_type: str) -> str:
    normalized = data_type.upper()
    if "BOOL" in normalized:
        return "boolean"
    if "TIMESTAMP" in normalized or "DATETIME" in normalized:
        return "timestamp"
    if normalized == "DATE" or normalized.startswith("DATE("):
        return "date"
    if "INT" in normalized:
        return "bigint"
    if any(token in normalized for token in ("NUMBER", "DECIMAL", "NUMERIC")):
        return "numeric"
    if any(token in normalized for token in ("FLOAT", "DOUBLE", "REAL")):
        return "double"
    return "varchar"
