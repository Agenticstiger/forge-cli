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

"""A derived sink names its table's namespace from ``location.dataset`` on GCP.

A GCP binding names its BigQuery dataset in ``location.dataset``. The sink
read only ``location.database``, so it wrote ``iceberg.tables=None.<table>``
and ``fluid validate`` passed. A derived sink whose expose names no namespace
is now refused.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog

pytestmark = [pytest.mark.unit]


def _gcp(table: Optional[str] = "events", **location: Any) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"catalog": "bigquery", "project": "acme-proj", "bucket": "lake"}
    if table is not None:
        loc["table"] = table
    loc.update(location)
    return {"platform": "gcp", "format": "iceberg", "location": loc}


def _contract(
    bindings: List[Dict[str, Any]],
    engine: str,
    *,
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "source": {"kind": "postgres", "mode": "incremental_append", "streams": ["public.o"]},
        "sink": {"format": "iceberg"},
    }
    if engine == "debezium":
        props["debezium"] = {"deployment": {"mode": "embedded"}, "server": {"sink": {}}}
    else:
        props["kafka-connect"] = {
            "deployment": {"mode": "bring-your-own", "server_url": "http://c:8083"}
        }
        if overrides:
            props["kafka-connect"]["iceberg_catalog_overrides"] = dict(overrides)
    exposes = [
        {
            "exposeId": f"e{i}",
            "kind": "table",
            "binding": copy.deepcopy(b),
            "contract": {"schema": [{"name": "id", "type": "integer"}]},
        }
        for i, b in enumerate(bindings)
    ]
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.o",
        "name": "o",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp"}},
        "exposes": exposes,
        "builds": [
            {
                "id": "b1",
                "pattern": "acquisition",
                "engine": engine,
                "outputs": [e["exposeId"] for e in exposes],
                "properties": props,
            }
        ],
    }


def _namespace_errors(errors: List[str]) -> List[str]:
    return [e for e in errors if "names no table namespace" in e]


def test_a_gcp_dataset_names_the_kafka_connect_table():
    resolved = resolve_iceberg_catalog(_gcp(dataset="analytics"))
    assert resolved.fq_table == "analytics.events"
    cfg = emit_iceberg_sink_config(resolved, product_id="bronze.o", topics=["public.o"])
    assert cfg["iceberg.tables"] == "analytics.events"


def test_a_gcp_dataset_names_the_debezium_namespace():
    resolved = resolve_iceberg_catalog(_gcp(dataset="analytics"))
    assert emit_debezium_iceberg_sink_config(resolved)["table-namespace"] == "analytics"


def test_location_database_wins_over_dataset():
    resolved = resolve_iceberg_catalog(_gcp(database="raw", dataset="analytics"))
    assert resolved.fq_table == "raw.events"


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_a_gcp_dataset_validates(engine):
    errors, _warnings = validate_iceberg_sink(_contract([_gcp(dataset="analytics")], engine))
    assert _namespace_errors(errors) == [], errors


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_a_derived_sink_with_no_namespace_is_refused(engine):
    contract = _contract([_gcp()], engine)
    errors, _warnings = validate_iceberg_sink(contract)
    assert len(_namespace_errors(errors)) == 1, errors
    assert "binding.location.dataset" in _namespace_errors(errors)[0]
    assert iceberg_sink_preflight(contract, "b1") is not None


def test_a_derived_kafka_connect_sink_with_no_table_is_refused():
    errors, _warnings = validate_iceberg_sink(
        _contract([_gcp(table=None, dataset="analytics")], "kafka-connect")
    )
    assert any("names no table, so the derived iceberg.tables" in e for e in errors), errors


def test_an_override_that_names_the_tables_is_not_refused():
    contract = _contract(
        [_gcp()], "kafka-connect", overrides={"iceberg.tables": "analytics.events"}
    )
    errors, _warnings = validate_iceberg_sink(contract)
    assert _namespace_errors(errors) == [], errors


def test_debezium_exposes_sharing_one_dataset_are_one_namespace():
    contract = _contract(
        [_gcp(dataset="analytics"), _gcp(table="refunds", dataset="analytics")],
        "debezium",
    )
    errors, _warnings = validate_iceberg_sink(contract)
    assert not [e for e in errors if "databases" in e or "names no table" in e], errors
