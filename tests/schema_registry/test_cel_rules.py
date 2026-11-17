# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Opt-in Confluent data-contract rules (CEL) for a produced subject: ``rules: true``."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.confluent import ConfluentIacPlugin
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.schema_registry.policy import validation_findings
from fluid_build.schema_registry.reference import produced_subjects, shape_errors
from fluid_build.schema_registry.rules import confluent_rules
from fluid_build.schema_registry.verify import verify_consumed

from .conftest import consumed_contract, produced_contract

pytestmark = pytest.mark.unit

URL = "https://sr.example"
SCHEMAS = Path(__file__).parents[2] / "fluid_build" / "schemas"
COLUMNS = [
    {"name": "id", "type": "string", "required": True},
    {"name": "amount", "type": "long"},
    {"name": "status", "type": "string", "required": True},
    {"name": "price", "type": "decimal(10,2)"},
]
INVARIANTS = [
    {"id": "amount_positive", "description": "An order is for something", "check": "amount > 0"},
    {"id": "paid_has_amount", "when": "status = 'PAID'", "check": "amount IS NOT NULL"},
    {"id": "soft", "check": "amount < 1000", "severity": "warning"},
    {"id": "some_bad", "check": "amount > 5", "mustBeLessThan": 3},
    {"id": "total", "derive": "price", "expr": "ROUND(amount * 1, 2)"},
    {"id": "dep", "dependency": {"from": ["id"], "to": ["status"]}},
]


def make(registry=None, serialization="avro", invariants=None, columns=None):
    registry = {"kind": "confluent", "url": URL, "rules": True} if registry is None else registry
    contract = produced_contract(registry, serialization=serialization)
    schema = columns or COLUMNS
    contract["exposes"][0]["contract"] = {
        "schema": copy.deepcopy(schema),
        "invariants": copy.deepcopy(INVARIANTS if invariants is None else invariants),
    }
    return contract


def subject(contract):
    return produced_subjects(contract)[0]


def test_each_invariant_kind():
    contract = make()
    rules, errors, warnings = confluent_rules(contract, subject(contract))
    assert errors == []
    assert [r["name"] for r in rules] == ["amount_positive", "paid_has_amount"]
    assert rules[0] == {
        "name": "amount_positive",
        "doc": "An order is for something",
        "kind": "CONDITION",
        "type": "CEL",
        "mode": "WRITE",
        "expr": "(type(message.amount) != null_type && message.amount > 0)",
        "on_failure": "ERROR",
    }
    assert "doc" not in rules[1]
    assert rules[1]["expr"] == (
        '(!(message.status == "PAID") || type(message.amount) != null_type)'
    )
    text = "\n".join(warnings)
    where = "exposes[0].binding.properties.registry"
    assert (
        f"{where}: invariant 'soft' is not enforced by the registry: its severity is warning"
        in text
    )
    assert (
        "invariant 'some_bad' is not enforced by the registry: it allows violating rows (mustBeLessThan)"
        in text
    )
    assert (
        "invariant 'total' is not enforced by the registry: a derive is checked on the data" in text
    )
    assert "invariant 'dep' is not enforced by the registry: a dependency" in text
    assert len(warnings) == 4


def test_an_aggregate_is_not_enforced():
    contract = make(
        invariants=[
            {
                "id": "n",
                "derive": "amount",
                "aggregate": {"function": "count", "from": "events", "via": "x"},
            }
        ]
    )
    _, _, warnings = confluent_rules(contract, subject(contract))
    assert "an aggregate is checked on the data" in warnings[0]
    assert "no invariant of this expose becomes a Confluent rule" in warnings[-1]


def test_no_rule_at_all_is_a_warning():
    contract = make(invariants=[])
    assert confluent_rules(contract, subject(contract)) == (
        [],
        [],
        [
            "exposes[0].binding.properties.registry.rules is true, and no invariant of this expose becomes a Confluent rule"
        ],
    )


def test_a_check_without_an_exact_cel_form_is_an_error():
    contract = make(invariants=[{"id": "p", "check": "price > 0"}])
    rules, errors, _ = confluent_rules(contract, subject(contract))
    assert rules == []
    assert errors == [
        "exposes[0].binding.properties.registry: invariant 'p' has no exact CEL form for a "
        "Confluent rule: column price is a decimal: Avro carries it as bytes, which a CEL rule "
        "cannot compare exactly"
    ]


def test_a_json_subject_is_refused_naming_avro():
    contract = make(serialization="json")
    _, errors, _ = confluent_rules(contract, subject(contract))
    assert len(errors) == 1 and "Avro" in errors[0] and ".rules:" in errors[0]


def test_a_consumed_subject_with_rules_is_refused():
    registry = {"kind": "confluent", "url": URL, "rules": True}
    contract = consumed_contract(registry)
    errors, _ = validation_findings(contract)
    message = "builds[0].properties.stream.registry.rules: a consumed subject's rules are its producer's; remove rules"
    assert message in errors
    _, plan_errors, _ = verify_consumed(contract)
    assert message in plan_errors


def test_glue_rules_are_refused_by_shape():
    errors = shape_errors({"kind": "glue", "rules": True}, "r")
    assert errors == ["r.rules is for kind: confluent"]


def test_a_non_boolean_is_refused():
    errors = shape_errors({"kind": "confluent", "url": URL, "rules": "yes"}, "r")
    assert errors == ["r.rules: expected true or false"]


def test_validate_and_plan_report_them():
    contract = make(invariants=[INVARIANTS[0], INVARIANTS[2], {"id": "p", "check": "price > 0"}])
    errors, warnings = validation_findings(contract)
    assert any("invariant 'p' has no exact CEL form" in e for e in errors)
    assert any("invariant 'soft' is not enforced" in w for w in warnings)
    _, plan_errors, plan_warnings = verify_consumed(contract)
    assert any("invariant 'p' has no exact CEL form" in e for e in plan_errors)
    assert any("invariant 'soft' is not enforced" in w for w in plan_warnings)


def test_no_message_changes_without_rules():
    for registry in (
        {"kind": "confluent", "url": URL},
        {"kind": "confluent", "url": URL, "rules": False},
    ):
        contract = make(registry=registry)
        errors, warnings = validation_findings(contract)
        assert not any("invariant" in m or ".rules" in m for m in errors + warnings)


def test_the_emitter_adds_the_ruleset():
    contract = make()
    schema = next(iter(ConfluentIacPlugin().emit(contract)["confluent_schema"].values()))
    rules = schema["ruleset"]["domain_rules"]
    assert [r["name"] for r in rules] == ["amount_positive", "paid_has_amount"]
    assert all(r["on_failure"] == "ERROR" and r["mode"] == "WRITE" for r in rules)
    json.dumps(schema)  # canonical JSON


@pytest.mark.parametrize(
    "registry",
    [{"kind": "confluent", "url": URL}, {"kind": "confluent", "url": URL, "rules": False}],
)
def test_no_ruleset_when_rules_is_false_or_absent(registry):
    schema = next(
        iter(ConfluentIacPlugin().emit(make(registry=registry))["confluent_schema"].values())
    )
    assert "ruleset" not in schema


def test_no_empty_ruleset():
    schema = next(iter(ConfluentIacPlugin().emit(make(invariants=[]))["confluent_schema"].values()))
    assert "ruleset" not in schema


def test_the_emitter_refuses_a_check_without_a_cel_form():
    contract = make(invariants=[{"id": "p", "check": "price > 0"}])
    with pytest.raises(UnsupportedBindingError) as caught:
        ConfluentIacPlugin().emit(contract)
    assert "no exact CEL form" in str(caught.value)


# -- the schema -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def manager():
    return FluidSchemaManager()


@pytest.mark.parametrize("rules,valid", [(True, True), (False, True), ("yes", False)])
def test_schema_077(manager, rules, valid):
    contract = make(registry={"kind": "confluent", "url": URL, "rules": rules})
    contract["fluidVersion"] = "0.7.7"
    result = manager.validate_contract(contract, offline_only=True)
    assert any("rules" in str(e) for e in result.errors) is (not valid)


def test_schema_077_refuses_glue_rules(manager):
    contract = make(registry={"kind": "glue", "rules": True})
    result = manager.validate_contract(contract, offline_only=True)
    assert any("rules" in str(e) for e in result.errors)


def test_schema_076_has_no_rules_key():
    schema = json.loads((SCHEMAS / "fluid-schema-0.7.6.json").read_text())
    assert "schemaRegistry" not in schema["$defs"]
    assert '"rules"' not in json.dumps(schema["$defs"].get("binding", {}))
