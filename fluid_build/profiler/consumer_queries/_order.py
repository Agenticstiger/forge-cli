# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Total orders: when ``LIMIT``, ``OFFSET`` and a window may depend on an ``ORDER BY``.

A query that keeps the first rows of an order, or numbers the rows of one, returns something that
depends on how ties fall unless the order has none. Which rows a tie keeps is not a property of the data, so a query whose order is not
total is refused.

An order is total when it lists

* a key the contract declares unique for the table it orders: every table of the ``FROM`` and the
  joins must then have one of its keys listed, since one row of each table fixes the joined row; or
* every column of the rows it orders: the whole output row of a ``SELECT`` or a set operation, or
  every column of the tables a window reads; or
* every expression the query groups by, for the rows of a ``GROUP BY``.

A query that aggregates without ``GROUP BY`` returns one row and needs no order. A window's
``ORDER BY`` is read within its ``PARTITION BY``: the partition's columns are constant inside a
partition, so they count with the order's. A window function that reads no order (an aggregate with
no ``ORDER BY``) is not asked for one.

This reads the qualified tree, where every column carries its table's alias, every star is
expanded and every ``ORDER BY 1`` names its expression, and a table is an expose of the contract
under its contract names, so a key the contract declares is found by name. A derived table has no
declared key: only the output-row rule applies to it. A key column the contract allows to be
null can tie in a database that holds several nulls; the contract's own declaration is taken as
given. Names are compared as sqlglot folds them (``str.lower``).

Adapted from FLUX ``flux_engine/consumers/_order.py`` at 19dfe48 (Apache-2.0).
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Set, Tuple

from sqlglot import exp

from fluid_build.profiler.consumer_queries._allow import AGGREGATES
from fluid_build.profiler.consumer_queries._types import Refusal, Table

__all__ = ["check_total_orders"]

Columns = Set[Tuple[str, str]]


def check_total_orders(tree: exp.Expr, schema: Mapping[str, Table], *, label: str) -> None:
    """Refuse a LIMIT, OFFSET or window of *tree* whose order is not total.

    *schema* maps each expose's exposeId, folded with ``str.lower``, to its schema.
    """
    _Orders(schema, label).check(tree)


def _key(node: exp.Expr) -> str:
    return str(node.sql(dialect="duckdb", comments=False))


def _fold(name: object) -> str:
    return str(name).lower()


def _projections(select: exp.Select) -> List[exp.Expr]:
    return [
        item.this if isinstance(item, exp.Alias) else item
        for item in select.args.get("expressions") or []
    ]


def _sources(select: exp.Select) -> List[exp.Expr]:
    found: List[exp.Expr] = []
    from_ = select.args.get("from_")
    if from_ is not None:
        found.append(from_.this)
    for join in select.args.get("joins") or []:
        found.append(join.this)
    return found


def _aggregates_without_group(select: exp.Select) -> bool:
    """Whether *select* aggregates over all its rows, so that it returns exactly one."""
    if select.args.get("group") is not None:
        return False
    for item in select.args.get("expressions") or []:
        for aggregate in item.find_all(*AGGREGATES):
            if aggregate.find_ancestor(exp.Window, exp.Select) is select:
                return True
    return False


def _one_row(select: exp.Select) -> bool:
    return not _sources(select) or _aggregates_without_group(select)


def _ordered(select: Optional[exp.Select], order: exp.Order) -> List[exp.Expr]:
    """The expressions *order* sorts by, a reference to an output name replaced by its expression."""
    aliases: Dict[str, exp.Expr] = {}
    if select is not None:
        for item in select.args.get("expressions") or []:
            if isinstance(item, exp.Alias):
                aliases[_fold(item.alias)] = item.this
    found: List[exp.Expr] = []
    for ordered in order.expressions:
        expression = ordered.this
        if isinstance(expression, exp.Column) and not expression.table:
            expression = aliases.get(_fold(expression.name), expression)
        found.append(expression)
    return found


def _columns(expressions: List[exp.Expr]) -> Columns:
    return {
        (_fold(item.table), _fold(item.name))
        for item in expressions
        if isinstance(item, exp.Column) and item.table
    }


def _refuse(label: str, what: str, hint: str) -> Refusal:
    return Refusal("order", f"{label} uses {what}, and {hint}")


class _Orders:
    """The total-order rules over one tree; what a SELECT answers for every window is worked out once."""

    def __init__(self, schema: Mapping[str, Table], label: str) -> None:
        self._schema = schema
        self._label = label
        self._groups: Dict[int, Optional[Set[str]]] = {}
        self._one_row: Dict[int, bool] = {}

    def check(self, tree: exp.Expr) -> None:
        for node in list(tree.walk()):
            if isinstance(node, exp.Select):
                self._select(node)
            elif isinstance(node, exp.SetOperation):
                self._set_operation(node)
            elif isinstance(node, exp.Window):
                self._window(node)

    # -- the rules ---------------------------------------------------------------------------

    def _expose_of(self, source: exp.Expr) -> Optional[Table]:
        if not isinstance(source, exp.Table):
            return None
        return self._schema.get(_fold(source.name))

    def _returns_one_row(self, select: exp.Select) -> bool:
        if id(select) not in self._one_row:
            self._one_row[id(select)] = _one_row(select)
        return self._one_row[id(select)]

    def _group_keys(self, select: exp.Select) -> Optional[Set[str]]:
        if id(select) not in self._groups:
            group = select.args.get("group")
            self._groups[id(select)] = (
                None if group is None else {_key(item) for item in group.expressions}
            )
        return self._groups[id(select)]

    def _covers_groups(self, select: exp.Select, keys: Set[str]) -> bool:
        wanted = self._group_keys(select)
        return wanted is not None and wanted <= keys

    @staticmethod
    def _covers_outputs(select: exp.Select, keys: Set[str]) -> bool:
        """Whether the order lists every output of *select* that depends on a column."""
        return all(
            _key(item) in keys for item in _projections(select) if any(item.find_all(exp.Column))
        )

    def _source_keyed(self, source: exp.Expr, columns: Columns) -> bool:
        expose = self._expose_of(source)
        if expose is None:
            return False
        alias = _fold(source.alias_or_name)
        return any(all((alias, _fold(name)) in columns for name in key) for key in expose.unique)

    def _covers_keys(self, select: exp.Select, columns: Columns) -> bool:
        sources = _sources(select)
        return bool(sources) and all(self._source_keyed(source, columns) for source in sources)

    def _source_complete(self, source: exp.Expr, columns: Columns) -> bool:
        expose = self._expose_of(source)
        if expose is None:
            return False
        alias = _fold(source.alias_or_name)
        return all((alias, _fold(name)) in columns for name in expose.column_names)

    def _covers_columns(self, select: exp.Select, columns: Columns) -> bool:
        sources = _sources(select)
        return bool(sources) and all(self._source_complete(source, columns) for source in sources)

    # -- LIMIT and OFFSET --------------------------------------------------------------------

    def _select_is_total(self, select: exp.Select) -> bool:
        order = select.args.get("order")
        if order is None:
            return False
        expressions = _ordered(select, order)
        keys = {_key(item) for item in expressions}
        if self._covers_outputs(select, keys):
            return True
        if select.args.get("group") is not None:
            return self._covers_groups(select, keys)
        if select.args.get("distinct") is not None:
            return False
        return self._covers_keys(select, _columns(expressions))

    def _select(self, select: exp.Select) -> None:
        if not (select.args.get("limit") or select.args.get("offset")):
            return
        kind = "LIMIT" if select.args.get("limit") else "OFFSET"
        if self._returns_one_row(select) or self._select_is_total(select):
            return
        if select.args.get("order") is None:
            raise _refuse(self._label, kind, "it has no ORDER BY")
        raise _refuse(
            self._label,
            kind,
            "its ORDER BY lists neither a key the contract declares unique for each table it "
            "reads nor every column of the rows (nor every GROUP BY expression)",
        )

    def _set_operation(self, node: exp.SetOperation) -> None:
        if not (node.args.get("limit") or node.args.get("offset")):
            return
        kind = "LIMIT" if node.args.get("limit") else "OFFSET"
        what = f"{kind} on a set operation"
        order = node.args.get("order")
        if order is None:
            raise _refuse(self._label, what, "it has no ORDER BY")
        listed = {
            _fold(item.this.name)
            for item in order.expressions
            if isinstance(item.this, exp.Column) and not item.this.table
        }
        outputs = [_fold(name) for name in node.named_selects]
        if not outputs or not all(name in listed for name in outputs):
            raise _refuse(self._label, what, "its ORDER BY does not list every output column")

    # -- windows -----------------------------------------------------------------------------

    def _window_is_total(self, window: exp.Window, select: exp.Select) -> bool:
        order = window.args["order"]
        expressions = _ordered(None, order) + list(window.args.get("partition_by") or [])
        if select.args.get("group") is not None:
            return self._covers_groups(select, {_key(item) for item in expressions})
        columns = _columns(expressions)
        return (
            self._returns_one_row(select)
            or self._covers_keys(select, columns)
            or self._covers_columns(select, columns)
        )

    def _window(self, window: exp.Window) -> None:
        function = window.this
        order = window.args.get("order")
        ranking = isinstance(function, (exp.RowNumber, exp.Rank, exp.DenseRank))
        if order is None and not ranking:
            return
        what = f"{type(function).__name__} over a window"
        if order is None:
            raise _refuse(self._label, what, "the window has no ORDER BY")
        owner = window.find_ancestor(exp.Select)
        if not isinstance(owner, exp.Select):
            raise _refuse(self._label, what, "it is not inside a SELECT")
        if not self._window_is_total(window, owner):
            raise _refuse(
                self._label,
                what,
                "its ORDER BY, with its PARTITION BY, lists neither a key the contract declares "
                "unique for each table it reads nor every column of those tables",
            )
