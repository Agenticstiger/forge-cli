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

"""Each exported ODCS is named after its own output port, not the product.

``fluid`` publishes one ODCS v3.1.0 contract per ``exposes[]`` entry. The
``name`` of every one of them used to be the product's name, so a product with
two output ports published two contracts both called "Orders Product". The
``id`` was already per-port (``<productId>.<exposeId>``, or a pinned
``exposes[].contract.id``), so the documents were distinguishable — but only to
a consumer that keys on ``id``. Anything keying files, registry subjects or
display names on ``name`` saw one name for two datasets; DataHub's ODCS
ingestion, for instance, displays a dataset as
``"<contract.name> — <schema.name>"``.

The product name is not dropped: it moves to a ``customProperties`` entry
(``fluidProductName``). Not ODCS ``dataProduct`` — Bitol deprecated that field
in v3.1.0 to keep products in ODPS and contracts in ODCS.

The whole-contract export (``render()`` with no ``expose_id``) is unchanged: it
describes the product, so it keeps the product's name.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping

import pytest

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.metadata import FLUID_PRODUCT_NAME_PROPERTY

pytestmark = pytest.mark.unit


PRODUCT_NAME = "Orders Product"

# Two output ports: one with a ``title``, one without, so a single fixture
# covers the preferred name and the fallback.
PRODUCT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "gold.commerce.orders_v1",
    "name": PRODUCT_NAME,
    "domain": "commerce",
    "metadata": {"layer": "Gold", "owner": {"team": "commerce-platform"}},
    "lifecycle": {"state": "active"},
    "exposes": [
        {
            "exposeId": "daily_orders",
            "title": "Daily Orders",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {
                    "account": "ACME-TEST",
                    "database": "FLUID_TEST",
                    "schema": "GOLD",
                    "table": "DAILY_ORDERS",
                },
            },
            "contract": {"schema": [{"name": "ORDER_DATE", "type": "date", "required": True}]},
        },
        {
            # No ``title`` — the exported name falls back to the exposeId.
            "exposeId": "hourly_orders",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {
                    "account": "ACME-TEST",
                    "database": "FLUID_TEST",
                    "schema": "GOLD",
                    "table": "HOURLY_ORDERS",
                },
            },
            "contract": {"schema": [{"name": "ORDER_HOUR", "type": "timestamp"}]},
        },
    ],
}


@pytest.fixture
def provider() -> OdcsProvider:
    return OdcsProvider()


def _ports(provider: OdcsProvider, contract: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {eid: odcs for eid, odcs in provider.render_all_ports(contract)}


def _custom_properties(odcs: Mapping[str, Any]) -> Dict[str, Any]:
    properties: List[Any] = list(odcs.get("customProperties") or [])
    return {p["property"]: p["value"] for p in properties if isinstance(p, Mapping)}


# ---------------------------------------------------------------------------
# The acceptance case
# ---------------------------------------------------------------------------


def test_two_exposes_export_two_differently_named_contracts(provider: OdcsProvider) -> None:
    """The defect: both documents were called "Orders Product"."""
    ports = _ports(provider, PRODUCT)
    names = [odcs["name"] for odcs in ports.values()]
    assert len(set(names)) == len(names) == 2, names
    assert ports["daily_orders"]["name"] == "Daily Orders"
    assert ports["hourly_orders"]["name"] == "hourly_orders"
    assert PRODUCT_NAME not in names


def test_a_title_less_expose_falls_back_to_its_expose_id(provider: OdcsProvider) -> None:
    """``exposeId`` is always present and unique within the product, and is what
    the document's own ``schema[0].name`` already carries."""
    odcs = provider.render(PRODUCT, expose_id="hourly_orders")
    assert odcs["name"] == "hourly_orders"
    assert odcs["schema"][0]["name"] == "hourly_orders"


@pytest.mark.parametrize("title", ["", "   "])
def test_a_blank_title_falls_back_too(provider: OdcsProvider, title: str) -> None:
    """A title of whitespace is not a name; naming a contract ``"   "`` would be
    worse than the collision it replaced."""
    contract = copy.deepcopy(PRODUCT)
    contract["exposes"][0]["title"] = title
    assert provider.render(contract, expose_id="daily_orders")["name"] == "daily_orders"


# ---------------------------------------------------------------------------
# The product name is not lost, and ``id`` is untouched
# ---------------------------------------------------------------------------


def test_product_name_is_recoverable_from_the_exported_document(
    provider: OdcsProvider,
) -> None:
    """Exactly where: ``customProperties[].property == "fluidProductName"``."""
    for odcs in _ports(provider, PRODUCT).values():
        assert _custom_properties(odcs)[FLUID_PRODUCT_NAME_PROPERTY] == PRODUCT_NAME
    assert FLUID_PRODUCT_NAME_PROPERTY == "fluidProductName"


def test_the_product_name_does_not_ride_in_deprecated_data_product(
    provider: OdcsProvider,
) -> None:
    """ODCS ``dataProduct`` reads "The name of the data product", but Bitol
    deprecated it in v3.1.0 (the official ``open-data-contract-standard``
    3.2.0 schema says "DEPRECATED since v3.1.0") precisely so product-level
    data lives in ODPS. Writing the name there would be a field removal in
    ODCS 4.0 waiting to happen."""
    for odcs in _ports(provider, PRODUCT).values():
        assert "dataProduct" not in odcs


def test_the_name_change_does_not_touch_the_contract_id(provider: OdcsProvider) -> None:
    """``id`` and ``name`` are independent identities: the id stays derived from
    the product id and the exposeId, whatever the port is titled."""
    ports = _ports(provider, PRODUCT)
    assert ports["daily_orders"]["id"] == "gold.commerce.orders_v1.daily_orders"
    assert ports["hourly_orders"]["id"] == "gold.commerce.orders_v1.hourly_orders"

    retitled = copy.deepcopy(PRODUCT)
    retitled["exposes"][0]["title"] = "Something Else Entirely"
    assert provider.render(retitled, expose_id="daily_orders")["id"] == (
        "gold.commerce.orders_v1.daily_orders"
    )


def test_a_pinned_contract_id_still_gets_the_port_name(provider: OdcsProvider) -> None:
    """The other direction of the same independence: an author who pins
    ``exposes[].contract.id`` (fluid-schema 0.7.6) fixes the ``id`` only — the
    ``name`` still follows the port's title."""
    pinned = copy.deepcopy(PRODUCT)
    pinned["fluidVersion"] = "0.7.6"
    pinned["exposes"][0]["contract"]["id"] = "urn:datacontract:commerce:orders-daily"
    odcs = provider.render(pinned, expose_id="daily_orders")
    assert odcs["id"] == "urn:datacontract:commerce:orders-daily"
    assert odcs["name"] == "Daily Orders"


# ---------------------------------------------------------------------------
# Scope of the behaviour change
# ---------------------------------------------------------------------------


def test_the_whole_contract_export_still_carries_the_product_name(
    provider: OdcsProvider,
) -> None:
    """``render()`` with no ``expose_id`` describes the product, not one port, so
    it keeps the product's name — and needs no ``fluidProductName`` to say what
    ``name`` already says."""
    odcs = provider.render(PRODUCT)
    assert odcs["name"] == PRODUCT_NAME
    assert FLUID_PRODUCT_NAME_PROPERTY not in _custom_properties(odcs)


def test_a_single_port_product_is_renamed_too(provider: OdcsProvider) -> None:
    """The behaviour change reaches the common case on purpose. Making the name
    depend on how many siblings a port has would change it the day a second
    port is added — the same collision, one release later — so a single-port
    product's contract is named after its port as well whenever that differs
    from the product name."""
    single = copy.deepcopy(PRODUCT)
    single["exposes"] = [copy.deepcopy(PRODUCT["exposes"][0])]
    ((_eid, odcs),) = provider.render_all_ports(single)
    assert odcs["name"] == "Daily Orders"
    assert _custom_properties(odcs)[FLUID_PRODUCT_NAME_PROPERTY] == PRODUCT_NAME


# ---------------------------------------------------------------------------
# Still valid ODCS, still a round-trip
# ---------------------------------------------------------------------------


def test_every_exported_port_validates_against_the_vendored_schema(
    provider: OdcsProvider,
) -> None:
    """``customProperties`` is the spec's own escape hatch, so the extra entry
    keeps the document valid — worth pinning, because ODCS v3.1.0 rejects
    undeclared top-level fields."""
    for odcs in _ports(provider, PRODUCT).values():
        provider.validate_contract(odcs)  # raises ProviderError on a violation


def test_odcs_round_trip_is_still_a_fixed_point(provider: OdcsProvider) -> None:
    """The renamed document must still survive ODCS → FLUID → ODCS unchanged;
    the per-port name and the new property both have to come back identically."""
    for odcs in _ports(provider, PRODUCT).values():
        result = provider.roundtrip_check(odcs)
        assert result["equal"], (
            f"round-trip diff for {odcs['id']}:\n"
            f"  missing: {result['missing']}\n"
            f"  extra  : {result['extra']}\n"
            f"  changed: {result['changed']}"
        )


def test_fluid_round_trip_keeps_the_product_name_in_the_passthrough(
    provider: OdcsProvider,
) -> None:
    """FLUID → ODCS → FLUID keeps the product name, but *not* at ``name``.

    The imported contract's ``name`` is the port's: a per-port ODCS document
    describes one dataset, and ``normalize.to_document`` reconstructs the FLUID
    root ``name`` from the ODCS ``name`` (``odcs.get("name") or
    metadata.get("name")``, with the ODCS value winning). So the import yields
    a one-port product called "Daily Orders", and the product it was split out
    of survives as provenance in the ODCS round-trip bucket, which is where
    ``customProperties`` is preserved verbatim.

    Restoring it to the root ``name`` instead would need ``normalize.py`` to
    treat a contract carrying ``fluidProductName`` as a port of a larger
    product and rebuild the expose title from the ODCS ``name`` — otherwise the
    re-export would emit the product name again and the ODCS fixed point above
    would break. That is a normalize-side change, deliberately not made here.
    """
    odcs = provider.render(PRODUCT, expose_id="daily_orders")
    imported = provider.import_contract(odcs)

    assert imported["name"] == "Daily Orders"
    assert imported["exposes"][0]["title"] == "Daily Orders"

    bucket = imported["extensions"]["odcs"]["metadata"]["custom_properties"]
    assert {p["property"]: p["value"] for p in bucket}[FLUID_PRODUCT_NAME_PROPERTY] == PRODUCT_NAME

    # ...and re-exporting that port reproduces both names, so nothing drifts.
    again = provider.render(imported, expose_id="daily_orders")
    assert again["name"] == "Daily Orders"
    assert _custom_properties(again)[FLUID_PRODUCT_NAME_PROPERTY] == PRODUCT_NAME
