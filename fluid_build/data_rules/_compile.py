# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Compile a contract's rules about valid data into SQL counts (``RFC-data-rules.md``).

:func:`compile_rules` turns each declaration into one :class:`Rule`: the declared
invariants (a ``check``, a ``derive`` with ``expr`` or ``aggregate``, a ``dependency``,
counted once per direction with ``bothWays``), each column's identifier formats and
placeholders, each relationship's ``sharedColumns`` and each member of a
``sharedAttribute`` other than its owner. A rule is one of:

- ``executable``: ``counts(table_sql)`` is one SQL query in the rule's dialect that
  returns two columns, ``covered`` and ``violating`` (§1.3, §1.5), and
  :meth:`Rule.measure` the query that returns the figure the band compares: violating
  rows, or violating rows times 100 divided by covered rows (0 when none is covered).
  An identifier format carries its anchored ``pattern`` (§3) instead;
- ``informational``: declared and not bounded (placeholders without an operator);
- ``unsupported``: ``reason`` says why no exact SQL exists (the validator refuses the
  declaration, the platform has no FLUID SQL/1 dialect, a link crosses platforms ...).
  A consumer reports it and never approximates it.

Every query is built from the parsed tree and the contract's names, quoted for the
dialect by :mod:`fluid_build.providers._sql_safety`; nothing is substituted into a
template. ``table_sql(exposeId)`` is the caller's: the ODCS exporter answers
``${table}`` for the rule's own expose and a quoted object name for the others.

**Links.** A path ``rel.col``, a shared column and each hop of a shared attribute's
route are a ``LEFT JOIN`` to the target's rows whose key occurs once (a window count
over the target's key), so a missing row and a key two target rows hold both read as
NULL, and a source row is never counted twice.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql import _nodes as n
from fluid_build.fluid_sql._types import NUMBER, STRING

from . import _masks
from ._lint import OPERATORS, AttributeRoutes, Finding, analyze
from ._model import Expose, as_dict, as_list, build, nfc

EXECUTABLE = "executable"
INFORMATIONAL = "informational"
UNSUPPORTED = "unsupported"

TableSql = Callable[[str], str]

#: ODCS quality dimensions (Consumer impact matrix).
CONSISTENCY, CONFORMITY, COMPLETENESS = "consistency", "conformity", "completeness"


@dataclass(frozen=True)
class Band:
    """The expected number of violating rows (§1.4): one ODCS operator and its unit."""

    operator: str
    value: Any
    unit: str = "rows"


NO_VIOLATION = Band("mustBe", 0, "rows")


@dataclass(frozen=True)
class Rule:
    """One compiled declaration. ``id`` is the invariant's own id, or one derived from
    the column or relationship it sits on; ``column`` is set for a column's own rule."""

    id: str
    kind: str
    expose: str
    path: Tuple[Any, ...]
    dimension: str
    description: str
    status: str
    reason: str = ""
    severity: str = "error"
    band: Optional[Band] = NO_VIOLATION
    column: Optional[str] = None
    dialect: Optional[str] = None
    #: The other exposes the SQL reads (ODCS ``transformSourceObjects``, dbt ``ref()``s).
    reads: Tuple[str, ...] = ()
    #: An identifier format's anchored regex (placeholders accepted).
    pattern: Optional[str] = None
    counts: Optional[Callable[[TableSql], str]] = field(default=None, compare=False, repr=False)

    def measure(self, table_sql: TableSql) -> str:
        """The query whose one value the band compares (``covered``/``violating`` → a
        count of rows, or a percent of the covered rows)."""
        if self.counts is None or self.dialect is None:
            raise ValueError(f"rule {self.id} has no SQL: {self.reason or self.status}")
        q = lambda name: fs.sql_name(self.dialect, name)  # noqa: E731
        inner = f"({self.counts(table_sql)}) AS {q('fluid_counts')}"
        if self.band is not None and self.band.unit == "percent":
            return (
                f"SELECT CASE WHEN {q('covered')} = 0 THEN 0 "
                f"ELSE 100.0 * {q('violating')} / {q('covered')} END FROM {inner}"
            )
        return f"SELECT {q('violating')} FROM {inner}"


# -- platforms ---------------------------------------------------------------------------

_AWS_QUERYABLE = frozenset({"athena_table", "glue_table", "iceberg"})


def platform_dialect(expose: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    """The FLUID SQL/1 dialect an expose's ``binding`` is queried in, or ``(None, why)``."""
    binding = as_dict(expose.get("binding"))
    platform = str(binding.get("platform") or "").strip().lower()
    fmt = str(binding.get("format") or "").strip().lower()
    if platform == "local":
        return "duckdb", ""
    if platform in ("snowflake", "databricks"):
        return platform, ""
    if platform in ("postgres", "pgvector"):
        return "postgres", ""
    if platform == "gcp" and fmt in ("", "bigquery_table"):
        return "bigquery", ""
    if platform == "aws" and fmt in _AWS_QUERYABLE:
        return "athena", ""
    shown = f"{platform or 'no platform'}" + (f" ({fmt})" if fmt else "")
    return None, (
        f"its binding is {shown}, which forge writes no FLUID SQL/1 dialect for "
        f"(it writes {', '.join(fs.DIALECTS)})"
    )


# -- compile -----------------------------------------------------------------------------


def compile_rules(contract: Mapping[str, Any], dialect: Optional[str] = None) -> List[Rule]:
    """Every rule about valid data in ``contract``, in contract order.

    ``dialect`` renders every rule for one engine (a dbt adapter's); without it, each
    rule is rendered in its expose's platform dialect (:func:`platform_dialect`).
    Nothing below ``fluidVersion`` 0.7.7, where the fields do not exist.
    """
    from . import FIRST_VERSION, _version

    if not isinstance(contract, Mapping):
        return []
    version = _version(contract)
    if not version or version < FIRST_VERSION:
        return []
    if dialect is not None and dialect not in fs.DIALECTS:
        raise ValueError(f"FLUID SQL/1 renders no SQL for dialect {dialect!r}")
    findings, routes = analyze(contract)
    return _Compiler(contract, findings, routes, dialect).run()


def _literal(value: Any) -> str:
    """A number from the contract as exact SQL text (never ``1e-07``)."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return str(value)
    return format(Decimal(repr(value)) if isinstance(value, float) else Decimal(value), "f")


class _Refused(Exception):
    """A declaration with no exact SQL: the message is the reason."""


class _Compiler:
    def __init__(
        self,
        contract: Mapping[str, Any],
        findings: List[Finding],
        routes: Dict[str, AttributeRoutes],
        dialect: Optional[str],
    ) -> None:
        self.contract = contract
        self.errors = [f for f in findings if f.severity == "error"]
        self.routes = routes
        self.dialect = dialect
        self.exposes = build(contract)
        self.by_id = {e.expose_id: e for e in self.exposes if e.expose_id}
        self.rules: List[Rule] = []

    # -- shared helpers ------------------------------------------------------------------

    def refusal(self, path: Tuple[Any, ...]) -> Optional[str]:
        for finding in self.errors:
            if finding.path[: len(path)] == path:
                return f"the validator refuses it ({finding.display})"
        return None

    def dialect_of(self, expose: Expose) -> str:
        if self.dialect is not None:
            return self.dialect
        dialect, why = platform_dialect(expose.raw)
        if dialect is None:
            raise _Refused(why)
        return dialect

    def same_dialect(self, dialect: str, other: Expose, how: str) -> None:
        if self.dialect is not None:
            return
        theirs, _ = platform_dialect(other.raw)
        if theirs != dialect:
            raise _Refused(
                f"it reads {other.label} {how}, which is queried in "
                f"{theirs or 'no dialect'}, not {dialect}: one query cannot read both"
            )

    def add(self, build: Callable[[], Rule], fallback: Dict[str, Any]) -> None:
        """Add the rule ``build`` makes, or an unsupported rule with the reason it fails."""
        refused = self.refusal(fallback["path"])
        if refused is None:
            try:
                self.rules.append(build())
                return
            except _Refused as why:
                refused = str(why)
            except (fs.FluidSqlError, _masks.MaskError, ValueError) as why:
                refused = str(why) or type(why).__name__
        self.rules.append(Rule(status=UNSUPPORTED, reason=refused, **fallback))

    def run(self) -> List[Rule]:
        for expose in self.exposes:
            self.invariants(expose)
            self.columns(expose)
            self.shared_columns(expose)
        self.shared_attributes()
        return self.rules

    # -- invariants ----------------------------------------------------------------------

    def invariants(self, expose: Expose) -> None:
        derives: Dict[str, List[Optional[n.Node]]] = {}  # earlier whens of each derived column
        for j, inv in enumerate(as_list(expose.contract.get("invariants"))):
            if not isinstance(inv, Mapping):
                continue
            inv_id = inv.get("id") if isinstance(inv.get("id"), str) else f"invariants_{j}"
            forms = [key for key in ("check", "derive", "dependency") if key in inv]
            kind = forms[0] if len(forms) == 1 else "invariant"
            if kind == "derive" and "aggregate" in inv:
                kind = "aggregate"
            path = ("exposes", expose.index, "contract", "invariants", j)
            severity = inv.get("severity") if isinstance(inv.get("severity"), str) else "error"
            band = _band(inv)
            fallback = dict(
                id=inv_id,
                kind=kind,
                expose=expose.expose_id,
                path=path,
                dimension=CONSISTENCY,
                description=_describe(inv),
                severity=severity,
                band=band,
            )
            earlier: List[Optional[n.Node]] = []
            if forms == ["derive"] and isinstance(inv.get("derive"), str):
                target = nfc(inv["derive"])
                earlier = list(derives.get(target, []))
                when = _parse(inv["when"]) if isinstance(inv.get("when"), str) else None
                derives.setdefault(target, []).append(when)
            if len(forms) != 1:
                self.rules.append(
                    Rule(
                        status=UNSUPPORTED,
                        reason="the entry has no form, or two (check, derive or dependency)",
                        **fallback,
                    )
                )
                continue
            if kind == "dependency":
                dep = as_dict(inv.get("dependency"))
                self.add(lambda: self.dependency(expose, dep, fallback, False), fallback)
                if dep.get("bothWays") is True:
                    reverse = dict(
                        fallback,
                        id=f"{inv_id}-reverse",
                        description=fallback["description"] + " (the reverse direction)",
                    )
                    self.add(lambda: self.dependency(expose, dep, reverse, True), reverse)
                continue
            self.add(lambda: self.row_rule(expose, inv, kind, earlier, fallback), fallback)

    def row_rule(
        self,
        expose: Expose,
        inv: Mapping[str, Any],
        kind: str,
        earlier: List[Optional[n.Node]],
        fallback: Dict[str, Any],
    ) -> Rule:
        dialect = self.dialect_of(expose)
        query = _RowQuery(self, expose, dialect)
        own = query.checked(inv["when"], fs.PREDICATE) if "when" in inv else None
        covered = query.coverage(own, earlier)
        if kind == "check":
            node, checked = query.checked(inv["check"], fs.PREDICATE)
            violated = f"(NOT {query.render(node, checked, predicate=True)})"
        elif kind == "derive":
            target = nfc(inv.get("derive"))
            value = fs.parse(inv["expr"])
            node = n.DistinctFrom(n.Column(target), value, negated=True)
            checked = fs.check(node, expose.model, fs.PREDICATE)
            query.uses(checked)
            violated = f"(NOT {query.render(node, checked, predicate=True)})"
        else:
            violated = query.aggregate(nfc(inv.get("derive")), as_dict(inv.get("aggregate")))
        return Rule(
            status=EXECUTABLE,
            dialect=dialect,
            reads=query.reads(),
            counts=query.counts(covered, violated),
            **fallback,
        )

    def dependency(
        self, expose: Expose, dep: Mapping[str, Any], fallback: Dict[str, Any], reverse: bool
    ) -> Rule:
        """The g3 count (§1.5): per key, its rows minus the rows of its most common ``to``."""
        dialect = self.dialect_of(expose)
        q = lambda name: fs.sql_name(dialect, name)  # noqa: E731
        sides = [
            [nfc(c) for c in as_list(dep.get(s)) if isinstance(c, str)] for s in ("from", "to")
        ]
        key, value = (sides[1], sides[0]) if reverse else (sides[0], sides[1])
        if not key or not value:
            raise _Refused("the dependency names no columns")
        names = _Names(expose.model.columns)
        rows, top = names.fresh("fluid_rows"), names.fresh("fluid_top")
        alias = q("t")
        grouped = ", ".join(f"{alias}.{q(c)}" for c in key + value)
        not_null = " AND ".join(
            f"{alias}.{q(c)} IS NOT NULL" for c in (key + value if reverse else key)
        )
        g = q("fluid_groups")

        def counts(table_sql: TableSql) -> str:
            return (
                f"SELECT COALESCE(SUM({q(rows)}), 0) AS {q('covered')}, "
                f"COALESCE(SUM({q(rows)} - {q(top)}), 0) AS {q('violating')} "
                f"FROM (SELECT SUM({q(rows)}) AS {q(rows)}, MAX({q(rows)}) AS {q(top)} "
                f"FROM (SELECT {grouped}, COUNT(*) AS {q(rows)} "
                f"FROM {table_sql(expose.expose_id)} AS {alias} WHERE {not_null} "
                f"GROUP BY {grouped}) AS {g} "
                f"GROUP BY {', '.join(f'{g}.{q(c)}' for c in key)}) AS {q('fluid_keys')}"
            )

        return Rule(status=EXECUTABLE, dialect=dialect, counts=counts, **fallback)

    # -- columns -------------------------------------------------------------------------

    def columns(self, expose: Expose) -> None:
        for name, (position, column) in expose.columns.items():
            base = ("exposes", expose.index, "contract", "schema", position)
            if isinstance(column.get("identifierFormat"), list):
                fallback = dict(
                    id=f"{name}.identifierFormat",
                    kind="identifierFormat",
                    expose=expose.expose_id,
                    column=name,
                    path=base + ("identifierFormat",),
                    dimension=CONFORMITY,
                    description=f"Values of {name} have the column's identifier format",
                )
                self.add(
                    lambda c=column, f=fallback: self.identifier_format(expose, c, f), fallback
                )
            if isinstance(column.get("placeholders"), Mapping):
                block = column["placeholders"]
                fallback = dict(
                    id=f"{name}.placeholders",
                    kind="placeholders",
                    expose=expose.expose_id,
                    column=name,
                    path=base + ("placeholders",),
                    dimension=COMPLETENESS,
                    description=_describe_placeholders(name, block),
                    band=_band(block, default=None),
                )
                self.add(lambda f=fallback, b=block: self.placeholders(expose, f, b), fallback)

    def identifier_format(
        self, expose: Expose, column: Mapping[str, Any], fallback: Dict[str, Any]
    ) -> Rule:
        alternatives = [_masks.alternative(entry) for entry in column["identifierFormat"]]
        values, prefixes = self.exemptions(expose, fallback["column"])
        exempt = [_masks.literal_regex(v) for v in values]
        exempt += [_masks.literal_regex(p) + ".*" for p in prefixes]
        body = _masks.column_body(alternatives)
        pattern = "^" + ("(" + "|".join([body, *exempt]) + ")" if exempt else body) + "$"
        return Rule(status=EXECUTABLE, pattern=pattern, **fallback)

    def exemptions(self, expose: Expose, name: str) -> Tuple[List[str], List[str]]:
        """The placeholders a column's own checks exempt: its own, and those of the column a
        foreign key copies (§3, Foreign keys)."""
        own = expose.model.columns[name]
        values, prefixes = list(own.placeholder_values), list(own.placeholder_prefixes)
        for rel in expose.relationships.values():
            target = self.by_id.get(rel.to)
            for from_name, to_name in zip(rel.from_columns, rel.to_columns, strict=False):
                if from_name != name or target is None or to_name not in target.model.columns:
                    continue
                theirs = target.model.columns[to_name]
                values += [v for v in theirs.placeholder_values if v not in values]
                prefixes += [p for p in theirs.placeholder_prefixes if p not in prefixes]
        return values, prefixes

    def placeholders(
        self, expose: Expose, fallback: Dict[str, Any], block: Mapping[str, Any]
    ) -> Rule:
        if fallback["band"] is None:
            return Rule(status=INFORMATIONAL, **fallback)
        dialect = self.dialect_of(expose)
        query = _RowQuery(self, expose, dialect)
        name = fallback["column"]
        values, prefixes = self.exemptions(expose, name)
        violated = placeholder_sql(dialect, query.column(None, name), values, prefixes)
        return Rule(
            status=EXECUTABLE, dialect=dialect, counts=query.counts("TRUE", violated), **fallback
        )

    # -- shared attributes -----------------------------------------------------------------

    def shared_columns(self, expose: Expose) -> None:
        for rel in expose.relationships.values():
            path = ("exposes", expose.index, "semantics", "relationships", rel.index)
            for name in rel.shared_columns:
                if not isinstance(name, str):
                    continue
                fallback = dict(
                    id=f"{rel.name}.{name}",
                    kind="sharedColumns",
                    expose=expose.expose_id,
                    path=path + ("sharedColumns",),
                    dimension=CONSISTENCY,
                    description=(
                        f"{name} is copied through {rel.name} from {rel.to} on every row whose "
                        "link reaches a row"
                    ),
                )
                self.add(
                    lambda r=rel, c=name, f=fallback: self.shared_column(expose, r, c, f), fallback
                )

    def shared_column(self, expose: Expose, rel: Any, name: str, fallback: Dict[str, Any]) -> Rule:
        dialect = self.dialect_of(expose)
        query = _RowQuery(self, expose, dialect)
        target_key = rel.to_columns[0] if rel.to_columns else None
        if target_key is None:
            raise _Refused(f"{rel.name} names no toColumns")
        covered = n.IsNull(n.Column(target_key, rel.name), negated=True)
        same = n.DistinctFrom(n.Column(name), n.Column(name, rel.name), negated=True)
        node = n.And(covered, n.Not(same))
        checked = fs.check(node, expose.model, fs.PREDICATE)
        query.uses(checked)
        return Rule(
            status=EXECUTABLE,
            dialect=dialect,
            reads=query.reads(),
            counts=query.counts(
                query.render(covered, checked, predicate=True),
                f"(NOT {query.render(same, checked, predicate=True)})",
            ),
            **fallback,
        )

    def shared_attributes(self) -> None:
        for attribute, route in self.routes.items():
            owner_id, owner_column = route.owner
            for member_id, column, hops in route.members:
                member = self.by_id[member_id]
                position = member.columns[column][0]
                fallback = dict(
                    id=f"{attribute}.{member_id}.{column}",
                    kind="sharedAttribute",
                    expose=member_id,
                    path=(
                        "exposes",
                        member.index,
                        "contract",
                        "schema",
                        position,
                        "sharedAttribute",
                    ),
                    dimension=CONSISTENCY,
                    description=(
                        f"{column} carries the attribute {attribute}, owned by "
                        f"{owner_id}.{owner_column}, through {'.'.join(hops)}"
                    ),
                )
                self.add(
                    lambda m=member, c=column, h=hops, o=route.owner, f=fallback: self.attribute(
                        m, c, h, o, f
                    ),
                    fallback,
                )

    def attribute(
        self,
        member: Expose,
        column: str,
        hops: Tuple[str, ...],
        owner: Tuple[str, str],
        fallback: Dict[str, Any],
    ) -> Rule:
        dialect = self.dialect_of(member)
        q = lambda name: fs.sql_name(dialect, name)  # noqa: E731
        joins: List[Callable[[TableSql], str]] = []
        here, previous, reads = member, "t", []
        for step, name in enumerate(hops):
            rel = here.relationships[name]
            target = self.by_id[rel.to]
            self.same_dialect(dialect, target, f"through {name}")
            last = step == len(hops) - 1
            following = () if last else target.relationships[hops[step + 1]].from_columns
            needed = list(rel.to_columns) + [c for c in following if c not in rel.to_columns]
            if last and owner[1] not in needed:
                needed.append(owner[1])
            joins.append(_link_join(dialect, previous, f"l{step + 1}", rel, target, needed))
            reads.append(target.expose_id)
            here, previous = target, f"l{step + 1}"
        mine, theirs = f"{q('t')}.{q(column)}", f"{q(previous)}.{q(owner[1])}"
        last_rel = _last_relationship(member, hops, self.by_id)
        covered = f"({q(previous)}.{q(last_rel.to_columns[0])} IS NOT NULL)"
        violated = f"(NOT ({mine} IS NOT DISTINCT FROM {theirs}))"
        return Rule(
            status=EXECUTABLE,
            dialect=dialect,
            reads=tuple(dict.fromkeys(reads)),
            counts=_counts(dialect, member.expose_id, joins, covered, violated),
            **fallback,
        )


def _last_relationship(member: Expose, hops: Sequence[str], by_id: Mapping[str, Expose]) -> Any:
    here, rel = member, None
    for name in hops:
        rel = here.relationships[name]
        here = by_id[rel.to]
    if rel is None or not rel.to_columns:
        raise _Refused("the route to the owner names no toColumns")
    return rel


# -- SQL builders --------------------------------------------------------------------------


class _Names:
    """Fresh output names that differ, case folded, from every column of a model (BigQuery
    and Snowflake fold unquoted names, so ``n`` and ``N`` would collide there)."""

    def __init__(self, *columns: Iterable[str]) -> None:
        self.taken: Set[str] = {c.casefold() for group in columns for c in group}

    def fresh(self, base: str) -> str:
        name = base
        while name.casefold() in self.taken:
            name += "_"
        self.taken.add(name.casefold())
        return name


def _link_join(
    dialect: str,
    source_alias: str,
    alias: str,
    rel: Any,
    target: Expose,
    columns: Sequence[str],
) -> Callable[[TableSql], str]:
    """A ``LEFT JOIN`` to ``target``'s rows whose key occurs once, as ``alias``."""
    q = lambda name: fs.sql_name(dialect, name)  # noqa: E731
    if not rel.from_columns or len(rel.from_columns) != len(rel.to_columns):
        raise _Refused(f"{rel.name} pairs no fromColumns with its toColumns")
    matches = _Names(target.model.columns).fresh("fluid_matches")
    selected = ", ".join(q(c) for c in dict.fromkeys(columns))
    partition = ", ".join(q(c) for c in rel.to_columns)
    on = " AND ".join(
        f"{q(source_alias)}.{q(f)} = {q(alias)}.{q(t)}"
        for f, t in zip(rel.from_columns, rel.to_columns, strict=True)
    )

    def join(table_sql: TableSql) -> str:
        return (
            f"LEFT JOIN (SELECT {selected}, COUNT(*) OVER (PARTITION BY {partition}) "
            f"AS {q(matches)} FROM {table_sql(target.expose_id)}) AS {q(alias)} "
            f"ON {on} AND {q(alias)}.{q(matches)} = 1"
        )

    return join


def _counts(
    dialect: str,
    expose_id: str,
    joins: Sequence[Callable[[TableSql], str]],
    covered: str,
    violated: str,
) -> Callable[[TableSql], str]:
    q = lambda name: fs.sql_name(dialect, name)  # noqa: E731

    def counts(table_sql: TableSql) -> str:
        cover = "COUNT(*)" if covered == "TRUE" else f"COUNT(CASE WHEN {covered} THEN 1 END)"
        both = violated if covered == "TRUE" else f"{covered} AND {violated}"
        parts = [
            f"SELECT {cover} AS {q('covered')}, "
            f"COUNT(CASE WHEN {both} THEN 1 END) AS {q('violating')} "
            f"FROM {table_sql(expose_id)} AS {q('t')}"
        ]
        parts += [join(table_sql) for join in joins]
        return " ".join(parts)

    return counts


def placeholder_sql(
    dialect: str, column: str, values: Sequence[str], prefixes: Sequence[str]
) -> str:
    """Whether ``column`` holds a placeholder (§4): one of ``values``, or a text starting with
    one of ``prefixes``, matched exactly as stored text; NULL is never one. Prefixes are
    compared with ``SUBSTR`` rather than ``LIKE``, so ``%`` and ``_`` stay literal."""
    tests = []
    if values:
        items = ", ".join(fs.sql_literal(dialect, v) for v in values)
        tests.append(f"{column} IN ({items})")
    for prefix in prefixes:
        tests.append(f"SUBSTR({column}, 1, {len(prefix)}) = {fs.sql_literal(dialect, prefix)}")
    if not tests:
        return "FALSE"
    return "COALESCE(" + " OR ".join(tests) + ", FALSE)"


class _RowQuery:
    """One expose's rows, as ``t``, and the links a rule reads, as ``l1``, ``l2`` ..."""

    def __init__(self, compiler: _Compiler, expose: Expose, dialect: str) -> None:
        self.compiler = compiler
        self.expose = expose
        self.dialect = dialect
        self.links: Dict[str, List[str]] = {}  # relationship -> columns read through it
        self.extra: List[Callable[[TableSql], str]] = []

    def q(self, name: str) -> str:
        return fs.sql_name(self.dialect, name)

    def alias(self, relationship: str) -> str:
        names = list(self.links)
        return f"l{names.index(relationship) + 1}"

    def column(self, relationship: Optional[str], name: str) -> str:
        table = "t" if relationship is None else self.alias(relationship)
        return f"{self.q(table)}.{self.q(name)}"

    def uses(self, checked: fs.Checked) -> None:
        for relationship, column in sorted(checked.reads, key=lambda r: (r[0] or "", r[1])):
            if relationship is None:
                continue
            columns = self.links.setdefault(relationship, [])
            if column not in columns:
                columns.append(column)

    def checked(self, text: Any, site: str) -> Tuple[n.Node, fs.Checked]:
        if not isinstance(text, str):
            raise _Refused("the text is not a string")
        node = fs.parse(text)
        checked = fs.check(node, self.expose.model, site)
        self.uses(checked)
        return node, checked

    def render(self, node: n.Node, checked: fs.Checked, predicate: bool = False) -> str:
        return fs.render_sql(node, checked, self.column, self.dialect, predicate)

    def coverage(
        self, own: Optional[Tuple[n.Node, fs.Checked]], earlier: List[Optional[n.Node]]
    ) -> str:
        """The rows the rule covers: its own ``when``, and none an earlier ``derive`` of the
        column covers (first match, §1.3)."""
        parts = [] if own is None else [self.render(own[0], own[1], predicate=True)]
        for when in earlier:
            if when is None:
                return "FALSE"  # an earlier derive covers every row
            checked = fs.check(when, self.expose.model, fs.PREDICATE)
            self.uses(checked)
            parts.append(f"(NOT {self.render(when, checked, predicate=True)})")
        return " AND ".join(parts) if parts else "TRUE"

    def aggregate(self, target: str, agg: Mapping[str, Any]) -> str:
        """The violation of a summary over child rows (§1.2), joined as ``s``."""
        compiler, dialect, q = self.compiler, self.dialect, self.q
        child = compiler.by_id.get(agg.get("from"))
        rel = child.relationships.get(nfc(agg.get("via"))) if child else None
        if child is None or rel is None:
            raise _Refused("the summary names no child relationship")
        compiler.same_dialect(dialect, child, f"in a summary over {child.expose_id}")
        function = agg.get("function")
        filter_sql = None
        if isinstance(agg.get("filter"), str):
            node = fs.parse(agg["filter"])
            checked = fs.check(node, child.model, fs.PREDICATE)
            if any(relationship for relationship, _ in checked.reads):
                raise _Refused("its filter reads through a relationship of the child")
            filter_sql = fs.render_sql(
                node, checked, fs.column_reader(dialect, "c"), dialect, predicate=True
            )
        column = nfc(agg.get("column")) if isinstance(agg.get("column"), str) else None
        names = _Names(child.model.columns)
        summary = names.fresh("fluid_summary")
        if function == "count":
            inner = (
                "COUNT(*)" if filter_sql is None else f"COUNT(CASE WHEN {filter_sql} THEN 1 END)"
            )
            summary_type = fs.SqlType(NUMBER, 0)
        else:
            value = f"{q('c')}.{q(column)}"
            child_column = child.model.columns[column]
            summary_type = child_column.type.type
            if dialect == "postgres" and summary_type.kind == STRING:
                value = f'({value} COLLATE "C")'
            if filter_sql is not None:
                value = f"CASE WHEN {filter_sql} THEN {value} END"
            inner = f"{str(function).upper()}({value})"
        keys = ", ".join(f"{q('c')}.{q(c)}" for c in rel.from_columns)
        on = " AND ".join(
            f"{q('t')}.{q(t)} = {q('s')}.{q(f)}"
            for f, t in zip(rel.from_columns, rel.to_columns, strict=True)
        )

        def join(table_sql: TableSql) -> str:
            return (
                f"LEFT JOIN (SELECT {keys}, {inner} AS {q(summary)} "
                f"FROM {table_sql(child.expose_id)} AS {q('c')} GROUP BY {keys}) AS {q('s')} "
                f"ON {on}"
            )

        self.extra.append(join)
        self.child = child.expose_id
        result = f"{q('s')}.{q(summary)}"
        if function == "count":
            result = f"COALESCE({result}, 0)"
        if "ifNone" in agg:
            result = f"COALESCE({result}, {self.literal(agg['ifNone'], summary_type)})"
        own = self.expose.model.columns[target]
        mine = self.column(None, target)
        if summary_type.kind == NUMBER:
            scale = max(summary_type.scale or 0, own.type.type.scale or 0)
            cast = fs.exact_number_type(dialect, scale)
            mine, result = f"CAST({mine} AS {cast})", f"CAST({result} AS {cast})"
        return f"(NOT ({mine} IS NOT DISTINCT FROM {result}))"

    def literal(self, value: Any, sql_type: fs.SqlType) -> str:
        if sql_type.kind == NUMBER:
            return _literal(value)
        if sql_type.kind == STRING:
            return fs.sql_literal(self.dialect, value)
        if sql_type.kind == "date":
            text = value.isoformat() if isinstance(value, datetime.date) else str(value)
            return f"DATE {fs.sql_literal(self.dialect, text)}"
        raise _Refused(f"ifNone of a {sql_type.kind} has no literal")

    def reads(self) -> Tuple[str, ...]:
        reads = [
            self.expose.relationships[r].to for r in self.links if r in self.expose.relationships
        ]
        if getattr(self, "child", None):
            reads.append(self.child)
        return tuple(dict.fromkeys(reads))

    def counts(self, covered: str, violated: str) -> Callable[[TableSql], str]:
        joins = list(self.extra)
        for relationship, columns in self.links.items():
            rel = self.expose.relationships[relationship]
            target = self.compiler.by_id.get(rel.to)
            if target is None:
                raise _Refused(f"{relationship} goes to no expose of the contract")
            self.compiler.same_dialect(self.dialect, target, f"through {relationship}")
            needed = list(rel.to_columns) + [c for c in columns if c not in rel.to_columns]
            joins.append(
                _link_join(self.dialect, "t", self.alias(relationship), rel, target, needed)
            )
        return _counts(self.dialect, self.expose.expose_id, joins, covered, violated)


# -- descriptions ----------------------------------------------------------------------------


def _parse(text: str) -> Optional[n.Node]:
    try:
        return fs.parse(text)
    except fs.FluidSqlError:
        return None


def _canonical(text: Any) -> str:
    node = _parse(text) if isinstance(text, str) else None
    return fs.canonical(node) if node is not None else repr(text)


def _describe(inv: Mapping[str, Any]) -> str:
    if isinstance(inv.get("description"), str) and inv["description"].strip():
        text = inv["description"].strip()
    elif "check" in inv:
        text = f"{_canonical(inv['check'])} holds"
    elif "derive" in inv and "expr" in inv:
        text = f"{inv['derive']} is {_canonical(inv['expr'])}"
    elif "derive" in inv and isinstance(inv.get("aggregate"), Mapping):
        agg = inv["aggregate"]
        what = agg.get("function")
        if agg.get("column"):
            what = f"{what}({agg['column']})"
        text = f"{inv['derive']} is the {what} of its {agg.get('from')} rows via {agg.get('via')}"
        if agg.get("filter"):
            text += f" where {_canonical(agg['filter'])}"
    elif isinstance(inv.get("dependency"), Mapping):
        dep = inv["dependency"]
        text = (
            f"{', '.join(map(str, as_list(dep.get('from'))))} determines "
            f"{', '.join(map(str, as_list(dep.get('to'))))}"
        )
    else:
        text = "a declared invariant"
    if isinstance(inv.get("when"), str):
        text += f", on rows where {_canonical(inv['when'])}"
    return text


def _describe_placeholders(name: str, block: Mapping[str, Any]) -> str:
    listed = [repr(v) for v in as_list(block.get("values"))]
    listed += [f"{p!r}…" for p in as_list(block.get("prefixes"))]
    return f"Rows of {name} holding a placeholder value ({', '.join(listed) or 'none listed'})"


def _band(entry: Mapping[str, Any], default: Optional[Band] = NO_VIOLATION) -> Optional[Band]:
    for op in OPERATORS:
        if op in entry:
            unit = entry.get("unit") if entry.get("unit") in ("rows", "percent") else "rows"
            return Band(op, entry[op], unit)
    return default
