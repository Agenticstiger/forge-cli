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

"""Three export defects: the description object, input-port lineage, and ids.

1. The ODPS exporter only ever wrapped a *string* ``description``. PR #585
   taught the ODCS exporter to pass the structured object through; this one
   matched no branch for an object and wrote no ``description`` at all, so an
   ODCS export of a contract kept purpose/usage/limitations while an ODPS
   export of the SAME contract kept at most purpose.

2. ``consumes[]`` became an ``inputPorts[]`` entry whose ``contractId`` was the
   upstream PRODUCT id — a contract that exists nowhere, since forge names that
   upstream port's ODCS ``<productId>.<exposeId>`` — and the consume's
   ``versionConstraint`` was read by the canonical-port helper and then dropped,
   so the export claimed an exact-version pin the contract never declared.

3. A contract id could not be pinned: both the ODCS ``id`` and the ODPS output
   port's ``contractId`` were derived as ``<FLUID id>.<exposeId>`` every time,
   so renaming the product or the expose renamed the contract under every
   consumer referencing it, a catalog with ids of its own could not be served,
   and a port imported with such an id re-exported with the derived one.

Every assertion here compares strings the exporters actually produced — the
``<productId>.<exposeId>`` convention is spelled once, by the code under test,
and read back out of the other export rather than restated.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli.validate import run_on_contract_dict
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.mappers.ports import VERSION_CONSTRAINT_PROPERTY
from fluid_build.providers.odps_standard.validation import (
    SUPPORTED_API_VERSIONS,
    load_schema,
    validate,
)

PROSE = "Gold customer mart built from the commerce orders product."
DESCRIPTION_OBJECT = {
    "purpose": PROSE,
    "usage": "Join on customer_id for churn and lifetime-value models.",
    "limitations": "Not a system of record; rebuilt nightly, so intraday edits are invisible.",
}


def _contract(
    *,
    product_id: str = "gold.customer_360",
    expose_id: str = "customers",
    description: Any = PROSE,
    contract_id: Optional[str] = None,
    consumes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """A 0.7.6 contract declared the documented way.

    0.7.6 is a PREVIEW schema version, so it is validatable only because the
    contract declares it — see ``FluidSchemaManager.PREVIEW_VERSIONS``.
    """
    contract_block: Dict[str, Any] = {}
    if contract_id is not None:
        contract_block["id"] = contract_id
    contract_block["schema"] = [
        {"name": "customer_id", "type": "STRING", "required": True},
        {"name": "churn_score", "type": "FLOAT"},
    ]
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": product_id,
        "name": "Customer 360",
        "description": copy.deepcopy(description),
        "domain": "commerce",
        "lifecycle": {"state": "active"},
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "customer-data", "email": "customer-data@example.com"},
        },
        "consumes": copy.deepcopy(consumes) if consumes else [],
        "exposes": [
            {
                "exposeId": expose_id,
                "version": "1.0.0",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": f"runtime/{expose_id}.parquet"},
                },
                "contract": contract_block,
            }
        ],
    }


def _upstream(*, product_id: str = "commerce.orders", expose_id: str = "orders") -> Dict[str, Any]:
    upstream = _contract(product_id=product_id, expose_id=expose_id, description="Orders.")
    upstream["metadata"]["layer"] = "Silver"
    upstream["consumes"] = []
    return upstream


@pytest.fixture
def provider() -> BitolOdpsProvider:
    # Strict validation on: every document asserted below has also passed the
    # vendored Bitol schema, so an assertion cannot pass on invalid output.
    return BitolOdpsProvider()


def _assert_valid_fluid(contract: Dict[str, Any]) -> None:
    """The premise of each test: the contract itself is legal FLUID 0.7.6."""
    result, rc = run_on_contract_dict(contract)
    assert result.is_valid, f"fixture is not a valid FLUID contract: {result.errors}"
    assert rc == 0


def _only_odcs(rendered: Dict[str, Any]) -> Dict[str, Any]:
    contracts = rendered["contracts"]
    assert len(contracts) == 1, f"expected one per-port ODCS, got {sorted(contracts)}"
    return next(iter(contracts.values()))


# ---------------------------------------------------------------------------
# 1 — description
# ---------------------------------------------------------------------------


class TestDescriptionObject:
    def test_a_string_exports_as_purpose_in_both_documents(
        self, provider: BitolOdpsProvider
    ) -> None:
        contract = _contract()
        _assert_valid_fluid(contract)

        rendered = provider.render(contract)

        assert rendered["product"]["description"] == {"purpose": PROSE}
        assert _only_odcs(rendered)["description"] == {"purpose": PROSE}

    def test_an_object_passes_through_unchanged_in_both_documents(
        self, provider: BitolOdpsProvider
    ) -> None:
        contract = _contract(description=DESCRIPTION_OBJECT)
        _assert_valid_fluid(contract)

        rendered = provider.render(contract)

        # The three field names are Bitol's own in both standards, so the
        # object is a pass-through and not a translation.
        assert rendered["product"]["description"] == DESCRIPTION_OBJECT
        assert _only_odcs(rendered)["description"] == DESCRIPTION_OBJECT

    def test_the_object_survives_an_odps_round_trip(self, provider: BitolOdpsProvider) -> None:
        rendered = provider.render(_contract(description=DESCRIPTION_OBJECT))

        # ``lenient``: the product document is imported on its own here, so the
        # sibling ODCS the port references is not on disk to resolve.
        reimported = provider.import_contract(rendered["product"], lenient=True)

        assert provider.render(reimported)["product"]["description"] == DESCRIPTION_OBJECT


# ---------------------------------------------------------------------------
# 2 — input ports
# ---------------------------------------------------------------------------


class TestInputPortLineage:
    def test_input_port_contract_id_is_the_upstream_ports_odcs_id(
        self, provider: BitolOdpsProvider
    ) -> None:
        """Lineage between two FLUID products has to resolve.

        The downstream input port's ``contractId`` is compared against the id
        the UPSTREAM export actually gave its per-port ODCS document, not
        against a second spelling of the convention.
        """
        upstream = _upstream()
        downstream = _contract(
            consumes=[{"productId": upstream["id"], "exposeId": "orders"}],
        )
        _assert_valid_fluid(upstream)
        _assert_valid_fluid(downstream)

        upstream_odcs_id = _only_odcs(provider.render(upstream))["id"]
        input_ports = provider.render(downstream)["product"]["inputPorts"]

        assert [port["contractId"] for port in input_ports] == [upstream_odcs_id]
        # The defect: the bare productId, which names no contract anywhere.
        assert input_ports[0]["contractId"] != upstream["id"]

    def test_an_explicit_consume_contract_id_still_wins(self, provider: BitolOdpsProvider) -> None:
        """An upstream with an id of its own — one FLUID did not derive."""
        downstream = _contract(
            consumes=[
                {
                    "productId": "commerce.orders",
                    "exposeId": "orders",
                    "contractId": "urn:acme:catalog:7f3a9c1e",
                }
            ],
        )
        _assert_valid_fluid(downstream)

        rendered = provider.render(downstream)

        assert rendered["product"]["inputPorts"][0]["contractId"] == "urn:acme:catalog:7f3a9c1e"

    @pytest.mark.parametrize("api_version", SUPPORTED_API_VERSIONS)
    def test_version_constraint_is_exported_and_validates(
        self, provider: BitolOdpsProvider, api_version: str
    ) -> None:
        """Bitol ``InputPort`` has no range field, so the constraint rides in
        ``customProperties`` — and the document still satisfies both vendored
        schemas, which are ``additionalProperties: false`` throughout."""
        downstream = _contract(
            consumes=[
                {
                    "productId": "commerce.orders",
                    "exposeId": "orders",
                    "versionConstraint": "^1.2",
                }
            ],
        )
        _assert_valid_fluid(downstream)

        product = provider.render(downstream)["product"]

        port = product["inputPorts"][0]
        assert port["customProperties"] == [
            {
                "property": VERSION_CONSTRAINT_PROPERTY,
                "value": "^1.2",
                "description": (
                    "FLUID consumes[].versionConstraint — the semver range "
                    "this product accepts from the upstream contract."
                ),
            }
        ]
        schema = load_schema(api_version)
        assert schema is not None
        validate(product, schema)

    def test_a_consume_without_a_constraint_gets_no_custom_properties(
        self, provider: BitolOdpsProvider
    ) -> None:
        downstream = _contract(
            consumes=[{"productId": "commerce.orders", "exposeId": "orders"}],
        )

        port = provider.render(downstream)["product"]["inputPorts"][0]

        assert "customProperties" not in port


# ---------------------------------------------------------------------------
# 3 — pinned and imported contract ids
# ---------------------------------------------------------------------------


class TestPinnedContractId:
    def test_a_pinned_id_is_used_verbatim_by_both_exporters(
        self, provider: BitolOdpsProvider
    ) -> None:
        contract = _contract(contract_id="urn:acme:catalog:2f8c41b0")
        _assert_valid_fluid(contract)

        rendered = provider.render(contract)

        assert rendered["product"]["outputPorts"][0]["contractId"] == "urn:acme:catalog:2f8c41b0"
        assert _only_odcs(rendered)["id"] == "urn:acme:catalog:2f8c41b0"

    def test_a_pinned_id_survives_renaming_the_product_and_the_expose(
        self, provider: BitolOdpsProvider
    ) -> None:
        """What the derivation cost: a rename silently renamed the contract."""
        pinned = "urn:acme:catalog:2f8c41b0"
        before = provider.render(_contract(contract_id=pinned))
        after = provider.render(
            _contract(product_id="gold.renamed", expose_id="renamed_port", contract_id=pinned)
        )

        for rendered in (before, after):
            assert rendered["product"]["outputPorts"][0]["contractId"] == pinned
            assert _only_odcs(rendered)["id"] == pinned

    def test_without_a_pinned_id_the_derivation_is_unchanged(
        self, provider: BitolOdpsProvider
    ) -> None:
        rendered = provider.render(_contract())

        assert rendered["product"]["outputPorts"][0]["contractId"] == "gold.customer_360.customers"
        assert _only_odcs(rendered)["id"] == "gold.customer_360.customers"

    def test_the_two_ids_stay_the_same_string(self, provider: BitolOdpsProvider) -> None:
        """Bitol fragments mode reads the sibling ODCS by the port's
        ``contractId``, so the provider refuses to emit a bundle where the two
        disagree (``BitolOdpsProvider.render`` raises on the mismatch). Assert
        the equality itself, for a pinned id and a derived one alike."""
        for contract_id in (None, "urn:acme:catalog:2f8c41b0"):
            rendered = provider.render(_contract(contract_id=contract_id))
            port_id = rendered["product"]["outputPorts"][0]["contractId"]
            assert port_id == _only_odcs(rendered)["id"]
            assert port_id in rendered["contracts"]


class TestImportedContractId:
    """An id the product already answers to must survive the round-trip.

    The imported document cannot carry it in ``exposes[].contract.id``: an
    import is pinned to the current STABLE FLUID schema, whose
    ``exposeContract`` is closed, so the field would make every imported
    contract fail ``fluid validate``. It rides in the per-expose pass-through
    bucket instead, which is what the exporters read when the contract pins
    nothing itself.
    """

    FOREIGN_ID = "9f1c2b34-5d6e-47a8-b9c0-1d2e3f4a5b6c"

    def _bundle(self, tmp_path: Path) -> Path:
        product = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "commerce.orders",
            "name": "Orders",
            "version": "1.0.0",
            "status": "active",
            "description": {"purpose": "Orders, registered under the catalog's own id."},
            "team": {"name": "commerce-team"},
            "outputPorts": [
                {
                    "name": "daily_orders",
                    "version": "1.0.0",
                    # Not ``commerce.orders.daily_orders``: the catalog that
                    # registered this product minted the id.
                    "contractId": self.FOREIGN_ID,
                    "type": "bigquery",
                }
            ],
        }
        odcs = {
            "apiVersion": "v3.1.0",
            "kind": "DataContract",
            "id": self.FOREIGN_ID,
            "version": "1.0.0",
            "status": "active",
            "name": "Orders",
            "schema": [
                {
                    "name": "daily_orders",
                    "logicalType": "object",
                    "physicalType": "table",
                    "properties": [
                        {
                            "name": "order_date",
                            "logicalType": "date",
                            "physicalType": "DATE",
                            "required": True,
                        }
                    ],
                }
            ],
        }
        (tmp_path / "commerce.orders.odps.yaml").write_text(
            yaml.safe_dump(product, sort_keys=False), encoding="utf-8"
        )
        (tmp_path / f"{self.FOREIGN_ID}.odcs.yaml").write_text(
            yaml.safe_dump(odcs, sort_keys=False), encoding="utf-8"
        )
        return tmp_path

    def test_an_imported_id_re_exports_verbatim(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        imported = provider.import_directory(self._bundle(tmp_path))

        rendered = provider.render(imported)

        assert rendered["product"]["outputPorts"][0]["contractId"] == self.FOREIGN_ID
        assert _only_odcs(rendered)["id"] == self.FOREIGN_ID

    def test_the_imported_document_still_validates(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        _assert_valid_fluid(provider.import_directory(self._bundle(tmp_path)))

    def test_a_pin_added_after_the_import_wins_over_it(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        imported = provider.import_directory(self._bundle(tmp_path))
        imported["fluidVersion"] = "0.7.6"
        imported["exposes"][0]["contract"]["id"] = "urn:acme:catalog:2f8c41b0"

        rendered = provider.render(imported)

        assert rendered["product"]["outputPorts"][0]["contractId"] == "urn:acme:catalog:2f8c41b0"
        assert _only_odcs(rendered)["id"] == "urn:acme:catalog:2f8c41b0"
