"""Recursive logical types shared by streaming DDL and physical catalog emitters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from fluid_build.providers._sql_safety import validate_ident

_ALIASES = {
    "str": "string",
    "text": "string",
    "varchar": "string",
    "integer": "int",
    "int32": "int",
    "int64": "long",
    "bigint": "long",
    "float32": "float",
    "float64": "double",
    "bool": "boolean",
    "bytes": "binary",
    "datetime": "timestamp",
    "timestamptz": "timestamp_ltz",
    "row": "struct",
    "record": "struct",
}
_FLINK = {
    "string": "STRING",
    "int": "INT",
    "long": "BIGINT",
    "float": "FLOAT",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "binary": "BYTES",
    "date": "DATE",
    "timestamp": "TIMESTAMP(6)",
    "timestamp_ltz": "TIMESTAMP_LTZ(6)",
}
_HIVE = {
    **{k: v.lower() for k, v in _FLINK.items()},
    "long": "bigint",
    "binary": "binary",
    "timestamp": "timestamp",
    "timestamp_ltz": "timestamp",
}
_DECIMAL = re.compile(r"decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_TIMESTAMP = re.compile(r"(timestamp|timestamp_ltz)\(\s*([0-6])\s*\)")
MAX_DEPTH = 32


@dataclass(frozen=True)
class LogicalType:
    kind: str
    required: bool = False
    fields: tuple[tuple[str, LogicalType], ...] = ()
    element: LogicalType | None = None
    key: LogicalType | None = None
    value: LogicalType | None = None
    precision: int | None = None
    scale: int | None = None

    def sql(self, dialect: str = "flink") -> str:
        if dialect not in {"flink", "hive"}:
            raise ValueError(f"Unsupported nested-type dialect: {dialect}")
        if self.kind == "struct":
            body = ", ".join(
                (
                    f"`{name}` {child.sql(dialect)}"
                    if dialect == "flink"
                    else f"{name}:{child.sql(dialect)}"
                )
                for name, child in self.fields
            )
            result = f"{'ROW' if dialect == 'flink' else 'struct'}<{body}>"
        elif self.kind == "array" and self.element is not None:
            result = f"{'ARRAY' if dialect == 'flink' else 'array'}<{self.element.sql(dialect)}>"
        elif self.kind == "map" and self.key is not None and self.value is not None:
            result = (
                f"{'MAP' if dialect == 'flink' else 'map'}"
                f"<{self.key.sql(dialect)}, {self.value.sql(dialect)}>"
            )
        elif self.kind == "decimal":
            result = f"DECIMAL({self.precision},{self.scale})"
        elif self.kind in {"timestamp", "timestamp_ltz"} and self.precision is not None:
            result = f"{self.kind.upper()}({self.precision})" if dialect == "flink" else "timestamp"
        else:
            result = (_FLINK if dialect == "flink" else _HIVE)[self.kind]
        if dialect == "flink" and self.required:
            result += " NOT NULL"
        return result


def parse_type(spec: Mapping[str, Any], *, path: str = "$", depth: int = 0) -> LogicalType:
    """Reject ambiguous/unsupported types rather than silently stringifying them."""
    if depth > MAX_DEPTH:
        raise ValueError(f"{path}: maximum nested type depth is {MAX_DEPTH}")
    raw = spec.get("type")
    if not isinstance(raw, str):
        raise ValueError(f"{path}: type must be a string")
    raw = raw.strip().lower()
    kind = _ALIASES.get(raw, raw)
    required = spec.get("required", False)
    if not isinstance(required, bool):
        raise ValueError(f"{path}: required must be boolean")
    children = {"fields", "element", "key", "value"} & spec.keys()
    allowed = {"struct": {"fields"}, "array": {"element"}, "map": {"key", "value"}}.get(kind, set())
    if children - allowed:
        raise ValueError(f"{path}: {kind} cannot declare {sorted(children - allowed)}")

    def child(key: str) -> LogicalType:
        value = spec.get(key)
        if not isinstance(value, Mapping):
            raise ValueError(f"{path}.{key}: typed definition is required")
        return parse_type(value, path=f"{path}.{key}", depth=depth + 1)

    if kind == "struct":
        fields = parse_fields(spec.get("fields"), path=path, depth=depth + 1)
        return LogicalType(kind, required, fields=fields)
    if kind == "array":
        return LogicalType(kind, required, element=child("element"))
    if kind == "map":
        key, value = child("key"), child("value")
        if key.kind != "string" or not key.required:
            raise ValueError(f"{path}.key: supported map keys are required strings")
        return LogicalType(kind, required, key=key, value=value)
    decimal = _DECIMAL.fullmatch(raw)
    if decimal:
        precision, scale = map(int, decimal.groups())
        if not 1 <= precision <= 38 or not 0 <= scale <= precision:
            raise ValueError(
                f"{path}: decimal requires 1 <= precision <= 38 and 0 <= scale <= precision"
            )
        return LogicalType("decimal", required, precision=precision, scale=scale)
    timestamp = _TIMESTAMP.fullmatch(raw)
    if timestamp:
        return LogicalType(timestamp[1], required, precision=int(timestamp[2]))
    if kind not in _FLINK:
        raise ValueError(
            f"{path}: unsupported streaming type {raw!r}; declare an explicit typed mapping"
        )
    return LogicalType(kind, required)


def parse_fields(
    fields: Any, *, path: str = "$", depth: int = 0
) -> tuple[tuple[str, LogicalType], ...]:
    if not isinstance(fields, list) or not fields:
        raise ValueError(f"{path}: a non-empty field list is required")
    result: list[tuple[str, LogicalType]] = []
    names: set[str] = set()
    for field in fields:
        if not isinstance(field, Mapping):
            raise ValueError(f"{path}: each field must be an object")
        name = field.get("name")
        if not isinstance(name, str):
            raise ValueError(f"{path}: field name is required")
        validate_ident(name)
        if name.casefold() in names:
            raise ValueError(f"{path}: duplicate field {name!r}")
        names.add(name.casefold())
        result.append((name, parse_type(field, path=f"{path}.{name}", depth=depth)))
    return tuple(result)


def columns_sql(fields: Sequence[tuple[str, LogicalType]]) -> str:
    return ",\n  ".join(f"`{name}` {kind.sql()}" for name, kind in fields)
