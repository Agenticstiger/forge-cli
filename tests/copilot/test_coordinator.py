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

import json

import pytest
from pydantic import ValidationError

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.builder_agent import BuilderAgent
from fluid_build.copilot.agents.contract_forge_agent import ContractForgeAgent
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.agents.errors import AgentExecutionError
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.data_model import (
    DV2Model,
    EntityRelationship,
    HubDefinition,
    JoinKeyDetail,
    LinkDefinition,
)
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct, Dimensions, Grain
from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSIDataset,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator
from fluid_build.forge_datamodel.from_ddl.parser import DDLParser


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_coordinator_from_intent_produces_valid_contract():
    session = StageSession(store=NullBackend())
    intent = BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )

    result = StageCoordinator().from_intent(session, intent=intent, technique="dimensional")
    report = FluidContractValidator().validate(logical=result.logical, contract=result.contract)

    assert result.logical.technique == "dimensional"
    assert report.passes_schema is True
    assert result.contract["labels"]["dataModelingTechnique"] == "dimensional"
    assert result.contract["labels"]["contractForgedBy"] == "ContractForgeAgent"
    assert result.contract["labels"]["agenticMode"] == "heuristic"
    assert result.contract["labels"]["agenticFallbackUsed"] == "false"
    manifest = json.loads(result.contract["labels"]["agenticStageManifest"])
    assert {
        "stage": "logical",
        "agent": "LogicalAgent",
        "mode": "heuristic",
        "status": "completed",
    } in manifest
    assert {
        "stage": "contract",
        "agent": "ContractForgeAgent",
        "mode": "deterministic",
        "status": "completed",
    } in manifest


def test_builder_agent_produces_transform_plan_from_logical():
    session = StageSession(store=NullBackend())
    ddl = """
    CREATE TABLE orders (
        order_id VARCHAR(64) PRIMARY KEY,
        customer_id VARCHAR(64),
        amount DECIMAL(18,2)
    );
    CREATE TABLE customers (
        customer_id VARCHAR(64) PRIMARY KEY,
        customer_name STRING
    );
    """
    tables = DDLParser().parse_ddl_content(ddl)
    coordinator = StageCoordinator()
    result = coordinator.from_tables(
        session, name="orders", tables=tables, technique="data_vault_2"
    )

    physical = BuilderAgent().build_physical(
        session,
        logical=result.logical,
        contract=result.contract,
        engine="dbt",
    )

    assert physical.transform_plan.builds
    assert physical.transform_plan.builds[0].engine == "dbt"
    assert physical.logical.technique == "data_vault_2"


def test_dv2_relationship_type_defaults_to_association():
    relationship = EntityRelationship(
        source_entity="Party",
        target_entity="Account",
    )

    assert relationship.relationship_type == "association"


def test_from_tables_infers_links_from_uppercase_snowflake_keys():
    session = StageSession(store=NullBackend())
    ddl = """
    create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."ACCOUNT" (
        "ACCOUNT_ID" VARCHAR(64) PRIMARY KEY,
        "PARTY_ID" VARCHAR(64),
        "STATUS" VARCHAR(32)
    );
    create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."PARTY" (
        "PARTY_ID" VARCHAR(64) PRIMARY KEY,
        "PARTY_TYPE" VARCHAR(32)
    );
    """
    tables = DDLParser().parse_ddl_content(ddl, dialect="snowflake")

    result = StageCoordinator().from_tables(
        session, name="telco", tables=tables, technique="data_vault_2"
    )

    assert result.logical.dv2 is not None
    assert [link.link_table_name for link in result.logical.dv2.links] == ["lnk_account_party"]
    join_key = result.logical.dv2.links[0].join_keys[0]
    assert join_key.column1 == "PARTY_ID"
    assert join_key.column2 == "PARTY_ID"


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_from_tables_combines_repeated_relationships_into_one_link():
    session = StageSession(store=NullBackend())
    ddl = """
    create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."PARTY_ROLE" (
        "PARTY_ROLE_ID" VARCHAR(64) PRIMARY KEY,
        "PARTY_ID" VARCHAR(64),
        "RELATED_PARTY_ID" VARCHAR(64)
    );
    create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."PARTY" (
        "PARTY_ID" VARCHAR(64) PRIMARY KEY,
        "PARTY_TYPE" VARCHAR(32)
    );
    """
    tables = DDLParser().parse_ddl_content(ddl, dialect="snowflake")

    result = StageCoordinator().from_tables(
        session, name="telco", tables=tables, technique="data_vault_2"
    )

    assert result.logical.dv2 is not None
    links = [
        link for link in result.logical.dv2.links if link.link_table_name == "lnk_party_role_party"
    ]
    assert len(links) == 1
    assert [(key.column1, key.column2) for key in links[0].join_keys] == [
        ("PARTY_ID", "PARTY_ID"),
        ("RELATED_PARTY_ID", "PARTY_ID"),
    ]
    assert [rel.name for rel in result.logical.osi.relationships].count("PARTY_ROLE_to_PARTY") == 1


def test_required_llm_refuses_modeler_heuristic_fallback(monkeypatch):
    session = StageSession(
        store=NullBackend(),
        llm_config=object(),
        active_provider="gemini",
        require_llm=True,
    )
    intent = BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer"]),
    )

    def fail_llm(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ModelerAgent, "_llm_from_intent", fail_llm)

    with pytest.raises(AgentExecutionError, match="LLM execution is required"):
        ModelerAgent().from_intent(session, intent=intent, technique="dimensional")


def test_non_strict_llm_fallback_is_visible_in_forged_contract(monkeypatch):
    session = StageSession(
        store=NullBackend(),
        llm_config=object(),
        active_provider="gemini",
    )
    intent = BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer"]),
    )

    def fail_llm(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ModelerAgent, "_llm_from_intent", fail_llm)

    result = StageCoordinator().from_intent(
        session,
        intent=intent,
        technique="dimensional",
    )

    labels = result.contract["labels"]
    assert session.fallback_used is True
    assert labels["contractForgedBy"] == "ContractForgeAgent"
    assert labels["agenticMode"] == "llm_with_fallback"
    assert labels["agenticFallbackUsed"] == "true"
    assert labels["agenticFallbackStages"] == "modeler"
    assert labels["agenticFallbackReasons"] == "llm_failed:RuntimeError"
    assert labels["llmProvider"] == "gemini"


def test_strict_llm_schema_repair_keeps_agentic_path(monkeypatch):
    session = StageSession(
        store=NullBackend(),
        llm_config=object(),
        active_provider="ollama",
        require_llm=True,
    )
    intent = BusinessIntent(
        data_product=DataProduct(name="telco_usage", domain="telecommunications"),
        grain=Grain(entity="usage_event", time_dimension="event_timestamp"),
        dimensions=Dimensions(entities=["party", "account"]),
    )
    try:
        LogicalDraft.model_validate(
            {
                "name": "telco_usage",
                "technique": "data_vault_2",
                "dv2": {
                    "hubs": [
                        {
                            "entity_name": "party",
                            "hub_table_name": "hub_party",
                            "business_key_columns": ["party_id"],
                        },
                        {
                            "entity_name": "account",
                            "hub_table_name": "hub_account",
                            "business_key_columns": ["account_id"],
                        },
                    ],
                    "links": [
                        {
                            "link_name": "party_account",
                            "link_table_name": "lnk_party_account",
                            "hubs_involved": ["hub_party", "hub_account"],
                            "join_keys": ["party_id"],
                        }
                    ],
                },
                "osi": {"name": "telco_usage"},
            }
        )
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - this fixture must stay invalid
        raise AssertionError("invalid LogicalDraft fixture unexpectedly validated")

    repaired = LogicalDraft(
        name="telco_usage",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="party",
                    hub_table_name="hub_party",
                    business_key_columns=["party_id"],
                ),
                HubDefinition(
                    entity_name="account",
                    hub_table_name="hub_account",
                    business_key_columns=["account_id"],
                ),
            ],
            links=[
                LinkDefinition(
                    link_name="party_account",
                    link_table_name="lnk_party_account",
                    hubs_involved=["hub_party", "hub_account"],
                    join_keys=[
                        JoinKeyDetail(
                            table1="party",
                            column1="party_id",
                            table2="account",
                            column2="party_id",
                            reasoning="Party owns account.",
                        )
                    ],
                )
            ],
        ),
        osi=OSISemanticModel(
            name="telco_usage",
            ai_context=OSIAIContext(),
            datasets=[
                OSIDataset(
                    name="usage_event",
                    primary_key=["usage_event_id"],
                    fields=[
                        OSIField(name="usage_event_id", data_type="STRING"),
                        OSIField(name="party_id", data_type="STRING"),
                        OSIField(name="account_id", data_type="STRING"),
                    ],
                )
            ],
            metrics=[
                OSIMetric(
                    name="event_count",
                    expression=OSIExpression(
                        dialects=[
                            OSIExpressionDialect(
                                dialect="ANSI_SQL",
                                expression="COUNT(usage_event_id)",
                            )
                        ]
                    ),
                )
            ],
        ),
    )
    calls = []

    def fake_call(
        self,
        session,
        *,
        system_prompt,
        user_prompt,
        output_schema,
        params,
        retry_schema_errors=True,
    ):
        calls.append({"user_prompt": user_prompt, "params": params})
        assert retry_schema_errors is False
        if len(calls) == 1:
            raise validation_error
        assert "schema_repair_attempt" in params
        assert "dv2.links[].join_keys[]" in user_prompt
        return repaired

    monkeypatch.setattr(ModelerAgent, "call", fake_call)

    result = ModelerAgent().from_intent(
        session,
        intent=intent,
        technique="data_vault_2",
    )

    assert result.dv2 is not None
    assert len(calls) == 2
    assert session.repair_used is True
    assert session.fallback_used is False
    labels = ContractForgeAgent().forge_contract(session, logical=result)["labels"]
    assert labels["agenticMode"] == "strict_llm"
    assert labels["agenticFallbackUsed"] == "false"
    assert labels["agenticRepairUsed"] == "true"
    assert labels["agenticRepairStages"] == "modeler"
    assert labels["agenticRepairReasons"] == "schema_validation_failed:ValidationError"
    assert "join_keys" in labels["agenticRepairDetails"]
