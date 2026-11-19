# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Declared invariants, identifier formats, placeholder values and shared attributes as
ODCS quality rules (``RFC-data-rules.md``, ODCS exporter).

Pinned here: the export validates against the bundled ODCS v3.1.0 schema; every
generated rule is marked ``fluidGenerated`` and carries the declaration's operator and
unit; a declaration with no exact SQL becomes a text rule and a logged warning; the
generated queries run in DuckDB and count what the compiler counts; FLUID -> ODCS ->
FLUID keeps every declaration and ODCS -> FLUID -> ODCS is a fixed point; the import
drops marked rules only when ``fluidExtras`` restores the declarations; and placeholders
are exempt from the existing ``invalidValues`` rules.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

import pytest

from fluid_build.providers.odcs import validation
from fluid_build.providers.odcs.mappers.data_rules import FLUID_GENERATED_PROPERTY, is_generated
from fluid_build.providers.odcs.provider import OdcsProvider

pytestmark = pytest.mark.unit


def _binding(platform: str, table: str) -> Dict[str, Any]:
    if platform == "local":
        return {"platform": "local", "format": "parquet", "location": {"table": table}}
    return {
        "platform": "snowflake",
        "format": "snowflake_table",
        "location": {"database": "BILLING", "schema": "CDP", "table": table.upper()},
    }


def _contract(platform: str = "snowflake") -> Dict[str, Any]:
    def expose(expose_id: str, schema: list, **extra: Any) -> Dict[str, Any]:
        return {
            "exposeId": expose_id,
            "kind": "table",
            "binding": _binding(platform, expose_id),
            "contract": {"schema": schema, **extra.pop("contract", {})},
            **extra,
        }

    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.billing.invoices",
        "name": "Invoices",
        "metadata": {"layer": "Gold", "owner": {"team": "billing-platform"}},
        "exposes": [
            expose(
                "invoices",
                [
                    {"name": "invoice_id", "type": "string", "required": True},
                    {"name": "contract_id", "type": "string"},
                    {"name": "net", "type": "decimal(12,2)"},
                    {"name": "tax_rate", "type": "decimal(5,4)"},
                    {"name": "gross", "type": "decimal(12,2)"},
                    {"name": "issue_date", "type": "date"},
                    {"name": "due_date", "type": "date"},
                    {"name": "status", "type": "string"},
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
                        "validationRules": [
                            {"type": "regex", "constraint": "^P"},
                        ],
                    },
                    {
                        "name": "channel",
                        "type": "string",
                        "placeholders": {"values": ["?"]},
                        "validationRules": [{"type": "enum", "constraint": "web, shop"}],
                    },
                ],
                contract={
                    "invariants": [
                        {
                            "id": "gross_from_net",
                            "derive": "gross",
                            "expr": "ROUND(net * (1 + tax_rate), 2)",
                        },
                        {
                            "id": "due_after_issue",
                            "check": "issue_date <= due_date",
                            "mustBeLessOrEqualTo": 50,
                            "unit": "percent",
                            "severity": "warning",
                        },
                        {"id": "fee_is_monthly", "check": "net >= contract.monthly_fee"},
                        {
                            "id": "date_by_contract",
                            "dependency": {"from": ["contract_id"], "to": ["issue_date"]},
                        },
                    ]
                },
                semantics={
                    "relationships": [
                        {
                            "name": "contract",
                            "to": "contracts",
                            "fromColumns": ["contract_id"],
                            "toColumns": ["contract_id"],
                            "cardinality": "many_to_one",
                            "sharedColumns": ["status"],
                        }
                    ]
                },
            ),
            expose(
                "contracts",
                [
                    {"name": "contract_id", "type": "string", "required": True},
                    {"name": "monthly_fee", "type": "decimal(12,2)"},
                    {"name": "status", "type": "string"},
                ],
            ),
        ],
    }


def _render(contract: Dict[str, Any], expose_id: str = "invoices") -> Dict[str, Any]:
    return OdcsProvider().render(copy.deepcopy(contract), fmt=None, expose_id=expose_id)


def _rules(odcs: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every quality rule of the document by name (or id), object and property level."""
    found: Dict[str, Dict[str, Any]] = {}
    for obj in odcs["schema"]:
        arrays: List[Any] = [obj.get("quality")]
        arrays += [p.get("quality") for p in obj.get("properties") or []]
        for array in arrays:
            for rule in array or []:
                found[rule.get("name") or rule["id"]] = rule
    return found


def _property(odcs: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(p for p in odcs["schema"][0]["properties"] if p["name"] == name)


def test_the_export_validates_against_the_bundled_odcs_schema() -> None:
    odcs = _render(_contract())
    assert validation.collect_errors(odcs, validation.load_schema()) == []


def test_each_declaration_becomes_a_marked_rule() -> None:
    rules = _rules(_render(_contract()))
    generated = {name for name, rule in rules.items() if is_generated(rule)}
    assert generated == {
        "gross_from_net",
        "due_after_issue",
        "fee_is_monthly",
        "date_by_contract",
        "product_code.identifierFormat",
        "product_code.placeholders",
        "channel.placeholders",
        "contract.status",
    }
    due = rules["due_after_issue"]
    assert due["type"] == "sql" and due["dimension"] == "consistency"
    assert due["mustBeLessOrEqualTo"] == 50 and due["unit"] == "percent"
    assert due["severity"] == "warning"
    assert due["query"].startswith('SELECT CASE WHEN "covered" = 0 THEN 0')
    assert "FROM ${table} AS" in due["query"]
    gross = rules["gross_from_net"]
    assert gross["mustBe"] == 0 and gross["unit"] == "rows"
    # A linked expose is named by its binding location, quoted for the platform.
    assert '"BILLING"."CDP"."CONTRACTS"' in rules["fee_is_monthly"]["query"]
    fmt = rules["product_code.identifierFormat"]
    assert fmt["type"] == "library" and fmt["metric"] == "invalidValues"
    assert fmt["arguments"] == {"pattern": "^((P[0-9]{5})|XXXXX|TBD_[\\s\\S]*)$"}
    assert fmt["dimension"] == "conformity"
    assert rules["product_code.placeholders"]["dimension"] == "completeness"
    unbounded = rules["channel.placeholders"]
    assert unbounded["type"] == "text" and "no operator bounds it" in unbounded["description"]
    for name in generated:
        assert {"property": FLUID_GENERATED_PROPERTY, "value": True} in rules[name][
            "customProperties"
        ]


def test_properties_carry_the_pattern_and_the_derivation() -> None:
    odcs = _render(_contract())
    assert _property(odcs, "product_code")["logicalTypeOptions"] == {
        "pattern": "^((P[0-9]{5})|XXXXX|TBD_[\\s\\S]*)$"
    }
    gross = _property(odcs, "gross")
    assert gross["transformLogic"] == "ROUND(net * (1 + tax_rate), 2)"
    assert gross["transformSourceObjects"] == ["invoices"]


def test_placeholders_are_exempt_from_the_existing_value_rules() -> None:
    rules = _rules(_render(_contract()))
    regex = rules["product_code-regex"]
    assert regex["arguments"]["pattern"] == "^P|^(XXXXX)$|^(TBD_)[\\s\\S]*"
    enum = rules["channel-enum"]
    assert enum["arguments"]["validValues"] == ["web", "shop", "?"]


def test_an_enum_with_placeholder_prefixes_counts_in_the_platform_dialect() -> None:
    contract = _contract()
    channel = contract["exposes"][0]["contract"]["schema"][9]
    channel["placeholders"] = {"prefixes": ["n/a"]}
    enum = _rules(_render(contract))["channel-enum"]
    assert enum["type"] == "sql" and enum["mustBe"] == 0
    assert "SUBSTR(\"channel\", 1, 3) = 'n/a'" in enum["query"]
    contract["exposes"][0]["binding"] = {"platform": "kafka", "format": "kafka_topic"}
    assert _rules(_render(contract))["channel-enum"]["type"] == "text"


def test_a_rule_with_no_exact_sql_is_a_text_rule_and_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    contract = _contract()
    contract["exposes"][1]["binding"] = {"platform": "gcp", "format": "bigquery_table"}
    with caplog.at_level(logging.WARNING):
        rules = _rules(_render(contract))
    rule = rules["fee_is_monthly"]
    assert rule["type"] == "text" and is_generated(rule)
    assert "Not executable here" in rule["description"] and "bigquery" in rule["description"]
    assert any("fee_is_monthly" in r.getMessage() for r in caplog.records)
    assert rules["gross_from_net"]["type"] == "sql"


def test_round_trip_keeps_every_declaration_and_is_a_fixed_point() -> None:
    contract = _contract()
    provider = OdcsProvider()
    odcs = _render(contract)
    back = provider.import_contract(copy.deepcopy(odcs))
    invoices = next(e for e in back["exposes"] if e["exposeId"] == "invoices")
    original = contract["exposes"][0]
    assert invoices["contract"]["invariants"] == original["contract"]["invariants"]
    columns = {c["name"]: c for c in invoices["contract"]["schema"]}
    assert columns["product_code"]["identifierFormat"] == [{"prefixes": ["P"], "mask": "99999"}]
    assert columns["product_code"]["placeholders"]["mustBeLessOrEqualTo"] == 1
    assert invoices["semantics"]["relationships"][0]["sharedColumns"] == ["status"]
    # The generated rules are not imported as hand-written ones ...
    for column in columns.values():
        assert not any(is_generated(rule) for rule in column.get("quality") or [])
    # ... and the re-export generates them again, unchanged.
    whole = copy.deepcopy(back)
    whole["exposes"].append(copy.deepcopy(contract["exposes"][1]))
    again = provider.render(whole, fmt=None, expose_id="invoices")
    # The contract id of a re-scoped import is derived again (not part of this mapping).
    assert {k: v for k, v in again.items() if k != "id"} == {
        k: v for k, v in odcs.items() if k != "id"
    }


def test_without_fluid_extras_the_marked_rules_stay_ordinary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODCS_FLUID_EXTRAS", "false")
    odcs = _render(_contract())
    back = OdcsProvider().import_contract(copy.deepcopy(odcs))
    columns = {c["name"]: c for c in back["exposes"][0]["contract"]["schema"]}
    assert "invariants" not in back["exposes"][0]["contract"]
    assert any(is_generated(rule) for rule in columns["product_code"]["quality"])


def test_quality_off_means_no_generated_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODCS_INCLUDE_QUALITY", "false")
    assert not any(is_generated(rule) for rule in _rules(_render(_contract())).values())


def test_a_contract_below_the_preview_exports_as_before() -> None:
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    for expose in contract["exposes"]:
        expose["contract"].pop("invariants", None)
        expose.pop("semantics", None)
        for column in expose["contract"]["schema"]:
            for key in ("identifierFormat", "placeholders"):
                column.pop(key, None)
    rules = _rules(_render(contract))
    assert not any(is_generated(rule) for rule in rules.values())
    assert rules["channel-enum"]["arguments"]["validValues"] == ["web", "shop"]
    assert rules["product_code-regex"]["arguments"]["pattern"] == "^P"


def test_the_queries_run_and_count_what_the_rules_say() -> None:
    duckdb = pytest.importorskip("duckdb")
    contract = _contract(platform="local")
    rules = _rules(_render(contract))
    db = duckdb.connect()
    db.execute(
        "CREATE TABLE invoices (invoice_id VARCHAR, contract_id VARCHAR, net DECIMAL(12,2), "
        "tax_rate DECIMAL(5,4), gross DECIMAL(12,2), issue_date DATE, due_date DATE, "
        "status VARCHAR, product_code VARCHAR, channel VARCHAR)"
    )
    db.execute(
        "INSERT INTO invoices VALUES "
        "('i1','c1',100,0.2,120,'2026-01-01','2026-02-01','A','P00001','web'),"
        "('i2','c1',100,0.2,121,'2026-03-01','2026-02-01','B','TBD_7','web'),"
        "('i3',NULL,1,0,1,NULL,'2026-01-01','A','XXXXX','?'),"
        "('i4','c2',5,0,5,'2026-01-01','2026-01-02','A','P00002','shop')"
    )
    db.execute(
        "CREATE TABLE contracts (contract_id VARCHAR, monthly_fee DECIMAL(12,2), status VARCHAR)"
    )
    db.execute("INSERT INTO contracts VALUES ('c1',10,'A'),('c2',10,'A')")

    def run(name: str) -> float:
        query = rules[name]["query"].replace("${table}", "invoices")
        return float(db.execute(query).fetchone()[0])

    assert run("gross_from_net") == 1
    assert run("due_after_issue") == 50.0  # i2 is due early, i3 has no issue date
    assert run("fee_is_monthly") == 2  # i3 has no contract, i4's net is below the fee
    assert run("date_by_contract") == 1  # c1 holds two issue dates
    assert run("contract.status") == 1  # i2 says B, its contract A
    assert run("product_code.placeholders") == 50.0


def test_a_link_to_an_expose_with_no_table_is_a_text_rule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    contract = _contract(platform="local")
    contract["exposes"][1]["binding"]["location"] = {"path": "contracts.parquet"}
    with caplog.at_level(logging.WARNING):
        rules = _rules(_render(contract))
    for name in ("fee_is_monthly", "contract.status"):
        assert rules[name]["type"] == "text"
        assert "names no table" in rules[name]["description"]
    assert rules["gross_from_net"]["type"] == "sql"
    warnings = [r.getMessage() for r in caplog.records if "text rules" in r.getMessage()]
    assert len(warnings) == 1  # one warning per expose and reason, naming every rule
    assert "fee_is_monthly" in warnings[0] and "contract.status" in warnings[0]
    assert "fluid validate" in warnings[0] and "fluidExtras" in warnings[0]


def test_the_warning_says_when_fluid_extras_is_off(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODCS_FLUID_EXTRAS", "false")
    contract = _contract()
    contract["exposes"][0]["binding"] = {"platform": "kafka", "format": "kafka_topic"}
    with caplog.at_level(logging.WARNING):
        _render(contract)
    warnings = [r.getMessage() for r in caplog.records if "text rules" in r.getMessage()]
    assert len(warnings) == 1 and "ODCS_FLUID_EXTRAS=false" in warnings[0]


def test_an_edit_after_import_reaches_the_next_export() -> None:
    contract = _contract()
    provider = OdcsProvider()
    back = provider.import_contract(copy.deepcopy(_render(contract)))
    back["exposes"].append(copy.deepcopy(contract["exposes"][1]))
    invoices = back["exposes"][0]
    columns = {c["name"]: c for c in invoices["contract"]["schema"]}
    columns["product_code"]["identifierFormat"] = [{"prefixes": ["Q"], "mask": "999"}]
    invoices["contract"]["invariants"][0]["expr"] = "ROUND(net * (2 + tax_rate), 2)"
    odcs = provider.render(back, fmt=None, expose_id="invoices")
    pattern = "^((Q[0-9]{3})|XXXXX|TBD_[\\s\\S]*)$"
    assert _property(odcs, "product_code")["logicalTypeOptions"]["pattern"] == pattern
    assert _rules(odcs)["product_code.identifierFormat"]["arguments"]["pattern"] == pattern
    assert _property(odcs, "gross")["transformLogic"] == "ROUND(net * (2 + tax_rate), 2)"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["exposes"][0]["contract"].__setitem__("invariants", 5),
        lambda c: c["exposes"][0]["contract"]["invariants"].append(
            {"id": ["x"], "derive": "gross", "expr": "1"}
        ),
        lambda c: c["exposes"][1]["binding"]["location"].__setitem__("table", ""),
        lambda c: c["exposes"][1]["binding"]["location"].__setitem__("table", "a\nb"),
        lambda c: c["exposes"][0]["contract"]["schema"][9].update(
            name="", placeholders={"prefixes": ["Z"]}
        ),
    ],
)
def test_a_malformed_contract_still_exports(mutate: Any) -> None:
    contract = _contract()
    mutate(contract)
    odcs = _render(contract)
    assert odcs["schema"][0]["name"] == "invoices"


def test_an_unquotable_linked_table_is_a_text_rule() -> None:
    contract = _contract()
    contract["exposes"][1]["binding"]["location"]["table"] = "a\nb"
    rule = _rules(_render(contract))["fee_is_monthly"]
    assert rule["type"] == "text" and "can quote" in rule["description"]


def test_every_port_of_a_contract_compiles_its_rules_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.data_rules import _compile
    from fluid_build.providers.odcs.mappers import data_rules

    calls: List[int] = []
    real = _compile.compile_rules

    def counting(contract: Any, dialect: Any = None) -> Any:
        calls.append(1)
        return real(contract, dialect)

    monkeypatch.setattr(_compile, "compile_rules", counting)
    monkeypatch.setattr(data_rules, "_COMPILED", type(data_rules._COMPILED)())
    contract = _contract()
    for expose_id in ("invoices", "contracts"):
        _render(contract, expose_id)
    assert len(calls) == 1
