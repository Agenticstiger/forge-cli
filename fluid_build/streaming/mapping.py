# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Kafka records to output rows, for the opt-in managed Flink lane.

``properties.stream`` says what the records are and ``properties.mapping`` how they
become the exposed table's rows. forge-cli writes the job's query from the two: every
identifier passes ``validate_ident`` and every literal ``quote_ansi_string_literal``, so
no contract text reaches the SQL, and the job reads nothing but its own source table.

Every output column's type and nullability is checked against the path it is read from
before anything deploys. dbt's model contracts run that check against the warehouse;
here it is static, because the contract declares every source type. The source table is
the Flink Kafka connector's own DDL: key fields through ``key.fields`` with
``value.fields-include = 'EXCEPT_KEY'``, metadata as ``METADATA FROM '<key>' VIRTUAL``
(Flink 1.20 "Available Metadata"), and arrays flattened with ``CROSS JOIN UNNEST``.
Paths into the record follow Estuary's projections and Doris' ``jsonpaths``: one
declared path per output column, never an expression.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from fluid_build.providers._sql_safety import quote_ansi_string_literal, validate_ident
from fluid_build.streaming.types import LogicalType, columns_sql, parse_fields, parse_type

SOURCE_TABLE = "source_events"
VALUE_FORMATS = ("json", "avro-confluent")
KEY_FORMATS = ("raw", "json", "avro-confluent")
_SRC = "src"
_ELEM = "elem"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_ENTRY = r"\['([^'\\\x00-\x1f\x7f]{1,256})'\]"
_PATH = re.compile(rf"{_IDENT}(?:\.{_IDENT}|{_ENTRY})*")
_STEP = re.compile(rf"\.?({_IDENT})|{_ENTRY}")
_TARGET = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")
_TOPIC = re.compile(r"[A-Za-z0-9._-]{1,249}")
MAX_STEPS = 64

#: Kafka connector metadata: the DDL type Flink declares, and the logical type it reads as.
_METADATA: dict[str, tuple[str, LogicalType]] = {
    "topic": ("STRING NOT NULL", LogicalType("string", True)),
    "partition": ("INT NOT NULL", LogicalType("int", True)),
    "offset": ("BIGINT NOT NULL", LogicalType("long", True)),
    "timestamp": ("TIMESTAMP_LTZ(3) NOT NULL", LogicalType("timestamp_ltz", True, precision=3)),
    "timestamp-type": ("STRING NOT NULL", LogicalType("string", True)),
    "headers": (
        "MAP<STRING, BYTES> NOT NULL",
        LogicalType("map", True, key=LogicalType("string", True), value=LogicalType("binary")),
    ),
    "leader-epoch": ("INT", LogicalType("int")),
}
_JSON_OPTIONS = {
    "json.fail-on-missing-field": "false",
    "json.ignore-parse-errors": "false",
    "json.timestamp-format.standard": "ISO-8601",
}
_WIDENING = {("int", "long"), ("float", "double")}

Step = tuple[str, str]  # ("field", name) or ("entry", map key)


@dataclass(frozen=True)
class Stream:
    """``properties.stream``, parsed: the columns of the job's source table."""

    topic: str
    format: str
    value: tuple[tuple[str, LogicalType], ...]
    key_format: str | None = None
    key: tuple[tuple[str, LogicalType], ...] = ()
    key_prefix: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def columns(self) -> dict[str, LogicalType]:
        """Every column of the source table, by name: value, key, then metadata."""
        return {
            **dict(self.value),
            **dict(self.key),
            **{name: _METADATA[source][1] for name, source in self.metadata},
        }

    @property
    def uses_registry(self) -> bool:
        return "avro-confluent" in (self.format, self.key_format)

    def ddl(self) -> str:
        parts = [columns_sql(self.value)]
        if self.key:
            parts.append(columns_sql(self.key))
        parts.extend(
            f"`{name}` {_METADATA[source][0]} METADATA FROM "
            f"{quote_ansi_string_literal(source)} VIRTUAL"
            for name, source in self.metadata
        )
        return ",\n  ".join(parts)

    def format_options(self, registry_url: str | None) -> dict[str, str]:
        """Connector options for the value format and, when declared, the key's."""

        def options(fmt: str) -> dict[str, str]:
            if fmt == "json":
                return dict(_JSON_OPTIONS)
            if fmt == "avro-confluent" and registry_url:
                return {"avro-confluent.url": registry_url}
            return {}

        if self.key_format is None:
            return {"format": self.format, **options(self.format)}
        result = {
            "key.format": self.key_format,
            "key.fields": ";".join(name for name, _ in self.key),
            "value.format": self.format,
            "value.fields-include": "EXCEPT_KEY",
            **{f"value.{k}": v for k, v in options(self.format).items()},
            **{f"key.{k}": v for k, v in options(self.key_format).items()},
        }
        if self.key_prefix:
            result["key.fields-prefix"] = self.key_prefix
        return result


def _object(value: Any, where: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"{where}: unsupported field(s) {unknown}")
    return value


def parse_stream(spec: Any, where: str = "properties.stream") -> Stream:
    stream = _object(spec, where, {"kind", "topic", "format", "schema", "key", "metadata"})
    if stream.get("kind") != "kafka":
        raise ValueError(f"{where}.kind: only kafka streams are supported")
    topic = stream.get("topic")
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        raise ValueError(f"{where}.topic: expected one Kafka topic name")
    fmt = stream.get("format")
    if fmt not in VALUE_FORMATS:
        raise ValueError(f"{where}.format: expected one of {list(VALUE_FORMATS)}")
    value = parse_fields(stream.get("schema"), path=f"{where}.schema")
    key_format: str | None = None
    key: tuple[tuple[str, LogicalType], ...] = ()
    prefix = ""
    if "key" in stream:
        spec_key = _object(stream["key"], f"{where}.key", {"format", "schema", "prefix"})
        key_format = spec_key.get("format")
        if key_format not in KEY_FORMATS:
            raise ValueError(f"{where}.key.format: expected one of {list(KEY_FORMATS)}")
        prefix = spec_key.get("prefix", "")
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(_IDENT, prefix)):
            raise ValueError(f"{where}.key.prefix: expected an identifier")
        fields = parse_fields(spec_key.get("schema"), path=f"{where}.key.schema")
        if key_format == "raw" and (
            len(fields) != 1 or fields[0][1].kind not in {"string", "binary"}
        ):
            raise ValueError(f"{where}.key: a raw key is one column of type string or binary")
        key = tuple((validate_ident(prefix + name), dtype) for name, dtype in fields)
    metadata: list[tuple[str, str]] = []
    entries = stream.get("metadata", [])
    if not isinstance(entries, list) or ("metadata" in stream and not entries):
        raise ValueError(f"{where}.metadata: expected a non-empty list")
    for i, entry in enumerate(entries):
        item = _object(entry, f"{where}.metadata[{i}]", {"name", "from", "description"})
        name, source = item.get("name"), item.get("from")
        if not isinstance(name, str):
            raise ValueError(f"{where}.metadata[{i}].name: a column name is required")
        if source not in _METADATA:
            raise ValueError(f"{where}.metadata[{i}].from: expected one of {sorted(_METADATA)}")
        metadata.append((validate_ident(name), source))
    seen: set[str] = set()
    for name in [n for n, _ in value] + [n for n, _ in key] + [n for n, _ in metadata]:
        if name.casefold() in seen:
            raise ValueError(
                f"{where}: column {name!r} is declared twice across schema, key and metadata"
                + ("; set key.prefix" if key else "")
            )
        seen.add(name.casefold())
    return Stream(topic, fmt, value, key_format, key, prefix, tuple(metadata))


def parse_path(text: Any, where: str) -> tuple[Step, ...]:
    """``customer.address.country`` or ``attributes['channel']``, never SQL."""
    if not isinstance(text, str) or not _PATH.fullmatch(text):
        raise ValueError(
            f"{where}: expected a path such as customer.address.country or attributes['channel']"
        )
    steps = tuple(("field", m[1]) if m[1] else ("entry", m[2]) for m in _STEP.finditer(text))
    if len(steps) > MAX_STEPS:
        raise ValueError(f"{where}: a path has at most {MAX_STEPS} steps")
    return steps


def show(steps: Sequence[Step]) -> str:
    text = ""
    for kind, name in steps:
        text += f"['{name}']" if kind == "entry" else (f".{name}" if text else name)
    return text


def _nullable(dtype: LogicalType, nullable: bool) -> LogicalType:
    return dataclasses.replace(dtype, required=not nullable)


def _sql_type(dtype: LogicalType) -> str:
    """The type in a CAST: nullability inside it kept, the outer NOT NULL dropped."""
    return _nullable(dtype, True).sql()


def _describe(dtype: LogicalType) -> str:
    if dtype.kind == "decimal":
        return f"decimal({dtype.precision},{dtype.scale})"
    if dtype.kind in {"timestamp", "timestamp_ltz"}:
        return f"{dtype.kind}({_precision(dtype)})"
    return dtype.kind


def _precision(dtype: LogicalType) -> int:
    return 6 if dtype.precision is None else dtype.precision


def _difference(src: LogicalType, tgt: LogicalType, path: str) -> str | None:
    """The first way ``src`` cannot be written to ``tgt`` as it is, or ``None``."""
    at = f" at {path}" if path else ""
    if tgt.required and not src.required:
        return f"a value{at} can be null, but the output requires it"
    if src.kind != tgt.kind:
        return f"{_describe(src)}{at} is not {_describe(tgt)}"
    if src.kind == "struct":
        names = [name for name, _ in src.fields]
        if names != [name for name, _ in tgt.fields]:
            return f"struct fields{at} are {names}, not {[name for name, _ in tgt.fields]}"
        for (name, child), (_, other) in zip(src.fields, tgt.fields, strict=True):
            found = _difference(child, other, f"{path}.{name}" if path else name)
            if found:
                return found
        return None
    if src.kind == "array" and src.element is not None and tgt.element is not None:
        return _difference(src.element, tgt.element, f"{path}[]" if path else "[]")
    if src.kind == "map" and src.value is not None and tgt.value is not None:
        # Iceberg map keys are always required, and both sides' keys are strings.
        return _difference(src.value, tgt.value, f"{path}{{value}}" if path else "{value}")
    if _describe(src) != _describe(tgt):
        return f"{_describe(src)}{at} is not {_describe(tgt)}"
    return None


def _widens(src: LogicalType, tgt: LogicalType) -> bool:
    """A scalar the output can hold without loss: Iceberg's own promotions, and finer time."""
    if (src.kind, tgt.kind) in _WIDENING:
        return True
    if src.kind == tgt.kind == "decimal":
        if src.precision is None or tgt.precision is None:
            return False
        return src.scale == tgt.scale and tgt.precision > src.precision
    if src.kind == tgt.kind and src.kind in {"timestamp", "timestamp_ltz"}:
        return _precision(tgt) > _precision(src)
    return False


@dataclass(frozen=True)
class _Ref:
    sql: str
    dtype: LogicalType  # the outer ``required`` is False when the value can be null
    origin: tuple[Step, ...] | None  # the path from a source column; None from the element


@dataclass(frozen=True)
class _Out:
    sql: str
    nullable: bool
    origins: tuple[tuple[Step, ...] | None, ...]


@dataclass(frozen=True)
class _Override:
    where: str
    source: tuple[Step, ...]
    cast: LogicalType | None


class _Compiler:
    def __init__(self, stream: Stream, spec: Any, where: str, target_where: str) -> None:
        self.columns = stream.columns
        self.where = where
        self.target_where = target_where
        self.alias: str | None = None
        self.element: LogicalType | None = None
        self.alias_used = False
        self.join = ""
        mapping = _object({} if spec is None else spec, where, {"explode", "columns"})
        if "explode" in mapping:
            self._explode(_object(mapping["explode"], f"{where}.explode", {"path", "as"}))
        self.overrides: dict[str, _Override] = {}
        entries = mapping.get("columns", [])
        if not isinstance(entries, list) or ("columns" in mapping and not entries):
            raise ValueError(f"{where}.columns: expected a non-empty list")
        for i, entry in enumerate(entries):
            at = f"{where}.columns[{i}]"
            item = _object(entry, at, {"name", "from", "cast", "description"})
            name = item.get("name")
            if not isinstance(name, str) or not _TARGET.fullmatch(name):
                raise ValueError(f"{at}.name: expected an output column or a dotted path into one")
            if name in self.overrides:
                raise ValueError(f"{at}.name: {name} is mapped twice")
            cast = None
            if "cast" in item:
                if str(item["cast"]).strip().lower() in {"struct", "row", "record", "array", "map"}:
                    raise ValueError(f"{at}.cast: only a scalar type can be a conversion target")
                cast = parse_type({"type": item["cast"]}, path=f"{at}.cast")
            self.overrides[name] = _Override(at, parse_path(item.get("from"), f"{at}.from"), cast)
        self.used: set[str] = set()

    def _explode(self, spec: Mapping[str, Any]) -> None:
        at = f"{self.where}.explode"
        array = self.resolve(parse_path(spec.get("path"), f"{at}.path"), f"{at}.path")
        if array.dtype.kind != "array" or array.dtype.element is None:
            raise ValueError(f"{at}.path: {show(array.origin or ())} is not an array")
        alias = spec.get("as")
        if not isinstance(alias, str) or not re.fullmatch(_IDENT, alias):
            raise ValueError(f"{at}.as: expected an identifier")
        if alias in self.columns:
            raise ValueError(f"{at}.as: {alias} is already a column of the stream")
        element = array.dtype.element
        names = [name for name, _ in element.fields] if element.kind == "struct" else [alias]
        self.alias, self.element = alias, element
        self.join = (
            f"\nCROSS JOIN UNNEST({array.sql}) AS `{_ELEM}` "
            f"({', '.join(f'`{name}`' for name in names)})"
        )

    def resolve(self, steps: tuple[Step, ...], where: str) -> _Ref:
        root = steps[0][1]
        rest = steps[1:]
        if self.alias is not None and self.element is not None and root == self.alias:
            self.alias_used = True
            element, nullable = self.element, not self.element.required
            if element.kind != "struct":
                ref = _Ref(f"`{_ELEM}`.`{root}`", _nullable(element, nullable), None)
            elif rest and rest[0][0] == "field":
                by_name = dict(element.fields)
                if rest[0][1] not in by_name:
                    raise ValueError(f"{where}: the element of {root} has no field {rest[0][1]}")
                child = by_name[rest[0][1]]
                ref = _Ref(
                    f"`{_ELEM}`.`{rest[0][1]}`",
                    _nullable(child, nullable or not child.required),
                    None,
                )
                rest = rest[1:]
            else:
                # UNNEST spreads a struct element over columns, so the whole element is
                # rebuilt from them. A rebuilt row is never null; its fields carry the nulls.
                fields = tuple(
                    (name, _nullable(child, nullable or not child.required))
                    for name, child in element.fields
                )
                rebuilt = LogicalType("struct", True, fields=fields)
                cells = ", ".join(f"`{_ELEM}`.`{name}`" for name, _ in fields)
                ref = _Ref(f"CAST(ROW({cells}) AS {_sql_type(rebuilt)})", rebuilt, None)
        elif root in self.columns:
            ref = _Ref(f"`{_SRC}`.`{root}`", self.columns[root], (steps[0],))
        else:
            raise ValueError(f"{where}: {root} is not a column of properties.stream")
        for step in rest:
            ref = self._step(ref, step, where)
        return ref

    def _step(self, ref: _Ref, step: Step, where: str) -> _Ref:
        kind, name = step
        dtype = ref.dtype
        origin = None if ref.origin is None else (*ref.origin, step)
        if kind == "field":
            if dtype.kind != "struct":
                raise ValueError(f"{where}: {_describe(dtype)} has no field {name}")
            fields = dict(dtype.fields)
            if name not in fields:
                raise ValueError(f"{where}: the struct has no field {name}")
            child = fields[name]
            nullable = not dtype.required or not child.required
            return _Ref(f"{ref.sql}.`{name}`", _nullable(child, nullable), origin)
        if dtype.kind != "map" or dtype.value is None:
            raise ValueError(f"{where}: ['{name}'] needs a map, not {_describe(dtype)}")
        # A key the record does not carry reads as null.
        literal = quote_ansi_string_literal(name)
        return _Ref(f"{ref.sql}[{literal}]", _nullable(dtype.value, True), origin)

    def _assign(self, ref: _Ref, target: LogicalType, override: _Override | None, name: str) -> str:
        where = override.where if override else f"{self.target_where}.{name}"
        source = ref.dtype
        sql = ref.sql
        if override is not None and override.cast is not None:
            cast = override.cast
            convertible = (
                (source.kind, cast.kind) == ("binary", "string")
                or _widens(source, cast)
                or _difference(_nullable(source, True), _nullable(cast, True), "") is None
            )
            if not convertible:
                raise ValueError(
                    f"{where}.cast: {_describe(source)} cannot become {_describe(cast)}; "
                    "supported: binary to string, and widening"
                )
            sql = f"CAST({sql} AS {_sql_type(cast)})"
            source = _nullable(cast, not source.required)
        difference = _difference(source, target, "")
        if difference is None:
            return sql
        if _widens(source, target) and (source.required or not target.required):
            return f"CAST({sql} AS {_sql_type(target)})"
        source_path = show(override.source) if override else f"the stream's {name}"
        raise ValueError(f"{where}: {name} cannot be read from {source_path}: {difference}")

    def target(self, path: tuple[str, ...], dtype: LogicalType) -> _Out:
        name = ".".join(path)
        override = self.overrides.get(name)
        nested = [key for key in self.overrides if key.startswith(name + ".")]
        if override is not None:
            if nested:
                raise ValueError(f"{override.where}: {nested[0]} is inside {name}, mapped whole")
            self.used.add(name)
            ref = self.resolve(override.source, f"{override.where}.from")
            sql = self._assign(ref, dtype, override, name)
            return _Out(sql, not ref.dtype.required, (ref.origin,))
        if dtype.kind == "struct" and nested:
            return self._row(path, dtype)
        steps = tuple(("field", part) for part in path)
        try:
            ref = self.resolve(steps, f"{self.target_where}.{name}")
        except ValueError as exc:
            raise ValueError(f"{exc}; map {name} in properties.mapping.columns") from None
        if (
            dtype.kind == "struct"
            and ref.dtype.kind == "struct"
            and _difference(ref.dtype, dtype, "") is not None
        ):
            return self._row(path, dtype)
        return _Out(self._assign(ref, dtype, None, name), not ref.dtype.required, (ref.origin,))

    def _row(self, path: tuple[str, ...], dtype: LogicalType) -> _Out:
        """A struct output built field by field, from the paths its fields are read from."""
        children = [self.target((*path, name), child) for name, child in dtype.fields]
        origins = tuple(origin for child in children for origin in child.origins)
        sql = f"CAST(ROW({', '.join(child.sql for child in children)}) AS {_sql_type(dtype)})"
        parent = self._shared_struct(origins)
        if parent is None:
            return _Out(sql, False, origins)
        if dtype.required:
            raise ValueError(
                f"{self.target_where}.{'.'.join(path)}: required, but its fields are read "
                f"from {show(parent.origin or ())}, which can be null"
            )
        # Fields read from one nullable struct: the output is null where that struct is.
        sql = f"CASE WHEN {parent.sql} IS NULL THEN CAST(NULL AS {_sql_type(dtype)}) ELSE {sql} END"
        return _Out(sql, True, origins)

    def _shared_struct(self, origins: tuple[tuple[Step, ...] | None, ...]) -> _Ref | None:
        """The nullable source struct every field of a built row is read from, if one is."""
        paths = [origin for origin in origins if origin is not None]
        if not paths or len(paths) != len(origins):
            return None
        prefix = list(paths[0])
        for path in paths[1:]:
            common = 0
            while common < min(len(prefix), len(path)) and prefix[common] == path[common]:
                common += 1
            prefix = prefix[:common]
        while prefix:
            if all(len(path) > len(prefix) for path in paths):
                ref = self.resolve(tuple(prefix), self.where)
                if ref.dtype.kind == "struct":
                    return None if ref.dtype.required else ref
            prefix.pop()
        return None


def compile_select(
    stream: Stream,
    mapping: Any,
    target: Sequence[tuple[str, LogicalType]],
    *,
    where: str = "properties.mapping",
    target_where: str = "exposes.contract.schema",
) -> str:
    """The query forge-cli writes: every output column in the expose's order, checked."""
    compiler = _Compiler(stream, mapping, where, target_where)
    select = [f"{compiler.target((name, ), dtype).sql} AS `{name}`" for name, dtype in target]
    unused = sorted(set(compiler.overrides) - compiler.used)
    if unused:
        raise ValueError(
            f"{compiler.overrides[unused[0]].where}.name: {unused[0]} is not an output column "
            "or a struct field of one (a field inside an array or map is mapped with its column)"
        )
    if compiler.alias is not None and not compiler.alias_used:
        raise ValueError(f"{where}.explode.as: no output column reads {compiler.alias}")
    return (
        "SELECT\n  " + ",\n  ".join(select) + f"\nFROM `{SOURCE_TABLE}` AS `{_SRC}`" + compiler.join
    )


def bind_by_name(sql: str, target: Sequence[tuple[str, LogicalType]]) -> str:
    """A contract's own SELECT, read by output column name instead of by position.

    A column it does not produce then fails Flink's planner instead of shifting the
    others; the planner, in the pod, also checks the types.
    """
    columns = ", ".join(f"`{name}`" for name, _ in target)
    return f"SELECT {columns}\nFROM (\n{sql.strip()}\n) AS `contract_sql`"
