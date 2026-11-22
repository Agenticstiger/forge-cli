# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Static reading of Python source for the ``airflow`` and ``pyspark`` importers.

Everything those importers know about a project comes through here, and none of it runs the
project: a file is read with :func:`fluid_build.util.safe_yaml.read_bounded_file` (a regular
file, never past the size cap), parsed with :mod:`ast`, and never imported, compiled or
executed. A DAG file is never imported, so Airflow is never loaded and a module-level call in
the file never runs.

This module holds the plumbing: finding the files (:func:`find_python_files`), parsing one
(:func:`parse_module`), the table of what each imported name stands for
(:func:`import_table`, :func:`qualified_name`) and the parent links of a syntax tree
(:func:`parent_map`). Reading a value out of the syntax is :mod:`._fold`.

Borrowed, not depended on: Apache Airflow's ``might_contain_dag_via_default_heuristic``
(Apache-2.0: a DAG file's bytes hold ``dag`` and ``airflow``, see :func:`might_contain_dag`)
and ruff's Airflow rules (MIT: resolve a call through the import table, so an aliased import
is still seen, and read only what is literal).
"""

from __future__ import annotations

import ast
import io
import os
import tokenize
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

#: A source file larger than this is not read (300 KB: a job or a DAG file is a few KB, and a
#: dense one costs about a kilobyte and a half of memory per byte of source once it is parsed).
MAX_FILE_BYTES = 300_000
#: A file with more ``{`` characters than this is not parsed. CPython parses an f-string in time
#: that grows with the square of its fields (a 900 KB file of ``{b}`` fields takes 15 s and
#: 500 MB); a job or a DAG file has a few hundred braces.
MAX_BRACES = 50_000
#: Source one scan reads in all, and the most syntax-tree nodes kept parsed at once (about
#: 280 MB: a node of a dense file costs up to 1.4 KB, parent links included).
MAX_TOTAL_BYTES = 64 * 1024 * 1024
#: Characters of SQL one import parses in all (a statement is parsed once per file, whatever the
#: number of calls that pass it): the parser takes about 4 ms per kilobyte, so this is some twelve
#: seconds at the most.
MAX_TOTAL_SQL_CHARS = 1_500_000
#: Source read again, in all, after its module was dropped from the cache to make room: a name
#: that needs three big modules in turn reads each over and over, and that is paid from this, so
#: the job files still get the budget above.
MAX_RELOAD_BYTES = 2 * 1024 * 1024
MAX_CACHED_NODES = 200_000
#: Most Python files one walk keeps, and directory entries it reads, and levels it descends.
MAX_FILES = 5_000
MAX_ENTRIES = 50_000
MAX_DEPTH = 12

#: Directories a project walk leaves alone, on top of the ones forge-cli's own walks skip
#: (virtual environments, build output, VCS metadata): tests are not jobs.
_EXTRA_SKIP = frozenset({"tests", "test", "__pycache__"})


class SourceUnreadable(Exception):
    """A file that cannot be read as Python source. ``str(exc)`` is the reason, free of the
    file's text."""


@dataclass(frozen=True)
class SourceFile:
    """A Python file found by :func:`find_python_files`."""

    #: POSIX path relative to the walked directory.
    rel: str
    path: Path


@dataclass
class Walk:
    files: List[SourceFile] = field(default_factory=list)
    #: What the walk did not take, one line each (symlinks, caps).
    notes: List[str] = field(default_factory=list)


def _skip_dirs() -> frozenset:
    from fluid_build.util.upstream_discovery import BUILD_OUTPUT_DIRS, IGNORED_DIRS

    return frozenset(IGNORED_DIRS | BUILD_OUTPUT_DIRS | _EXTRA_SKIP)


def find_python_files(root: Path, *, ignore: Iterable[str] = ()) -> Walk:
    """The ``*.py`` files under ``root``, sorted by relative path.

    Bounded: at most :data:`MAX_FILES` files, :data:`MAX_ENTRIES` directory entries and
    :data:`MAX_DEPTH` levels. Symbolic links (to a file or a directory) are never followed
    and are counted in the notes, so a link cannot lead the read out of the directory.
    Hidden directories and the ones forge-cli's own walks skip are left out, with
    ``tests``. ``ignore`` holds glob patterns over the relative path; a match (of a file or
    a directory) is left out.

    Raises:
        NotADirectoryError: ``root`` is not a directory.
    """
    import fnmatch

    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    skip = _skip_dirs()
    patterns = [str(p) for p in ignore]
    walk = Walk()
    links = 0
    entries = 0
    stack: List[Tuple[str, str, int]] = [(str(root), "", 0)]
    capped_files = capped_entries = capped_depth = False

    def ignored(rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, p) for p in patterns)

    while stack:
        directory, prefix, depth = stack.pop()
        try:
            with os.scandir(directory) as scan:
                listing = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            walk.notes.append(f"could not list {prefix or '.'}: skipped")
            continue
        subdirs: List[Tuple[str, str, int]] = []
        for entry in listing:
            entries += 1
            if entries > MAX_ENTRIES:
                capped_entries = True
                break
            name = entry.name
            rel = f"{prefix}{name}"
            try:
                if entry.is_symlink():
                    links += 1
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name in skip or name.startswith(".") or ignored(rel):
                    continue
                if depth + 1 > MAX_DEPTH:
                    capped_depth = True
                    continue
                subdirs.append((entry.path, f"{rel}/", depth + 1))
            elif is_file and name.endswith(".py") and not ignored(rel):
                if len(walk.files) >= MAX_FILES:
                    capped_files = True
                    continue
                walk.files.append(SourceFile(rel=rel, path=Path(entry.path)))
        stack.extend(reversed(subdirs))
        if capped_entries:
            break
    walk.files.sort(key=lambda f: f.rel)
    if links:
        walk.notes.append(f"{links} symbolic link(s) not followed")
    if capped_files:
        walk.notes.append(f"stopped at {MAX_FILES} Python files")
    if capped_entries:
        walk.notes.append(f"stopped after reading {MAX_ENTRIES} directory entries")
    if capped_depth:
        walk.notes.append(f"directories deeper than {MAX_DEPTH} levels not read")
    return walk


@dataclass
class SourceBudget:
    """How much source one scan reads in all (:data:`MAX_TOTAL_BYTES`): a scan of a directory
    full of files at the size limit costs what the sum of them costs, not what one does."""

    left: int = MAX_TOTAL_BYTES
    #: Files not read because the budget was spent.
    skipped: int = 0
    #: Characters of SQL still to parse (:data:`MAX_TOTAL_SQL_CHARS`), and statements left alone.
    sql_left: int = MAX_TOTAL_SQL_CHARS
    sql_skipped: int = 0
    #: Bytes of source that may still be read a second time (:data:`MAX_RELOAD_BYTES`).
    reload_left: int = MAX_RELOAD_BYTES

    def take_reload(self, size: int) -> bool:
        """Charge a file read again; ``False`` when the reloads have used their budget."""
        if size > self.reload_left:
            return False
        self.reload_left -= size
        return True

    def take_sql(self, size: int) -> bool:
        """Charge a statement of ``size`` characters; ``False`` when the SQL budget is spent."""
        if size > self.sql_left:
            self.sql_skipped += 1
            return False
        self.sql_left -= size
        return True

    def take(self, size: int) -> bool:
        if size > self.left:
            self.skipped += 1
            self.left = 0
            return False
        self.left -= size
        return True


def read_source(
    path: Path, budget: Optional[SourceBudget] = None, *, reload: bool = False
) -> bytes:
    """The bytes of a Python source file: a regular file of at most :data:`MAX_FILE_BYTES`,
    counted against ``budget`` when there is one (a file read again, ``reload``, against the
    budget for that, not the one the files of the project need).

    Raises:
        SourceUnreadable: with the reason (not a regular file, too large, unreadable, or the
            scan has read all the source it reads).
    """
    from fluid_build.util.safe_yaml import read_bounded_file

    try:
        data = read_bounded_file(path, MAX_FILE_BYTES)
    except OSError as exc:
        raise SourceUnreadable(f"cannot read it ({type(exc).__name__})") from exc
    if data is None:
        raise SourceUnreadable(f"it is larger than {MAX_FILE_BYTES:,} bytes")
    if budget is not None and reload:
        if not budget.take_reload(len(data)):
            raise SourceUnreadable(
                f"the scan has read modules again for {MAX_RELOAD_BYTES // (1024 * 1024)} MB, "
                "the most it re-reads"
            )
    elif budget is not None and not budget.take(len(data)):
        raise SourceUnreadable(
            f"the scan has read {MAX_TOTAL_BYTES // (1024 * 1024)} MB of source, the most it reads"
        )
    return data


def might_contain_dag(data: bytes) -> bool:
    """Airflow's own cheap test for "this may be a DAG file": the lower-cased bytes hold both
    ``dag`` and ``airflow`` (``airflow.utils.file.might_contain_dag_via_default_heuristic``).
    A file that fails it is not parsed."""
    lowered = data.lower()
    return b"dag" in lowered and b"airflow" in lowered


@dataclass
class Module:
    """One parsed source file."""

    rel: str
    tree: ast.Module
    imports: Dict[str, str]
    _parents: Optional[Dict[ast.AST, ast.AST]] = None
    #: The places a name is imported from, for each name the file imports from more than one (the
    #: table keeps the last), and ``"*"`` (no places) when it has a star import: any name may come
    #: from it.
    ambiguous: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    #: Bytes of source it was parsed from.
    size: int = 0
    #: How many syntax-tree nodes it has: what keeping it parsed costs.
    nodes: int = 0

    @property
    def parents(self) -> Dict[ast.AST, ast.AST]:
        if self._parents is None:
            self._parents = parent_map(self.tree)
        return self._parents


def _braces(data: bytes) -> int:
    """How many ``{`` the source has once it is decoded as Python decodes it: a coding cookie
    (``# coding: utf-7``) changes the bytes a brace is written in, and the parser follows it."""
    try:
        encoding, _lines = tokenize.detect_encoding(io.BytesIO(data).readline)
    except (SyntaxError, LookupError, ValueError):
        return data.count(b"{")  # a cookie the parser refuses too: it reports it
    if encoding.lower().replace("_", "-") in ("utf-8", "utf-8-sig"):
        return data.count(b"{")
    return data.decode(encoding, "replace").count("{")


def parse_module(rel: str, data: bytes) -> Module:
    """Parse ``data`` as a module. Nothing in it runs.

    Raises:
        SourceUnreadable: it is not valid Python for this interpreter, or too deeply nested
            to parse; the reason carries no source text.
    """
    if _braces(data) > MAX_BRACES:
        raise SourceUnreadable(
            f"it has more than {MAX_BRACES:,} '{{' characters, more than a job or a DAG file has: "
            "it is not read"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # invalid escape sequences, in a file we only read
            tree = ast.parse(data, filename=rel)
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if exc.lineno else ""
        raise SourceUnreadable(f"it is not valid Python{where}: {exc.msg}") from exc
    except (ValueError, RecursionError, MemoryError, OverflowError) as exc:
        raise SourceUnreadable(f"it cannot be parsed ({type(exc).__name__})") from exc
    return Module(
        rel=rel,
        tree=tree,
        imports=import_table(tree),
        ambiguous=ambiguous_imports(tree),
        size=len(data),
        nodes=sum(1 for _ in ast.walk(tree)),
    )


def import_table(tree: ast.AST) -> Dict[str, str]:
    """What each imported name stands for, over the whole module (also imports inside a
    function or a ``try``: the table is not scoped, so a name bound twice keeps the later
    import). ``import a.b as c`` gives ``c -> a.b``; ``from a import b as c`` gives
    ``c -> a.b``; a relative ``from .x import y`` keeps its dots (``.x.y``)."""
    table: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    table.setdefault(top, top)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not base:
                    full = alias.name
                elif base.endswith("."):  # `from . import x`, `from .. import x`
                    full = f"{base}{alias.name}"
                else:
                    full = f"{base}.{alias.name}"
                table[alias.asname or alias.name] = full
    return table


def ambiguous_imports(tree: ast.AST) -> Dict[str, FrozenSet[str]]:
    """The places each name that ``tree`` imports from more than one is imported from, and ``"*"``
    when it has a star import (see :attr:`Module.ambiguous`)."""
    places: Dict[str, set] = {}
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pairs = [((a.asname or a.name.split(".")[0]), a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            pairs = []
            for alias in node.names:
                if alias.name == "*":
                    star = True
                else:
                    pairs.append((alias.asname or alias.name, f"{base}.{alias.name}"))
        else:
            continue
        for name, target in pairs:
            places.setdefault(name, set()).add(target)
    found = {name: frozenset(where) for name, where in places.items() if len(where) > 1}
    if star:
        found["*"] = frozenset()
    return found


def qualified_name(node: ast.AST, imports: Dict[str, str]) -> Optional[str]:
    """The dotted name a ``Name`` or ``Attribute`` chain stands for, through the import table:
    ``F.col`` with ``import pyspark.sql.functions as F`` is ``pyspark.sql.functions.col``.
    ``None`` for any other expression."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = imports.get(node.id, node.id)
    return ".".join([head, *reversed(parts)])


def parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    """``{node: its parent}`` over ``tree``, built without recursion."""
    parents: Dict[ast.AST, ast.AST] = {}
    stack: List[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            stack.append(child)
    return parents


def ancestors(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> Iterable[ast.AST]:
    """``node``'s parents, nearest first."""
    while node in parents:
        node = parents[node]
        yield node


def clean_chars(text: object, *, keep: str = "") -> str:
    """``text`` without the characters that can rewrite what a terminal shows or hide what a
    reader sees: control characters (a new line, an escape sequence) and line and paragraph
    separators become spaces, and format characters (bidirectional marks, zero-width
    characters) are dropped. ``keep`` names control characters that stay (``"\\n\\t"`` for a
    command written over several lines). Text from the scanned code goes through this before it
    is printed or written."""
    value = str(text)
    if value.isprintable():
        return value
    out: List[str] = []
    for char in value:
        if char in keep:
            out.append(char)
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Zl", "Zp"):
            out.append(" ")
        elif category == "Cs":
            out.append(chr(0xFFFD))  # a lone surrogate cannot be written out as text
        elif category != "Cf":
            out.append(char)
    return "".join(out)


#: A snippet shows at most 80 characters, so an expression of more than this many nodes, or with
#: more than this many characters of text in its strings, is not unparsed to be cut short.
_SNIPPET_NODES = 200
_SNIPPET_TEXT = 2_000


def _is_large(node: ast.AST) -> bool:
    """Whether ``node`` is too big to be worth unparsing for a short description: unparsing a
    subtree costs its size, and a report line may ask for it at every level. Counted on the tree,
    not on lines and columns (a node on 40 lines of 7 KB each is as costly as one line of 280 KB),
    and the count stops at the limit, so it costs at most that much itself. The answer is kept on
    the node: a name used a hundred times is judged once."""
    kept = getattr(node, "_fluid_large", None)
    if kept is not None:
        return bool(kept)
    nodes = text = 0
    large = False
    stack = [node]
    while stack and not large:
        current = stack.pop()
        nodes += 1
        if isinstance(current, ast.Constant) and isinstance(current.value, (str, bytes)):
            text += len(current.value)
        large = nodes > _SNIPPET_NODES or text > _SNIPPET_TEXT
        stack.extend(ast.iter_child_nodes(current))
    node._fluid_large = large  # type: ignore[attr-defined]
    return large


def snippet(node: ast.AST, limit: int = 80) -> str:
    """A short, single-line rendering of an expression, for a report. Never raises: a tree
    too deep to unparse is shown as ``<expression>``."""
    if _is_large(node):
        return "<expression>"
    try:
        text = ast.unparse(node)
    except (RecursionError, ValueError, MemoryError):
        return "<expression>"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
