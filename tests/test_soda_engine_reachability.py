# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``fluid test --engine soda`` must be reachable, and must never fake a pass.

The regression these tests pin: ``render_sodacl`` read
``exposes[].quality.tests[]``, a key that ``$defs.expose``
(``additionalProperties: false`` in 0.7.1–0.7.6) does not define. No contract
that passes ``fluid validate`` could produce a single SodaCL check, so the
engine's only reachable outcome was "nothing to check via Soda" + **exit 0** —
a quality gate reporting green without running anything.

Every schema assertion below is made against the **bundled schema files**, not
against a fixture written by these tests, so a schema change that reintroduces
the divergence fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.exporters.sodacl import render_sodacl, render_sodacl_document

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "fluid_build" / "schemas"
SCHEMA_FILES = sorted(SCHEMA_DIR.glob("fluid-schema-0.7.*.json"))


def _defs(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("$defs") or doc.get("definitions") or {}


# ----------------------------------------------------------------------
# The definition side: where DQ rules are actually allowed to live
# ----------------------------------------------------------------------


@pytest.mark.parametrize("schema_path", SCHEMA_FILES, ids=lambda p: p.stem)
def test_expose_has_no_quality_key_so_it_cannot_be_the_only_source(schema_path):
    """``exposes[].quality`` is not a thing — reading only it means reading nothing."""
    expose = _defs(schema_path)["expose"]
    assert expose["additionalProperties"] is False
    assert "quality" not in expose["properties"]


@pytest.mark.parametrize("schema_path", SCHEMA_FILES, ids=lambda p: p.stem)
def test_dq_rules_are_reachable_from_a_schema_valid_expose(schema_path):
    """``exposes[].contract.dq.rules[]`` is the canonical, schema-valid location."""
    defs = _defs(schema_path)
    assert "dq" in defs["exposeContract"]["properties"]
    assert "rules" in defs["dqSpec"]["properties"]


def _schema_valid_contract(rules):
    """A contract shaped exactly like ``$defs.expose`` allows."""
    return {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "silver.test.v1",
        "exposes": [
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "DB",
                        "schema": "SCH",
                        "table": "CUSTOMERS",
                    },
                },
                "contract": {"dq": {"rules": rules}},
            }
        ],
    }


# ----------------------------------------------------------------------
# The exporter now produces checks for schema-valid contracts
# ----------------------------------------------------------------------


def test_schema_valid_dq_rules_produce_sodacl_checks():
    """The core regression: a validate-clean contract must yield real checks."""
    rendering = render_sodacl_document(
        _schema_valid_contract(
            [
                {
                    "id": "pk_unique",
                    "type": "uniqueness",
                    "selector": "CUSTOMER_ID",
                    "threshold": 1.0,
                    "operator": ">=",
                    "severity": "error",
                }
            ]
        )
    )
    doc = yaml.safe_load(rendering.text)
    assert doc["checks for CUSTOMERS"] == ["duplicate_count(CUSTOMER_ID) = 0"]
    assert rendering.mapped == ["customers.pk_unique"]
    assert rendering.unmapped == []


def test_table_name_comes_from_the_schema_valid_binding_location():
    """``binding.location.table`` — not ``location.properties.table``."""
    rendering = render_sodacl_document(
        _schema_valid_contract(
            [{"id": "r", "type": "completeness", "selector": "NAME", "severity": "error"}]
        )
    )
    assert "checks for CUSTOMERS" in yaml.safe_load(rendering.text)


@pytest.mark.parametrize(
    "rule,expected",
    [
        (
            {"type": "completeness", "selector": "PHONE", "threshold": 1.0, "operator": ">="},
            "missing_count(PHONE) = 0",
        ),
        (
            {"type": "completeness", "selector": "PHONE", "threshold": 0.9, "operator": ">="},
            # 1 - 0.9 in binary float is 0.09999999999999998; the emitted
            # literal must not carry that noise into the SodaCL document.
            "missing_percent(PHONE) <= 10",
        ),
        (
            {"type": "accuracy", "selector": "BAL", "threshold": 0, "operator": ">="},
            "min(BAL) >= 0",
        ),
        (
            # An upper bound is decided by MAX, mirroring the native engine.
            {"type": "accuracy", "selector": "BAL", "threshold": 5000, "operator": "<="},
            "max(BAL) <= 5000",
        ),
        (
            {"type": "anomaly_detection", "selector": "*", "threshold": 1000, "operator": ">="},
            "row_count >= 1000",
        ),
        (
            {"type": "freshness", "selector": "ORDER_DATE", "window": "PT24H"},
            "freshness(ORDER_DATE) < 1d",
        ),
        (
            {"type": "freshness", "selector": "TS", "window": "PT90M"},
            "freshness(TS) < 1h30m",
        ),
    ],
)
def test_dq_rule_types_map_to_the_documented_sodacl_grammar(rule, expected):
    rendering = render_sodacl_document(
        _schema_valid_contract([dict(rule, id="r", severity="error")])
    )
    checks = yaml.safe_load(rendering.text)["checks for CUSTOMERS"]
    assert expected in checks


def test_valid_values_are_read_from_the_description_like_the_native_engine():
    """``$defs.dqRule`` has no ``validValues`` key, so the description carries them."""
    rendering = render_sodacl_document(
        _schema_valid_contract(
            [
                {
                    "id": "seg",
                    "type": "valid_values",
                    "selector": "SEGMENT",
                    "severity": "error",
                    "description": "SEGMENT valid values: A, B, C.",
                }
            ]
        )
    )
    checks = yaml.safe_load(rendering.text)["checks for CUSTOMERS"]
    assert checks == [{"invalid_count(SEGMENT) = 0": {"valid values": ["A", "B", "C"]}}]


# ----------------------------------------------------------------------
# Nothing is dropped on the floor
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        # No SodaCL equivalent at all.
        {"id": "r", "type": "schema", "selector": "X", "severity": "critical"},
        {"id": "r", "type": "drift_detection", "selector": "X", "severity": "error"},
        # Schema-legal type, but the specific shape is not expressible.
        {
            "id": "r",
            "type": "uniqueness",
            "selector": "X",
            "threshold": 0.95,
            "operator": ">=",
            "severity": "error",
        },
        {
            "id": "r",
            "type": "accuracy",
            "selector": "X",
            "threshold": 5,
            "operator": "!=",
            "severity": "error",
        },
        # A gate that checks nothing is a failed gate, not a skipped one.
        {"id": "r", "type": "valid_values", "selector": "X", "severity": "error"},
        {"id": "r", "type": "freshness", "selector": "TS", "severity": "error"},
        {"id": "r", "type": "freshness", "selector": "TS", "window": "PT30S", "severity": "error"},
        {"id": "r", "type": "completeness", "severity": "error"},
    ],
    ids=[
        "schema",
        "drift_detection",
        "partial-uniqueness",
        "not-equal-bound",
        "valid_values-without-values",
        "freshness-without-window",
        "freshness-below-sodacl-resolution",
        "no-selector",
    ],
)
def test_inexpressible_rules_are_reported_not_silently_skipped(rule):
    rendering = render_sodacl_document(_schema_valid_contract([rule]))
    assert rendering.mapped == []
    assert len(rendering.unmapped) == 1
    unmapped = rendering.unmapped[0]
    assert unmapped.rule_id == "r"
    # The reason has to tell the author what to do, not just "unsupported".
    assert len(unmapped.reason) > 20
    assert rendering.declared == 1


def test_expression_quality_rules_are_counted_as_declared_but_unrun():
    """``exposes[].contract.quality[]`` is schema-legal and executed by nobody."""
    contract = _schema_valid_contract([])
    contract["exposes"][0]["contract"]["quality"] = [
        {"rule": "bal_sane", "expression": "BAL > -1e6", "severity": "error"}
    ]
    rendering = render_sodacl_document(contract)
    assert rendering.declared == 1
    assert rendering.unmapped[0].rule_id == "bal_sane"


def test_declared_count_separates_no_rules_from_all_rules_dropped():
    """The distinction the string-sniffing caller could not make."""
    empty = render_sodacl_document(_schema_valid_contract([]))
    assert empty.declared == 0
    assert empty.has_checks is False

    dropped = render_sodacl_document(
        _schema_valid_contract(
            [{"id": "r", "type": "schema", "selector": "X", "severity": "error"}]
        )
    )
    assert dropped.declared == 1
    assert dropped.has_checks is False
    # Both render the same YAML — which is exactly why the caller must not
    # decide the exit code by sniffing the text.
    assert empty.text == dropped.text


# ----------------------------------------------------------------------
# Backwards compatibility
# ----------------------------------------------------------------------


def test_legacy_quality_tests_block_still_renders():
    """Hand-written pre-schema files keep working."""
    out = render_sodacl(
        {
            "exposes": [
                {
                    "id": "orders",
                    "binding": {"location": {"properties": {"table": "ORDERS"}}},
                    "quality": {"tests": [{"type": "not_null", "column": "order_id"}]},
                }
            ]
        }
    )
    assert "missing_count(order_id) = 0" in yaml.safe_load(out)["checks for ORDERS"]


def test_both_sources_merge_into_one_checks_block():
    contract = _schema_valid_contract(
        [{"id": "r", "type": "completeness", "selector": "NAME", "severity": "error"}]
    )
    contract["exposes"][0]["quality"] = {"tests": [{"type": "unique", "column": "CUSTOMER_ID"}]}
    checks = yaml.safe_load(render_sodacl_document(contract).text)["checks for CUSTOMERS"]
    assert "missing_count(NAME) = 0" in checks
    assert "duplicate_count(CUSTOMER_ID) = 0" in checks


def test_legacy_only_contract_still_counts_as_having_checks():
    """A legacy block emits real checks that no rule id can account for.

    Counting only ``mapped`` would make ``declared == 0`` for this contract,
    and the caller would print "nothing to check" and exit 0 while holding a
    document full of checks — the same silent no-op, moved to another key.
    """
    rendering = render_sodacl_document(
        {
            "exposes": [
                {
                    "id": "orders",
                    "binding": {"location": {"properties": {"table": "ORDERS"}}},
                    "quality": {"tests": [{"type": "unique", "column": "ORDER_ID"}]},
                }
            ]
        }
    )
    assert "duplicate_count(ORDER_ID) = 0" in rendering.text
    assert rendering.has_checks is True
    assert rendering.emitted_checks == 1
    assert rendering.declared == 1


def test_a_rule_whose_table_cannot_be_resolved_is_reported_not_dropped():
    """No table name means no ``checks for`` block — so the rule did not run."""
    contract = _schema_valid_contract(
        [{"id": "r", "type": "completeness", "selector": "NAME", "severity": "error"}]
    )
    del contract["exposes"][0]["exposeId"]
    contract["exposes"][0]["binding"]["location"] = {"database": "DB", "schema": "SCH"}
    rendering = render_sodacl_document(contract)
    assert rendering.has_checks is False
    assert [u.rule_id for u in rendering.unmapped] == ["r"]
    assert "binding.location.table" in rendering.unmapped[0].reason
