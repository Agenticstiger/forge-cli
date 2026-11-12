# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""SodaCL export of ``valid_values`` rules from the column's own declarations, and of
multi-column selectors."""

from __future__ import annotations

import yaml

from fluid_build.exporters.sodacl import render_sodacl_document

COLUMNS = [
    {"name": "status", "validationRules": [{"type": "enum", "constraint": "A | B"}]},
    {"name": "code", "validationRules": [{"type": "regex", "constraint": "^x$"}]},
    {"name": "pct", "validationRules": [{"type": "range", "constraint": ">= 0 and <= 100"}]},
    {"name": "pos", "validationRules": [{"type": "range", "constraint": "> 0"}]},
    {"name": "site", "labels": {"references": "acme.sites.site_id"}},
    {"name": "tier", "validationRules": [{"type": "enum", "constraint": "P | Q"}]},
]


def render(rules):
    contract = {
        "exposes": [
            {
                "id": "e1",
                "binding": {
                    "platform": "databricks",
                    "location": {"database": "c", "schema": "s", "table": "t"},
                },
                "contract": {"schema": COLUMNS, "dq": {"rules": rules}},
            }
        ]
    }
    result = render_sodacl_document(contract)
    checks = (yaml.safe_load(result.text) or {}).get("checks for t", [])
    return result, checks


def valid_values(rule_id, selector, **extra):
    return {"id": rule_id, "type": "valid_values", "selector": selector, **extra}


def test_enum_becomes_valid_values():
    result, checks = render([valid_values("r", "status")])
    assert checks == [{"invalid_count(status) = 0": {"valid values": ["A", "B"]}}]
    assert result.mapped == ["e1.r"]


def test_regex_becomes_valid_regex():
    _, checks = render([valid_values("r", "code")])
    assert checks == [{"invalid_count(code) = 0": {"valid regex": "^x$"}}]


def test_inclusive_range_becomes_valid_min_and_max():
    _, checks = render([valid_values("r", "pct")])
    assert checks == [{"invalid_count(pct) = 0": {"valid min": 0, "valid max": 100}}]


def test_strict_bound_is_unmapped_naming_the_bound():
    result, checks = render([valid_values("r", "pos")])
    assert checks == [] and result.mapped == []
    (rule,) = result.unmapped
    assert rule.rule_id == "r" and "> 0" in rule.reason


def test_reference_column_is_unmapped_naming_the_product():
    result, checks = render([valid_values("r", "site")])
    assert checks == []
    (rule,) = result.unmapped
    assert "acme.sites" in rule.reason


def test_rule_description_list_wins_over_the_column_enum():
    _, checks = render([valid_values("r", "tier", description="tier valid values: X, Y.")])
    assert checks == [{"invalid_count(tier) = 0": {"valid values": ["X", "Y"]}}]


def test_multi_column_completeness_is_one_check_per_column_and_mapped():
    result, checks = render([{"id": "c", "type": "completeness", "selector": "a,b"}])
    assert checks == ["missing_count(a) = 0", "missing_count(b) = 0"]
    assert result.mapped == ["e1.c"] and result.unmapped == []


def test_multi_column_uniqueness_is_unmapped():
    result, checks = render([{"id": "u", "type": "uniqueness", "selector": "a,b"}])
    assert checks == [] and result.mapped == []
    (rule,) = result.unmapped
    assert "takes one column" in rule.reason
