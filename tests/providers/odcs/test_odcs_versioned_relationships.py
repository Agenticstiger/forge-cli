# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The 0.7.7 versioned-relationship members through the ODCS exporter.

ODCS has no field for a validity window's conventions (``openEnd``, ``chain``, the natural
entity's ``lifecycle``) or for a relationship's ``asOf``, so the expose's ``semantics`` rides
verbatim in the ``fluidExtras`` custom property (``mappers/extras.py``), and an import reads it
back unchanged. The ``sharedColumns`` rules that the window makes by-entity or as-of joins are
SQL quality checks. Pinned for both ODCS versions the exporter writes.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest

from fluid_build.providers.odcs import validation
from fluid_build.providers.odcs.mappers.extras import FLUID_EXTRAS_PROPERTY
from fluid_build.providers.odcs.provider import OdcsProvider

pytestmark = pytest.mark.unit

VERSIONS = ["v3.1.0", "v3.2.0"]

WINDOW_SEMANTICS: Dict[str, Any] = {
    "entities": [
        {
            "name": "station",
            "type": "natural",
            "expr": "station_id",
            "lifecycle": {
                "activation": "commissioned_on",
                "endMarker": {"column": "status", "value": "DECOMMISSIONED"},
                "currentFlag": {"column": "is_current", "value": True},
            },
        }
    ],
    "dimensions": [
        {
            "name": "valid_from",
            "type": "time",
            "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
        },
        {
            "name": "valid_to",
            "type": "time",
            "typeParams": {
                "timeGranularity": "day",
                "validityParams": {"isEnd": True, "openEnd": "9999-12-31", "chain": "dayBefore"},
            },
        },
    ],
}

RELATIONSHIPS: List[Dict[str, Any]] = [
    {
        "name": "station",
        "to": "station_version",
        "fromColumns": ["station_id"],
        "toColumns": ["station_id"],
        "cardinality": "many_to_one",
        "sharedColumns": ["network_id"],
    },
    {
        "name": "station_on_date",
        "to": "station_version",
        "fromColumns": ["station_id"],
        "toColumns": ["station_id"],
        "cardinality": "many_to_one",
        "asOf": "reading_date",
        "sharedColumns": ["sensor_model"],
    },
]

DEPENDENCY = {
    "id": "network_stable_per_station",
    "dependency": {"from": ["station_id"], "to": ["network_id"]},
}


def _binding(table: str) -> Dict[str, Any]:
    return {
        "platform": "snowflake",
        "format": "snowflake_table",
        "location": {"database": "WEATHER", "schema": "CDP", "table": table.upper()},
    }


def _contract() -> Dict[str, Any]:
    def column(name: str, declared: str) -> Dict[str, str]:
        return {"name": name, "type": declared}

    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.weather.stations",
        "name": "Stations",
        "metadata": {"layer": "Gold", "owner": {"team": "weather-platform"}},
        "exposes": [
            {
                "exposeId": "station_version",
                "kind": "table",
                "binding": _binding("station_version"),
                "contract": {
                    "schema": [
                        column("station_id", "bigint"),
                        column("valid_from", "date"),
                        column("valid_to", "date"),
                        column("network_id", "bigint"),
                        column("sensor_model", "string"),
                        column("commissioned_on", "date"),
                        column("status", "string"),
                        column("is_current", "boolean"),
                    ],
                    "invariants": [copy.deepcopy(DEPENDENCY)],
                },
                "semantics": copy.deepcopy(WINDOW_SEMANTICS),
            },
            {
                "exposeId": "reading",
                "kind": "table",
                "binding": _binding("reading"),
                "contract": {
                    "schema": [
                        column("reading_id", "bigint"),
                        column("station_id", "bigint"),
                        column("network_id", "bigint"),
                        column("sensor_model", "string"),
                        column("reading_date", "date"),
                        column("temperature_c", "double"),
                    ]
                },
                "semantics": {"relationships": copy.deepcopy(RELATIONSHIPS)},
            },
        ],
    }


def _render(version: str, expose_id: str) -> Dict[str, Any]:
    return OdcsProvider(version=version).render(_contract(), fmt=None, expose_id=expose_id)


def _extras(odcs: Dict[str, Any]) -> Dict[str, Any]:
    entry = next(p for p in odcs["customProperties"] if p["property"] == FLUID_EXTRAS_PROPERTY)
    value = entry["value"]
    return json.loads(value) if isinstance(value, str) else value


def _queries(odcs: Dict[str, Any]) -> List[str]:
    return [
        rule["query"]
        for obj in odcs["schema"]
        for rule in obj.get("quality") or []
        if rule.get("type") == "sql"
    ]


@pytest.mark.parametrize("version", VERSIONS)
def test_both_exports_validate_against_the_vendored_schema(version: str) -> None:
    schema = validation.load_schema(version)
    for expose_id in ("station_version", "reading"):
        odcs = _render(version, expose_id)
        assert odcs["apiVersion"] == version
        assert validation.collect_errors(odcs, schema) == []


@pytest.mark.parametrize("version", VERSIONS)
def test_the_window_and_the_dependency_ride_in_fluid_extras_unchanged(version: str) -> None:
    extras = _extras(_render(version, "station_version"))
    expose = extras["exposes"]["station_version"]
    assert expose["semantics"] == WINDOW_SEMANTICS
    assert expose["contract"]["invariants"] == [DEPENDENCY]


@pytest.mark.parametrize("version", VERSIONS)
def test_as_of_and_shared_columns_ride_in_fluid_extras_unchanged(version: str) -> None:
    extras = _extras(_render(version, "reading"))
    assert extras["exposes"]["reading"]["semantics"]["relationships"] == RELATIONSHIPS


@pytest.mark.parametrize("version", VERSIONS)
def test_an_import_returns_the_members_exactly(version: str) -> None:
    provider = OdcsProvider(version=version)
    window = provider.import_contract(copy.deepcopy(_render(version, "station_version")))
    version_expose = next(e for e in window["exposes"] if e["exposeId"] == "station_version")
    assert version_expose["semantics"] == WINDOW_SEMANTICS
    assert version_expose["contract"]["invariants"] == [DEPENDENCY]

    readings = provider.import_contract(copy.deepcopy(_render(version, "reading")))
    reading_expose = next(e for e in readings["exposes"] if e["exposeId"] == "reading")
    assert reading_expose["semantics"]["relationships"] == RELATIONSHIPS


@pytest.mark.parametrize("version", VERSIONS)
def test_the_shared_columns_rules_are_sql_checks_with_the_versioned_joins(version: str) -> None:
    queries = _queries(_render(version, "reading"))
    assert len(queries) == 2, queries
    by_entity = next(q for q in queries if "SELECT DISTINCT" in q)
    as_of = next(q for q in queries if "LEAD(" in q)
    assert by_entity is not as_of
    assert "network_id" in by_entity and "sensor_model" in as_of
