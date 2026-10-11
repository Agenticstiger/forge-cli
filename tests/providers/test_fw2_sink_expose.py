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

"""LOGIC-1: the build->expose join for a sink config forge-cli derives.

``resolve_iceberg_sink_exposes`` picks the Iceberg sink exposes from the
build's outputs. Kafka Connect derives one ``iceberg.tables`` entry, so it
resolves exactly one expose; embedded Debezium Server derives one
``table-namespace`` and one catalog, so it resolves several when they share a
database and resolve to one catalog.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build.providers._iceberg_catalog import (
    find_iceberg_expose_binding,
    resolve_iceberg_sink_exposes,
)

pytestmark = [pytest.mark.unit]


def _expose(eid: str, database: str, table: str, **location: Any) -> Dict[str, Any]:
    return {
        "exposeId": eid,
        "binding": {
            "platform": "aws",
            "format": "iceberg",
            "location": {"database": database, "table": table, "bucket": "lake", **location},
        },
    }


def _ids(exposes) -> List[str]:
    return [e["exposeId"] for e in exposes]


def _build(outputs: Optional[Any]) -> Dict[str, Any]:
    build: Dict[str, Any] = {"id": "ingest"}
    if outputs is not None:
        build["outputs"] = outputs
    return build


ORDERS = _expose("orders", "sales", "orders")
REFUNDS = _expose("refunds", "sales", "refunds")
CONFLUENT = {
    "exposeId": "managed",
    "binding": {"platform": "confluent", "format": "iceberg", "location": {"table": "m"}},
}
PARQUET = {"exposeId": "raw", "binding": {"platform": "aws", "format": "parquet"}}


# ── Kafka Connect (one table) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "exposes, outputs, expected",
    [
        ([ORDERS, REFUNDS], ["refunds"], ["refunds"]),
        ([ORDERS, REFUNDS], ["orders"], ["orders"]),
        ([ORDERS, REFUNDS, PARQUET], ["raw", "refunds"], ["refunds"]),
        ([ORDERS], None, ["orders"]),
        ([ORDERS], [], ["orders"]),
        ([ORDERS], "orders", ["orders"]),
        ([CONFLUENT, ORDERS], None, ["orders"]),
    ],
    ids=[
        "second-expose",
        "first-expose",
        "skips-non-iceberg-output",
        "single-no-outputs",
        "single-empty-outputs",
        "outputs-as-a-string",
        "confluent-is-not-a-candidate",
    ],
)
def test_one_table_resolves(exposes, outputs, expected):
    target, error = resolve_iceberg_sink_exposes({"exposes": exposes}, _build(outputs))
    assert error is None
    assert _ids(target) == expected


@pytest.mark.parametrize(
    "exposes, outputs, message",
    [
        ([ORDERS, PARQUET], ["raw"], "outputs ['raw'] name none of the Iceberg sink exposes"),
        ([ORDERS, CONFLUENT], ["managed"], "outputs ['managed'] name none"),
        ([ORDERS, REFUNDS], ["orders", "refunds"], "outputs ['orders', 'refunds'] name 2 of"),
        ([ORDERS, REFUNDS], None, "declares no outputs, and the contract has 2"),
    ],
    ids=["non-iceberg", "confluent-managed", "two-outputs", "no-outputs"],
)
def test_one_table_refuses(exposes, outputs, message):
    target, error = resolve_iceberg_sink_exposes({"exposes": exposes}, _build(outputs))
    assert target == ()
    assert error is not None and message in error
    assert error.startswith("iceberg sink (build 'ingest'): ")
    assert "['orders (sales.orders)'" in error
    assert "a derived Kafka Connect sink writes one expose" in error


def test_no_iceberg_sink_expose_is_left_to_the_validator():
    assert resolve_iceberg_sink_exposes({"exposes": [PARQUET]}, _build(["raw"])) == ((), None)
    assert resolve_iceberg_sink_exposes({}, _build(None), namespace=True) == ((), None)


# ── embedded Debezium Server (one namespace, one catalog) ───────────────────


@pytest.mark.parametrize(
    "exposes, outputs, expected",
    [
        ([ORDERS, REFUNDS], ["orders", "refunds"], ["orders", "refunds"]),
        ([ORDERS, REFUNDS], None, ["orders", "refunds"]),
        ([ORDERS, REFUNDS], ["refunds"], ["refunds"]),
        ([ORDERS, _expose("refunds", "finance", "refunds")], ["refunds"], ["refunds"]),
    ],
    ids=["both-outputs", "no-outputs", "one-output", "one-output-other-database"],
)
def test_namespace_resolves(exposes, outputs, expected):
    target, error = resolve_iceberg_sink_exposes(
        {"exposes": exposes}, _build(outputs), namespace=True
    )
    assert error is None
    assert _ids(target) == expected


def test_namespace_glue_warehouse_is_per_table_not_a_catalog_difference():
    # get_iceberg_warehouse derives s3://lake/sales/<table>/ for each one
    target, error = resolve_iceberg_sink_exposes(
        {"exposes": [ORDERS, REFUNDS]}, _build(None), namespace=True
    )
    assert error is None and len(target) == 2


_LK = {"uri": "http://lakekeeper:8181/catalog", "warehouse": "wh"}


@pytest.mark.parametrize(
    "refunds, message",
    [
        (_expose("refunds", "finance", "refunds"), "sit in the databases ['finance', 'sales']"),
        (
            _expose("refunds", "sales", "refunds", catalog="lakekeeper", **_LK),
            "(their catalog, warehouse, uri differ)",
        ),
        (_expose("refunds", "sales", "refunds", region="eu-west-1"), "(their region differ)"),
    ],
    ids=["two-databases", "two-catalog-kinds", "two-regions"],
)
@pytest.mark.parametrize("outputs", [["orders", "refunds"], None], ids=["outputs", "no-outputs"])
def test_namespace_refuses(refunds, message, outputs):
    target, error = resolve_iceberg_sink_exposes(
        {"exposes": [ORDERS, refunds]}, _build(outputs), namespace=True
    )
    assert target == ()
    assert error is not None and message in error
    assert "a derived Debezium Server sink" in error
    assert "hand-write the sink config (properties.debezium.server.sink.config)" in error


@pytest.mark.parametrize(
    "location, message",
    [
        ({"warehouse": "other"}, "(their warehouse differ)"),
        ({"uri": "http://other:8181/catalog"}, "(their uri differ)"),
        ({"catalog": "polaris"}, "(their catalog differ)"),
    ],
    ids=["warehouse", "uri", "kind"],
)
def test_namespace_rest_catalogs_must_match(location, message):
    orders = _expose("orders", "sales", "orders", catalog="lakekeeper", **_LK)
    refunds = _expose("refunds", "sales", "refunds", **{"catalog": "lakekeeper", **_LK, **location})
    contract = {"exposes": [orders, refunds]}
    target, error = resolve_iceberg_sink_exposes(contract, _build(None), namespace=True)
    assert target == () and error is not None and message in error
    same = {
        "exposes": [orders, _expose("refunds", "sales", "refunds", catalog="lakekeeper", **_LK)]
    }
    assert _ids(resolve_iceberg_sink_exposes(same, _build(None), namespace=True)[0]) == [
        "orders",
        "refunds",
    ]


def test_namespace_outputs_naming_none_are_refused():
    target, error = resolve_iceberg_sink_exposes(
        {"exposes": [ORDERS, PARQUET]}, _build(["raw"]), namespace=True
    )
    assert target == ()
    assert error is not None and "outputs ['raw'] name none of the Iceberg sink exposes" in error


# ── the first-expose helper a config that names its tables is checked against ─


def test_find_iceberg_expose_binding_is_still_the_first_iceberg_sink_expose():
    assert find_iceberg_expose_binding({"exposes": [CONFLUENT, ORDERS, REFUNDS]}) == (
        ORDERS["binding"]
    )
