# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Nested columns, partitioning and clustering through the real ``tofu`` and provider.

What the data-loss gate decides depends on what terraform-provider-google plans for a
table that already exists, so these apply a table to ``_fake_bigquery.FakeBigQuery``
(the in-process BigQuery stand-in ``test_iac_gcp_governance_plan.py`` uses) and plan
the change. A plan shows a replacement only where the provider forces one: adding a
partition column (BigQuery cannot partition an existing table), or making an existing
column REPEATED. Clustering, a new nested column and a new field inside a RECORD are
in-place updates, and a table that was applied plans no change.

Not proven here: what real BigQuery accepts (the fake stores what it is sent).
Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry or a mirror.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli._apply_opentofu_engine import _data_loss_blocked
from fluid_build.iac import runner

from .test_iac_gcp_governance_plan import (  # noqa: F401 - fake and tofu_env are fixtures
    PROJECT,
    _actions,
    _live,
    _plan,
    _write,
    fake,
    tofu_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.gcp, pytest.mark.provider]

_SKIP = runner.tofu_path() is None
TABLE = "google_bigquery_table.layout_plan_orders"

_SCHEMA = [
    {"name": "id", "type": "string", "required": True},
    {"name": "event_date", "type": "date", "required": True},
    {"name": "region", "type": "string"},
    {"name": "customer_id", "type": "bigint"},
    {"name": "note", "type": "string"},
]

_NESTED = [
    {
        "name": "tags",
        "type": "array",
        "required": True,
        "element": {"type": "string", "required": True},
    },
    {
        "name": "address",
        "type": "struct",
        "description": "postal address",
        "fields": [{"name": "street", "type": "string"}, {"name": "zip", "type": "string"}],
    },
    {
        "name": "items",
        "type": "array",
        "required": True,
        "element": {
            "type": "struct",
            "required": True,
            "fields": [
                {"name": "sku", "type": "string", "required": True},
                {"name": "qty", "type": "integer"},
            ],
        },
    },
    {
        "name": "attrs",
        "type": "map",
        "required": True,
        "key": {"type": "string", "required": True},
        "value": {"type": "string"},
    },
]


def _contract(
    *,
    schema: Optional[List[Dict[str, Any]]] = None,
    partition_by: Optional[List[str]] = None,
    cluster_by: Optional[List[str]] = None,
    retention: Optional[str] = None,
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "project": PROJECT,
        "dataset": "sales",
        "table": "orders",
        "region": "europe-west1",
    }
    if partition_by is not None:
        location["partitionBy"] = partition_by
    binding: Dict[str, Any] = {"platform": "gcp", "format": "bigquery_table", "location": location}
    if cluster_by is not None:
        binding["properties"] = {"cluster_by": cluster_by}
    exposure: Dict[str, Any] = {
        "exposeId": "orders",
        "binding": binding,
        "contract": {"schema": copy.deepcopy(schema if schema is not None else _SCHEMA)},
    }
    if retention:
        exposure["lifecycle"] = {"retention": retention, "expire": True}
    return {"fluidVersion": "0.7.7", "id": "layout.plan", "exposes": [exposure]}


def _replan(contract, workdir, fake, env) -> Dict[str, Any]:  # noqa: F811
    _write(contract, workdir, fake.endpoint)
    return _plan(workdir, env)


def _stored(fake) -> Dict[str, Any]:  # noqa: F811
    return fake.tables[(PROJECT, "sales", "orders")]


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_nested_partitioned_clustered_table_applies_and_plans_no_change(
    tmp_path, fake, tofu_env  # noqa: F811
):
    contract = _contract(
        schema=_SCHEMA + _NESTED, partition_by=["event_date"], cluster_by=["region", "customer_id"]
    )
    _live(contract, tmp_path, fake, tofu_env)
    stored = _stored(fake)
    assert stored["timePartitioning"]["type"] == "DAY"
    assert stored["timePartitioning"]["field"] == "event_date"
    assert "expirationMs" not in stored["timePartitioning"]
    assert stored["clustering"] == {"fields": ["region", "customer_id"]}
    fields = {f["name"]: f for f in stored["schema"]["fields"]}
    assert (fields["tags"]["type"], fields["tags"]["mode"]) == ("STRING", "REPEATED")
    assert (fields["address"]["type"], [f["name"] for f in fields["address"]["fields"]]) == (
        "RECORD",
        ["street", "zip"],
    )
    assert (fields["items"]["type"], fields["items"]["mode"]) == ("RECORD", "REPEATED")
    assert [(f["name"], f["mode"]) for f in fields["attrs"]["fields"]] == [
        ("key", "REQUIRED"),
        ("value", "NULLABLE"),
    ]

    again = _replan(contract, tmp_path, fake, tofu_env)
    assert again["summary"] == {"add": 0, "change": 0, "remove": 0}, again["summary"]


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
@pytest.mark.parametrize(
    "before, after",
    [(None, ["region", "customer_id"]), (["region", "customer_id"], ["customer_id", "region"])],
    ids=["added", "reordered"],
)
def test_clustering_a_live_table_is_an_in_place_update(
    tmp_path, fake, tofu_env, before, after  # noqa: F811
):
    _live(_contract(cluster_by=before), tmp_path, fake, tofu_env)
    plan = _replan(_contract(cluster_by=after), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"]["remove"] == 0
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert plan["changes"][TABLE]["change"]["after"]["clustering"] == after


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_partitioning_a_live_table_plans_its_replacement_and_the_gate_refuses_it(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """BigQuery cannot partition an existing table: a contract that already declared
    partitionBy without retention plans this replacement once the emitter reads it."""
    _live(_contract(), tmp_path, fake, tofu_env)
    plan = _replan(_contract(partition_by=["event_date"]), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=True)
    after = plan["changes"][TABLE]["change"]["after"]["time_partitioning"][0]
    assert (after["type"], after["field"]) == ("DAY", "event_date")


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_retention_on_a_table_partitioned_by_the_same_column_is_in_place(
    tmp_path, fake, tofu_env  # noqa: F811
):
    _live(_contract(partition_by=["event_date"]), tmp_path, fake, tofu_env)
    plan = _replan(
        _contract(partition_by=["event_date"], retention="P30D"), tmp_path, fake, tofu_env
    )
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"]["remove"] == 0
    after = plan["changes"][TABLE]["change"]["after"]["time_partitioning"][0]
    assert after["expiration_ms"] == 30 * 86_400_000
