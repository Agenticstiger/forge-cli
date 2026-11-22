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
from ._fold import MAX_SQL_STRING, FoldedDict, Folder, Unresolved
from ._pysource import MAX_TOTAL_SQL_CHARS, Module, clean_chars, qualified_name, snippet
from ._redact import scrub_host, scrub_path

#: Receivers whose ``.table(...)`` and ``.sql(...)`` are Spark's. The conventions add names; a
#: name assigned from ``....getOrCreate()`` in the file is added as it is met.
DEFAULT_SESSION_NAMES = frozenset({"spark", "session", "spark_session", "sparkSession", "ss"})
#: Reads, and writes, kept for one job file: a job has tens, and a file of nothing but ``.table(...)``
#: calls is not one.
MAX_REFS_PER_JOB = 5_000
#: Longest table name or path kept (an object key is at most 1,024 bytes anywhere): every ref is
#: held until the proposal is built, and a scan keeps up to ``MAX_REFS_PER_SCAN`` of them.
MAX_NAME = 512
#: Notes kept for one job file: a file whose every ``spark.sql`` takes a name that is not a
#: constant has one per call, and a person reads a few.
MAX_NOTES_PER_JOB = 200

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

#: DataFrame methods that take another DataFrame, so a name given to them is data, not a value.
_FRAME_METHODS = frozenset(
    {"join", "crossJoin", "union", "unionAll", "unionByName", "intersect", "intersectAll"}
    | {"subtract", "exceptAll", "broadcast", "createDataFrame"}
)
#: Methods that run a function over the rows: what the function reads is not known.
_FUNCTION_METHODS = frozenset(
    {"transform", "mapInPandas", "mapInArrow", "applyInPandas", "cogroup", "foreach"}
    | {"foreachBatch", "foreachPartition", "map", "flatMap", "mapPartitions", "udf", "pandas_udf"}
    | {"rdd", "toPandas", "collect"}
)
#: Options that say WHAT a source or a sink is (where it reads or writes, and its state). A
#: variable's later assignment may add other options, never one of these: it would be a different
#: source or sink in each branch.
_IDENTITY_OPTIONS = frozenset(
    {"path", "paths", "topic", "subscribe", "subscribepattern", "assign", "table", "dbtable"}
    | {"url", "query", "kafka.bootstrap.servers", "checkpointlocation"}
)
#: Joins the triggers a stream may have when the code picks one by a branch.
TRIGGER_SEPARATOR = " | "

#: A placeholder a templating layer fills in later, in SQL that is not an f-string: ``${db}``,
#: ``{db}``, ``{{ db }}``, ``%(db)s``. The SQL parser would read the text around it as a name.
_PLACEHOLDER = re.compile(
    r"\$\{[^}\s]{0,200}\}|\{\{[^}]{0,200}\}\}|\{[A-Za-z_][A-Za-z0-9_.]{0,100}\}"
    r"|%\([A-Za-z_][A-Za-z0-9_]{0,100}\)s"
)


_IDENTIFIER_CALL = re.compile(r"(?i)\bIDENTIFIER\s*\(")
#: ``IDENTIFIER(`` calls of one statement turned into holes; the rest are left as written. Each
#: costs up to 500 characters of scanning for its closing parenthesis, and a statement may be 256 KB.
_MAX_IDENTIFIER_CALLS = 500


def _closing_paren(text: str, open_at: int) -> int:
    """Where the parenthesis opened at ``open_at`` closes, or -1 (within 500 characters, quotes
    respected)."""
    depth, quote = 0, ""
    for index in range(open_at, min(len(text), open_at + 500)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _identifier_calls_to_holes(text: str, holes: Dict[str, str]) -> str:
    """Spark's ``IDENTIFIER(...)`` names a table by an expression: it is a hole, not a table
    called ``identifier``."""
    out: List[str] = []
    index = 0
    seen = 0
    while True:
        match = _IDENTIFIER_CALL.search(text, index) if seen < _MAX_IDENTIFIER_CALLS else None
        if match is None:
            out.append(text[index:])
            return "".join(out)
        seen += 1
        end = _closing_paren(text, match.end() - 1)
        if end < 0:
            out.append(text[index : match.end()])
            index = match.end()
            continue
        token = _spark_sql.HOLE.format(len(holes))
        holes[token] = text[match.start() : end + 1]
        out.append(text[index : match.start()])
        out.append(token)
        index = end + 1


def _placeholders_to_holes(text: str, holes: Dict[str, str]) -> str:
    """``text`` with each placeholder replaced by a hole token (as an f-string field that does not
    fold is), so the name around it is reported as unresolved, not read as another table."""
    text = _identifier_calls_to_holes(text, holes)

    def token(match: "re.Match[str]") -> str:
        name = _spark_sql.HOLE.format(len(holes))
        holes[name] = match.group(0)
        return name

    return _PLACEHOLDER.sub(token, text)


def path_root(known: str, rules: Sequence[Tuple[str, bool]]) -> Optional[str]:
    """The table a path names when only its beginning is known (``known``, the text before the
    first part that does not fold), by the first of the conventions' path ``rules`` it falls
    under: the rule's prefix and the segment after it (the rule's prefix alone when the rule gives
    a fixed table). ``None`` when no rule applies, or when the segment that names the table is not
    complete in what is known (it may go on in the part that does not fold). ``rules`` are
    ``(prefix, the rule names a table)``, in the order the conventions give them."""
    path = known.split("?")[0].split("#")[0]
    for prefix, fixed in rules:
        base = prefix.rstrip("/")
        if not (path == base or path.startswith(base + "/")):
            continue
        if fixed:
            return base
        rest = path[len(base) :].lstrip("/")
        if "/" not in rest:
            return None  # the segment may go on where the text stops being known
        return base + "/" + rest.split("/")[0]
    return None


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
    #: True for a read that feeds one write and nothing else: one a closed SQL statement makes
    #: (see :meth:`_Reader._on_sql`), or one written inside the expression of a write.
    exclusive: bool = False


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
    #: The keys (:func:`read_key`) of every read that feeds this write, when the code says (a SQL
    #: statement that names all it reads, a DataFrame built from reads and nothing the reader
    #: cannot follow); ``None`` when any read of the job may feed it.
    feeds: Optional[Tuple[Tuple[Any, ...], ...]] = None


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


def read_key(read: "Read") -> Tuple[Any, ...]:
    """What identifies a read among the reads of one job: two reads with this key are one read
    (a chain that two calls both reach), and a write names the reads that feed it by it."""
    return (read.kind, read.parts, read.shown, read.line)


@dataclass
class JobFacts:
    """What one file does with Spark."""

    rel: str
    reads: List[Read] = field(default_factory=list)
    writes: List[Write] = field(default_factory=list)
    streams: List[Stream] = field(default_factory=list)
    #: Job-level lines for the report: SQL that did not parse, JDBC, names from parameters.
    notes: List[str] = field(default_factory=list)
    #: Lines for the report that are about what was read, not about what was not: a person checks
    #: them, nothing has to be re-authored.
    remarks: List[str] = field(default_factory=list)
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
    #: 0 for a link every run goes through. ``k`` for a link added by the ``k``-th later
    #: assignment of the variable the chain started from (``if a: w = w.trigger(x)``): one branch.
    branch: int = 0
    #: For a link of a branch: the branches are the two arms of one ``if``, so one of them runs.
    either: bool = False


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
        path_rules: Sequence[Tuple[str, bool]] = (),
    ) -> None:
        self.module = module
        self.folder = folder
        self.helpers = helpers
        self.path_rules = tuple(path_rules)
        self.sessions: Set[str] = set(session_names)
        self.file = shown or module.rel
        self.facts = JobFacts(rel=module.rel)
        self.temp_views: Set[str] = set()
        self.default_checkpoint: Optional[str] = None
        #: What feeds what: the reads each call made (positions in ``facts.reads``), the calls
        #: whose reads are not all in ``facts.reads`` (a name that did not fold, a temporary view,
        #: SQL that did not parse), the call being read, and the expression the write being read
        #: is made of.
        self._produced: Dict[int, List[int]] = {}
        self._open: Set[int] = set()
        self._current: Optional[ast.Call] = None
        self._expression: Optional[ast.AST] = None
        self._write_exprs: List[Tuple[int, ast.AST]] = []
        self.parameter_named = 0
        self._handled: Set[int] = set()
        #: What the SQL parser found in a statement, by the statement (and its holes): the same
        #: text passed to ``spark.sql`` a thousand times is parsed once.
        self._sql_cache: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], _spark_sql.SqlFacts] = {}

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

    @staticmethod
    def _plain_links(node: ast.AST) -> Tuple[List[_Link], ast.AST]:
        """The method and attribute links of a fluent expression, root first, and the root
        expression, as written (a root variable is not followed)."""
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
        return links, current

    def _links(self, node: ast.AST, depth: int = 0) -> Tuple[List[_Link], ast.AST]:
        """The method and attribute links of a fluent expression, root first, and the root
        expression. A root variable assigned once to another chain is followed (three levels), and
        so is one that later assignments only add to (:meth:`_extended`)."""
        links, current = self._plain_links(node)
        if isinstance(current, ast.Name) and depth < 3:
            started = any(
                link.name in _READER_MARKERS | _WRITER_MARKERS | {"writeTo"} for link in links
            )
            bound = self._bound_expression(current)
            if bound is not None and not started:
                earlier, root = self._links(bound, depth + 1)
                return earlier + links, root
            if bound is None and not started:
                extended = self._extended(current, depth)
                if extended is not None:
                    earlier, root = extended
                    return earlier + links, root
        return links, current

    def _assignments(self, name: ast.Name) -> Optional[List[ast.expr]]:
        """Every assignment of the variable ``name`` is when it is assigned more than once and
        only by plain assignments, else ``None``."""
        if self.folder.rebound(name.id):
            return None
        current: Optional[Any] = self.folder.scope_of(name)
        while current is not None:
            if name.id in current.assignments:
                return sorted(current.assignments[name.id], key=lambda e: (e.lineno, e.col_offset))
            if (
                name.id in current.values
                or name.id in current.blocked
                or name.id in current.classes
                or name.id in current.recipes
            ):
                return None
            current = current.parent
        return None

    def _extended(self, name: ast.Name, depth: int) -> Optional[Tuple[List[_Link], ast.AST]]:
        """The chain a variable assigned more than once holds at ``name``, when the first
        assignment starts it and every later one only adds to it: ``w = a.writeStream.format(..)``
        then ``w = w.trigger(x)`` in one branch and ``w = w.trigger(y)`` in the other. The links
        of each addition that comes before ``name`` are kept, each tagged as a branch of its own.
        ``None`` when a later assignment does anything else (starts another chain, changes what
        the sink or source is): which assignment is meant then depends on the order the code runs
        in, and the read or write is not followed."""
        found = self._assignments(name)
        if not found or found[0].lineno >= name.lineno:
            return None
        base, later = found[0], found[1:]
        base_links, base_root = self._plain_links(base)
        if isinstance(base_root, ast.Name) and base_root.id == name.id:
            return None  # the first one adds to a chain nobody started
        earlier, root = self._links(base, depth + 1)
        if not any(
            link.name in _READER_MARKERS | _WRITER_MARKERS | {"writeTo"} for link in earlier
        ):
            return None
        kept: List[Tuple[int, ast.expr, List[_Link]]] = []
        for branch, expression in enumerate(later, start=1):
            more, more_root = self._plain_links(expression)
            if not (isinstance(more_root, ast.Name) and more_root.id == name.id and more):
                return None
            if not all(self._adds_nothing_to_identity(link) for link in more):
                return None
            if expression.lineno < name.lineno:
                # one in the block of the first assignment runs on every path to the use: it is
                # a plain step of the chain, not a branch
                kept.append((0 if self._same_block(base, expression) else branch, expression, more))
        conditional = [expression for branch, expression, _ in kept if branch]
        either = len(conditional) == 2 and self._arms_of_one_if(conditional[0], conditional[1])
        added = [
            replace(link, branch=branch, either=either)
            for branch, _expression, more in kept
            for link in more
        ]
        return earlier + added, root

    def _same_block(self, first: ast.AST, second: ast.AST) -> bool:
        """Whether the statements that assign ``first`` and ``second`` are in one block (one list
        of statements of one parent): the later one runs whenever the earlier one does."""
        parents = self.module.parents

        def block(expression: ast.AST) -> Optional[Tuple[int, str]]:
            statement = parents.get(expression)
            owner = parents.get(statement) if statement is not None else None
            if owner is None:
                return None
            for name, value in ast.iter_fields(owner):
                if isinstance(value, list) and any(item is statement for item in value):
                    return id(owner), name
            return None

        found = block(first)
        return found is not None and found == block(second)

    def _arms_of_one_if(self, first: ast.AST, second: ast.AST) -> bool:
        """Whether ``first`` and ``second`` sit in the two different arms of one ``if``: exactly
        one of them runs."""
        parents = self.module.parents

        def arms(node: ast.AST) -> Dict[int, str]:
            found: Dict[int, str] = {}
            child, current = node, parents.get(node)
            while current is not None:
                if isinstance(current, ast.If):
                    found[id(current)] = "body" if child in current.body else "orelse"
                elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    break
                child, current = current, parents.get(current)
            return found

        left, right = arms(first), arms(second)
        return any(key in right and right[key] != arm for key, arm in left.items())

    def _adds_nothing_to_identity(self, link: _Link) -> bool:
        """Whether a link a later assignment adds is one that leaves the source or sink what it
        is: a trigger, or an option that is not one of :data:`_IDENTITY_OPTIONS`."""
        call = link.call
        if call is None:
            return False
        if link.name == "trigger":
            return True
        if link.name == "option" and len(call.args) >= 2:
            key = self._literal(call.args[0])
            return key is not None and key.lower() not in _IDENTITY_OPTIONS
        if link.name == "options":
            return (
                all(
                    kw.arg is not None and kw.arg.lower() not in _IDENTITY_OPTIONS
                    for kw in call.keywords
                )
                and not call.args
            )
        return False

    def _bound_expression(self, name: ast.Name) -> Optional[ast.expr]:
        scope = self.folder.scope_of(name)
        current: Optional[Any] = scope
        while current is not None:
            if name.id in current.values:
                return current.values[name.id]
            if (
                name.id in current.blocked
                or name.id in current.classes
                or name.id in current.recipes
            ):
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
        """Read one call, and remember which reads it made."""
        before = len(self.facts.reads)
        self._current = call
        try:
            self._dispatch(call)
        finally:
            self._current = None
        if len(self.facts.reads) > before:
            self._produced[id(call)] = list(range(before, len(self.facts.reads)))

    def _dispatch(self, call: ast.Call) -> None:
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
            if (
                root.id in current.values
                or root.id in current.classes
                or root.id in current.recipes
            ):
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

    def _path_text(self, node: Optional[ast.expr]) -> Union[str, Unresolved, None]:
        """:meth:`_text` for a path: when it does not fold, the table root a conventions rule
        gives it if the beginning of it does (:meth:`_rooted`)."""
        value = self._text(node)
        if isinstance(value, Unresolved) and node is not None:
            rooted = self._rooted(node)
            if rooted is not None:
                return rooted
        return value

    def _rooted(self, node: ast.expr) -> Optional[str]:
        """The table root of a path such as ``f"{LANDING}/orders/dt={run_date}/"``, whose end is
        not a constant (a run date, a parameter) but whose beginning is the prefix of one of the
        conventions' path rules and runs past the segment that names the table."""
        if not self.path_rules:
            return None
        text, holes = self.folder.fold_sql(node)
        if not isinstance(text, str) or not holes:
            return None
        cut = text.find(_spark_sql.HOLE.split("{")[0])
        root = path_root(text if cut < 0 else text[:cut], self.path_rules)
        if root is not None:
            note = (
                f"{self.file}:{getattr(node, 'lineno', 0)} reads or writes a path that is not a "
                f"constant past {scrub_path(root)}: it is taken as that table"
            )
            if note not in self.facts.remarks and len(self.facts.remarks) < MAX_NOTES_PER_JOB:
                self.facts.remarks.append(note)
        return root

    def _literal(self, node: Optional[ast.expr]) -> Optional[str]:
        value = self._text(node)
        return value if isinstance(value, str) else None

    def _option_value(self, key: str, node: ast.expr) -> Any:
        """The folded value of an option; a ``path`` that only folds at its beginning is the
        table root the conventions give it (:meth:`_rooted`)."""
        value = self.folder.fold(node)
        if isinstance(value, Unresolved) and key.lower() == "path":
            return self._rooted(node) or value
        return value

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
                    options[key.lower()] = self._option_value(key, call.args[1])
            elif link.name == "options":
                for kw in call.keywords:
                    if kw.arg is not None:
                        options[kw.arg.lower()] = self._option_value(kw.arg, kw.value)
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
        if len(text) > MAX_NAME:
            why = f"the name is longer than {MAX_NAME} characters"
            return Ref(kind, (), text[:60] + "...", why, rel, line, fmt)
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
        value = self._path_text(node)
        if (
            fmt == "iceberg"
            and isinstance(value, str)
            and _PLAIN_TABLE.fullmatch(value.strip() or "!")
            and "/" not in value
        ):
            return self._ref("table", value, at, fmt)
        return self._ref("path", value, at, fmt, missing="no path is given")

    # ── records ─────────────────────────────────────────────────────

    def _drop(self) -> None:
        """A read the call makes is not recorded: the reads of the call are not all known."""
        if self._current is not None:
            self._open.add(id(self._current))

    def _add_read(
        self, ref: Ref, *, streaming: bool, host: Optional[str] = None, exclusive: bool = False
    ) -> Optional[int]:
        """Record a read; its position in ``facts.reads``, or ``None`` when it was not recorded."""
        if not ref.resolved and _PARAMETER in ref.why:
            self.parameter_named += 1
            self._drop()
            return None
        if ref.kind == "table" and ref.resolved and self._is_temp_view(ref):
            self._drop()
            return None
        if len(self.facts.reads) >= MAX_REFS_PER_JOB:
            self._too_many("reads")
            self._drop()
            return None
        host = scrub_host(host) if host else host
        self.facts.reads.append(
            Read(**_fields(ref), streaming=streaming, host=host, exclusive=exclusive)
        )
        return len(self.facts.reads) - 1

    def _add_write(self, ref: Ref, **extra: Any) -> None:
        if not ref.resolved and _PARAMETER in ref.why:
            self.parameter_named += 1
            return
        if len(self.facts.writes) >= MAX_REFS_PER_JOB:
            self._too_many("writes")
            return
        if extra.get("host"):
            extra["host"] = scrub_host(extra["host"])
        self.facts.writes.append(Write(**_fields(ref), **extra))
        if self._expression is not None and "feeds" not in extra:
            self._write_exprs.append((len(self.facts.writes) - 1, self._expression))

    def _too_many(self, kind: str) -> None:
        note = f"{self.file}: more than {MAX_REFS_PER_JOB:,} {kind}: the rest are not listed"
        if note not in self.facts.notes:
            self.facts.notes.append(note)

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
            spec.kind,
            self._path_text(node) if spec.kind == "path" else self._text(node),
            call,
            spec.fmt,
            missing="the argument is not given",
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
        value, holes = self.folder.fold_sql(node, limit=MAX_SQL_STRING)
        if isinstance(value, Unresolved):
            self._drop()  # what it reads is not known
            if _PARAMETER in value.why:
                self.parameter_named += 1
            else:
                self.facts.notes.append(
                    f"{self.file}:{call.lineno} spark.sql(...) takes SQL that is not a "
                    f"constant ({value.why})"
                )
            return
        # Looked up before the text is read for placeholders: the same constant passed to a
        # thousand ``spark.sql`` calls is read once.
        key = (value, tuple(sorted(holes.items())))
        sql = self._sql_cache.get(key)
        if sql is None:
            prepared = _placeholders_to_holes(value, holes)
            sql = self._sql_cache[key] = self._analyze_sql(prepared, holes)
        self.temp_views.update(sql.temp_views)
        for note in sql.notes:
            self._drop()  # SQL that was not read whole
            self.facts.notes.append(f"{self.file}:{call.lineno} {note}")
        self._record_sql(call, sql)

    def _record_sql(self, call: ast.Call, sql: _spark_sql.SqlFacts) -> None:
        """The reads and the writes of a statement that was read, each write with the reads that
        feed it when the statement says so."""
        refs = {id(name): self._from_sql_name(name, call) for name in sql.reads}
        #: A statement is closed when everything it reads is a table or a path that is named:
        #: nothing it reads is a temporary view (which something else built) or a name with a
        #: hole, so the tables it names are all that feed what it writes. Its reads feed that
        #: write and nothing else, and the proposal lists only them for the product.
        closed: Dict[int, bool] = {}
        owned: Set[int] = set()
        for index, write in enumerate(sql.writes):
            sources = [refs[id(s)] for s in write.sources if id(s) in refs]
            closed[index] = all(r.resolved and not self._is_temp_view(r) for r in sources)
            if closed[index]:
                owned.update(id(s) for s in write.sources)
        position: Dict[int, Optional[int]] = {}
        for name in sql.reads:
            position[id(name)] = self._add_read(
                refs[id(name)], streaming=False, exclusive=id(name) in owned
            )
        for index, write in enumerate(sql.writes):
            ref = self._from_sql_name(write.name, call)
            feeds = None
            if closed[index] and not self._unrecorded(write.sources, position):
                feeds = tuple(
                    sorted(
                        {
                            read_key(self.facts.reads[p])
                            for s in write.sources
                            if (p := position.get(id(s))) is not None
                        }
                    )
                )
            self._add_write(ref, mode=write.mode, feeds=feeds)

    @staticmethod
    def _unrecorded(sources: Sequence[Any], position: Dict[int, Optional[int]]) -> bool:
        return any(position.get(id(s)) is None for s in sources)

    def _analyze_sql(self, prepared: str, holes: Dict[str, str]) -> _spark_sql.SqlFacts:
        """What the parser finds in a statement, unless this import has parsed all the SQL it
        parses (a text too long to parse costs nothing and is not charged)."""
        index = self.folder.index
        charged = index is not None and len(prepared) <= _spark_sql.MAX_SQL_CHARS
        if charged and not index.budget.take_sql(len(prepared)):  # type: ignore[union-attr]
            skipped = _spark_sql.SqlFacts()
            skipped.notes.append(
                f"SQL was not read: one import parses at most {MAX_TOTAL_SQL_CHARS:,} characters "
                "of SQL in all"
            )
            return skipped
        return _spark_sql.analyze(prepared, holes)

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
                if isinstance(value, Unresolved):
                    value = self._rooted(node) or value
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
                    self._ref("path", self._path_text(keyword_path), call, name),
                    streaming=streaming,
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
        self._expression = getattr(start.node, "value", None)
        try:
            self._write(call, own, streaming=streaming)
        finally:
            self._expression = None

    def _write(self, call: ast.Call, own: Sequence[_Link], *, streaming: bool) -> None:
        terminal = own[-1]
        options, fmt = self._options(own[:-1])
        mode = None
        triggers: Dict[int, str] = {}  # by branch; the last one of a branch wins
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
                found = self._trigger(link.call)
                if found:
                    triggers[link.branch] = found
            elif link.name == "foreachBatch" and args:
                foreach = args[0]
            elif link.name == "foreach":
                fmt = fmt or "foreach"
        trigger = self._trigger_of_write(call, own, triggers) if streaming else None
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
            self._add_write(self._ref("path", self._path_text(node), call, name), **extra)

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
        func = start.call.func
        self._expression = func.value if isinstance(func, ast.Attribute) else None
        try:
            self._add_write(ref, mode=_V2_TERMINALS[terminal.name])
        finally:
            self._expression = None

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

    def _trigger_of_write(
        self, call: ast.Call, own: Sequence[_Link], triggers: Mapping[int, str]
    ) -> Optional[str]:
        """The trigger of a stream. When the code picks it by a branch (``if a: w = w.trigger(x)``
        ``else: w = w.trigger(y)``) it is every one that can apply, joined by
        :data:`TRIGGER_SEPARATOR`: the path through no branch counts too (the trigger set before
        it, else Spark's default) unless the branches are the two arms of one ``if`` and each
        sets one. A stream that may keep running is then not taken for a one-shot."""
        if not triggers:
            return None
        branches = {link.branch for link in own if link.branch}
        if not branches:
            return triggers.get(0)
        chosen = [value for branch, value in triggers.items() if branch]
        covered = any(link.either for link in own) and len(chosen) == len(branches)
        if not covered:
            chosen.append(triggers.get(0, "default"))
        alternatives = list(dict.fromkeys(chosen))
        if len(alternatives) > 1:
            note = (
                f"{self.file}:{call.lineno} the trigger of this stream is picked by a branch of "
                f"the code ({', '.join(alternatives)}): it is `continuous` unless every one of "
                "them ends by itself"
            )
            if note not in self.facts.remarks:
                self.facts.remarks.append(note)
        return TRIGGER_SEPARATOR.join(alternatives)

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

    # ── what feeds what ─────────────────────────────────────────────

    #: Nodes one expression is followed through, and the names it is followed through.
    _MAX_LINEAGE_NODES = 400
    _MAX_LINEAGE_DEPTH = 6

    def _lineage(self) -> None:
        """Tie each write that is made of a DataFrame to the reads that DataFrame is made of."""
        facts = self.facts
        for index, expression in self._write_exprs:
            found = self._made_of(expression)
            if found is None:
                continue
            keys = tuple(sorted({read_key(facts.reads[position]) for position in found}))
            facts.writes[index] = replace(facts.writes[index], feeds=keys)
            for position, inline in found.items():
                if inline and not facts.reads[position].exclusive:
                    facts.reads[position] = replace(facts.reads[position], exclusive=True)

    def _made_of(self, expression: ast.AST) -> Optional[Dict[int, bool]]:
        """The reads an expression is made of, as ``{position: written inside the expression}``
        (not reached through a name, so nothing else uses it), or ``None`` when part of it that
        carries data is not understood: a name that is not a session, a pyspark import or assigned
        once, a call to anything but a method, a pyspark function or a session, a lambda, a
        comprehension, a read whose name did not fold. What only gives a value (a name inside an
        f-string, a comparison or a pyspark function's argument) need not be understood, unless it
        is a function or class of the file, which may read anything."""
        found: Dict[int, bool] = {}
        budget = [self._MAX_LINEAGE_NODES]

        def walk(node: ast.AST, depth: int, shared: bool, scalar: bool) -> bool:
            budget[0] -= 1
            if budget[0] < 0 or depth > self._MAX_LINEAGE_DEPTH:
                return False
            if isinstance(node, ast.Constant):
                return True
            if isinstance(node, ast.Name):
                if node.id in self.sessions or self._is_pyspark(node.id):
                    return True
                bound = self._bound_expression(node)
                if bound is not None and not self.folder.rebound(node.id):
                    return walk(bound, depth + 1, True, scalar)
                return scalar and not self._is_callable_of_file(node)
            if isinstance(node, ast.Attribute):
                return walk(node.value, depth, shared, scalar)
            if isinstance(node, ast.Call):
                if id(node) in self._open:
                    return False
                for position in self._produced.get(id(node), ()):
                    found[position] = found.get(position, True) and not shared
                func = node.func
                inner = scalar
                if isinstance(func, ast.Attribute):
                    if func.attr in _FUNCTION_METHODS:
                        return False
                    ok = walk(func.value, depth, shared, scalar)
                    inner = scalar or func.attr not in _FRAME_METHODS
                elif isinstance(func, ast.Name):
                    ok = self._is_pyspark(func.id) or (
                        scalar and not self._is_callable_of_file(func)
                    )
                    inner = True
                else:
                    ok = False
                return ok and all(
                    walk(argument, depth, shared, inner)
                    for argument in (*node.args, *(k.value for k in node.keywords))
                )
            if isinstance(node, ast.Starred):
                return walk(node.value, depth, shared, scalar)
            if isinstance(node, ast.Subscript):
                return walk(node.value, depth, shared, scalar) and walk(
                    node.slice, depth, shared, True
                )
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return all(walk(item, depth, shared, scalar) for item in node.elts)
            if isinstance(node, ast.Dict):
                return all(
                    walk(part, depth, shared, scalar)
                    for part in (*node.keys, *node.values)
                    if part is not None
                )
            if isinstance(node, (ast.JoinedStr, ast.BoolOp, ast.Compare, ast.BinOp, ast.UnaryOp)):
                return all(walk(child, depth, shared, True) for child in ast.iter_child_nodes(node))
            if isinstance(node, ast.FormattedValue):
                return walk(node.value, depth, shared, True)
            if isinstance(node, ast.Slice):
                return all(
                    walk(part, depth, shared, True)
                    for part in (node.lower, node.upper, node.step)
                    if part is not None
                )
            if isinstance(node, ast.IfExp):
                return walk(node.test, depth, shared, True) and all(
                    walk(n, depth, shared, scalar) for n in (node.body, node.orelse)
                )
            if isinstance(node, (ast.operator, ast.cmpop, ast.boolop, ast.unaryop)):
                return True
            return False

        return found if walk(expression, 0, False, False) else None

    def _is_callable_of_file(self, node: ast.Name) -> bool:
        """Whether ``node`` is a function or a class defined in the file (it may read anything)."""
        current: Optional[Any] = self.folder.scope_of(node)
        while current is not None:
            why = current.blocked.get(node.id)
            if why is not None:
                return "is a function" in why or "is a class" in why
            if node.id in current.classes:
                return True
            if node.id in current.values or node.id in current.recipes:
                return False
            current = current.parent
        return False

    def _is_pyspark(self, name: str) -> bool:
        """Whether ``name`` is an import from pyspark (``F``, ``col``, ``Window``)."""
        return self.module.imports.get(name, "").split(".")[0] == "pyspark"

    def _finish(self) -> None:
        self._lineage()
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
        # a read is one write's own only while a write names it (two writes that were one and
        # said different things are one write that may be fed by anything)
        owned = {key for write in facts.writes if write.feeds for key in write.feeds}
        facts.reads = [
            (
                replace(read, exclusive=False)
                if read.exclusive and read_key(read) not in owned
                else read
            )
            for read in facts.reads
        ]
        if len(facts.notes) > MAX_NOTES_PER_JOB:
            more = len(facts.notes) - MAX_NOTES_PER_JOB
            facts.notes = [*facts.notes[:MAX_NOTES_PER_JOB], f"{self.file}: and {more} more notes"]
        if facts.has_io:
            facts.is_spark = True


def _fields(ref: Ref) -> Dict[str, Any]:
    """The fields a read or a write keeps of ``ref``. A data path loses the credential in it
    (the userinfo before the ``@`` of a URL-style path), and every name loses control and
    bidirectional characters:
    ids, file names and bindings are made from what is kept here."""
    if ref.kind == "path":
        parts = tuple(scrub_path(clean_chars(p)) for p in ref.parts)
        shown = scrub_path(clean_chars(ref.shown))
    else:
        parts = tuple(clean_chars(p) for p in ref.parts)
        shown = clean_chars(ref.shown)
    return {
        "kind": ref.kind,
        "parts": parts,
        "shown": shown,
        "why": ref.why,
        "file": ref.file,
        "line": ref.line,
        "fmt": ref.fmt,
    }


def _unique(items: Sequence[Any]) -> List[Any]:
    """The same refs seen twice (a chain that two calls both reach) once, in line order. Of two
    that are one, a read is its own to a write only if both are, and a write's feeds are the
    reads of both (``None``, any read may feed it, if either says so)."""
    seen: Dict[Tuple[Any, ...], int] = {}
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
        at = seen.get(key)
        if at is None:
            seen[key] = len(out)
            out.append(item)
        elif isinstance(item, Read) and out[at].exclusive and not item.exclusive:
            out[at] = replace(out[at], exclusive=False)
        elif isinstance(item, Write) and out[at].feeds != item.feeds:
            both = (
                None
                if out[at].feeds is None or item.feeds is None
                else tuple(sorted({*out[at].feeds, *item.feeds}))
            )
            out[at] = replace(out[at], feeds=both)
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
    path_rules: Sequence[Tuple[str, bool]] = (),
) -> JobFacts:
    """What ``module`` reads, writes and streams. See the module docstring. ``shown`` is the
    path reports name the file by (default: the module's own relative path). ``path_rules`` are
    the conventions' data path rules as ``(prefix, names a table)``: a path of which only the
    beginning folds is read as the table such a rule gives it (:func:`path_root`)."""
    return _Reader(module, folder, helpers or {}, session_names, shown, path_rules).run()


__all__ = [
    "DEFAULT_SESSION_NAMES",
    "HelperSpec",
    "JobFacts",
    "Read",
    "Ref",
    "Stream",
    "Write",
    "path_root",
    "read_job",
    "split_table_name",
]
