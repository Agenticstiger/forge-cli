# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The rules about valid data compiled to SQL counts (``fluid_build.data_rules._compile``).

DuckDB runs every compiled query on seeded rows, and the counts must be the ones
``RFC-data-rules.md`` §1.3-§1.5 define: NULL rows covered and violating, a link that
reaches no row or a key two target rows hold reading as NULL (no fan-out), first-match
coverage of several derives, the g3 count of a dependency per direction, summaries over
children, shared attributes along a route, placeholder counts, and hostile names.
Every dialect's query parses with sqlglot.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.data_rules import lint_contract
from fluid_build.data_rules._compile import (
    EXECUTABLE,
    INFORMATIONAL,
    UNSUPPORTED,
    compile_rules,
    platform_dialect,
)

pytestmark = pytest.mark.unit

duckdb = pytest.importorskip("duckdb")

LOCAL = {"platform": "local", "format": "parquet"}


def _expose(expose_id: str, schema: list, binding: Any = LOCAL, **extra: Any) -> Dict[str, Any]:
    contract = {"schema": schema, **extra.pop("contract", {})}
    return {"exposeId": expose_id, "binding": binding, "contract": contract, **extra}


def _rel(name: str, to: str, frm: List[str], too: List[str], **extra: Any) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": frm,
        "toColumns": too,
        "cardinality": "many_to_one",
        **extra,
    }


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "x",
        "name": "x",
        "exposes": list(exposes),
    }


def _billing() -> Dict[str, Any]:
    return _contract(
        _expose(
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
                {"name": "status", "type": "string"},
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
                    {
                        "id": "billed",
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
                    {"id": "fee_is_monthly", "check": "billed_base_fee = contract.monthly_fee"},
                    {
                        "id": "dep",
                        "dependency": {"from": ["contract_id"], "to": ["issue_date"]},
                    },
                ]
            },
            semantics={
                "relationships": [
                    _rel(
                        "contract",
                        "contracts",
                        ["contract_id"],
                        ["contract_id"],
                        sharedColumns=["status"],
                    )
                ]
            },
        ),
        _expose(
            "invoice_lines",
            [
                {"name": "line_id", "type": "string"},
                {"name": "invoice_id", "type": "string"},
                {"name": "kind", "type": "string"},
                {"name": "amount", "type": "decimal(12,2)"},
            ],
            semantics={
                "relationships": [_rel("invoice", "invoices", ["invoice_id"], ["invoice_id"])]
            },
        ),
        _expose(
            "contracts",
            [
                {"name": "contract_id", "type": "string"},
                {"name": "monthly_fee", "type": "decimal(12,2)"},
                {"name": "status", "type": "string"},
            ],
        ),
    )


def _db(statements: List[str]) -> Any:
    db = duckdb.connect()
    for statement in statements:
        db.execute(statement)
    return db


def _billing_db() -> Any:
    return _db(
        [
            (
                "CREATE TABLE invoices (invoice_id VARCHAR, contract_id VARCHAR, net DECIMAL(12,2), "
                + "tax_rate DECIMAL(5,4), gross DECIMAL(12,2), billed_base_fee DECIMAL(12,2), "
                + "issue_date DATE, due_date DATE, status VARCHAR)"
            ),
            # i1 holds everything; i2 is off by a cent, due before issue and links to a key
            # two contracts hold; i3 has a NULL key and NULL dates; i4 links to no contract.
            (
                "INSERT INTO invoices VALUES "
                + "('i1','c1',100,0.2,120,10,'2026-01-01','2026-02-01','A'),"
                + "('i2','c2',100,0.2,121,0,'2026-03-01','2026-02-01','B'),"
                + "('i3',NULL,1,0,1,NULL,NULL,'2026-01-01','A'),"
                + "('i4','c9',1,0,1,0,'2026-01-01','2026-01-02','A')"
            ),
            (
                "CREATE TABLE invoice_lines (line_id VARCHAR, invoice_id VARCHAR, kind VARCHAR, "
                + "amount DECIMAL(12,2))"
            ),
            (
                "INSERT INTO invoice_lines VALUES ('l1','i1','base_fee',10),('l2','i1','other',5),"
                + "('l3','i2','other',3)"
            ),
            (
                "CREATE TABLE contracts (contract_id VARCHAR, monthly_fee DECIMAL(12,2), "
                + "status VARCHAR)"
            ),
            "INSERT INTO contracts VALUES ('c1',10,'A'),('c2',5,'B'),('c2',5,'B')",
        ]
    )


def _table(expose_id: str) -> str:
    return fs.sql_name("duckdb", expose_id)


def _run(db: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
    """``{rule id: (covered, violating, measure)}`` for every executable counted rule."""
    results: Dict[str, Any] = {}
    for rule in compile_rules(contract):
        if rule.status == EXECUTABLE and rule.counts is not None:
            covered, violating = db.execute(rule.counts(_table)).fetchone()
            (measure,) = db.execute(rule.measure(_table)).fetchone()
            results[rule.id] = (covered, violating, float(measure))
    return results


def _by_id(contract: Dict[str, Any]) -> Dict[str, Any]:
    return {rule.id: rule for rule in compile_rules(contract)}


def test_the_rfc_example_counts_what_the_rfc_defines() -> None:
    counts = _run(_billing_db(), _billing())
    assert counts["gross_from_net"] == (4, 1, 1.0)  # i2's gross is off by a cent
    # i2 is due before issue; i3's NULL date makes the comparison FALSE (§2.5): 2 of 4.
    assert counts["due_after_issue"] == (4, 2, 50.0)
    # i3's NULL billed fee is distinct from the summary of no children (ifNone 0).
    assert counts["billed"] == (4, 1, 1.0)
    # i2's key is held by two contracts (no match), i3's key is NULL, i4's matches nothing.
    assert counts["fee_is_monthly"] == (4, 3, 3.0)
    assert counts["dep"] == (3, 0, 0.0)  # three non-NULL keys, one value each
    # Only i1's link reaches one row: an ambiguous or missing link leaves a row uncovered.
    assert counts["contract.status"] == (1, 0, 0.0)


def test_a_link_never_counts_a_row_twice() -> None:
    db = _billing_db()
    db.execute("INSERT INTO contracts VALUES ('c1',10,'A')")  # c1 is now ambiguous too
    counts = _run(db, _billing())
    assert counts["fee_is_monthly"][:2] == (4, 4)
    assert counts["contract.status"][:2] == (0, 0)


def test_rule_metadata() -> None:
    rules = _by_id(_billing())
    assert rules["due_after_issue"].severity == "warning"
    assert rules["due_after_issue"].band.operator == "mustBeLessOrEqualTo"
    assert rules["due_after_issue"].band.unit == "percent"
    assert rules["gross_from_net"].band.operator == "mustBe"
    assert rules["fee_is_monthly"].reads == ("contracts",)
    assert rules["billed"].reads == ("invoice_lines",)
    assert rules["billed"].kind == "aggregate"
    assert rules["contract.status"].kind == "sharedColumns"
    assert all(r.dialect == "duckdb" for r in rules.values())


def test_several_derives_cover_rows_first_match() -> None:
    contract = _contract(
        _expose(
            "a",
            [
                {"name": "kind", "type": "string"},
                {"name": "x", "type": "decimal(10,2)"},
                {"name": "y", "type": "decimal(10,2)"},
            ],
            contract={
                "invariants": [
                    {"id": "small", "derive": "y", "expr": "x", "when": "x < 10"},
                    {"id": "double", "derive": "y", "expr": "x * 2", "when": "x < 100"},
                ]
            },
        )
    )
    db = _db(
        [
            "CREATE TABLE a (kind VARCHAR, x DECIMAL(10,2), y DECIMAL(10,2))",
            # 5 -> first derive (y = x holds); 50 -> second (y = 2x violated); 500 -> neither.
            "INSERT INTO a VALUES ('k',5,5),('k',50,50),('k',500,1),('k',NULL,NULL)",
        ]
    )
    counts = _run(db, contract)
    assert counts["small"][:2] == (1, 0)
    assert counts["double"][:2] == (1, 1)  # the 5 row belongs to the first derive only


def test_a_dependency_is_counted_by_g3_per_direction() -> None:
    contract = _contract(
        _expose(
            "g",
            [{"name": "gid", "type": "string"}, {"name": "gname", "type": "string"}],
            contract={
                "invariants": [
                    {
                        "id": "name_by_id",
                        "dependency": {"from": ["gid"], "to": ["gname"], "bothWays": True},
                        "mustBeLessOrEqualTo": 2,
                    }
                ]
            },
        )
    )
    db = _db(
        [
            "CREATE TABLE g (gid VARCHAR, gname VARCHAR)",
            # g1: a, a, b, NULL -> keeps the two a rows: 2 violating (a NULL to is a value).
            # g2: x, y (a tie) -> 1. A NULL key is not covered forward.
            (
                "INSERT INTO g VALUES ('g1','a'),('g1','a'),('g1','b'),('g1',NULL),"
                + "('g2','x'),('g2','y'),(NULL,'a')"
            ),
        ]
    )
    counts = _run(db, contract)
    assert counts["name_by_id"][:2] == (6, 3)
    # Reverse: rows with both sides: a -> g1, g1, NULL-key excluded; b, x, y once each.
    assert counts["name_by_id-reverse"][:2] == (5, 0)
    db.execute("INSERT INTO g VALUES ('g3','a')")
    assert _run(db, contract)["name_by_id-reverse"][:2] == (6, 1)


def test_summaries_count_min_max_and_if_none() -> None:
    contract = _contract(
        _expose(
            "p",
            [
                {"name": "pid", "type": "string"},
                {"name": "n", "type": "integer"},
                {"name": "first", "type": "string"},
                {"name": "top", "type": "decimal(8,2)"},
            ],
            contract={
                "invariants": [
                    {
                        "id": "n_children",
                        "derive": "n",
                        "aggregate": {"function": "count", "from": "c", "via": "parent"},
                    },
                    {
                        "id": "first_name",
                        "derive": "first",
                        "aggregate": {
                            "function": "min",
                            "column": "name",
                            "from": "c",
                            "via": "parent",
                            "ifNone": "none",
                        },
                    },
                    {
                        "id": "top_value",
                        "derive": "top",
                        "aggregate": {
                            "function": "max",
                            "column": "v",
                            "from": "c",
                            "via": "parent",
                            "filter": "v < 100",
                        },
                    },
                ]
            },
        ),
        _expose(
            "c",
            [
                {"name": "cid", "type": "string"},
                {"name": "pid", "type": "string"},
                {"name": "name", "type": "string"},
                {"name": "v", "type": "decimal(8,2)"},
            ],
            semantics={"relationships": [_rel("parent", "p", ["pid"], ["pid"])]},
        ),
    )
    db = _db(
        [
            "CREATE TABLE p (pid VARCHAR, n INTEGER, first VARCHAR, top DECIMAL(8,2))",
            "INSERT INTO p VALUES ('p1',2,'B',5),('p2',0,'none',NULL),('p3',1,'x',1)",
            "CREATE TABLE c (cid VARCHAR, pid VARCHAR, name VARCHAR, v DECIMAL(8,2))",
            (
                "INSERT INTO c VALUES ('1','p1','B',5),('2','p1','a',500),('3','p3','y',2),"
                + "('4',NULL,'z',1)"
            ),
        ]
    )
    counts = _run(db, contract)
    assert counts["n_children"][:2] == (3, 0)  # p2 has no child: count 0, not NULL
    # min is by code point: 'B' < 'a'; p2 has none (ifNone); p3's is 'y', not 'x'.
    assert counts["first_name"][:2] == (3, 1)
    # p1's 500 is filtered out; p2's NULL matches the summary of nothing; p3 is 2, not 1.
    assert counts["top_value"][:2] == (3, 1)


def _segments() -> Dict[str, Any]:
    return _contract(
        _expose(
            "accounts",
            [
                {"name": "account_id", "type": "string"},
                {"name": "segment", "type": "string", "sharedAttribute": "customer_segment"},
            ],
        ),
        _expose(
            "contracts",
            [{"name": "contract_id", "type": "string"}, {"name": "account_id", "type": "string"}],
            semantics={
                "relationships": [_rel("account", "accounts", ["account_id"], ["account_id"])]
            },
        ),
        _expose(
            "invoices",
            [
                {"name": "invoice_id", "type": "string"},
                {"name": "contract_id", "type": "string"},
                {
                    "name": "customer_segment",
                    "type": "string",
                    "sharedAttribute": "customer_segment",
                },
            ],
            semantics={
                "relationships": [_rel("contract", "contracts", ["contract_id"], ["contract_id"])]
            },
        ),
    )


def test_a_shared_attribute_follows_its_route_without_fan_out() -> None:
    contract = _segments()
    rules = _by_id(contract)
    rule = rules["customer_segment.invoices.customer_segment"]
    assert rule.status == EXECUTABLE and rule.reads == ("contracts", "accounts")
    db = _db(
        [
            "CREATE TABLE accounts (account_id VARCHAR, segment VARCHAR)",
            "INSERT INTO accounts VALUES ('a1','SMB'),('a2','ENT'),('a3','X'),('a3','Y')",
            "CREATE TABLE contracts (contract_id VARCHAR, account_id VARCHAR)",
            (
                "INSERT INTO contracts VALUES ('c1','a1'),('c2','a2'),('c3',NULL),('c4','a3'),"
                + "('c5','a1'),('c5','a2')"
            ),
            (
                "CREATE TABLE invoices (invoice_id VARCHAR, contract_id VARCHAR, "
                + "customer_segment VARCHAR)"
            ),
            # i1 holds, i2 differs, i3's contract has no account, i4's account is ambiguous,
            # i5's contract is ambiguous, i6 has no contract, i7's contract does not exist.
            (
                "INSERT INTO invoices VALUES ('i1','c1','SMB'),('i2','c2','SMB'),('i3','c3','SMB'),"
                + "('i4','c4','X'),('i5','c5','SMB'),('i6',NULL,'SMB'),('i7','c9','SMB')"
            ),
        ]
    )
    counts = _run(db, contract)
    assert counts["customer_segment.invoices.customer_segment"][:2] == (2, 1)
    assert set(counts) == {"customer_segment.invoices.customer_segment"}


def test_placeholders_are_counted_and_exempt_from_formats() -> None:
    contract = _contract(
        _expose(
            "products",
            [
                {
                    "name": "code",
                    "type": "string",
                    "identifierFormat": [{"prefixes": ["P"], "mask": "99999"}],
                    "placeholders": {
                        "values": ["XXXXX"],
                        "prefixes": ["TBD_%"],
                        "mustBeLessOrEqualTo": 25,
                        "unit": "percent",
                    },
                },
                {"name": "alt", "type": "string", "placeholders": {"values": ["?"]}},
            ],
        ),
        _expose(
            "sales",
            [
                {
                    "name": "code",
                    "type": "string",
                    "identifierFormat": [{"prefixes": ["P"], "mask": "99999"}],
                }
            ],
            semantics={"relationships": [_rel("product", "products", ["code"], ["code"])]},
        ),
    )
    rules = _by_id(contract)
    fmt = rules["code.identifierFormat"]
    assert fmt.pattern == "^((P[0-9]{5})|XXXXX|TBD_%.*)$"
    # A foreign key inherits the placeholders of the column it copies (RFC §3).
    sales = [r for r in compile_rules(contract) if r.expose == "sales"][0]
    assert sales.pattern == fmt.pattern
    assert rules["alt.placeholders"].status == INFORMATIONAL
    db = _db(
        [
            "CREATE TABLE products (code VARCHAR, alt VARCHAR)",
            # TBD_x does not start with the prefix TBD_% (SUBSTR, not LIKE): % is literal.
            (
                "INSERT INTO products VALUES ('P00001',''),('XXXXX',''),('TBD_%1',''),('TBD_x',''),"
                + "(NULL,'')"
            ),
            "CREATE TABLE sales (code VARCHAR)",
            "INSERT INTO sales VALUES ('P00002'),('TBD_%9'),('p00003'),(NULL)",
        ]
    )
    counts = _run(db, contract)
    assert counts["code.placeholders"] == (5, 2, 40.0)
    # The format: NULL is no value; TBD_x is neither the mask nor a placeholder.
    formats = {r.expose: r for r in compile_rules(contract) if r.kind == "identifierFormat"}
    assert db.execute(formats["products"].counts(_table)).fetchone() == (4, 1)
    covered, violating = db.execute(formats["sales"].counts(_table)).fetchone()
    assert (covered, violating) == (3, 1)  # lower-case p fails; the inherited TBD_% is exempt


def test_percent_with_no_covered_row_is_zero() -> None:
    contract = _contract(
        _expose(
            "a",
            [{"name": "x", "type": "integer"}],
            contract={
                "invariants": [
                    {
                        "id": "pos",
                        "check": "x > 0",
                        "when": "x IS NOT NULL",
                        "mustBeLessThan": 1,
                        "unit": "percent",
                    }
                ]
            },
        )
    )
    db = _db(["CREATE TABLE a (x INTEGER)", "INSERT INTO a VALUES (NULL)"])
    assert _run(db, contract)["pos"] == (0, 0, 0.0)


def test_hostile_names_and_literals_stay_inside_their_quotes() -> None:
    odd = 'a"b;--/*{{x}}'
    rel = 'r`{%raw%}"'
    contract = _contract(
        _expose(
            "t1",
            [{"name": odd, "type": "string"}, {"name": "k", "type": "string"}],
            contract={
                "invariants": [
                    {
                        "id": "hostile",
                        "check": '"' + odd.replace('"', '""') + "\" <> 'it''s \\ {{y}}' "
                        'AND "' + rel.replace('"', '""') + '".v IS NOT NULL',
                    }
                ]
            },
            semantics={"relationships": [_rel(rel, "t2", ["k"], ["k"])]},
        ),
        _expose("t2", [{"name": "k", "type": "string"}, {"name": "v", "type": "string"}]),
    )
    assert not [f for f in lint_contract(contract) if f.severity == "error"]
    q = lambda name: fs.sql_name("duckdb", name)  # noqa: E731
    db = _db(
        [
            f"CREATE TABLE t1 ({q(odd)} VARCHAR, k VARCHAR)",
            "INSERT INTO t1 VALUES ('fine','1'),('it''s \\ {{y}}','1'),('fine','2')",
            "CREATE TABLE t2 (k VARCHAR, v VARCHAR)",
            "INSERT INTO t2 VALUES ('1','v')",
        ]
    )
    assert _run(db, contract)["hostile"][:2] == (3, 2)


def test_what_has_no_exact_sql_is_unsupported_with_a_reason() -> None:
    contract = _billing()
    contract["exposes"][2]["binding"] = {"platform": "snowflake", "format": "snowflake_table"}
    contract["exposes"][0]["contract"]["invariants"] += [
        {"id": "bad_text", "check": "net LIKE 'x%'"},
        {"id": "two_forms", "check": "net > 0", "dependency": {"from": ["net"], "to": ["gross"]}},
    ]
    kafka = _expose(
        "events",
        [{"name": "x", "type": "integer"}],
        binding={"platform": "kafka", "format": "kafka_topic"},
        contract={"invariants": [{"id": "positive", "check": "x > 0"}]},
    )
    contract["exposes"].append(kafka)
    rules = _by_id(contract)
    assert rules["fee_is_monthly"].status == UNSUPPORTED
    assert "snowflake" in rules["fee_is_monthly"].reason
    assert rules["contract.status"].status == UNSUPPORTED
    assert rules["bad_text"].status == UNSUPPORTED
    assert "validator refuses it" in rules["bad_text"].reason
    assert rules["two_forms"].status == UNSUPPORTED
    assert rules["positive"].status == UNSUPPORTED
    assert "kafka" in rules["positive"].reason
    assert rules["gross_from_net"].status == EXECUTABLE


def test_a_filter_through_a_child_link_is_unsupported() -> None:
    contract = _billing()
    contract["exposes"][1]["semantics"]["relationships"].append(
        _rel("inv2", "invoices", ["invoice_id"], ["invoice_id"])
    )
    agg = contract["exposes"][0]["contract"]["invariants"][2]["aggregate"]
    agg["filter"] = "inv2.status = 'A'"
    rule = _by_id(contract)["billed"]
    assert rule.status == UNSUPPORTED and "filter" in rule.reason


def test_nothing_below_the_preview_version() -> None:
    contract = _billing()
    contract["fluidVersion"] = "0.7.6"
    assert compile_rules(contract) == []


def test_platform_dialects() -> None:
    cases = {
        ("local", "parquet"): "duckdb",
        ("snowflake", "snowflake_table"): "snowflake",
        ("gcp", "bigquery_table"): "bigquery",
        ("databricks", "delta_table"): "databricks",
        ("postgres", "postgres_table"): "postgres",
        ("aws", "athena_table"): "athena",
        ("aws", "glue_table"): "athena",
        ("aws", "redshift_table"): None,
        ("gcp", "gcs_file"): None,
        ("kafka", "kafka_topic"): None,
    }
    for (platform, fmt), dialect in cases.items():
        assert platform_dialect({"binding": {"platform": platform, "format": fmt}})[0] == dialect
    assert platform_dialect({})[0] is None


@pytest.mark.parametrize("dialect", fs.DIALECTS)
def test_every_dialect_writes_sql_sqlglot_parses(dialect: str) -> None:
    sqlglot = pytest.importorskip("sqlglot")
    contract = copy.deepcopy(_billing())
    contract["exposes"].extend(_segments()["exposes"][:1])
    rules = compile_rules(contract, dialect=dialect)
    assert rules and all(r.status != UNSUPPORTED for r in rules)
    read = {"postgres": "postgres", "athena": "athena"}.get(dialect, dialect)
    for rule in rules:
        if rule.counts is None:
            continue
        table = lambda e: fs.sql_name(dialect, e)  # noqa: E731
        for sql in (rule.counts(table), rule.measure(table)):
            sqlglot.parse_one(sql, read=read)


def test_an_unknown_dialect_is_refused() -> None:
    with pytest.raises(ValueError):
        compile_rules(_billing(), dialect="oracle")


def test_a_summary_function_never_reaches_sql_as_written() -> None:
    contract = _billing()
    agg = contract["exposes"][0]["contract"]["invariants"][2]["aggregate"]
    agg["function"] = "max(0) AS x FROM t) AS s ON TRUE) AS q; DROP TABLE victim; --"
    errors = [f.display for f in lint_contract(contract) if f.severity == "error"]
    assert any("is not a summary function" in e for e in errors)
    rule = _by_id(contract)["billed"]
    assert rule.status == UNSUPPORTED and rule.counts is None
    assert "DROP" not in rule.reason.upper() or "validator refuses it" in rule.reason


def test_a_summary_function_is_refused_even_when_the_lint_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.data_rules import _compile

    monkeypatch.setattr(_compile, "analyze", lambda contract: ([], {}))
    contract = _billing()
    contract["exposes"][0]["contract"]["invariants"][2]["aggregate"]["function"] = "avg"
    rule = _by_id(contract)["billed"]
    assert rule.status == UNSUPPORTED and "not a summary function" in rule.reason


def test_a_non_finite_if_none_is_unsupported() -> None:
    contract = _billing()
    contract["exposes"][0]["contract"]["invariants"][2]["aggregate"]["ifNone"] = float("nan")
    rule = _by_id(contract)["billed"]
    assert rule.status == UNSUPPORTED and rule.counts is None


def test_a_refused_shared_attribute_is_reported_per_member() -> None:
    contract = _segments()
    contract["exposes"][1].pop("semantics")  # contracts no longer reach accounts
    rules = [r for r in compile_rules(contract) if r.kind == "sharedAttribute"]
    assert {r.id for r in rules} == {
        "customer_segment.accounts.segment",
        "customer_segment.invoices.customer_segment",
    }
    assert all(r.status == UNSUPPORTED and "no owner" in r.reason for r in rules)


def test_a_percent_measure_is_divided_in_the_engines_double() -> None:
    contract = _billing()
    rule = _by_id(contract)["due_after_issue"]
    assert 'CAST("violating" AS DOUBLE) * 100' in rule.measure(_table)
    athena = {r.id: r for r in compile_rules(contract, dialect="athena")}["due_after_issue"]
    assert "AS DOUBLE) * 100" in athena.measure(lambda e: fs.sql_name("athena", e))
    postgres = {r.id: r for r in compile_rules(contract, dialect="postgres")}["due_after_issue"]
    assert "AS NUMERIC) * 100" in postgres.measure(lambda e: fs.sql_name("postgres", e))


def test_a_later_derive_is_refused_when_an_earlier_when_is_unreadable() -> None:
    contract = _contract(
        _expose(
            "a",
            [
                {"name": "s", "type": "string"},
                {"name": "d", "type": "integer"},
                {"name": "base", "type": "integer"},
            ],
            contract={
                "invariants": [
                    {"id": "e1", "derive": "d", "expr": "base", "when": "s = = 'A'"},
                    {"id": "e2", "derive": "d", "expr": "base + 1", "when": "s = 'B'"},
                ]
            },
        )
    )
    rules = _by_id(contract)
    assert rules["e1"].status == UNSUPPORTED
    assert rules["e2"].status == UNSUPPORTED and "earlier derive" in rules["e2"].reason


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "d1", "derive": "gross"},
        {"id": "d2", "derive": "gross", "aggregate": {"function": "sum", "from": ["x"]}},
        {"id": "d3", "derive": "gross", "aggregate": "sum"},
        {"id": "d4", "check": 3},
        {"id": "d5", "dependency": "net -> gross"},
    ],
)
def test_a_malformed_declaration_is_unsupported_never_a_crash(entry: Dict[str, Any]) -> None:
    contract = _billing()
    contract["exposes"][0]["contract"]["invariants"].append(entry)
    rule = _by_id(contract)[entry["id"]]
    assert rule.status == UNSUPPORTED and rule.reason


def _keys_chain() -> Dict[str, Any]:
    return _contract(
        _expose(
            "c3",
            [
                {
                    "name": "code",
                    "type": "string",
                    "identifierFormat": [{"prefixes": ["P"], "mask": "99999"}],
                    "placeholders": {"values": ["TBD"]},
                }
            ],
        ),
        _expose(
            "b2",
            [{"name": "code", "type": "string"}],
            semantics={"relationships": [_rel("to_c3", "c3", ["code"], ["code"])]},
        ),
        _expose(
            "a1",
            [
                {
                    "name": "code",
                    "type": "string",
                    "identifierFormat": [{"prefixes": ["P"], "mask": "99999"}],
                }
            ],
            semantics={"relationships": [_rel("to_b2", "b2", ["code"], ["code"])]},
        ),
    )


def test_a_foreign_key_inherits_the_format_and_placeholders_down_its_chain() -> None:
    contract = _keys_chain()
    assert not [f for f in lint_contract(contract) if f.severity == "error"]
    formats = {r.expose: r for r in compile_rules(contract) if r.kind == "identifierFormat"}
    expected = "^((P[0-9]{5})|TBD)$"
    assert {e: r.pattern for e, r in formats.items()} == {
        "c3": expected,
        "b2": expected,
        "a1": expected,
    }
    assert "copies" in formats["b2"].description


def test_a_decimal_if_none_is_exact_on_bigquery() -> None:
    contract = _billing()
    contract["exposes"][0]["contract"]["invariants"][2]["aggregate"]["ifNone"] = 0.5
    rule = {r.id: r for r in compile_rules(contract, dialect="bigquery")}["billed"]
    assert "BIGNUMERIC '0.5'" in rule.counts(lambda e: fs.sql_name("bigquery", e))
