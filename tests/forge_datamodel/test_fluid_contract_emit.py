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
    FactTable,
    FieldDefinition,
)
from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSIDataset,
    OSIDimension,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator


def test_second_time_grain_is_normalized_before_contract_validation():
    logical = LogicalDraft(
        name="healthcare_events",
        description="Healthcare event analytics",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_events",
                    grain_statement="one row per clinical event",
                    measures=[FieldDefinition(name="event_count", data_type="INTEGER")],
                    foreign_keys=["patient_id"],
                    degenerate_dimensions=["event_timestamp"],
                )
            ],
            dimensions=[DimensionTable(name="dim_patient", natural_keys=["patient_id"])],
        ),
        osi=OSISemanticModel(
            name="healthcare_events",
            ai_context=OSIAIContext(),
            datasets=[
                OSIDataset(
                    name="events",
                    primary_key=["event_id"],
                    fields=[
                        OSIField(name="event_id", data_type="STRING"),
                        OSIField(
                            name="event_timestamp",
                            data_type="TIMESTAMP",
                            expression=OSIExpression(
                                dialects=[
                                    OSIExpressionDialect(
                                        dialect="ANSI_SQL",
                                        expression="event_timestamp",
                                    )
                                ]
                            ),
                            dimension=OSIDimension(is_time=True, grain="second"),
                        ),
                        OSIField(name="patient_id", data_type="STRING"),
                        OSIField(name="event_count", data_type="INTEGER"),
                    ],
                )
            ],
            metrics=[
                OSIMetric(
                    name="event_count",
                    description="Count of clinical events",
                    expression=OSIExpression(
                        dialects=[
                            OSIExpressionDialect(
                                dialect="ANSI_SQL",
                                expression="COUNT(event_id)",
                            )
                        ]
                    ),
                )
            ],
        ),
    )

    contract = build_contract_from_logical(logical)
    dimensions = contract["exposes"][0]["semantics"]["dimensions"]
    event_time = next(item for item in dimensions if item["name"] == "event_timestamp")

    assert event_time["type"] == "time"
    assert event_time["typeParams"]["timeGranularity"] == "minute"
    report = FluidContractValidator().validate(logical=logical, contract=contract)
    assert report.passes_schema is True
