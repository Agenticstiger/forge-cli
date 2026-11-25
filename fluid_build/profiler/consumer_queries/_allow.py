# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0
# Adapted from FLUX flux_engine/consumers/_allow.py at 19dfe48, Apache-2.0

"""The closed lists a consumer query is checked against.

Everything here is an allowlist. A node of the parsed tree passes only when its exact class is on
:data:`EXPRESSIONS` or :data:`FUNCTIONS` (``type(node) in ...``, never ``isinstance``, so a subclass
sqlglot adds later is refused until someone lists it), when each argument it carries is on the
list of its class, and when each scalar argument holds a value that list names. Whatever a list
does not name is refused: there is no denylist to forget a name from.

The tree is walked without recursion, and the node and depth caps are counted while it is walked,
so a hostile text stops the walk before the walk can exhaust the stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple, Type

from sqlglot import exp

from fluid_build.profiler.consumer_queries._types import Refusal

__all__ = [
    "CANONICAL_CAPS",
    "EXPRESSIONS",
    "FUNCTIONS",
    "QUERY_ROOTS",
    "SOURCE_CAPS",
    "Caps",
    "describe",
    "vet_tree",
]


@dataclass(frozen=True)
class Caps:
    """How large a text, and its tree, may be."""

    tokens: int
    brackets: int
    nodes: int
    depth: int


#: The caps of the consumer's own text.
SOURCE_CAPS = Caps(tokens=20_000, brackets=64, nodes=10_000, depth=64)

#: The caps of the canonical text, which is the same query after its stars are expanded and every
#: name is qualified and quoted, so it is larger than the text it came from.
CANONICAL_CAPS = Caps(tokens=200_000, brackets=96, nodes=100_000, depth=96)

#: What a statement may be at the root.
QUERY_ROOTS: Tuple[Type[exp.Expr], ...] = (exp.Select, exp.Union, exp.Intersect, exp.Except)

#: Expression classes: names, values, operators, predicates and clauses.
EXPRESSIONS: Tuple[Type[exp.Expr], ...] = (
    # statements, clauses and what they hold
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
    exp.With,
    exp.CTE,
    exp.From,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Distinct,
    exp.Join,
    exp.Window,
    # names
    exp.Table,
    exp.TableAlias,
    exp.Alias,
    exp.Column,
    exp.Identifier,
    exp.Star,
    # values
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Var,
    exp.Paren,
    exp.Tuple,
    exp.Interval,
    exp.DataType,
    exp.DataTypeParam,
    # operators and predicates
    exp.And,
    exp.Or,
    exp.Not,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.IntDiv,
    exp.Neg,
    exp.DPipe,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
    exp.Is,
    # conditionals and casts
    exp.Case,
    exp.If,
    exp.Coalesce,
    exp.Nullif,
    exp.Cast,
    exp.Exists,
)

#: Aggregate functions, which may also be window functions.
AGGREGATES: Tuple[Type[exp.Expr], ...] = (
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Stddev,
    exp.StddevPop,
    exp.StddevSamp,
    exp.Variance,
    exp.VariancePop,
)

#: Functions that only make sense over a window.
RANKING: Tuple[Type[exp.Expr], ...] = (exp.RowNumber, exp.Rank, exp.DenseRank)

#: Function classes: each reads only its arguments.
FUNCTIONS: Tuple[Type[exp.Expr], ...] = (
    *AGGREGATES,
    *RANKING,
    # strings
    exp.Lower,
    exp.Upper,
    exp.Trim,
    exp.Length,
    exp.Substring,
    exp.Concat,
    exp.Replace,
    # numbers
    exp.Abs,
    exp.Round,
    exp.Floor,
    exp.Ceil,
    exp.Trunc,
    # dates and times
    exp.Extract,
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.DatetimeTrunc,
    exp.DateAdd,
    exp.DateSub,
    exp.DateDiff,
    exp.TsOrDsAdd,
    exp.TsOrDsDiff,
    exp.TsOrDsToDate,
    exp.TimestampAdd,
    exp.TimestampSub,
    exp.TimestampDiff,
    exp.DatetimeAdd,
    exp.DatetimeSub,
    exp.DatetimeDiff,
    exp.Year,
    exp.Month,
    exp.Day,
    exp.Quarter,
    exp.Week,
    exp.WeekOfYear,
    exp.DayOfWeek,
    exp.DayOfMonth,
    exp.DayOfYear,
    exp.Hour,
    exp.Minute,
    exp.Second,
)

_ALLOWED_CLASSES: FrozenSet[type] = frozenset((*EXPRESSIONS, *FUNCTIONS))

#: The types a CAST may name: the logical types of the contract and the spellings that map onto them.
_CAST_TYPES: FrozenSet[exp.DType] = frozenset(
    {
        exp.DType.VARCHAR,
        exp.DType.TEXT,
        exp.DType.CHAR,
        exp.DType.NCHAR,
        exp.DType.NVARCHAR,
        exp.DType.TINYINT,
        exp.DType.SMALLINT,
        exp.DType.INT,
        exp.DType.BIGINT,
        exp.DType.DECIMAL,
        exp.DType.DATE,
        exp.DType.TIMESTAMP,
        exp.DType.TIMESTAMPNTZ,
        exp.DType.BOOLEAN,
    }
)

#: The units EXTRACT, DATE_TRUNC and INTERVAL may name.
_DATE_PARTS: FrozenSet[str] = frozenset(
    {
        "YEAR",
        "YEARS",
        "QUARTER",
        "QUARTERS",
        "MONTH",
        "MONTHS",
        "WEEK",
        "WEEKS",
        "DAY",
        "DAYS",
        "HOUR",
        "HOURS",
        "MINUTE",
        "MINUTES",
        "SECOND",
        "SECONDS",
        "DAYOFWEEK",
        "DAYOFYEAR",
        "DOW",
        "DOY",
    }
)

_SET_OPERATION_ARGS: Mapping[str, Optional[FrozenSet[Any]]] = {
    key: None for key in ("this", "expression", "distinct", "with_", "order", "limit", "offset")
}

# The arguments each structural class may carry, with the values a scalar one may hold (``None``
# is any value). A truthy argument not listed here is refused, which is how a clause nobody
# listed (QUALIFY, a WINDOW clause, a frame, a sample, a lock, a hint, USING) is refused.
_STRICT_ARGS: Dict[type, Mapping[str, Optional[FrozenSet[Any]]]] = {
    exp.Select: {
        key: None
        for key in (
            "expressions",
            "from_",
            "joins",
            "where",
            "group",
            "having",
            "order",
            "limit",
            "offset",
            "distinct",
            "with_",
        )
    },
    exp.Union: _SET_OPERATION_ARGS,
    exp.Intersect: _SET_OPERATION_ARGS,
    exp.Except: _SET_OPERATION_ARGS,
    exp.Subquery: {"this": None, "alias": None, "with_": None},
    exp.With: {"expressions": None},
    exp.CTE: {"this": None, "alias": None},
    exp.From: {"this": None},
    exp.Where: {"this": None},
    exp.Group: {"expressions": None},
    exp.Having: {"this": None},
    exp.Order: {"expressions": None},
    exp.Ordered: {"this": None, "desc": None, "nulls_first": None},
    exp.Limit: {"expression": None},
    exp.Offset: {"expression": None},
    exp.Distinct: {"expressions": None},
    exp.Join: {
        "this": None,
        "on": None,
        "side": frozenset({"LEFT", "RIGHT", "FULL"}),
        "kind": frozenset({"INNER", "OUTER", "CROSS"}),
    },
    exp.Window: {
        "this": None,
        "partition_by": None,
        "order": None,
        "over": frozenset({"OVER"}),
    },
    exp.Table: {"this": None, "alias": None, "db": None, "catalog": None},
    exp.TableAlias: {"this": None, "columns": None},
    exp.Alias: {"this": None, "alias": None},
    exp.Column: {"this": None, "table": None},
    exp.Star: {},
    exp.Cast: {"this": None, "to": None},
    exp.DataType: {"this": None, "expressions": None},
    exp.Case: {"this": None, "ifs": None, "default": None},
    exp.If: {"this": None, "true": None, "false": None},
    exp.Between: {"this": None, "low": None, "high": None},
    exp.In: {"this": None, "expressions": None, "query": None},
    exp.Interval: {"this": None, "unit": None},
    exp.Extract: {"this": None, "expression": None},
    # The two classes sqlglot writes for ``YEAR(x)`` in MySQL, T-SQL, Spark, Hive and Databricks
    # and for integer division in PostgreSQL, Redshift, T-SQL, SQLite, Trino and Drill.
    exp.TsOrDsToDate: {"this": None, "default_date": None},
    exp.Trunc: {"this": None, "decimals": None},
    exp.Exists: {"this": None},
    exp.Paren: {"this": None},
}

# The scalar arguments the other classes may carry. A node argument is always fine here, since the
# node is walked and its own class is checked.
_LOOSE_SCALARS: Dict[type, Mapping[str, Optional[FrozenSet[Any]]]] = {
    exp.Identifier: {"this": None, "quoted": None},
    exp.Literal: {"this": None, "is_string": None},
    exp.Boolean: {"this": None},
    exp.Var: {"this": None},
    # Flags a dialect's parser sets so that the DuckDB writer reproduces the dialect's meaning
    # (NOT LIKE, IFNULL, integer division, a zero-based SUBSTRING, and the like). Each is a bool.
    exp.Like: {"negate": None},
    exp.ILike: {"negate": None},
    exp.Is: {"negate": None},
    exp.Count: {"big_int": None},
    exp.Concat: {"safe": None, "coalesce": None},
    exp.DPipe: {"safe": None},
    exp.Div: {"typed": None, "safe": None},
    exp.IntDiv: {"typed": None, "safe": None},
    exp.Mod: {"typed": None, "safe": None},
    exp.Coalesce: {"is_null": None, "is_nvl": None},
    exp.Variance: {"single_value_returns_zero": None},
    exp.Substring: {"zero_start": None},
    exp.Round: {"casts_non_integer_decimals": None, "negative_decimals_are_zero": None},
    exp.DateTrunc: {"century_from_zero": None, "input_type_preserved": None},
    exp.TimestampTrunc: {"century_from_zero": None, "input_type_preserved": None},
    exp.DateDiff: {"big_int": None, "date_part_boundary": None},
    exp.DatetimeDiff: {"date_part_boundary": None},
    exp.TimestampDiff: {"boundary_for_date": None},
    exp.Trim: {"position": frozenset({"LEADING", "TRAILING", "BOTH"})},
}

_NO_VALUES: Mapping[str, Optional[FrozenSet[Any]]] = {}

#: What a reader calls the argument that a class does not list, for the refusal's message.
_CONSTRUCTS: Dict[Tuple[str, str], str] = {
    ("With", "recursive"): "WITH RECURSIVE",
    ("With", "search"): "WITH ... SEARCH",
    ("With", "cycle"): "WITH ... CYCLE",
    ("CTE", "materialized"): "MATERIALIZED",
    ("Join", "using"): "a join with USING (write the condition with ON)",
    ("Join", "method"): "a NATURAL or ASOF join",
    ("Join", "match_condition"): "an ASOF join",
    ("Select", "qualify"): "QUALIFY",
    ("Select", "windows"): "a WINDOW clause",
    ("Select", "into"): "SELECT INTO",
    ("Select", "locks"): "a locking clause",
    ("Select", "sample"): "TABLESAMPLE",
    ("Select", "exclude"): "SELECT EXCLUDE",
    ("Select", "kind"): "SELECT AS STRUCT or VALUE",
    ("Select", "hint"): "an optimizer hint",
    ("Window", "spec"): "a window frame",
    ("Window", "alias"): "a named window",
    ("Group", "all"): "GROUP BY ALL",
    ("Group", "rollup"): "ROLLUP",
    ("Group", "cube"): "CUBE",
    ("Group", "grouping_sets"): "GROUPING SETS",
    ("Distinct", "on"): "DISTINCT ON",
    ("Limit", "limit_options"): "LIMIT with PERCENT or WITH TIES",
    ("Table", "sample"): "TABLESAMPLE",
    ("Table", "pivots"): "PIVOT or UNPIVOT",
    ("Table", "version"): "time travel",
    ("Column", "db"): "a column qualified by a schema (qualify it by its table or alias)",
    ("Column", "catalog"): "a column qualified by a catalog (qualify it by its table or alias)",
    ("Star", "except_"): "a star modifier (EXCEPT)",
    ("Star", "replace"): "a star modifier (REPLACE)",
    ("Star", "rename"): "a star modifier (RENAME)",
    ("Star", "ilike"): "a star modifier (ILIKE)",
}


def _unlisted(cls: type, key: str, label: str) -> Refusal:
    construct = _CONSTRUCTS.get((cls.__name__, key), f"{cls.__name__}.{key}")
    return Refusal("construct", f"{label} uses {construct}, which is not on the list")


def name_of(raw: object, *, most: int = 64) -> str:
    """A name written in a text, made safe to show: printable characters only, at most *most*."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(raw))
    return text if len(text) <= most else text[:most] + "..."


def describe(node: exp.Expr) -> str:
    """A short name for *node* for a message: a function's name, or the node's class."""
    if isinstance(node, exp.Anonymous):
        return f"the function {name_of(node.name)!r}"
    if isinstance(node, exp.Func):
        return f"the function {node.sql_name()} ({type(node).__name__})"
    return f"{type(node).__name__}"


def vet_tree(tree: exp.Expr, *, label: str, caps: Caps) -> int:
    """Check every node of *tree* against the lists; return the number of nodes.

    No placeholder, no clock and no unknown function passes: a query text takes no parameters
    and reads nothing but its tables. A node or depth cap that is passed, and any refusal, raise
    a :class:`Refusal` naming *label*. The walk is iterative, so the caps are checked before any
    depth can hurt.
    """
    allowed = _ALLOWED_CLASSES
    stack: List[Tuple[exp.Expr, int]] = [(tree, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > caps.nodes:
            raise Refusal("bounds", f"{label} holds more than {caps.nodes} expression nodes")
        if depth > caps.depth:
            raise Refusal(
                "bounds",
                f"{label} nests expressions more than {caps.depth} levels deep (a long chain of "
                "AND or OR nests one level per term)",
            )
        if isinstance(node, (exp.Placeholder, exp.Parameter)):
            raise Refusal(
                "placeholder", f"{label} holds a placeholder; a query text takes no parameters"
            )
        _vet_node(node, label, allowed)
        for child in reversed(list(node.iter_expressions())):
            stack.append((child, depth + 1))
    return count


def _vet_node(node: exp.Expr, label: str, allowed: FrozenSet[type]) -> None:
    cls = type(node)
    if cls not in allowed:
        raise _refusal(node, label)
    _vet_args(node, label)
    rule = _RULES.get(cls)
    if rule is not None:
        rule(node, label)


def _refusal(node: exp.Expr, label: str) -> Refusal:
    if isinstance(node, exp.Func):
        return Refusal("function", f"{label} calls {describe(node)}, which is not on the list")
    return Refusal("construct", f"{label} uses {describe(node)}, which is not on the list")


#: Marks a scalar argument that no list names.
_NOT_LISTED: Any = frozenset({object()})


def _is_node_value(value: Any) -> bool:
    if isinstance(value, exp.Expr):
        return True
    return isinstance(value, list) and all(isinstance(item, exp.Expr) for item in value)


def _is_empty_arg(value: Any) -> bool:
    return value is None or value is False or value == "" or value == []


def _listed_values(
    cls: type, key: str, strict: Optional[Mapping[str, Any]], loose: Mapping[str, Any], label: str
) -> Any:
    """The values the list allows for argument *key* of *cls*; refuses an argument no list names."""
    if strict is None:
        return loose.get(key) if key in loose else _NOT_LISTED
    if key not in strict:
        raise _unlisted(cls, key, label)
    return strict[key]


def _vet_args(node: exp.Expr, label: str) -> None:
    cls = type(node)
    strict = _STRICT_ARGS.get(cls)
    loose = _LOOSE_SCALARS.get(cls, _NO_VALUES)
    for key, value in node.args.items():
        if _is_empty_arg(value):
            continue
        where = f"{cls.__name__}.{key}"
        values = _listed_values(cls, key, strict, loose, label)
        if _is_node_value(value):
            continue
        if not isinstance(value, (str, int, Enum)) or values is _NOT_LISTED:
            raise _unlisted(cls, key, label)
        if values is not None and value not in values:
            raise Refusal("construct", f"{label} uses {where}={value!r}, which is not on the list")


def _rule_table(node: exp.Expr, label: str) -> None:
    for key in ("this", "db", "catalog"):
        part = node.args.get(key)
        if isinstance(part, exp.Dot):
            raise Refusal("table", f"{label} names a table with more than three parts")
        if part is not None and not isinstance(part, exp.Identifier):
            raise Refusal(
                "table",
                f"{label} reads a table that is not a name: {describe(part)} in a FROM or a JOIN",
            )
    if node.args.get("this") is None:
        raise Refusal("table", f"{label} reads a table that is not a name")
    alias = node.args.get("alias")
    if isinstance(alias, exp.TableAlias) and alias.args.get("columns"):
        raise Refusal("construct", f"{label} gives a table a list of column aliases")


def _rule_select(node: exp.Expr, label: str) -> None:
    if not node.args.get("expressions"):
        raise Refusal("construct", f"{label} has a SELECT with no columns")


def _rule_join(node: exp.Expr, label: str) -> None:
    if node.args.get("using"):
        raise Refusal("construct", f"{label} joins with USING; write the join with ON")


def _rule_with(node: exp.Expr, label: str) -> None:
    if node.args.get("recursive"):
        raise Refusal("construct", f"{label} uses WITH RECURSIVE")
    seen = set()
    for cte in node.expressions:
        name = str(cte.alias).lower()
        if name in seen:
            raise Refusal("construct", f"{label} names the WITH entry {name!r} twice")
        seen.add(name)


def _rule_datatype(node: exp.Expr, label: str) -> None:
    if node.args.get("this") not in _CAST_TYPES:
        kind = getattr(node.args.get("this"), "name", str(node.args.get("this")))
        raise Refusal("construct", f"{label} uses a type ({kind}) outside the list of CAST types")


def _rule_datatype_param(node: exp.Expr, label: str) -> None:
    value = node.args.get("this")
    if not isinstance(value, exp.Literal) or value.is_string:
        raise Refusal("construct", f"{label} gives a type a parameter that is not a number")


def _rule_cast(node: exp.Expr, label: str) -> None:
    if not isinstance(node.args.get("to"), exp.DataType):
        raise Refusal("construct", f"{label} casts to something that is not a type")


def _rule_var(node: exp.Expr, label: str) -> None:
    if str(node.args.get("this")).upper() not in _DATE_PARTS:
        raise Refusal(
            "construct",
            f"{label} uses the name {name_of(node.args.get('this'))!r}, which is not a date part",
        )


#: Classes that may not appear anywhere inside the arguments of the two classes sqlglot writes for
#: the transpiler's own rewrites: a query, a table, a table function, an unlisted call.
_REWRITE_FORBIDDEN: Tuple[Type[exp.Expr], ...] = (
    exp.Query,
    exp.Subquery,
    exp.Exists,
    exp.Table,
    exp.Anonymous,
    exp.UDTF,
)


def _refuse_rewrite_operand(node: exp.Expr, this: exp.Expr, label: str) -> None:
    """Refuse a query, a table, a table function or an unlisted call anywhere in *this*."""
    for inner in this.walk():
        if isinstance(inner, exp.Anonymous):
            raise Refusal(
                "function", f"{label} gives {describe(node)} {describe(inner)} as an argument"
            )
        if isinstance(inner, _REWRITE_FORBIDDEN) and not isinstance(inner, exp.Anonymous):
            raise Refusal(
                "function",
                f"{label} gives {describe(node)} {describe(inner)} as an argument; only a "
                f"column, a literal or a scalar expression of them may be the operand of "
                f"{type(node).__name__}",
            )


def _refuse_rewrite_literal(node: exp.Expr, label: str) -> None:
    """Refuse a non-operand argument other than the one literal sqlglot writes for *node*."""
    if isinstance(node, exp.TsOrDsToDate):
        default = node.args.get("default_date")
        # T-SQL writes the date it reads a zero as; it is this one literal and no other.
        if default is not None and not (
            isinstance(default, exp.Literal) and default.is_string and default.this == "1900-01-01"
        ):
            raise Refusal(
                "function", f"{label} gives {describe(node)} a default date it never writes"
            )
        return
    decimals = node.args.get("decimals")
    if decimals is not None and not (isinstance(decimals, exp.Literal) and not decimals.is_string):
        raise Refusal(
            "function", f"{label} gives {describe(node)} a digit count that is not a number"
        )


def _rule_rewrite(node: exp.Expr, label: str) -> None:
    """Arguments of ``TsOrDsToDate`` and ``Trunc``: a plain operand, never a query or a table."""
    this = node.args.get("this")
    if not isinstance(this, exp.Expr):
        raise Refusal("function", f"{label} calls {describe(node)} without an operand")
    _refuse_rewrite_operand(node, this, label)
    _refuse_rewrite_literal(node, label)


def _rule_limit(node: exp.Expr, label: str) -> None:
    count = node.args.get("expression")
    if not isinstance(count, exp.Literal) or count.is_string:
        raise Refusal(
            "construct", f"{label} gives LIMIT or OFFSET something other than a whole number"
        )


def _rule_ranking(node: exp.Expr, label: str) -> None:
    parent = node.parent
    if not (isinstance(parent, exp.Window) and parent.this is node):
        raise Refusal("construct", f"{label} uses {describe(node)} without OVER")


def _rule_window(node: exp.Expr, label: str) -> None:
    function = node.this
    if type(function) not in (*AGGREGATES, *RANKING):
        raise Refusal("construct", f"{label} uses {describe(function)} as a window function")
    if isinstance(function.args.get("this"), exp.Distinct):
        raise Refusal("construct", f"{label} uses DISTINCT inside a window function")


_RULES: Dict[type, Any] = {
    exp.Select: _rule_select,
    exp.Table: _rule_table,
    exp.Join: _rule_join,
    exp.With: _rule_with,
    exp.DataType: _rule_datatype,
    exp.DataTypeParam: _rule_datatype_param,
    exp.Cast: _rule_cast,
    exp.Var: _rule_var,
    exp.Limit: _rule_limit,
    exp.Offset: _rule_limit,
    exp.RowNumber: _rule_ranking,
    exp.Rank: _rule_ranking,
    exp.DenseRank: _rule_ranking,
    exp.Window: _rule_window,
    exp.TsOrDsToDate: _rule_rewrite,
    exp.Trunc: _rule_rewrite,
}
