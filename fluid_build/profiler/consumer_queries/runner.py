# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Statements over a profile's tables, in a locked DuckDB in a process of their own.

The parent (this module) never imports DuckDB. It starts the child, ``python -I <_child.py>``
(see :mod:`._child`), sends it the tables as framed JSON, asks it for each statement's result
and stops it. Nothing is pickled and every message is bounded in size on both sides.

**The child's world.** Its environment holds ``PATH`` and ``LC_ALL=C`` and nothing else the
parent has: no inherited variable, credential, ``DUCKDB_*`` or ``PYTHON*`` setting reaches it.
It gets no stderr, no descriptor but its two pipes (``close_fds``, no ``pass_fds``), and ``/`` as
its working directory. Its first reply names its environment variables (never their values), the
limits it set and the settings DuckDB reads back; this module refuses a child whose settings are
not the ones it asked for, and a DuckDB older than the sandbox's floor
(:data:`fluid_build.providers._duckdb_sandbox.MIN_DUCKDB_VERSION`).

**Time.** A statement that passes ``seconds`` is interrupted inside the child and takes the
status ``timedOut``. The parent keeps a clock of its own per statement (``seconds`` plus
``grace_seconds``) and ends a child that has not answered by then; that statement is ``timedOut``
too. A statement that runs out of memory takes ``outOfMemory``, one DuckDB refuses (a lock held,
a type, a name) takes ``error``, and the others still run.

**Crashes.** When the child dies (a signal, a resource limit, a fault inside DuckDB), the statement
in flight takes ``crashed`` and is not run again. A fresh child, under the same locks, is sent the
tables again and runs the statements that remain. A child that cannot be started again leaves the
rest ``crashed``.

**What a result holds.** Whole numbers, booleans, group keys and bits, and a status with a
reason of this module's own words. DuckDB's message is never passed on (it can quote a value of
a table), nothing of the tables is printed or logged, and the reasons name no value.

Adapted from FLUX ``flux_engine/consumers/runner.py`` at 19dfe48 (Apache-2.0).
"""

from __future__ import annotations

import base64
import binascii
import math
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.profiler.consumer_queries import _child

__all__ = [
    "Bits",
    "ChildRun",
    "ChildUnavailable",
    "Statement",
    "StatementResult",
    "run_in_child",
]

#: The statuses a statement takes.
STATUSES = ("ok", "timedOut", "outOfMemory", "error", "crashed")
RETURNS = ("count", "counts", "bits")

#: The seconds past a statement's limit before the parent ends the child.
GRACE_SECONDS = 15
#: The seconds the child has to start and take its tables.
START_SECONDS = 300
#: CPU the child may spend beyond the statements' limits: start-up and loading the tables.
_CPU_MARGIN_SECONDS = 60
_ROWS_PER_CPU_SECOND = 20_000

#: The environment of the child: these two keys and nothing the parent holds.
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}

_CHILD_FILE = str(Path(_child.__file__).resolve())
_LEAVE_SECONDS = 5
_TERMINATE_SECONDS = 2
_REASON_LIMIT = 300
_MOST_NAME = 256
#: Rows of one column in one frame: a column of any length crosses as a run of frames.
_CHUNK_ROWS = 20_000

ColumnValues = Tuple[str, str, Sequence[Any]]


class ChildUnavailable(FluidUserError):
    """The query process cannot run here, or does not hold the locks it was asked to."""

    code: str = "ConsumerQueryChildUnavailable"


def _unavailable(what: str, why: str, fix: str) -> ChildUnavailable:
    return ChildUnavailable(what=what, why=why, fix=fix, doc=doc_url())


@dataclass(frozen=True)
class Statement:
    """One statement to run: its id, one SELECT, and what it returns.

    ``returns`` is ``"count"`` (one row of one whole number), ``"counts"`` (the rows of a
    ``GROUP BY``: each row the group columns, booleans, whole numbers, text or ``NULL`` (a
    ``DECIMAL``, date or timestamp key arrives as its text), then a whole count) or ``"bits"``
    (one boolean column, one bit per row, in the order DuckDB returns the rows: write the
    statement so that order is fixed, and keep a ``NULL`` out of it, which is an error).
    """

    id: str
    sql: str
    returns: str


@dataclass(frozen=True)
class Bits:
    """One boolean per row, packed eight to a byte, bit ``i`` of byte ``i // 8`` (low bit first)."""

    n: int
    packed: bytes

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, index: int) -> bool:
        if not 0 <= index < self.n:
            raise IndexError(index)
        return bool(self.packed[index >> 3] >> (index & 7) & 1)

    def to_list(self) -> List[bool]:
        return [self[i] for i in range(self.n)]


GroupRow = Tuple[Union[None, bool, int, str], ...]


@dataclass(frozen=True)
class StatementResult:
    """What one statement returned, or why it did not.

    ``status`` is one of :data:`STATUSES`. Only ``ok`` carries a value: ``count`` for
    ``returns="count"``, ``groups`` (rows of group keys then the count, in DuckDB's order) for
    ``"counts"``, ``bits`` for ``"bits"``. ``reason`` is a sentence of this package's own, with
    no value of the data.
    """

    id: str
    status: str
    reason: Optional[str] = None
    count: Optional[int] = None
    groups: Optional[Tuple[GroupRow, ...]] = None
    bits: Optional[Bits] = None


@dataclass(frozen=True)
class ChildRun:
    """The answers of one run, in the order of the statements.

    ``duckdb_version`` is the version the child reported (empty when no child came up), ``limits``
    the resource limits it set (``None`` for one the platform does not enforce), ``environment``
    the names of its environment variables, and ``restarts`` how many fresh children replaced a
    dead one.
    """

    results: Tuple[StatementResult, ...]
    duckdb_version: str
    limits: Mapping[str, Optional[int]]
    environment: Tuple[str, ...]
    restarts: int


# ----------------------------------------------------------------------------- input checks


def _name(value: Any, what: str) -> str:
    if type(value) is not str or not value or len(value) > _MOST_NAME or "\x00" in value:
        raise ValueError(f"{what} is a text of 1 to {_MOST_NAME} characters with no NUL")
    return value


def _type(value: Any) -> str:
    """A DuckDB type the child loads, normalized, or a ValueError."""
    text = "".join(str(value).upper().split())
    if text in _child._PLAIN_TYPES or text in ("DOUBLE", "DATE", "TIMESTAMP"):
        return text
    try:
        _child._column_expression(1, text)
    except _child._Refused:
        raise ValueError("a column has a type the process does not load") from None
    return text


def _whole(value: Any, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{what} is a whole number from {low} to {high}")
    return value


def _decimal_text(value: Any) -> str:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise ValueError("a decimal value is not a number") from None
    if not number.is_finite():
        raise ValueError("a decimal value is not finite")
    return format(number, "f")


def _double_text(value: Any) -> str:
    if type(value) is float:
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    return _decimal_text(value)


def _text(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("a text value is not text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("a text value is not valid Unicode") from None
    return value


def _encode_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "BOOLEAN":
        if type(value) is not bool:
            raise ValueError("a boolean value is not a boolean")
        return value
    if kind == "VARCHAR":
        return _text(value)
    if kind.startswith("DECIMAL"):
        return _decimal_text(value)
    if kind == "DOUBLE":
        return _double_text(value)
    if type(value) is not int:  # booleans are not whole numbers here
        raise ValueError("a whole-number value is not a whole number")
    return value


def _encode_tables(
    tables: Mapping[str, Sequence[ColumnValues]],
) -> Tuple[Tuple[bytes, Tuple[bytes, ...]], ...]:
    """Each table as its header and one JSON frame per column, or a ValueError (never a value)."""
    out: List[Tuple[bytes, Tuple[bytes, ...]]] = []
    for table_name in sorted(tables):
        _name(table_name, "a table name")
        columns = list(tables[table_name])
        if not columns:
            raise ValueError("a table has at least one column")
        seen = set()
        rows: Optional[int] = None
        declared: List[Dict[str, Any]] = []
        frames: List[bytes] = []
        for entry in columns:
            if len(entry) != 3:
                raise ValueError("a column is (name, type, values)")
            column, kind_text, values = entry
            _name(column, "a column name")
            if column.lower() in seen:
                raise ValueError("a table has two columns of one name")
            seen.add(column.lower())
            kind = _type(kind_text)
            if rows is None:
                rows = len(values)
            elif len(values) != rows:
                raise ValueError("the columns of a table hold as many values as it has rows")
            try:
                encoded = [_encode_value(kind, value) for value in values]
            except ValueError as error:
                raise ValueError(f"a value of {kind} column in table: {error}") from None
            chunks = [encoded[i : i + _CHUNK_ROWS] for i in range(0, len(encoded), _CHUNK_ROWS)]
            declared.append({"name": column, "type": kind, "chunks": len(chunks)})
            for chunk in chunks:
                try:
                    frames.append(_child.encode_json(chunk))
                except _child.FrameError as error:
                    raise ValueError(f"a column cannot be sent: {error}") from None
                if len(frames[-1]) > _child.COLUMN_LIMIT:
                    raise ValueError("a run of rows passes the size of one frame")
        header = _child.encode_json(
            {"op": "table", "name": table_name, "rows": rows, "columns": declared}
        )
        out.append((header, tuple(frames)))
    return tuple(out)


def _statements(items: Sequence[Union[Statement, Mapping[str, Any]]]) -> Tuple[Statement, ...]:
    found: List[Statement] = []
    seen = set()
    for item in items:
        statement = item if isinstance(item, Statement) else Statement(**dict(item))
        _name(statement.id, "a statement id")
        if statement.id in seen:
            raise ValueError("two statements have one id")
        seen.add(statement.id)
        if type(statement.sql) is not str or "\x00" in statement.sql:
            raise ValueError("a statement is text with no NUL")
        if statement.returns not in RETURNS:
            raise ValueError(f"a statement returns one of {RETURNS}")
        found.append(statement)
    return tuple(found)


# ----------------------------------------------------------------------------- the process


class _Gone(Exception):
    """The child ended, closed its pipe or broke the protocol. The text says how."""


class _Failed(Exception):
    """The child is up and will not take the tables; the status and the reason it gives."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _describe(returncode: Optional[int]) -> str:
    if returncode is None:
        return "it was still running"
    if returncode < 0:
        try:
            return f"signal {signal.Signals(-returncode).name}"
        except ValueError:
            return f"signal {-returncode}"
    return f"exit status {returncode}"


class _Child:
    """One query process and its pipes."""

    def __init__(self, command: Sequence[str]) -> None:
        self.process = subprocess.Popen(
            list(command),
            env=dict(_ENVIRONMENT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(),
            cwd="/" if sys.platform != "win32" else None,
        )

    def put(self, data: bytes, limit: int = _child.HEADER_LIMIT) -> None:
        stream = self.process.stdin
        assert stream is not None
        try:
            _child.write_frame(stream, data, limit)
        except (OSError, ValueError) as error:  # a closed pipe is a child that is gone
            raise _Gone(self._how_it_ended()) from error

    def take(self) -> Dict[str, Any]:
        stream = self.process.stdout
        assert stream is not None
        try:
            document = _child.decode_json(_child.read_frame(stream, _child.REPLY_LIMIT))
        except EOFError as error:
            raise _Gone(self._how_it_ended()) from error
        except _child.FrameError as error:
            self.end()
            raise _Gone(f"it broke the protocol: {error}") from error
        except (OSError, ValueError) as error:
            raise _Gone(self._how_it_ended()) from error
        if not isinstance(document, dict):
            self.end()
            raise _Gone("it sent a message that is not an object")
        return document

    def _how_it_ended(self) -> str:
        try:
            returncode: Optional[int] = self.process.wait(timeout=_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            returncode = None
        if returncode is None:
            self.end()
            return "it closed its pipe and was ended"
        return f"it ended with {_describe(returncode)}"

    def end(self) -> None:
        """Stop the process now: terminate, then kill. Safe to call from any thread, and twice."""
        process = self.process
        if process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_LEAVE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):  # already gone
            pass

    def stop(self) -> None:
        """Ask the process to leave, then end it if it does not, and close both pipes."""
        process = self.process
        try:
            if process.poll() is None:
                try:
                    self.put(_child.encode_json({"op": "stop"}))
                    assert process.stdin is not None
                    process.stdin.close()
                    process.wait(timeout=_LEAVE_SECONDS)
                except (_Gone, OSError, subprocess.TimeoutExpired):
                    # It left, broke its pipe or did not leave in time: end() below makes sure.
                    pass
            self.end()
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        # A pipe the process already broke has nothing left to close.
                        pass


def _clean_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Gone("it sent a reason that is not text")
    return "".join(ch if ch.isprintable() else " " for ch in value)[:_REASON_LIMIT]


def _count_of(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _Gone("it sent a count that is not a whole number")
    return value


def _groups_of(value: Any) -> Tuple[GroupRow, ...]:
    if not isinstance(value, list):
        raise _Gone("it sent groups that are not a list")
    rows: List[GroupRow] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) < 2:
            raise _Gone("it sent a group that is not keys and a count")
        keys = entry[:-1]
        if any(key is not None and type(key) not in (bool, int, str) for key in keys):
            raise _Gone("it sent a group key that is not a boolean, a whole number or text")
        rows.append((*keys, _count_of(entry[-1])))
    return tuple(rows)


def _bits_of(value: Any) -> Bits:
    if not isinstance(value, dict):
        raise _Gone("it sent bits that are not an object")
    n = _count_of(value.get("n"))
    data = value.get("data")
    if not isinstance(data, str):
        raise _Gone("it sent bits that are not text")
    try:
        packed = base64.b64decode(data.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        raise _Gone("it sent bits that are not base64") from None
    if len(packed) != (n + 7) // 8:
        raise _Gone("it sent bits of the wrong length")
    return Bits(n, packed)


def _version(text: str) -> Tuple[int, int, int]:
    from fluid_build.providers._duckdb_sandbox import duckdb_version

    return duckdb_version(SimpleNamespace(__version__=text))


def _zero_bytes(text: str) -> bool:
    digits = text.lower().replace("bytes", "").replace("b", "").strip()
    try:
        return Decimal(digits) == 0
    except InvalidOperation:
        return False


class _Run:
    """One run: a child at a time, the tables to send it, and the statements to ask."""

    def __init__(
        self,
        tables: Tuple[Tuple[bytes, Tuple[bytes, ...]], ...],
        statements: Tuple[Statement, ...],
        *,
        seconds: int,
        memory: int,
        grace: int,
        start: int,
        rows: int,
        command: Sequence[str],
    ) -> None:
        self._tables = tables
        self._statements = statements
        self._seconds = seconds
        self._memory = memory
        self._grace = grace
        self._start_seconds = start
        self._cpu = min(
            len(statements) * seconds + _CPU_MARGIN_SECONDS + rows // _ROWS_PER_CPU_SECOND,
            10_000_000,
        )
        self._command = tuple(command)
        self._child: Optional[_Child] = None
        self._expired = threading.Event()
        self.hello: Dict[str, Any] = {}

    # -- starting a child

    def _begin(self, seconds: float) -> threading.Timer:
        self._expired.clear()
        timer = threading.Timer(seconds, self._expire)
        timer.daemon = True
        timer.start()
        return timer

    def _expire(self) -> None:
        self._expired.set()
        child = self._child
        if child is not None:
            child.end()

    def stop(self) -> None:
        child, self._child = self._child, None
        if child is not None:
            child.stop()

    def start(self) -> None:
        """Start a child under the locks and send it the tables.

        ``_Gone`` when it dies, ``_Failed`` when it will not take a table, and
        :class:`ChildUnavailable` when it cannot run here or lacks a lock.
        """
        timer = self._begin(self._start_seconds)
        try:
            self._start()
        except BaseException:
            self.stop()
            raise
        finally:
            timer.cancel()

    def _start(self) -> None:
        try:
            child = _Child(self._command)
        except OSError as error:
            raise _Gone(f"it could not be started: {type(error).__name__}") from error
        self._child = child
        child.put(
            _child.encode_json(
                {
                    "op": "init",
                    "seconds": self._seconds,
                    "memoryBytes": self._memory,
                    "cpuSeconds": self._cpu,
                }
            )
        )
        self.hello = self._checked_hello(child.take())
        for header, frames in self._tables:
            child.put(header)
            for frame in frames:
                child.put(frame, _child.COLUMN_LIMIT)
            reply = child.take()
            if reply.get("op") != "ok":
                status = reply.get("status")
                raise _Failed(
                    status if status in ("error", "outOfMemory") else "error",
                    f"the tables could not be loaded: {_clean_reason(reply.get('reason'))}",
                )

    def _checked_hello(self, reply: Dict[str, Any]) -> Dict[str, Any]:
        if reply.get("op") == "missing":
            raise _unavailable(
                "The profile's query process needs DuckDB, which it could not import",
                "Consumer queries run in a DuckDB process of their own.",
                "pip install 'data-product-forge[profile]'",
            )
        if reply.get("op") != "hello":
            raise _Gone("its first message is not a hello")
        version = reply.get("duckdb")
        if not isinstance(version, str):
            raise _Gone("its hello has no DuckDB version")
        from fluid_build.providers._duckdb_sandbox import MIN_DUCKDB_VERSION

        if _version(version) < MIN_DUCKDB_VERSION:
            want = ".".join(str(n) for n in MIN_DUCKDB_VERSION)
            raise _unavailable(
                f"DuckDB {version} is too old to run consumer queries",
                f"The locks the query process relies on need DuckDB {want} or newer.",
                f"pip install 'duckdb>={want}'",
            )
        if reply.get("settings") != _child.connect_settings(self._memory):
            raise _unavailable(
                "The query process did not connect with the settings it was asked for",
                "A lock that is not confirmed is a lock that is not held.",
                "Reinstall the profile extra: pip install --force-reinstall 'data-product-forge[profile]'",
            )
        self._confirm_locks(reply.get("readBack"))
        if not isinstance(reply.get("limits"), dict):
            raise _Gone("its hello does not report its limits")
        environment = reply.get("environment")
        if not isinstance(environment, list) or not all(isinstance(k, str) for k in environment):
            raise _Gone("its hello does not list its environment")
        return reply

    @staticmethod
    def _confirm_locks(read_back: Any) -> None:
        expected = _child.settings_read_back()
        ok = isinstance(read_back, dict) and set(read_back) == set(expected)
        if ok:
            for key, want in expected.items():
                got = str(read_back[key]).lower()
                if key == "max_temp_directory_size":
                    ok = ok and _zero_bytes(got)
                else:
                    ok = ok and got == want
        if not ok:
            raise _unavailable(
                "DuckDB did not confirm the locks of the query process",
                "Its settings read back differently from the ones it connected with.",
                "Use a DuckDB release the profile extra supports: "
                "pip install --upgrade 'data-product-forge[profile]'",
            )

    # -- a statement

    def ask(self, statement: Statement) -> StatementResult:
        """Run one statement. ``_Gone`` when the child dies or passes its clock."""
        child = self._child
        if child is None:
            raise _Gone("there is no query process")
        timer = self._begin(self._seconds + self._grace)
        try:
            child.put(
                _child.encode_json(
                    {
                        "op": "run",
                        "id": statement.id,
                        "sql": statement.sql,
                        "returns": statement.returns,
                    }
                )
            )
            reply = child.take()
            return self._answer(statement, reply)
        except _Gone as gone:
            if self._expired.is_set():
                raise _Gone("it did not answer in time and was ended") from gone
            raise
        finally:
            timer.cancel()

    def _answer(self, statement: Statement, reply: Dict[str, Any]) -> StatementResult:
        if reply.get("op") == "refused":
            return StatementResult(statement.id, "error", _clean_reason(reply.get("reason")))
        if reply.get("op") != "result" or reply.get("id") != statement.id:
            raise _Gone("it answered another statement")
        status = reply.get("status")
        if status not in STATUSES[:4]:
            raise _Gone("it sent a status that is not one it has")
        reason = _clean_reason(reply.get("reason"))
        if status != "ok":
            return StatementResult(statement.id, str(status), reason)
        count, groups, bits = reply.get("count"), reply.get("groups"), reply.get("bits")
        wants = statement.returns
        if (count is None) != (wants != "count") or (groups is None) != (wants != "counts"):
            raise _Gone("it answered with something the statement did not return")
        if (bits is None) != (wants != "bits"):
            raise _Gone("it answered with something the statement did not return")
        return StatementResult(
            statement.id,
            "ok",
            None,
            _count_of(count) if wants == "count" else None,
            _groups_of(groups) if wants == "counts" else None,
            _bits_of(bits) if wants == "bits" else None,
        )


def _failed(statement: Statement, status: str, reason: str) -> StatementResult:
    return StatementResult(statement.id, status, reason)


def run_in_child(
    tables: Mapping[str, Sequence[ColumnValues]],
    statements: Sequence[Union[Statement, Mapping[str, Any]]],
    *,
    seconds: int,
    memory: int,
    grace_seconds: int = GRACE_SECONDS,
    start_seconds: int = START_SECONDS,
    _command: Optional[Sequence[str]] = None,
) -> ChildRun:
    """Run *statements* over *tables* in a locked DuckDB process of their own; stop it.

    *tables* maps each table name (an exposeId) to its ordered columns, ``(name, duckdb_type,
    values)``: ``duckdb_type`` one of ``BOOLEAN``, ``TINYINT``, ``SMALLINT``, ``INTEGER``,
    ``BIGINT``, ``HUGEINT``, ``DOUBLE``, ``VARCHAR``, ``DATE``, ``TIMESTAMP`` or
    ``DECIMAL(p,s)`` (see :func:`~._types.duckdb_type`), and ``values`` the column's Python values
    as the profiler reads them: ints, booleans, strings, ``Decimal`` or decimal text for a
    ``DECIMAL`` (loaded exactly), epoch milliseconds as an int for ``DATE`` and ``TIMESTAMP``,
    and ``None``. *statements* are :class:`Statement` objects (or mappings of ``id``, ``sql`` and
    ``returns``); *seconds* is the time limit of each and *memory* the child's budget in bytes.

    The result holds one :class:`StatementResult` per statement, in order. A table that cannot
    be loaded (DuckDB refuses it, or it passes the memory budget) gives every statement that
    status (``error`` or ``outOfMemory``); a child that cannot be started again gives the
    statements that remain ``crashed``.

    Raises :class:`ValueError` for an argument that is not as described (never quoting a value),
    and :class:`ChildUnavailable` when DuckDB is missing, too old, or does not confirm its locks.
    """
    items = _statements(statements)
    _whole(seconds, 1, 86_400, "seconds")
    _whole(memory, 1 << 20, 1 << 44, "memory")
    _whole(grace_seconds, 1, 86_400, "grace_seconds")
    _whole(start_seconds, 1, 86_400, "start_seconds")
    encoded = _encode_tables(tables)
    rows = sum(len(columns[0][2]) for columns in map(list, tables.values()) if columns)
    run = _Run(
        encoded,
        items,
        seconds=seconds,
        memory=memory,
        grace=grace_seconds,
        start=start_seconds,
        rows=rows,
        command=_command or (sys.executable, "-I", _CHILD_FILE),
    )
    results: List[StatementResult] = []
    restarts = 0
    try:
        position = 0
        while position < len(items):
            if run._child is None:
                try:
                    run.start()
                except _Gone as gone:
                    reason = f"the query process did not start: {gone}"
                    timed_out = run._expired.is_set()
                    status = "timedOut" if timed_out else "crashed"
                    results.extend(_failed(item, status, reason) for item in items[position:])
                    break
                except _Failed as failure:
                    results.extend(
                        _failed(item, failure.status, failure.reason) for item in items[position:]
                    )
                    break
                if results:
                    restarts += 1
            statement = items[position]
            position += 1
            try:
                results.append(run.ask(statement))
            except _Gone as gone:
                timed_out = run._expired.is_set()
                reason = f"the query process failed: {gone}"
                results.append(_failed(statement, "timedOut" if timed_out else "crashed", reason))
                run.stop()
        return ChildRun(
            results=tuple(results),
            duckdb_version=str(run.hello.get("duckdb", "")),
            limits=dict(run.hello.get("limits") or {}),
            environment=tuple(run.hello.get("environment") or ()),
            restarts=restarts,
        )
    finally:
        run.stop()
