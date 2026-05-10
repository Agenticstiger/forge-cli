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

"""Pin the 4 cross-technique industry skeletons the plan requires.

The v1.1+ / v1.4 roadmap lists 4 cross-technique skeletons to complement
the 4 "obvious default" skeletons that already shipped in v1.0:

* ``telco/dimensional.yaml``        — TMF SID → Kimball marts
* ``retail/data_vault_2.yaml``      — NRF ARTS → raw-vault foundation
* ``healthcare/dimensional.yaml``   — HL7 FHIR → clinical analytics marts
* ``finance/one_big_table.yaml``    — ISO 20022 → fully-denormalised OBT

These are how off-default-combo users avoid "forge from a blank page":
a telco team that insists on Kimball gets TMF-SID-shaped facts/dims;
a retail team that wants Data Vault gets NRF-ARTS-shaped hubs/links.
Without them, off-default-combo users land the same blank skeleton
as any other industry and lose the correctness lift the industry
pack was built to provide.

This file pins:

1. All 4 files exist at the plan's exact paths.
2. Each one parses cleanly through ``IndustryPackCompiler.compile()`` and
   attaches to the right pack attribute
   (``seed_dv2_skeleton`` vs ``seed_dimensional_skeleton``).
3. ``one_big_table`` is treated as a degenerate dimensional form —
   it lands on ``seed_dimensional_skeleton`` (NOT a new third IR),
   and produces a single wide fact with NO conformed dimensions.
4. The new skeletons carry industry-characteristic primitives — this
   is a smoke check against the "oops, we copy-pasted the wrong
   industry's hubs" class of regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.copilot.industry.compiler import IndustryPackCompiler
from fluid_build.copilot.schemas.data_model import DimensionalModel, DV2Model

_SKELETONS_DIR = (
    Path(__file__).parent.parent.parent / "fluid_build" / "copilot" / "industry" / "skeletons"
)


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_path",
    [
        "telco/dimensional.yaml",
        "retail/data_vault_2.yaml",
        "healthcare/dimensional.yaml",
        "finance/one_big_table.yaml",
    ],
)
def test_cross_technique_skeleton_file_exists(relative_path: str) -> None:
    """The 4 plan-named skeleton files must be present at the exact
    paths the compiler looks them up by. File-not-found would degrade
    the compiler to an empty pack — silently worse accuracy on every
    off-default-combo forge."""
    assert (_SKELETONS_DIR / relative_path).exists(), (
        f"missing cross-technique skeleton: {relative_path}"
    )


# ---------------------------------------------------------------------------
# Compiler wiring — each skeleton attaches to the right pack attribute
# ---------------------------------------------------------------------------


def test_telco_dimensional_attaches_as_dimensional_model() -> None:
    pack = IndustryPackCompiler().compile("telecommunications", technique="dimensional")
    assert pack.seed_dv2_skeleton is None
    assert isinstance(pack.seed_dimensional_skeleton, DimensionalModel)
    skel = pack.seed_dimensional_skeleton
    # TMF SID fingerprint — CDR usage fact is the canonical telco grain.
    fact_names = {f.name for f in skel.facts}
    assert "fact_usage" in fact_names, f"expected fact_usage in {fact_names}"
    # Subscription is TMF-SID's party/product-instance lens on the customer.
    dim_names = {d.name for d in skel.dimensions}
    assert "dim_subscription" in dim_names, f"expected dim_subscription in {dim_names}"


def test_retail_data_vault_2_attaches_as_dv2_model() -> None:
    pack = IndustryPackCompiler().compile("retail", technique="data_vault_2")
    assert pack.seed_dimensional_skeleton is None
    assert isinstance(pack.seed_dv2_skeleton, DV2Model)
    dv2 = pack.seed_dv2_skeleton
    hub_names = {h.hub_table_name for h in dv2.hubs}
    # NRF ARTS retail fingerprint — transaction + product + store trio.
    assert {"hub_customer", "hub_product", "hub_store", "hub_transaction"}.issubset(hub_names)


def test_healthcare_dimensional_attaches_as_dimensional_model() -> None:
    pack = IndustryPackCompiler().compile("healthcare", technique="dimensional")
    assert pack.seed_dv2_skeleton is None
    assert isinstance(pack.seed_dimensional_skeleton, DimensionalModel)
    skel = pack.seed_dimensional_skeleton
    fact_names = {f.name for f in skel.facts}
    # FHIR fingerprint — encounters and observations are the canonical
    # clinical-analytics grains.
    assert "fact_encounter" in fact_names
    assert "fact_observation" in fact_names


def test_finance_one_big_table_attaches_as_dimensional_model() -> None:
    """OBT is a degenerate dimensional form — same container, different
    shape. The compiler must recognise ``one_big_table`` and attach to
    ``seed_dimensional_skeleton`` without introducing a third IR."""
    pack = IndustryPackCompiler().compile("finance", technique="one_big_table")
    assert pack.seed_dv2_skeleton is None
    assert isinstance(pack.seed_dimensional_skeleton, DimensionalModel)
    skel = pack.seed_dimensional_skeleton
    # OBT invariant: exactly one fact table, zero conformed dimensions.
    assert len(skel.facts) == 1, f"OBT must have exactly one wide fact, got {len(skel.facts)}"
    assert skel.conformed_dimensions == [], (
        f"OBT has no conformed dims; got {skel.conformed_dimensions}"
    )
    assert skel.dimensions == [], (
        f"OBT inlines attributes into the fact; got dims {skel.dimensions}"
    )
    # ISO 20022 fingerprint — ``message_id`` / ``end_to_end_id`` /
    # ``instruction_id`` are the payment-message identifiers that
    # appear across pain.*, pacs.* and camt.* message types.
    fact = skel.facts[0]
    dd = set(fact.degenerate_dimensions)
    assert {"message_id", "end_to_end_id", "instruction_id"}.issubset(dd)


# ---------------------------------------------------------------------------
# The 4 already-shipped "obvious default" skeletons still resolve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("industry", "technique", "expected_attr"),
    [
        ("telecommunications", "data_vault_2", "seed_dv2_skeleton"),
        ("retail", "dimensional", "seed_dimensional_skeleton"),
        ("healthcare", "data_vault_2", "seed_dv2_skeleton"),
        ("finance", "dimensional", "seed_dimensional_skeleton"),
    ],
)
def test_obvious_default_skeletons_still_resolve(industry, technique, expected_attr) -> None:
    """Regression guard: adding cross-technique skeletons must not
    break the 4 default skeletons that already shipped in v1.0."""
    pack = IndustryPackCompiler().compile(industry, technique=technique)
    assert getattr(pack, expected_attr) is not None
