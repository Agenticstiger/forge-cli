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

"""Skeleton-lint coverage on :class:`FluidContractValidator`.

The lint fires when an :class:`IndustryPack` carries a seed skeleton and
the emitted :class:`LogicalDraft` disagrees with it. We verify three
behaviours that matter in production:

* **Clean match**: same names → zero skeleton warnings.
* **Naming drift**: close-but-different names (``hub_customer`` vs the
  canonical ``hub_party``) → ``warning`` severity citing both names.
* **Missing canonical entity**: skeleton defines a hub/link/satellite
  that has no fuzzy counterpart in the emitted model → ``warning``
  citing the missing name.

We exercise both DV2 and Dimensional techniques so the shared helper
doesn't regress on either IR shape.
"""

from __future__ import annotations

from fluid_build.copilot.industry.pack import IndustryPack
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
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator


def _osi() -> OSISemanticModel:
    """OSI stub sufficient to pass the validator's own shape check."""
    return OSISemanticModel(name="fixture", ai_context=OSIAIContext())


def _conceptual() -> ConceptualDraft:
    return ConceptualDraft(name="fixture")


def _telco_dv2_pack(
    *,
    hubs: list[HubDefinition] | None = None,
    links: list[LinkDefinition] | None = None,
    satellites: list[SatelliteDefinition] | None = None,
) -> IndustryPack:
    """Build a minimal telco-flavoured pack with a DV2 skeleton."""
    return IndustryPack(
        name="telecommunications",
        version="1.0",
        seed_dv2_skeleton=DV2Model(
            hubs=hubs
            or [
                HubDefinition(
                    entity_name="party",
                    hub_table_name="hub_party",
                    business_key_columns=["party_id"],
                ),
                HubDefinition(
                    entity_name="service",
                    hub_table_name="hub_service",
                    business_key_columns=["service_id"],
                ),
            ],
            links=links
            or [
                LinkDefinition(
                    link_name="party_service",
                    link_table_name="lnk_party_service",
                    hubs_involved=["hub_party", "hub_service"],
                    join_keys=[
                        JoinKeyDetail(
                            table1="hub_party",
                            column1="party_id",
                            table2="hub_service",
                            column2="service_id",
                        )
                    ],
                )
            ],
            satellites=satellites
            or [
                SatelliteDefinition(
                    entity_name="party_profile",
                    satellite_table_name="sat_party_profile",
                    parent_hub="hub_party",
                    attributes=["party_name"],
                )
            ],
        ),
    )


def _retail_dim_pack() -> IndustryPack:
    return IndustryPack(
        name="retail",
        version="1.0",
        seed_dimensional_skeleton=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_sales_line",
                    grain_statement="one row per sold line item",
                    measures=[FieldDefinition(name="sales_amount", data_type="NUMBER")],
                    foreign_keys=["customer_id", "product_id"],
                )
            ],
            dimensions=[
                DimensionTable(
                    name="dim_customer",
                    attributes=[FieldDefinition(name="customer_name", data_type="STRING")],
                    natural_keys=["customer_id"],
                ),
                DimensionTable(
                    name="dim_product",
                    attributes=[FieldDefinition(name="product_name", data_type="STRING")],
                    natural_keys=["product_id"],
                ),
            ],
        ),
    )


def _dv2_logical(
    *, hubs: list[HubDefinition], links: list[LinkDefinition], satellites: list[SatelliteDefinition]
) -> LogicalDraft:
    return LogicalDraft(
        name="fixture",
        technique="data_vault_2",
        dv2=DV2Model(hubs=hubs, links=links, satellites=satellites),
        osi=_osi(),
        conceptual=_conceptual(),
    )


def _dim_logical(*, facts: list[FactTable], dimensions: list[DimensionTable]) -> LogicalDraft:
    return LogicalDraft(
        name="fixture",
        technique="dimensional",
        dimensional=DimensionalModel(facts=facts, dimensions=dimensions),
        osi=_osi(),
        conceptual=_conceptual(),
    )


def test_skeleton_lint_clean_match_emits_no_warnings() -> None:
    pack = _telco_dv2_pack()
    logical = _dv2_logical(
        hubs=[
            HubDefinition(
                entity_name="party",
                hub_table_name="hub_party",
                business_key_columns=["party_id"],
            ),
            HubDefinition(
                entity_name="service",
                hub_table_name="hub_service",
                business_key_columns=["service_id"],
            ),
        ],
        links=[
            LinkDefinition(
                link_name="party_service",
                link_table_name="lnk_party_service",
                hubs_involved=["hub_party", "hub_service"],
                join_keys=[
                    JoinKeyDetail(
                        table1="hub_party",
                        column1="party_id",
                        table2="hub_service",
                        column2="service_id",
                    )
                ],
            )
        ],
        satellites=[
            SatelliteDefinition(
                entity_name="party_profile",
                satellite_table_name="sat_party_profile",
                parent_hub="hub_party",
                attributes=["party_name"],
            )
        ],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    skeleton_warnings = [
        issue for issue in report.issues if issue.field and issue.field.startswith("dv2.")
    ]
    assert skeleton_warnings == []
    assert report.passes_schema is True


def test_skeleton_lint_flags_naming_drift_as_warning() -> None:
    pack = _telco_dv2_pack()
    # ``hub_party`` → ``hub_parties`` (similar enough to drift-warn, not miss).
    logical = _dv2_logical(
        hubs=[
            HubDefinition(
                entity_name="party",
                hub_table_name="hub_parties",
                business_key_columns=["party_id"],
            ),
            HubDefinition(
                entity_name="service",
                hub_table_name="hub_service",
                business_key_columns=["service_id"],
            ),
        ],
        links=[
            LinkDefinition(
                link_name="party_service",
                link_table_name="lnk_party_service",
                hubs_involved=["hub_parties", "hub_service"],
                join_keys=[
                    JoinKeyDetail(
                        table1="hub_parties",
                        column1="party_id",
                        table2="hub_service",
                        column2="service_id",
                    )
                ],
            )
        ],
        satellites=[
            SatelliteDefinition(
                entity_name="party_profile",
                satellite_table_name="sat_party_profile",
                parent_hub="hub_parties",
                attributes=["party_name"],
            )
        ],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    drift_warnings = [
        issue
        for issue in report.issues
        if issue.severity == "warning"
        and issue.field == "dv2.hubs"
        and "hub_parties" in issue.message
        and "hub_party" in issue.message
    ]
    assert len(drift_warnings) == 1
    assert "Naming drift" in drift_warnings[0].message
    # Drift is a warning — schema validation must still pass.
    assert report.passes_schema is True


def test_skeleton_lint_flags_missing_canonical_entity() -> None:
    pack = _telco_dv2_pack()
    # Emit an entirely unrelated hub → neither exact match nor fuzzy hit
    # on the canonical ``hub_party``.
    logical = _dv2_logical(
        hubs=[
            HubDefinition(
                entity_name="widget",
                hub_table_name="hub_widget",
                business_key_columns=["widget_id"],
            )
        ],
        links=[],
        satellites=[],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    missing_hub_warnings = [
        issue
        for issue in report.issues
        if issue.severity == "warning"
        and issue.field == "dv2.hubs"
        and "Missing canonical" in issue.message
        and "hub_party" in issue.message
    ]
    assert len(missing_hub_warnings) == 1

    # Missing link and satellite should also warn.
    missing_link_warnings = [issue for issue in report.issues if issue.field == "dv2.links"]
    missing_sat_warnings = [issue for issue in report.issues if issue.field == "dv2.satellites"]
    assert missing_link_warnings, "expected missing-link warning for skeleton"
    assert missing_sat_warnings, "expected missing-satellite warning for skeleton"


def test_skeleton_lint_dimensional_clean_match_is_quiet() -> None:
    pack = _retail_dim_pack()
    logical = _dim_logical(
        facts=[
            FactTable(
                name="fact_sales_line",
                grain_statement="one row per line",
                measures=[FieldDefinition(name="sales_amount", data_type="NUMBER")],
                foreign_keys=["customer_id", "product_id"],
            )
        ],
        dimensions=[
            DimensionTable(
                name="dim_customer",
                attributes=[FieldDefinition(name="customer_name", data_type="STRING")],
                natural_keys=["customer_id"],
            ),
            DimensionTable(
                name="dim_product",
                attributes=[FieldDefinition(name="product_name", data_type="STRING")],
                natural_keys=["product_id"],
            ),
        ],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    dim_issues = [
        issue for issue in report.issues if issue.field and issue.field.startswith("dimensional.")
    ]
    assert dim_issues == []


def test_skeleton_lint_dimensional_missing_fact_warns() -> None:
    pack = _retail_dim_pack()
    # Emit a model that drops the canonical fact and both dims.
    logical = _dim_logical(
        facts=[FactTable(name="fact_other", grain_statement="unrelated grain")],
        dimensions=[],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    fact_warnings = [
        issue
        for issue in report.issues
        if issue.field == "dimensional.facts"
        and issue.severity == "warning"
        and "fact_sales_line" in issue.message
    ]
    dim_warnings = [
        issue
        for issue in report.issues
        if issue.field == "dimensional.dimensions"
        and issue.severity == "warning"
        and ("dim_customer" in issue.message or "dim_product" in issue.message)
    ]
    assert len(fact_warnings) == 1
    assert len(dim_warnings) == 2


def test_skeleton_lint_noop_when_industry_pack_is_none() -> None:
    # Without a pack, the validator must behave exactly as before.
    logical = _dv2_logical(
        hubs=[
            HubDefinition(
                entity_name="widget",
                hub_table_name="hub_widget",
                business_key_columns=["widget_id"],
            )
        ],
        links=[],
        satellites=[],
    )

    report = FluidContractValidator().validate(logical=logical)

    skeleton_issues = [
        issue for issue in report.issues if issue.field and issue.field.startswith("dv2.")
    ]
    assert skeleton_issues == []


def test_skeleton_lint_noop_when_skeleton_missing_on_pack() -> None:
    # Pack is present but carries no seed_dv2_skeleton — the lint should
    # still be silent (the pack simply has no expectations to enforce).
    pack = IndustryPack(name="telecommunications", version="1.0")
    logical = _dv2_logical(
        hubs=[
            HubDefinition(
                entity_name="widget",
                hub_table_name="hub_widget",
                business_key_columns=["widget_id"],
            )
        ],
        links=[],
        satellites=[],
    )

    report = FluidContractValidator().validate(logical=logical, industry_pack=pack)

    skeleton_issues = [
        issue for issue in report.issues if issue.field and issue.field.startswith("dv2.")
    ]
    assert skeleton_issues == []
