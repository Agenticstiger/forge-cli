# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Run ``fluid_build/fluid_sql/fluid-sql-vectors.json`` through this implementation.

The vectors are data, not Python: flux-expr/2 runs the same file through its own
parser and evaluator (RFC-data-rules.md §2.9), so the file defines FLUID SQL/1 and
neither implementation does. This module is the first implementation checking
itself, and it also holds the file to its own format: the reason codes, the
keywords, the limits and the node kinds it lists are the ones the code uses, and
every one of them is exercised by at least one vector.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Set

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql import _nodes
from fluid_build.fluid_sql._vectors import build_models, load_vectors, run_vector

DOCUMENT = load_vectors()
MODELS = build_models(DOCUMENT)
VECTORS = DOCUMENT["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_vector(vector: Dict[str, Any]) -> None:
    assert run_vector(vector, MODELS) == []


def test_ids_are_unique_and_kebab_case() -> None:
    ids = [v["id"] for v in VECTORS]
    assert len(ids) == len(set(ids))
    for vector_id in ids:
        assert vector_id == vector_id.lower() and " " not in vector_id, vector_id


def test_file_lists_what_the_implementation_uses() -> None:
    assert DOCUMENT["language"] == "FLUID SQL/1"
    assert DOCUMENT["reasonCodes"] == fs.REASONS
    assert sorted(DOCUMENT["keywords"]) == sorted(fs.KEYWORDS)
    assert DOCUMENT["functions"] == list(fs.FUNCTIONS)
    assert DOCUMENT["limits"]["maxCodePoints"] == str(fs.MAX_CODE_POINTS)
    assert DOCUMENT["limits"]["maxDepth"] == str(fs.MAX_DEPTH)
    assert DOCUMENT["limits"]["maxRoundPlaces"] == str(fs.MAX_ROUND_PLACES)


def test_every_reason_code_is_exercised() -> None:
    used = {v["expect"]["reason"] for v in VECTORS if v["expect"]["verdict"] == "refused"}
    assert used == set(fs.REASONS)


def test_every_node_kind_is_exercised() -> None:
    seen: Set[str] = set()

    def visit(tree: Any) -> None:
        if isinstance(tree, dict):
            if "node" in tree:
                seen.add(tree["node"])
            for value in tree.values():
                visit(value)
        elif isinstance(tree, list):
            for value in tree:
                visit(value)

    for vector in VECTORS:
        visit(vector["expect"].get("tree"))
    assert seen == set(DOCUMENT["nodes"])


def test_vector_shape() -> None:
    for vector in VECTORS:
        assert set(vector) <= {"id", "text", "site", "model", "derive", "note", "expect"}, vector
        assert vector["site"] in (fs.PREDICATE, fs.VALUE), vector["id"]
        assert vector["model"] in MODELS, vector["id"]
        expect = vector["expect"]
        if expect["verdict"] == "refused":
            assert set(expect) == {"verdict", "reason"}, vector["id"]
        else:
            assert expect["verdict"] == "accepted", vector["id"]
            assert {"tree", "canonical", "type", "eval"} <= set(expect), vector["id"]
            assert ("scale" in expect) == (expect["type"] == "number"), vector["id"]
            assert expect["eval"], f"{vector['id']}: an accepted vector carries eval cases"


def test_no_json_number_anywhere() -> None:
    """Numbers, scales and limits are strings, as in flux-expr/1's vectors, so no reader
    turns one into a binary double."""

    def visit(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                visit(inner, f"{where}.{key}")
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                visit(inner, f"{where}[{index}]")
        else:
            assert not isinstance(value, (int, float)) or isinstance(value, bool), where

    visit(DOCUMENT, "$")


def test_file_is_ascii() -> None:
    """Non-ASCII text is written as \\u escapes, so no editor can renormalize a vector."""
    from importlib import resources

    raw = resources.files("fluid_build.fluid_sql").joinpath("fluid-sql-vectors.json").read_bytes()
    assert raw.isascii()


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS if v["expect"]["verdict"] == "accepted"],
    ids=lambda v: v["id"],
)
def test_canonical_text_is_a_fixed_point(vector: Dict[str, Any]) -> None:
    tree = fs.parse(vector["text"])
    again = fs.parse(fs.canonical(tree))
    assert again == tree
    assert fs.canonical(again) == fs.canonical(tree)
    assert _nodes.from_json(vector["expect"]["tree"]) == tree


def test_the_fill_script_has_nothing_to_fill(capsys: pytest.CaptureFixture[str]) -> None:
    """``scripts/fill_fluid_sql_vectors.py --check`` agrees with the committed file."""
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "fill_fluid_sql_vectors.py"
    spec = importlib.util.spec_from_file_location("fill_fluid_sql_vectors", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = sys.argv
    sys.argv = [str(script), "--check"]
    try:
        assert module.main() == 0
    finally:
        sys.argv = argv
    assert "0 fields filled, 0 disagreements" in capsys.readouterr().out


def test_vectors_round_trip_through_json() -> None:
    assert json.loads(json.dumps(DOCUMENT)) == DOCUMENT


# -- ADD_DAYS, ADD_MONTHS, ADD_YEARS (EXPR-SPEC-3) ----------------------------------------

_BIG = 10**30


@pytest.mark.parametrize("unit", ["day", "month", "year"])
@pytest.mark.parametrize("count", [_BIG, -_BIG, 2**63, -(2**63) - 1, 10**9, -(10**9)])
def test_a_huge_count_gives_null_never_an_error(unit: str, count: int) -> None:
    import datetime
    from fractions import Fraction

    for day in (datetime.date(2000, 2, 29), datetime.date.min, datetime.date.max):
        tree = _nodes.DateAdd(unit, _nodes.Column("d"), _nodes.Column("k"))
        assert fs.evaluate(tree, {"d": day, "k": Fraction(count)}) is None


@pytest.mark.parametrize(
    "unit, day, count, want",
    [
        ("day", "2000-01-01", 2921939, "9999-12-31"),
        ("day", "2000-01-01", 2921940, None),
        ("day", "2000-01-01", -730119, "0001-01-01"),
        ("day", "2000-01-01", -730120, None),
        ("month", "9999-12-31", 0, "9999-12-31"),
        ("month", "9999-12-31", 1, None),
        ("month", "0001-01-31", -1, None),
        ("month", "0001-01-31", 1, "0001-02-28"),
        ("month", "2001-02-28", 1, "2001-03-28"),
        ("year", "2000-02-29", 100, "2100-02-28"),
        ("year", "2000-02-29", 400, "2400-02-29"),
        ("year", "0001-02-28", 9998, "9999-02-28"),
        ("year", "0001-02-28", 9999, None),
    ],
)
def test_date_addition_edges(unit: str, day: str, count: int, want: Any) -> None:
    import datetime
    from fractions import Fraction

    tree = _nodes.DateAdd(unit, _nodes.Column("d"), _nodes.Column("k"))
    got = fs.evaluate(tree, {"d": datetime.date.fromisoformat(day), "k": Fraction(count)})
    assert got == (None if want is None else datetime.date.fromisoformat(want))


def test_the_date_addition_names_and_hints() -> None:
    assert _nodes.DATE_ADD_FUNCTIONS == {
        "day": "ADD_DAYS",
        "month": "ADD_MONTHS",
        "year": "ADD_YEARS",
    }
    assert fs.FUNCTIONS[-3:] == ("ADD_DAYS", "ADD_MONTHS", "ADD_YEARS")
    for text in ("INTERVAL(1)", "DATEADD(day, 1, d)", "DATE_ADD(d, 1)", "TIMESTAMPADD(d, 1)"):
        with pytest.raises(fs.FluidSqlError) as caught:
            fs.parse(text)
        assert "ADD_DAYS(d, n), ADD_MONTHS(d, n) or ADD_YEARS(d, n)" in caught.value.detail
    with pytest.raises(fs.FluidSqlError) as caught:
        fs.parse("DATE_SUB(a, b)")
    assert "DAYS_BETWEEN" in caught.value.detail


def test_a_date_addition_prints_its_canonical_text() -> None:
    tree = fs.parse("add_months( issue_date ,1+1 )")
    assert fs.canonical(tree) == "ADD_MONTHS(issue_date, 1 + 1)"
    assert [type(c).__name__ for c in tree.children()] == ["Column", "Arith"]
