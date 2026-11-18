# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""DuckDB is the oracle for the SQL renderings (RFC-data-rules.md §2.9).

Every accepted vector with eval cases is rendered to DuckDB SQL (§2.8) and run on
a one-row table per case, its links left-joined from a table of zero or one row.
DuckDB must compute the vector's value, except where §2.8 says engines are
approximate: a ``/`` (DuckDB 1.5 divides DECIMAL in DOUBLE) and arithmetic on a
float column. Those are still run, so their SQL is known to execute, and are
checked against the exact value only. DuckDB is a test dependency here; nothing
under ``fluid_build`` imports it for FLUID SQL/1.

The hostile vectors (quotes, backslashes, ``;``, ``--``, ``/*`` and Jinja
delimiters in names and literals) pass through here too: a rendering that let one
break out of its quotes would fail to run, or compute the wrong value.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql._render import duckdb_column
from fluid_build.fluid_sql._types import Model
from fluid_build.fluid_sql._vectors import build_models, decode, load_vectors
from fluid_build.providers._sql_safety import quote_identifier_for

duckdb = pytest.importorskip("duckdb")

DOCUMENT = load_vectors()
MODELS = build_models(DOCUMENT)
CASES = [
    v for v in DOCUMENT["vectors"] if v["expect"]["verdict"] == "accepted" and v["expect"]["eval"]
]


def _sql_type(model: Model, name: str) -> str:
    column = model.columns[name]
    if column.type is None:
        return "VARCHAR"
    kind = column.type.type.kind
    if kind == "number":
        if column.type.is_float:
            return "DOUBLE"
        if column.type.precision is not None:
            # DuckDB's DECIMAL is at most 38 digits wide, its scale at most its width, so a
            # wider column (decimal(1000, 1000)) is clamped; the vectors' values fit the clamp.
            width = min(column.type.precision, 38)
            return f"DECIMAL({width}, {min(column.type.type.scale, width)})"
        return "BIGINT" if column.type.type.scale == 0 else "DECIMAL(38, 10)"
    return {
        "bool": "BOOLEAN",
        "string": "VARCHAR",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "timestamptz": "TIMESTAMPTZ",
    }[kind]


def _table(model: Model, row: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    """A subquery of one row (``row``), or of none (``row`` is None), and its parameters."""
    names = sorted(model.columns)
    selects, params = [], []
    for name in names:
        value = None if row is None else row.get(name)
        selects.append(
            f"CAST(? AS {_sql_type(model, name)}) AS {quote_identifier_for('duckdb', name)}"
        )
        params.append(value)
    where = " WHERE FALSE" if row is None else ""
    return f"(SELECT {', '.join(selects)}{where})", params


def _query(vector: Dict[str, Any], case: Dict[str, Any], rendered: str) -> Tuple[str, List[Any]]:
    model = MODELS[vector["model"]]
    base, params = _table(model, case.get("row") or {})
    sql = f"SELECT {rendered} AS result FROM {base} AS {quote_identifier_for('duckdb', 't')}"
    links = case.get("links") or {}
    for link_name, link in sorted(model.links.items()):
        if link.target is None:
            continue
        joined, more = _table(link.target, links.get(link_name))
        sql += f" LEFT JOIN {joined} AS {quote_identifier_for('duckdb', link_name)} ON TRUE"
        params += more
    return sql, params


def _from_duckdb(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, Decimal)):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(Decimal(repr(value)))
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value
    raise TypeError(f"unexpected DuckDB value {value!r}")


@pytest.fixture(scope="module")
def connection() -> Any:
    con = duckdb.connect()
    yield con
    con.close()


@pytest.mark.parametrize("vector", CASES, ids=[v["id"] for v in CASES])
def test_duckdb_computes_the_vector(vector: Dict[str, Any], connection: Any) -> None:
    model = MODELS[vector["model"]]
    site = vector["site"]
    derive = model.columns[vector["derive"]] if vector.get("derive") else None
    tree = fs.parse(vector["text"])
    checked = fs.check(tree, model, site, derive)
    rendered = fs.render_duckdb(tree, checked, duckdb_column("t"), predicate=site == fs.PREDICATE)
    for case in vector["expect"]["eval"]:
        sql, params = _query(vector, case, rendered)
        got = _from_duckdb(connection.execute(sql, params).fetchone()[0])
        if checked.approximate:
            continue  # exact in FLUID SQL/1, approximate in DuckDB (RFC 2.8)
        expected = case["result"]
        want = expected if site == fs.PREDICATE else decode(expected, checked.type.kind)
        if isinstance(want, bool) or isinstance(got, bool):
            assert got is want, (vector["id"], case, rendered)
        else:
            assert got == want, (vector["id"], case, rendered)


def test_the_oracle_ran_most_vectors() -> None:
    """A guard against an oracle that silently skips: few vectors are approximate."""
    approximate = 0
    for vector in CASES:
        model = MODELS[vector["model"]]
        derive = model.columns[vector["derive"]] if vector.get("derive") else None
        if fs.check(fs.parse(vector["text"]), model, vector["site"], derive).approximate:
            approximate += 1
    assert len(CASES) >= 100
    assert approximate <= 12


# Wide decimals: DuckDB unifies DECIMAL(38,2) and DECIMAL(38,4) as DECIMAL(38,2) and drops
# scale silently, so the renderer casts numbers to one exact type (defect scan, 2026-11-16).
_WIDE = {
    "a": "decimal(38,2)",
    "b": "decimal(38,4)",
    "n": "decimal(18,2)",
    "m": "decimal(10,2)",
    "q": "bigint",
}


@pytest.mark.parametrize(
    "text, site, row, expected",
    [
        ("COALESCE(a, b)", fs.VALUE, {"a": None, "b": "1.2345"}, Fraction("1.2345")),
        ("COALESCE(a, b) = 1.2345", fs.PREDICATE, {"a": None, "b": "1.2345"}, True),
        ("a = b", fs.PREDICATE, {"a": "1.23", "b": "1.2345"}, False),
        ("a IN (1.2345)", fs.PREDICATE, {"a": "1.23"}, False),
        ("b BETWEEN a AND 1.23", fs.PREDICATE, {"a": "1.23", "b": "1.2345"}, False),
        (
            "COALESCE(m, ROUND(n * q, 0, 'HALF_TO_EVEN'))",
            fs.VALUE,
            {"n": "12.35", "m": "7.25", "q": "3"},
            Fraction("7.25"),
        ),
        (
            "CASE WHEN q > 1 THEN m ELSE ROUND(n * q, 0) END",
            fs.VALUE,
            {"n": "12.35", "m": "7.25", "q": "3"},
            Fraction("7.25"),
        ),
        ("ROUND(n, 38, 'HALF_TO_EVEN')", fs.VALUE, {"n": "12.35"}, Fraction("12.35")),
    ],
)
def test_wide_decimals_keep_their_scale(text, site, row, expected, connection) -> None:
    from fluid_build.fluid_sql._types import Column, column_type

    model = Model("wide", {name: Column(name, t, column_type(t)) for name, t in _WIDE.items()})
    tree = fs.parse(text)
    checked = fs.check(tree, model, site)
    rendered = fs.render_duckdb(tree, checked, duckdb_column("t"), predicate=site == fs.PREDICATE)
    selects = ", ".join(
        f"CAST(? AS {_sql_type(model, name)}) AS {quote_identifier_for('duckdb', name)}"
        for name in sorted(model.columns)
    )
    params = [row.get(name) for name in sorted(model.columns)]
    sql = f'SELECT {rendered} FROM (SELECT {selects}) AS "t"'
    got = _from_duckdb(connection.execute(sql, params).fetchone()[0])
    assert got == expected, rendered
    decoded = {k: (None if v is None else Fraction(v)) for k, v in row.items()}
    assert fs.evaluate(tree, decoded, predicate=site == fs.PREDICATE) == expected
