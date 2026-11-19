# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Decimal precision and scale through the real ``tofu`` and terraform-provider-google.

What the data-loss gate decides depends on what the provider plans for a table that
already exists, so these apply a table to ``_fake_bigquery.FakeBigQuery`` (the
in-process BigQuery stand-in ``test_iac_gcp_governance_plan.py`` uses) and plan the
change. The provider plans a precision added to a live NUMERIC as an in-place update,
which BigQuery refuses (a narrowing). A table written before decimals carried
parameters is replaced instead, once, which the gate sees: the contract's marker is
created then. After that, a new decimal column, a changed precision and a decimal made
bare are in-place updates, and a decimal moved to BIGNUMERIC is a replacement the
provider forces.

Not proven here: what real BigQuery accepts (the fake stores what it is sent); its API
documents no change of a column's data type.
Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry or a mirror.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli._apply_opentofu_engine import _data_loss_blocked
from fluid_build.iac import get_iac_plugin, runner

from .test_iac_gcp_governance_plan import (  # noqa: F401 - fake and tofu_env are fixtures
    PROJECT,
    _actions,
    _apply,
    _init,
    _live,
    _plan,
    _write,
    fake,
    tofu_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.gcp, pytest.mark.provider]

_SKIP = runner.tofu_path() is None
TABLE = "google_bigquery_table.decimal_plan_orders"
MARKER = "terraform_data.decimal_plan_decimal_parameters"

_SCHEMA = [
    {"name": "id", "type": "string", "required": True},
    {"name": "amount", "type": "decimal(18,4)"},
]


def _contract(schema: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    exposure: Dict[str, Any] = {
        "exposeId": "orders",
        "binding": {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {
                "project": PROJECT,
                "dataset": "sales",
                "table": "orders",
                "region": "europe-west1",
            },
        },
        "contract": {"schema": copy.deepcopy(schema if schema is not None else _SCHEMA)},
    }
    return {"fluidVersion": "0.7.7", "id": "decimal.plan", "exposes": [exposure]}


def _with(*columns: Dict[str, Any]) -> Dict[str, Any]:
    return _contract([*_SCHEMA[:1], *columns])


def _strip_parameters(fields: List[Dict[str, Any]]) -> None:
    for field in fields:
        field.pop("precision", None)
        field.pop("scale", None)
        _strip_parameters(field.get("fields") or [])


def _live_as_written_before(contract, workdir: Path, fake, env) -> None:  # noqa: F811
    """Apply ``contract`` as the emitter wrote it before decimals carried parameters:
    every decimal a bare NUMERIC, and no trigger."""
    _write(contract, workdir, fake.endpoint)
    module = json.loads((workdir / "main.tf.json").read_text())
    resources = module["resource"]
    resources.pop("terraform_data", None)
    for table in resources["google_bigquery_table"].values():
        table.pop("lifecycle", None)
        fields = json.loads(table["schema"])
        _strip_parameters(fields)
        table["schema"] = json.dumps(fields)
    (workdir / "main.tf.json").write_text(json.dumps(module))
    _init(workdir, env)
    _plan(workdir, env)
    _apply(workdir, env)


def _replan(contract, workdir, fake, env) -> Dict[str, Any]:  # noqa: F811
    _write(contract, workdir, fake.endpoint)
    return _plan(workdir, env)


def _stored_fields(fake) -> Dict[str, Dict[str, Any]]:  # noqa: F811
    stored = fake.tables[(PROJECT, "sales", "orders")]
    return {f["name"]: f for f in stored["schema"]["fields"]}


def _after_fields(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    schema = plan["changes"][TABLE]["change"]["after"]["schema"]
    return {f["name"]: f for f in json.loads(schema)}


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_table_with_parameterised_decimals_applies_and_plans_no_change(
    tmp_path, fake, tofu_env  # noqa: F811
):
    contract = _with(
        {"name": "amount", "type": "decimal(18,4)"},
        {"name": "wide", "type": "decimal(40,20)"},
        {"name": "whole", "type": "decimal(10)"},
        {"name": "fee", "type": "struct", "fields": [{"name": "value", "type": "decimal(12,2)"}]},
    )
    _live(contract, tmp_path, fake, tofu_env)
    stored = _stored_fields(fake)
    assert (stored["amount"]["type"], stored["amount"]["precision"]) == ("NUMERIC", "18")
    assert stored["amount"]["scale"] == "4"
    assert (stored["wide"]["type"], stored["wide"]["precision"]) == ("BIGNUMERIC", "40")
    assert stored["wide"]["scale"] == "20"
    assert stored["whole"]["precision"] == "10" and "scale" not in stored["whole"]
    assert stored["fee"]["fields"][0]["precision"] == "12"

    again = _replan(contract, tmp_path, fake, tofu_env)
    assert again["summary"] == {"add": 0, "change": 0, "remove": 0}, again["summary"]


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_table_whose_decimal_was_written_bare_plans_its_replacement_and_the_gate_refuses_it(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """A table applied before decimals carried parameters holds a bare NUMERIC; adding
    the declared precision in place is a narrowing BigQuery refuses."""
    _live_as_written_before(_contract(), tmp_path, fake, tofu_env)
    assert "precision" not in _stored_fields(fake)["amount"]
    plan = _replan(_contract(), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=True)
    assert _after_fields(plan)["amount"]["precision"] == "18"


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_new_decimal_column_is_an_in_place_update(tmp_path, fake, tofu_env):  # noqa: F811
    _live(_contract(), tmp_path, fake, tofu_env)
    plan = _replan(
        _with(_SCHEMA[1], {"name": "fee", "type": "decimal(10,2)"}), tmp_path, fake, tofu_env
    )
    assert _actions(plan, TABLE) == ["update"]
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert _after_fields(plan)["fee"]["precision"] == "10"


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_wider_precision_is_an_in_place_update(tmp_path, fake, tofu_env):  # noqa: F811
    _live(_with({"name": "amount", "type": "decimal(10,2)"}), tmp_path, fake, tofu_env)
    plan = _replan(_with({"name": "amount", "type": "decimal(12,2)"}), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert _after_fields(plan)["amount"]["precision"] == "12"


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_decimal_moved_to_bignumeric_plans_the_replacement_the_provider_forces(
    tmp_path, fake, tofu_env  # noqa: F811
):
    _live(_contract(), tmp_path, fake, tofu_env)
    plan = _replan(_with({"name": "amount", "type": "decimal(40,20)"}), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_table_without_parameters_is_left_alone_when_the_marker_arrives(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """Every contract with a table gains the marker once; only a table that names it
    is replaced."""
    contract = _with({"name": "amount", "type": "decimal"})
    _live_as_written_before(contract, tmp_path, fake, tofu_env)
    plan = _replan(contract, tmp_path, fake, tofu_env)
    assert plan["summary"] == {"add": 1, "change": 0, "remove": 0}, plan["summary"]
    assert _actions(plan, MARKER) == ["create"]
    assert _actions(plan, TABLE) == ["no-op"]
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_the_first_decimal_column_of_a_live_table_is_an_in_place_update(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """The marker is already there, so the table's first parameterised decimal does not
    create it, which would replace the table."""
    note = {"name": "note", "type": "string"}
    _live(_with(note), tmp_path, fake, tofu_env)
    plan = _replan(_with(note, {"name": "fee", "type": "decimal(10,2)"}), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"]["remove"] == 0
    assert _after_fields(plan)["fee"]["precision"] == "10"


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_decimal_made_bare_is_an_in_place_update_the_gate_passes(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """The marker stays when no table names it, so the plan removes nothing."""
    _live(_contract(), tmp_path, fake, tofu_env)
    plan = _replan(_with({"name": "amount", "type": "decimal"}), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"] == {"add": 0, "change": 1, "remove": 0}, plan["summary"]
    assert "precision" not in _after_fields(plan)["amount"]


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_adopting_a_table_with_parameterised_decimals_plans_its_replacement(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """A known limit, which the partitioning trigger shares: a table adopted into an empty
    state (``fluid apply`` imports what ``discover_imports`` names, never a
    ``terraform_data``) gets the marker created beside it, and that create replaces the
    table even though it already holds the declared precision and scale."""
    _live(_contract(), tmp_path, fake, tofu_env)
    workdir = str(tmp_path)
    forgotten = runner.tofu_state_rm(
        workdir, runner.tofu_state_list(workdir, env=tofu_env), env=tofu_env
    )
    assert forgotten.ok, forgotten.stderr
    for block in get_iac_plugin("gcp").discover_imports(_contract()):
        done = runner.tofu_import(workdir, block.to, block.id, env=tofu_env)
        assert done.ok, done.stderr or done.stdout
    assert _stored_fields(fake)["amount"]["precision"] == "18"
    plan = _replan(_contract(), tmp_path, fake, tofu_env)
    assert _actions(plan, MARKER) == ["create"]
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
