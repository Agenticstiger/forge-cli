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

"""``fluid init --discover`` must not emit a contract its own validator rejects.

The emitter wrote the source's type name straight through, so a discover on
DuckDB 2.0 produced ``type: hugeint`` and ``type: '"null"'`` and ``fluid
validate`` then rejected the file the tool had just written. Two of the rejected
names (``uinteger``, ``integer[]``) were rejected on DuckDB 1.5 too, so this was
never only a 2.0 problem.

The sharp test here is :func:`test_no_duckdb_type_name_is_emitted_invalid`: it
validates the mapping of EVERY name in the installed DuckDB's own
``duckdb_types()`` against the bundled schema, so a future DuckDB that adds a
type name is caught by this file rather than by a user's broken contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli.discover.emitter import canonical_column_type

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = REPO_ROOT / "fluid_build" / "schemas"


def _type_schema() -> Dict[str, Any]:
    newest = sorted(SCHEMAS.glob("fluid-schema-*.json"))[-1]
    doc = json.loads(newest.read_text(encoding="utf-8"))
    return doc["$defs"]["column"]["properties"]["type"]


def _validator() -> Any:
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(_type_schema())


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The two the bug report named.
        ("HUGEINT", "numeric"),
        ('"NULL"', "string"),
        # Rejected on 1.5 as well, so not a 2.0-only problem.
        ("UINTEGER", "bigint"),
        ("INTEGER[]", "array"),
        # A width that holds the range, not the similar-sounding name.
        ("UBIGINT", "numeric"),
        ("UHUGEINT", "numeric"),
        ("USMALLINT", "integer"),
        ("UTINYINT", "smallint"),
        # Precision suffixes are not different types.
        ("TIMESTAMP_MS", "timestamp"),
        ("TIMESTAMP_NS", "timestamp"),
        ("TIMESTAMPTZ_NS", "timestamptz"),
        # Composites and odds.
        ("LIST", "array"),
        ("UNION", "variant"),
        ("LOGICAL", "boolean"),
        ("BPCHAR", "varchar"),
        ("VARCHAR[][]", "array"),
        # Already valid: untouched apart from case.
        ("VARCHAR", "varchar"),
        ("BIGINT", "bigint"),
        ("DECIMAL(18,2)", "decimal(18,2)"),
        ("STRUCT(a INTEGER)", "struct(a integer)"),
        ("timestamp with time zone", "timestamp with time zone"),
        # No type at all: the emitter's own pre-existing fallback.
        (None, "string"),
        ("", "string"),
        ("   ", "string"),
    ],
)
def test_named_types_map_as_documented(raw: str, expected: str) -> None:
    assert canonical_column_type(raw) == expected


def test_every_mapped_type_satisfies_the_bundled_schema() -> None:
    """The documented aliases must each land on a value the schema accepts."""
    validator = _validator()
    bad = []
    for raw in (
        "HUGEINT",
        '"NULL"',
        "UINTEGER",
        "INTEGER[]",
        "UBIGINT",
        "UHUGEINT",
        "USMALLINT",
        "UTINYINT",
        "TIMESTAMP_MS",
        "TIMESTAMPTZ_NS",
        "TIME_NS",
        "TIMETZ",
        "LIST",
        "UNION",
        "TUPLE",
        "LOGICAL",
        "BPCHAR",
        "BITSTRING",
        "VARINT",
        "BIGNUM",
        "INT1",
        "INT128",
        "UINT128",
        "OID",
        "SHORT",
        "SIGNED",
        "INTEGRAL",
        "NULL",
        "TYPE",
    ):
        mapped = canonical_column_type(raw)
        if not validator.is_valid(mapped):
            bad.append((raw, mapped))
    assert bad == [], f"these map to values the contract schema rejects: {bad}"


def test_no_duckdb_type_name_is_emitted_invalid() -> None:
    """Sweep the INSTALLED DuckDB's own type list, so a new type name is caught here.

    Run against whichever DuckDB is installed, which is the point: a 1.5 install
    and a 2.0 install enumerate different sets, and both must map cleanly.
    """
    duckdb = pytest.importorskip("duckdb")
    validator = _validator()
    con = duckdb.connect(":memory:")
    try:
        names = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT lower(type_name) FROM duckdb_types()"
            ).fetchall()
        ]
    finally:
        con.close()
    assert names, "duckdb_types() returned nothing"
    bad = [
        (n, canonical_column_type(n))
        for n in names
        if not validator.is_valid(canonical_column_type(n))
    ]
    assert bad == [], (
        f"duckdb {duckdb.__version__} has type names this emitter would write "
        f"into a contract that then fails validation: {bad}"
    )


def test_an_already_valid_name_is_never_remapped() -> None:
    """A contract emitted before this change must keep the same types."""
    validator = _validator()
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        names = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT lower(type_name) FROM duckdb_types()"
            ).fetchall()
        ]
    finally:
        con.close()
    changed = [
        (n, canonical_column_type(n))
        for n in names
        if validator.is_valid(n) and canonical_column_type(n) != n
    ]
    assert changed == [], f"already-valid type names were remapped: {changed}"
