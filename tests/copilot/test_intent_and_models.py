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

import pytest
from pydantic import ValidationError

from fluid_build.copilot.schemas.data_model import (
    BridgeDefinition,
    DimensionTable,
    DV2Model,
    FactTable,
    HubDefinition,
    LinkDefinition,
    PitDefinition,
    SatelliteDefinition,
)
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


# ---------------------------------------------------------------------------
# SECURITY — table-name fields must reject path-traversal payloads
# ---------------------------------------------------------------------------
#
# These LLM-chosen names flow verbatim into physical file paths
# (``models/<layer>/<name>.sql``) on the staged authoring path. LLM output
# is UNTRUSTED, so a name with a path separator or ``..`` is a
# prompt-injection → arbitrary-file-write vector. The schema validator is
# the innermost of three defense layers.


_MALICIOUS_NAMES = [
    "../../../../tmp/pwned",
    "..",
    "foo/../bar",
    "sub/dir/name",
    "back\\slash",
    "/abs/path",
]


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_hub_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        HubDefinition(entity_name="customer", hub_table_name=bad)


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_link_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        LinkDefinition(link_name="cust_order", link_table_name=bad)


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_satellite_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        SatelliteDefinition(
            entity_name="customer", satellite_table_name=bad, parent_hub="hub_customer"
        )


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_pit_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        PitDefinition(pit_table_name=bad, parent_hub="hub_customer")


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_bridge_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        BridgeDefinition(bridge_table_name=bad)


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_fact_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        FactTable(name=bad, grain_statement="one row per order line")


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_dimension_table_name_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        DimensionTable(name=bad)


def test_table_names_accept_benign_values():
    """Positive control: legitimate names must still validate."""
    assert HubDefinition(entity_name="customer", hub_table_name="hub_customer").hub_table_name == (
        "hub_customer"
    )
    assert FactTable(name="fct_orders", grain_statement="one row per order").name == "fct_orders"
    assert DimensionTable(name="dim_customer").name == "dim_customer"
    # The DV2 container composing the validated leaf models still builds.
    model = DV2Model(
        hubs=[HubDefinition(entity_name="customer", hub_table_name="hub_customer")],
        satellites=[
            SatelliteDefinition(
                entity_name="customer",
                satellite_table_name="sat_customer",
                parent_hub="hub_customer",
            )
        ],
    )
    assert model.hubs[0].hub_table_name == "hub_customer"
