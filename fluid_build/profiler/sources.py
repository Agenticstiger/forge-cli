# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Read the columns of one table where it lives, once, whole.

``--source`` names the table's records in one of three ways:

* a newline-delimited JSON file (``.ndjson``, ``.jsonl``): the committed-slice
  format of flux-engine's fidelity check (SEAM-FIDELITY-SPEC §4), read as its
  ``read_slice`` reads one, with the standard library: one JSON object per line,
  blank lines skipped, keys in NFC, and a binary floating-point number refused
  (KERNEL-SPEC §2.1) so that ``"12.50"`` is read exactly and ``12.5`` is not read
  at all. A line ends at ``\\n``, as the NDJSON format says; ``read_slice`` uses
  ``str.splitlines``, which also ends one at U+2028 inside a string, a line this
  reader keeps whole.
* a CSV or Parquet file, through DuckDB, inside the sandbox ``fluid init
  --discover`` reads with (:mod:`fluid_build.providers._duckdb_sandbox`), allowed
  that one file and nothing else. A CSV is read with every column as text
  (``all_varchar``), so a decimal arrives as the digits the file holds.
* a ``postgres://``, ``mysql://`` or ``sqlite:///`` database, attached by
  :func:`fluid_build.cli.discover._jdbc_introspect.attach_jdbc_source`, the one
  place ``fluid init --discover`` opens such a source.

Values are handed on as such a file would hold them, and the statistic
registry's reader decides what each means (``fluid_build.profiler._stats``). The
only conversions here are for driver types that have no text of their own: a
timestamp becomes RFC 3339 in UTC (a zone-less one is taken as UTC, as FLUX's
clock is), a date that day's midnight in UTC, and a UUID its text. Everything
else (a Decimal, an integer, a string, a boolean, a binary float the reader will
refuse) passes as it came.

Nothing here samples: every record of the table is read, so the statistics
describe the table and not a sample of it. The asked-for columns are held in memory
while the registry measures them (its ``measure`` takes a column's values whole), so
a table larger than memory is out of reach until the registry measures in parts.

A message from here names a column, a line number, a path or a masked URI, never
a record's value. A DuckDB error that can quote a record (a conversion or a
parse error) is reported by its class alone.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

__all__ = [
    "DATABASE_SCHEMES",
    "FILE_FORMATS",
    "SourceColumns",
    "SourceError",
    "describe_source",
    "local_file",
    "read_columns",
]

#: The file suffixes ``--source`` reads, and the reader each one takes.
FILE_FORMATS: Mapping[str, str] = {
    ".ndjson": "ndjson",
    ".jsonl": "ndjson",
    ".csv": "csv",
    ".parquet": "parquet",
}

#: The URI schemes of a database ``--source``, and the scanner each one attaches with.
DATABASE_SCHEMES: Mapping[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}

#: The DuckDB column types that can hold +-infinity, which the driver would read as
#: the largest or smallest date it has.
_MAY_BE_INFINITE = frozenset(
    {
        "DATE",
        "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
    }
)


class SourceError(Exception):
    """The source cannot be read; the message names where, never a record's value."""


@dataclass(frozen=True)
class SourceColumns:
    """What one read of a table returns: its row count and the asked-for columns."""

    description: str
    row_count: int
    values: Mapping[str, List[Any]]


def _scheme(source: str) -> str:
    return source.split("://", 1)[0].lower() if "://" in source else ""


def describe_source(source: str) -> str:
    """*source* as it may be shown: a database URI with its password masked.

    A URI after a leading ``<name>=`` is masked too, so a ``--source`` paired with an
    exposeId, or with a mistyped one, never shows its password. A URI's own ``=`` (its
    query) sits after ``://``.
    """
    if _scheme(source) in DATABASE_SCHEMES:
        from fluid_build.cli.discover._jdbc_introspect import _masked_uri

        return _masked_uri(source)
    head, sep, rest = source.partition("=")
    if sep and "://" not in head and "://" in rest:
        return f"{head}={describe_source(rest)}"
    return source


def local_file(source: str, *, cwd: Optional[Path] = None) -> Optional[Path]:
    """The local file *source* reads (a file, or a sqlite database), or ``None``."""
    scheme = _scheme(source)
    here = cwd or Path.cwd()
    if scheme == "sqlite":
        from fluid_build.cli.discover._jdbc_introspect import _parse_uri

        try:
            path = Path(_parse_uri(source, "sqlite")["path"])
        except ValueError:
            return None
        return path if path.is_absolute() else here / path
    if scheme in DATABASE_SCHEMES:
        return None
    try:
        return _local_path(source, here)
    except SourceError:
        return None


def read_columns(
    source: str,
    columns: Sequence[str],
    *,
    table: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> SourceColumns:
    """Read every record of the table *source* names, keeping *columns*.

    *table* names the table of a database source (``name`` or ``schema.name``)
    and is refused for a file, which is one table. A relative path is read
    against *cwd* (default: the process's working directory).
    """
    if not columns:
        raise SourceError("no column to read")
    scheme = _scheme(source)
    if scheme in DATABASE_SCHEMES:
        if not table:
            raise SourceError(
                f"--source {describe_source(source)} is a database: name its table with --table"
            )
        return _read_database(source, DATABASE_SCHEMES[scheme], table, columns)
    if table:
        raise SourceError("--table names a table of a database --source; a file is one table")
    path = _local_path(source, cwd or Path.cwd())
    suffix = path.suffix.lower()
    kind = FILE_FORMATS.get(suffix)
    if kind is None:
        known = ", ".join(sorted(FILE_FORMATS))
        raise SourceError(
            f"{path} is not a file --source reads: give a {known} file, or a "
            "postgres://, mysql:// or sqlite:/// database"
        )
    if not path.is_file():
        raise SourceError(f"{path} is not a file")
    if kind == "ndjson":
        return _read_ndjson(path, columns)
    return _read_file_with_duckdb(path, kind, columns)


def _local_path(source: str, cwd: Path) -> Path:
    if "://" not in source:
        try:
            path = Path(source).expanduser()
        except RuntimeError:
            raise SourceError(
                f"--source {source} names a home directory that does not exist"
            ) from None
        return path if path.is_absolute() else cwd / path
    parsed = urlparse(source)
    if parsed.scheme.lower() != "file":
        raise SourceError(
            f"--source does not read {parsed.scheme}:// in this release: give a local file "
            "(a path or a file:// URL), or a postgres://, mysql:// or sqlite:/// database"
        )
    if parsed.netloc not in ("", "localhost"):
        raise SourceError("a file:// --source names no host other than localhost")
    return Path(unquote(parsed.path))


# ---------------------------------------------------------------------------
# Values as a file would hold them
# ---------------------------------------------------------------------------


def _rfc3339(moment: datetime) -> str:
    """RFC 3339 in UTC; milliseconds when they hold every digit, microseconds otherwise.

    A zone-less timestamp is taken as UTC. Sub-millisecond digits are kept, not
    dropped: the reader refuses a precision FLUX's millisecond clock cannot hold,
    rather than this module rounding it away.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    micro = moment.microsecond
    fraction = f"{micro // 1000:03d}" if micro % 1000 == 0 else f"{micro:06d}"
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{fraction}Z"
    )


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return _rfc3339(value)
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}T00:00:00.000Z"
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# Newline-delimited JSON, read as flux-engine reads a committed slice
# ---------------------------------------------------------------------------


class _BinaryFloat(Exception):
    pass


def _refuse_float(_text: str) -> Any:
    raise _BinaryFloat


def _refuse_constant(_text: str) -> Any:
    raise _BinaryFloat


def _read_ndjson(path: Path, columns: Sequence[str]) -> SourceColumns:
    values: Dict[str, List[Any]] = {name: [] for name in columns}
    rows = 0
    try:
        handle = path.open("r", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise SourceError(f"{path} could not be opened: {exc.strerror}") from None
    try:
        with handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                record = _json_line(line, path, number)
                row = {unicodedata.normalize("NFC", str(key)): item for key, item in record.items()}
                for name in columns:
                    if name not in row:
                        raise SourceError(f"column {name!r} is absent from {path} line {number}")
                    values[name].append(_wire(row[name]))
                rows += 1
    except UnicodeDecodeError:
        raise SourceError(f"{path} is not UTF-8 text") from None
    except OSError as exc:
        raise SourceError(f"{path} could not be read: {exc.strerror}") from None
    return SourceColumns(description=str(path), row_count=rows, values=values)


def _json_line(line: str, path: Path, number: int) -> Mapping[str, Any]:
    try:
        record = json.loads(line, parse_float=_refuse_float, parse_constant=_refuse_constant)
    except _BinaryFloat:
        raise SourceError(
            f"{path} line {number} carries a binary floating-point number: write numbers "
            'as exact decimal strings ("12.50"), as a FLUX slice does (KERNEL-SPEC §2.1)'
        ) from None
    except json.JSONDecodeError as exc:
        raise SourceError(f"{path} line {number} is not JSON: {exc.msg}") from None
    except RecursionError:
        raise SourceError(f"{path} line {number} nests too deeply to read") from None
    except ValueError:
        # Python refuses an integer of more than 4300 digits (CVE-2020-10735).
        raise SourceError(f"{path} line {number} holds an integer too long to read") from None
    if not isinstance(record, dict):
        raise SourceError(f"{path} line {number} is a JSON {type(record).__name__}, not an object")
    return record


# ---------------------------------------------------------------------------
# DuckDB: CSV and Parquet files, and attached databases
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    """*name* as a quoted DuckDB identifier; a name with a control character is refused."""
    from fluid_build.providers._sql_safety import reject_control_chars

    try:
        reject_control_chars(name, "column or table name")
    except ValueError:
        raise SourceError("a column or table name holds a control character") from None
    return '"' + name.replace('"', '""') + '"'


def _selected(name: str, column_type: str) -> str:
    """The select expression for one column.

    A ``TIMESTAMP WITH TIME ZONE`` is selected as the UTC timestamp it denotes: the
    driver would otherwise build zone-aware values through ``pytz``, which the extra
    does not install, and the reading is the same instant either way.
    """
    quoted = _quote_ident(name)
    if column_type == "TIMESTAMP WITH TIME ZONE":
        return f"timezone('UTC', {quoted})"
    return quoted


def _described_error(exc: BaseException, subject: str, uri: Optional[str]) -> SourceError:
    """An error from ``DESCRIBE``, before any record is read, as a :class:`SourceError`.

    A catalog or binder error names a table or a column, and its text is kept. For a
    file, so is the text of an I/O or a permission error, which names the file. For a
    database (*uri* given), an I/O error can carry the server's own message, which can
    quote a value, so it is named by its class, like any other error (a CSV sniffing
    error quotes a line). A kept text has the URI's password masked.
    """
    import duckdb

    named: Tuple[type, ...] = (duckdb.CatalogException, duckdb.BinderException)
    if uri is None:
        named += (duckdb.IOException, duckdb.PermissionException)
    if not isinstance(exc, named):
        return _read_error(exc, subject)
    text = str(exc)
    if uri is not None:
        from fluid_build.cli.discover._jdbc_introspect import _error_text

        text = _error_text(exc, uri)
    return SourceError(f"{subject}: {text}")


def _read_error(exc: BaseException, subject: str) -> SourceError:
    """An error while records are read, named by its class alone: it can depend on a record."""
    return SourceError(
        f"{subject}: DuckDB raised {type(exc).__name__}; its message is not shown, "
        "because it can quote a record"
    )


def _fetch(
    con: Any,
    relation: str,
    columns: Sequence[str],
    *,
    subject: str,
    uri: Optional[str] = None,
) -> Tuple[int, Dict[str, List[Any]]]:
    """Read *columns* of *relation* (already quoted SQL) whole, in one read.

    One ``fetchall()``, never ``fetchmany()``: on DuckDB 2.0 a result read only
    partway keeps its connection busy (``providers/_duckdb_compat.first_row``).
    Every value is held in memory to be measured anyway.
    """
    try:
        described = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
    except Exception as exc:  # noqa: BLE001 - every DuckDB failure is reported
        raise _described_error(exc, subject, uri) from None
    types = {row[0]: str(row[1]).upper() for row in described}
    missing = [name for name in columns if name not in types]
    if missing:
        raise SourceError("the source has no column " + ", ".join(repr(name) for name in missing))
    selected = [_selected(name, types[name]) for name in columns]
    # The driver reads +-infinity as the largest or smallest date it has, a real
    # date, so infinity is asked of DuckDB beside each date or timestamp column.
    infinite = [name for name in columns if types[name] in _MAY_BE_INFINITE]
    selected += [f"isinf({_quote_ident(name)})" for name in infinite]
    values: Dict[str, List[Any]] = {name: [] for name in columns}
    try:
        records = con.execute(f"SELECT {', '.join(selected)} FROM {relation}").fetchall()
        rows = len(records)
        for record in records:
            for name, flag in zip(infinite, record[len(columns) :], strict=True):
                if flag:
                    raise SourceError(
                        f"column {name!r} holds an infinite date or timestamp, "
                        "which FLUX cannot read"
                    )
            for name, item in zip(columns, record[: len(columns)], strict=True):
                values[name].append(_wire(item))
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - every DuckDB failure is reported, none quoted
        raise _read_error(exc, subject) from None
    return rows, values


def _read_file_with_duckdb(path: Path, kind: str, columns: Sequence[str]) -> SourceColumns:
    from fluid_build._optional import require

    require("duckdb", "profile", "fluid profile")
    from fluid_build.providers._duckdb_sandbox import DuckDBAllowlist, secure_duckdb_connect
    from fluid_build.providers._sql_safety import quote_ansi_string_literal

    location = str(path)
    reader = (
        f"read_csv({quote_ansi_string_literal(location)}, all_varchar = true, header = true)"
        if kind == "csv"
        else f"read_parquet({quote_ansi_string_literal(location)})"
    )
    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_paths(location))
    try:
        rows, values = _fetch(
            con, reader, columns, subject=f"{path} could not be read as {kind.upper()}"
        )
    finally:
        con.close()
    return SourceColumns(description=location, row_count=rows, values=values)


def _table_reference(alias: str, table: str) -> str:
    parts = table.split(".")
    if not 1 <= len(parts) <= 2 or not all(parts):
        raise SourceError("--table is a table name or schema.name")
    return ".".join([alias] + [_quote_ident(part) for part in parts])


def _read_database(source: str, kind: str, table: str, columns: Sequence[str]) -> SourceColumns:
    from fluid_build._optional import require

    require("duckdb", "profile", "fluid profile")
    from fluid_build.cli.discover._jdbc_introspect import _error_text, attach_jdbc_source

    shown = describe_source(source)
    if kind == "sqlite":
        # DuckDB's sqlite scanner creates a database at a path that does not exist.
        from fluid_build.cli.discover._jdbc_introspect import _parse_uri

        if not Path(_parse_uri(source, "sqlite")["path"]).is_file():
            raise SourceError(f"{shown} names no sqlite database file")
    try:
        # Typed by the operator on the command line, so it authenticates as the
        # operator's own client does (see build_attach_dsn). Read-only: a profile
        # reads, and nothing it runs can write to the source.
        attached = attach_jdbc_source(kind, source, operator_supplied=True, read_only=True)
    except ImportError as exc:
        raise SourceError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - every connect failure is reported, masked
        # Nothing has been read yet, so a connect error quotes no record; its text
        # is shown with every reading of the URI's password masked.
        raise SourceError(f"{shown} could not be opened: {_error_text(exc, source)}") from None
    con = attached.connection
    try:
        relation = _table_reference(attached.alias, table)
        rows, values = _fetch(
            con,
            relation,
            columns,
            subject=f"table {table!r} of {shown} could not be read",
            uri=source,
        )
    finally:
        con.close()
    return SourceColumns(description=f"{shown} table {table}", row_count=rows, values=values)
