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

from fluid_build.copilot.schemas.data_model import DV2Model, HubDefinition
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct, Grain
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft


def test_business_intent_round_trip():
    intent = BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
    )
    dumped = intent.model_dump()
    loaded = BusinessIntent.model_validate(dumped)
    assert loaded.data_product.name == "orders"
    assert loaded.grain is not None
    assert loaded.grain.entity == "order_line"


def test_logical_draft_accepts_dv2_shape():
    draft = LogicalDraft(
        name="orders",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                )
            ]
        ),
        osi=OSISemanticModel(name="orders", ai_context=OSIAIContext()),
        conceptual=ConceptualDraft(name="orders"),
    )
    assert draft.dv2 is not None
    assert draft.dimensional is None


def test_logical_draft_normalizes_nullable_llm_metadata():
    draft = LogicalDraft.model_validate(
        {
            "name": "orders",
            "technique": "data_vault_2",
            "conceptual": {"name": "orders", "ai_context": None},
            "dv2": {
                "hubs": [
                    {
                        "entity_name": "customer",
                        "hub_table_name": "hub_customer",
                        "business_key_columns": ["customer_id"],
                    }
                ]
            },
            "dimensional": None,
            "osi": None,
            "source_summary": None,
        }
    )

    assert draft.osi.name == "orders"
    assert draft.source_summary == {}
    assert draft.conceptual is not None
    assert draft.conceptual.ai_context.synonyms == []
