# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An ODCS Databricks server names the Unity Catalog catalog its table is in.

A FLUID Databricks binding names its Unity Catalog catalog
``binding.location.database``: the Databricks emitter creates the table there
and refuses ``location.catalog``, which selects an Iceberg catalog. ODCS's
DatabricksServer calls the same catalog ``catalog``. Two defects followed:

* The export read only ``location.catalog``, so every contract forge can
  deploy exported ``catalog: ${DATABRICKS_CATALOG}``. A consumer of the ODCS
  document (``datacontract test``, DQX, a catalog) could not find the table.
* The import copied no ``catalog`` from an ODCS Databricks server, so the
  catalog was lost on the way into FLUID.

The Entropy Data publish builds its own ODCS document, which copied
``database`` but never wrote ``catalog``.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional

import pytest

from fluid_build.iac.providers.databricks import validate_databricks_binding
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.servers import (
    _expect_to_server,
    _location_from_server,
    _server_details_from_location,
)
from fluid_build.providers.odcs.validation import collect_errors, load_schema
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

CATALOG_PLACEHOLDER = "${DATABRICKS_CATALOG}"
HOST_PLACEHOLDER = "${DATABRICKS_HOST}"
EXPOSE_ID = "orders"
#: What ``fluid apply`` reads from the environment; a contract never names either.
ENV = {
    "DATABRICKS_HOST": "https://dbc-example.cloud.databricks.com",
    "DATABRICKS_WAREHOUSE_ID": "abc123def4567890",
}


def _contract(location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "domain": "sales",
        "metadata": {
            "layer": "Gold",
            "productType": "ADP",
            "owner": {"team": "sales-data", "email": "sales-data@example.com"},
        },
        "exposes": [
            {
                "exposeId": EXPOSE_ID,
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": (
                        {"database": "workspace", "schema": "sales", "table": "orders"}
                        if location is None
                        else location
                    ),
                },
                "contract": {"schema": [{"name": "order_id", "type": "string", "required": True}]},
            }
        ],
    }


def _export(contract: Dict[str, Any]) -> Dict[str, Any]:
    return OdcsProvider().render(copy.deepcopy(contract))


def _databricks_server(odcs: Dict[str, Any]) -> Dict[str, Any]:
    servers = [s for s in odcs["servers"] if s["type"] == "databricks"]
    assert len(servers) == 1, odcs["servers"]
    return servers[0]


class TestFixtureIsAContractForgeDeploys:
    """Every case below starts from this fixture, so pin that forge applies it."""

    def test_the_fixture_validates_as_0_7_6(self) -> None:
        result = FluidSchemaManager().validate_contract(_contract(), "0.7.6", offline_only=True)
        assert result.is_valid, result.errors

    def test_the_databricks_emitter_accepts_the_fixture(self) -> None:
        errors, _ = validate_databricks_binding(_contract(), ENV)
        assert errors == []

    def test_the_emitter_refuses_location_catalog(self) -> None:
        # Why the export cannot read the catalog from ``location.catalog``:
        # a contract that names it there is not one forge deploys.
        errors, _ = validate_databricks_binding(
            _contract({"catalog": "workspace", "schema": "sales", "table": "orders"}), ENV
        )
        assert any("location.catalog" in e for e in errors), errors


class TestExport:
    def test_the_server_names_the_catalog_the_table_is_deployed_to(self) -> None:
        odcs = _export(_contract())

        server = _databricks_server(odcs)
        assert server["catalog"] == "workspace"
        assert server["schema"] == "sales"
        assert CATALOG_PLACEHOLDER not in json.dumps(odcs)

    def test_the_host_stays_the_placeholder(self) -> None:
        # The workspace comes from DATABRICKS_HOST only: the emitter refuses a
        # host in a contract, so the export has no host to name.
        assert _databricks_server(_export(_contract()))["host"] == HOST_PLACEHOLDER

    def test_location_catalog_is_still_read_when_database_is_absent(self) -> None:
        details = _server_details_from_location({"catalog": "main", "schema": "s"}, "databricks")

        assert details["catalog"] == "main"

    def test_database_wins_when_a_location_sets_both(self) -> None:
        details = _server_details_from_location(
            {"database": "uc_catalog", "catalog": "iceberg_catalog", "schema": "s"}, "databricks"
        )

        assert details["catalog"] == "uc_catalog"

    def test_neither_keeps_the_documented_placeholders(self) -> None:
        details = _server_details_from_location({}, "databricks")

        assert details == {
            "host": HOST_PLACEHOLDER,
            "catalog": CATALOG_PLACEHOLDER,
            "schema": "${DATABRICKS_SCHEMA}",
        }

    def test_no_field_outside_the_databricks_server_is_written(self) -> None:
        # ODCS servers are closed (``unevaluatedProperties: false``): ``database``
        # and ``table`` have no place on a DatabricksServer.
        server = _databricks_server(_export(_contract()))

        assert set(server) == {"server", "type", "host", "catalog", "schema"}


class TestExportedServerIsValidOdcs:
    @pytest.fixture(scope="class")
    def odcs_schema(self) -> Dict[str, Any]:
        pytest.importorskip("jsonschema")
        schema = load_schema()
        assert schema is not None, "the vendored ODCS v3.1.0 schema must be present"
        return schema

    def test_the_exported_document_validates_against_odcs_3_1_0(
        self, odcs_schema: Dict[str, Any]
    ) -> None:
        assert collect_errors(_export(_contract()), odcs_schema) == []

    def test_the_validation_is_not_vacuous(self, odcs_schema: Dict[str, Any]) -> None:
        # A Databricks server without a catalog is what ODCS rejects; if this
        # stopped failing, the check above would prove nothing.
        broken = _export(_contract())
        del _databricks_server(broken)["catalog"]

        errors = collect_errors(broken, odcs_schema)

        assert any("catalog" in e["message"] for e in errors), errors


class TestImport:
    def _odcs_server(self, **fields: Any) -> Dict[str, Any]:
        return {"server": "production", "type": "databricks", "host": "h", **fields}

    def test_the_catalog_becomes_location_database(self) -> None:
        location = _location_from_server(self._odcs_server(catalog="main", schema="sales"))

        assert location["database"] == "main"
        assert location["schema"] == "sales"

    def test_an_expect_built_from_the_server_exports_its_catalog_again(self) -> None:
        expect = {
            "id": "production",
            "provider": "databricks",
            "location": _location_from_server(self._odcs_server(catalog="main", schema="sales")),
        }

        assert _expect_to_server(expect)["catalog"] == "main"

    def test_a_server_that_names_database_keeps_it(self) -> None:
        location = _location_from_server(self._odcs_server(catalog="c", database="d"))

        assert location["database"] == "d"

    def test_another_server_type_gains_no_database(self) -> None:
        # ``catalog`` means the Unity Catalog catalog on Databricks only; a
        # Trino or Iceberg catalog is something else.
        location = _location_from_server({"type": "trino", "catalog": "hive", "schema": "s"})

        assert "database" not in location


def _foreign_odcs() -> Dict[str, Any]:
    """An ODCS document forge did not write: no FLUID extras to restore a binding from."""
    return {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "orders-contract",
        "version": "1.0.0",
        "status": "active",
        "name": "orders",
        "schema": [
            {
                "name": "orders",
                "physicalName": "orders",
                "physicalType": "table",
                "properties": [{"name": "order_id", "logicalType": "string"}],
            }
        ],
        "servers": [
            {
                "server": "production",
                "type": "databricks",
                "host": "https://dbc-example.cloud.databricks.com",
                "catalog": "main",
                "schema": "sales",
            }
        ],
    }


class TestImportOfAForeignContract:
    def _expose_location(self, fluid: Dict[str, Any]) -> Dict[str, Any]:
        exposes = fluid.get("exposes") or []
        assert len(exposes) == 1, exposes
        return exposes[0]["binding"]["location"]

    def test_the_binding_names_the_catalog_as_location_database(self) -> None:
        fluid = OdcsProvider().import_contract(_foreign_odcs())

        assert self._expose_location(fluid) == {
            "database": "main",
            "schema": "sales",
            "table": "orders",
        }

    def test_the_databricks_emitter_finds_the_catalog(self) -> None:
        fluid = OdcsProvider().import_contract(_foreign_odcs())

        errors, _ = validate_databricks_binding(fluid, ENV)

        assert not [e for e in errors if "location.database" in e], errors

    def test_the_re_export_still_reproduces_the_source_servers(self) -> None:
        # The import marks the binding it derived from the servers as
        # synthesized and keeps the servers verbatim; the export writes them
        # back. Adding ``database`` to the binding must not disturb that.
        source = _foreign_odcs()

        odcs = _export(OdcsProvider().import_contract(copy.deepcopy(source)))

        assert odcs["servers"] == source["servers"]


class TestEntropyDataContract:
    def test_the_databricks_server_names_its_catalog(self) -> None:
        provider = DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

        dc = provider._build_data_contract_odcs(_contract(), "sales.orders")

        servers = [s for s in dc["servers"] if s.get("type") == "databricks"]
        assert len(servers) == 1, dc["servers"]
        assert servers[0]["catalog"] == "workspace"
        assert servers[0]["schema"] == "sales"
        # The keys this payload carried before stay as they were.
        assert servers[0]["database"] == "workspace"
        assert servers[0]["table"] == "orders"
