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

"""Regression test for the DMM publish team-id slug fix.

``_publish_one`` resolves the wire team id via ``_derive_team_id``, which
slugifies ``metadata.owner.team`` (spaces -> hyphens, lower-cased). The ODPS
payload built by ``_to_data_product_odps`` already carries ``team["name"]``
set to the raw display name, so the override in ``_publish_one`` MUST
force-assign (``team["name"] = tid``) rather than ``setdefault`` — otherwise
the un-slugified display name reaches the wire and Entropy / DMM rejects the
publish with HTTP 422 "Could not find team by id '<display name>'".
"""

from __future__ import annotations

import pytest

from fluid_build.providers.datamesh_manager.datamesh_manager import DataMeshManagerProvider

pytestmark = pytest.mark.unit


def _contract_with_team(team: str) -> dict:
    """A minimal ODPS-publishable contract whose owner team is ``team``."""
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "silver.telco.subscriber360_v1",
        "name": "Telco Subscriber 360",
        "domain": "telco",
        "metadata": {"owner": {"team": team, "email": "ci@example.com"}},
        "exposes": [
            {
                "exposeId": "subscriber360_core",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "TELCO_LAB",
                        "schema": "GOLD",
                        "table": "SUBSCRIBER_360_V1",
                    },
                },
                "contract": {"schema": [{"name": "subscriber_id", "type": "string"}]},
            }
        ],
    }


class TestPublishTeamNameSlugified:
    """The PUT payload's ``team.name`` is the slugified id, never the raw name."""

    def test_team_name_with_spaces_is_slugified_in_payload(self) -> None:
        provider = DataMeshManagerProvider(api_key="test-key-123")
        result = provider._publish_one(
            _contract_with_team("Customer Platform"),
            dry_run=True,
            data_product_specification="odps",
        )
        team = result["payload"]["team"]
        # "Customer Platform" -> "customer-platform". The raw display name
        # would trigger HTTP 422 "Could not find team by id" on the wire.
        assert team["name"] == "customer-platform"
        assert team["name"] != "Customer Platform"

    def test_already_slug_safe_team_name_is_unchanged(self) -> None:
        provider = DataMeshManagerProvider(api_key="test-key-123")
        result = provider._publish_one(
            _contract_with_team("telco-customer-intelligence"),
            dry_run=True,
            data_product_specification="odps",
        )
        assert result["payload"]["team"]["name"] == "telco-customer-intelligence"
