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

"""A whitespace-only ``location.warehouse`` counts as unset.

``'  '`` used to reach the sink as its warehouse. Now a DynamoDB or JDBC sink
derives the warehouse from the bucket, or validate and the preflight refuse.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog

pytestmark = [pytest.mark.unit]


def _binding(catalog: str, **location: Any) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"database": "streaming", "table": "orders", "catalog": catalog}
    loc["region"] = "eu-west-1"
    if catalog == "jdbc":
        loc["uri"] = "jdbc:postgresql://pg:5432/iceberg"
    loc.update(location)
    return {"platform": "aws", "format": "iceberg", "location": loc}


def _contract(binding: Dict[str, Any], engine: str) -> Dict[str, Any]:
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
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.o",
        "name": "o",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
        "builds": [
            {
                "id": "b1",
                "pattern": "acquisition",
                "engine": engine,
                "outputs": ["orders"],
                "properties": props,
            }
        ],
    }


@pytest.mark.parametrize("catalog", ["dynamodb", "jdbc"])
@pytest.mark.parametrize("blank", ["  ", "\t", ""], ids=["spaces", "tab", "empty"])
def test_a_blank_warehouse_falls_back_to_the_bucket(catalog, blank):
    resolved = resolve_iceberg_catalog(_binding(catalog, bucket="lake", warehouse=blank))
    assert resolved.warehouse == "s3://lake/streaming/orders/"
    dbz = emit_debezium_iceberg_sink_config(resolved)
    assert dbz.get("warehouse") == "s3://lake/streaming/orders/", dbz


@pytest.mark.parametrize("catalog", ["dynamodb", "jdbc"])
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_a_blank_warehouse_and_no_bucket_is_refused(catalog, engine):
    contract = _contract(_binding(catalog, warehouse="  "), engine)
    errors, _warnings = validate_iceberg_sink(contract)
    assert any(f"{catalog} catalog requires binding.location.warehouse" in e for e in errors)
    assert iceberg_sink_preflight(contract, "b1") is not None
