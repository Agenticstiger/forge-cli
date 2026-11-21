# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a PySpark job reads, writes and streams, read from its source.

:func:`read_job` takes one parsed module and returns a :class:`JobFacts`: the tables, paths and
Kafka topics it reads and writes, and for a Structured Streaming query its checkpoint
location, trigger and query name. Nothing runs; a name that is not a constant is reported as
unresolved with the reason, never guessed.

How a call is recognised. Spark's API is a fluent chain, so the reader works on chains: from a
call such as ``.load(...)`` it walks down to the last ``.read``, ``.readStream``, ``.write``,
``.writeStream`` or ``.writeTo(...)`` and reads the links between (``format``, ``option``,
``options``, ``mode``, ``trigger`` ...). A chain started from a variable (``reader =
spark.read.format("kafka")`` then ``reader.load()``) is followed through that variable when it
is assigned once. The matching is by method name, as the Databricks Labs UCX linters match
Spark calls with a table of ``(method, argument position or name, read or write)``; the table
here is :data:`_TERMINALS`, with the spellings checked against Spark 3.5 and 4.0
(``pyspark/sql/readwriter.py``, ``pyspark/sql/streaming/readwriter.py``). UCX is under the
Databricks License, so only the idea is used, not its code.

What it reads, in short:

* batch: ``spark.table``, ``spark.read.table|load|parquet|csv|json|orc|text``, ``df.write.
  saveAsTable|insertInto|save|parquet|csv|json|orc|text``, ``df.writeTo(t).append()`` and the
  other DataFrameWriterV2 terminals;
* streaming: ``readStream...load|table``, ``writeStream...start|toTable|foreachBatch``, with
  ``checkpointLocation`` (option, ``start`` keyword, or the session conf), ``trigger``,
  ``queryName`` and ``outputMode``;
* Kafka: ``subscribe``, ``subscribePattern``, ``assign`` on a source, ``topic`` on a sink,
  and the bootstrap servers; no other option is read (``kafka.sasl.*`` carries credentials);
* SQL inside ``spark.sql(...)`` (:mod:`._spark_sql`), with f-string fields that do not fold
  read as holes, so one unknown name does not hide the rest of the statement;
* helper functions the operator names in the conventions (``read_table(name)``), and
  temporary views registered in the file, which are not tables.

A name that comes from a function parameter is not reported one by one: it is the sign of a
helper (a function that takes the table name), and the file gets one note saying so, with the
way to name the helper.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from . import _spark_sql
from ._fold import FoldedDict, Folder, Unresolved
from ._pysource import Module, qualified_name, snippet

#: Receivers whose ``.table(...)`` and ``.sql(...)`` are Spark's. The conventions add names; a
#: name assigned from ``....getOrCreate()`` in the file is added as it is met.
DEFAULT_SESSION_NAMES = frozenset({"spark", "session", "spark_session", "sparkSession", "ss"})

#: Formats whose writes and reads are not data we keep: nothing persistent is behind them.
_NON_PERSISTENT = frozenset({"console", "memory", "noop", "foreach", "rate", "socket"})
#: The file readers and writers named by the method (``spark.read.parquet(path)``).
_FILE_FORMATS = frozenset({"parquet", "csv", "json", "orc", "text", "xml", "avro"})
_PARAMETER = "is a parameter of the function"
_TEMP_VIEW_METHODS = frozenset(
    {
        "createOrReplaceTempView",
        "createTempView",
        "createGlobalTempView",
        "createOrReplaceGlobalTempView",
        "registerTempTable",
    }
)
_READER_MARKERS = frozenset({"read", "readStream"})
_WRITER_MARKERS = frozenset({"write", "writeStream"})
_V2_TERMINALS = {
    "append": "append",
    "create": "create",
    "replace": "replace",
    "createOrReplace": "overwrite",
    "overwrite": "overwrite",
    "overwritePartitions": "overwrite",
}
#: The terminal call of a chain, by method name: ``(position of the name argument, keyword)``
#: of the table, path or topic. Only the terminals the Spark docs list.
_TERMINALS = frozenset(
    {"load", "table", "save", "saveAsTable", "insertInto", "start", "toTable", "jdbc"}
    | _FILE_FORMATS
    | set(_V2_TERMINALS)
)

_PLAIN_TABLE = re.compile(r"[A-Za-z0-9_$`.\-]+")

#: A placeholder a templating layer fills in later, in SQL that is not an f-string: ``${db}``,
#: ``{db}``, ``{{ db }}``, ``%(db)s``. The SQL parser would read the text around it as a name.
_PLACEHOLDER = re.compile(
    r"\$\{[^}\s]*\}|\{\{[^}]*\}\}|\{[A-Za-z_][A-Za-z0-9_.]*\}|%\([A-Za-z_][A-Za-z0-9_]*\)s"
)


def _placeholders_to_holes(text: str, holes: Dict[str, str]) -> str:
    """``text`` with each placeholder replaced by a hole token (as an f-string field that does not
    fold is), so the name around it is reported as unresolved, not read as another table."""

    def token(match: "re.Match[str]") -> str:
        name = _spark_sql.HOLE.format(len(holes))
        holes[name] = match.group(0)
        return name

    return _PLACEHOLDER.sub(token, text)


@dataclass(frozen=True)
class HelperSpec:
    """A project function the operator declared as Spark IO, in the conventions:
    ``read_table: {reads: table, arg: 0}``."""

    direction: str  # "reads" | "writes"
    kind: str  # "table" | "path" | "topic"
    arg: Union[int, str]  # the argument's position, or its keyword
    fmt: Optional[str] = None


@dataclass(frozen=True)
class Ref:
    """A table, path or topic a job touches, found at ``file:line``."""

    kind: str  # "table" | "path" | "topic" | "topic_pattern"
    #: A table: lower-case ``(catalog?, database?, table)``. A path, topic or pattern: the text.
    #: Empty when the name did not resolve.
    parts: Tuple[str, ...]
    #: The name as a person reads it; for a name that did not resolve, its template or code.
    shown: str
    #: Why the name did not resolve; empty when it did.
    why: str
    file: str
    line: int
    fmt: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return not self.why

    @property
    def name(self) -> str:
        return (
            ".".join(self.parts) if self.kind == "table" else (self.parts[0] if self.parts else "")
        )


@dataclass(frozen=True)
class Read(Ref):
    streaming: bool = False
    #: Kafka bootstrap servers, when given as a constant.
    host: Optional[str] = None


@dataclass(frozen=True)
class Write(Ref):
    #: ``append``, ``overwrite``, ``create``, ``merge``, ... as the code says it.
    mode: Optional[str] = None
    streaming: bool = False
    checkpoint: Optional[str] = None
    trigger: Optional[str] = None
    query_name: Optional[str] = None
    output_mode: Optional[str] = None
    host: Optional[str] = None


@dataclass(frozen=True)
class Stream:
    """One Structured Streaming query, as its ``writeStream`` chain states it."""

    file: str
    line: int
    via_foreach_batch: bool
    checkpoint: Optional[str]
    trigger: Optional[str]
    query_name: Optional[str]
    output_mode: Optional[str]
    #: First and last line of the ``foreachBatch`` function, when the sink is one and it was found.
    batch_function: Optional[Tuple[int, int]] = None


@dataclass
class JobFacts:
    """What one file does with Spark."""

    rel: str
    reads: List[Read] = field(default_factory=list)
    writes: List[Write] = field(default_factory=list)
    streams: List[Stream] = field(default_factory=list)
    #: Job-level lines for the report: SQL that did not parse, JDBC, names from parameters.
    notes: List[str] = field(default_factory=list)
    #: The file imports pyspark or builds a session.
    is_spark: bool = False

    @property
    def streaming(self) -> bool:
        return (
            bool(self.streams)
            or any(r.streaming for r in self.reads)
            or any(w.streaming for w in self.writes)
        )

    @property
    def has_io(self) -> bool:
        return bool(self.reads or self.writes or self.streams)


@dataclass
class _Link:
    name: str
    call: Optional[ast.Call]  # None for a plain attribute (``.read``, ``.write``)
    node: ast.AST


def split_table_name(text: str) -> List[str]:
    """The parts of ``catalog.db.table``, honouring backtick quoting (``\\`my db\\`.t``)."""
    parts: List[str] = []
    current: List[str] = []
    quoted = False
    for char in text.strip():
        if char == "`":
            quoted = not quoted
        elif char == "." and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _normalise_table(value: str) -> Tuple[Tuple[str, ...], str]:
    parts = tuple(p.lower() for p in split_table_name(value))
    return parts, value.strip()


class _Reader:
    def __init__(
        self,
        module: Module,
        folder: Folder,
        helpers: Mapping[str, HelperSpec],
        session_names: Iterable[str],
        shown: Optional[str] = None,
    ) -> None:
        self.module = module
        self.folder = folder
        self.helpers = helpers
        self.sessions: Set[str] = set(session_names)
        self.file = shown or module.rel
        self.facts = JobFacts(rel=module.rel)
        self.temp_views: Set[str] = set()
        self.default_checkpoint: Optional[str] = None
        self.parameter_named = 0
        self._handled: Set[int] = set()

    # ── entry ───────────────────────────────────────────────────────

    def run(self) -> JobFacts:
        tree = self.module.tree
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        calls.sort(key=lambda n: (n.lineno, n.col_offset))
        self.facts.is_spark = any(
            q.split(".")[0] == "pyspark" for q in self.module.imports.values()
        )
        self._preliminaries(calls)
        for call in calls:
            self._on_call(call)
        self._finish()
        return self.facts

    def _preliminaries(self, calls: Sequence[ast.Call]) -> None:
        for call in calls:
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "getOrCreate":
                self.facts.is_spark = True
                for parent in self._assignment_targets(call):
                    self.sessions.add(parent)
            elif func.attr in _TEMP_VIEW_METHODS and call.args:
                name = self.folder.string(call.args[0])
                if isinstance(name, str):
                    self.temp_views.add(name.lower())
                else:
                    self.facts.notes.append(
                        f"{self.file}:{call.lineno} registers a temporary view under a name that "
                        "is not a constant: a read of a table this file does not name may be "
                        "that view"
                    )
            elif func.attr in {"config", "set"} and len(call.args) >= 2:
                key = self.folder.string(call.args[0])
                if key == "spark.sql.streaming.checkpointLocation":
                    value = self.folder.string(call.args[1])
                    if isinstance(value, str):
                        self.default_checkpoint = value

    def _assignment_targets(self, call: ast.Call) -> List[str]:
        """Names a chain ending in ``call`` is assigned to (``spark = ....getOrCreate()``)."""
        node: ast.AST = call
        parents = self.module.parents
        while node in parents and not isinstance(parents[node], (ast.Assign, ast.AnnAssign)):
            if isinstance(parents[node], (ast.stmt,)):
                return []
            node = parents[node]
        assign = parents.get(node)
        if isinstance(assign, ast.Assign):
            return [t.id for t in assign.targets if isinstance(t, ast.Name)]
        if isinstance(assign, ast.AnnAssign) and isinstance(assign.target, ast.Name):
            return [assign.target.id]
        return []

    # ── chains ──────────────────────────────────────────────────────

    def _links(self, node: ast.AST, depth: int = 0) -> Tuple[List[_Link], ast.AST]:
        """The method and attribute links of a fluent expression, root first, and the root
        expression. A root variable assigned once to another chain is followed (three levels)."""
        links: List[_Link] = []
        current = node
        while True:
            if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
                links.append(_Link(current.func.attr, current, current))
                current = current.func.value
            elif isinstance(current, ast.Attribute):
                links.append(_Link(current.attr, None, current))
                current = current.value
            else:
                break
        links.reverse()
        if isinstance(current, ast.Name) and depth < 3:
            bound = self._bound_expression(current)
            if bound is not None and not any(
                link.name in _READER_MARKERS | _WRITER_MARKERS | {"writeTo"} for link in links
            ):
                earlier, root = self._links(bound, depth + 1)
                return earlier + links, root
        return links, current

    def _bound_expression(self, name: ast.Name) -> Optional[ast.expr]:
        scope = self.folder.scope_of(name)
        current: Optional[Any] = scope
        while current is not None:
            if name.id in current.values:
                return current.values[name.id]
            if name.id in current.blocked or name.id in current.classes:
                return None
            current = current.parent
        return None

    @staticmethod
    def _marker(links: Sequence[_Link]) -> Optional[int]:
        """Index of the last read, write or writeTo link: where this chain's own part starts."""
        for index in range(len(links) - 1, -1, -1):
            link = links[index]
            if link.call is None and link.name in _READER_MARKERS | _WRITER_MARKERS:
                return index
            if link.call is not None and link.name == "writeTo":
                return index
        return None

    # ── dispatch ────────────────────────────────────────────────────

    def _on_call(self, call: ast.Call) -> None:
        func = call.func
        helper = self._helper_for(call)
        if helper is not None:
            self._on_helper(call, helper)
            return
        if not isinstance(func, ast.Attribute):
            return
        attr = func.attr
        if attr == "sql" and self._on_session(func.value):
            self._on_sql(call)
            return
        if attr == "table" and self._on_session(func.value):
            self._add_read(
                self._table_ref(self._arg(call, 0, "tableName"), call, None), streaming=False
            )
            return
        if attr not in _TERMINALS:
            return
        links, root = self._links(call)
        marker = self._marker(links)
        if marker is None:
            self._note_unfollowed(call, root, attr)
            return
        own = links[marker + 1 :]
        start = links[marker]
        if start.call is None and start.name in _READER_MARKERS:
            self._on_read(call, own, streaming=start.name == "readStream")
        elif start.call is None and start.name in _WRITER_MARKERS:
            self._on_write(call, own, streaming=start.name == "writeStream", start=start)
        elif start.call is not None and start.name == "writeTo":
            self._on_write_v2(call, own, start)

    def _note_unfollowed(self, call: ast.Call, root: ast.AST, attr: str) -> None:
        """A read or write made through a variable that is assigned more than once cannot be
        followed (which assignment is meant depends on the order the code runs in): say so."""
        if not (isinstance(root, ast.Name) and self.facts.is_spark):
            return
        scope = self.folder.scope_of(root)
        current: Optional[Any] = scope
        while current is not None:
            why = current.blocked.get(root.id)
            if why is not None:
                if "assigned more than once" in why:
                    note = (
                        f"{self.file}:{call.lineno} .{attr}(...) is called on "
                        f"`{root.id}`, which is assigned more than once: the read or write it "
                        "makes is not followed"
                    )
                    if note not in self.facts.notes:
                        self.facts.notes.append(note)
                return
            if root.id in current.values or root.id in current.classes:
                return
            current = current.parent

    def _on_session(self, receiver: ast.AST) -> bool:
        name = None
        if isinstance(receiver, ast.Name):
            name = receiver.id
        elif isinstance(receiver, ast.Attribute):
            name = receiver.attr
        return name in self.sessions

    # ── argument helpers ────────────────────────────────────────────

    @staticmethod
    def _arg(call: ast.Call, position: Optional[int], keyword: Optional[str]) -> Optional[ast.expr]:
        if position is not None and len(call.args) > position:
            candidate = call.args[position]
            if not any(isinstance(a, ast.Starred) for a in call.args[: position + 1]):
                return candidate
            return None
        if keyword is not None:
            for kw in call.keywords:
                if kw.arg == keyword:
                    return kw.value
        return None

    def _text(self, node: Optional[ast.expr]) -> Union[str, Unresolved, None]:
        if node is None:
            return None
        return self.folder.string(node)

    def _literal(self, node: Optional[ast.expr]) -> Optional[str]:
        value = self._text(node)
        return value if isinstance(value, str) else None

    def _options(self, links: Sequence[_Link]) -> Tuple[Dict[str, Any], Optional[str]]:
        """``({option key: folded value}, format)`` from the ``option``, ``options`` and
        ``format`` links. Keys are lower-cased (Spark reads option names case-insensitively)."""
        options: Dict[str, Any] = {}
        fmt: Optional[str] = None
        for link in links:
            call = link.call
            if call is None:
                continue
            if link.name == "format" and call.args:
                fmt = (self._literal(call.args[0]) or "").lower() or fmt
            elif link.name == "option" and len(call.args) >= 2:
                key = self._literal(call.args[0])
                if key is not None:
                    options[key.lower()] = self.folder.fold(call.args[1])
            elif link.name == "options":
                for kw in call.keywords:
                    if kw.arg is not None:
                        options[kw.arg.lower()] = self.folder.fold(kw.value)
                    else:
                        spread = self.folder.fold(kw.value)
                        if isinstance(spread, FoldedDict):
                            for key, value in spread.items():
                                if isinstance(key, str):
                                    options[key.lower()] = value
        return options, fmt

    def _ref(
        self,
        kind: str,
        value: Union[str, Unresolved, None],
        node: ast.AST,
        fmt: Optional[str],
        missing: str = "no name is given",
    ) -> Ref:
        line = getattr(node, "lineno", 0)
        rel = self.file
        if value is None:
            return Ref(kind, (), "", missing, rel, line, fmt)
        if isinstance(value, Unresolved):
            return Ref(kind, (), value.text, value.why, rel, line, fmt)
        text = value.strip()
        if not text:
            return Ref(kind, (), "", "the name is empty", rel, line, fmt)
        if any(mark in text for mark in ("{", "}", "%(")):
            return Ref(kind, (), text, "the name holds a placeholder", rel, line, fmt)
        if kind == "table":
            parts, shown = _normalise_table(text)
            if not parts or "/" in text or ":" in text:
                return Ref(kind, (), text, "the name is not a table name", rel, line, fmt)
            return Ref(kind, parts, shown, "", rel, line, fmt)
        return Ref(kind, (text,), text, "", rel, line, fmt)

    def _table_ref(self, node: Optional[ast.expr], at: ast.AST, fmt: Optional[str]) -> Ref:
        return self._ref("table", self._text(node), at, fmt)

    def _path_or_table(self, node: Optional[ast.expr], at: ast.AST, fmt: Optional[str]) -> Ref:
        """The target of ``load``/``save``: for Iceberg a bare name is a table identifier (the
        Iceberg docs write ``save("glue.db.t")``); anything with a slash or a scheme is a path."""
        value = self._text(node)
        if (
            fmt == "iceberg"
            and isinstance(value, str)
            and _PLAIN_TABLE.fullmatch(value.strip() or "!")
            and "/" not in value
        ):
            return self._ref("table", value, at, fmt)
        return self._ref("path", value, at, fmt, missing="no path is given")

    # ── records ─────────────────────────────────────────────────────

    def _add_read(self, ref: Ref, *, streaming: bool, host: Optional[str] = None) -> None:
        if not ref.resolved and _PARAMETER in ref.why:
            self.parameter_named += 1
            return
        if ref.kind == "table" and ref.resolved and self._is_temp_view(ref):
            return
        self.facts.reads.append(Read(**_fields(ref), streaming=streaming, host=host))

    def _add_write(self, ref: Ref, **extra: Any) -> None:
        if not ref.resolved and _PARAMETER in ref.why:
            self.parameter_named += 1
            return
        self.facts.writes.append(Write(**_fields(ref), **extra))

    def _is_temp_view(self, ref: Ref) -> bool:
        if len(ref.parts) == 1:
            return ref.parts[0] in self.temp_views
        return len(ref.parts) >= 2 and ref.parts[-2] == "global_temp"

    # ── helpers declared in the conventions ─────────────────────────

    def _helper_for(self, call: ast.Call) -> Optional[HelperSpec]:
        if not self.helpers:
            return None
        qualified = qualified_name(call.func, self.module.imports)
        if qualified is None:
            return None
        return self.helpers.get(qualified.rsplit(".", 1)[-1])

    def _on_helper(self, call: ast.Call, spec: HelperSpec) -> None:
        position = spec.arg if isinstance(spec.arg, int) else None
        keyword = spec.arg if isinstance(spec.arg, str) else None
        node = self._arg(call, position, keyword)
        ref = self._ref(
            spec.kind, self._text(node), call, spec.fmt, missing="the argument is not given"
        )
        if spec.direction == "reads":
            self._add_read(ref, streaming=False)
        else:
            self._add_write(ref)

    # ── SQL ─────────────────────────────────────────────────────────

    def _on_sql(self, call: ast.Call) -> None:
        node = self._arg(call, 0, "sqlQuery")
        if node is None:
            return
        value, holes = self.folder.fold_sql(node)
        if isinstance(value, Unresolved):
            if _PARAMETER in value.why:
                self.parameter_named += 1
            else:
                self.facts.notes.append(
                    f"{self.file}:{call.lineno} spark.sql(...) takes SQL that is not a "
                    f"constant ({value.why})"
                )
            return
        sql = _spark_sql.analyze(_placeholders_to_holes(value, holes), holes)
        self.temp_views.update(sql.temp_views)
        for note in sql.notes:
            self.facts.notes.append(f"{self.file}:{call.lineno} {note}")
        for name in sql.reads:
            ref = self._from_sql_name(name, call)
            self._add_read(ref, streaming=False)
        for write in sql.writes:
            ref = self._from_sql_name(write.name, call)
            self._add_write(ref, mode=write.mode)

    def _from_sql_name(self, name: _spark_sql.SqlName, call: ast.Call) -> Ref:
        rel, line = self.file, call.lineno
        if name.why:
            return Ref(name.kind, (), name.shown, name.why, rel, line, name.fmt)
        return Ref(name.kind, name.parts, name.shown, "", rel, line, name.fmt)

    # ── reads ───────────────────────────────────────────────────────

    def _on_read(self, call: ast.Call, own: Sequence[_Link], *, streaming: bool) -> None:
        terminal = own[-1] if own else None
        if terminal is None or terminal.call is not call:
            return
        options, fmt = self._options(own[:-1])
        name = terminal.name
        if name == "table":
            ref = self._table_ref(self._arg(call, 0, "tableName"), call, fmt)
            self._add_read(ref, streaming=streaming)
        elif name == "load":
            self._on_load(call, options, fmt, streaming)
        elif name in _FILE_FORMATS:
            for node in call.args:
                if isinstance(node, ast.Starred):
                    self._add_read(
                        self._ref(
                            "path", Unresolved("paths given with *", snippet(node)), call, name
                        ),
                        streaming=streaming,
                    )
                    continue
                value = self.folder.fold(node)
                items = value if isinstance(value, list) else [value]
                for item in items:
                    self._add_read(
                        self._ref(
                            "path",
                            item if isinstance(item, (str, Unresolved)) else None,
                            call,
                            name,
                        ),
                        streaming=streaming,
                    )
            keyword_path = self._arg(call, None, "path")
            if keyword_path is not None:
                self._add_read(
                    self._ref("path", self._text(keyword_path), call, name), streaming=streaming
                )
        elif name == "jdbc":
            self.facts.notes.append(
                f"{self.file}:{call.lineno} reads a JDBC table: declare the source by hand"
            )

    def _on_load(
        self, call: ast.Call, options: Dict[str, Any], fmt: Optional[str], streaming: bool
    ) -> None:
        fmt_arg = self._literal(self._arg(call, 1, "format"))
        fmt = (fmt_arg or fmt or "").lower() or None
        if fmt == "kafka":
            self._kafka_reads(call, options, streaming)
            return
        if fmt in _NON_PERSISTENT:
            return
        if fmt == "jdbc":
            self.facts.notes.append(
                f"{self.file}:{call.lineno} reads a JDBC table: declare the source by hand"
            )
            return
        node = self._arg(call, 0, "path")
        if node is None and "path" in options:
            value = options["path"]
            ref = self._path_or_table_value(value, call, fmt)
        else:
            ref = self._path_or_table(node, call, fmt)
        self._add_read(ref, streaming=streaming)

    def _path_or_table_value(self, value: Any, at: ast.AST, fmt: Optional[str]) -> Ref:
        if isinstance(value, (str, Unresolved)):
            if (
                fmt == "iceberg"
                and isinstance(value, str)
                and "/" not in value
                and _PLAIN_TABLE.fullmatch(value.strip() or "!")
            ):
                return self._ref("table", value, at, fmt)
            return self._ref("path", value, at, fmt)
        return self._ref("path", None, at, fmt)

    def _kafka_reads(self, call: ast.Call, options: Dict[str, Any], streaming: bool) -> None:
        host = self._host(options)
        found = False
        subscribe = options.get("subscribe")
        if subscribe is not None:
            found = True
            if isinstance(subscribe, str):
                for topic in (t.strip() for t in subscribe.split(",")):
                    if topic:
                        self._add_read(
                            self._ref("topic", topic, call, "kafka"), streaming=streaming, host=host
                        )
            else:
                self._add_read(
                    self._ref(
                        "topic",
                        subscribe if isinstance(subscribe, Unresolved) else None,
                        call,
                        "kafka",
                    ),
                    streaming=streaming,
                    host=host,
                )
        pattern = options.get("subscribepattern")
        if pattern is not None:
            found = True
            self._add_read(
                self._ref(
                    "topic_pattern",
                    pattern if isinstance(pattern, (str, Unresolved)) else None,
                    call,
                    "kafka",
                ),
                streaming=streaming,
                host=host,
            )
        assign = options.get("assign")
        if assign is not None:
            found = True
            topics = _assign_topics(assign)
            if topics is None:
                self._add_read(
                    self._ref(
                        "topic",
                        Unresolved("the assigned partitions are not a constant", ""),
                        call,
                        "kafka",
                    ),
                    streaming=streaming,
                    host=host,
                )
            for topic in topics or []:
                self._add_read(
                    self._ref("topic", topic, call, "kafka"), streaming=streaming, host=host
                )
        if not found:
            self._add_read(
                self._ref(
                    "topic",
                    None,
                    call,
                    "kafka",
                    missing="no subscribe, subscribePattern or assign option",
                ),
                streaming=streaming,
                host=host,
            )

    @staticmethod
    def _host(options: Mapping[str, Any]) -> Optional[str]:
        value = options.get("kafka.bootstrap.servers")
        if isinstance(value, str) and value.strip() and "@" not in value and "{" not in value:
            return value.strip()
        return None

    # ── writes ──────────────────────────────────────────────────────

    def _on_write(
        self, call: ast.Call, own: Sequence[_Link], *, streaming: bool, start: _Link
    ) -> None:
        terminal = own[-1] if own else None
        if terminal is None or terminal.call is not call:
            return
        options, fmt = self._options(own[:-1])
        mode = None
        trigger = None
        query_name = None
        output_mode = None
        foreach: Optional[ast.expr] = None
        for link in own[:-1]:
            if link.call is None:
                continue
            args = link.call.args
            if link.name == "mode" and args:
                mode = (self._literal(args[0]) or "").lower() or mode
            elif link.name == "outputMode" and args:
                output_mode = (self._literal(args[0]) or "").lower() or output_mode
            elif link.name == "queryName" and args:
                query_name = self._literal(args[0]) or query_name
            elif link.name == "trigger":
                trigger = self._trigger(link.call) or trigger
            elif link.name == "foreachBatch" and args:
                foreach = args[0]
            elif link.name == "foreach":
                fmt = fmt or "foreach"
        name = terminal.name
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        checkpoint = self._checkpoint(options, kwargs)
        if name in {"start", "toTable"}:
            output_mode = (
                self._literal(self._arg(call, 2, "outputMode")) or output_mode or ""
            ) or None
            query_name = self._literal(self._arg(call, 4, "queryName")) or query_name
        extra = {
            "mode": mode or output_mode,
            "streaming": streaming,
            "checkpoint": checkpoint if streaming else None,
            "trigger": trigger if streaming else None,
            "query_name": query_name if streaming else None,
            "output_mode": output_mode if streaming else None,
        }
        fmt_arg = None
        if name in {"save", "saveAsTable", "start", "toTable"}:
            fmt_arg = self._literal(self._arg(call, 1, "format"))
        fmt = (fmt_arg or fmt or "").lower() or None
        if foreach is not None:
            self._foreach_batch(call, foreach, extra)
            return
        if fmt in _NON_PERSISTENT:
            return
        if fmt == "jdbc" or name == "jdbc":
            self.facts.notes.append(
                f"{self.file}:{call.lineno} writes a JDBC table: declare the target by hand"
            )
            return
        if name in {"saveAsTable", "toTable"}:
            ref = self._table_ref(
                self._arg(call, 0, "name" if name == "saveAsTable" else "tableName"), call, fmt
            )
            if name == "saveAsTable":
                extra["mode"] = (
                    self._literal(self._arg(call, 2, "mode")) or mode or ""
                ).lower() or None
            self._add_write(ref, **extra)
        elif name == "insertInto":
            overwrite = self._arg(call, 1, "overwrite")
            overwritten = isinstance(overwrite, ast.Constant) and overwrite.value is True
            extra["mode"] = "overwrite" if overwritten else "append"
            self._add_write(self._table_ref(self._arg(call, 0, "tableName"), call, fmt), **extra)
        elif name in {"save", "start"}:
            if fmt == "kafka":
                self._kafka_write(call, options, extra)
                return
            node = self._arg(call, 0, "path")
            if node is None and "path" in options:
                ref = self._path_or_table_value(options["path"], call, fmt)
            else:
                ref = self._path_or_table(node, call, fmt)
            if name == "save":
                extra["mode"] = (
                    self._literal(self._arg(call, 2, "mode")) or mode or ""
                ).lower() or None
            self._add_write(ref, **extra)
        elif name in _FILE_FORMATS:
            node = self._arg(call, 0, "path")
            # text() has no mode argument: its second positional is the compression.
            mode_at = None if name == "text" else 1
            extra["mode"] = (
                self._literal(self._arg(call, mode_at, "mode")) or mode or ""
            ).lower() or None
            self._add_write(self._ref("path", self._text(node), call, name), **extra)

    def _on_write_v2(self, call: ast.Call, own: Sequence[_Link], start: _Link) -> None:
        terminal = own[-1] if own else None
        if terminal is None or terminal.call is not call or terminal.name not in _V2_TERMINALS:
            return
        assert start.call is not None
        fmt = None
        for link in own[:-1]:
            if link.name == "using" and link.call is not None and link.call.args:
                fmt = (self._literal(link.call.args[0]) or "").lower() or None
        ref = self._table_ref(self._arg(start.call, 0, "table"), start.call, fmt)
        self._add_write(ref, mode=_V2_TERMINALS[terminal.name])

    def _kafka_write(self, call: ast.Call, options: Dict[str, Any], extra: Dict[str, Any]) -> None:
        topic = options.get("topic")
        host = self._host(options)
        if isinstance(topic, str):
            ref = self._ref("topic", topic, call, "kafka")
        elif isinstance(topic, Unresolved):
            ref = self._ref("topic", topic, call, "kafka")
        else:
            ref = self._ref(
                "topic",
                None,
                call,
                "kafka",
                missing="the topic is not set as an option (it may come from a column)",
            )
        self._add_write(ref, host=host, **extra)

    def _checkpoint(self, options: Dict[str, Any], kwargs: Dict[str, ast.expr]) -> Optional[str]:
        value: Any = options.get("checkpointlocation")
        for key, node in kwargs.items():
            if key.lower() == "checkpointlocation":
                value = self.folder.fold(node)
        if value is None:
            return self.default_checkpoint
        if isinstance(value, str):
            return value
        return f"(not a constant: {value.why})" if isinstance(value, Unresolved) else None

    def _trigger(self, call: ast.Call) -> Optional[str]:
        """``processingTime=1 minute``, ``availableNow``, ``once``, ``continuous=1 second``."""
        for kw in call.keywords:
            if kw.arg in {"processingTime", "continuous"}:
                value = self.folder.fold(kw.value)
                shown = (
                    value
                    if isinstance(value, str)
                    else (value.text if isinstance(value, Unresolved) else "?")
                )
                return f"{kw.arg}={shown}"
            if kw.arg in {"once", "availableNow"}:
                value = self.folder.fold(kw.value)
                return kw.arg if value is True or isinstance(value, Unresolved) else None
        return None

    def _foreach_batch(self, call: ast.Call, function: ast.expr, extra: Dict[str, Any]) -> None:
        span: Optional[Tuple[int, int]] = None
        if isinstance(function, ast.Lambda):
            span = (function.lineno, getattr(function, "end_lineno", function.lineno))
        elif isinstance(function, ast.Name):
            for node in ast.walk(self.module.tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == function.id
                ):
                    span = (node.lineno, getattr(node, "end_lineno", node.lineno))
                    break
        self.facts.streams.append(
            Stream(
                file=self.file,
                line=call.lineno,
                via_foreach_batch=True,
                checkpoint=extra["checkpoint"],
                trigger=extra["trigger"],
                query_name=extra["query_name"],
                output_mode=extra["output_mode"],
                batch_function=span,
            )
        )

    # ── the end ─────────────────────────────────────────────────────

    def _finish(self) -> None:
        facts = self.facts
        # A write inside a foreachBatch function is a streaming write, with the query's facts.
        for stream in facts.streams:
            if stream.batch_function is None:
                continue
            first, last = stream.batch_function
            facts.writes = [
                (
                    replace(
                        w,
                        streaming=True,
                        checkpoint=w.checkpoint or stream.checkpoint,
                        trigger=w.trigger or stream.trigger,
                        query_name=w.query_name or stream.query_name,
                    )
                    if first <= w.line <= last
                    else w
                )
                for w in facts.writes
            ]
        if self.parameter_named:
            facts.notes.append(
                f"{self.file}: {self.parameter_named} read(s) or write(s) take a table, path "
                "or SQL from a function parameter: this looks like a helper. Name it under "
                "`helpers` in the conventions file, and its call sites are read instead"
            )
        if self.folder.exhausted:
            facts.notes.append(
                f"{self.file}: folding stopped early (the file asks for too much work): some "
                "names are reported as not constant"
            )
        facts.reads = _unique(facts.reads)
        facts.writes = _unique(facts.writes)
        if facts.has_io:
            facts.is_spark = True


def _fields(ref: Ref) -> Dict[str, Any]:
    return {
        "kind": ref.kind,
        "parts": ref.parts,
        "shown": ref.shown,
        "why": ref.why,
        "file": ref.file,
        "line": ref.line,
        "fmt": ref.fmt,
    }


def _unique(items: Sequence[Any]) -> List[Any]:
    """The same refs seen twice (a chain that two calls both reach) once, in line order."""
    seen: Set[Tuple[Any, ...]] = set()
    out: List[Any] = []
    for item in sorted(items, key=lambda r: (r.line, r.kind, r.shown)):
        key = (
            type(item).__name__,
            item.kind,
            item.parts,
            item.shown,
            item.line,
            getattr(item, "mode", None),
        )
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _assign_topics(value: Any) -> Optional[List[str]]:
    """Topic names of a Kafka ``assign`` option (JSON ``{"topic": [partitions]}``)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if isinstance(parsed, dict) and all(isinstance(k, str) for k in parsed):
        return sorted(parsed)
    return None


def read_job(
    module: Module,
    folder: Folder,
    *,
    helpers: Optional[Mapping[str, HelperSpec]] = None,
    session_names: Iterable[str] = DEFAULT_SESSION_NAMES,
    shown: Optional[str] = None,
) -> JobFacts:
    """What ``module`` reads, writes and streams. See the module docstring. ``shown`` is the
    path reports name the file by (default: the module's own relative path)."""
    return _Reader(module, folder, helpers or {}, session_names, shown).run()


__all__ = [
    "DEFAULT_SESSION_NAMES",
    "HelperSpec",
    "JobFacts",
    "Read",
    "Ref",
    "Stream",
    "Write",
    "read_job",
    "split_table_name",
]
