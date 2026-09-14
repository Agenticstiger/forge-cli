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

"""ODCS contracts land on OpenMetadata's first-class Data Contracts entity.

The registrar used to write the ODCS document only into the table's
free-form ``extension`` blob, which leaves it invisible to OpenMetadata's
contracts UI, contract search and validation runs. Since OpenMetadata 1.10
Data Contracts are a real entity, so the registrar now also imports the
document through ``PUT /api/v1/dataContracts/odcs/yaml``.

Route shape verified against ``DataContractResource.java`` on
open-metadata/OpenMetadata ``main``: class ``@Path("/v1/dataContracts")``,
ODCS create-or-update at ``PUT /odcs/yaml`` consuming ``application/yaml``
with ``entityId`` / ``entityType`` / ``mode`` / ``objectName`` query params.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.build_runners.catalog_registrars import OpenMetadataRegistrar

pytestmark = pytest.mark.unit


def _contract(*, columns: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.x",
        "name": "x",
        "description": "Bronze test",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform", "email": "x@y.z"}},
        "tags": ["bronze"],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"path": "/data/orders/"},
                },
                "contract": {
                    "schema": columns or [{"name": "id", "type": "string"}],
                    "schemaPolicy": "discover_and_freeze",
                },
            }
        ],
    }


def _register(mock, **kwargs) -> Any:
    registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test", **kwargs)
    return registrar.register("bronze.x", "orders", _contract(), {})


class TestOdcsContractPublish:
    def test_contract_is_registered_against_the_data_contracts_api(self, openmetadata_mock):
        result = _register(openmetadata_mock)
        assert result.succeeded
        assert len(openmetadata_mock.odcs_contracts) == 1, (
            "the ODCS document must be imported through the Data Contracts API, "
            "not left in the table's extension blob"
        )

    def test_table_is_published_before_the_contract(self, openmetadata_mock):
        """The contract import keys on the table's UUID, so ordering matters."""
        _register(openmetadata_mock)
        assert openmetadata_mock.calls.index("put_table") < openmetadata_mock.calls.index(
            "put_odcs_contract"
        )

    def test_entity_id_is_resolved_from_the_fqn(self, openmetadata_mock):
        """ODCS import takes entityId, not an FQN, so a lookup hop is required."""
        _register(openmetadata_mock)
        assert "get_table_by_name" in openmetadata_mock.calls
        imported = openmetadata_mock.odcs_contracts[0]
        assert imported["entityId"] == "om-1"

    def test_query_params_match_the_upstream_signature(self, openmetadata_mock):
        _register(openmetadata_mock)
        imported = openmetadata_mock.odcs_contracts[0]
        assert imported["entityType"] == "table"
        # merge preserves server-side fields the registrar does not own.
        assert imported["mode"] == "merge"

    def test_body_is_yaml_with_the_yaml_content_type(self, openmetadata_mock):
        _register(openmetadata_mock)
        imported = openmetadata_mock.odcs_contracts[0]
        assert imported["headers"]["content-type"] == "application/yaml"
        assert "apiVersion" in imported["yaml"]

    def test_imported_document_is_odcs(self, openmetadata_mock):
        import yaml

        _register(openmetadata_mock)
        doc = yaml.safe_load(openmetadata_mock.odcs_contracts[0]["yaml"])
        assert doc["apiVersion"] == "v3.1.0"
        assert doc["kind"] == "DataContract"

    def test_bearer_token_is_forwarded(self, openmetadata_mock):
        _register(openmetadata_mock, api_token="tok")
        assert openmetadata_mock.odcs_contracts[0]["headers"]["authorization"] == "Bearer tok"


class TestDegradesCleanly:
    def test_pre_1_10_server_does_not_fail_registration(self, openmetadata_mock):
        """A 404 on the contracts route must not lose the table publish."""
        openmetadata_mock.data_contracts_available = False
        result = _register(openmetadata_mock)
        assert result.succeeded
        assert openmetadata_mock.tables, "the table publish must still have happened"
        assert not openmetadata_mock.odcs_contracts

    def test_extension_blob_still_carries_the_contract_as_fallback(self, openmetadata_mock):
        """Kept so pre-1.10 servers still surface the contract somewhere."""
        openmetadata_mock.data_contracts_available = False
        _register(openmetadata_mock)
        assert "odcs_contract" in openmetadata_mock.tables[0]["extension"]

    def test_failure_log_does_not_leak_the_token(self, openmetadata_mock, caplog):
        openmetadata_mock.data_contracts_available = False
        with caplog.at_level("DEBUG"):
            _register(openmetadata_mock, api_token="super-secret-token")
        assert "super-secret-token" not in " ".join(r.getMessage() for r in caplog.records)


class TestUnchangedBehaviour:
    def test_table_publish_still_carries_fluid_native_attachments(self, openmetadata_mock):
        """extension keeps the attachments that have no first-class home."""
        _register(openmetadata_mock)
        extension = openmetadata_mock.tables[0]["extension"]
        assert extension["fluid_layer"] == "Bronze"

    def test_unregister_is_untouched(self, openmetadata_mock):
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        assert registrar.unregister("bronze.x", "orders").succeeded


class TestReadPath:
    """The read half: an OpenMetadata-held contract can drive plan/apply.

    Preferring ``extension.odcs_contract`` over OpenMetadata's own ODCS
    export is not a hack around the API. Their converter drops ``servers``
    and six other top-level blocks (open-metadata/OpenMetadata#30493), so
    the native export cannot round-trip a physical binding. The extension
    is written verbatim and preserved verbatim, so it can.
    """

    ODCS_WITH_SERVERS = (
        "apiVersion: v3.1.0\n"
        "kind: DataContract\n"
        "id: bronze.x.orders\n"
        "status: active\n"
        "servers:\n"
        "  - server: prod\n"
        "    type: snowflake\n"
        "    account: acme-prod\n"
        "    database: ANALYTICS\n"
        "    schema: PUBLIC\n"
    )

    def _seed(self, mock, *, odcs: str) -> str:
        """Publish a table carrying an ODCS contract, return its FQN."""
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        contract = _contract()
        contract["exposes"][0]["contract"]["odcs"] = odcs
        registrar.register("bronze.x", "orders", contract, {})
        mock.tables[0]["extension"]["odcs_contract"] = odcs
        return mock.tables[0]["fullyQualifiedName"]

    def test_contract_is_read_back_from_the_extension(self, openmetadata_mock):
        fqn = self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        got = registrar.fetch_odcs_contract(fqn)
        assert got == self.ODCS_WITH_SERVERS

    def test_the_physical_binding_survives(self, openmetadata_mock):
        """servers[] is the binding. Losing it is what stops an
        OpenMetadata-held contract driving fluid apply."""
        import yaml as _yaml

        fqn = self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        doc = _yaml.safe_load(registrar.fetch_odcs_contract(fqn))
        assert doc["servers"][0]["account"] == "acme-prod"
        assert doc["servers"][0]["database"] == "ANALYTICS"

    def test_extension_is_preferred_over_the_lossy_native_export(self, openmetadata_mock):
        fqn = self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        registrar.fetch_odcs_contract(fqn)
        assert "get_native_odcs" not in openmetadata_mock.calls

    def test_falls_back_to_the_native_export(self, openmetadata_mock):
        """A contract published by something other than fluid has no
        extension, so the native route is the only source."""
        self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        openmetadata_mock.tables[0]["extension"].pop("odcs_contract", None)
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        got = registrar.fetch_odcs_contract(openmetadata_mock.tables[0]["fullyQualifiedName"])
        assert got and "id: native.export" in got
        assert "get_native_odcs" in openmetadata_mock.calls

    def test_unknown_asset_returns_none(self, openmetadata_mock):
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        assert registrar.fetch_odcs_contract("forge.nope.missing") is None

    def test_neither_source_available_returns_none(self, openmetadata_mock):
        self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        openmetadata_mock.tables[0]["extension"].pop("odcs_contract", None)
        openmetadata_mock.native_odcs_available = False
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        assert (
            registrar.fetch_odcs_contract(openmetadata_mock.tables[0]["fullyQualifiedName"]) is None
        )

    def test_read_failure_does_not_leak_the_token(self, openmetadata_mock, caplog):
        openmetadata_mock.native_odcs_available = False
        registrar = OpenMetadataRegistrar(
            base_url="https://openmetadata.test", api_token="super-secret-token"
        )
        with caplog.at_level("DEBUG"):
            registrar.fetch_odcs_contract("forge.nope.missing")
        assert "super-secret-token" not in " ".join(r.getMessage() for r in caplog.records)

    def test_round_trips_into_a_fluid_contract(self, openmetadata_mock):
        """End to end: OpenMetadata to a FLUID contract, binding intact."""
        from fluid_build.providers.odcs.provider import OdcsProvider

        fqn = self._seed(openmetadata_mock, odcs=self.ODCS_WITH_SERVERS)
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        odcs_yaml = registrar.fetch_odcs_contract(fqn)

        import yaml as _yaml

        fluid = OdcsProvider().import_contract(_yaml.safe_load(odcs_yaml))
        assert fluid is not None
