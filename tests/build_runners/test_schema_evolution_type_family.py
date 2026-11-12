# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Upgrading DuckDB is not schema drift.

DuckDB 2.0 infers HUGEINT where 1.5 infers DOUBLE for the same integer literal,
and TIMESTAMP WITH TIME ZONE where 1.5 infers TIMESTAMP for the same JSON
timestamp. The drift gate compared type NAMES, so a DuckDB upgrade alone -- no
contract edit, no new data -- raised ``SchemaDriftError`` on a working pipeline.

:func:`test_the_family_check_does_not_swallow_the_ladder` pins the invariant
that no ordered ``_NUMERIC_ORDER`` pair is ever reported as a non-event. Worth
being precise about what that does and does not prove: with the narrow, explicit
family table, only ``double`` of those eight names belongs to a family at all,
so no pair is same-family and the check's POSITION after the ladder is defensive
rather than load-bearing -- moving it first changes nothing today, and this test
does not currently discriminate on ordering. It is here because the obvious
future refactor is to reuse the broad ``normalize_type`` helper, under which all
eight collapse into one numeric family; that change would make all 56 pairs
same-family, and then ordering decides whether ``type_widened`` and
``type_narrowed`` ever fire again. This test fails loudly at that point.
"""

from __future__ import annotations

import itertools

import pytest

from fluid_build.api.schema import SchemaColumn
from fluid_build.build_runners._schema_evolution import (
    _NUMERIC_ORDER,
    diff_columns,
    family_rename_note,
)


def _event(old: str, new: str) -> str:
    events = diff_columns(
        [SchemaColumn(name="c", type=old)],
        [SchemaColumn(name="c", type=new)],
    )
    return events[0][0] if events else "none"


def test_the_duckdb_version_renames_are_not_drift() -> None:
    for old, new in (
        ("double", "hugeint"),
        ("hugeint", "double"),
        ("double", "bignum"),
        ("timestamp", "timestamptz"),
        ("timestamptz", "timestamp"),
        ("timestamp", "timestamp with time zone"),
    ):
        assert _event(old, new) == "none", f"{old} -> {new} should not be drift"


def test_the_family_check_does_not_swallow_the_ladder() -> None:
    """Every ordered numeric pair must still be classified, not silently dropped.

    All 8 names in ``_NUMERIC_ORDER`` normalise into one numeric family, so a
    family check placed before the ladder would report ``none`` for all 56
    ordered pairs. If this fails, the family comparison has been moved ahead of
    ``_is_widening`` / ``_is_narrowing``.
    """
    pairs = list(itertools.permutations(_NUMERIC_ORDER, 2))
    assert len(pairs) == 56
    swallowed = [(a, b) for a, b in pairs if _event(a, b) == "none"]
    assert swallowed == [], f"the widen/narrow ladder was bypassed for {swallowed}"


def test_widening_and_narrowing_still_report_themselves() -> None:
    assert _event("int", "bigint") == "type_widened"
    assert _event("tinyint", "double") == "type_widened"
    assert _event("bigint", "int") == "type_narrowed"
    assert _event("decimal", "smallint") == "type_narrowed"


def test_a_real_type_change_still_fails() -> None:
    for old, new in (
        ("varchar", "integer"),
        ("integer", "varchar"),
        ("timestamp", "date"),
        ("double", "boolean"),
    ):
        assert _event(old, new) == "type_changed", f"{old} -> {new} is real drift"


def test_an_identical_type_is_still_no_event() -> None:
    assert _event("double", "double") == "none"
    assert _event("DOUBLE", "double") == "none"


def test_the_rename_is_reported_rather_than_hidden() -> None:
    """DOUBLE and HUGEINT are not value-identical, so the note says so.

    For the same CSV literal 1.5 landed a lossy 17-significant-digit float where
    2.0 lands the exact 128-bit integer. Collapsing the pair keeps the pipeline
    running; the note is what keeps that honest.
    """
    note = family_rename_note("double", "hugeint")
    assert note is not None
    assert "not schema drift" in note
    assert "precision" in note
    assert family_rename_note("varchar", "integer") is None


def test_the_family_check_is_case_and_space_insensitive() -> None:
    assert _event("DOUBLE", "HugeInt") == "none"
    assert _event("timestamp", "  TIMESTAMPTZ  ") == "none"


# ── the collapse is allowed, but it is not silent ────────────────────────


def test_the_acquisition_gate_logs_a_family_rename(caplog: pytest.LogCaptureFixture) -> None:
    """Suppressing the FAILURE is intended; suppressing the NOTICE is not.

    ``diff_columns`` emits no event for a family rename, so the drift gate
    returns early and a contract that declared ``schemaPolicy: strict`` would
    otherwise hear nothing at all — even though DOUBLE and HUGEINT are not
    value-identical. Without this wiring ``family_rename_note`` had no
    production caller, which made the module comment's claim that the change is
    "reported rather than hidden" untrue.
    """
    import logging

    from fluid_build.build_runners._acquisition_common import _log_family_renames

    with caplog.at_level(logging.WARNING):
        _log_family_renames(
            [SchemaColumn(name="big", type="double"), SchemaColumn(name="other", type="varchar")],
            [SchemaColumn(name="big", type="hugeint"), SchemaColumn(name="other", type="varchar")],
        )
    messages = [r.message for r in caplog.records]
    assert "acquisition_schema_family_rename" in messages, messages
    assert (
        len([m for m in messages if m == "acquisition_schema_family_rename"]) == 1
    ), "only the renamed column should be reported, not the unchanged one"


def test_the_gate_logs_nothing_when_no_type_changed(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from fluid_build.build_runners._acquisition_common import _log_family_renames

    with caplog.at_level(logging.WARNING):
        _log_family_renames(
            [SchemaColumn(name="a", type="double")],
            [SchemaColumn(name="a", type="double")],
        )
    assert [r.message for r in caplog.records] == []


def _through_the_strict_gate(declared: str, observed: str) -> None:
    """One column through ``enforce_schema_policy_or_raise`` under ``schemaPolicy: strict``.

    ``declared`` is the contract's type for it, ``observed`` the type the
    runner's fingerprint reports for the source.
    """
    from fluid_build.api.schema import SchemaFingerprint
    from fluid_build.build_runners._acquisition_common import (
        enforce_schema_policy_or_raise,
    )

    class _Ctx:
        contract = {
            "exposes": [
                {
                    "contract": {
                        "schema": [{"name": "c", "type": declared}],
                        "schemaPolicy": "strict",
                    }
                }
            ]
        }

    class _Runner:
        def fingerprint(self, _ctx: object) -> SchemaFingerprint:
            return SchemaFingerprint.of([SchemaColumn(name="c", type=observed)])

    enforce_schema_policy_or_raise(_Ctx(), _Runner())


def test_the_rename_reaches_the_operator_through_the_real_gate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through ``enforce_schema_policy_or_raise``, not the helper directly.

    This is the test that fails if the call is ever removed from the gate: a
    direct call to ``_log_family_renames`` passes either way. ``schemaPolicy:
    strict`` is used deliberately -- that is the contract that would otherwise
    hear nothing, since the family collapse means the gate returns before it
    raises.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        # What a DuckDB 2.0 install now infers for the same source bytes.
        _through_the_strict_gate(declared="double", observed="hugeint")

    assert "acquisition_schema_family_rename" in [
        r.message for r in caplog.records
    ], "the gate allowed the family rename without telling anyone"


# ── the family table and the discover emitter must not drift apart ───────


def test_every_discover_alias_is_same_family_as_its_target() -> None:
    """Otherwise fixing one bug causes another, which is what happened.

    `fluid init --discover` writes the CANONICAL name into the contract while
    DuckDB keeps inferring the SOURCE name, so unless the two are one family the
    drift gate fails at apply exactly what the emitter stopped failing at
    validate -- the failure moves rather than going away.
    """
    from fluid_build.build_runners._schema_evolution import _same_type_family
    from fluid_build.cli.discover.emitter import _SCHEMA_TYPE_ALIASES

    missing = [
        (source, target)
        for source, target in _SCHEMA_TYPE_ALIASES.items()
        if not _same_type_family(source, target)
    ]
    assert missing == [], f"add these to _TYPE_FAMILIES: {missing}"


def test_no_duckdb_type_name_trips_the_gate_after_discover() -> None:
    """The round trip over the INSTALLED DuckDB's whole type list.

    Catches a future DuckDB type name here rather than in a user's failed apply.
    """
    duckdb = pytest.importorskip("duckdb")
    from fluid_build.cli.discover.emitter import canonical_column_type

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
    tripped = [
        (name, canonical_column_type(name))
        for name in names
        if _event(canonical_column_type(name), name) != "none"
    ]
    assert tripped == [], (
        f"duckdb {duckdb.__version__}: discover would write these and the drift "
        f"gate would then fail them at apply: {tripped}"
    )


def test_an_all_null_column_round_trips() -> None:
    """``typeof(NULL)`` is the quoted string ``"NULL"``, which must still match."""
    from fluid_build.cli.discover.emitter import canonical_column_type

    assert canonical_column_type('"NULL"') == "string"
    assert _event("string", '"NULL"') == "none"
    # ... but a column that gains a real type is still drift.
    assert _event("string", "integer") == "type_changed"


# ── DuckDB's names for VARCHAR are one type, not a family rename ─────────

#: What ``duckdb_types()`` gives the logical type VARCHAR on duckdb 1.5.1 and
#: 2.0.0, less ``json`` (see the installed-DuckDB test below). A column
#: declared with any of them DESCRIBEs as VARCHAR.
_DUCKDB_VARCHAR_NAMES = ("varchar", "string", "text", "char", "bpchar", "nvarchar")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        *itertools.permutations(_DUCKDB_VARCHAR_NAMES, 2),
        ("STRING", "VARCHAR"),
        # DuckDB takes a length on each of them but neither enforces nor
        # reports it: the column DESCRIBEs as plain VARCHAR.
        *((f"{name}(255)", "VARCHAR") for name in _DUCKDB_VARCHAR_NAMES),
        ("CHAR (3)", "VARCHAR"),
    ],
)
def test_duckdbs_names_for_varchar_are_one_type(old: str, new: str) -> None:
    """A contract that types text STRING (Spark, Databricks) against DuckDB.

    DuckDB DESCRIBEs every text column as VARCHAR whatever it was declared as,
    so without this each STRING column failed the gate as ``type_changed`` on
    every ingest, under every policy but ``evolve_all``. Nothing moved, so the
    rename note stays silent too: it is for a physical type that did move
    (DOUBLE -> HUGEINT), and here it would claim a width change that does not
    happen, once per text column per run.
    """
    assert _event(old, new) == "none"
    assert family_rename_note(old, new) is None


def test_only_a_length_on_a_varchar_name_is_dropped() -> None:
    assert _event("varchar(255)", "integer") == "type_changed"
    assert _event("VARCHAR(255)[]", "VARCHAR") == "type_changed"  # a list is not text
    assert _event("decimal(12,2)", "VARCHAR") == "type_changed"


def test_an_all_null_column_that_fills_with_text_is_not_drift() -> None:
    """Discover writes ``string`` for an all-null column; text then DESCRIBEs as VARCHAR.

    While STRING and VARCHAR were two families this was ``type_changed``: the
    all-null case passed only for as long as the column stayed empty.
    """
    assert _event("string", '"NULL"') == "none"
    assert _event("string", "varchar") == "none"
    assert _event("varchar", '"NULL"') == "none"
    # A real type is still drift, whichever spelling the text side uses.
    for name in _DUCKDB_VARCHAR_NAMES:
        assert _event(name, "integer") == "type_changed", name
        assert _event("integer", name) == "type_changed", name


def test_every_installed_duckdb_name_for_varchar_is_one_type() -> None:
    """Over the INSTALLED DuckDB's own list, so a new alias fails here first."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        names = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT lower(type_name) FROM duckdb_types() "
                "WHERE logical_type = 'VARCHAR'"
            ).fetchall()
        ]
    finally:
        con.close()
    assert "string" in names, f"the query read no VARCHAR names: {names}"
    # JSON is the json extension's own type: DESCRIBE reports it as JSON and a
    # cast into it validates the text, so VARCHAR -> JSON is a real change.
    wrong = [
        name
        for name in names
        if name != "json"
        and (_event("varchar", name) != "none" or family_rename_note("varchar", name))
    ]
    assert wrong == [], f"duckdb {duckdb.__version__}: not read as VARCHAR: {wrong}"


def test_a_string_contract_passes_the_strict_gate_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reported case: VARCHAR is what DuckDB DESCRIBEs for a Postgres text column."""
    import logging

    with caplog.at_level(logging.WARNING):
        _through_the_strict_gate(declared="STRING", observed="VARCHAR")
    assert "acquisition_schema_family_rename" not in [r.message for r in caplog.records]


def test_a_string_column_that_turns_numeric_still_fails_the_strict_gate() -> None:
    from fluid_build._errors import SchemaDriftError

    with pytest.raises(SchemaDriftError) as raised:
        _through_the_strict_gate(declared="STRING", observed="INTEGER")
    assert "c/type_changed" in raised.value.why
