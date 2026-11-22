# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``orchestration.airflow.poolPerOutputTable``: the writers of one output table share a one-slot
Airflow pool, across DAGs. Off unless the contract asks for it."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Dict

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.util import airflow_pools
from tests.cli._schedule_dag_fixtures import load_dag
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT

pytestmark = pytest.mark.unit

DAILY_TABLE = "s3://lake/iceberg/curated/events_daily"


def _contract(**airflow: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(SPARK_CONTRACT)
    if airflow:
        contract["orchestration"] = {"engine": "airflow", "airflow": {"dagId": "x", **airflow}}
    return contract


def _daily(contract: Dict[str, Any]) -> str:
    files = fluid_apply.render_fluid_apply_dags(contract, env=None, contract_path="c.fluid.yaml")
    (name,) = [n for n in files if "daily" in n]
    return files[name]


# ── off by default ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("airflow", [{}, {"poolPerOutputTable": False}])
def test_a_dag_has_no_pool_unless_the_contract_asks_for_one(airflow):
    source = _daily(_contract(**airflow))
    assert "pool" not in source and airflow_pools.CONSTANT not in source


# ── on ──────────────────────────────────────────────────────────────────────────────────────


def test_the_task_that_writes_a_table_runs_in_the_pool_of_that_table(monkeypatch):
    source = _daily(_contract(poolPerOutputTable=True))
    loaded = load_dag(source, monkeypatch)
    ((task),) = loaded.tasks
    pool = airflow_pools.pool_name(DAILY_TABLE)
    assert (task["pool"], task["pool_slots"]) == (pool, 1)
    assert loaded.namespace[airflow_pools.CONSTANT] == {
        pool: airflow_pools.pool_description(DAILY_TABLE)
    }
    # The generated text, as a reader of the file sees it.
    assert re.search(rf"^FLUID_TABLE_POOLS = \{{\n    '{pool}': '[^']+',\n\}}$", source, re.M)
    assert f"        pool='{pool}',\n        pool_slots=1,\n" in source.replace('"', "'")


def test_a_non_boolean_value_is_refused():
    with pytest.raises(fluid_apply.ScheduleRenderError, match="poolPerOutputTable must be true"):
        _daily(_contract(poolPerOutputTable="yes"))


def test_a_build_that_writes_no_asset_gets_no_pool(monkeypatch):
    contract = _contract(poolPerOutputTable=True)
    contract["exposes"][1]["binding"]["location"]["bucket"] = "{{ env.LAKE }}"
    source = _daily(contract)
    assert "pool" not in source


def test_two_products_that_write_one_table_share_its_pool():
    other = _contract(poolPerOutputTable=True)
    other["id"] = "another.product"
    assert (
        airflow_pools.declared_pools(_daily(other))
        == airflow_pools.declared_pools(_daily(_contract(poolPerOutputTable=True)))
        != {}
    )


# ── the name ────────────────────────────────────────────────────────────────────────────────


def test_a_pool_name_is_stable_and_readable():
    name = airflow_pools.pool_name(DAILY_TABLE)
    assert name == airflow_pools.pool_name(DAILY_TABLE)
    # Pinned: a change to the scheme would orphan every pool already made.
    assert re.fullmatch(r"fluid_tbl_s3_lake_iceberg_curated_events_daily_[0-9a-f]{10}", name)
    assert name.endswith("_" + hashlib.sha256(DAILY_TABLE.encode()).hexdigest()[:10])


@pytest.mark.parametrize(
    "table",
    [
        DAILY_TABLE,
        "bigquery://my-project/my_dataset/orders",
        "snowflake://xy12345.eu-central-1/DB/S/T",
        "s3://lake/" + "very/" * 80 + "deep",
        "file:///data/out/a b=c~d+e.parquet",
        "////",
    ],
)
def test_a_name_fits_airflow_whatever_the_table_is_called(table):
    name = airflow_pools.pool_name(table)
    assert re.fullmatch(r"[A-Za-z0-9_]{1,64}", name)
    assert re.fullmatch(r"[A-Za-z0-9 _.:/-]{1,200}", airflow_pools.pool_description(table))


def test_tables_that_differ_only_in_punctuation_get_different_pools():
    assert airflow_pools.pool_name("s3://lake/a-b") != airflow_pools.pool_name("s3://lake/a_b")


def test_a_hash_only_table_name_still_has_a_readable_part():
    assert airflow_pools.pool_name("////").startswith("fluid_tbl_table_")


# ── reading the file back ───────────────────────────────────────────────────────────────────


def test_only_a_literal_dict_of_safe_strings_is_read_back():
    good = f"{airflow_pools.CONSTANT} = {{'p_1': 'fluid: one writer for x'}}\n"
    assert airflow_pools.declared_pools(good) == {"p_1": "fluid: one writer for x"}
    for text in (
        f"{airflow_pools.CONSTANT} = {{'bad name; rm': 'd'}}\n",
        f"{airflow_pools.CONSTANT} = {{'p': 'a \"; rm -rf ~ #'}}\n",
        f"{airflow_pools.CONSTANT} = dict(a='b')\n",
        f"{airflow_pools.CONSTANT} = ['p']\n",
        "def broken(:\n",
        "",
    ):
        assert airflow_pools.declared_pools(text) == {}, text


def test_the_commands_are_airflow_pool_set_lines_in_name_order():
    assert airflow_pools.set_commands({"b": "second one", "a": "first one"}) == [
        'airflow pools set a 1 "first one"',
        'airflow pools set b 1 "second one"',
    ]


# ── the schema ──────────────────────────────────────────────────────────────────────────────


def _pool_errors(contract: Dict[str, Any]) -> list:
    result = FluidSchemaManager().validate_contract(
        contract, schema_version="0.7.7", offline_only=True
    )
    return [e for e in result.errors if "poolPerOutputTable" in e]


def test_the_0_7_7_schema_takes_the_boolean_and_refuses_another_type():
    assert _pool_errors(_contract(poolPerOutputTable=True)) == []
    assert _pool_errors(_contract(poolPerOutputTable=False)) == []
    (error,) = _pool_errors(_contract(poolPerOutputTable="yes"))
    assert "boolean" in error


# ── generate schedule ───────────────────────────────────────────────────────────────────────


def test_generate_schedule_prints_the_pool_commands_only_when_a_dag_uses_pools(capsys):
    from fluid_build.cli.generate_schedule import _print_pools

    on = fluid_apply.render_fluid_apply_dags(
        _contract(poolPerOutputTable=True), env=None, contract_path="c.fluid.yaml"
    )
    _print_pools(on)
    out = capsys.readouterr().out
    assert "airflow pools set fluid_tbl_s3_lake_iceberg_curated_events_daily_" in out
    assert out.count("airflow pools set") == 1

    off = fluid_apply.render_fluid_apply_dags(_contract(), env=None, contract_path="c.fluid.yaml")
    _print_pools(off)
    assert capsys.readouterr().out == ""
