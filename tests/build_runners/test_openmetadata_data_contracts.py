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
