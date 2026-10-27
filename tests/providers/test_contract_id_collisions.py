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

"""One contract id per output port, from every site that mints one.

``exposes[].contract.id`` is used verbatim as the per-port ODCS ``id`` and as
the ODPS output port's ``contractId``. The feature had one hole with four
symptoms, and this file pins all four shut:

* **Pinned against derived.** The uniqueness guard compared pinned ids against
  pinned ids only, so a pin that collided with the ``<product
  id>.<exposeId>`` an un-pinned sibling derives validated with a tick — and
  then ``fluid odps export`` wrote one ODCS file for two output ports, keyed by
  the shared id. The losing port's schema was absent from the bundle entirely,
  so a consumer resolving it got the surviving port's schema.
* **One resolution, five readers.** The imported id lives in root
  ``extensions.odps`` on disk and only ``BitolOdpsProvider.render`` pushes it
  down onto the expose, so the five entry points that render a per-expose ODCS
  disagreed about a port's id depending on which one you reached it through.
* **Data Mesh Manager re-derived.** The publish flow overwrote a pinned
  ``contractId`` on the wire and PUT the companion ODCS body under a url the
  published product did not reference.
* **The guard protected a stage the exporters skip.** Neither ``fluid odps
  export`` nor ``fluid generate artifacts`` runs ``fluid validate``, so the
  exporter has to refuse a colliding contract itself rather than silently drop
  a port.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.api.catalog_publication import _render_odcs_yaml
from fluid_build.cli.validate import _run_contract_rules
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.odcs.provider import OdcsProvider
from fluid_build.providers.odps_standard.provider import BitolOdpsProvider
from fluid_build.schema_manager import FluidSchemaManager, ValidationResult

pytestmark = pytest.mark.unit

PRODUCT_ID = "shop.catalog"
UUID_CONTRACT_ID = "3f2a9c1e-5b47-4d0a-9f21-7c8e6b5a4d33"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _expose(expose_id: str, table: str, column: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "SHOP", "schema": "CDP", "table": table},
        },
        "contract": {
            "schema": [
                {"name": "ITEM_ID", "type": "integer", "required": True},
                {"name": column, "type": "string"},
            ]
        },
    }


def _two_port_contract() -> Dict[str, Any]:
    """Two output ports, neither pinning a contract id."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": PRODUCT_ID,
        "name": "Catalog",
        "description": "Catalog, one row per item.",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "catalog-platform", "email": "catalog-platform@example.com"},
        },
        "exposes": [
            _expose("items", "ITEMS", "ITEM_NAME"),
            _expose("prices", "PRICES", "CURRENCY"),
        ],
    }


def _pinned_to_a_siblings_derived_id() -> Dict[str, Any]:
    """``prices`` pins the exact id the un-pinned ``items`` derives."""
    contract = _two_port_contract()
    contract["exposes"][1]["contract"]["id"] = f"{PRODUCT_ID}.items"
    return contract


def _rule_errors(contract: Dict[str, Any]) -> List[str]:
    """Run the post-JSON-Schema contract rules the way ``fluid validate`` does."""
    result = ValidationResult(is_valid=True)
    args = SimpleNamespace(verbose=False, quiet=True, strict=False, offline=True)
    _run_contract_rules(contract, None, result, args, logging.getLogger("test"))
    return [str(e) for e in result.errors]


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


@pytest.fixture
def exporter() -> BitolOdpsProvider:
    provider = BitolOdpsProvider()
    # The collision has to be refused on its own, not incidentally by the ODPS
    # or ODCS JSON-Schema check that runs after the per-port contracts are
    # collected.
    provider.strict_validation = False
    return provider


@pytest.fixture
def imported_with_uuid_contract_id(tmp_path) -> Dict[str, Any]:
    """A real ODPS import of a port whose ``contractId`` is a UUID.

    Built by running the importer rather than hand-writing the result: the
    imported id ends up in root ``extensions.odps`` only because the import
    declared 0.7.5, where ``exposeContract`` is closed and has no ``id`` field
    — which is precisely the shape the four non-rehydrating entry points used
    to re-derive over.
    """
    (tmp_path / "product.odps.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1.0.0",
                "kind": "DataProduct",
                "id": PRODUCT_ID,
                "name": "Catalog",
                "status": "active",
                "owner": "catalog-platform",
                "outputPorts": [
                    {
                        "name": "items",
                        "version": "1",
                        "contractId": UUID_CONTRACT_ID,
                        "type": "snowflake",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / f"{UUID_CONTRACT_ID}.odcs.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v3.1.0",
                "kind": "DataContract",
                "id": UUID_CONTRACT_ID,
                "version": "1.0.0",
                "status": "active",
                "name": "Items",
                "schema": [
                    {
                        "name": "ITEMS",
                        "logicalType": "object",
                        "physicalType": "snowflake_table",
                        "properties": [{"name": "ITEM_ID", "logicalType": "integer"}],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    imported = BitolOdpsProvider().import_directory(tmp_path)
    # Guard the fixture's own premise: if a later schema bump lets the import
    # write ``contract.id`` instead, the entry-point test below would pass for
    # the wrong reason.
    assert "id" not in (imported["exposes"][0].get("contract") or {})
    assert (
        imported["extensions"]["odps"]["exposes"]["items"]["expose"]["contract_id"]
        == UUID_CONTRACT_ID
    )
    return imported


def _dmm_provider() -> DataMeshManagerProvider:
    """A provider wired for payload inspection — no HTTP, no credentials."""
    provider = DataMeshManagerProvider.__new__(DataMeshManagerProvider)
    provider._log = logging.getLogger("test")
    provider.api_url = "https://dmm.example"
    return provider


# ---------------------------------------------------------------------------
# Finding 1 — a pin colliding with a sibling's DERIVED id
# ---------------------------------------------------------------------------


class TestValidateRejectsPinnedAgainstDerived:
    def test_pin_colliding_with_an_unpinned_siblings_derived_id_is_rejected(self, manager):
        contract = _pinned_to_a_siblings_derived_id()
        # Schema-valid: ``uniqueItems`` compares whole array items, and the
        # derived id is not in the document at all.
        result = manager.validate_contract(contract, schema_version="0.7.6", offline_only=True)
        assert result.is_valid, [getattr(e, "message", str(e)) for e in result.errors]

        errors = _rule_errors(contract)
        assert len(errors) == 1, errors
        message = errors[0]
        assert f"{PRODUCT_ID}.items" in message
        # Both exposes are named, so the author knows which two collided.
        assert "'items'" in message
        assert "'prices'" in message
        # And the advice fits: ``items`` has no contract.id to change, so the
        # pinned-vs-pinned wording would send the author to a field that is
        # not there.
        assert "give each expose its own contract.id" not in message
        assert "pins no contract.id" in message

    def test_fluid_validate_offline_reaches_the_check(self, tmp_path, capsys):
        from fluid_build.cli import build_parser
        from fluid_build.cli.validate import run

        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(
            yaml.safe_dump(_pinned_to_a_siblings_derived_id(), sort_keys=False),
            encoding="utf-8",
        )
        args = build_parser().parse_args(["validate", str(contract_path), "--offline"])
        rc = run(args, logging.getLogger("test"))
        captured = capsys.readouterr()

        assert rc == 1
        assert f"{PRODUCT_ID}.items" in captured.out + captured.err

    def test_two_pinned_exposes_sharing_an_id_are_still_rejected(self, manager):
        """The pinned-vs-pinned case the guard already caught, unregressed."""
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:catalog:1"
        contract["exposes"][1]["contract"]["id"] = "urn:acme:contract:catalog:1"
        result = manager.validate_contract(contract, schema_version="0.7.6", offline_only=True)
        assert result.is_valid

        assert _rule_errors(contract) == [
            "contract id 'urn:acme:contract:catalog:1' is used by exposes 'items' and 'prices'"
            " — two output ports under one contract id publish two different schemas under a"
            " single identity, so a consumer resolving it gets one of the two arbitrarily and"
            " the other port's schema is lost; give each expose its own contract.id"
        ]

    def test_distinct_pins_and_no_pins_at_all_are_accepted(self):
        contract = _two_port_contract()
        assert _rule_errors(contract) == []
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        contract["exposes"][1]["contract"]["id"] = "urn:acme:contract:prices:1"
        assert _rule_errors(contract) == []

    def test_a_pin_matching_a_PINNED_siblings_derived_id_is_accepted(self):
        """A pinned expose's derived id is never published, so nothing collides.

        ``items`` pins a urn, which is what its port is exported under;
        ``shop.catalog.items`` is then a string no contract answers to, and
        ``prices`` is free to take it. Seeding derived ids for pinned exposes
        too would reject this contract for a collision that cannot happen.
        """
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        contract["exposes"][1]["contract"]["id"] = f"{PRODUCT_ID}.items"
        assert _rule_errors(contract) == []

    def test_the_exported_bundle_for_that_contract_really_has_both_schemas(self, exporter):
        """The acceptance above is only right if the export is in fact lossless."""
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        contract["exposes"][1]["contract"]["id"] = f"{PRODUCT_ID}.items"
        rendered = exporter.render(contract)
        assert sorted(rendered["contracts"]) == [
            PRODUCT_ID + ".items",
            "urn:acme:contract:items:1",
        ]


# ---------------------------------------------------------------------------
# Finding 4 — the exporter refuses rather than overwrites
# ---------------------------------------------------------------------------


class TestExporterRefusesACollision:
    def test_render_raises_instead_of_dropping_a_port(self, exporter):
        contract = _pinned_to_a_siblings_derived_id()
        with pytest.raises(ProviderError) as excinfo:
            exporter.render(contract)
        message = str(excinfo.value)
        # Both ports and the shared id, so the operator can act on it without
        # re-deriving the collision by hand.
        assert "items" in message
        assert "prices" in message
        assert f"{PRODUCT_ID}.items" in message

    def test_a_valid_pinned_contract_still_yields_one_odcs_per_output_port(self, exporter):
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        rendered = exporter.render(contract)
        contracts = rendered["contracts"]
        output_ports = rendered["product"]["outputPorts"]
        assert len(contracts) == len(output_ports)
        assert sorted(contracts) == sorted(p["contractId"] for p in output_ports)
        # And the one that was dropped before carries its own schema, not the
        # other port's.
        columns = [
            prop["name"]
            for obj in contracts["urn:acme:contract:items:1"]["schema"]
            for prop in obj.get("properties", [])
        ]
        assert "ITEM_NAME" in columns
        assert "CURRENCY" not in columns

    def test_an_unpinned_contract_still_yields_one_odcs_per_output_port(self, exporter):
        rendered = exporter.render(_two_port_contract())
        assert len(rendered["contracts"]) == len(rendered["product"]["outputPorts"])


# ---------------------------------------------------------------------------
# Finding 2 — one resolution, read the same from every entry point
# ---------------------------------------------------------------------------


def _via_render_all_ports(contract: Dict[str, Any]) -> str:
    """``fluid generate artifacts --emit odcs`` (odcs/provider.py)."""
    return dict(OdcsProvider().render_all_ports(contract))["items"]["id"]


def _via_dmm_preview(contract: Dict[str, Any]) -> str:
    """``fluid publish --dry-run`` to Data Mesh Manager (_publish_flow.py)."""
    previews = _dmm_provider()._preview_odcs_per_expose(contract, PRODUCT_ID)
    return previews[0]["url"].rsplit("/", 1)[-1]


def _via_dmm_publish(contract: Dict[str, Any]) -> str:
    """The live Data Mesh Manager publish (_publish_flow.py)."""
    provider = _dmm_provider()
    provider._request = lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {})
    return provider._publish_odcs_per_expose(contract, PRODUCT_ID)[0]["contract_id"]


def _via_copilot_roundtrip(contract: Dict[str, Any]) -> str:
    """The forge copilot's per-expose round-trip check (forge_copilot_runtime.py)."""
    return OdcsProvider().render(contract, expose_id="items")["id"]


def _via_catalog_publication(contract: Dict[str, Any]) -> str:
    """The canonical catalog payload builder (api/catalog_publication.py)."""
    return yaml.safe_load(_render_odcs_yaml(contract, "items"))["id"]


ODCS_ENTRY_POINTS = {
    "render_all_ports": _via_render_all_ports,
    "dmm_preview": _via_dmm_preview,
    "dmm_publish": _via_dmm_publish,
    "copilot_roundtrip": _via_copilot_roundtrip,
    "catalog_publication": _via_catalog_publication,
}


class TestEveryEntryPointResolvesTheSameId:
    """The invariant is that the five agree, so it is tested as one set.

    Only ``BitolOdpsProvider.render`` rehydrates ``extensions.odps`` onto the
    expose. When the resolution read the expose alone, that made it an export
    whose answer depended on the caller: the ODPS exporter honoured the
    imported UUID while all five of these re-derived ``shop.catalog.items``.
    """

    @pytest.mark.parametrize("name", sorted(ODCS_ENTRY_POINTS))
    def test_an_imported_uuid_survives_every_entry_point(
        self, name, imported_with_uuid_contract_id
    ):
        resolve = ODCS_ENTRY_POINTS[name]
        assert resolve(copy.deepcopy(imported_with_uuid_contract_id)) == UUID_CONTRACT_ID

    def test_the_odps_exporter_agrees_with_them(self, exporter, imported_with_uuid_contract_id):
        rendered = exporter.render(copy.deepcopy(imported_with_uuid_contract_id))
        assert rendered["product"]["outputPorts"][0]["contractId"] == UUID_CONTRACT_ID
        assert sorted(rendered["contracts"]) == [UUID_CONTRACT_ID]

    @pytest.mark.parametrize("name", sorted(ODCS_ENTRY_POINTS))
    def test_a_pinned_id_survives_every_entry_point(self, name):
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        assert ODCS_ENTRY_POINTS[name](contract) == "urn:acme:contract:items:1"

    @pytest.mark.parametrize("name", sorted(ODCS_ENTRY_POINTS))
    def test_an_unpinned_port_still_derives_at_every_entry_point(self, name):
        assert ODCS_ENTRY_POINTS[name](_two_port_contract()) == f"{PRODUCT_ID}.items"


# ---------------------------------------------------------------------------
# Finding 3 — Data Mesh Manager publishes under the id the ODPS file references
# ---------------------------------------------------------------------------


class TestDataMeshManagerMatchesTheOdpsFile:
    def test_the_odps_port_and_the_dmm_publish_url_are_one_string(self):
        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"

        exporter = BitolOdpsProvider()
        exporter.strict_validation = False
        product = exporter.render(contract)["product"]
        odps_ids = {p["name"]: p["contractId"] for p in product["outputPorts"]}

        previews = _dmm_provider()._preview_odcs_per_expose(contract, PRODUCT_ID)
        dmm_ids = {p["url"].rsplit("/", 1)[-1] for p in previews}

        assert odps_ids == {"items": "urn:acme:contract:items:1", "prices": f"{PRODUCT_ID}.prices"}
        assert dmm_ids == set(odps_ids.values())
        # And the body's own ``id`` matches the url it is PUT to, which is what
        # makes the published product's ``contractId`` resolvable.
        for preview in previews:
            assert preview["payload"]["id"] == preview["url"].rsplit("/", 1)[-1]

    # Both payload shapes carry the reference, under different key names, and
    # both point at the same companion ODCS contract — so both have to name
    # the id it is published under.
    @pytest.mark.parametrize(
        ("specification", "reference_key"),
        [("odps", "contractId"), ("dps", "dataContractId")],
    )
    def test_the_published_output_port_references_the_pinned_id(self, specification, reference_key):
        """``_publish_one`` must not overwrite the pin on the wire."""
        from unittest.mock import MagicMock, patch

        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"

        provider = DataMeshManagerProvider(api_key="fake", api_url="https://dmm.example")
        with (
            patch.object(DataMeshManagerProvider, "_ensure_team"),
            patch.object(DataMeshManagerProvider, "_request") as request,
        ):
            request.return_value = MagicMock(status_code=200)
            provider._publish_one(
                contract,
                publish_contract=True,
                contract_format="odcs",
                data_product_specification=specification,
            )

        body = request.call_args_list[0][1]["json_body"]
        by_name = {p.get("name") or p.get("id"): p for p in body.get("outputPorts") or []}
        assert by_name["items"][reference_key] == "urn:acme:contract:items:1"
        assert by_name["prices"][reference_key] == f"{PRODUCT_ID}.prices"

        # And the ODCS bodies really were PUT under those ids, so the
        # references resolve.
        put_paths = {
            call[0][1]
            for call in request.call_args_list
            if len(call[0]) > 1 and str(call[0][1]).startswith("/api/datacontracts/")
        }
        assert "/api/datacontracts/urn:acme:contract:items:1" in put_paths
        assert f"/api/datacontracts/{PRODUCT_ID}.prices" in put_paths


# ---------------------------------------------------------------------------
# Finding 5 — the check is the contract's, not one exporter's
# ---------------------------------------------------------------------------


class TestTheSharedCollisionCheck:
    """``util.contract.contract_id_collisions`` is the one implementation.

    It lived inside ``BitolOdpsProvider.render``, so only the ODPS doors
    refused a colliding contract. ``fluid generate standard --format odcs``,
    ``fluid generate artifacts --emit odcs`` and the Data Mesh Manager publish
    each wrote two ODCS documents carrying one ``id`` instead.
    ``tests/cli/test_collision_guard_every_door.py`` pins the doors; this pins
    what they all call.
    """

    def test_a_pin_colliding_with_an_unpinned_siblings_derived_id(self):
        from fluid_build.util.contract import contract_id_collisions

        (message,) = contract_id_collisions(_pinned_to_a_siblings_derived_id())
        assert f"{PRODUCT_ID}.items" in message
        assert "'items'" in message
        assert "'prices'" in message
        # The sibling has no contract.id to change, so the message names the
        # two moves that exist rather than the one that does not.
        assert "change the contract.id on 'prices'" in message
        assert "rename the expose 'items'" in message
        assert "give each expose its own contract.id" not in message

    def test_two_pinned_exposes_sharing_an_id(self):
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:catalog:1"
        contract["exposes"][1]["contract"]["id"] = "urn:acme:contract:catalog:1"
        (message,) = contract_id_collisions(contract)
        assert "'urn:acme:contract:catalog:1'" in message
        assert "give each expose its own contract.id" in message

    def test_a_pin_matching_a_PINNED_siblings_derived_id_is_accepted(self):
        """The false positive, at the helper. ``items`` is published under its
        urn, so ``shop.catalog.items`` is an id no contract answers to."""
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        contract["exposes"][1]["contract"]["id"] = f"{PRODUCT_ID}.items"
        assert contract_id_collisions(contract) == []

    def test_distinct_pins_and_no_pins_at_all_are_accepted(self):
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        assert contract_id_collisions(contract) == []
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:items:1"
        contract["exposes"][1]["contract"]["id"] = "urn:acme:contract:prices:1"
        assert contract_id_collisions(contract) == []

    def test_an_imported_id_is_resolved_the_same_way_as_a_pin(self, imported_with_uuid_contract_id):
        """An import leaves the id in root ``extensions.odps``, and the check
        has to read it from there or it compares against an id nothing
        publishes."""
        from fluid_build.util.contract import contract_id_collisions

        contract = copy.deepcopy(imported_with_uuid_contract_id)
        assert contract_id_collisions(contract) == []
        contract["exposes"].append(_expose("extra", "EXTRA", "EXTRA_NAME"))
        contract["exposes"][-1]["contract"]["id"] = UUID_CONTRACT_ID
        (message,) = contract_id_collisions(contract)
        assert UUID_CONTRACT_ID in message
        assert "'items'" in message
        assert "'extra'" in message

    def test_product_id_scopes_the_derived_half(self):
        """Data Mesh Manager derives from the id of the product it is writing,
        so the check has to derive from the same one or it misses the
        collision its own urls would have."""
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"][1]["contract"]["id"] = "other.product.items"
        assert contract_id_collisions(contract) == []
        (message,) = contract_id_collisions(contract, product_id="other.product")
        assert "'other.product.items'" in message

    def test_only_the_colliding_pair_is_reported(self):
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"].append(_expose("stock", "STOCK", "ON_HAND"))
        contract["exposes"][2]["contract"]["id"] = f"{PRODUCT_ID}.items"
        (message,) = contract_id_collisions(contract)
        assert "'prices'" not in message

    def test_the_single_collision_form_returns_the_first_or_none(self):
        from fluid_build.util.contract import contract_id_collision, contract_id_collisions

        collider = _pinned_to_a_siblings_derived_id()
        assert contract_id_collision(collider) == contract_id_collisions(collider)[0]
        assert contract_id_collision(_two_port_contract()) is None

    @pytest.mark.parametrize("exposes", [None, "not-a-list", [], ["not-a-mapping"]])
    def test_shapes_the_schema_would_have_rejected_are_skipped(self, exposes):
        """The check runs on exporter paths that never gated on ``fluid
        validate``, so a malformed document must not raise out of it."""
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"] = exposes
        assert contract_id_collisions(contract) == []

    def test_one_name_used_twice_is_left_to_the_duplicate_name_guard(self, exporter):
        """Two exposes answering to one name derive one id, and reporting that
        as a contract-id collision cost the author the message that actually
        describes it."""
        from fluid_build.util.contract import contract_id_collisions

        contract = _two_port_contract()
        contract["exposes"][1]["exposeId"] = "items"
        assert contract_id_collisions(contract) == []
        with pytest.raises(ProviderError, match="Duplicate output port name"):
            exporter.render(contract)
