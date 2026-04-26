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
    DV2Model,
    FactTable,
    FieldDefinition,
    HubDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import OSIDataset, OSIDimension, OSIField, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.logical_canonicalizer import canonicalize_logical_draft


def test_logical_pre_validator_repairs_missing_dv2_satellite_parent_hub() -> None:
    logical = LogicalDraft.model_validate(
        {
            "name": "biz_lab",
            "technique": "data_vault_2",
            "dv2": {
                "hubs": [
                    {
                        "entity_name": "account",
                        "hub_table_name": "hub_account",
                        "business_key_columns": ["ACCOUNT_ID"],
                    }
                ],
                "satellites": [
                    {
                        "entity_name": "account",
                        "satellite_table_name": "sat_account_details",
                        "attributes": ["STATUS"],
                    }
                ],
            },
            "osi": {"name": "biz_lab"},
        }
    )

    assert logical.dv2 is not None
    assert logical.dv2.satellites[0].parent_hub == "hub_account"


def test_canonicalizer_dedupes_and_normalizes_osi_time_grain() -> None:
    logical = LogicalDraft(
        name="orders",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_orders",
                    grain_statement="one row per order",
                    measures=[
                        FieldDefinition(name="revenue", data_type="number"),
                        FieldDefinition(name="revenue", data_type="number"),
                    ],
                )
            ]
        ),
        osi=OSISemanticModel(
            name="orders",
            datasets=[
                OSIDataset(
                    name="dim_customer",
                    fields=[OSIField(name="customer_id")],
                ),
                OSIDataset(
                    name="fact_orders",
                    primary_key=["order_id", "order_id"],
                    fields=[
                        OSIField(
                            name="order_ts", dimension=OSIDimension(is_time=True, grain="second")
                        ),
                        OSIField(name="order_id"),
                        OSIField(
                            name="order_ts", dimension=OSIDimension(is_time=True, grain="second")
                        ),
                    ],
                ),
            ],
        ),
    )

    canonical = canonicalize_logical_draft(logical)

    assert canonical.dimensional is not None
    assert [measure.name for measure in canonical.dimensional.facts[0].measures] == ["revenue"]
    assert [dataset.name for dataset in canonical.osi.datasets] == ["fact_orders", "dim_customer"]
    fact_fields = canonical.osi.datasets[0].fields
    assert [field.name for field in fact_fields] == ["order_id", "order_ts"]
    assert fact_fields[1].dimension is not None
    assert fact_fields[1].dimension.grain == "minute"


def test_canonicalizer_repairs_empty_dv2_business_keys() -> None:
    logical = LogicalDraft(
        name="biz_lab",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="service account",
                    hub_table_name="hub_service_account",
                    business_key_columns=[],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="service account",
                    satellite_table_name="sat_service_account_details",
                    parent_hub="unknown",
                    attributes=["status", "status"],
                )
            ],
        ),
        osi=OSISemanticModel(name="biz_lab"),
    )

    canonical = canonicalize_logical_draft(logical)

    assert canonical.dv2 is not None
    assert canonical.dv2.hubs[0].business_key_columns == ["service_account_id"]
    assert canonical.dv2.satellites[0].parent_hub == "hub_service_account"
    assert canonical.dv2.satellites[0].attributes == ["status"]
