# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``util/dq_checks.py``: a contract's DQ rules read once, as checks or refusals.

The module docstring is the spec. Shapes follow the industry packs' contracts (FLUID 0.7.6):
pipe-separated enums, comma-separated completeness selectors, ``labels.references`` keys.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build.util.dq_checks import (
    Reference,
    parse_enum,
    parse_range,
    read_rules,
)

pytestmark = [pytest.mark.unit]

COLUMNS: List[Dict[str, Any]] = [
    {"name": "tenancy_id", "type": "STRING", "required": True},
    {
        "name": "site_id",
        "type": "STRING",
        "labels": {"references": "bronze.asset.site_master_v1.site_id"},
    },
    {
        "name": "tenant_operator",
        "type": "STRING",
        "validationRules": [{"type": "enum", "constraint": "ZAIN | OOREDOO | ISP"}],
    },
    {
        "name": "country_code",
        "type": "STRING",
        "validationRules": [{"type": "regex", "constraint": "^[A-Z]{2}$"}],
    },
    {
        "name": "grid_kwh",
        "type": "DECIMAL(12,3)",
        "validationRules": [{"type": "range", "constraint": ">= 0"}],
    },
    {"name": "antenna_count", "type": "INTEGER"},
    {"name": "_ingested_at", "type": "TIMESTAMP"},
    {"name": "snapshot_date", "type": "DATE"},
]
CONSUMES = [{"productId": "bronze.asset.site_master_v1", "exposeId": "site_master"}]


def _contract(*rules: Dict[str, Any], consumes: Optional[list] = None) -> Dict[str, Any]:
    return {
        "id": "silver.commercial.active_tenancy_v1",
        "consumes": CONSUMES if consumes is None else consumes,
        "exposes": [
            {
                "exposeId": "active_tenancy",
                "contract": {"schema": COLUMNS, "dq": {"rules": list(rules)}},
            }
        ],
    }


def _read(*rules: Dict[str, Any], **kwargs: Any):
    contract = _contract(*rules, **kwargs)
    return read_rules(contract, contract["exposes"])


def _rule(type_: str, selector: str = "tenancy_id", **fields: Any) -> Dict[str, Any]:
    return {"id": f"r_{type_}", "type": type_, "selector": selector, "severity": "error", **fields}


def _one(*rules: Dict[str, Any], **kwargs: Any):
    read = _read(*rules, **kwargs)
    assert not read.refusals, [r.message for r in read.refusals]
    (check,) = read.checks
    return check


def _refused(*rules: Dict[str, Any], **kwargs: Any) -> str:
    read = _read(*rules, **kwargs)
    assert not read.checks
    assert read.refusals
    return " ".join(f"{r.message} {r.fix}" for r in read.refusals)


class TestTypes:
    def test_uniqueness_is_unique_on_its_column(self):
        check = _one(_rule("uniqueness", threshold=1.0, operator=">="))
        assert (check.kind, check.column, check.share) == ("unique", "tenancy_id", None)

    def test_completeness_with_several_columns_checks_each(self):
        read = _read(_rule("completeness", "site_id, tenant_operator,antenna_count"))
        assert [(c.kind, c.column) for c in read.checks] == [
            ("not_null", "site_id"),
            ("not_null", "tenant_operator"),
            ("not_null", "antenna_count"),
        ]
        assert [c.name for c in read.checks] == [
            "r_completeness__site_id",
            "r_completeness__tenant_operator",
            "r_completeness__antenna_count",
        ]

    def test_uniqueness_on_several_columns_is_refused(self):
        assert "one uniqueness rule per column" in _refused(
            _rule("uniqueness", "tenancy_id,site_id")
        )

    def test_valid_values_reads_the_columns_enum_split_on_pipes(self):
        check = _one(_rule("valid_values", "tenant_operator"))
        assert (check.kind, check.values) == ("accepted_values", ("ZAIN", "OOREDOO", "ISP"))

    def test_valid_values_reads_the_columns_regex(self):
        check = _one(_rule("valid_values", "country_code"))
        assert (check.kind, check.pattern) == ("regex", "^[A-Z]{2}$")

    def test_valid_values_reads_the_columns_range(self):
        check = _one(_rule("valid_values", "grid_kwh"))
        assert (check.kind, check.bounds) == ("bounds", ((">=", 0.0),))

    def test_valid_values_reads_a_reference_to_a_consumed_product(self):
        check = _one(_rule("valid_values", "site_id", severity="warn"))
        assert check.kind == "reference"
        assert check.reference == Reference("bronze.asset.site_master_v1", "site_master", "site_id")
        assert check.severity == "warn"

    def test_valid_values_falls_back_to_values_the_description_lists(self):
        check = _one(
            _rule("valid_values", "antenna_count", description="antenna_count valid values: 1, 2.")
        )
        assert check.values == ("1", "2")

    def test_valid_values_without_any_allowed_value_is_refused_with_the_fix(self):
        text = _refused(_rule("valid_values", "antenna_count"))
        assert "declares no allowed values" in text
        assert "validationRules" in text and "labels.references" in text

    def test_a_column_with_two_declarations_gets_a_check_for_each(self):
        columns = [
            {
                "name": "code",
                "type": "STRING",
                "validationRules": [
                    {"type": "enum", "constraint": "AA,BB"},
                    {"type": "regex", "constraint": "^[A-Z]+$"},
                ],
            }
        ]
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "t",
                    "contract": {
                        "schema": columns,
                        "dq": {"rules": [_rule("valid_values", "code")]},
                    },
                }
            ],
        }
        read = read_rules(contract, contract["exposes"])
        assert [(c.name, c.kind) for c in read.checks] == [
            ("r_valid_values__accepted_values", "accepted_values"),
            ("r_valid_values__regex", "regex"),
        ]

    def test_accuracy_is_a_bound_on_every_value(self):
        check = _one(_rule("accuracy", "antenna_count", threshold=0, operator=">="))
        assert (check.kind, check.bounds, check.share) == ("bounds", ((">=", 0.0),), None)

    def test_accuracy_without_a_threshold_is_refused(self):
        assert "no numeric threshold" in _refused(_rule("accuracy", "antenna_count"))

    def test_freshness_is_the_newest_values_age_in_seconds(self):
        check = _one(
            _rule("freshness", "_ingested_at", window="PT2H", operator="<=", severity="warn")
        )
        assert (check.kind, check.max_age_seconds, check.strict) == ("freshness", 7200, False)
        assert _one(_rule("freshness", "snapshot_date", window="P35D", operator="<")).strict

    @pytest.mark.parametrize(
        "fields, needle",
        [
            ({"window": "P1M"}, "not a fixed ISO 8601 duration"),
            ({"window": "PT1H", "operator": ">="}, "bounds the newest value's age from above"),
            ({"window": "PT1H", "threshold": 1.0}, "does not take"),
        ],
    )
    def test_a_freshness_rule_that_cannot_run_is_refused(self, fields, needle):
        assert needle in _refused(_rule("freshness", "_ingested_at", **fields))

    def test_freshness_on_a_non_temporal_column_is_refused(self):
        assert "needs a date or timestamp column" in _refused(
            _rule("freshness", "antenna_count", window="PT1H")
        )

    def test_schema_checks_every_contract_column_with_its_declared_type(self):
        check = _one({"id": "s", "type": "schema", "selector": "*", "severity": "error"})
        assert check.kind == "schema"
        assert check.columns[0] == ("tenancy_id", "STRING")
        assert len(check.columns) == len(COLUMNS)

    @pytest.mark.parametrize("kind", ["anomaly_detection", "drift_detection"])
    def test_history_rules_are_refused(self, kind):
        assert "its own history" in _refused(_rule(kind, "*"))


class TestShares:
    def test_every_row_when_the_threshold_is_one(self):
        assert _one(_rule("completeness", "site_id", threshold=1.0, operator="==")).share is None

    def test_a_lower_threshold_is_a_share(self):
        check = _one(_rule("completeness", "site_id", threshold=0.95))
        assert check.share == (">=", 0.95)

    def test_a_threshold_above_one_reads_as_a_percentage_and_is_refused(self):
        assert "0.99, not 99" in _refused(_rule("completeness", "site_id", threshold=99))


class TestRefusals:
    def test_an_unknown_column_is_refused(self):
        assert "does not declare" in _refused(_rule("completeness", "nope"))

    def test_a_column_differing_only_in_case_is_the_schemas_column(self):
        assert _one(_rule("completeness", "SITE_ID")).column == "site_id"

    def test_a_reference_to_a_product_not_consumed_is_refused(self):
        assert "consumes[] does not list" in _refused(_rule("valid_values", "site_id"), consumes=[])

    def test_a_reference_consumed_by_two_exposes_is_refused(self):
        consumes = [
            {"productId": "bronze.asset.site_master_v1", "exposeId": "a"},
            {"productId": "bronze.asset.site_master_v1", "exposeId": "b"},
        ]
        assert "ambiguous" in _refused(_rule("valid_values", "site_id"), consumes=consumes)

    def test_an_unknown_severity_is_refused(self):
        assert "severity" in _refused(_rule("completeness", "site_id", severity="fatal"))

    @pytest.mark.parametrize("severity, level", [("critical", "error"), ("info", "warn")])
    def test_severities_map_to_the_two_dbt_levels(self, severity, level):
        assert _one(_rule("completeness", "site_id", severity=severity)).severity == level


class TestNames:
    def test_names_are_unique_across_exposes(self):
        rule = _rule("completeness", "site_id")
        contract = _contract(rule)
        second = {**contract["exposes"][0], "exposeId": "other"}
        read = read_rules(contract, [contract["exposes"][0], second])
        assert [c.name for c in read.checks] == ["r_completeness", "r_completeness__other"]

    def test_a_rule_id_folds_to_lower_case_letters_digits_and_underscores(self):
        check = _one({**_rule("completeness", "site_id"), "id": "Site-ID check!"})
        assert check.name == "site_id_check"


def test_parse_enum_reads_pipes_or_commas():
    assert parse_enum("A | B |C") == ["A", "B", "C"]
    assert parse_enum("A,B") == ["A", "B"]
    assert parse_enum(None) == []


@pytest.mark.parametrize(
    "text, bounds",
    [
        (">= 0", ((">=", 0.0),)),
        (">= 0 and < 1.5", ((">=", 0.0), ("<", 1.5))),
        ("between -1 and 10", ((">=", -1.0), ("<=", 10.0))),
        ("0 to 10", None),
        (">= 0 or <= 2", None),
    ],
)
def test_parse_range(text, bounds):
    assert parse_range(text) == bounds
