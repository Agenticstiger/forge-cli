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

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    FieldDefinition,
    HubDefinition,
    JoinKeyDetail,
    LinkDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import (
    OSIDataset,
    OSIDimension,
    OSIField,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.model_doc import emit_model_markdown


def test_model_doc_dv2_includes_hub_link_satellite_mermaid():
    logical = LogicalDraft(
        name="customer_orders",
        description="Customer order model",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="Customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                    mapped_source_tables=["CUSTOMER"],
                ),
                HubDefinition(
                    entity_name="Order",
                    hub_table_name="hub_order",
                    business_key_columns=["order_id"],
                    mapped_source_tables=["ORDER"],
                ),
            ],
            links=[
                LinkDefinition(
                    link_name="customer_order",
                    link_table_name="lnk_customer_order",
                    hubs_involved=["hub_customer", "hub_order"],
                    join_keys=[
                        JoinKeyDetail(
                            table1="CUSTOMER",
                            column1="customer_id",
                            table2="ORDER",
                            column2="customer_id",
                        )
                    ],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="Customer",
                    satellite_table_name="sat_customer_profile",
                    parent_hub="hub_customer",
                    attributes=["customer_name"],
                )
            ],
        ),
        osi=OSISemanticModel(
            name="customer_orders",
            datasets=[
                OSIDataset(
                    name="customer_orders",
                    source="ORDER",
                    primary_key=["order_id"],
                    fields=[
                        OSIField(name="customer_id", data_type="STRING"),
                        OSIField(
                            name="order_date",
                            data_type="DATE",
                            dimension=OSIDimension(is_time=True, grain="day"),
                        ),
                    ],
                )
            ],
        ),
        source_summary={"source_kind": "intent"},
        review_notes=["Customer is identified by customer_id."],
    )

    markdown = emit_model_markdown(logical)

    assert "```mermaid" in markdown
    assert "hub_customer" in markdown
    assert "lnk_customer_order" in markdown
    assert "sat_customer_profile" in markdown
    assert "### Hubs" in markdown
    assert "### Links" in markdown
    assert "### Satellites" in markdown
    assert "Customer is identified by customer_id." in markdown


def test_model_doc_dimensional_includes_fact_dimension_relationships():
    logical = LogicalDraft(
        name="customer_orders",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_order_line",
                    grain_statement="one row per order line",
                    foreign_keys=["customer_key"],
                    measures=[
                        FieldDefinition(
                            name="gross_revenue",
                            data_type="NUMBER",
                            description="Gross revenue",
                        )
                    ],
                )
            ],
            dimensions=[
                DimensionTable(
                    name="dim_customer",
                    surrogate_key="customer_key",
                    natural_keys=["customer_id"],
                    attributes=[FieldDefinition(name="customer_name", data_type="STRING")],
                )
            ],
        ),
        osi=OSISemanticModel(
            name="customer_orders",
            datasets=[
                OSIDataset(
                    name="fact_order_line",
                    primary_key=["order_line_id"],
                    fields=[
                        OSIField(name="customer_id", data_type="STRING"),
                        OSIField(name="gross_revenue", data_type="NUMBER"),
                    ],
                )
            ],
        ),
    )

    markdown = emit_model_markdown(logical)

    assert "```mermaid" in markdown
    assert "fact_order_line" in markdown
    assert "dim_customer" in markdown
    assert "fact_order_line --> dim_customer" in markdown
    assert "### Facts" in markdown
    assert "### Dimensions" in markdown
    assert "gross_revenue" in markdown
