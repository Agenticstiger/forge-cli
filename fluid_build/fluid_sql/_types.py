# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""FLUID SQL/1's six types, the FLUID column types they come from, and the model a
text is checked against (``RFC-data-rules.md`` §2.2 and §2.3).

A :class:`Model` is one expose as an expression sees it: its columns, each with
its FLUID SQL/1 type (or none), and its relationships, each to another model.
The contract lint builds one per expose; the vectors file describes its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

BOOL, NUMBER, STRING, DATE, TIMESTAMP, TIMESTAMPTZ = (
    "bool",
    "number",
    "string",
    "date",
    "timestamp",
    "timestamptz",
)
KINDS = (BOOL, NUMBER, STRING, DATE, TIMESTAMP, TIMESTAMPTZ)
#: The types ``<``, ``<=``, ``>``, ``>=`` and ``BETWEEN`` take.
ORDERABLE = frozenset({NUMBER, STRING, DATE, TIMESTAMP, TIMESTAMPTZ})

_BOOL_TYPES = frozenset({"boolean", "bool"})
_INT_TYPES = frozenset(
    {"int", "integer", "int2", "int4", "int8", "int16", "int32", "int64", "tinyint"}
    | {"smallint", "mediumint", "bigint", "long", "longint", "serial", "bigserial"}
)
_DECIMAL_TYPES = frozenset({"decimal", "dec", "numeric", "number", "bignumeric"})
_FLOAT_TYPES = frozenset(
    {"float", "float4", "float8", "float32", "float64", "double", "double precision", "real"}
)
_STRING_TYPES = frozenset(
    {"string", "text", "varchar", "varchar2", "nvarchar", "char", "nchar", "character"}
    | {"clob", "uuid", "uniqueidentifier", "guid", "enum"}
)
_TIMESTAMP_TYPES = frozenset(
    {"datetime", "datetime2", "smalldatetime", "timestamp", "timestamp_ntz", "timestampntz"}
    | {"timestamp without time zone"}
)
_TIMESTAMPTZ_TYPES = frozenset(
    {"timestamptz", "timestamp_tz", "timestamp_ltz", "timestamp with time zone"}
)
#: The widest DECIMAL precision read (PostgreSQL's limit): a wider declaration has no FLUID
#: SQL/1 type, so no contract can make a value of unbounded width (flux-expr/2 reads it so too).
MAX_DECIMAL_PRECISION = 1000
_DECLARED = re.compile(r"(?P<base>[a-z0-9_ ]+?)\s*(?:\((?P<params>[^()]*)\))?\Z")
_PRECISION_SCALE = re.compile(r"(?P<p>[0-9]+)(?:\s*,\s*(?P<s>[0-9]+))?\Z")


@dataclass(frozen=True)
class SqlType:
    """A FLUID SQL/1 type. ``scale`` is a number's static scale: an integer, or
    ``None`` when it is unbounded (a float, a decimal without ``(p,s)``, a quotient)."""

    kind: str
    scale: Optional[int] = None

    def describe(self) -> str:
        if self.kind != NUMBER:
            return self.kind
        return (
            "number of unbounded scale" if self.scale is None else f"number of scale {self.scale}"
        )


@dataclass(frozen=True)
class ColumnType:
    """What a FLUID column type is to FLUID SQL/1."""

    type: SqlType
    is_float: bool = False
    #: A number column whose scale the contract declares: an integer type, or a
    #: decimal with ``(p)`` or ``(p,s)``. A derive target that is a number needs it.
    declared_scale: bool = False
    precision: Optional[int] = None


def column_type(declared: object) -> Optional[ColumnType]:
    """The FLUID SQL/1 type of a column declared as ``declared``, or ``None`` when it
    has none (``time``, ``json``, ``array``, ``interval``, the binary types ...)."""
    if not isinstance(declared, str):
        return None
    text = " ".join(declared.strip().lower().split())
    match = _DECLARED.match(text)
    if not match:
        return None
    base, params = match.group("base").strip(), match.group("params")
    if base in _BOOL_TYPES and params is None:
        return ColumnType(SqlType(BOOL))
    if base == "bit" and (params is None or params.strip() == "1"):
        return ColumnType(SqlType(BOOL))
    if base in _INT_TYPES:
        return ColumnType(SqlType(NUMBER, 0), declared_scale=True)
    if base in _DECIMAL_TYPES:
        if params is None:
            return ColumnType(SqlType(NUMBER, None))
        sized = _PRECISION_SCALE.match(params.strip())
        if not sized:
            return None
        # Read the digits without leading zeros, and refuse more than four before int():
        # a longer text is wider than MAX_DECIMAL_PRECISION, and int() refuses 4,300 digits.
        digits = sized.group("p").lstrip("0") or "0"
        places = (sized.group("s") or "0").lstrip("0") or "0"
        if len(digits) > 4 or len(places) > 4:
            return None
        precision = int(digits)
        if precision > MAX_DECIMAL_PRECISION:
            return None
        scale = int(places)
        return ColumnType(SqlType(NUMBER, scale), declared_scale=True, precision=precision)
    if base in _FLOAT_TYPES:
        return ColumnType(SqlType(NUMBER, None), is_float=True)
    if base in _STRING_TYPES:
        return ColumnType(SqlType(STRING))
    if base == "date" and params is None:
        return ColumnType(SqlType(DATE))
    if base in _TIMESTAMP_TYPES:
        return ColumnType(SqlType(TIMESTAMP))
    if base in _TIMESTAMPTZ_TYPES:
        return ColumnType(SqlType(TIMESTAMPTZ))
    return None


@dataclass(frozen=True)
class Column:
    """A column an expression can name: ``type`` is ``None`` when it has no FLUID
    SQL/1 type; ``enum`` and the placeholders come from the contract."""

    name: str
    declared: str = ""
    type: Optional[ColumnType] = None
    enum: Tuple[str, ...] = ()
    placeholder_values: Tuple[str, ...] = ()
    placeholder_prefixes: Tuple[str, ...] = ()

    def is_placeholder(self, value: str) -> bool:
        """Whether ``value`` is one of this column's placeholders (§4)."""
        return value in self.placeholder_values or any(
            value.startswith(prefix) for prefix in self.placeholder_prefixes
        )


@dataclass
class Link:
    """A relationship of a model: ``target`` is ``None`` when its target is missing."""

    name: str
    target: Optional["Model"]
    target_name: str = ""


@dataclass
class Model:
    """One expose as an expression sees it."""

    name: str
    columns: Dict[str, Column] = field(default_factory=dict)
    links: Dict[str, Link] = field(default_factory=dict)
    #: The expose declares a validity window (a slowly changing dimension): no path,
    #: summary or shared attribute reaches it through a relationship in v1 (§2.2).
    validity_window: bool = False
