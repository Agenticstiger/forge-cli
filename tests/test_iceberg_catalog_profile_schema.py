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

"""End-to-end JSON-schema validation for Iceberg catalog-profile bindings.

Regression test for the schema/resolver mismatch on the 0.7.5 streaming-sink
surface: ``providers/_iceberg_catalog.py`` and
``build_runners/kafka_connect/iceberg_sink_validation.py`` read REST/GCP/Azure
catalog-profile fields off ``binding.location`` — ``catalog``, ``uri``,
``warehouse``, ``partitionBy`` — but ``$defs/bindingLocation`` in
fluid-schema-0.7.5.json was ``additionalProperties: false`` and did not declare
them, so a contract that selected a catalog profile via ``binding.location``
failed ``fluid validate`` with "Additional properties are not allowed".

The names mirror the canonical Apache Iceberg REST catalog vocabulary (``uri`` /
``warehouse`` are the two core REST-catalog properties; ``catalog`` →
``iceberg.catalog.type``; ``partitionBy`` → ``iceberg.tables.default-partition-by``).
A borrow-before-build prior-art sweep confirmed those four are canonical/acceptable
and that the two snake_case/duplicate spellings the reader used to also accept as
fallbacks (``catalogUri`` / ``catalog_warehouse``) are redundant — they were never
in a released schema and nothing emits them, so they are intentionally NOT added;
``uri`` / ``warehouse`` are the single canonical spellings.

The existing resolver/validator unit tests
(``tests/providers/test_iceberg_catalog_profiles.py``,
``tests/build_runners/test_iceberg_sink_validation.py``) pass raw dicts straight
to the functions and BYPASS the JSON schema, so they never exercised this path.
These tests drive the REAL schema-validate path
(``FluidSchemaManager.validate_contract``) end-to-end.
"""

from __future__ import annotations

import pytest

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit]


def _base_contract() -> dict:
    """A schema-valid 0.7.5 streaming-Iceberg contract WITHOUT catalog-profile
    location fields — mirrors tests/fixtures/contracts/compatibility/minimal_075.yaml.
    Each test layers a profile onto ``exposes[0].binding.location`` so the only
    thing under test is the catalog-profile fields."""
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "compat.ops.iceberg_catalog_profile",
        "name": "Iceberg Catalog Profile",
        "description": "Schema-validate coverage for REST/GCP/Azure Iceberg catalog profiles.",
        "domain": "ops",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "platform-ops", "email": "ops@example.com"},
        },
        "builds": [
            {
                "id": "ingest_events",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "mode": "incremental_append",
                        "streams": ["public.events"],
                    },
                    "sink": {"format": "iceberg", "catalog": "rest"},
                    "kafka-connect": {
                        "iceberg_sink_enabled": True,
                        "streamingSink": {"autoCreate": True, "commitIntervalMs": 1000},
                    },
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {"database": "bronze", "table": "events"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "integer", "required": True},
                        {"name": "payload", "type": "string"},
                    ]
                },
            }
        ],
    }


# Each profile exercises a different warehouse scheme (-> FileIO) using ONLY the
# canonical Iceberg-REST spellings (uri / warehouse), so all four declared fields
# are covered across the set.
CATALOG_PROFILES = {
    # REST catalog, object-store-agnostic warehouse NAME + endpoint uri.
    "rest": {
        "catalog": "rest",
        "uri": "https://catalog.example.com/api",
        "warehouse": "prod_catalog",
    },
    # GCP: gs:// warehouse (-> GCSFileIO) + partitionBy.
    "gcp": {
        "catalog": "rest",
        "uri": "https://bq-metastore.example/iceberg",
        "warehouse": "gs://fluid-lake/warehouse/",
        "partitionBy": ["event_date"],
    },
    # Azure: abfss:// warehouse (-> ADLSFileIO).
    "azure": {
        "catalog": "rest",
        "uri": "https://adls-catalog.example/api",
        "warehouse": "abfss://wh@acct.dfs.core.windows.net/warehouse/",
    },
}


@pytest.mark.parametrize("profile", sorted(CATALOG_PROFILES))
def test_catalog_profile_passes_schema_validation(profile: str) -> None:
    contract = _base_contract()
    contract["exposes"][0]["binding"]["location"].update(CATALOG_PROFILES[profile])

    result = FluidSchemaManager().validate_contract(contract, "0.7.5")

    assert result.is_valid, f"{profile} catalog profile rejected by schema: {result.errors}"


def test_all_catalog_profile_fields_accepted_together() -> None:
    """A single binding.location carrying every newly-declared field at once."""
    contract = _base_contract()
    contract["exposes"][0]["binding"]["location"].update(
        {
            "catalog": "rest",
            "uri": "https://catalog.example.com/api",
            "warehouse": "gs://fluid-lake/warehouse/",
            "partitionBy": ["event_date", "tenant"],
        }
    )

    result = FluidSchemaManager().validate_contract(contract, "0.7.5")

    assert result.is_valid, f"combined catalog-profile location rejected: {result.errors}"


def test_base_contract_is_valid_without_profile_fields() -> None:
    """Guard: the base contract validates clean, so a failing profile test is
    attributable to the catalog-profile fields and nothing else."""
    result = FluidSchemaManager().validate_contract(_base_contract(), "0.7.5")
    assert result.is_valid, f"base contract unexpectedly invalid: {result.errors}"


def test_bindinglocation_still_rejects_unknown_field() -> None:
    """We added NAMED fields, not additionalProperties:true — a genuinely
    unknown key must still be rejected (additionalProperties:false intact)."""
    contract = _base_contract()
    contract["exposes"][0]["binding"]["location"]["totallyBogusField"] = "nope"

    result = FluidSchemaManager().validate_contract(contract, "0.7.5")

    assert not result.is_valid
    assert any(
        "Additional properties" in e and "totallyBogusField" in e for e in result.errors
    ), f"expected additionalProperties rejection, got: {result.errors}"


@pytest.mark.parametrize("alias", ["catalogUri", "catalog_warehouse"])
def test_redundant_aliases_are_not_part_of_the_schema(alias: str) -> None:
    """The redundant non-canonical aliases (catalogUri / catalog_warehouse) were
    intentionally NOT added — the single canonical spellings are uri / warehouse.
    Guard that they stay rejected, so they can't silently creep back in."""
    contract = _base_contract()
    contract["exposes"][0]["binding"]["location"].update(
        {"catalog": "rest", "uri": "https://c.example/api", "warehouse": "wh", alias: "x"}
    )

    result = FluidSchemaManager().validate_contract(contract, "0.7.5")

    assert not result.is_valid
    assert any(
        "Additional properties" in e and alias in e for e in result.errors
    ), f"expected {alias} to be rejected, got: {result.errors}"
