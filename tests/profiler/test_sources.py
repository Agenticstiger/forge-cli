# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Reading one table's columns where they live, whole, without quoting a record."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fluid_build.profiler.sources import SourceError, describe_source, read_columns

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-value-9431"  # pragma: allowlist secret


def _ndjson(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Newline-delimited JSON, as flux-engine reads a committed slice
# ---------------------------------------------------------------------------


def test_a_slice_is_read_line_by_line_with_blank_lines_skipped(tmp_path):
    path = _ndjson(
        tmp_path / "s.ndjson",
        '{"a": "12.50", "b": 3, "c": null}',
        "",
        "   ",
        '{"a": "1", "b": -4, "c": "x", "extra": [1]}',
    )
    read = read_columns(str(path), ["a", "b", "c"])
    assert read.row_count == 2
    assert read.values == {"a": ["12.50", "1"], "b": [3, -4], "c": [None, "x"]}


def test_a_slice_key_is_matched_in_nfc(tmp_path):
    path = _ndjson(tmp_path / "s.jsonl", '{"café": "1"}')
    assert read_columns(str(path), ["café"]).values == {"café": ["1"]}


def test_a_line_ends_at_a_newline_only(tmp_path):
    # read_slice's str.splitlines also ends a line at U+2028 inside a string.
    path = _ndjson(tmp_path / "s.ndjson", '{"a": "x y"}')
    assert read_columns(str(path), ["a"]).values == {"a": ["x y"]}


@pytest.mark.parametrize("number", ["98.76", "4e7", "NaN", "-Infinity"])
def test_a_binary_float_in_a_slice_is_refused_without_quoting_it(tmp_path, number):
    path = _ndjson(tmp_path / "s.ndjson", '{"a": "1"}', f'{{"a": {number}}}')
    with pytest.raises(SourceError) as raised:
        read_columns(str(path), ["a"])
    message = str(raised.value)
    assert "line 2" in message and "binary floating-point" in message
    assert number not in message


@pytest.mark.parametrize(
    "line, words", [("[1, 2]", "a JSON list"), ('"x"', "a JSON str"), ("{oops", "not JSON")]
)
def test_a_line_that_is_not_an_object_is_refused(tmp_path, line, words):
    path = _ndjson(tmp_path / "s.ndjson", line)
    with pytest.raises(SourceError, match=words):
        read_columns(str(path), ["a"])


def test_a_column_absent_from_a_line_is_refused_not_read_as_null(tmp_path):
    path = _ndjson(tmp_path / "s.ndjson", '{"a": "1"}', '{"b": "2"}')
    with pytest.raises(SourceError, match=r"column 'a' is absent from .* line 2"):
        read_columns(str(path), ["a"])


def test_a_slice_that_is_not_utf8_is_refused(tmp_path):
    path = tmp_path / "s.ndjson"
    path.write_bytes(b'{"a": "\xff"}\n')
    with pytest.raises(SourceError, match="not UTF-8"):
        read_columns(str(path), ["a"])


def test_a_file_url_and_a_relative_path_are_read(tmp_path):
    path = _ndjson(tmp_path / "s.ndjson", '{"a": 1}')
    assert read_columns(path.as_uri(), ["a"]).row_count == 1
    assert read_columns("s.ndjson", ["a"], cwd=tmp_path).row_count == 1


# ---------------------------------------------------------------------------
# CSV and Parquet through DuckDB
# ---------------------------------------------------------------------------


def test_a_csv_is_read_as_the_text_it_holds(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text('a,b\n12.50,x\n0007,""\n,y\n', encoding="utf-8")
    read = read_columns(str(path), ["a", "b"])
    assert read.row_count == 3
    # No float: the digits are kept as written, for the registry to read exactly.
    assert read.values == {"a": ["12.50", "0007", None], "b": ["x", None, "y"]}


def test_a_column_the_file_lacks_is_named(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(SourceError, match="no column 'missing'"):
        read_columns(str(path), ["a", "missing"])


def test_a_parquet_file_keeps_its_types_and_timestamps_become_rfc_3339(tmp_path):
    path = tmp_path / "t.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 12.50::DECIMAL(10,2) AS d, 7::BIGINT AS i, 0.5::DOUBLE AS f, "
        "TIMESTAMP '2026-07-01 10:00:00.250' AS ts, DATE '2026-07-02' AS dt, "
        "true AS b) TO '" + str(path) + "' (FORMAT parquet)"
    )
    con.close()
    read = read_columns(str(path), ["d", "i", "f", "ts", "dt", "b"])
    assert read.values == {
        "d": [Decimal("12.50")],
        "i": [7],
        "f": [0.5],  # passed on as it came: the registry's reader refuses a binary float
        "ts": ["2026-07-01T10:00:00.250Z"],
        "dt": ["2026-07-02T00:00:00.000Z"],
        "b": [True],
    }


def test_a_duckdb_parse_error_is_reported_by_its_class_alone(tmp_path):
    path = tmp_path / "t.parquet"
    path.write_bytes(b"PAR1" + SECRET.encode() + b"not parquet")
    with pytest.raises(SourceError) as raised:
        read_columns(str(path), ["a"])
    assert SECRET not in str(raised.value)
    assert "could not be read as PARQUET" in str(raised.value)


def test_duckdb_reads_nothing_but_the_one_file(tmp_path):
    other = tmp_path / "other.csv"
    other.write_text(f"v\n{SECRET}\n", encoding="utf-8")
    path = tmp_path / "t.csv"
    path.write_text("a\n1\n", encoding="utf-8")
    from fluid_build.profiler import sources

    # A connection allowed one file cannot be pointed at another.
    with pytest.raises(SourceError, match="Permission"):
        sources._fetch(
            _sandboxed(path),
            "read_csv('" + str(other) + "', all_varchar = true)",
            ["v"],
            subject="other.csv",
        )


def _sandboxed(path: Path):
    from fluid_build.providers._duckdb_sandbox import DuckDBAllowlist, secure_duckdb_connect

    return secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_paths(str(path)))


# ---------------------------------------------------------------------------
# Databases, attached as fluid init --discover attaches them
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db(tmp_path):
    try:
        probe = duckdb.connect()
        probe.execute("INSTALL sqlite; LOAD sqlite")
        probe.close()
    except duckdb.Error:
        pytest.skip("the sqlite extension is not installable here (offline)")
    db = tmp_path / "shop.db"
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE "order lines" (qty INTEGER, sku TEXT)')
        con.executemany(
            'INSERT INTO "order lines" VALUES (?, ?)', [(n, f"sku{n % 3}") for n in range(25)]
        )
    return db


def test_a_sqlite_table_is_read_whole(sqlite_db):
    read = read_columns(f"sqlite:///{sqlite_db}", ["qty", "sku"], table="order lines")
    assert read.row_count == 25
    assert sorted(read.values["qty"]) == list(range(25))


def test_a_database_source_needs_a_table_and_a_file_takes_none(sqlite_db, tmp_path):
    with pytest.raises(SourceError, match="--table"):
        read_columns(f"sqlite:///{sqlite_db}", ["qty"])
    path = _ndjson(tmp_path / "s.ndjson", '{"a": 1}')
    with pytest.raises(SourceError, match="a file is one table"):
        read_columns(str(path), ["a"], table="t")


def test_a_missing_table_is_named(sqlite_db):
    with pytest.raises(SourceError, match="nope"):
        read_columns(f"sqlite:///{sqlite_db}", ["qty"], table="nope")


@pytest.mark.parametrize("table", ["a.b.c", "a..b", ".a"])
def test_a_table_is_a_name_or_schema_dot_name(sqlite_db, table):
    with pytest.raises(SourceError):
        read_columns(f"sqlite:///{sqlite_db}", ["qty"], table=table)


def test_a_database_uri_is_shown_with_its_password_masked():
    shown = describe_source(f"postgresql://reader:{SECRET}@db.internal:5432/sales")
    assert SECRET not in shown and "reader" in shown


@pytest.mark.parametrize("name", ["orders", "ordres"])  # an exposeId, or a mistyped one
def test_a_database_uri_after_a_name_is_masked_too(name):
    shown = describe_source(f"{name}=postgresql://reader:{SECRET}@db.internal/sales?ssl=on")
    assert SECRET not in shown and shown.startswith(f"{name}=postgresql://reader")


def test_a_path_holding_an_equals_sign_is_shown_as_given():
    assert describe_source("./orders=2024.csv") == "./orders=2024.csv"


def test_a_connect_failure_masks_the_password():
    with pytest.raises(SourceError) as raised:
        read_columns(
            f"postgresql://reader:{SECRET}@127.0.0.1:9/sales", ["a"], table="public.orders"
        )
    assert SECRET not in str(raised.value)


# ---------------------------------------------------------------------------
# What --source refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, words",
    [
        ("s3://bucket/orders.parquet", "does not read s3://"),
        ("https://example.com/orders.csv", "does not read https://"),
        ("file://elsewhere/orders.csv", "no host other than localhost"),
        ("orders.xlsx", "is not a file --source reads"),
        ("missing.csv", "is not a file"),
    ],
)
def test_what_source_does_not_read_is_refused(tmp_path, source, words):
    with pytest.raises(SourceError, match=words):
        read_columns(source, ["a"], cwd=tmp_path)


def test_timestamps_become_utc_rfc_3339_and_keep_their_precision():
    from fluid_build.profiler.sources import _wire

    zone = timezone(timedelta(hours=2))
    assert _wire(datetime(2026, 7, 1, 12, 0, tzinfo=zone)) == "2026-07-01T10:00:00.000Z"
    assert _wire(datetime(2026, 7, 1, 10, 0)) == "2026-07-01T10:00:00.000Z"
    assert _wire(datetime(2026, 7, 1, 10, 0, 0, 123456)) == "2026-07-01T10:00:00.123456Z"
    assert _wire(datetime(2026, 7, 1, 10, 0, 0, 123000)) == "2026-07-01T10:00:00.123Z"
    assert _wire(date(99, 1, 2)) == "0099-01-02T00:00:00.000Z"


class _Connection:
    """A DuckDB connection that fails where it is told to."""

    def __init__(self, describe=None, select=None):
        self.describe, self.select = describe, select

    def execute(self, sql, *args):
        error = self.describe if sql.startswith("DESCRIBE") else self.select
        if error is not None:
            raise error
        return self

    def fetchall(self):
        return [("a", "VARCHAR")]


def test_an_error_once_records_are_read_is_named_by_its_class_alone():
    from fluid_build.profiler.sources import _fetch

    con = _Connection(select=duckdb.IOException(f"server said: value {SECRET} is bad"))
    with pytest.raises(SourceError) as raised:
        _fetch(con, "t", ["a"], subject="table 't' could not be read", uri="sqlite:///x.db")
    assert SECRET not in str(raised.value) and "IOException" in str(raised.value)


def test_before_records_are_read_a_database_io_error_is_named_by_its_class():
    from fluid_build.profiler.sources import _fetch

    error = duckdb.IOException(f"server said: value {SECRET} is bad")
    with pytest.raises(SourceError) as database:
        _fetch(_Connection(describe=error), "t", ["a"], subject="db", uri="sqlite:///x.db")
    assert SECRET not in str(database.value)
    # A file's I/O error names the file, and its text is kept.
    with pytest.raises(SourceError) as file:
        _fetch(_Connection(describe=duckdb.IOException("No files found")), "t", ["a"], subject="f")
    assert "No files found" in str(file.value)


def test_a_name_with_a_control_character_is_refused():
    from fluid_build.profiler.sources import _quote_ident

    with pytest.raises(SourceError, match="control character"):
        _quote_ident("a\x1b[2Jb")
    assert _quote_ident('we"ird') == '"we""ird"'


def test_a_zoned_timestamp_is_read_as_the_utc_instant_without_pytz(tmp_path):
    path = tmp_path / "z.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT TIMESTAMPTZ '2026-07-01 12:00:00.250+02' AS ts) TO '"
        + str(path)
        + "' (FORMAT parquet)"
    )
    con.close()
    assert read_columns(str(path), ["ts"]).values == {"ts": ["2026-07-01T10:00:00.250Z"]}


def test_a_missing_sqlite_database_is_refused_and_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(SourceError, match="names no sqlite database file"):
        read_columns(f"sqlite:///{missing}", ["a"], table="t")
    assert not missing.exists()


@pytest.mark.parametrize(
    "line, words",
    [
        # Python 3.14 bounds this by the C stack, not the recursion limit, and
        # parses 100,000 levels; 1,000,000 is past it on every supported Python.
        ('{"a": ' + "[" * 1_000_000 + "]" * 1_000_000 + "}", "nests too deeply"),
        ('{"a": ' + "9" * 5000 + "}", "integer too long"),
    ],
)
def test_a_line_python_cannot_parse_is_a_named_refusal(tmp_path, line, words):
    path = _ndjson(tmp_path / "s.ndjson", line)
    with pytest.raises(SourceError, match=words):
        read_columns(str(path), ["a"])


def test_an_unknown_home_directory_is_a_named_refusal(tmp_path):
    with pytest.raises(SourceError, match="home directory"):
        read_columns("~no_such_user_9431/o.csv", ["a"], cwd=tmp_path)


@pytest.mark.parametrize(
    "literal",
    [
        "'infinity'::DATE",
        "'-infinity'::TIMESTAMP",
        "'infinity'::TIMESTAMPTZ",
        # TIMESTAMP_S is not listed: DuckDB's Parquet writer cannot store its infinity.
        "'-infinity'::TIMESTAMP_MS",
        "'infinity'::TIMESTAMP_NS",
    ],
)
def test_an_infinite_date_is_refused_not_read_as_year_9999(tmp_path, literal):
    path = tmp_path / "inf.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT {literal} AS d UNION ALL SELECT {literal.split('::')[0]}::"
        f"{literal.split('::')[1]} AS d) TO '{path}' (FORMAT parquet)"
    )
    con.close()
    with pytest.raises(SourceError, match="column 'd' holds an infinite date or timestamp"):
        read_columns(str(path), ["d"])


def test_a_database_is_attached_read_only(monkeypatch, sqlite_db):
    from fluid_build.cli.discover import _jdbc_introspect
    from fluid_build.profiler import sources

    seen = {}
    real = _jdbc_introspect.attach_jdbc_source

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_jdbc_introspect, "attach_jdbc_source", spy)
    read_columns(f"sqlite:///{sqlite_db}", ["qty"], table="order lines")
    assert seen.get("read_only") is True
    assert sources.local_file(f"sqlite:///{sqlite_db}") == sqlite_db
