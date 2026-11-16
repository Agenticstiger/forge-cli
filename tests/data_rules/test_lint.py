# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pins the v1 validator lint of ``RFC-data-rules.md`` ("Validator lint (v1)").

``fluid_build.data_rules.lint_contract`` on a realistic 0.7.7 contract (rebuilt below
as a dict factory): a valid contract has no error and one note; a contract below
0.7.7 or without ``fluidVersion`` is not linted; and each lint item (2 and 4 to 16)
and each note has a test asserting the severity, the exact RFC 6901 pointer and one
stable fragment of the message. Every finding's pointer must resolve in the contract.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.data_rules import Finding, lint_contract

pytestmark = pytest.mark.unit

_HEADER = {
    "kind": "table",
    "binding": {
        "platform": "snowflake",
        "format": "snowflake_table",
        "location": {"database": "BILLING", "schema": "CDP"},
    },
}


def _rel(name: str, to: str, frm: str, to_col: str) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": [frm],
        "toColumns": [to_col],
        "cardinality": "many_to_one",
    }


def _expose(expose_id: str, schema: List[Any], **more: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"exposeId": expose_id, **_HEADER}
    out["contract"] = {"schema": schema}
    for key, value in more.items():
        if key == "semantics":
            out["semantics"] = value
        else:
            out["contract"][key] = value
    return out


def _contract() -> Dict[str, Any]:
    """A fresh, valid 0.7.7 contract: invoices, lines, contracts, accounts, subscribers."""
    invoices = _expose(
        "invoices",
        [
            {"name": "invoice_id", "type": "string", "required": True},
            {"name": "contract_id", "type": "string"},
            {"name": "net", "type": "decimal(12,2)"},
            {"name": "tax_rate", "type": "decimal(5,4)"},
            {"name": "gross", "type": "decimal(12,2)"},
            {"name": "billed_base_fee", "type": "decimal(12,2)"},
            {"name": "issue_date", "type": "date"},
            {"name": "due_date", "type": "date"},
            {"name": "customer_segment", "type": "string", "sharedAttribute": "customer_segment"},
            {
                "name": "tower_id",
                "type": "string",
                "identifierFormat": [
                    {"prefixes": ["MSC", "TWR"], "mask": "99999"},
                    {"prefixes": ["LEGACY-"], "mask": "A9-99AA"},
                ],
            },
            {
                "name": "product_code",
                "type": "string",
                "identifierFormat": [{"prefixes": ["P"], "mask": "99999"}],
                "placeholders": {
                    "values": ["XXXXX"],
                    "prefixes": ["TBD_"],
                    "mustBeLessOrEqualTo": 1,
                    "unit": "percent",
                },
            },
        ],
        invariants=[
            {"id": "gross_from_net", "derive": "gross", "expr": "ROUND(net * (1 + tax_rate), 2)"},
            {
                "id": "due_after_issue",
                "check": "issue_date <= due_date",
                "mustBeLessOrEqualTo": 0.5,
                "unit": "percent",
            },
            {
                "id": "billed_base_fee_from_lines",
                "derive": "billed_base_fee",
                "aggregate": {
                    "function": "sum",
                    "column": "amount",
                    "from": "invoice_lines",
                    "via": "invoice",
                    "filter": "kind = 'base_fee'",
                    "ifNone": 0,
                },
            },
            {"id": "base_fee_is_monthly_fee", "check": "billed_base_fee = contract.monthly_fee"},
        ],
        semantics={"relationships": [_rel("contract", "contracts", "contract_id", "contract_id")]},
    )
    lines = _expose(
        "invoice_lines",
        [
            {"name": "line_id", "type": "string", "required": True},
            {"name": "invoice_id", "type": "string"},
            {
                "name": "kind",
                "type": "string",
                "validationRules": [{"type": "enum", "constraint": "base_fee,usage"}],
            },
            {"name": "amount", "type": "decimal(12,2)"},
        ],
        semantics={"relationships": [_rel("invoice", "invoices", "invoice_id", "invoice_id")]},
    )
    contracts = _expose(
        "contracts",
        [
            {"name": "contract_id", "type": "string", "required": True},
            {"name": "account_id", "type": "string"},
            {"name": "monthly_fee", "type": "decimal(12,2)"},
            {"name": "status", "type": "string"},
        ],
        semantics={"relationships": [_rel("account", "accounts", "account_id", "account_id")]},
    )
    accounts = _expose(
        "accounts",
        [
            {"name": "account_id", "type": "string", "required": True},
            {"name": "segment", "type": "string", "sharedAttribute": "customer_segment"},
            {"name": "group_id", "type": "string"},
            {"name": "group_name", "type": "string"},
            {"name": "status", "type": "string"},
        ],
        invariants=[
            {
                "id": "group_name_by_id",
                "dependency": {"from": ["group_id"], "to": ["group_name"], "bothWays": True},
            }
        ],
    )
    subscribers = _expose(
        "subscribers",
        [
            {"name": "subscriber_id", "type": "string", "required": True},
            {"name": "product_id", "type": "string"},
            {"name": "birth_date", "type": "date"},
            {"name": "snapshot_date", "type": "date"},
            {"name": "age", "type": "int"},
            {"name": "activation_date", "type": "date"},
            {"name": "first_use_date", "type": "date"},
            {"name": "min_term_end", "type": "date"},
            {"name": "speed_mbps", "type": "decimal(8,1)"},
            {"name": "is_fast", "type": "boolean"},
        ],
        invariants=[
            {
                "id": "age_at_snapshot",
                "derive": "age",
                "expr": "YEARS_BETWEEN(birth_date, snapshot_date)",
            },
            {
                "id": "activation_order",
                "check": "activation_date <= first_use_date AND first_use_date <= min_term_end",
                "when": "first_use_date IS NOT NULL",
                "mustBeBetween": [1.5, 2.5],
                "unit": "percent",
            },
            {
                "id": "fast_flag",
                "derive": "is_fast",
                "expr": "speed_mbps > product.speed_threshold_mbps",
            },
        ],
        semantics={"relationships": [_rel("product", "products", "product_id", "product_id")]},
    )
    products = _expose(
        "products",
        [
            {"name": "product_id", "type": "string", "required": True},
            {"name": "speed_threshold_mbps", "type": "decimal(8,1)"},
        ],
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.billing.invoices",
        "name": "Invoices",
        "metadata": {"layer": "Gold", "owner": {"team": "billing-platform"}},
        "exposes": [invoices, lines, contracts, accounts, subscribers, products],
    }


# -- helpers -------------------------------------------------------------------


def _resolve(document: Any, pointer: str) -> Any:
    """Walk an RFC 6901 pointer; raises KeyError/IndexError when it leads nowhere."""
    node = document
    if pointer == "":
        return node
    assert pointer.startswith("/"), pointer
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _lint(contract: Dict[str, Any]) -> List[Finding]:
    findings = lint_contract(contract)
    for finding in findings:
        try:
            _resolve(contract, finding.pointer)
        except (KeyError, IndexError, ValueError, TypeError):  # pragma: no cover
            pytest.fail(f"pointer {finding.pointer!r} does not resolve ({finding.text})")
    return findings


def _errors(contract: Dict[str, Any]) -> List[Finding]:
    return [f for f in _lint(contract) if f.severity == "error"]


def _notes(contract: Dict[str, Any]) -> List[Finding]:
    return [f for f in _lint(contract) if f.severity == "note"]


def _expose_of(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    return next(e for e in contract["exposes"] if e["exposeId"] == expose_id)


def _column(contract: Dict[str, Any], expose_id: str, name: str) -> Dict[str, Any]:
    return next(
        c for c in _expose_of(contract, expose_id)["contract"]["schema"] if c["name"] == name
    )


def _invariant(contract: Dict[str, Any], expose_id: str, inv_id: str) -> Dict[str, Any]:
    return next(
        i for i in _expose_of(contract, expose_id)["contract"]["invariants"] if i["id"] == inv_id
    )


def _add_invariant(contract: Dict[str, Any], expose_id: str, inv: Dict[str, Any]) -> int:
    invariants = _expose_of(contract, expose_id)["contract"].setdefault("invariants", [])
    invariants.append(inv)
    return len(invariants) - 1


def _window(contract: Dict[str, Any], expose_id: str, edge: str = "isStart") -> None:
    """Declare a validity window on an expose (a slowly changing dimension)."""
    _expose_of(contract, expose_id).setdefault("semantics", {})["dimensions"] = [
        {
            "name": "valid_from",
            "type": "time",
            "typeParams": {"validityParams": {edge: True}},
        }
    ]


def _only(findings: List[Finding], severity: str = "error") -> Finding:
    picked = [f for f in findings if f.severity == severity]
    assert len(picked) == 1, [f.text for f in picked]
    return picked[0]


def _pick(findings: List[Finding], fragment: str, severity: str = "error") -> Finding:
    """The one finding of ``severity`` whose text holds ``fragment``."""
    picked = [f for f in findings if f.severity == severity and fragment in f.text]
    assert len(picked) == 1, [f.text for f in findings]
    return picked[0]


def _expect(finding: Finding, severity: str, pointer: str, fragment: str) -> None:
    assert finding.severity == severity, finding.text
    assert finding.pointer == pointer, (finding.pointer, finding.text)
    assert fragment in finding.text, finding.text


def _index(contract: Dict[str, Any], expose_id: str) -> int:
    return contract["exposes"].index(_expose_of(contract, expose_id))


# -- the valid contract, versions ------------------------------------------------


def test_factory_returns_a_fresh_dict_each_call():
    first = _contract()
    first["exposes"][0]["exposeId"] = "changed"
    assert _contract()["exposes"][0]["exposeId"] == "invoices"


def test_valid_contract_has_no_error_and_one_note():
    findings = _lint(_contract())
    assert [f for f in findings if f.severity == "error"] == []
    notes = [f for f in findings if f.severity == "note"]
    assert len(notes) == 1
    note = notes[0]
    contracts = _index(_contract(), "contracts")
    _expect(
        note,
        "note",
        f"/exposes/{contracts}/semantics/relationships/0",
        "both have status",
    )
    assert "exposes[contracts].semantics.relationships[account]" in note.text
    assert "exposes[accounts]" in note.text


def test_finding_properties():
    note = _lint(_contract())[0]
    assert note.text == f"{note.label}: {note.message}"
    assert note.display.startswith(note.text)
    assert note.fix and note.fix in note.display


def test_version_0_7_6_is_not_linted():
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    # the body that would fail at 0.7.7 still gives nothing
    _invariant(contract, "invoices", "due_after_issue")["check"] = "nett >"
    assert lint_contract(contract) == []


def test_missing_fluid_version_is_not_linted():
    contract = _contract()
    del contract["fluidVersion"]
    _invariant(contract, "invoices", "due_after_issue")["check"] = "nett >"
    assert lint_contract(contract) == []


def test_malformed_fluid_version_is_not_linted():
    contract = _contract()
    contract["fluidVersion"] = "preview"
    assert lint_contract(contract) == []


def test_a_later_version_is_linted():
    contract = _contract()
    contract["fluidVersion"] = "0.7.8"
    _invariant(contract, "invoices", "due_after_issue")["check"] = "nett > 0"
    assert _only(_lint(contract)).severity == "error"


def test_non_mapping_input_gives_nothing():
    assert lint_contract(["not", "a", "contract"]) == []  # type: ignore[arg-type]


# -- 2. ids -------------------------------------------------------------------


def test_duplicate_invariant_id_across_exposes():
    contract = _contract()
    j = _add_invariant(contract, "contracts", {"id": "gross_from_net", "check": "monthly_fee >= 0"})
    e = _index(contract, "contracts")
    finding = _only(_lint(contract))
    _expect(finding, "error", f"/exposes/{e}/contract/invariants/{j}/id", "is also")
    assert "exposes[invoices].contract.invariants[gross_from_net]" in finding.text
    assert finding.label == "exposes[contracts].contract.invariants[gross_from_net]"


def test_invariant_id_clashes_with_a_dq_rule_id():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["dq"] = {
        "rules": [{"id": "due_after_issue", "type": "not_null"}]
    }
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/1/id", "is also")
    assert "dq.rules[due_after_issue]" in finding.text


def test_invariant_id_clashes_with_a_quality_rule():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["quality"] = [
        {"rule": "due_after_issue", "type": "custom"}
    ]
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/1/id", "is also")
    assert "quality[due_after_issue]" in finding.text


def test_two_preexisting_dq_rules_sharing_an_id_are_not_ours_to_report():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["dq"] = {
        "rules": [{"id": "same", "type": "not_null"}, {"id": "same", "type": "unique"}]
    }
    _expose_of(contract, "contracts")["contract"]["quality"] = [{"rule": "same", "type": "custom"}]
    assert _errors(contract) == []


# An invariant id reused by a dq rule or a quality rule of a LATER expose is a clash too (the RFC
# counts both): every id is collected before any is reported, so the order does not matter.


def test_dq_rule_id_in_a_later_expose_repeats_an_invariant_id():
    contract = _contract()
    _expose_of(contract, "contracts")["contract"]["dq"] = {
        "rules": [{"id": "due_after_issue", "type": "not_null"}]
    }
    findings = [f for f in _errors(contract) if "due_after_issue" in f.text]
    assert len(findings) == 1


def test_quality_rule_id_in_a_later_expose_repeats_an_invariant_id():
    contract = _contract()
    _expose_of(contract, "contracts")["contract"]["quality"] = [
        {"rule": "due_after_issue", "type": "custom"}
    ]
    findings = [f for f in _errors(contract) if "due_after_issue" in f.text]
    assert len(findings) == 1


def test_duplicate_invariant_id_within_one_expose():
    contract = _contract()
    j = _add_invariant(contract, "invoices", {"id": "due_after_issue", "check": "net >= 0"})
    finding = _only(_lint(contract))
    _expect(finding, "error", f"/exposes/0/contract/invariants/{j}/id", "is also")


# -- 4. a text outside FLUID SQL/1 ------------------------------------------------


def test_syntax_error_names_the_character():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "issue_date <="
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/invariants/1/check", "not FLUID SQL/1 at character"
    )
    assert finding.label == "exposes[invoices].contract.invariants[due_after_issue].check"
    assert "contract.quality[]" in finding.message or "contract.quality[]" in finding.fix


def test_syntax_error_in_when():
    contract = _contract()
    _invariant(contract, "subscribers", "activation_order")["when"] = "first_use_date IS"
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/4/contract/invariants/1/when", "not FLUID SQL/1 at character"
    )


def test_syntax_error_in_expr():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["expr"] = "ROUND(net *, 2)"
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/invariants/0/expr", "not FLUID SQL/1 at character"
    )


def test_syntax_error_in_aggregate_filter():
    contract = _contract()
    _invariant(contract, "invoices", "billed_base_fee_from_lines")["aggregate"]["filter"] = "kind ="
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/2/aggregate/filter",
        "not FLUID SQL/1 at character",
    )


# -- 5. unknown names, paths, validity windows -----------------------------------------


def test_unknown_column():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "issue_dat <= due_date"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/1/check"
    assert "issue_dat" in finding.text


def test_unknown_relationship_column():
    contract = _contract()
    _invariant(contract, "invoices", "base_fee_is_monthly_fee")[
        "check"
    ] = "billed_base_fee = contract.monthly_fe"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/3/check"
    assert "monthly_fe" in finding.text


def test_unknown_relationship():
    contract = _contract()
    _invariant(contract, "invoices", "base_fee_is_monthly_fee")[
        "check"
    ] = "billed_base_fee = agreement.monthly_fee"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/3/check"
    assert "agreement" in finding.text


def test_path_over_two_links_is_refused():
    contract = _contract()
    _invariant(contract, "invoices", "base_fee_is_monthly_fee")[
        "check"
    ] = "billed_base_fee = contract.account.account_id"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/3/check"
    assert finding.message.startswith("not FLUID SQL/1")


@pytest.mark.parametrize("edge", ["isStart", "isEnd"])
def test_path_through_a_target_with_a_validity_window(edge):
    contract = _contract()
    _window(contract, "contracts", edge)
    findings = _errors(contract)
    # the shared attribute customer_segment also reaches its owner through contracts
    assert len(findings) == 2
    finding = _pick(findings, "base_fee_is_monthly_fee")
    _expect(finding, "error", "/exposes/0/contract/invariants/3/check", "validity window")
    other = _pick(findings, "sharedAttribute")
    assert other.pointer == "/exposes/0/contract/schema/8/sharedAttribute"


def test_a_dimension_without_validity_params_is_no_window():
    contract = _contract()
    _expose_of(contract, "contracts")["semantics"]["dimensions"] = [
        {"name": "status", "type": "categorical"}
    ]
    assert _errors(contract) == []


def test_summary_into_a_parent_with_a_validity_window():
    contract = _contract()
    _window(contract, "invoices")
    # the window also bars the invoices -> contracts path: remove that check
    del _expose_of(contract, "invoices")["contract"]["invariants"][3]
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/2/aggregate/via",
        "declares a validity window",
    )


def test_shared_columns_through_a_target_with_a_validity_window():
    contract = _contract()
    _window(contract, "contracts")
    _invariant(contract, "invoices", "base_fee_is_monthly_fee")["check"] = "billed_base_fee >= 0"
    _expose_of(contract, "invoices")["semantics"]["relationships"][0]["sharedColumns"] = ["net"]
    finding = _pick(_errors(contract), "relationships[contract].sharedColumns")
    _expect(
        finding,
        "error",
        "/exposes/0/semantics/relationships/0/sharedColumns",
        "declares a validity window",
    )


# -- 6. type errors ------------------------------------------------------------------


def test_type_mismatch():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "issue_date <= net"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/1/check"
    assert finding.label.endswith(".check")


def test_enum_literal_typo_in_aggregate_filter():
    contract = _contract()
    _invariant(contract, "invoices", "billed_base_fee_from_lines")["aggregate"][
        "filter"
    ] = "kind = 'base_feee'"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/2/aggregate/filter", "base_feee")
    assert finding.fix == "use one of the column's enum values"


def test_enum_literal_typo_in_check_uses_the_enum_of_the_pipe_form_too():
    contract = _contract()
    _column(contract, "invoice_lines", "kind")["validationRules"] = [
        {"type": "enum", "constraint": "base_fee|usage"}
    ]
    assert _errors(contract) == []


def test_null_literal_as_a_comparison_operand():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "NULL = NULL"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/1/check"


# -- 7. derive targets -------------------------------------------------------------------


def test_derive_target_missing():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["derive"] = "grosss"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/0/derive", "'grosss' is not a column")
    assert "did you mean 'gross'" in finding.text


def test_derive_scale_too_large_says_to_round():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["expr"] = "net * (1 + tax_rate)"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/0/expr"
    assert "wrap it in ROUND" in finding.text


def test_derive_into_a_float_column():
    contract = _contract()
    _column(contract, "invoices", "gross")["type"] = "double"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/0/derive", "gross")
    assert finding.label.endswith(".derive")


def test_derive_with_a_type_that_differs_from_the_value():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["derive"] = "issue_date"
    finding = _only(_lint(contract))
    assert finding.pointer.startswith("/exposes/0/contract/invariants/0/")


def test_aggregate_derive_scale_does_not_fit():
    contract = _contract()
    _column(contract, "invoices", "billed_base_fee")["type"] = "decimal(12,1)"
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/0/contract/invariants/2/aggregate"


# -- 8. cycles ----------------------------------------------------------------------------


def test_cycle_across_two_derives():
    contract = _contract()
    _add_invariant(
        contract,
        "invoices",
        {"id": "net_from_gross", "derive": "net", "expr": "ROUND(gross / (1 + tax_rate), 2)"},
    )
    finding = _only(_lint(contract))
    # the cycle is reported once, at its first column in sorted order (gross, schema index 4)
    _expect(finding, "error", "/exposes/0/contract/schema/4", "form a cycle")
    assert "invoices.gross" in finding.message and "invoices.net" in finding.message


def test_cycle_across_exposes_through_a_link_and_an_aggregate():
    contract = _contract()
    # invoice_lines.amount <- invoices.billed_base_fee (through the link) <- invoice_lines.amount
    _add_invariant(
        contract,
        "invoice_lines",
        {"id": "amount_from_invoice", "derive": "amount", "expr": "invoice.billed_base_fee"},
    )
    finding = _only(_lint(contract))
    # reported at the first member in sorted order: invoice_lines.amount (index 3, expose 1)
    _expect(finding, "error", "/exposes/1/contract/schema/3", "form a cycle")
    assert "invoices.billed_base_fee" in finding.message


def test_a_derive_reading_its_own_column_is_a_cycle():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["expr"] = "ROUND(gross + 1, 2)"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/schema/4", "form a cycle")


# -- 9. two sources ----------------------------------------------------------------------------


def test_two_sources_derive_and_dependency_to():
    contract = _contract()
    _add_invariant(
        contract,
        "accounts",
        {"id": "group_name_derived", "derive": "group_name", "expr": "group_id"},
    )
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/3/contract/schema/3", "2 sources")
    assert "group_name_derived" in finding.message
    assert "dependency group_name_by_id" in finding.message


# -- 10. aggregates ------------------------------------------------------------------------------


def _aggregate(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _invariant(contract, "invoices", "billed_base_fee_from_lines")["aggregate"]


def test_aggregate_bad_from():
    contract = _contract()
    _aggregate(contract)["from"] = "invoice_line"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/2/aggregate/from", "not an expose")
    assert "did you mean 'invoice_lines'" in finding.text


def test_aggregate_bad_via():
    contract = _contract()
    _aggregate(contract)["via"] = "invoic"
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/invariants/2/aggregate/via", "not a relationship"
    )
    assert "did you mean 'invoice'" in finding.text


def test_aggregate_via_pointing_elsewhere():
    contract = _contract()
    _expose_of(contract, "invoice_lines")["semantics"]["relationships"].append(
        _rel("owner_contract", "contracts", "invoice_id", "contract_id")
    )
    _aggregate(contract)["via"] = "owner_contract"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/2/aggregate/via", "not to")


def test_count_takes_no_column():
    contract = _contract()
    _aggregate(contract)["function"] = "count"
    _aggregate(contract)["ifNone"] = 0
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/2/aggregate/column",
        "count takes no column",
    )


def test_count_without_a_column_is_fine():
    contract = _contract()
    _column(contract, "invoices", "billed_base_fee")["type"] = "int"
    aggregate = _aggregate(contract)
    aggregate["function"] = "count"
    del aggregate["column"]
    del aggregate["ifNone"]
    assert _errors(contract) == []


def test_sum_of_a_string_column():
    contract = _contract()
    _aggregate(contract)["column"] = "line_id"
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/2/aggregate/column",
        "sum takes a number of bounded scale",
    )


def test_aggregate_unknown_column():
    contract = _contract()
    _aggregate(contract)["column"] = "amout"
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/2/aggregate/column", "'amout'")


def test_if_none_of_the_wrong_type():
    contract = _contract()
    _aggregate(contract)["ifNone"] = "none"
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/invariants/2/aggregate/ifNone", "ifNone is a number"
    )


def _date_summary(contract: Dict[str, Any], if_none: Any) -> None:
    _expose_of(contract, "invoice_lines")["contract"]["schema"].append(
        {"name": "posted", "type": "date"}
    )
    _expose_of(contract, "invoices")["contract"]["schema"].append(
        {"name": "last_posted", "type": "date"}
    )
    _add_invariant(
        contract,
        "invoices",
        {
            "id": "last_posted_from_lines",
            "derive": "last_posted",
            "aggregate": {
                "function": "max",
                "column": "posted",
                "from": "invoice_lines",
                "via": "invoice",
                "ifNone": if_none,
            },
        },
    )


def test_if_none_date_literal_is_accepted_as_yyyy_mm_dd():
    contract = _contract()
    _date_summary(contract, "2020-01-01")
    assert _errors(contract) == []


@pytest.mark.parametrize("bad", ["2020-13-01", "01/01/2020", 20200101])
def test_if_none_date_literal_must_be_a_calendar_date(bad):
    contract = _contract()
    _date_summary(contract, bad)
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/4/aggregate/ifNone", "'YYYY-MM-DD'")


# -- 11. dependencies -----------------------------------------------------------------------------


def _dependency(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _invariant(contract, "accounts", "group_name_by_id")["dependency"]


def test_dependency_with_an_unknown_column():
    contract = _contract()
    _dependency(contract)["from"] = ["group_idd"]
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/3/contract/invariants/0/dependency/from", "'group_idd'")
    assert "did you mean 'group_id'" in finding.text


def test_dependency_with_an_unknown_column_on_the_to_side():
    contract = _contract()
    _dependency(contract)["to"] = ["nope"]
    finding = _only(_lint(contract))
    assert finding.pointer == "/exposes/3/contract/invariants/0/dependency/to"


def test_dependency_with_a_column_on_both_sides_is_one_finding():
    contract = _contract()
    dependency = _dependency(contract)
    dependency["from"] = ["group_id", "group_name"]
    dependency["to"] = ["group_name"]
    del dependency["bothWays"]
    finding = _only(_lint(contract))  # and no cycle finding on top
    _expect(
        finding,
        "error",
        "/exposes/3/contract/invariants/0/dependency",
        "group_name on both sides",
    )


def test_dependency_bothways_with_two_columns_a_side():
    contract = _contract()
    dependency = _dependency(contract)
    dependency["from"] = ["group_id", "segment"]
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/3/contract/invariants/0/dependency/bothWays",
        "one column on each side",
    )


# -- 12. share operators --------------------------------------------------------------------------


def test_two_share_operators():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["mustBeLessThan"] = 1
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/invariants/1", "at most one share operator")
    assert "mustBeLessThan and mustBeLessOrEqualTo" in finding.message


def test_percent_above_100():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["mustBeLessOrEqualTo"] = 101
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/1/mustBeLessOrEqualTo",
        "a percentage is at most 100",
    )


def test_above_100_without_percent_is_fine():
    contract = _contract()
    inv = _invariant(contract, "invoices", "due_after_issue")
    inv["mustBeLessOrEqualTo"] = 101
    del inv["unit"]
    assert _errors(contract) == []


def test_must_be_between_not_ascending():
    contract = _contract()
    _invariant(contract, "subscribers", "activation_order")["mustBeBetween"] = [3, 2]
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/4/contract/invariants/1/mustBeBetween", "does not ascend")


def test_must_be_between_equal_bounds_do_not_ascend():
    contract = _contract()
    _invariant(contract, "subscribers", "activation_order")["mustBeBetween"] = [2, 2]
    assert _only(_lint(contract)).pointer == "/exposes/4/contract/invariants/1/mustBeBetween"


def test_sixteen_significant_digits():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["mustBeLessOrEqualTo"] = 0.1234567890123456
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/invariants/1/mustBeLessOrEqualTo",
        "more than 15 significant digits",
    )


def test_fifteen_significant_digits_are_fine():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["mustBeLessOrEqualTo"] = 0.123456789012345
    assert _errors(contract) == []


# -- 13. identifier formats -------------------------------------------------------------------------


def _format(contract: Dict[str, Any], entries: List[Any]) -> None:
    _column(contract, "invoices", "tower_id")["identifierFormat"] = entries


def test_mask_with_no_symbol():
    contract = _contract()
    _format(contract, [{"mask": "--"}])
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/schema/9/identifierFormat/0", "no symbol")


def test_mask_ending_in_a_backslash():
    contract = _contract()
    _format(contract, [{"mask": "99\\"}])
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/0/contract/schema/9/identifierFormat/0", "backslash")


def test_mask_with_a_non_ascii_character():
    contract = _contract()
    _format(contract, [{"mask": "9é"}])
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/schema/9/identifierFormat/0", "not printable ASCII"
    )


def test_prefix_that_is_not_printable_ascii():
    contract = _contract()
    _format(contract, [{"prefixes": ["é"], "mask": "99"}])
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/0/contract/schema/9/identifierFormat/0", "not printable ASCII"
    )


def test_format_on_an_int_column():
    contract = _contract()
    _column(contract, "subscribers", "age")["identifierFormat"] = [{"mask": "99"}]
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/4/contract/schema/4/identifierFormat",
        "only a string column takes an identifier format",
    )


def test_overlapping_alternatives_name_an_example_value():
    contract = _contract()
    _format(contract, [{"mask": "99"}, {"mask": "9X"}])
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/9/identifierFormat",
        "alternatives 0 and 1 overlap",
    )
    assert "'00'" in finding.message


def test_alternatives_that_differ_in_length_do_not_overlap():
    contract = _contract()
    _format(contract, [{"mask": "99"}, {"mask": "999"}])
    assert _errors(contract) == []


def test_foreign_key_whose_format_differs_from_its_target():
    contract = _contract()
    _column(contract, "invoices", "invoice_id")["identifierFormat"] = [{"mask": "99999"}]
    _column(contract, "invoice_lines", "invoice_id")["identifierFormat"] = [{"mask": "9999"}]
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/1/contract/schema/1/identifierFormat",
        "whose format differs",
    )


def test_foreign_key_with_the_same_format_in_another_order_is_fine():
    contract = _contract()
    alternatives = [{"prefixes": ["A", "B"], "mask": "99"}, {"prefixes": ["C"], "mask": "99"}]
    _column(contract, "invoices", "invoice_id")["identifierFormat"] = alternatives
    _column(contract, "invoice_lines", "invoice_id")["identifierFormat"] = [
        {"prefixes": ["C"], "mask": "99"},
        {"prefixes": ["B", "A"], "mask": "99"},
    ]
    assert _errors(contract) == []


# -- 14. placeholders ------------------------------------------------------------------------------------


def test_placeholders_on_a_non_string_column():
    contract = _contract()
    _column(contract, "invoices", "net")["placeholders"] = {"values": ["0"]}
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/2/placeholders",
        "only a string column takes placeholders",
    )


def test_placeholders_with_neither_values_nor_prefixes():
    contract = _contract()
    _column(contract, "invoices", "product_code")["placeholders"] = {}
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/10/placeholders",
        "neither values nor prefixes",
    )


def test_placeholder_band_above_100_percent():
    contract = _contract()
    _column(contract, "invoices", "product_code")["placeholders"]["mustBeLessOrEqualTo"] = 150
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/10/placeholders/mustBeLessOrEqualTo",
        "a percentage is at most 100",
    )


def test_placeholder_band_not_ascending():
    contract = _contract()
    placeholders = _column(contract, "invoices", "product_code")["placeholders"]
    del placeholders["mustBeLessOrEqualTo"]
    placeholders["mustBeBetween"] = [5, 1]
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/10/placeholders/mustBeBetween",
        "does not ascend",
    )


def test_placeholder_with_two_operators():
    contract = _contract()
    _column(contract, "invoices", "product_code")["placeholders"]["mustBeLessThan"] = 2
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/10/placeholders",
        "at most one share operator",
    )


# -- 15. sharedColumns -------------------------------------------------------------------------------------


def _shared(contract: Dict[str, Any], expose_id: str, names: List[Any]) -> None:
    _expose_of(contract, expose_id)["semantics"]["relationships"][0]["sharedColumns"] = names


def test_shared_columns_naming_a_key_column():
    contract = _contract()
    _shared(contract, "invoices", ["contract_id"])
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/semantics/relationships/0/sharedColumns",
        "contract_id is a key column",
    )


def test_shared_columns_missing_on_one_end():
    contract = _contract()
    _shared(contract, "invoices", ["tax_rate"])
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/semantics/relationships/0/sharedColumns",
        "tax_rate is not a column of exposes[contracts]",
    )
    assert "exposes[invoices]" not in finding.message.split("is not a column of")[1].split(":")[0]


def test_shared_columns_missing_on_both_ends():
    contract = _contract()
    _shared(contract, "invoices", ["ghost"])
    finding = _only(_lint(contract))
    assert "ghost is not a column of exposes[invoices] and exposes[contracts]" in finding.message


def test_shared_columns_of_different_scales():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["schema"].append(
        {"name": "fee", "type": "decimal(12,2)"}
    )
    _expose_of(contract, "contracts")["contract"]["schema"].append(
        {"name": "fee", "type": "decimal(12,3)"}
    )
    _shared(contract, "invoices", ["fee"])
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/0/semantics/relationships/0/sharedColumns",
        "a shared column has one type",
    )


def test_shared_columns_of_one_type_are_fine():
    contract = _contract()
    for expose_id in ("invoices", "contracts"):
        _expose_of(contract, expose_id)["contract"]["schema"].append(
            {"name": "fee", "type": "decimal(12,2)"}
        )
    _shared(contract, "invoices", ["fee"])
    assert _errors(contract) == []


# -- 16. sharedAttribute --------------------------------------------------------------------------------------


def _tag(contract: Dict[str, Any], expose_id: str, name: str, attribute: str = "region") -> None:
    _column(contract, expose_id, name)["sharedAttribute"] = attribute


def test_shared_attribute_with_no_owner():
    contract = _contract()
    _tag(contract, "contracts", "status")
    _expose_of(contract, "subscribers")["contract"]["schema"].append(
        {"name": "region", "type": "string", "sharedAttribute": "region"}
    )
    finding = _only(_lint(contract))
    _expect(finding, "error", "/exposes/2/contract/schema/3/sharedAttribute", "has no owner")


def test_shared_attribute_with_two_candidates():
    contract = _contract()
    _tag(contract, "contracts", "status")
    _tag(contract, "accounts", "status")
    _expose_of(contract, "accounts")["semantics"] = {
        "relationships": [_rel("contract", "contracts", "group_id", "contract_id")]
    }
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/2/contract/schema/3/sharedAttribute",
        "two candidates for its owner",
    )


def test_shared_attribute_on_two_paths():
    contract = _contract()
    _tag(contract, "contracts", "status")
    _tag(contract, "accounts", "status")
    _expose_of(contract, "contracts")["semantics"]["relationships"].append(
        _rel("billing_account", "accounts", "account_id", "account_id")
    )
    # invoices reaches accounts on two paths too (through contracts): customer_segment
    finding = _pick(_errors(contract), "'region'")
    _expect(finding, "error", "/exposes/2/contract/schema/3/sharedAttribute", "on two paths")
    assert "account and billing_account" in finding.message


def test_shared_attribute_one_expose_twice():
    contract = _contract()
    _tag(contract, "contracts", "status")
    _tag(contract, "contracts", "account_id")
    finding = _only(_lint(contract))
    # reported at the second column, in schema order
    _expect(
        finding,
        "error",
        "/exposes/2/contract/schema/3/sharedAttribute",
        "carries the attribute 'region' twice",
    )
    assert "(account_id, status)" in finding.message


def test_shared_attribute_with_mixed_types():
    contract = _contract()
    _tag(contract, "contracts", "monthly_fee")
    _tag(contract, "accounts", "status")
    finding = _only(_lint(contract))
    _expect(
        finding, "error", "/exposes/2/contract/schema/2/sharedAttribute", "have different types"
    )


def test_shared_attribute_on_a_nested_field():
    contract = _contract()
    _expose_of(contract, "accounts")["contract"]["schema"].append(
        {
            "name": "address",
            "type": "object",
            "fields": [{"name": "city", "type": "string", "sharedAttribute": "city"}],
        }
    )
    finding = _only(_lint(contract))
    _expect(
        finding,
        "error",
        "/exposes/3/contract/schema/5/fields/0/sharedAttribute",
        "a nested field carries no shared attribute",
    )


def _three_hop(contract: Dict[str, Any]) -> None:
    _expose_of(contract, "invoice_lines")["contract"]["schema"].append(
        {"name": "line_segment", "type": "string", "sharedAttribute": "customer_segment"}
    )


def test_shared_attribute_three_hops_without_a_window_is_fine():
    contract = _contract()
    _three_hop(contract)
    assert _errors(contract) == []


def test_shared_attribute_three_hop_owner_path_through_a_validity_window():
    contract = _contract()
    _three_hop(contract)
    _window(contract, "invoices")
    del _expose_of(contract, "invoices")["contract"]["invariants"][2]  # the summary into invoices
    finding = _only(_lint(contract))
    # reported at the group's first member in expose order (invoices.customer_segment)
    _expect(
        finding,
        "error",
        "/exposes/0/contract/schema/8/sharedAttribute",
        "declares a validity window",
    )
    assert "invoice_lines" in finding.message


# -- notes ------------------------------------------------------------------------------------------------------


def test_note_same_named_columns_go_away_when_shared():
    contract = _contract()
    _shared(contract, "contracts", ["status"])
    assert _notes(contract) == []


def test_note_same_named_columns_go_away_when_an_invariant_relates_them():
    contract = _contract()
    _add_invariant(
        contract, "contracts", {"id": "status_agrees", "check": "status = account.status"}
    )
    assert _notes(contract) == []


def test_note_division_outside_round():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "net / 2 > tax_rate"
    finding = _pick(_lint(contract), "a / outside ROUND", "note")
    _expect(finding, "note", "/exposes/0/contract/invariants/1/check", "a / outside ROUND")
    assert _errors(contract) == []


def test_division_inside_round_is_no_note():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["expr"] = "ROUND(net / 3, 2)"
    assert [n for n in _notes(contract) if "ROUND" in n.message] == []


def test_note_float_arithmetic():
    contract = _contract()
    _expose_of(contract, "subscribers")["contract"]["schema"].append(
        {"name": "speed_ratio", "type": "double"}
    )
    _add_invariant(
        contract, "subscribers", {"id": "ratio_positive", "check": "speed_ratio + 1 > 2"}
    )
    finding = [n for n in _notes(contract) if "float" in n.message][0]
    _expect(
        finding, "note", "/exposes/4/contract/invariants/3/check", "arithmetic on a float column"
    )
    assert _errors(contract) == []


def test_note_format_and_a_regex_rule():
    contract = _contract()
    _column(contract, "invoices", "tower_id")["validationRules"] = [
        {"type": "regex", "constraint": "^[A-Z]+$"}
    ]
    finding = [n for n in _notes(contract) if "regex" in n.message][0]
    _expect(
        finding,
        "note",
        "/exposes/0/contract/schema/9/identifierFormat",
        "both an identifier format and a regex rule",
    )
    assert _errors(contract) == []


def test_note_quality_expression_that_is_fluid_sql():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["quality"] = [
        {"rule": "net_not_negative", "type": "custom", "expression": "net >= 0"}
    ]
    finding = [n for n in _notes(contract) if "quality" in n.label][0]
    _expect(
        finding,
        "note",
        "/exposes/0/contract/quality/0/expression",
        "this expression is FLUID SQL/1",
    )
    assert finding.label == "exposes[invoices].contract.quality[net_not_negative]"
    assert _errors(contract) == []


def test_no_note_for_a_quality_expression_outside_fluid_sql():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["quality"] = [
        {"rule": "raw_sql", "type": "custom", "expression": "SELECT 1; DROP TABLE x"}
    ]
    assert [n for n in _notes(contract) if "quality" in n.label] == []


# -- pointers ----------------------------------------------------------------------------------------------------


def test_every_pointer_resolves_in_a_contract_with_many_findings():
    contract = _contract()
    _invariant(contract, "invoices", "due_after_issue")["check"] = "issue_date <="
    _invariant(contract, "invoices", "gross_from_net")["derive"] = "grosss"
    _aggregate(contract)["from"] = "nope"
    _dependency(contract)["from"] = ["ghost"]
    _format(contract, [{"mask": "--"}])
    _column(contract, "invoices", "net")["placeholders"] = {}
    _shared(contract, "invoices", ["contract_id"])
    findings = _lint(contract)  # _lint resolves every pointer
    assert len([f for f in findings if f.severity == "error"]) >= 7


# -- round 1 of the gate (defect scan and delivery review, 2026-11-16) --------------


def test_contract_names_compare_in_nfc():
    """RFC 2.3: the contract's own texts are NFC, so an NFD column name, enum value or
    placeholder is named by either spelling."""
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["schema"].append(
        {
            "name": "Café",
            "type": "string",
            "validationRules": [{"type": "enum", "constraint": "Café,b"}],
        }
    )
    for text in ("\"Café\" = 'Café'", "\"Café\" = 'Café'"):
        _add_invariant(contract, "invoices", {"id": f"nfc_{len(text)}", "check": text})
    assert _errors(contract) == []


def test_two_derives_without_when_are_two_sources():
    contract = _contract()
    j = _add_invariant(
        contract, "invoices", {"id": "z", "derive": "gross", "expr": "ROUND(net, 2)"}
    )
    finding = _pick(_lint(contract), "covers none")
    assert "gross_from_net has no when" in finding.message
    assert "derive invariant z" in finding.message
    assert finding.pointer.startswith("/exposes/0/contract/schema/")
    assert j > 0


def test_a_derive_with_a_when_before_an_unconditional_one_is_one_source():
    contract = _contract()
    _invariant(contract, "invoices", "gross_from_net")["when"] = "net > 0"
    _add_invariant(contract, "invoices", {"id": "z", "derive": "gross", "expr": "ROUND(net, 2)"})
    assert _errors(contract) == []


def test_an_unreachable_owner_is_found_in_bounded_time():
    """paths() searches only exposes that can reach the goal, so a typo'd member of a
    dense contract is reported at once (it took 9 s at ten exposes, then hung)."""
    import time

    contract = _contract()
    names = [f"d{i}" for i in range(14)]
    for name in names:
        rels = [_rel(f"to_{other}", other, "k", "k") for other in names if other != name]
        contract["exposes"].append(
            _expose(
                name,
                [{"name": "k", "type": "string"}, {"name": "attr", "type": "string"}],
                semantics={"relationships": rels},
            )
        )
    _column(contract, "d0", "attr")["sharedAttribute"] = "dense"
    _column(contract, "d1", "attr")["sharedAttribute"] = "dense"
    contract["exposes"].append(
        _expose("island", [{"name": "attr", "type": "string", "sharedAttribute": "dense"}])
    )
    started = time.monotonic()
    findings = _lint(contract)
    assert time.monotonic() - started < 20
    assert any("has no owner" in f.message or "more paths" in f.message for f in findings)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda inv: inv.pop("aggregate"),
        lambda inv: inv["aggregate"].pop("via"),
        lambda inv: inv["aggregate"].pop("function"),
    ],
    ids=["derive-alone", "aggregate-without-via", "aggregate-without-function"],
)
def test_a_form_the_schema_refuses_gets_no_second_message(mutate):
    contract = _contract()
    inv = _invariant(contract, "invoices", "billed_base_fee_from_lines")
    mutate(inv)
    assert [f for f in _errors(contract) if "None" in f.message] == []


def test_a_dependency_without_to_gets_no_both_ways_message():
    contract = _contract()
    _dependency(contract).pop("to")
    assert [f for f in _errors(contract) if "bothWays" in f.message] == []


def test_sum_without_a_column_says_so():
    contract = _contract()
    _invariant(contract, "invoices", "billed_base_fee_from_lines")["aggregate"].pop("column")
    finding = _pick(_lint(contract), "needs a column")
    assert "None" not in finding.message


def test_a_foreign_key_format_its_target_lacks_is_an_error():
    contract = _contract()
    _column(contract, "invoices", "contract_id")["identifierFormat"] = [{"mask": "999"}]
    finding = _pick(_lint(contract), "has no format, declared or inherited")
    assert finding.pointer.endswith("/identifierFormat")


def test_exposes_without_an_id_do_not_crash_the_shared_attribute_check():
    contract = _contract()
    for expose_id in (None, 5):
        expose = _expose("x", [{"name": "s1", "type": "string", "sharedAttribute": "s"}])
        if expose_id is None:
            del expose["exposeId"]
        else:
            expose["exposeId"] = expose_id
        contract["exposes"].append(expose)
    _lint(contract)  # the schema reports the ids; the lint must not raise


def test_mixed_type_prefixes_on_a_foreign_key_do_not_crash():
    contract = _contract()
    for expose_id in ("invoices", "contracts"):
        _column(contract, expose_id, "contract_id")["identifierFormat"] = [
            {"prefixes": ["a", 1], "mask": "99"}
        ]
    _lint(contract)


def test_a_huge_decimal_precision_has_no_type_and_does_not_crash():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["schema"].append(
        {"name": "huge", "type": "decimal(" + "9" * 4400 + ",2)"}
    )
    _add_invariant(contract, "invoices", {"id": "huge_check", "check": "huge > 0"})
    finding = _pick(_lint(contract), "has no FLUID SQL/1 type")
    assert "huge" in finding.message


def test_the_quality_note_has_its_own_fix():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["quality"] = [
        {"rule": "net_positive", "expression": "net > 0", "severity": "error"}
    ]
    note = _pick(_lint(contract), "is FLUID SQL/1", severity="note")
    assert note.fix.startswith("move it to contract.invariants[]")


# -- round 2 of the gate --------------------------------------------------------------


def test_a_decimal_precision_with_thousands_of_leading_zeros_does_not_crash():
    contract = _contract()
    _expose_of(contract, "invoices")["contract"]["schema"].append(
        {"name": "zeros", "type": "decimal(10," + "0" * 4400 + "2)"}
    )
    _lint(contract)


def test_a_chained_foreign_key_inherits_the_root_format():
    """lines.invoice_id -> invoices.invoice_id (no format) -> its own target: the middle
    column inherits, so a foreign key repeating the root format is fine (RFC 3)."""
    contract = _contract()
    fmt = [{"prefixes": ["C"], "mask": "999"}]
    _column(contract, "contracts", "contract_id")["identifierFormat"] = fmt
    _expose_of(contract, "invoice_lines")["contract"]["schema"].append(
        {"name": "contract_id", "type": "string", "identifierFormat": fmt}
    )
    _expose_of(contract, "invoice_lines")["semantics"]["relationships"].append(
        _rel("via_invoice", "invoices", "contract_id", "contract_id")
    )
    assert [f for f in _errors(contract) if "format" in f.message] == []


def test_a_long_chain_sharing_one_attribute_is_linear_enough():
    import time

    contract = _contract()
    names = [f"c{i}" for i in range(200)]
    for i, name in enumerate(names):
        rels = [_rel("next", names[i + 1], "k", "k")] if i + 1 < len(names) else []
        contract["exposes"].append(
            _expose(
                name,
                [
                    {"name": "k", "type": "string"},
                    {"name": "seg", "type": "string", "sharedAttribute": "chain"},
                ],
                semantics={"relationships": rels},
            )
        )
    started = time.monotonic()
    _lint(contract)
    assert time.monotonic() - started < 20
