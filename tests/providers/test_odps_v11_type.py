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

"""Bitol ODPS v1.1.0 top-level ``type`` (approved RFC 0029).

The field carries the architectural classification (sourceAligned /
aggregate / consumerAligned), which maps 1:1 onto FLUID's SDP / ADP / CDP.
v1.1.0 is approved but unreleased, so the emit target stays v1.0.0 by
default and v1.1.0 is opt-in; a v1.0.0 document must never carry ``type``
because that schema is ``additionalProperties: false``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.forge.product_types import (
    ODPS_TYPE_TO_PRODUCT_TYPE,
    PRODUCT_TYPE_TO_ODPS_TYPE,
)
from fluid_build.providers.odps_standard.provider import BitolOdpsProvider
from fluid_build.providers.odps_standard.validation import load_schema, validate

pytestmark = pytest.mark.unit


def _contract(**metadata_extra) -> Dict[str, Any]:
    metadata = {"name": "orders", "version": "1.0.0", "status": "active"}
    metadata.update(metadata_extra)
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "silver.orders",
        "name": "orders",
        "metadata": metadata,
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "DB", "schema": "PUBLIC", "table": "ORDERS"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _provider(api_version: str = "v1.0.0") -> BitolOdpsProvider:
    provider = BitolOdpsProvider()
    provider.api_version = api_version
    provider.schema = load_schema(api_version)
    return provider


def _product(provider: BitolOdpsProvider, contract: Dict[str, Any]) -> Dict[str, Any]:
    return provider.render(contract)["product"]


class TestCanonicalMapping:
    def test_registry_carries_the_rfc_0029_trio(self):
        assert PRODUCT_TYPE_TO_ODPS_TYPE == {
            "SDP": "sourceAligned",
            "ADP": "aggregate",
            "CDP": "consumerAligned",
        }
        assert ODPS_TYPE_TO_PRODUCT_TYPE == {
            "sourceAligned": "SDP",
            "aggregate": "ADP",
            "consumerAligned": "CDP",
        }


class TestDefaultEmitTargetIsUnchanged:
    def test_default_stays_v1_0_0(self):
        product = _product(_provider(), _contract(productType="ADP"))
        assert product["apiVersion"] == "v1.0.0"

    def test_v1_0_0_never_carries_type(self):
        """v1.0.0 is additionalProperties:false; emitting type would make
        every default export fail validation against the released schema."""
        product = _product(_provider(), _contract(productType="ADP", layer="Silver"))
        assert "type" not in product

    def test_default_product_validates_against_the_released_schema(self):
        product = _product(_provider(), _contract(productType="ADP"))
        validate(product, load_schema("v1.0.0"))


class TestOptInV11Emission:
    @pytest.mark.parametrize(
        ("product_type", "expected"),
        [("SDP", "sourceAligned"), ("ADP", "aggregate"), ("CDP", "consumerAligned")],
    )
    def test_product_type_maps_onto_type(self, product_type, expected):
        product = _product(_provider("v1.1.0"), _contract(productType=product_type))
        assert product["apiVersion"] == "v1.1.0"
        assert product["type"] == expected

    @pytest.mark.parametrize(
        ("layer", "expected"),
        [("Bronze", "sourceAligned"), ("Silver", "aggregate"), ("Gold", "consumerAligned")],
    )
    def test_layer_only_contract_derives_type_via_the_canonical_mapping(self, layer, expected):
        product = _product(_provider("v1.1.0"), _contract(layer=layer))
        assert product["type"] == expected

    @pytest.mark.parametrize("layer", ["Platinum", "Logical"])
    def test_layers_with_no_data_mesh_analogue_emit_no_type(self, layer):
        product = _product(_provider("v1.1.0"), _contract(layer=layer))
        assert "type" not in product

    def test_unclassified_contract_emits_no_type(self):
        product = _product(_provider("v1.1.0"), _contract())
        assert "type" not in product

    def test_v1_1_0_product_validates_against_the_vendored_schema(self):
        product = _product(_provider("v1.1.0"), _contract(productType="CDP"))
        validate(product, load_schema("v1.1.0"))

    def test_env_var_opt_in(self, monkeypatch):
        monkeypatch.setenv("ODPS_API_VERSION", "v1.1.0")
        provider = BitolOdpsProvider()
        assert provider.api_version == "v1.1.0"
        product = _product(provider, _contract(productType="SDP"))
        assert product["type"] == "sourceAligned"

    def test_unsupported_env_version_falls_back_loudly(self, monkeypatch):
        monkeypatch.setenv("ODPS_API_VERSION", "v9.9.9")
        assert BitolOdpsProvider().api_version == "v1.0.0"


class TestImportAndRoundTrip:
    def _v11_doc(self, odps_type: str = "aggregate") -> Dict[str, Any]:
        return {
            "apiVersion": "v1.1.0",
            "kind": "DataProduct",
            "id": "silver.orders",
            "name": "orders",
            "status": "active",
            "type": odps_type,
        }

    def test_known_type_maps_to_product_type_on_import(self):
        fluid = _provider().import_contract(self._v11_doc("aggregate"))
        assert fluid["metadata"]["productType"] == "ADP"

    def test_custom_org_type_survives_without_forcing_a_classification(self):
        """RFC 0029 allows custom organisation types; those must round-trip
        verbatim without inventing a Data Mesh classification."""
        fluid = _provider().import_contract(self._v11_doc("referenceData"))
        assert "productType" not in fluid["metadata"]
        passthrough = fluid["metadata"]["odps_passthrough"]
        assert passthrough["odps_type"] == "referenceData"

    def test_custom_type_round_trips_verbatim(self):
        provider = _provider("v1.1.0")
        fluid = provider.import_contract(self._v11_doc("referenceData"))
        fluid.setdefault("id", "silver.orders")
        fluid.setdefault("exposes", [])
        product = provider.render(fluid)["product"]
        assert product["type"] == "referenceData"

    def test_edited_classification_beats_the_stale_imported_type(self):
        """Security-review finding: after import stored odps_type=aggregate,
        an operator editing metadata.productType to CDP must re-export
        consumerAligned, not the stale imported value."""
        provider = _provider("v1.1.0")
        fluid = provider.import_contract(self._v11_doc("aggregate"))
        fluid.setdefault("id", "silver.orders")
        fluid.setdefault("exposes", [])
        assert fluid["metadata"]["productType"] == "ADP"
        fluid["metadata"]["productType"] = "CDP"
        product = provider.render(fluid)["product"]
        assert product["type"] == "consumerAligned"

    def test_classification_beats_a_custom_passthrough_when_set(self):
        """A custom org type survives only while the contract stays
        unclassified; once the operator classifies it, the canonical
        mapping is the truth."""
        provider = _provider("v1.1.0")
        fluid = provider.import_contract(self._v11_doc("referenceData"))
        fluid.setdefault("id", "silver.orders")
        fluid.setdefault("exposes", [])
        fluid["metadata"]["productType"] = "SDP"
        product = provider.render(fluid)["product"]
        assert product["type"] == "sourceAligned"

    def test_validate_product_honours_the_documents_own_version(self):
        """A v1.1.0 document is valid even when the provider's emit target
        is v1.0.0: validation keys on the document's declared apiVersion."""
        _provider("v1.0.0").validate_product(self._v11_doc())
