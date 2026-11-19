# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Glue table is partitioned as its binding declares, or the layout is refused.

``binding.location.partitionBy`` never reached the Glue table the AWS emitter wrote,
and ``binding.properties.cluster_by`` was read by nobody on AWS; both passed
``fluid validate --strict``. Now a Hive table's partition columns become its
``partition_keys`` (and leave ``columns``), Athena partition projection is written
when every key's values are known, a layout Glue cannot hold is refused naming the
field, and the writers that put one file under the location refuse a partitioned
table. ``test_iac_aws_partition_plan_moto.py`` runs the real provider's plan.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import aws_layout
from fluid_build.iac.providers.aws_layout import (
    glue_partition_key_changes,
    partition_registration_notes,
    partitioned_hive_table,
    validate_aws_layout,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.aws]

TABLE_KEY = "sales_layout_sales_orders"
TRIGGER = f"{TABLE_KEY}_partitioning"

_SCHEMA = [
    {"name": "id", "type": "string", "required": True},
    {
        "name": "order_date",
        "type": "date",
        "required": True,
        "description": "Day of the order (UTC)",
    },
    {"name": "occurred_at", "type": "timestamp"},
    {"name": "region", "type": "string"},
    {"name": "channel", "type": "enum('web', 'store')", "required": True},
    {"name": "is_test", "type": "boolean", "required": True},
    {
        "name": "shard",
        "type": "int",
        "required": True,
        "validationRules": [{"type": "range", "constraint": "between 0 and 15"}],
    },
    {"name": "customer_id", "type": "bigint"},
    {"name": "amount", "type": "decimal(12,2)"},
    {"name": "score", "type": "double"},
    {"name": "payload", "type": "string"},
    {"name": "attributes", "type": "json"},
    {"name": "Country", "type": "string"},
    {"name": "lines", "type": "array", "element": {"type": "string"}},
]


def _contract(*, partition_by=None, cluster_by=None, fmt="parquet", schema=None, extra=None):
    """A Glue table over Parquet; ``schema`` replaces the default columns."""
    location = {
        "bucket": "acme-lake",
        "path": "silver/orders/",
        "database": "sales",
        "table": "orders",
    }
    if partition_by is not None:
        location["partitionBy"] = partition_by
    binding = {"platform": "aws", "format": fmt, "location": location}
    if cluster_by is not None:
        binding["properties"] = {"cluster_by": cluster_by}
    if extra:
        binding.update(extra)
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "sales.layout",
        "name": "Layout demo",
        "description": "partitioned",
        "domain": "sales",
        "metadata": {"layer": "Silver", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": copy.deepcopy(schema or _SCHEMA)},
            }
        ],
    }


def _resources(contract):
    return get_iac_plugin("aws").emit(contract)


def _table(contract):
    return _resources(contract)["aws_glue_catalog_table"][TABLE_KEY]


def _projection(table):
    return {k: v for k, v in table["parameters"].items() if k.startswith("projection.")}


def _refusal(contract):
    with pytest.raises(UnsupportedBindingError) as caught:
        _resources(contract)
    raised = f"{caught.value} {' '.join(caught.value.remediation)}"
    assert raised in validate_aws_layout(contract)[0], "validate must report the emit refusal"
    return caught.value


# ── Hive tables: partition keys ─────────────────────────────────────────────


def test_partition_columns_become_partition_keys_in_order_and_leave_columns():
    table = _table(_contract(partition_by=["order_date", "channel"]))
    assert table["partition_keys"] == [
        {"name": "order_date", "type": "date", "comment": "Day of the order (UTC)"},
        {"name": "channel", "type": "string"},
    ]
    names = [c["name"] for c in table["storage_descriptor"]["columns"]]
    assert "order_date" not in names and "channel" not in names
    assert names[0] == "id" and len(names) == len(_SCHEMA) - 2


def test_an_unpartitioned_table_is_written_as_before():
    resources = _resources(_contract())
    table = resources["aws_glue_catalog_table"][TABLE_KEY]
    assert "partition_keys" not in table
    assert "terraform_data" not in resources
    assert table["lifecycle"] == {"ignore_changes": ["parameters"]}
    assert not _projection(table)
    assert len(table["storage_descriptor"]["columns"]) == len(_SCHEMA)


@pytest.mark.parametrize("empty", [[], None])
def test_an_empty_partition_by_writes_no_keys(empty):
    assert "partition_keys" not in _table(_contract(partition_by=empty))


@pytest.mark.parametrize("fmt", sorted(aws_layout.HIVE_FORMATS))
def test_every_hive_format_is_partitioned(fmt):
    assert _table(_contract(partition_by=["region"], fmt=fmt))["partition_keys"]


def test_the_layout_trigger_replaces_the_table():
    resources = _resources(_contract(partition_by=["order_date", "channel"]))
    table = resources["aws_glue_catalog_table"][TABLE_KEY]
    assert table["lifecycle"] == {
        "ignore_changes": ["parameters"],
        "replace_triggered_by": [f"terraform_data.{TRIGGER}"],
    }
    trigger = resources["terraform_data"][TRIGGER]["input"]
    assert trigger["partition_keys"] == [["order_date", "date"], ["channel", "string"]]
    assert trigger["projection"] == _projection(table) | {"projection.enabled": "true"}


def test_lake_formation_grants_on_a_partitioned_table_are_replaced_with_it():
    lf = {
        "governance": {
            "lakeFormation": {
                "grants": [
                    {"principal": "arn:aws:iam::123456789012:role/a", "permissions": ["SELECT"]}
                ]
            }
        }
    }
    partitioned = _resources(_contract(partition_by=["region"], extra=lf))
    grants = partitioned["aws_lakeformation_permissions"]
    table_grants = [
        b for b in grants.values() if f"aws_glue_catalog_table.{TABLE_KEY}." in json.dumps(b)
    ]
    assert table_grants
    for body in table_grants:
        assert body["lifecycle"]["replace_triggered_by"] == [f"terraform_data.{TRIGGER}"]
    plain = _resources(_contract(extra=lf))["aws_lakeformation_permissions"]
    assert all("replace_triggered_by" not in json.dumps(b) for b in plain.values())


# ── Athena partition projection ─────────────────────────────────────────────


def test_a_date_key_is_projected_by_day_from_1970_to_now():
    assert _projection(_table(_contract(partition_by=["order_date"]))) == {
        "projection.enabled": "true",
        "projection.order_date.type": "date",
        "projection.order_date.format": "yyyy-MM-dd",
        "projection.order_date.range": "1970-01-01,NOW",
        "projection.order_date.interval": "1",
        "projection.order_date.interval.unit": "DAYS",
    }


def test_enum_boolean_and_integer_keys_are_projected_from_what_the_contract_declares():
    projection = _projection(_table(_contract(partition_by=["channel", "is_test", "shard"])))
    assert projection == {
        "projection.enabled": "true",
        "projection.channel.type": "enum",
        "projection.channel.values": "web,store",
        "projection.is_test.type": "enum",
        "projection.is_test.values": "true,false",
        "projection.shard.type": "integer",
        "projection.shard.range": "0,15",
    }


def test_an_enum_validation_rule_gives_the_values():
    schema = [
        {"name": "id", "type": "string"},
        {
            "name": "region",
            "type": "string",
            "required": True,
            "validationRules": [{"type": "enum", "constraint": "eu | us | eu"}],
        },
    ]
    projection = _projection(_table(_contract(partition_by=["region"], schema=schema)))
    assert projection["projection.region.values"] == "eu,us"  # a level named twice: once


def test_a_key_that_may_be_null_is_not_projected():
    """Its null rows sit under ``__HIVE_DEFAULT_PARTITION__``, which no value names."""
    schema = [{"name": "id", "type": "string"}, {"name": "kind", "type": "enum('a', 'b')"}]
    contract = _contract(partition_by=["kind"], schema=schema)
    assert not _projection(_table(contract))
    assert "because kind may be null (declare required: true)" in (
        partition_registration_notes(contract)[0]
    )


@pytest.mark.parametrize(
    "constraint, expected",
    [
        ("between 0 and 99", "0,99"),
        (">= 1 and <= 9", "1,9"),
        ("> 0 and < 10", "1,9"),
        (">= 0", None),
        ("> 0.5 and < 9", None),
        ("between 9 and 1", None),
    ],
)
def test_an_integer_key_is_projected_only_from_a_range_with_both_whole_bounds(constraint, expected):
    schema = [
        {"name": "id", "type": "string"},
        {
            "name": "bucket",
            "type": "int",
            "required": True,
            "validationRules": [{"type": "range", "constraint": constraint}],
        },
    ]
    projection = _projection(_table(_contract(partition_by=["bucket"], schema=schema)))
    assert projection.get("projection.bucket.range") == expected


def test_one_key_without_known_values_leaves_the_table_unprojected_and_says_so():
    contract = _contract(partition_by=["order_date", "region"])
    assert not _projection(_table(contract))
    (note,) = partition_registration_notes(contract)
    assert note.startswith(
        "exposes[orders]: Glue table sales.orders is partitioned by (order_date, region) "
        "without Athena partition projection, because region may be null"
    )
    assert "MSCK REPAIR TABLE" in note
    assert validate_aws_layout(contract) == ([], [note])
    assert get_iac_plugin("aws").plan_notices(contract) == [note]


def test_values_a_writer_escapes_in_a_path_are_not_projected():
    schema = [
        {"name": "id", "type": "string"},
        {"name": "kind", "type": "enum('a b', 'c')", "required": True},
    ]
    contract = _contract(partition_by=["kind"], schema=schema)
    assert not _projection(_table(contract))
    assert "'a b'" in partition_registration_notes(contract)[0]


def test_a_projected_table_needs_no_registration_and_its_date_cutoff_is_noted():
    assert partition_registration_notes(_contract(partition_by=["channel"])) == []
    (note,) = partition_registration_notes(_contract(partition_by=["order_date", "channel"]))
    assert note == (
        "exposes[orders]: Athena projects the partitions of order_date from 1970-01-01 to "
        "today (UTC), so a row dated later is not read until its day comes."
    )


# ── Refusals ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "column, says",
    [
        ("occurred_at", "of type 'timestamp'"),
        ("amount", "of type 'decimal(12,2)'"),
        ("score", "of type 'double'"),
        ("attributes", "of type 'json'"),
        ("lines", "of type 'array'"),
    ],
)
def test_a_key_hive_cannot_hold_as_a_path_segment_is_refused(column, says):
    refused = _refusal(_contract(partition_by=[column]))
    assert refused.kind == "partition-by"
    assert f"exposes[orders].binding.location.partitionBy names {column!r}, {says}" in str(refused)
    assert "event_date (CAST(ts AS DATE))" in refused.remediation[0]


def test_an_undeclared_key_is_refused_with_the_declared_spelling():
    refused = _refusal(_contract(partition_by=["country"]))
    assert "names 'country', which is not a top-level column" in str(refused)
    assert "(the schema spells it 'Country')" in str(refused)


def test_a_key_named_in_upper_case_is_refused():
    refused = _refusal(_contract(partition_by=["Country"]))
    assert "Hive keeps a partition column's name in lower case" in str(refused)


def test_a_key_named_twice_is_refused():
    assert "names 'region' twice" in str(_refusal(_contract(partition_by=["region", "region"])))


def test_partitioning_by_every_column_is_refused():
    schema = [{"name": "region", "type": "string"}]
    refused = _refusal(_contract(partition_by=["region"], schema=schema))
    assert "names every column of the table" in str(refused)


@pytest.mark.parametrize("raw", ["region", [""], [1], {"region": True}])
def test_a_partition_by_that_is_not_column_names_is_refused(raw):
    assert "it must name the columns to partition by" in str(_refusal(_contract(partition_by=raw)))


@pytest.mark.parametrize("cluster_by", [["region"], "region"])
def test_cluster_by_on_a_glue_table_is_refused(cluster_by):
    refused = _refusal(_contract(cluster_by=cluster_by))
    assert refused.kind == "cluster-by"
    assert "exposes[orders].binding.properties.cluster_by is set on an AWS Glue table" in str(
        refused
    )


def test_partition_by_on_an_iceberg_table_in_glue_is_refused_with_the_fix():
    refused = _refusal(_contract(partition_by=["order_date"], fmt="iceberg"))
    assert "Iceberg keeps its partition spec in its own metadata" in str(refused)
    assert "ADD PARTITION FIELD" in refused.remediation[0]


def test_partition_by_on_a_delta_table_is_refused():
    refused = _refusal(_contract(partition_by=["order_date"], fmt="delta"))
    assert "Delta records its partition columns in its own log" in str(refused)


def test_an_iceberg_table_in_another_catalog_is_left_to_its_sink():
    contract = _contract(
        partition_by=["order_date"],
        fmt="iceberg",
        extra={
            "location": {
                **_contract()["exposes"][0]["binding"]["location"],
                "catalog": "lakekeeper",
                "partitionBy": ["order_date"],
            }
        },
    )
    assert validate_aws_layout(contract)[0] == []


def test_an_aws_binding_with_no_glue_table_is_not_read():
    contract = _contract(partition_by=["nope"])
    del contract["exposes"][0]["binding"]["location"]["table"]
    assert validate_aws_layout(contract) == ([], [])
    assert partitioned_hive_table(contract["exposes"][0]["binding"]) == ()


# ── The data-loss gate ──────────────────────────────────────────────────────


def _plan(before_keys, after_keys, actions=("update",)):
    def state(keys):
        return {
            "database_name": "sales",
            "name": "orders",
            "partition_keys": [{"name": n, "type": t} for n, t in keys],
        }

    return {
        "resource_changes": [
            {
                "type": "aws_glue_catalog_table",
                "change": {
                    "actions": list(actions),
                    "before": state(before_keys),
                    "after": state(after_keys),
                },
            }
        ]
    }


def test_dropped_keys_in_place_are_reported_for_the_gate():
    (line,) = glue_partition_key_changes(_plan([("order_date", "date")], []))
    assert line == (
        "Glue table sales.orders: partition keys change in place ((order_date date) -> none); "
        "the values of order_date are only in the files' <key>=<value>/ paths, so no engine "
        "reads them any more, and reverting the keys restores them"
    )
    assert line in get_iac_plugin("aws").plan_data_changes(_plan([("order_date", "date")], []))


def test_new_keys_in_place_are_reported_for_the_gate():
    (line,) = glue_partition_key_changes(_plan([], [("region", "string")]))
    assert "(none -> (region string))" in line
    assert "not in any of the new partitions" in line


@pytest.mark.parametrize(
    "plan",
    [
        _plan([("a", "string")], [("a", "string")]),
        _plan([("a", "string")], [], actions=("delete", "create")),
        _plan([], [("a", "string")], actions=("create",)),
        {"resource_changes": [{"type": "aws_s3_bucket", "change": {"actions": ["update"]}}]},
        {},
    ],
)
def test_no_key_change_in_place_gives_no_line(plan):
    assert glue_partition_key_changes(plan) == []


# ── Writers that put one file under the location ────────────────────────────


def test_validate_accepts_a_duckdb_build_into_a_partitioned_table():
    """The duckdb runner writes the Hive layout (``test_duckdb_partitioned_landing.py``)."""
    contract = _contract(partition_by=["region"])
    contract["builds"] = [{"id": "land", "engine": "duckdb", "outputs": ["orders"]}]
    assert validate_aws_layout(contract)[0] == []


def test_the_embedded_sql_landing_refuses_a_partitioned_table():
    from fluid_build.build_runners._embedded_sql_io import (
        EmbeddedSqlLandingError,
        object_store_landing,
    )

    contract = _contract(partition_by=["region"])
    with pytest.raises(EmbeddedSqlLandingError) as caught:
        object_store_landing(contract, {"id": "b"})
    assert "cannot land a Glue table partitioned by (region)" in str(caught.value)
    assert object_store_landing(_contract(), {"id": "b"}) is not None


# ── The CLI ─────────────────────────────────────────────────────────────────


#: The columns without the one Glue writes as a fallback (a ``--strict`` warning).
_EXACT = [c for c in _SCHEMA if c["type"] != "json"]


def _run(command, tmp_path, contract, *flags):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    argv = {
        "validate": [str(path), "--offline"],
        "generate": ["iac", str(path), "--provider", "aws", "-o", str(tmp_path / "out")],
    }[command]
    args = build_parser().parse_args([command, *argv, *flags])
    return args.func(args, logging.getLogger("test"))


def test_strict_validate_passes_a_projected_layout_and_refuses_cluster_by(tmp_path, capsys):
    assert (
        _run(
            "validate",
            tmp_path,
            _contract(partition_by=["order_date", "channel"], schema=_EXACT),
            "--strict",
        )
        == 0
    )
    assert _run("validate", tmp_path, _contract(cluster_by=["region"])) == 1
    seen = capsys.readouterr()
    assert "binding.properties.cluster_by is set on an AWS Glue table" in seen.out + seen.err


def test_validate_notes_an_unprojected_table_without_failing_strict(tmp_path, capsys):
    assert (
        _run("validate", tmp_path, _contract(partition_by=["region"], schema=_EXACT), "--strict")
        == 0
    )
    seen = capsys.readouterr()
    assert "without Athena partition projection" in seen.out + seen.err


def test_generate_iac_writes_the_keys_deterministically(tmp_path):
    contract = _contract(partition_by=["order_date", "channel"])
    assert _run("generate", tmp_path, contract) == 0
    module = json.loads(Path(tmp_path / "out" / "main.tf.json").read_text())
    table = module["resource"]["aws_glue_catalog_table"][TABLE_KEY]
    assert [k["name"] for k in table["partition_keys"]] == ["order_date", "channel"]
    assert module["resource"]["terraform_data"][TRIGGER]
    plugin = get_iac_plugin("aws")
    assert build_module(plugin, contract) == build_module(plugin, contract)


# ── fluid verify ────────────────────────────────────────────────────────────


def test_verify_names_registration_for_an_empty_unprojected_table():
    from fluid_build.cli._verify_athena import RULE_EQUAL, _row_count_dimension

    found = _row_count_dimension(0, 3, {"rule": RULE_EQUAL}, "sales.orders", unregistered=True)
    assert found["status"] == "fail"
    assert "run MSCK REPAIR TABLE sales.orders" in found["message"]


def test_verify_holds_a_partitioned_full_refresh_to_at_least_its_rows():
    from fluid_build.cli._verify_athena import (
        RULE_AT_LEAST_RUN,
        _partition_layout,
        _row_count_dimension,
    )

    contract = _contract(partition_by=["order_date"])
    expose = contract["exposes"][0]
    assert _partition_layout("orders", expose, contract) == (True, False)
    assert _partition_layout("orders", _contract()["exposes"][0], _contract()) == (False, False)
    info = {"rule": RULE_AT_LEAST_RUN}
    assert _row_count_dimension(5, 3, info, "sales.orders")["status"] == "pass"
    assert _row_count_dimension(2, 3, info, "sales.orders")["status"] == "fail"
