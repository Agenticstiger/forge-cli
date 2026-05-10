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

"""Pin the type-aware framework: registry, normalisation, engine selection,
composition rules, and the byte-equivalence promise (I2)."""

from __future__ import annotations

import pytest
import yaml

from fluid_build.forge.product_types import (
    PRODUCT_TYPES,
    ProductType,
    ProductTypeAnswer,
    ProductTypeError,
    get_product_type,
    list_acquisition_engines,
    normalize_metadata_in_place,
    scaffold_files,
    select_acquisition_engine,
    shape_contract,
    validate_composition,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_three_canonical_types():
    codes = [pt.code for pt in PRODUCT_TYPES]
    layers = [pt.layer for pt in PRODUCT_TYPES]
    assert codes == ["SDP", "ADP", "CDP"]
    assert layers == ["Bronze", "Silver", "Gold"]


@pytest.mark.parametrize(
    "needle,expected_code",
    [
        ("SDP", "SDP"),
        ("sdp", "SDP"),
        ("Bronze", "SDP"),
        ("bronze", "SDP"),
        ("source-aligned", "SDP"),
        ("ADP", "ADP"),
        ("Silver", "ADP"),
        ("aggregated", "ADP"),
        ("CDP", "CDP"),
        ("Gold", "CDP"),
        ("consumption-aligned", "CDP"),
        ("marts", "CDP"),
    ],
)
def test_get_product_type_accepts_codes_layers_and_aliases(needle, expected_code):
    pt = get_product_type(needle)
    assert pt is not None
    assert pt.code == expected_code


def test_get_product_type_returns_none_for_unknown():
    assert get_product_type("Platinum") is None  # Platinum has no Data Mesh code
    assert get_product_type("nonsense") is None
    assert get_product_type("") is None


# ---------------------------------------------------------------------------
# normalize_metadata_in_place — the equivalence axiom
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata,expected_layer,expected_pt",
    [
        ({"layer": "Bronze"}, "Bronze", "SDP"),
        ({"layer": "Silver"}, "Silver", "ADP"),
        ({"layer": "Gold"}, "Gold", "CDP"),
        ({"productType": "SDP"}, "Bronze", "SDP"),
        ({"productType": "ADP"}, "Silver", "ADP"),
        ({"productType": "CDP"}, "Gold", "CDP"),
        ({"layer": "Bronze", "productType": "SDP"}, "Bronze", "SDP"),
        ({"layer": "Gold", "productType": "CDP"}, "Gold", "CDP"),
    ],
)
def test_normalize_fills_missing_twin(metadata, expected_layer, expected_pt):
    normalize_metadata_in_place(metadata)
    assert metadata["layer"] == expected_layer
    assert metadata["productType"] == expected_pt


def test_normalize_passes_through_platinum():
    md = {"layer": "Platinum"}
    normalize_metadata_in_place(md)
    assert md["layer"] == "Platinum"
    assert "productType" not in md  # no Data Mesh twin


def test_normalize_passes_through_neither_set():
    md = {"owner": {"team": "data"}}
    normalize_metadata_in_place(md)
    assert "layer" not in md
    assert "productType" not in md


def test_normalize_raises_on_disagreement():
    with pytest.raises(ProductTypeError) as excinfo:
        normalize_metadata_in_place({"layer": "Bronze", "productType": "ADP"})
    assert "Bronze" in str(excinfo.value)
    assert "ADP" in str(excinfo.value)


def test_normalize_raises_on_invalid_layer():
    with pytest.raises(ProductTypeError):
        normalize_metadata_in_place({"layer": "Copper"})


def test_normalize_raises_on_invalid_product_type():
    with pytest.raises(ProductTypeError):
        normalize_metadata_in_place({"productType": "NDP"})


def test_normalize_raises_on_platinum_with_product_type():
    # Platinum has no productType analogue; user must omit productType.
    with pytest.raises(ProductTypeError):
        normalize_metadata_in_place({"layer": "Platinum", "productType": "CDP"})


# ---------------------------------------------------------------------------
# Acquisition engine selection
# ---------------------------------------------------------------------------


def test_select_acquisition_engine_defaults_to_duckdb_for_unknown():
    engine = select_acquisition_engine()
    assert engine.name == "duckdb"


def test_select_acquisition_engine_uses_kind():
    assert select_acquisition_engine(source_kind="parquet").name == "duckdb"
    assert select_acquisition_engine(source_kind="salesforce").name == "airbyte"
    assert select_acquisition_engine(source_kind="rest").name == "dlt"
    assert select_acquisition_engine(source_kind="postgres-cdc").name == "debezium"


def test_select_acquisition_engine_uses_uri_scheme():
    assert select_acquisition_engine(source_uri="kafka://broker:9092/topic").name == "kafka_connect"
    assert select_acquisition_engine(source_uri="https://api.example.com/v1").name == "dlt"


def test_select_acquisition_engine_capability_filter():
    engines = list_acquisition_engines(capabilities=frozenset({"streaming", "cdc"}))
    assert {e.name for e in engines} == {"debezium"}


# ---------------------------------------------------------------------------
# Composition rules
# ---------------------------------------------------------------------------


def test_composition_sdp_rejects_all_upstreams():
    violations = validate_composition(target_type="SDP", upstream_types={"x.y.z": "SDP"})
    assert len(violations) == 1
    assert violations[0].target_type == "SDP"


def test_composition_adp_accepts_sdp_and_adp():
    assert (
        validate_composition(
            target_type="ADP",
            upstream_types={"a.b.c": "SDP", "a.b.d": "ADP"},
        )
        == []
    )


def test_composition_adp_rejects_cdp_upstream():
    violations = validate_composition(target_type="ADP", upstream_types={"a.b.c": "CDP"})
    assert len(violations) == 1
    assert violations[0].upstream_id == "a.b.c"
    assert violations[0].upstream_type == "CDP"


def test_composition_cdp_accepts_sdp_and_adp():
    assert (
        validate_composition(
            target_type="CDP",
            upstream_types={"a.b.c": "SDP", "a.b.d": "ADP"},
        )
        == []
    )


def test_composition_unknown_upstream_type_flagged():
    violations = validate_composition(target_type="ADP", upstream_types={"a.b.c": None})
    assert len(violations) == 1
    assert "unknown" in violations[0].reason.lower()


# ---------------------------------------------------------------------------
# shape_contract — the I2 byte-equivalence promise
# ---------------------------------------------------------------------------


def _sample_answer(**overrides):
    base = dict(
        product_type="SDP",
        name="pricing",
        domain="commerce",
        owner_team="data-platform",
        owner_email="dp@example.com",
        source_kind="parquet",
    )
    base.update(overrides)
    return ProductTypeAnswer(**base)


def test_shape_contract_is_pure_for_same_answer():
    a = _sample_answer()
    c1 = shape_contract(a)
    c2 = shape_contract(a)
    assert c1 == c2
    # Byte-equivalence after YAML serialisation:
    assert yaml.safe_dump(c1, sort_keys=False) == yaml.safe_dump(c2, sort_keys=False)


def test_shape_contract_canonicalises_metadata_pair():
    contract = shape_contract(_sample_answer(product_type="bronze"))
    assert contract["metadata"]["layer"] == "Bronze"
    assert contract["metadata"]["productType"] == "SDP"


def test_shape_contract_sdp_uses_acquisition_pattern():
    contract = shape_contract(_sample_answer(product_type="SDP"))
    build = contract["builds"][0]
    assert build["pattern"] == "acquisition"
    assert build["engine"] in {"duckdb", "dlt", "airbyte", "meltano", "kafka_connect", "debezium"}


def test_shape_contract_adp_uses_transformation_pattern():
    contract = shape_contract(
        _sample_answer(
            product_type="ADP",
            upstream_products=(("bronze.commerce.orders_v1", "orders_output"),),
        )
    )
    build = contract["builds"][0]
    assert build["pattern"] == "embedded-logic"
    assert build["engine"] == "dbt"
    assert contract["metadata"]["layer"] == "Silver"
    assert contract["metadata"]["productType"] == "ADP"
    assert len(contract["consumes"]) == 1
    # Schema requires productId + exposeId, not id + ref:
    assert contract["consumes"][0]["productId"] == "bronze.commerce.orders_v1"
    assert contract["consumes"][0]["exposeId"] == "orders_output"


def test_shape_contract_cdp_uses_transformation_pattern():
    contract = shape_contract(_sample_answer(product_type="CDP"))
    assert contract["metadata"]["layer"] == "Gold"
    assert contract["metadata"]["productType"] == "CDP"


def test_shape_contract_dlt_path_emits_dlt_engine():
    contract = shape_contract(
        _sample_answer(used_dlt_generation=True, dlt_source_module="./sources/pricing.py")
    )
    build = contract["builds"][0]
    assert build["engine"] == "dlt"
    # dlt-specific source_module rides on connection.module (schema's
    # acquisitionSource shape); legacy ``properties.dlt.source_module``
    # is no longer accepted by the validator.
    assert build["properties"]["source"]["connection"]["module"] == "./sources/pricing.py"


def test_shape_contract_byte_equivalence_across_aliases():
    """Different alias inputs that resolve to the same product type must
    produce byte-identical contracts. Pure I2 invariant."""
    a1 = _sample_answer(product_type="SDP")
    a2 = _sample_answer(product_type="bronze")
    a3 = _sample_answer(product_type="source-aligned")
    c1 = yaml.safe_dump(shape_contract(a1), sort_keys=False)
    c2 = yaml.safe_dump(shape_contract(a2), sort_keys=False)
    c3 = yaml.safe_dump(shape_contract(a3), sort_keys=False)
    assert c1 == c2 == c3


def test_shape_contract_includes_sovereignty_when_set():
    contract = shape_contract(_sample_answer(jurisdiction="EU", regulatory_framework=("GDPR",)))
    assert contract["sovereignty"]["jurisdiction"] == "EU"
    assert contract["sovereignty"]["regulatoryFramework"] == ["GDPR"]


def test_shape_contract_unknown_product_type_raises():
    with pytest.raises(ProductTypeError):
        shape_contract(_sample_answer(product_type="NDP"))


# ---------------------------------------------------------------------------
# scaffold_files
# ---------------------------------------------------------------------------


def test_scaffold_files_emits_contract_yaml():
    contract = shape_contract(_sample_answer())
    files = scaffold_files(contract, "/tmp/x")
    assert "contract.fluid.yaml" in files
    parsed = yaml.safe_load(files["contract.fluid.yaml"])
    assert parsed["metadata"]["productType"] == "SDP"


def test_scaffold_files_dlt_path_emits_source_module():
    contract = shape_contract(
        _sample_answer(used_dlt_generation=True, dlt_source_module="./sources/pricing.py")
    )
    files = scaffold_files(contract, "/tmp/x")
    assert any(p.endswith("sources/pricing.py") for p in files.keys())


# ---------------------------------------------------------------------------
# End-to-end: shape_contract output validates under the schema validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product_type", ["SDP", "ADP", "CDP"])
def test_shape_contract_passes_schema_validator(product_type):
    """Every shape_contract output MUST validate cleanly under the
    JSON-schema validator (FluidSchemaManager). Catches issues like a
    wrong ``trigger.type`` that pure-shape unit tests can't see — and
    the productType↔layer cross-check the equivalence axiom enforces.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    answer = _sample_answer(
        product_type=product_type,
        upstream_products=(
            (("bronze.commerce.orders_v1", "orders_output"),) if product_type != "SDP" else ()
        ),
    )
    contract = shape_contract(answer)
    sm = FluidSchemaManager()
    result = sm.validate_contract(contract)
    assert result.is_valid, (
        f"{product_type} contract failed schema validation: {', '.join(result.errors)}"
    )
