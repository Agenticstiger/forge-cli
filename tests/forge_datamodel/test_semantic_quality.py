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

from __future__ import annotations

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    HubDefinition,
    LinkDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import (
    OSIDataset,
    OSIDimension,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator


def test_dv2_quality_gate_errors_on_hub_without_business_key() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=[],
                )
            ],
        ),
        osi=OSISemanticModel(name="orders"),
    )

    report = FluidContractValidator().validate(logical=logical)

    assert report.passes_schema is False
    assert any(
        issue.severity == "error" and issue.field == "dv2.hubs[0].business_key_columns"
        for issue in report.issues
    )


def test_dv2_quality_gate_errors_on_orphan_satellite_parent() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="order",
                    satellite_table_name="sat_order",
                    parent_hub="hub_order",
                    attributes=["order_status"],
                )
            ],
        ),
        osi=OSISemanticModel(name="orders"),
    )

    report = FluidContractValidator().validate(logical=logical)

    assert report.passes_schema is False
    assert any(
        issue.severity == "error" and issue.field == "dv2.satellites[0].parent_hub"
        for issue in report.issues
    )


def test_dimensional_quality_gate_warns_on_measureless_fact() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_orders",
                    grain_statement="one row per order",
                    foreign_keys=["customer_id"],
                )
            ],
            dimensions=[DimensionTable(name="dim_customer", natural_keys=["customer_id"])],
        ),
        osi=OSISemanticModel(name="orders"),
    )

    report = FluidContractValidator().validate(logical=logical)

    assert report.passes_schema is True
    assert any(
        issue.severity == "warning" and issue.field == "dimensional.facts[0].measures"
        for issue in report.issues
    )


def test_osi_quality_gate_errors_on_invalid_time_grain() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_orders",
                    grain_statement="one row per order",
                    measures=[],
                )
            ],
        ),
        osi=OSISemanticModel(
            name="orders",
            datasets=[
                OSIDataset(
                    name="fact_orders",
                    fields=[
                        OSIField(
                            name="order_ts",
                            dimension=OSIDimension(is_time=True, grain="fortnight"),
                        )
                    ],
                )
            ],
            metrics=[
                OSIMetric(
                    name="order_count",
                    expression=OSIExpression(
                        dialects=[
                            OSIExpressionDialect(
                                dialect="ANSI_SQL",
                                expression="count(*)",
                            )
                        ]
                    ),
                )
            ],
        ),
    )

    report = FluidContractValidator().validate(logical=logical)

    assert report.passes_schema is False
    assert any(
        issue.severity == "error" and issue.field == "osi.datasets[0].fields[0].dimension.grain"
        for issue in report.issues
    )


def test_dv2_complete_quality_shape_passes_schema() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                ),
                HubDefinition(
                    entity_name="order",
                    hub_table_name="hub_order",
                    business_key_columns=["order_id"],
                ),
            ],
            links=[
                LinkDefinition(
                    link_name="customer_order",
                    link_table_name="lnk_customer_order",
                    hubs_involved=["hub_customer", "hub_order"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="customer",
                    satellite_table_name="sat_customer",
                    parent_hub="hub_customer",
                    attributes=["customer_name"],
                )
            ],
        ),
        osi=OSISemanticModel(name="orders"),
    )

    report = FluidContractValidator().validate(logical=logical)

    assert report.passes_schema is True
