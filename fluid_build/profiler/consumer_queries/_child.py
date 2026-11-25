# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0
# Adapted from FLUX flux_engine/consumers/_child.py and _frames.py at 19dfe48, Apache-2.0

"""The query process: statements over a locked DuckDB.

Started by :mod:`.runner` as ``python -I <this file>`` with an environment of its own (``PATH``
and ``LC_ALL`` only) and its two pipes as the only descriptors it holds. This file imports the
standard library and, after the limits are set, ``duckdb``: nothing of forge, so ``-I`` (no
working directory and no user site on ``sys.path``) leaves nothing to import but the two. The
parent imports this file too, for the framing and the settings both ends must agree on; that
needs the standard library only.

The framing is a 4-byte big-endian length and then that many bytes: a JSON header frame and, for
a table, one JSON frame of values per column. Nothing is pickled, a frame past its bound ends the
read before a byte of it is taken, and JSON that holds a float or a non-finite number is refused,
so a count is a whole number on both sides.

In this order:

1. Read ``init`` (the limits). Standard library only.
2. Lower the resource limits the platform enforces, **before** DuckDB is imported:
   ``RLIMIT_FSIZE`` to 0 (a file may not grow past zero bytes), ``RLIMIT_CPU`` to the time budget
   and ``RLIMIT_AS`` to the memory budget (where the platform enforces it; macOS accepts a
   setting and ignores it, and so reports ``None``). A limit the platform refuses is reported
   as ``None`` too, never as a number that is not in force.
3. Import DuckDB and open the in-memory database with every lock passed at connect
   (:func:`connect_settings`), then read the settings back from DuckDB itself.
4. Lower ``RLIMIT_NOFILE`` to the highest descriptor it holds plus a small margin, so it can open
   no new file.
5. Answer ``hello`` (version, limits set, settings and their read-back, the *names* of its
   environment variables) and then ``table``, ``run`` and ``stop``.

A statement is split with DuckDB's own ``extract_statements`` and runs only when that finds
exactly one statement and it is a ``SELECT``. A result is a whole number, the rows of a grouped
count (booleans, whole numbers, text and ``NULL`` for the group, a whole count), or one boolean
per row packed into bits. DuckDB's own message never leaves the process: it can quote a value of
a table. Nothing is printed or logged.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import struct
import sys
import threading
from typing import IO, Any, Dict, List, Optional, Tuple

__all__ = [
    "MIN_DUCKDB",
    "FrameError",
    "connect_settings",
    "decode_json",
    "encode_json",
    "main",
    "read_frame",
    "settings_read_back",
    "write_frame",
]

#: The lowest DuckDB the child runs on (the sandbox's floor, ``providers/_duckdb_sandbox.py``).
MIN_DUCKDB: Tuple[int, int, int] = (1, 5, 0)

#: The most bytes of a JSON header the parent sends: a request holds one text.
HEADER_LIMIT = 4 << 20
#: The most bytes of one column of values.
COLUMN_LIMIT = 1 << 28
#: The most bytes of a reply: whole numbers, group rows and packed bits.
REPLY_LIMIT = 64 << 20

#: The most groups one grouped count may return, and the most rows one bit column may have.
MAX_GROUPS = 100_000
MAX_BITS = 1 << 28

#: The room above the highest open descriptor the child keeps when it lowers its file limit.
_NOFILE_SPARE = 8
#: The address space the child may add to its memory budget, for the interpreter, DuckDB and the
#: tables it is sent. Applied only where the platform enforces ``RLIMIT_AS``.
_ADDRESS_SPACE_SPARE = 4 << 30

COUNTED = "ok"
TIMED_OUT = "timedOut"
OUT_OF_MEMORY = "outOfMemory"
ERROR = "error"

_LENGTH = struct.Struct(">I")
_RETURNS = ("count", "counts", "bits")
_NAME = re.compile(r"[^\x00]{1,256}")
_DECIMAL = re.compile(r"^DECIMAL\((\d{1,2}),(\d{1,2})\)$")
_PLAIN_TYPES = frozenset(
    {"BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "VARCHAR"}
)


class FrameError(ValueError):
    """A frame that is too large, or a header that is not a JSON object of whole numbers and text."""


class _Refused(Exception):
    """A request the child will not run, with the reason it reports (a sentence of ours)."""

    def __init__(self, reason: str, status: str = ERROR) -> None:
        super().__init__()
        self.reason = reason
        self.status = status


# ----------------------------------------------------------------------------- framing


def _refuse_number(text: str) -> Any:
    raise FrameError("a message holds a float or a non-finite number")


def encode_json(document: Any) -> bytes:
    """*document* as UTF-8 JSON bytes (ASCII only, so no text can fail to encode)."""
    try:
        return json.dumps(
            document, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise FrameError(f"a message cannot be written: {type(error).__name__}") from error


def decode_json(data: bytes) -> Any:
    """The JSON value in *data*, or a :class:`FrameError`."""
    try:
        return json.loads(
            data.decode("utf-8"), parse_float=_refuse_number, parse_constant=_refuse_number
        )
    except FrameError:
        raise
    except (ValueError, RecursionError) as error:
        raise FrameError(f"a message is not JSON in UTF-8: {type(error).__name__}") from error


def _read_exactly(stream: IO[bytes], size: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("the pipe closed inside a frame")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def read_frame(stream: IO[bytes], limit: int) -> bytes:
    """One frame of at most *limit* bytes. ``EOFError`` when the pipe is closed."""
    prefix = stream.read(_LENGTH.size)
    if not prefix:
        raise EOFError("the pipe closed")
    if len(prefix) < _LENGTH.size:
        prefix += _read_exactly(stream, _LENGTH.size - len(prefix))
    (length,) = _LENGTH.unpack(prefix)
    if length > limit:
        raise FrameError(f"a frame announces {length} bytes; the bound is {limit}")
    return _read_exactly(stream, length) if length else b""


def write_frame(stream: IO[bytes], data: bytes, limit: int) -> None:
    """Write one frame of at most *limit* bytes and flush."""
    if len(data) > limit:
        raise FrameError(f"a frame holds {len(data)} bytes; the bound is {limit}")
    stream.write(_LENGTH.pack(len(data)) + data)
    stream.flush()


def _read_header(stream: IO[bytes]) -> Dict[str, Any]:
    document = decode_json(read_frame(stream, HEADER_LIMIT))
    if not isinstance(document, dict):
        raise FrameError("a header is not a JSON object")
    return document


# ----------------------------------------------------------------------------- limits


def _lower(resource: Any, which: int, value: int) -> Optional[int]:
    """Lower both limits of *which* to *value* (or leave one that is lower); the limit now in force.

    ``None`` when the platform refuses: a limit that is not in force is never reported.
    """
    try:
        soft, hard = resource.getrlimit(which)
        for current in (soft, hard):
            if current != resource.RLIM_INFINITY and current < value:
                value = current
        resource.setrlimit(which, (value, value))
        return int(value)
    except (ValueError, OSError):
        return None


def _highest_descriptor(limit: int) -> int:
    """The highest file descriptor this process holds open."""
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        return max((int(name) for name in names if name.isdigit()), default=2)
    highest = 2
    for descriptor in range(min(limit, 65_536)):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        highest = descriptor
    return highest


def _lower_before_imports(memory: int, cpu: int) -> Dict[str, Optional[int]]:
    """A file may not grow, CPU time is capped, and so is the address space where it is enforced."""
    done: Dict[str, Optional[int]] = {"fsize": None, "cpu": None, "as": None, "nofile": None}
    try:
        import resource
    except ImportError:  # no ``resource`` on this platform
        return done
    done["fsize"] = _lower(resource, resource.RLIMIT_FSIZE, 0)
    done["cpu"] = _lower(resource, resource.RLIMIT_CPU, cpu)
    # macOS accepts RLIMIT_AS and does not enforce it, so a number there would claim a limit that
    # is not in force. The headroom above the memory budget covers the interpreter and DuckDB
    # mappings and the tables the child is sent; DuckDB's memory_limit does not count them.
    if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
        done["as"] = _lower(resource, resource.RLIMIT_AS, memory + _ADDRESS_SPACE_SPARE)
    return done


def _lower_nofile(done: Dict[str, Optional[int]]) -> None:
    try:
        import resource
    except ImportError:
        return
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    ceiling = soft if soft != resource.RLIM_INFINITY else 65_536
    done["nofile"] = _lower(
        resource, resource.RLIMIT_NOFILE, _highest_descriptor(ceiling) + _NOFILE_SPARE
    )


# ----------------------------------------------------------------------------- database


def _memory_text(memory: int) -> str:
    return f"{memory >> 20}MiB" if memory % (1 << 20) == 0 else f"{memory}B"


def connect_settings(memory: int) -> Dict[str, Any]:
    """Exactly the settings the child connects with, all passed when the connection opens."""
    return {
        "enable_external_access": False,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "allow_community_extensions": False,
        "allow_unsigned_extensions": False,
        "allow_persistent_secrets": False,
        "python_enable_replacements": False,
        "threads": 1,
        "memory_limit": _memory_text(memory),
        "max_temp_directory_size": "0B",
        "lock_configuration": True,
    }


#: What DuckDB reports for the settings that must hold, read back from ``duckdb_settings()``.
_EXPECTED_READ_BACK: Dict[str, str] = {
    "enable_external_access": "false",
    "autoload_known_extensions": "false",
    "autoinstall_known_extensions": "false",
    "allow_community_extensions": "false",
    "allow_unsigned_extensions": "false",
    "allow_persistent_secrets": "false",
    "python_enable_replacements": "false",
    "threads": "1",
    "max_temp_directory_size": "0 bytes",
    "lock_configuration": "true",
}


def settings_read_back() -> Dict[str, str]:
    """What the child must read back from DuckDB for each lock; the parent compares."""
    return dict(_EXPECTED_READ_BACK)


def _read_back(connection: Any) -> Dict[str, str]:
    names = sorted(_EXPECTED_READ_BACK)
    marks = ",".join("?" for _ in names)
    rows = connection.execute(
        f"SELECT name, CAST(value AS VARCHAR) FROM duckdb_settings() WHERE name IN ({marks})",
        names,
    ).fetchall()
    return {str(name): str(value) for name, value in rows}


class _Deadline:
    """Interrupts the connection when a statement passes its seconds.

    The interrupt is sent under the lock and only while the deadline is armed, so once
    :meth:`disarm` returns no interrupt is in flight or still to come, and a late timer cannot
    stop the next statement.
    """

    def __init__(self, connection: Any, seconds: int) -> None:
        self._connection = connection
        self._seconds = seconds
        self._lock = threading.Lock()
        self._armed = False
        self._timer: Optional[threading.Timer] = None

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self._timer = threading.Timer(self._seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._armed:
                self._connection.interrupt()

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            if self._timer is not None:
                self._timer.cancel()


def _quote(name: str) -> str:
    """An identifier, quoted: every ``"`` doubled. No value is ever formatted into SQL."""
    return '"' + name.replace('"', '""') + '"'


def _column_expression(position: int, type_text: str) -> str:
    """The expression that unnests the list parameter ``$position`` as a column of *type_text*.

    *type_text* must be one of the types below, matched exactly; nothing of it is formatted into
    SQL unless it matched.
    """
    param = f"${position}"
    if type_text in _PLAIN_TYPES:
        return f"unnest({param}::{type_text}[])"
    if type_text == "DOUBLE":
        return f"CAST(unnest({param}::VARCHAR[]) AS DOUBLE)"
    if type_text == "DATE":
        return f"CAST(epoch_ms(unnest({param}::BIGINT[])) AS DATE)"
    if type_text == "TIMESTAMP":
        return f"epoch_ms(unnest({param}::BIGINT[]))"
    found = _DECIMAL.match(type_text)
    if found is not None:
        precision, scale = int(found.group(1)), int(found.group(2))
        if 1 <= precision <= 38 and 0 <= scale <= precision:
            return f"CAST(unnest({param}::VARCHAR[]) AS DECIMAL({precision},{scale}))"
    raise _Refused("a column has a type the process does not load")


def _create_table(
    connection: Any, name: str, columns: List[Tuple[str, str]], values: List[List[Any]]
) -> None:
    selects = ", ".join(
        f"{_column_expression(index + 1, kind)} AS {_quote(column)}"
        for index, (column, kind) in enumerate(columns)
    )
    connection.execute(f"CREATE TABLE {_quote(name)} AS SELECT {selects}", values)


# ----------------------------------------------------------------------------- statements


def _whole(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _Refused("a count is not a whole number")
    return value


def _group_key(value: Any) -> Any:
    """A group key as it crosses the pipe: a boolean, a whole number, text or ``None``."""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value).__name__ in ("Decimal", "date", "datetime", "time"):
        return str(value)
    raise _Refused("a group key is of a type the process does not return")


def _fetch_count(cursor: Any) -> Dict[str, Any]:
    rows = cursor.fetchmany(2)
    if len(rows) != 1 or len(rows[0]) != 1:
        raise _Refused("a count is one row of one column")
    return {"count": _whole(rows[0][0])}


def _fetch_counts(cursor: Any) -> Dict[str, Any]:
    rows = cursor.fetchmany(MAX_GROUPS + 1)
    if len(rows) > MAX_GROUPS:
        raise _Refused(f"the result has more than {MAX_GROUPS} groups")
    groups: List[List[Any]] = []
    for row in rows:
        if len(row) < 2:
            raise _Refused("a grouped count is group columns and a count")
        groups.append([_group_key(key) for key in row[:-1]] + [_whole(row[-1])])
    return {"groups": groups}


def _fetch_bits(cursor: Any) -> Dict[str, Any]:
    packed = bytearray()
    total = 0
    while True:
        rows = cursor.fetchmany(65_536)
        if not rows:
            break
        for row in rows:
            if len(row) != 1 or type(row[0]) is not bool:
                raise _Refused("a bit column holds a boolean in every row")
            if total >= MAX_BITS:
                raise _Refused("the result has more rows than a bit column carries")
            if total % 8 == 0:
                packed.append(0)
            if row[0]:
                packed[-1] |= 1 << (total % 8)
            total += 1
    return {"bits": {"n": total, "data": base64.b64encode(bytes(packed)).decode("ascii")}}


_FETCH = {"count": _fetch_count, "counts": _fetch_counts, "bits": _fetch_bits}


def _engine_error(error: BaseException) -> str:
    """What crosses the pipe about an engine error: its class name and a sentence of ours.

    DuckDB's message is never sent. It can quote a value of a table, and no value of the data
    leaves this process.
    """
    return f"{type(error).__name__}: the engine refused the statement"


def _is_memory(error: BaseException) -> bool:
    """Whether DuckDB ran out of memory or room to spill (read here; the message never leaves)."""
    text = str(error).lower()
    # With no temporary directory allowed, a statement that must spill reports that it cannot
    # make one (an IO error) where one that may spill reports the memory error.
    return (
        "out of memory" in text
        or "max_temp_directory_size" in text
        or ("failed to create directory" in text and ".tmp" in text)
    )


def _result(statement_id: str, status: str, reason: Optional[str]) -> Dict[str, Any]:
    return {
        "op": "result",
        "id": statement_id,
        "status": status,
        "reason": reason,
        "count": None,
        "groups": None,
        "bits": None,
    }


def _select(connection: Any, duckdb: Any, sql: str, select_only: bool) -> Any:
    """The one statement of *sql*, parsed by DuckDB, as the object that is then executed."""
    statements = connection.extract_statements(sql)
    if len(statements) != 1:
        raise _Refused(f"the text holds {len(statements)} statements; exactly one SELECT runs")
    statement = statements[0]
    if select_only and statement.type != duckdb.StatementType.SELECT:
        raise _Refused(f"the text is a {statement.type.name} statement; only a SELECT runs")
    return statement


def _run(
    connection: Any, duckdb: Any, request: Dict[str, Any], seconds: int, select_only: bool
) -> Dict[str, Any]:
    """Answer one ``run`` request. Never raises for a statement: a failure is the answer's status."""
    statement_id = request["id"]
    deadline = _Deadline(connection, seconds)
    deadline.arm()
    try:
        statement = _select(connection, duckdb, request["sql"], select_only)
        answer = _FETCH[request["returns"]](connection.execute(statement))
        result = _result(statement_id, COUNTED, None)
        result.update(answer)
        return result
    except _Refused as refusal:
        return _result(statement_id, refusal.status, refusal.reason)
    except duckdb.InterruptException:
        return _result(statement_id, TIMED_OUT, f"the statement passed {seconds} seconds")
    except duckdb.OutOfMemoryException:
        return _result(statement_id, OUT_OF_MEMORY, "the statement passed the memory limit")
    except duckdb.Error as error:
        if _is_memory(error):
            return _result(statement_id, OUT_OF_MEMORY, "the statement passed the memory limit")
        return _result(statement_id, ERROR, _engine_error(error))
    # A fault here is an answer, not a dead process.
    except Exception as error:  # noqa: BLE001
        return _result(statement_id, ERROR, f"internal error: {type(error).__name__}")
    finally:
        deadline.disarm()


# ----------------------------------------------------------------------------- messages


def _reply(out: IO[bytes], document: Dict[str, Any]) -> None:
    data = encode_json(document)
    if len(data) > REPLY_LIMIT:
        if document.get("op") != "result":
            raise FrameError("a reply passes its bound")
        data = encode_json(
            _result(str(document["id"]), ERROR, "the result passes the size of a reply")
        )
    write_frame(out, data, REPLY_LIMIT)


def _read_init(stdin: IO[bytes]) -> Optional[Tuple[int, int, int]]:
    """The ``(seconds, memoryBytes, cpuSeconds)`` of the ``init`` frame, or ``None`` when refused."""
    try:
        init = _read_header(stdin)
        if init.get("op") != "init":
            return None
        values = []
        for key, low, high in (
            ("seconds", 1, 86_400),
            ("memoryBytes", 1 << 20, 1 << 44),
            ("cpuSeconds", 1, 10_000_000),
        ):
            value = init.get(key)
            if type(value) is not int or not low <= value <= high:
                return None
            values.append(value)
    except (EOFError, FrameError):
        return None
    return values[0], values[1], values[2]


def _table_columns(request: Dict[str, Any]) -> Tuple[str, int, List[Tuple[str, str]]]:
    name = request.get("name")
    rows = request.get("rows")
    declared = request.get("columns")
    if type(name) is not str or not _NAME.fullmatch(name):
        raise _Refused("a table has a name")
    if type(rows) is not int or rows < 0:
        raise _Refused("a table has a row count")
    if not isinstance(declared, list) or not declared:
        raise _Refused("a table has columns")
    columns: List[Tuple[str, str]] = []
    seen = set()
    for item in declared:
        if not isinstance(item, dict):
            raise _Refused("a column is a name and a type")
        column, kind = item.get("name"), item.get("type")
        if type(column) is not str or not _NAME.fullmatch(column) or type(kind) is not str:
            raise _Refused("a column is a name and a type")
        if column.lower() in seen:
            raise _Refused("a table has two columns of one name")
        seen.add(column.lower())
        columns.append((column, kind))
    return name, rows, columns


def _chunk_counts(request: Dict[str, Any]) -> List[int]:
    """How many frames each column arrives in, as declared; never trusted past what was sent."""
    declared = request.get("columns")
    counts: List[int] = []
    if isinstance(declared, list):
        for item in declared:
            chunks = item.get("chunks") if isinstance(item, dict) else None
            counts.append(chunks if type(chunks) is int and 0 <= chunks <= 1 << 20 else 0)
    return counts


def _load_table(
    request: Dict[str, Any], stdin: IO[bytes], engine: Tuple[Any, Any], tables: set[str]
) -> None:
    """Read the column frames of a ``table`` message (always all of them) and create the table."""
    connection, duckdb = engine
    counts = _chunk_counts(request)
    frames = [[read_frame(stdin, COLUMN_LIMIT) for _ in range(count)] for count in counts]
    name, rows, columns = _table_columns(request)
    if name in tables:
        raise _Refused("a table is sent once")
    values: List[List[Any]] = []
    for column_frames in frames:
        column: List[Any] = []
        for data in column_frames:
            chunk = decode_json(data)
            if not isinstance(chunk, list):
                raise _Refused("a column holds a list of values")
            column.extend(chunk)
        if len(column) != rows:
            raise _Refused("a column holds as many values as the table has rows")
        values.append(column)
    try:
        _create_table(connection, name, columns, values)
    except duckdb.OutOfMemoryException:
        raise _Refused("the table passed the memory limit", OUT_OF_MEMORY) from None
    except duckdb.Error as error:
        if _is_memory(error):
            raise _Refused("the table passed the memory limit", OUT_OF_MEMORY) from None
        raise _Refused(_engine_error(error)) from None
    except Exception as error:  # noqa: BLE001
        raise _Refused(f"internal error: {type(error).__name__}") from None
    tables.add(name)


def _serve_one(
    request: Dict[str, Any],
    stdin: IO[bytes],
    stdout: IO[bytes],
    engine: Tuple[Any, Any],
    tables: set[str],
    seconds: int,
    select_only: bool,
) -> Optional[int]:
    """Answer one ``table`` or ``run`` request; an exit status when the child must stop."""
    connection, duckdb = engine
    op = request.get("op")
    try:
        if op == "table":
            _load_table(request, stdin, engine, tables)
            _reply(stdout, {"op": "ok"})
        elif op == "run":
            if (
                type(request.get("id")) is not str
                or type(request.get("sql")) is not str
                or request.get("returns") not in _RETURNS
            ):
                raise _Refused("a run has an id, a text and what it returns")
            _reply(stdout, _run(connection, duckdb, request, seconds, select_only))
        else:
            return 2
    except _Refused as refusal:
        _reply(stdout, {"op": "refused", "reason": refusal.reason, "status": refusal.status})
    except (EOFError, FrameError):
        return 2
    return None


def main(*, select_only: bool = True) -> int:
    """Run the protocol until ``stop`` or the end of stdin. The process exit status.

    *select_only* is ``False`` only for a test that drives the child with statements that are
    not queries, to show that the locks hold under the same path; the parent never passes it.
    """
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing a library prints can land in a frame
    asked = _read_init(stdin)
    if asked is None:
        return 2
    seconds, memory, cpu = asked
    limits = _lower_before_imports(memory, cpu)
    try:
        duckdb = importlib.import_module("duckdb")
    except ModuleNotFoundError as error:
        _reply(stdout, {"op": "missing", "module": error.name or ""})
        return 3
    settings = connect_settings(memory)
    connection = duckdb.connect(":memory:", config=settings)
    read_back = _read_back(connection)
    _lower_nofile(limits)
    _reply(
        stdout,
        {
            "op": "hello",
            "duckdb": str(duckdb.__version__),
            "python": sys.version.split()[0],
            "limits": limits,
            "settings": {key: settings[key] for key in sorted(settings)},
            "readBack": read_back,
            "environment": sorted(os.environ),
        },
    )
    tables: set[str] = set()
    engine = (connection, duckdb)
    while True:
        try:
            request = _read_header(stdin)
        except EOFError:
            return 0
        except FrameError:
            return 2
        if request.get("op") == "stop":
            return 0
        status = _serve_one(request, stdin, stdout, engine, tables, seconds, select_only)
        if status is not None:
            return status


if __name__ == "__main__":
    raise SystemExit(main())
