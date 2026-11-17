# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where an exported dq rule lands: on the column it checks, as ``fluid test`` reads it.

``util/dq_checks.py`` reads ``selector`` as a column, or several comma-separated: a
completeness or valid_values rule checks each one, a uniqueness rule checks one (a list
could mean each column or their combination), and a rule that selects no column (none,
``*``) or one the schema does not declare is refused. The ODCS export put a rule whose
selector was not exactly one property name on the schema object, a ``nullValues`` check
naming no column, which no ODCS consumer can run as the rule meant.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from fluid_build.providers.odcs import OdcsProvider

pytestmark = pytest.mark.unit


def _render(*rules: Dict[str, Any]) -> Dict[str, Any]:
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "name": "p",
        "metadata": {"owner": {"team": "t"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "orders.parquet"},
                },
                "contract": {
                    "schema": [
                        {"name": "a", "type": "VARCHAR"},
                        {"name": "b", "type": "VARCHAR"},
                        {"name": "Amount", "type": "DECIMAL(12,2)"},
                    ],
                    "dq": {"rules": list(rules)},
                },
            }
        ],
    }
    return OdcsProvider().render(contract, expose_id="orders")


def _object(odcs: Dict[str, Any]) -> Dict[str, Any]:
    return odcs["schema"][0]


def _declared(odcs: Dict[str, Any], prop: str) -> List[Dict[str, Any]]:
    for p in _object(odcs)["properties"]:
        if p["name"] == prop:
            return [q for q in p.get("quality") or [] if q.get("type") == "library"]
    raise AssertionError(prop)


def _object_library(odcs: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [q for q in _object(odcs).get("quality") or [] if q.get("type") == "library"]


def test_a_completeness_rule_on_two_columns_checks_each():
    odcs = _render({"id": "ab", "type": "completeness", "selector": "a, b"})
    a, b = _declared(odcs, "a"), _declared(odcs, "b")
    assert [q["metric"] for q in a] == ["nullValues"] and [q["metric"] for q in b] == ["nullValues"]
    assert a[0]["id"] != b[0]["id"]
    assert not _object_library(odcs)


def test_a_valid_values_rule_on_two_columns_checks_each():
    odcs = _render({"id": "ab", "type": "valid_values", "selector": "a,b"})
    assert [q["metric"] for q in _declared(odcs, "a")] == ["invalidValues"]
    assert [q["metric"] for q in _declared(odcs, "b")] == ["invalidValues"]


def test_a_single_column_rule_keeps_its_id():
    odcs = _render({"id": "a_complete", "type": "completeness", "selector": "a"})
    assert [q["id"] for q in _declared(odcs, "a")] == ["a_complete"]


def test_a_selector_in_another_case_finds_its_column():
    odcs = _render({"id": "amt", "type": "completeness", "selector": "amount"})
    assert [q["metric"] for q in _declared(odcs, "Amount")] == ["nullValues"]


@pytest.mark.parametrize(
    "rule,needle",
    [
        ({"id": "x", "type": "completeness"}, "selects no column"),
        ({"id": "x", "type": "completeness", "selector": "*"}, "selects no column"),
        ({"id": "x", "type": "completeness", "selector": "nope"}, "'nope'"),
        ({"id": "x", "type": "uniqueness", "selector": "a, b"}, "one column"),
    ],
)
def test_a_rule_without_its_columns_is_not_exported_object_level(rule, needle, caplog):
    with caplog.at_level(logging.WARNING):
        odcs = _render(rule)
    assert not _object_library(odcs)
    assert not any(_declared(odcs, c) for c in ("a", "b", "Amount"))
    assert any(needle in r.getMessage() for r in caplog.records), caplog.text
