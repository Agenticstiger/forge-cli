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

"""The ODCS quality export, pinned against three defects a real DQ engine found.

1. Rules forge synthesises from ``required`` and the primary-key tag carried no
   ``id``, and an engine that labels its results by rule id refuses such a
   contract — the one we tested refuses the whole document, so one derived rule
   made the export unusable.
2. ``exposes[].contract.dq.rules`` and ``exposes[].contract.quality`` never
   reached the ODCS quality block at all. They were parked in
   ``customProperties.fluidExtras``, so a consumer running the published
   contract's quality rules saw only forge's own derived checks and none of the
   rules the author wrote.
3. The exporter read ``field.validations``, a key no FLUID schema defines, while
   the schema defines ``field.validationRules`` — so rules written the documented
   way exported nothing. And the exporter replayed a column's ``quality`` list
   while ``$defs.column`` was closed and did not define it, so a column written
   the way the exporter supports was refused by ``fluid validate``.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List

import pytest

from fluid_build.providers.odcs import OdcsProvider, validation
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit


def _contract(**overrides: Any) -> Dict[str, Any]:
    """A valid FLUID 0.7.6 contract with one tabular expose."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "acme.sales.orders",
        "name": "Orders",
        "domain": "sales",
        "metadata": {
            "owner": {"team": "sales-platform", "email": "sales@example.com"},
            "layer": "Gold",
            "productType": "CDP",
        },
        "exposes": [
            {
                "exposeId": "orders_table",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "ANALYTICS", "schema": "SALES", "table": "ORDERS"},
                },
                "contract": {
                    "schema": [
                        {
                            "name": "order_id",
                            "type": "string",
                            "required": True,
                            "tags": ["primary-key"],
                        },
                        {"name": "amount", "type": "decimal(12,2)"},
                        {"name": "status", "type": "string"},
                    ]
                },
            }
        ],
    }
    for key, value in overrides.items():
        contract[key] = value
    return contract


def _with_expose_contract(**contract_keys: Any) -> Dict[str, Any]:
    fluid = _contract()
    fluid["exposes"][0]["contract"].update(contract_keys)
    return fluid


def _render(fluid: Dict[str, Any]) -> Dict[str, Any]:
    ports = OdcsProvider().render_all_ports(fluid)
    assert len(ports) == 1, f"expected one output port, got {[p[0] for p in ports]}"
    return ports[0][1]


def _quality_ids(odcs: Dict[str, Any]) -> List[Any]:
    """Every quality-rule id in the document, object level and column level."""
    ids: List[Any] = []
    for obj in odcs.get("schema") or []:
        ids += [rule.get("id") for rule in obj.get("quality") or []]
        for prop in obj.get("properties") or []:
            ids += [rule.get("id") for rule in prop.get("quality") or []]
    return ids


def _column_quality(odcs: Dict[str, Any], column: str) -> List[Dict[str, Any]]:
    for obj in odcs.get("schema") or []:
        for prop in obj.get("properties") or []:
            if prop.get("name") == column:
                return prop.get("quality") or []
    raise AssertionError(f"column {column!r} not in the exported schema")


def _object_quality(odcs: Dict[str, Any]) -> List[Dict[str, Any]]:
    objects = odcs.get("schema") or []
    assert objects, "expected one SchemaObject"
    return objects[0].get("quality") or []


def _fluid_extras(odcs: Dict[str, Any]) -> Dict[str, Any]:
    for prop in odcs.get("customProperties") or []:
        if prop.get("property") == "fluidExtras":
            return prop.get("value") or {}
    return {}


# A contract exercising every quality surface at once.
_RICH = _with_expose_contract(
    dq={
        "rules": [
            {
                "id": "status_unique",
                "type": "uniqueness",
                "severity": "error",
                "selector": "status",
            },
            {
                "id": "amount_complete",
                "type": "completeness",
                "severity": "warn",
                "selector": "amount",
                "threshold": 5,
                "operator": "<=",
            },
            {"id": "orders_fresh", "type": "freshness", "severity": "error", "window": "PT1H"},
            {
                "id": "orders_anomaly",
                "type": "anomaly_detection",
                "severity": "warn",
                "description": "row-count anomaly",
            },
        ]
    },
    quality=[{"rule": "amount_positive", "expression": "amount > 0", "severity": "error"}],
)
_RICH["exposes"][0]["contract"]["schema"][1]["validationRules"] = [
    {"type": "range", "constraint": ">= 0", "message": "amount must be non-negative"}
]
_RICH["exposes"][0]["contract"]["schema"][2]["validationRules"] = [
    {"type": "enum", "constraint": "NEW, PAID, SHIPPED"},
    {"type": "regex", "constraint": "^[A-Z]+$"},
]


# ---------------------------------------------------------------------------
# Defect 1 — every rule forge emits carries a stable, unique id
# ---------------------------------------------------------------------------


def test_every_exported_quality_rule_has_an_id_unique_in_its_schema_object() -> None:
    ids = _quality_ids(_render(_RICH))
    assert ids, "expected the export to carry quality rules"
    assert all(ids), f"quality rule(s) exported with no id: {ids}"
    assert len(ids) == len(set(ids)), f"duplicate quality rule ids: {ids}"


def test_derived_rules_carry_an_id_too() -> None:
    """The ``required`` / primary-key checks are the ones that had none."""
    rules = _column_quality(_render(_contract()), "order_id")
    assert [rule["id"] for rule in rules] == [
        "order_id-nullValues",
        "order_id-duplicateValues",
    ]


def test_rule_ids_are_stable_across_two_renders() -> None:
    """A counter or a hash of anything outside the contract would make every
    export a spurious diff."""
    first = _render(copy.deepcopy(_RICH))
    second = _render(copy.deepcopy(_RICH))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# Defect 2 — the author's own dq rules and quality expressions reach the ODCS
# ---------------------------------------------------------------------------


def test_dq_uniqueness_rule_lands_on_the_column_its_selector_names() -> None:
    rules = _column_quality(_render(_RICH), "status")
    unique = [r for r in rules if r.get("metric") == "duplicateValues"]
    assert len(unique) == 1, f"no duplicateValues rule on 'status': {rules}"
    assert unique[0] == {
        "id": "status_unique",
        "type": "library",
        "metric": "duplicateValues",
        "dimension": "uniqueness",
        "mustBe": 0,
        "description": "uniqueness check on status",
        "severity": "error",
    }


def test_dq_threshold_and_operator_become_the_matching_odcs_operator() -> None:
    rules = _column_quality(_render(_RICH), "amount")
    complete = [r for r in rules if r.get("id") == "amount_complete"]
    assert complete, f"no amount_complete rule exported: {rules}"
    assert complete[0]["metric"] == "nullValues"
    assert complete[0]["mustBeLessOrEqualTo"] == 5
    # ODCS types severity as a free string and documents "warning", not "warn".
    assert complete[0]["severity"] == "warning"


def test_quality_expression_lands_on_the_object_as_a_sql_rule() -> None:
    """ODCS defines ``sql`` as a query returning a comparable value, so a FLUID
    row predicate becomes one. It used to be emitted verbatim, which handed a DQ
    engine a fragment it could not execute."""
    rules = {r["id"]: r for r in _object_quality(_render(_RICH))}
    assert "amount_positive" in rules, f"expression not exported: {sorted(rules)}"
    assert rules["amount_positive"]["type"] == "sql"
    assert (
        rules["amount_positive"]["query"]
        == "SELECT COUNT(*) FROM ${table} WHERE (\namount > 0\n) IS NOT TRUE"
    )
    assert rules["amount_positive"]["mustBe"] == 0


def test_freshness_rule_exports_as_an_odcs_custom_rule() -> None:
    """ODCS has no freshness metric, but it has a named escape hatch."""
    rules = {r["id"]: r for r in _object_quality(_render(_RICH))}
    assert "orders_fresh" in rules, f"freshness rule not exported: {sorted(rules)}"
    assert rules["orders_fresh"]["type"] == "custom"
    assert rules["orders_fresh"]["engine"] == "fluid"
    assert rules["orders_fresh"]["implementation"] == {"window": "PT1H"}
    assert rules["orders_fresh"]["dimension"] == "timeliness"


def test_dq_rule_with_no_odcs_form_warns_and_is_still_parked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        odcs = _render(_RICH)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    named = [m for m in warnings if "orders_anomaly" in m]
    assert named, f"no warning named the unmappable rule; warnings were {warnings}"
    assert "anomaly_detection" in named[0]
    assert "fluidExtras" in named[0]

    # Not exported as ODCS quality …
    assert "orders_anomaly" not in _quality_ids(odcs)
    # … but still carried, so nothing is lost.
    parked = _fluid_extras(odcs)["exposes"]["orders_table"]["contract"]["dq"]["rules"]
    assert any(r["id"] == "orders_anomaly" for r in parked)


def test_declared_quality_does_not_double_on_re_export() -> None:
    """``export(import(export(x))) == export(x)``.

    The first export writes the dq rule onto the column; the import reads it
    back into ``field.quality``; the second export replays that list verbatim.
    Re-deriving the rule there appended it a second time, and the duplicate grew
    by one on every trip.
    """
    provider = OdcsProvider()
    first = _render(copy.deepcopy(_RICH))
    result = provider.roundtrip_check(first)
    assert result["equal"], (
        f"re-export diff — extra: {result['extra']}, "
        f"missing: {result['missing']}, changed: {result['changed']}"
    )


def test_mappable_dq_rules_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """The warning must name a real gap, not fire on every rule."""
    fluid = _with_expose_contract(
        dq={"rules": [{"id": "status_unique", "type": "uniqueness", "severity": "error"}]}
    )
    with caplog.at_level(logging.WARNING):
        _render(fluid)
    assert not [r for r in caplog.records if "no ODCS v3.1.0 quality form" in r.getMessage()]


# ---------------------------------------------------------------------------
# Defect 3 — the schema and the exporter agree about column-level quality
# ---------------------------------------------------------------------------


def test_column_validation_rules_export_odcs_quality() -> None:
    """``validationRules`` is the key the schema defines; it exported nothing."""
    rules = {r["id"]: r for r in _column_quality(_render(_RICH), "status")}
    assert rules["status-enum"]["metric"] == "invalidValues"
    assert rules["status-enum"]["arguments"] == {"validValues": ["NEW", "PAID", "SHIPPED"]}
    assert rules["status-regex"]["metric"] == "invalidValues"
    assert rules["status-regex"]["arguments"] == {"pattern": "^[A-Z]+$"}

    amount = {r["id"]: r for r in _column_quality(_render(_RICH), "amount")}
    assert amount["amount-range"]["type"] == "text"
    assert amount["amount-range"]["description"] == "amount must be non-negative"


def test_column_quality_list_validates_and_round_trips() -> None:
    hand_written = [
        {
            "id": "amount-positive",
            "type": "library",
            "metric": "invalidValues",
            "mustBe": 0,
            "arguments": {"pattern": "^[0-9.]+$"},
            "dimension": "conformity",
        }
    ]
    fluid = _contract()
    fluid["exposes"][0]["contract"]["schema"][1]["quality"] = hand_written

    result = FluidSchemaManager().validate_contract(copy.deepcopy(fluid), "0.7.6")
    assert result.is_valid, f"fluid validate rejected a column quality list: {result.errors}"

    # The exporter replays the list verbatim — that is the round-trip path.
    assert _column_quality(_render(fluid), "amount") == hand_written


def test_column_quality_list_wins_over_the_derived_checks() -> None:
    """Replacing, not merging, is what keeps an imported ODCS column lossless."""
    fluid = _contract()
    fluid["exposes"][0]["contract"]["schema"][0]["quality"] = [
        {"id": "only-this", "type": "text", "description": "hand written"}
    ]
    assert [r["id"] for r in _column_quality(_render(fluid), "order_id")] == ["only-this"]


# ---------------------------------------------------------------------------
# No regression for a contract that uses none of it
# ---------------------------------------------------------------------------


def test_contract_without_declared_quality_exports_as_before() -> None:
    odcs = _render(_contract())
    obj = odcs["schema"][0]
    assert "quality" not in obj, "an object-level quality block appeared out of nowhere"
    assert _column_quality(odcs, "order_id") == [
        {
            "id": "order_id-nullValues",
            "type": "library",
            "metric": "nullValues",
            "mustBe": 0,
            "dimension": "completeness",
            "description": "Field 'order_id' must not contain null values",
        },
        {
            "id": "order_id-duplicateValues",
            "type": "library",
            "metric": "duplicateValues",
            "mustBe": 0,
            "dimension": "uniqueness",
            "description": "Primary key field 'order_id' must contain only unique values",
        },
    ]
    for column in ("amount", "status"):
        assert _column_quality(odcs, column) == [], f"{column} grew a quality rule"


# ---------------------------------------------------------------------------
# Everything emitted is valid ODCS v3.1.0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fluid",
    [
        pytest.param(_contract(), id="plain"),
        pytest.param(_RICH, id="rich"),
        pytest.param(
            _with_expose_contract(
                quality=[{"rule": "row", "expression": "1 = 1", "severity": "info"}]
            ),
            id="expression-only",
        ),
        pytest.param(
            _with_expose_contract(
                dq={
                    "rules": [
                        {
                            "id": "valid_status",
                            "type": "valid_values",
                            "severity": "critical",
                            "selector": "status",
                        }
                    ]
                }
            ),
            id="valid-values",
        ),
    ],
)
def test_exported_document_validates_against_the_vendored_odcs_schema(
    fluid: Dict[str, Any],
) -> None:
    schema = validation.load_schema()
    assert schema is not None, "the vendored ODCS v3.1.0 schema is missing"
    errors = validation.collect_errors(_render(copy.deepcopy(fluid)), schema)
    assert not errors, f"exported document is not valid ODCS v3.1.0: {errors}"
