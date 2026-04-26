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

"""Tests for :mod:`fluid_build.forge_datamodel.emit.coverage`.

Coverage summaries need to agree with the validator's skeleton-lint on
the "what counts as covered" question — otherwise the user would see a
missing warning and a "✓ present" line side-by-side for the same entity.
The tests lock in the shared contract:

* Exact-name match counts as present.
* Naming drift (``hub_party`` → ``hub_parties``) also counts as present
  — the validator already surfaces the drift warning separately.
* Entirely unrelated names don't count.
* The helper returns ``None`` when the pack has no seed skeleton.
"""

from __future__ import annotations

from fluid_build.copilot.industry.pack import CanonicalModel, IndustryPack
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    HubDefinition,
    LinkDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.coverage import compute_canonical_coverage


def _dv2_pack() -> IndustryPack:
    return IndustryPack(
        name="telecommunications",
        version="1.0",
        canonical_model=CanonicalModel(primary="TMF SID", label="TMF SID"),
        seed_dv2_skeleton=DV2Model(
            hubs=[
                HubDefinition(entity_name="party", hub_table_name="hub_party"),
                HubDefinition(entity_name="service", hub_table_name="hub_service"),
            ],
            links=[
                LinkDefinition(
                    link_name="party_service",
                    link_table_name="lnk_party_service",
                    hubs_involved=["hub_party", "hub_service"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="party_profile",
                    satellite_table_name="sat_party_profile",
                    parent_hub="hub_party",
                )
            ],
        ),
    )


def _osi() -> OSISemanticModel:
    return OSISemanticModel(name="fixture", ai_context=OSIAIContext())


def _conceptual() -> ConceptualDraft:
    return ConceptualDraft(name="fixture")


def test_coverage_all_present_exact_match() -> None:
    pack = _dv2_pack()
    logical = LogicalDraft(
        name="fixture",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(entity_name="party", hub_table_name="hub_party"),
                HubDefinition(entity_name="service", hub_table_name="hub_service"),
            ],
            links=[
                LinkDefinition(
                    link_name="party_service",
                    link_table_name="lnk_party_service",
                    hubs_involved=["hub_party", "hub_service"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="party_profile",
                    satellite_table_name="sat_party_profile",
                    parent_hub="hub_party",
                )
            ],
        ),
        osi=_osi(),
        conceptual=_conceptual(),
    )

    summary = compute_canonical_coverage(logical, pack)
    assert summary is not None
    assert summary.is_clean
    # Render must include the canonical label so users know which
    # reference model was consulted.
    rendered = summary.render()
    assert "TMF SID" in rendered
    assert "telecommunications" in rendered
    # All groups hit 2/2 or 1/1 present.
    groups_by_kind = {g.kind: g for g in summary.groups}
    assert groups_by_kind["hubs"].present == 2
    assert groups_by_kind["links"].present == 1
    assert groups_by_kind["satellites"].present == 1
    assert groups_by_kind["hubs"].missing_names == []


def test_coverage_counts_drifted_name_as_present() -> None:
    pack = _dv2_pack()
    # ``hub_party`` → ``hub_parties`` (drifted, not missing).
    logical = LogicalDraft(
        name="fixture",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(entity_name="party", hub_table_name="hub_parties"),
                HubDefinition(entity_name="service", hub_table_name="hub_service"),
            ],
            links=[
                LinkDefinition(
                    link_name="party_service",
                    link_table_name="lnk_party_service",
                    hubs_involved=["hub_parties", "hub_service"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="party_profile",
                    satellite_table_name="sat_party_profile",
                    parent_hub="hub_parties",
                )
            ],
        ),
        osi=_osi(),
        conceptual=_conceptual(),
    )

    summary = compute_canonical_coverage(logical, pack)
    assert summary is not None
    # Coverage is clean even though the name drifted — the validator
    # surfaces the drift as a separate warning; coverage doesn't
    # double-count it.
    assert summary.is_clean


def test_coverage_reports_missing_canonical_entity() -> None:
    pack = _dv2_pack()
    logical = LogicalDraft(
        name="fixture",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[HubDefinition(entity_name="widget", hub_table_name="hub_widget")],
            links=[],
            satellites=[],
        ),
        osi=_osi(),
        conceptual=_conceptual(),
    )

    summary = compute_canonical_coverage(logical, pack)
    assert summary is not None
    groups_by_kind = {g.kind: g for g in summary.groups}
    # Both canonical hubs must be reported missing — ``hub_widget`` is
    # not close enough to either.
    assert set(groups_by_kind["hubs"].missing_names) == {"hub_party", "hub_service"}
    assert groups_by_kind["links"].missing_names == ["lnk_party_service"]
    assert groups_by_kind["satellites"].missing_names == ["sat_party_profile"]
    assert summary.is_clean is False
    rendered = summary.render()
    assert "missing: hub_party" in rendered or "missing: hub_service" in rendered


def test_coverage_returns_none_without_skeleton() -> None:
    pack = IndustryPack(name="telecommunications", version="1.0")
    logical = LogicalDraft(
        name="fixture",
        technique="data_vault_2",
        dv2=DV2Model(hubs=[HubDefinition(entity_name="x", hub_table_name="hub_x")]),
        osi=_osi(),
        conceptual=_conceptual(),
    )

    assert compute_canonical_coverage(logical, pack) is None


def test_coverage_dimensional_clean_match() -> None:
    pack = IndustryPack(
        name="retail",
        version="1.0",
        canonical_model=CanonicalModel(primary="NRF ARTS", label="NRF ARTS"),
        seed_dimensional_skeleton=DimensionalModel(
            facts=[FactTable(name="fact_sales_line", grain_statement="line")],
            dimensions=[
                DimensionTable(name="dim_customer"),
                DimensionTable(name="dim_product"),
            ],
        ),
    )
    logical = LogicalDraft(
        name="fixture",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[FactTable(name="fact_sales_line", grain_statement="line")],
            dimensions=[
                DimensionTable(name="dim_customer"),
                DimensionTable(name="dim_product"),
            ],
        ),
        osi=_osi(),
        conceptual=_conceptual(),
    )

    summary = compute_canonical_coverage(logical, pack)
    assert summary is not None
    assert summary.is_clean
    # Dimensional reports facts + dimensions, not hubs/links/satellites.
    kinds = {g.kind for g in summary.groups}
    assert kinds == {"facts", "dimensions"}
