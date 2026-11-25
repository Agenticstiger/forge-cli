# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A consumer query is checked with sqlglot before anything runs it.

A query or view in a profile request is untrusted text. This module reads it with sqlglot,
refuses it, naming the construct and never echoing the text, unless every part of it is on a
closed list (:mod:`._allow`), and writes it back as one canonical DuckDB text. Nothing here runs
SQL: the canonical text it returns is what the locked child (:mod:`.runner`) runs later.

The checks, in order
--------------------

1. **Bounded read.** sqlglot's tokenizer runs first. A text of more than 20,000 tokens, or with
   brackets nested more than 64 deep, is refused before the parser runs.
2. **One query.** ``sqlglot.parse`` (the DuckDB dialect) must find exactly one statement, and it
   must be a ``SELECT``, a set operation, or ``WITH`` before one. DDL, DML, ``PRAGMA``, ``SET``,
   ``ATTACH``, ``COPY``, ``INSTALL``, ``LOAD``, ``EXPORT``, ``CALL`` and anything sqlglot reads as
   a command are refused.
3. **Closed lists.** Every node, argument, function and type is on an allowlist; more than 10,000
   nodes or 64 levels is refused. A table function, an environment, setting, catalog, sequence
   or random function, the clock and a placeholder are not on it. A table written as a string or
   a path is refused here too.
4. **Tables.** Each table is one of the exposeIds the request lists, or a ``WITH`` entry the text
   defines before it. A name that is neither, a name with a schema or catalog part, a ``WITH``
   that shadows an expose and an expose with a twin that differs only in case (DuckDB reads both
   as one name) are refused. ``WITH RECURSIVE`` is refused.
5. **Columns.** sqlglot qualifies the tree against the listed columns, expands stars and resolves
   each column to its table; a column in no listed table is refused. A projection alias read in
   its own SELECT is refused (it would be written out in place, and the tree would double).
6. **Total orders** (:mod:`._order`). ``LIMIT``, ``OFFSET`` and a window ``ORDER BY`` must list a
   key the contract declares unique, or every column of the rows.
7. **The same text, twice.** The canonical text is ``tree.sql(dialect="duckdb",
   comments=False)``, so a comment never carries a second statement. That text is parsed again
   and passes every check once more (steps 1 to 3 and 6, with the larger caps of a qualified
   text), and the tables it reads must be the tables the first tree read. The tree that is
   returned is that second tree.

A hostile text ends in a refusal and never in a raise: a ``RecursionError`` is refused as
``recursion`` and any other error nobody named as ``internal``, with the exception's class name
and nothing of the text.

Adapted from FLUX ``flux_engine/consumers/check.py`` at 19dfe48 (Apache-2.0).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.tokens import Token, TokenType

from fluid_build.profiler.consumer_queries import _allow
from fluid_build.profiler.consumer_queries._bounds import (
    refuse_alias_references,
    refuse_long_patterns,
    refuse_wide_expansion,
)
from fluid_build.profiler.consumer_queries._order import check_total_orders
from fluid_build.profiler.consumer_queries._types import (
    Checked,
    Refusal,
    Refused,
    Table,
    duckdb_type,
)

__all__ = ["SQLGLOT_VERSION", "check_text"]

#: The sqlglot version every canonical text is written by.
SQLGLOT_VERSION: str = str(sqlglot.__version__)

_KINDS = ("query", "view")
_OPEN = frozenset({TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE})
_CLOSE = frozenset({TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.R_BRACE})
_STRINGS = frozenset(
    {
        TokenType.STRING,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.NATIONAL_RAW_STRING,
        TokenType.HEREDOC_STRING,
        TokenType.UNICODE_STRING,
        TokenType.BYTE_STRING,
    }
)
_EXPOSE = "consumer_expose"
_DUCKDB = "duckdb"
#: The names of the listed tables a refusal may list, at most.
_MOST_LISTED = 20


def check_text(text: str, *, kind: str, tables: Mapping[str, Table]) -> Checked | Refused:
    """Check *text* against the listed *tables*; return it in canonical form, or refuse it.

    *kind* is ``"query"`` or ``"view"`` and only words the reasons (the checks are the same).
    *tables* maps each exposeId the request lists to its columns and contract types. Never
    raises for a hostile text: every refusal is a :class:`Refused` with a code and a reason that
    names the construct, a table, a column or a function, and never the text itself.
    """
    if kind not in _KINDS:
        raise ValueError(f"kind is one of {_KINDS}")
    label = f"the {kind}"
    try:
        return _Checker(tables, kind, label).check(text)
    except Refusal as refusal:
        return Refused(kind, refusal.code, refusal.detail)
    except RecursionError:
        return Refused(kind, "recursion", f"{label} nests deeper than sqlglot can read or write it")
    except Exception as exc:  # a hostile text must end in a refusal, never in a raise
        return Refused(kind, "internal", f"{label}: the check stopped on a {type(exc).__name__}")


# ---------------------------------------------------------------------------------------------
# The text: tokens, statements, caps
# ---------------------------------------------------------------------------------------------


def _where(exc: BaseException) -> str:
    """Where a parse failed, as line and column; never what sqlglot said about the text."""
    if isinstance(exc, ParseError) and exc.errors:
        first = exc.errors[0]
        return f" (line {first.get('line')}, column {first.get('col')})"
    return ""


def _scan_tokens(
    dialect: Dialect, sql: str, label: str, caps: _allow.Caps
) -> FrozenSet[Tuple[int, int]]:
    """Count the tokens and the bracket depth of *sql*; return the spans of its string tokens."""
    try:
        tokens: List[Token] = dialect.tokenize(sql)
    except RecursionError:
        raise
    except Exception as exc:  # sqlglot's tokenizer raises its own errors and, on odd input, others
        raise Refusal("unparsable", f"{label} does not tokenize ({type(exc).__name__})") from None
    if len(tokens) > caps.tokens:
        raise Refusal("bounds", f"{label} holds more than {caps.tokens} tokens")
    depth = 0
    strings: Set[Tuple[int, int]] = set()
    for token in tokens:
        kind = token.token_type
        if kind in _OPEN:
            depth += 1
            if depth > caps.brackets:
                raise Refusal("bounds", f"{label} nests brackets more than {caps.brackets} deep")
        elif kind in _CLOSE:
            depth = max(0, depth - 1)
        elif kind in _STRINGS:
            strings.add((token.start, token.end))
    return frozenset(strings)


def _refuse_string_tables(tree: exp.Expr, strings: FrozenSet[Tuple[int, int]], label: str) -> None:
    """Refuse a table that the text wrote as a string, such as ``FROM 'x.parquet'``.

    sqlglot reads such a table as a quoted name, which a quoted name cannot be told from, so the
    identifier's position is matched against the string tokens of the text.
    """
    for table in tree.find_all(exp.Table):
        for key in ("catalog", "db", "this"):
            part = table.args.get(key)
            if isinstance(part, exp.Identifier):
                span = (part.meta.get("start"), part.meta.get("end"))
                if span in strings:
                    raise Refusal(
                        "table", f"{label} names a table with a string, as a path or a file"
                    )


def _parse(sql: str, label: str) -> exp.Expr:
    try:
        parsed = sqlglot.parse(sql, read=_DUCKDB)
    except RecursionError:
        raise
    except Exception as exc:  # ParseError, TokenError and whatever else a hostile text provokes
        raise Refusal(
            "unparsable", f"{label} does not parse ({type(exc).__name__}){_where(exc)}"
        ) from None
    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise Refusal("several_statements", f"{label} holds no statement")
    if len(statements) > 1:
        raise Refusal(
            "several_statements",
            f"{label} holds {len(statements)} statements; a query is exactly one",
        )
    root = statements[0]
    if type(root) not in _allow.QUERY_ROOTS:
        kind = type(root).__name__
        if isinstance(root, exp.Command):
            kind = f"{kind} {_allow.name_of(str(root.this).upper(), most=24)}"
        raise Refusal("not_a_query", f"{label} is not a query: it is {kind}")
    return root


# ---------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------


def _fold(name: str) -> str:
    """The case a name is compared in: the one sqlglot folds to, so that both read one name."""
    return name.lower()


def _visible_ctes(node: exp.Expr) -> Set[str]:
    """The WITH names, folded, that a table at *node* may read: those defined before it."""
    names: Set[str] = set()
    child: exp.Expr = node
    parent = child.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            for cte in parent.expressions:
                if cte is child:
                    break
                names.add(_fold(str(cte.alias)))
        else:
            with_ = parent.args.get("with_")
            if isinstance(with_, exp.With) and with_ is not child:
                names.update(_fold(str(cte.alias)) for cte in with_.expressions)
        child, parent = parent, parent.parent
    return names


def _same_part(written: str, part: exp.Identifier) -> bool:
    """Quoted names compare exactly, unquoted names without regard to case."""
    if part.args.get("quoted"):
        return str(part.name) == written
    return _fold(str(part.name)) == _fold(written)


@dataclass(frozen=True)
class _ExposeColumns:
    """The columns of one listed table."""

    #: A column name (folded) to the contract's own spelling.
    exact: Mapping[str, str]
    #: The columns qualify sees: contract name to DuckDB type.
    visible: Mapping[str, str]


@dataclass(frozen=True)
class _Built:
    text: str
    tree: exp.Expr
    outputs: Tuple[str, ...]
    exposes: Tuple[str, ...]


class _Checker:
    """The listed tables and the checks of one text."""

    def __init__(self, tables: Mapping[str, Table], kind: str, label: str) -> None:
        self._tables = tables
        self._kind = kind
        self._label = label
        self._duckdb = Dialect.get_or_raise(_DUCKDB)
        self._columns: Dict[str, _ExposeColumns] = {}
        for name in tables:
            self._columns[name] = self._expose_columns(name, tables[name])

    @staticmethod
    def _expose_columns(expose_id: str, table: Table) -> _ExposeColumns:
        if not table.columns:
            raise Refusal("schema", f"the table {_allow.name_of(expose_id)!r} lists no column")
        exact: Dict[str, str] = {}
        visible: Dict[str, str] = {}
        for name, contract_type in table.columns:
            if _fold(name) in exact:
                raise Refusal(
                    "schema",
                    f"the table {_allow.name_of(expose_id)!r} has two columns that DuckDB reads "
                    f"as the name {_allow.name_of(name)!r}",
                )
            exact[_fold(name)] = name
            visible[name] = duckdb_type(contract_type) or "UNKNOWN"
        return _ExposeColumns(exact=exact, visible=visible)

    # -- reading a text ---------------------------------------------------------------------

    def check(self, sql: str) -> Checked:
        label = self._label
        if "\x00" in sql:
            raise Refusal("unparsable", f"{label} holds a NUL character")
        strings = _scan_tokens(self._duckdb, sql, label, _allow.SOURCE_CAPS)
        tree = _parse(sql, label)
        _allow.vet_tree(tree, label=label, caps=_allow.SOURCE_CAPS)
        _refuse_string_tables(tree, strings, label)
        refuse_long_patterns(tree, label=label)
        self._resolve_tables(tree)
        built = self._build(tree)
        columns = self._columns_read(built.tree)
        return Checked(
            kind=self._kind,
            text=built.text,
            digest="sha256:" + hashlib.sha256(built.text.encode("utf-8")).hexdigest(),
            tables=built.exposes,
            columns=columns,
            output_columns=built.outputs,
            sqlglot_version=SQLGLOT_VERSION,
            tree=built.tree,
        )

    # -- tables -----------------------------------------------------------------------------

    def _resolve_tables(self, tree: exp.Expr) -> None:
        """Rewrite each table onto its listed exposeId; refuse the others."""
        for table in [node for node in tree.walk() if isinstance(node, exp.Table)]:
            parts = [
                part
                for part in (table.args.get("catalog"), table.args.get("db"), table.this)
                if part is not None
            ]
            expose = self._resolve(parts, _visible_ctes(table))
            if expose is not None:
                self._rewrite_expose(table, parts, expose)

    @staticmethod
    def _rewrite_expose(table: exp.Expr, parts: List[exp.Identifier], expose: str) -> None:
        if table.args.get("alias") is None:
            last = parts[-1]
            table.set(
                "alias",
                exp.TableAlias(
                    this=exp.Identifier(this=str(last.name), quoted=bool(last.args.get("quoted")))
                ),
            )
        table.set("catalog", None)
        table.set("db", None)
        table.set("this", exp.Identifier(this=expose, quoted=True))
        table.meta[_EXPOSE] = expose

    def _resolve(self, parts: List[exp.Identifier], visible: Set[str]) -> Optional[str]:
        """The exposeId a table name is, ``None`` for a WITH entry; a refusal for anything else."""
        label = self._label
        written = _allow.name_of(".".join(str(part.name) for part in parts))
        if len(parts) > 1:
            raise Refusal(
                "table",
                f"{label} names the table {written!r} with a schema or catalog part; "
                "write the exposeId alone",
            )
        part = parts[0]
        found = {name for name in self._tables if _same_part(name, part)}
        cte = _fold(str(part.name)) in visible
        if found and cte:
            raise Refusal(
                "ambiguous_table",
                f"{label} names {written!r}, which is a listed table and a WITH entry that "
                "shadows it; rename the entry or quote the name",
            )
        if len(found) > 1:
            raise Refusal(
                "ambiguous_table",
                f"{label} names {written!r}, which matches the tables {sorted(found)[:_MOST_LISTED]}",
            )
        if found:
            expose = next(iter(found))
            twins = sorted(
                other
                for other in self._tables
                if other != expose and other.casefold() == expose.casefold()
            )
            if twins:
                raise Refusal(
                    "ambiguous_table",
                    f"{label} names {written!r}, which is the table {_allow.name_of(expose)!r}; "
                    f"{twins[:_MOST_LISTED]} differs from it only in case, and DuckDB cannot "
                    "tell them apart",
                )
            return expose
        if cte:
            return None
        raise Refusal(
            "table",
            f"{label} names the table {written!r}, which is not one of the tables the request "
            f"lists ({sorted(self._tables)[:_MOST_LISTED]}) and not a WITH entry",
        )

    # -- qualifying, renaming and the second reading ------------------------------------------

    def _build(self, prepared: exp.Expr) -> _Built:
        label = self._label
        tree = prepared.copy()
        exposes = sorted(
            {
                str(node.meta[_EXPOSE])
                for node in tree.walk()
                if isinstance(node, exp.Table) and _EXPOSE in node.meta
            }
        )
        schema: Dict[str, object] = {name: dict(self._columns[name].visible) for name in exposes}
        widths = {name: len(self._columns[name].visible) for name in exposes}
        qualified = self._qualify(tree, schema, widths)
        outputs = _named_selects(qualified)
        self._restore_names(qualified, exposes)
        by_fold = {_fold(name): self._tables[name] for name in exposes}
        check_total_orders(qualified, by_fold, label=label)
        first = self._table_refs(qualified)
        text = qualified.sql(dialect=_DUCKDB, comments=False)
        second = self._reread(text, first, by_fold)
        return _Built(
            text=text,
            tree=second,
            outputs=outputs,
            exposes=tuple(sorted({name for kind, name in first if kind == "expose"})),
        )

    def _qualify(
        self, tree: exp.Expr, schema: Dict[str, object], widths: Mapping[str, int]
    ) -> exp.Expr:
        # No alias is ever expanded: a text that reads one is refused first, and qualify is told
        # not to expand. The stars and positional GROUP BY keys qualify does write out are
        # counted first, so the tree it writes stays within the canonical caps.
        label = self._label
        refuse_alias_references(tree, label=label)
        try:
            refuse_wide_expansion(tree, widths, label=label, most=_allow.CANONICAL_CAPS.nodes)
            qualified = qualify(tree, schema=schema, dialect=_DUCKDB, expand_alias_refs=False)
            annotate_types(qualified, schema=schema, dialect=_DUCKDB)
        except (Refusal, RecursionError):
            raise
        except Exception as exc:  # OptimizeError, and whatever else sqlglot raises on odd trees
            raise Refusal(
                "column",
                f"{label} names a column that is in none of the tables it reads, or reads one "
                f"ambiguously ({type(exc).__name__})",
            ) from None
        for star in qualified.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise Refusal("column", f"{label} holds a star that cannot be expanded")
        return qualified

    def _restore_names(self, tree: exp.Expr, exposes: List[str]) -> None:
        """Tables take their exact ids and columns of an expose their exact contract names.

        qualify folds every name to lower case, which DuckDB reads as the same name; the canonical
        text spells each as the contract does.
        """
        known = {_fold(name): name for name in exposes}
        root = build_scope(tree)
        if root is not None:
            for scope in root.traverse():
                for column in scope.columns:
                    self._restore_column(scope, column, known)
        for table in tree.find_all(exp.Table):
            exact = known.get(_fold(str(table.name)))
            if exact is not None:
                table.set("this", exp.Identifier(this=exact, quoted=True))

    def _restore_column(self, scope: Scope, column: exp.Column, known: Mapping[str, str]) -> None:
        if not column.table:
            return
        source = _source_of(scope, str(column.table))
        if not isinstance(source, exp.Table):
            return
        expose = known.get(_fold(str(source.name)))
        if expose is None:
            return
        exact = self._columns[expose].exact.get(_fold(str(column.name)))
        if exact is not None:
            column.set("this", exp.Identifier(this=exact, quoted=True))

    def _table_refs(self, tree: exp.Expr) -> List[Tuple[str, str]]:
        label = self._label
        refs: List[Tuple[str, str]] = []
        for node in tree.walk():
            if not isinstance(node, exp.Table):
                continue
            if node.args.get("db") is not None or node.args.get("catalog") is not None:
                raise Refusal(
                    "canonical", f"{label}: the canonical text qualifies a table by a schema"
                )
            name = str(node.name)
            if _fold(name) in _visible_ctes(node):
                refs.append(("cte", _fold(name)))
            elif name in self._tables:
                refs.append(("expose", name))
            else:
                raise Refusal(
                    "canonical",
                    f"{label}: the canonical text reads {_allow.name_of(name)!r}, which is no "
                    "listed table or WITH entry",
                )
        return sorted(refs)

    def _reread(
        self, text: str, first: List[Tuple[str, str]], by_fold: Mapping[str, Table]
    ) -> exp.Expr:
        """Parse the canonical text again, in DuckDB's dialect, and check it as a new text."""
        label = self._label
        try:
            strings = _scan_tokens(self._duckdb, text, label, _allow.CANONICAL_CAPS)
            second = _parse(text, label)
            _allow.vet_tree(second, label=label, caps=_allow.CANONICAL_CAPS)
            _refuse_string_tables(second, strings, label)
            again = self._table_refs(second)
            if again != first:
                raise Refusal(
                    "canonical",
                    f"{label}: the canonical text reads other tables than the checked tree",
                )
            check_total_orders(second, by_fold, label=label)
        except Refusal as exc:
            if exc.code == "canonical":
                raise
            raise Refusal(
                "canonical",
                f"{label}: the canonical text fails the check ({exc.code}) on the second "
                f"reading: {exc.detail}",
            ) from None
        return second

    # -- what the text reads --------------------------------------------------------------------

    def _columns_read(self, tree: exp.Expr) -> Tuple[Tuple[str, str], ...]:
        """The ``(exposeId, column)`` pairs the canonical tree reads, sorted."""
        found: Set[Tuple[str, str]] = set()
        root = build_scope(tree)
        if root is None:
            return ()
        for scope in root.traverse():
            for column in scope.columns:
                if not column.table:
                    continue
                source = _source_of(scope, str(column.table))
                if not isinstance(source, exp.Table) or str(source.name) not in self._tables:
                    continue
                expose = str(source.name)
                exact = self._columns[expose].exact.get(_fold(str(column.name)))
                if exact is not None:
                    found.add((expose, exact))
        return tuple(sorted(found))


def _source_of(scope: Scope, alias: str) -> Any:
    """What the name *alias* stands for in *scope* or one above it: a table, a scope or ``None``."""
    walker: Optional[Scope] = scope
    while walker is not None:
        source = walker.sources.get(alias)
        if source is not None:
            return source
        walker = walker.parent
    return None


def _named_selects(tree: exp.Expr) -> Tuple[str, ...]:
    """The names of the rows a query returns."""
    return tuple(str(name) for name in tree.named_selects) if isinstance(tree, exp.Query) else ()
