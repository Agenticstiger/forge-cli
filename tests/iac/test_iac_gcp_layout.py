# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A BigQuery table is partitioned and clustered as its binding declares.

``binding.location.partitionBy`` was read only with retention, so a table that
declared it and no ``lifecycle`` was created unpartitioned, and
``binding.properties.cluster_by`` (honoured on Snowflake) was read by nobody on
BigQuery. Both passed ``fluid validate --strict``. Now a partition column
partitions the table by day with or without retention, ``cluster_by`` becomes its
``clustering``, and what BigQuery cannot lay out as declared is refused, naming the
field. ``test_iac_gcp_layout_plan.py`` runs the real provider's plan on a live table.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers import gcp_governance as gov
from fluid_build.iac.providers.gcp import validate_gcp_binding

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.gcp]

_SCHEMA = [
    {"name": "id", "type": "string", "required": True},
    {"name": "event_date", "type": "date", "required": True},
    {"name": "occurred_at", "type": "timestamp"},
    {"name": "region", "type": "string"},
    {"name": "customer_id", "type": "bigint"},
    {"name": "score", "type": "double"},
    {"name": "payload", "type": "json"},
    {
        "name": "tags",
        "type": "array",
        "required": True,
        "element": {"type": "string", "required": True},
    },
]


def _contract(*, partition_by=None, cluster_by=None, lifecycle=None, fmt="bigquery_table"):
    location = {"project": "acme", "dataset": "sales", "table": "orders"}
    if fmt == "bigquery_view":
        location = {"project": "acme", "dataset": "sales", "view": "orders_v", "query": "SELECT 1"}
    if partition_by is not None:
        location["partitionBy"] = partition_by
    binding = {"platform": "gcp", "format": fmt, "location": location}
    if cluster_by is not None:
        binding["properties"] = {"cluster_by": cluster_by}
    exposure = {
        "exposeId": "orders",
        "kind": "table",
        "binding": binding,
        "contract": {"schema": copy.deepcopy(_SCHEMA)},
    }
    if lifecycle is not None:
        exposure["lifecycle"] = lifecycle
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "sales.layout",
        "name": "Layout demo",
        "description": "partitioned and clustered",
        "domain": "sales",
        "metadata": {"layer": "Silver", "owner": {"team": "data"}},
        "exposes": [exposure],
    }


def _resources(contract):
    return get_iac_plugin("gcp").emit(contract)


def _table(contract):
    return next(iter(_resources(contract)["google_bigquery_table"].values()))


def _errors(contract):
    return validate_gcp_binding(contract)[0] + validate_governance(contract)[0]


def _refusal(contract):
    with pytest.raises(UnsupportedBindingError) as caught:
        _resources(contract)
    raised = f"{caught.value} {' '.join(caught.value.remediation)}"
    assert raised in _errors(contract), "validate must report the emit refusal word for word"
    return caught.value


# ── Partitioning ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("column", ["event_date", "occurred_at"])
def test_a_partition_column_without_retention_partitions_the_table_by_day(column):
    resources = _resources(_contract(partition_by=[column]))
    table = next(iter(resources["google_bigquery_table"].values()))
    assert table["time_partitioning"] == {"type": "DAY", "field": column}
    trigger = next(iter(resources["terraform_data"]))
    assert resources["terraform_data"][trigger] == {"input": {"type": "DAY", "field": column}}
    assert table["lifecycle"] == {"replace_triggered_by": [f"terraform_data.{trigger}"]}


def test_retention_on_the_same_column_keeps_the_trigger_so_it_is_an_in_place_change():
    plain = _resources(_contract(partition_by=["event_date"]))
    retained = _resources(
        _contract(partition_by=["event_date"], lifecycle={"retention": "P30D", "expire": True})
    )
    assert plain["terraform_data"] == retained["terraform_data"]
    table = next(iter(retained["google_bigquery_table"].values()))
    assert table["time_partitioning"] == {
        "type": "DAY",
        "field": "event_date",
        "expiration_ms": 30 * 86_400_000,
    }


def test_retention_without_a_partition_column_is_written_as_before():
    table = _table(_contract(lifecycle={"retention": "P7D", "expire": True}))
    assert table["time_partitioning"] == {"type": "DAY", "expiration_ms": 7 * 86_400_000}


def test_no_partition_column_and_no_retention_writes_no_partitioning():
    resources = _resources(_contract())
    table = next(iter(resources["google_bigquery_table"].values()))
    assert "time_partitioning" not in table and "lifecycle" not in table
    assert "clustering" not in table
    assert not [name for name in resources.get("terraform_data", {}) if "partitioning" in name]


@pytest.mark.parametrize(
    "partition_by, says",
    [
        (["region"], "names 'region', which is not a DATE, TIMESTAMP or DATETIME column"),
        (["nope"], "names 'nope', which is not a DATE, TIMESTAMP or DATETIME column"),
        (["tags"], "names 'tags', which is not a DATE, TIMESTAMP or DATETIME column"),
        (["event_date", "region"], "is ['event_date', 'region']; a BigQuery table is partitioned"),
    ],
    ids=["string-column", "undeclared", "repeated", "two-columns"],
)
def test_a_partition_column_bigquery_cannot_partition_by_is_refused_by_path(partition_by, says):
    error = _refusal(_contract(partition_by=partition_by))
    assert error.kind == "partition-column"
    assert str(error).startswith("exposes[orders].binding.location.partitionBy ")
    assert says in str(error)
    assert "cluster" in " ".join(error.remediation)
    assert "ingestion time" not in " ".join(error.remediation)


def test_with_retention_the_partition_refusal_keeps_its_retention_wording():
    error = _refusal(
        _contract(partition_by=["region"], lifecycle={"retention": "P30D", "expire": True})
    )
    assert error.kind == "retention-partition-column"
    assert "drop partitionBy to partition by ingestion time" in " ".join(error.remediation)


# ── Clustering ──────────────────────────────────────────────────────────────


def test_cluster_by_becomes_clustering_in_the_declared_order():
    table = _table(_contract(cluster_by=["region", "customer_id"]))
    assert table["clustering"] == ["region", "customer_id"]
    assert "lifecycle" not in table, "clustering updates in place: no replacement trigger"


def test_one_cluster_column_may_be_written_as_a_string_as_on_snowflake():
    assert _table(_contract(cluster_by="region"))["clustering"] == ["region"]


def test_four_cluster_columns_are_held_and_a_fifth_is_refused():
    four = ["region", "customer_id", "event_date", "occurred_at"]
    assert _table(_contract(cluster_by=four))["clustering"] == four
    error = _refusal(_contract(cluster_by=four + ["id"]))
    assert error.kind == "cluster-by"
    assert "names 5 columns; BigQuery clusters a table by at most 4" in str(error)


@pytest.mark.parametrize(
    "cluster_by, says",
    [
        (["score"], "cluster_by[0] names 'score', whose type is FLOAT64"),
        (["payload"], "cluster_by[0] names 'payload', whose type is JSON"),
        (["region", "tags"], "cluster_by[1] names 'tags', whose type is REPEATED STRING"),
        (["region", "nope"], "cluster_by[1] names 'nope', which is not a column of the expose's"),
        (["region", "region"], "cluster_by[1] names 'region' again"),
    ],
    ids=["float", "json", "repeated", "undeclared", "duplicate"],
)
def test_a_column_bigquery_cannot_cluster_by_is_refused_by_path(cluster_by, says):
    error = _refusal(_contract(cluster_by=cluster_by))
    assert error.kind == "cluster-by"
    assert f"exposes[orders].binding.properties.{says}" in str(error)
    assert "BIGNUMERIC, BOOL, DATE, DATETIME, GEOGRAPHY, INT64, NUMERIC, STRING or TIMESTAMP" in (
        str(error)
    )


def test_a_column_named_in_another_case_is_refused_with_the_declared_spelling():
    """A binding names its columns as the schema declares them; the refusal says how."""
    contract = _contract(cluster_by=["Region"], partition_by=["EVENT_DATE"])
    error = _refusal(contract)
    assert "partitionBy names 'EVENT_DATE'" in str(error)
    assert "(the schema spells it 'event_date')" in str(error)
    error = _refusal(_contract(cluster_by=["Region"]))
    assert "(the schema spells it 'region')" in str(error)


@pytest.mark.parametrize("cluster_by", [{"region": 1}, ["region", 3], [""]])
def test_a_cluster_by_that_is_not_column_names_is_refused(cluster_by):
    error = _refusal(_contract(cluster_by=cluster_by))
    assert error.kind == "cluster-by" and "it must name the columns" in str(error)


def test_an_empty_cluster_by_writes_no_clustering():
    assert "clustering" not in _table(_contract(cluster_by=[]))


def test_the_clusterable_types_are_the_ones_bigquery_documents():
    assert gov._CLUSTERING_COLUMN_TYPES == {
        "BIGNUMERIC",
        "BOOL",
        "DATE",
        "DATETIME",
        "GEOGRAPHY",
        "INT64",
        "NUMERIC",
        "STRING",
        "TIMESTAMP",
    }
    assert gov.CLUSTERING_MAX_COLUMNS == 4


# ── Views ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "layout, says",
    [
        ({"partition_by": ["event_date"]}, "binding.location.partitionBy is set"),
        ({"cluster_by": ["region"]}, "binding.properties.cluster_by is set"),
        (
            {"partition_by": ["event_date"], "cluster_by": ["region"]},
            "binding.location.partitionBy and binding.properties.cluster_by are set",
        ),
    ],
    ids=["partition", "cluster", "both"],
)
def test_layout_on_a_view_is_refused(layout, says):
    error = _refusal(_contract(fmt="bigquery_view", **layout))
    assert error.kind == "layout-view"
    assert f"exposes[orders].{says} on a BigQuery view" in str(error)


# ── The module, and the CLI ─────────────────────────────────────────────────


def test_the_module_is_deterministic_and_carries_partitioning_and_clustering():
    contract = _contract(partition_by=["event_date"], cluster_by=["region", "customer_id"])
    first = build_module(get_iac_plugin("gcp"), contract)
    assert first == build_module(get_iac_plugin("gcp"), copy.deepcopy(contract))
    module = json.loads(first)
    table = next(iter(module["resource"]["google_bigquery_table"].values()))
    assert table["time_partitioning"] == {"type": "DAY", "field": "event_date"}
    assert table["clustering"] == ["region", "customer_id"]


def _run(command, tmp_path, contract, *flags):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    argv = {"validate": [str(path), "--offline"], "generate": ["iac", str(path)]}[command]
    args = build_parser().parse_args([command, *argv, *flags])
    return args.func(args, logging.getLogger("test"))


def test_strict_validate_refuses_an_unclusterable_column_and_passes_a_good_layout(tmp_path, capsys):
    good = _contract(partition_by=["event_date"], cluster_by=["region", "customer_id"])
    assert _run("validate", tmp_path, good, "--strict") == 0
    bad = _contract(partition_by=["event_date"], cluster_by=["score"])
    assert _run("validate", tmp_path, bad) == 1
    seen = capsys.readouterr()
    assert "cluster_by[0] names 'score', whose type is FLOAT64" in seen.out + seen.err
