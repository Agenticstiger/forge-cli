# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Nested BigQuery columns through the real ``tofu`` and terraform-provider-google.

What the data-loss gate decides depends on what the provider plans for a table that
already exists, so these apply a table to ``_fake_bigquery.FakeBigQuery`` (the
in-process BigQuery stand-in ``test_iac_gcp_governance_plan.py`` uses) and plan the
change. A table with REPEATED and RECORD columns plans no change once applied, a new
nested column and a new field inside a RECORD are in-place updates, and an existing
column made REPEATED plans a replacement, which the provider forces and the gate sees.

Not proven here: what real BigQuery accepts (the fake stores what it is sent).
Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry or a mirror.
"""

from __future__ import annotations

import copy
import json
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
TABLE = "google_bigquery_table.nested_plan_orders"

_SCHEMA = [
    {"name": "id", "type": "string", "required": True},
    {"name": "event_date", "type": "date", "required": True},
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
    return {"fluidVersion": "0.7.7", "id": "nested.plan", "exposes": [exposure]}


def _replan(contract, workdir, fake, env) -> Dict[str, Any]:  # noqa: F811
    _write(contract, workdir, fake.endpoint)
    return _plan(workdir, env)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_nested_table_applies_and_plans_no_change(tmp_path, fake, tofu_env):  # noqa: F811
    contract = _contract(_SCHEMA + _NESTED)
    _live(contract, tmp_path, fake, tofu_env)
    fields = {f["name"]: f for f in fake.tables[(PROJECT, "sales", "orders")]["schema"]["fields"]}
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
def test_a_new_nested_column_and_a_new_record_field_are_in_place(
    tmp_path, fake, tofu_env  # noqa: F811
):
    _live(_contract(_SCHEMA + _NESTED[1:2]), tmp_path, fake, tofu_env)
    grown = copy.deepcopy(_NESTED[1:2])
    grown[0]["fields"].append({"name": "city", "type": "string"})
    plan = _replan(_contract(_SCHEMA + grown + _NESTED[:1]), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"]["remove"] == 0
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)
    schema = json.loads(plan["changes"][TABLE]["change"]["after"]["schema"])
    assert [f["name"] for f in schema][-2:] == ["address", "tags"]


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_making_an_existing_column_repeated_plans_its_replacement(
    tmp_path, fake, tofu_env  # noqa: F811
):
    """NULLABLE to REPEATED is a change BigQuery cannot make in place; the provider
    forces the replacement, and the gate sees the removal."""
    _live(_contract(), tmp_path, fake, tofu_env)
    schema = copy.deepcopy(_SCHEMA)
    schema[-1] = {
        "name": "note",
        "type": "array",
        "required": True,
        "element": {"type": "string", "required": True},
    }
    plan = _replan(_contract(schema), tmp_path, fake, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
