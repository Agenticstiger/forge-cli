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
    assert DOCUMENT["limits"]["maxCodePoints"] == fs.MAX_CODE_POINTS
    assert DOCUMENT["limits"]["maxDepth"] == fs.MAX_DEPTH


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


def test_no_json_number_in_rows_or_results() -> None:
    """Numbers are decimal strings, so no reader turns one into a binary double."""
    for vector in VECTORS:
        for case in vector["expect"].get("eval", []):
            values = list(case["row"].values()) + [case["result"]]
            for reached in (case.get("links") or {}).values():
                values.extend((reached or {}).values())
            for value in values:
                assert not isinstance(value, (int, float)) or isinstance(value, bool), (
                    vector["id"],
                    value,
                )


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
