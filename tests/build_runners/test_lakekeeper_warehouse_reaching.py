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

"""The by-name warehouse check reads the warehouse that reaches the sink.

A name-addressed catalog (Lakekeeper, Polaris, Unity) is refused an
object-store URI not only in ``binding.location.warehouse`` but also when an
override or a hand-written config pushes one, and with leading whitespace.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import validate_iceberg_sink
from fluid_build.iac.iceberg_validation import validate_iceberg_bindings

pytestmark = [pytest.mark.unit]


def _binding(kind: str, *, platform: str = "local", **location: Any) -> Dict[str, Any]:
    loc = {"database": "default", "table": "events", "catalog": kind}
    loc.update(location)
    return {"platform": platform, "format": "iceberg", "location": loc}


def _contract(
    binding: Dict[str, Any],
    *,
    kc: Optional[Dict[str, Any]] = None,
    debezium: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "source": {"kind": "postgres", "mode": "incremental_append"},
        "sink": {"format": "iceberg"},
    }
    engine = "kafka-connect"
    if debezium is not None:
        engine = "debezium"
        props["debezium"] = debezium
    else:
        props["kafka-connect"] = kc or {}
    return {
        "id": "b.x",
        "exposes": [{"exposeId": "events", "kind": "table", "binding": binding}],
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": engine,
                "outputs": ["events"],
                "properties": props,
            }
        ],
    }


def _by_name(errors):
    return [e for e in errors if "addresses a warehouse by NAME" in e]


_LK = {"uri": "http://c:8181/catalog", "warehouse": "analytics"}


def test_an_override_warehouse_uri_is_refused():
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://lake/wh"}}
    errors = _by_name(validate_iceberg_sink(_contract(_binding("lakekeeper", **_LK), kc=kc))[0])
    assert len(errors) == 1, errors
    assert "iceberg_catalog_overrides iceberg.catalog.warehouse is 's3://lake/wh'" in errors[0]


def test_a_hand_written_kafka_connect_warehouse_uri_is_refused():
    kc = {"sink_connector_config": {"iceberg.catalog.warehouse": "gs://lake/wh"}}
    errors = _by_name(validate_iceberg_sink(_contract(_binding("polaris", **_LK), kc=kc))[0])
    assert len(errors) == 1, errors
    assert "sink_connector_config iceberg.catalog.warehouse is 'gs://lake/wh'" in errors[0]


def test_a_debezium_server_sink_config_warehouse_uri_is_refused():
    debezium = {
        "deployment": {"mode": "embedded"},
        "server": {"sink": {"config": {"warehouse": "s3://lake/wh"}}},
    }
    errors = _by_name(
        validate_iceberg_sink(_contract(_binding("unity", **_LK), debezium=debezium))[0]
    )
    assert len(errors) == 1, errors
    assert "debezium.server.sink.config warehouse is 's3://lake/wh'" in errors[0]


def test_an_override_warehouse_name_is_accepted():
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "analytics"}}
    assert _by_name(validate_iceberg_sink(_contract(_binding("lakekeeper", **_LK), kc=kc))[0]) == []


def test_leading_whitespace_does_not_hide_a_uri():
    binding = _binding("lakekeeper", uri="http://c:8181/catalog", warehouse=" s3://lake/wh")
    assert len(_by_name(validate_iceberg_sink(_contract(binding))[0])) == 1


@pytest.mark.parametrize(
    "kind, offered", [("lakekeeper", True), ("polaris", False), ("unity", False)]
)
def test_only_lakekeeper_is_offered_the_project_form(kind, offered):
    binding = _binding(kind, uri="http://c:8181/catalog", warehouse="s3://lake/wh")
    errors = _by_name(validate_iceberg_sink(_contract(binding))[0])
    assert len(errors) == 1, errors
    assert ("<project-id>/<name>" in errors[0]) is offered


@pytest.mark.parametrize("kind, offered", [("lakekeeper", False), ("nessie", True)])
def test_the_gcp_check_offers_gs_only_to_catalogs_that_take_a_location(kind, offered):
    binding = _binding(kind, platform="gcp", uri="http://c:8181/catalog", warehouse="s3://lake/wh")
    errors = [
        e
        for e in validate_iceberg_bindings(_contract(binding))[0]
        if "not Google Cloud Storage" in e
    ]
    assert len(errors) == 1, errors
    assert ("gs://<bucket>" in errors[0]) is offered
