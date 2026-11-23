# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A DAG id and a task id in the 0.7.7 preview follow the ASCII part of Airflow's rule for them.

Airflow takes letters and digits (``\\w`` in Python is Unicode), underscore, dot and dash, at
most 250 characters (``KEY_REGEX = ^[\\w.-]+$`` and ``validate_key``). The FLUID schema used to
take lowercase only, so a DAG called ``Sales_Daily_V2`` could not be written down as it is
spelled. The preview now takes the ASCII ids Airflow takes. Every id the lowercase rule took is
still taken, and 0.7.6 (a released version) is unchanged.

The pattern ends in ``(?![\\s\\S])``, not ``$``, so every validator ends it where the value
ends: stock jsonschema runs ``pattern`` with Python's ``re.search``, where ``$`` also matches
before a final line break, and so took ``"abc\\n"`` where an ECMA-262 validator refuses it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

import fluid_build
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

_SCHEMAS = Path(fluid_build.__file__).parent / "schemas"
_LOWERCASE_ONLY = "^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$"
#: What the pattern was before it ended in ``(?![\\s\\S])``: Python's ``$`` takes a final ``\\n``.
_ENDED_IN_DOLLAR = "^[A-Za-z0-9_.-]+$"
_ID_FIELDS = [("airflowOrchestration", "dagId"), ("airflowTask", "taskId")]


def _contract(dag_id: str, task_id: str = "extract", version: str = "0.7.7") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.sales.daily",
        "name": "Daily sales",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "sales-platform", "email": "sales-platform@example.org"},
        },
        "orchestration": {
            "engine": "airflow",
            "airflow": {
                "dagId": dag_id,
                "tasks": [{"taskId": task_id, "type": "bash"}],
            },
        },
        "exposes": [
            {
                "exposeId": "daily_sales",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "data/daily_sales.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
    }


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _refusals(manager: FluidSchemaManager, contract: Dict[str, Any]) -> List[Any]:
    result = manager.validate_contract(contract, offline_only=True)
    return [] if result.is_valid else (list(result.errors) or ["refused, with no message"])


def _id_schema(version: str, definition: str, field: str) -> Dict[str, Any]:
    schema = json.loads((_SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    declared: Dict[str, Any] = schema["$defs"][definition]["properties"][field]
    return declared


ACCEPTED = [
    "Sales_Daily_V2",
    "SALES",
    "sales_daily",
    "sales.daily-v2",
    "Sales.Daily-V2",
    "_leading_underscore",
    "-leading-dash",
    ".leading.dot",
    "trailing-",
    "a",
    "A",
    "0",
    "x" * 250,
]

REFUSED = [
    "",
    "Sales Daily",
    " Sales",
    "Sales ",
    "sales/daily",
    "sales\\daily",
    "Sales:Daily",
    "Sales\nDaily",
    "Sales_Daily\n",
    "sales$daily",
    "sales@daily",
    "x" * 251,
    "x" * 1000,
]


def test_the_fixture_is_valid(manager):
    assert _refusals(manager, _contract("sales_daily")) == []


@pytest.mark.parametrize("dag_id", ACCEPTED)
def test_a_dag_id_may_be_any_id_airflow_takes(manager, dag_id):
    assert _refusals(manager, _contract(dag_id)) == []


@pytest.mark.parametrize("task_id", ACCEPTED)
def test_a_task_id_may_be_any_id_airflow_takes(manager, task_id):
    assert _refusals(manager, _contract("sales_daily", task_id)) == []


@pytest.mark.parametrize("dag_id", REFUSED)
def test_a_dag_id_airflow_would_refuse_is_refused(manager, dag_id):
    assert _refusals(manager, _contract(dag_id))


@pytest.mark.parametrize("task_id", REFUSED)
def test_a_task_id_airflow_would_refuse_is_refused(manager, task_id):
    assert _refusals(manager, _contract("sales_daily", task_id))


@pytest.mark.parametrize("non_ascii", ["Salès", "売上", "sales daily", "Ｓales"])
def test_the_class_is_ascii_not_unicode_word_characters(manager, non_ascii):
    # Python's re reads \w as Unicode, other JSON Schema validators do not; an explicit
    # ASCII class gives every validator the same answer.
    assert _refusals(manager, _contract(non_ascii))
    assert _refusals(manager, _contract("sales_daily", non_ascii))


@pytest.mark.parametrize("definition,field", _ID_FIELDS)
def test_the_ids_carry_airflows_rule_and_length_limit(definition, field):
    declared = _id_schema("0.7.7", definition, field)
    assert declared["pattern"] == "^[A-Za-z0-9_.-]+(?![\\s\\S])"
    assert declared["maxLength"] == 250
    assert declared["minLength"] == 1


@pytest.mark.parametrize("definition,field", _ID_FIELDS)
def test_the_ids_say_they_take_the_ascii_part_of_airflows_rule(definition, field):
    assert (
        "The ASCII part of Airflow's rule" in _id_schema("0.7.7", definition, field)["description"]
    )


def _stock(definition: str, field: str) -> Any:
    """The id's sub-schema run as stock jsonschema runs it: ``pattern`` through Python's
    ``re.search`` with no rewrite of ``$``, which is how a tool that does not read the end
    anchor as ECMA-262 runs it (``fluid_build.schema_manager.schema_validator`` rewrites it)."""
    import jsonschema

    return jsonschema.Draft202012Validator(_id_schema("0.7.7", definition, field))


@pytest.mark.parametrize("definition,field", _ID_FIELDS)
@pytest.mark.parametrize("value", ["abc\n", "a.b-C_1\n", "Sales_Daily_V2\n"])
def test_an_id_that_ends_in_a_line_break_is_refused_by_stock_jsonschema_too(
    definition, field, value
):
    assert not _stock(definition, field).is_valid(value)
    # the pattern it replaced let it through: that is what this guards against
    assert re.search(_ENDED_IN_DOLLAR, value)


@pytest.mark.parametrize("definition,field", _ID_FIELDS)
def test_stock_jsonschema_takes_and_refuses_the_ids_fluid_does(definition, field):
    stock = _stock(definition, field)
    for value in ACCEPTED:
        assert stock.is_valid(value), f"stock jsonschema refused {value!r}"
    for value in REFUSED:
        assert not stock.is_valid(value), f"stock jsonschema took {value!r}"
    assert stock.is_valid("a.b-C_1") and not stock.is_valid("")
    assert stock.is_valid("x" * 250) and not stock.is_valid("x" * 251)


def test_everything_the_lowercase_rule_took_is_still_taken():
    old = re.compile(_LOWERCASE_ONLY)
    new = re.compile(_id_schema("0.7.7", "airflowOrchestration", "dagId")["pattern"])
    alphabet = "az09_.-"
    candidates = {""}
    for _ in range(5):
        candidates |= {c + a for c in candidates for a in alphabet}
    accepted_before = [c for c in candidates if old.search(c)]
    assert accepted_before
    assert all(new.search(c) for c in accepted_before)


@pytest.mark.parametrize("definition,field", _ID_FIELDS)
def test_0_7_6_is_unchanged(definition, field):
    assert _id_schema("0.7.6", definition, field)["pattern"] == _LOWERCASE_ONLY


def test_0_7_6_still_refuses_capitals(manager):
    assert _refusals(manager, _contract("Sales_Daily_V2", version="0.7.6"))
    assert _refusals(manager, _contract("sales_daily", version="0.7.6")) == []
