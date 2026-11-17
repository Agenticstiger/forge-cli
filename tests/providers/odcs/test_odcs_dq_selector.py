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


def _render_schema(schema: List[Dict[str, Any]], *rules: Dict[str, Any]) -> Dict[str, Any]:
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
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "o"}},
                "contract": {"schema": schema, "dq": {"rules": list(rules)}},
            }
        ],
    }
    return OdcsProvider().render(contract, expose_id="orders")


def _all_ids(odcs: Dict[str, Any]) -> List[str]:
    obj = _object(odcs)
    ids = [q["id"] for q in obj.get("quality") or []]
    for p in obj["properties"]:
        ids += [q["id"] for q in p.get("quality") or []]
    return ids


def test_columns_whose_ids_sanitise_alike_each_keep_their_check():
    schema = [{"name": "a b", "type": "VARCHAR"}, {"name": "a_b", "type": "VARCHAR"}]
    odcs = _render_schema(schema, {"id": "r", "type": "completeness", "selector": "a b,a_b"})
    assert len(_declared(odcs, "a b")) == 1 and len(_declared(odcs, "a_b")) == 1
    ids = _all_ids(odcs)
    assert len(ids) == len(set(ids))


def test_a_rule_whose_id_another_rule_produced_is_still_exported():
    odcs = _render(
        {"id": "a-b", "type": "completeness", "selector": "b"},
        {"id": "a", "type": "completeness", "selector": "a,b"},
    )
    assert len(_declared(odcs, "b")) == 2 and len(_declared(odcs, "a")) == 1
    ids = _all_ids(odcs)
    assert len(ids) == len(set(ids))


def test_a_freshness_rule_stays_on_the_column_it_names():
    odcs = _render({"id": "f", "type": "freshness", "selector": "Amount", "window": "PT1H"})
    amount = next(p for p in _object(odcs)["properties"] if p["name"] == "Amount")
    assert [q["id"] for q in amount.get("quality") or []] == ["f"]


def test_re_exporting_a_document_the_old_exporter_wrote_moves_the_rule_not_doubles_it():
    odcs = _render({"id": "ab", "type": "completeness", "selector": "a, b"})
    # What the old exporter wrote: the same rule as one object-level check, none on columns.
    obj = _object(odcs)
    for p in obj["properties"]:
        p["quality"] = [q for q in p.get("quality") or [] if not q["id"].startswith("ab")]
    obj["quality"] = [
        {"id": "ab", "type": "library", "metric": "nullValues", "mustBe": 0},
        *(obj.get("quality") or []),
    ]
    reexport = OdcsProvider().render(OdcsProvider().import_contract(odcs), expose_id="orders")
    assert "ab" not in [q["id"] for q in _object(reexport).get("quality") or []]
    assert [q["id"] for q in _declared(reexport, "a")] == ["ab-a"]
    assert [q["id"] for q in _declared(reexport, "b")] == ["ab-b"]
