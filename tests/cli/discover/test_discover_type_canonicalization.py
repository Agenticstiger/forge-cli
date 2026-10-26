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
from typing import Any, Dict, List

import pytest

from fluid_build.cli.discover.emitter import canonical_column_type, emit_contract

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = REPO_ROOT / "fluid_build" / "schemas"


def _type_schema(version: str) -> Dict[str, Any]:
    doc = json.loads((SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    return doc["$defs"]["column"]["properties"]["type"]


def _bundled_versions() -> List[str]:
    return sorted(p.stem.replace("fluid-schema-", "") for p in SCHEMAS.glob("fluid-schema-*.json"))


def _emitted_version() -> str:
    """The schema version the emitter DECLARES, which is what gates the contract.

    Validating against the newest bundled schema would be the wrong gate:
    `fluid validate` reads `fluidVersion` out of the emitted contract, and the
    emitter writes an older one than the newest bundled.
    """
    contract = emit_contract(
        product_id="p",
        name="p",
        domain="d",
        owner_team="t",
        owner_email="e",
        engine="duckdb",
        source_kind="filesystem",
        connection={},
        streams=[],
    )
    return str(contract["fluidVersion"])


def _validator(version: str) -> Any:
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(_type_schema(version))


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
    validator = _validator(_emitted_version())
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
    validator = _validator(_emitted_version())
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
    validator = _validator(_emitted_version())
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


def test_every_bundled_schema_accepts_what_the_emitter_writes() -> None:
    """Stronger than the declared version alone: a bump must not break this.

    The emitter's `fluidVersion` has moved before and will again, so every
    bundled schema has to accept the canonical names rather than only the one
    currently declared.
    """
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
    broken = {}
    for version in _bundled_versions():
        validator = _validator(version)
        bad = [
            (n, canonical_column_type(n))
            for n in names
            if not validator.is_valid(canonical_column_type(n))
        ]
        if bad:
            broken[version] = bad
    assert broken == {}, f"schema versions rejecting an emitted type: {broken}"


def test_the_emitter_actually_applies_the_canonicalisation() -> None:
    """Pins the WIRING. The discover tests pass with the call site reverted."""
    from fluid_build.cli.discover.registry import DiscoveredColumn, DiscoveredStream

    stream = DiscoveredStream(
        name="t",
        columns=[
            DiscoveredColumn(name="big", type="HUGEINT"),
            DiscoveredColumn(name="note", type='"NULL"'),
            DiscoveredColumn(name="tags", type="INTEGER[]"),
        ],
    )
    contract = emit_contract(
        product_id="p",
        name="p",
        domain="d",
        owner_team="t",
        owner_email="e",
        engine="duckdb",
        source_kind="filesystem",
        connection={},
        streams=[stream],
    )
    emitted = {c["name"]: c["type"] for c in contract["exposes"][0]["contract"]["schema"]}
    assert emitted == {"big": "numeric", "note": "string", "tags": "array"}, emitted
    validator = _validator(str(contract["fluidVersion"]))
    for name, declared in emitted.items():
        assert validator.is_valid(declared), f"{name}: {declared!r} fails the declared schema"
