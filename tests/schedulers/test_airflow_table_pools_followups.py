# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Table pools, second round: a build that writes several tables is refused, ``dagId`` is not
needed to opt in, and generated DAG file names are compared without regard to case."""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.cli.validate import contract_rule_findings
from fluid_build.schedulers.airflow import fluid_apply
from fluid_build.schema_manager import FluidSchemaManager
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT

pytestmark = pytest.mark.unit

RAW = "s3://lake/iceberg/raw/events"
DAILY = "s3://lake/iceberg/curated/events_daily"


def _pooled(**airflow: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["orchestration"] = {
        "engine": "airflow",
        "airflow": {"poolPerOutputTable": True, **airflow},
    }
    return contract


def _two_tables() -> Dict[str, Any]:
    contract = _pooled()
    contract["builds"][1]["outputs"] = ["events_raw", "events_daily"]
    return contract


def _render(contract: Dict[str, Any]) -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(contract, env=None, contract_path="c.fluid.yaml")


def _no_outputs() -> Dict[str, Any]:
    """One scheduled build that names no outputs, so it writes every expose."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "orchestration": {"engine": "airflow", "airflow": {"poolPerOutputTable": True}},
        "builds": [{"id": "only", "execution": {"trigger": {"cron": "@daily"}}}],
        "exposes": [
            {
                "exposeId": "x",
                "binding": {
                    "platform": "gcp",
                    "location": {"project": "pr", "dataset": "d", "table": "x"},
                },
            },
            {
                "exposeId": "y",
                "binding": {
                    "platform": "gcp",
                    "location": {"project": "pr", "dataset": "d", "table": "y"},
                },
            },
        ],
    }


# ── several tables in one build ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("contract", [_two_tables(), _no_outputs()], ids=["outputs", "no outputs"])
def test_a_build_that_writes_several_tables_is_refused_by_the_renderer(contract):
    with pytest.raises(fluid_apply.ScheduleRenderError) as raised:
        _render(contract)
    message = str(raised.value)
    assert "writes 2 tables" in message
    assert "one output per build" in message and "split the build" in message


def test_the_refusal_names_the_tables():
    with pytest.raises(fluid_apply.ScheduleRenderError) as raised:
        _render(_two_tables())
    message = str(raised.value)
    assert f"events_raw ({RAW})" in message and f"events_daily ({DAILY})" in message


@pytest.mark.parametrize("contract", [_two_tables(), _no_outputs()], ids=["outputs", "no outputs"])
def test_validate_reports_the_same_build(contract):
    errors = contract_rule_findings(contract, plugins=False).errors
    (ours,) = [e for e in errors if "poolPerOutputTable" in e]
    assert "writes 2 tables" in ours


def test_one_output_per_build_and_the_flag_off_both_pass():
    contract = _pooled()  # daily-curated writes events_daily only
    assert fluid_apply.table_pool_errors(contract) == []
    assert _render(contract)
    off = _two_tables()
    off["orchestration"]["airflow"]["poolPerOutputTable"] = False
    assert fluid_apply.table_pool_errors(off) == [] and _render(off)


def test_a_non_boolean_value_is_a_validate_finding_too():
    errors = fluid_apply.table_pool_errors(_pooled(poolPerOutputTable="yes"))
    assert errors and "must be true or false" in errors[0]


# ── dagId is not needed ─────────────────────────────────────────────────────────────────────


def test_a_contract_with_only_the_flag_validates_against_the_0_7_7_schema():
    contract = _pooled()
    assert contract["orchestration"]["airflow"] == {"poolPerOutputTable": True}
    result = FluidSchemaManager().validate_contract(
        contract, schema_version="0.7.7", offline_only=True
    )
    assert not [e for e in result.errors if "dagId" in e or "poolPerOutputTable" in e]


def test_a_dag_id_that_is_given_is_still_checked():
    result = FluidSchemaManager().validate_contract(
        _pooled(dagId="bad id!"), schema_version="0.7.7", offline_only=True
    )
    assert [e for e in result.errors if "dagId" in e]


# ── file names compare without regard to case ───────────────────────────────────────────────


def test_dag_file_names_that_differ_only_in_case_are_refused():
    contract = copy.deepcopy(SPARK_CONTRACT)
    other = copy.deepcopy(contract["builds"][1])
    other["id"] = "Daily-Curated"
    other["outputs"] = ["events_raw"]
    contract["builds"].append(other)
    with pytest.raises(fluid_apply.ScheduleRenderError, match="without regard to case"):
        _render(contract)


def test_file_names_that_differ_in_more_than_case_are_not_refused():
    contract = copy.deepcopy(SPARK_CONTRACT)
    other = copy.deepcopy(contract["builds"][1])
    other["id"] = "hourly-curated"
    other["outputs"] = ["events_raw"]
    contract["builds"].append(other)
    assert len(_render(contract)) == 2
