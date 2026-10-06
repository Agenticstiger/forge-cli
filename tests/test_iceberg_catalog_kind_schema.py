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

"""Schema surface for the Iceberg catalog kinds.

``location.catalog`` is a free string the kind table in
``providers/_iceberg_catalog.py`` classifies, and ``sink.catalog`` is an enum.
These tests pin the schema's enum and prose to the table rather than to a
hand-kept list, so a kind the table does not know cannot pass the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fluid_build
from fluid_build.providers._iceberg_catalog import (
    FAMILY_UNKNOWN,
    canonical_catalog_kind,
    catalog_kind_info,
    known_catalog_kinds,
)
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit]

_SCHEMA_DIR = Path(fluid_build.__file__).parent / "schemas"


def _schema(version: str) -> dict:
    return json.loads((_SCHEMA_DIR / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))


def _contract(version: str, *, sink_catalog=None, location=None) -> dict:
    sink = {"format": "iceberg"}
    if sink_catalog is not None:
        sink["catalog"] = sink_catalog
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "compat.ops.iceberg_catalog_kind",
        "name": "Iceberg Catalog Kind",
        "description": "Schema coverage for the Iceberg catalog kinds.",
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
                    "sink": sink,
                    "kafka-connect": {"iceberg_sink_enabled": True},
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
                    "location": {"database": "bronze", "table": "events", **(location or {})},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    }


def _validate(contract: dict, version: str):
    return FluidSchemaManager().validate_contract(contract, version)


_LAKEKEEPER_LOCATION = {
    "catalog": "lakekeeper",
    "uri": "http://lakekeeper:8181/catalog",
    "warehouse": "demo",
}


@pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
def test_base_contract_is_valid(version: str) -> None:
    """Guard: a failure below is the catalog value's, not the fixture's."""
    result = _validate(_contract(version), version)
    assert result.is_valid, result.errors


@pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
def test_location_catalog_lakekeeper_is_a_free_string(version: str) -> None:
    contract = _contract(version, location=dict(_LAKEKEEPER_LOCATION))
    result = _validate(contract, version)
    assert result.is_valid, f"{version} refused location.catalog lakekeeper: {result.errors}"


@pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
def test_sink_catalog_enum_names_only_kinds_the_table_knows(version: str) -> None:
    """An enum member the kind table does not know would pass the schema and
    then be refused by ``fluid validate`` (or, worse, hit an emitter fallback)."""
    enum = _schema(version)["$defs"]["acquisitionSink"]["properties"]["catalog"]["enum"]
    unknown = [v for v in enum if catalog_kind_info(v).family == FAMILY_UNKNOWN]
    assert unknown == []


def test_location_catalog_description_lists_every_kind() -> None:
    """The prose is the only place a contract author learns the kinds, so it
    must name every canonical kind in the table."""
    description = _schema("0.7.6")["$defs"]["bindingLocation"]["properties"]["catalog"][
        "description"
    ]
    canonical = [k for k in known_catalog_kinds() if canonical_catalog_kind(k) == k]
    missing = [k for k in canonical if k not in description]
    assert missing == [], f"location.catalog description omits {missing}"
