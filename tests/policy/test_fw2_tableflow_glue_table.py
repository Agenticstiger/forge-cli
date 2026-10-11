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

"""A Confluent Tableflow expose is granted the Glue table Tableflow publishes.

The Tableflow IaC names that table for the Kafka topic (``_topic_name``:
topic > table > exposeId) in ``location.database``, or with no database in
the database Tableflow names after the Kafka cluster id. The compiler used
to read only ``location.table``: a grant on a table nothing publishes, or,
with no table, a database-wide ``glue.table`` grant with ``table: null``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import pytest

from fluid_build.iac.providers.confluent import ConfluentIacPlugin
from fluid_build.policy.compiler import SAFE_GLUE_PERMS, compile_policy

pytestmark = [pytest.mark.unit]

_LOCATION: Dict[str, Any] = {
    "environment_id": "env-123",
    "kafka_cluster_id": "lkc-123",
    "database": "sales_glue",
    "bucket": "acme-tableflow",
    "confluent_role_arn": "arn:aws:iam::123456789012:role/tableflow",
    "region": "eu-west-1",
}


def _contract(permissions: Sequence[str] = ("read",), **location: Any) -> Dict[str, Any]:
    loc = {**_LOCATION, **location}
    loc = {k: v for k, v in loc.items() if v is not None}
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.tableflow",
        "name": "Tableflow orders",
        "metadata": {"layer": "Bronze"},
        "accessPolicy": {
            "grants": [
                {"principal": "group:analysts@example.com", "permissions": list(permissions)}
            ]
        },
        "exposes": [
            {
                "exposeId": "tableflow_orders",
                "kind": "table",
                "binding": {"platform": "confluent", "format": "iceberg", "location": loc},
                "contract": {"schema": [{"name": "order_id", "type": "integer"}]},
            }
        ],
    }


def _glue(bindings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [b for b in bindings if b["resource_type"] == "glue.table"]


def _published_topic(contract: Dict[str, Any]) -> str:
    resources = ConfluentIacPlugin().emit(contract)
    return next(iter(resources["confluent_tableflow_topic"].values()))["display_name"]


@pytest.mark.parametrize(
    "location, table",
    [
        ({"topic": "orders.v1", "table": "orders"}, "orders.v1"),
        ({"topic": "orders"}, "orders"),
        ({"table": "orders"}, "orders"),
        ({}, "tableflow_orders"),
    ],
    ids=["topic-and-table", "topic-only", "table-only", "neither"],
)
def test_the_glue_grant_names_the_table_tableflow_publishes(location, table):
    contract = _contract(**location)
    bindings, warnings = compile_policy(contract)
    assert warnings == []
    assert _published_topic(contract) == table
    [glue] = _glue(bindings)
    assert (glue["resource_id"], glue["database"], glue["table"]) == (
        f"sales_glue.{table}",
        "sales_glue",
        table,
    )
    assert glue["actions"] == SAFE_GLUE_PERMS["readData"]
    assert [b["resource_id"] for b in bindings if b["resource_type"] == "s3.bucket"] == [
        "acme-tableflow"
    ]


def test_no_database_grants_the_cluster_id_database():
    bindings, warnings = compile_policy(_contract(topic="orders", database=None))
    assert warnings == []
    [glue] = _glue(bindings)
    assert (glue["resource_id"], glue["database"], glue["table"]) == (
        "lkc-123.orders",
        "lkc-123",
        "orders",
    )


def test_no_database_and_no_cluster_id_warns_instead_of_granting_a_database():
    bindings, warnings = compile_policy(
        _contract(topic="orders", database=None, kafka_cluster_id=None)
    )
    assert _glue(bindings) == []
    assert [b["resource_type"] for b in bindings] == ["s3.bucket"]
    assert len(warnings) == 1
    assert "'tableflow_orders'" in warnings[0]
    assert "no Glue table grant was compiled" in warnings[0]


def test_a_write_grant_manages_the_published_table():
    bindings, _ = compile_policy(_contract(permissions=["write"], topic="orders"))
    [glue] = _glue(bindings)
    assert glue["resource_id"] == "sales_glue.orders"
    assert glue["actions"] == SAFE_GLUE_PERMS["manage"]


def test_no_tableflow_glue_grant_has_a_null_table():
    for location in ({}, {"topic": "orders"}, {"database": None}):
        bindings, _ = compile_policy(_contract(**location))
        assert all(b["table"] for b in _glue(bindings)), bindings
