# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Kafka records to output rows, for the managed Flink lane and the forge Spark runner.

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

The same mapping compiles to Spark SQL (``dialect="spark"``, Spark 3.5) for the forge
Spark runner. Arrays flatten with ``LATERAL VIEW inline`` (struct elements) or
``LATERAL VIEW explode`` (scalars), a built struct is ``named_struct`` with the same
null propagation, and :func:`spark_source_select` reads the value, key and metadata
columns from the Spark Kafka source's own columns (``key``, ``value``, ``topic``,
``partition``, ``offset``, ``timestamp``, ``timestampType``, ``headers``) once the
runner has decoded ``key`` and ``value``. A mapped column may set ``maskAtIngest:
hash``, Spark only: the column is the SHA-256 of the source, in lowercase hex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from fluid_build.providers._sql_safety import quote_ansi_string_literal, validate_ident
from fluid_build.streaming.types import LogicalType, columns_sql, parse_fields, parse_type

SOURCE_TABLE = "source_events"
#: The relation the Spark runner registers: the Kafka source's columns, with ``key``
#: and ``value`` decoded (a struct, or the raw bytes of a raw key).
SPARK_RECORDS_TABLE = "kafka_records"
DIALECTS = ("flink", "spark")
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
_SPARK_TYPES = {
    "string": "STRING",
    "int": "INT",
    "long": "BIGINT",
    "float": "FLOAT",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "binary": "BINARY",
    "date": "DATE",
    # Spark 3.5's TIMESTAMP is the instant (local time zone); TIMESTAMP_NTZ is the
    # wall-clock time, as Iceberg's timestamptz and timestamp map to Spark's.
    "timestamp": "TIMESTAMP_NTZ",
    "timestamp_ltz": "TIMESTAMP",
}
#: Spark's Kafka source column each metadata key reads, as the source table's column.
#: Spark reads the timestamp type as an int (Kafka's TimestampType id); Flink's name
#: for it is the column's value. Headers arrive as an array of key/value structs.
_SPARK_METADATA = {
    "topic": "`topic`",
    "partition": "`partition`",
    "offset": "`offset`",
    "timestamp": "`timestamp`",
    "timestamp-type": (
        "CASE `timestampType` WHEN 0 THEN 'CreateTime' WHEN 1 THEN 'LogAppendTime' "
        "ELSE 'NoTimestampType' END"
    ),
    "headers": "COALESCE(map_from_entries(`headers`), CAST(map() AS MAP<STRING, BINARY>))",
}
#: Source types a ``maskAtIngest: hash`` column may read: identifiers, whose text form
#: does not depend on a session setting (a timestamp's does, on the time zone).
_MASKABLE = {"string", "binary", "int", "long"}

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


def spark_type(dtype: LogicalType) -> str:
    """The Spark SQL type of ``dtype``, nullability left out: a CAST cannot narrow it."""
    if dtype.kind == "struct":
        return (
            f"STRUCT<{', '.join(f'`{name}`: {spark_type(child)}' for name, child in dtype.fields)}>"
        )
    if dtype.kind == "array" and dtype.element is not None:
        return f"ARRAY<{spark_type(dtype.element)}>"
    if dtype.kind == "map" and dtype.key is not None and dtype.value is not None:
        return f"MAP<{spark_type(dtype.key)}, {spark_type(dtype.value)}>"
    if dtype.kind == "decimal":
        return f"DECIMAL({dtype.precision},{dtype.scale})"
    return _SPARK_TYPES[dtype.kind]


def spark_schema(fields: Sequence[tuple[str, LogicalType]]) -> str:
    """A field list as a Spark DDL schema string, as ``from_json`` takes it."""
    return ", ".join(f"`{name}` {spark_type(dtype)}" for name, dtype in fields)


def _spark_literal(value: str) -> str:
    """A Spark string literal. Spark reads ``\\`` as an escape, so a value holding a
    quote, a backslash or a control character is refused instead of escaped; no path,
    field name or metadata key that reaches here can hold one."""
    # ``${...}`` is refused too: spark.sql() expands ${env:..} and ${spark:..} in the
    # query text before it parses it (spark.sql.variable.substitute, on by default).
    if re.search(r"['\\\x00-\x1f\x7f]|\$\{", value):
        raise ValueError(f"{value!r} cannot be a Spark string literal")
    return f"'{value}'"


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
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic) or topic in {".", ".."}:
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
        if "prefix" in spec_key and (
            not isinstance(prefix, str) or not re.fullmatch(_IDENT, prefix)
        ):
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
        if not isinstance(source, str) or source not in _METADATA:
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


def _needs_read(dtype: LogicalType | None) -> bool:
    """Whether a CAST would not do: a struct, a required value, or one inside."""
    if dtype is None:
        return False
    return (
        dtype.kind == "struct"
        or dtype.required
        or _needs_read(dtype.element)
        or _needs_read(dtype.value)
    )


def _spark_read(sql: str, dtype: LogicalType, label: str, depth: int = 0) -> str:
    """``sql`` read as ``dtype``: a struct field by field, by name; a required value checked.

    Spark casts one struct to another by position, so a decoded record whose struct
    holds the same fields in another order (an Avro writer schema's order) would swap
    their values under a CAST. Reading each field by name keeps them apart. A null
    struct stays null, and arrays and map values are read element by element.

    Spark types carry no NOT NULL, so a required value the record holds as null (a
    JSON record without the field) fails the job with ``raise_error``, as Flink fails
    a NOT NULL column; a required field of a null struct is not read.
    """
    if dtype.kind == "struct":
        cells = ", ".join(
            f"{_spark_literal(name)}, "
            f"{_spark_read(f'{sql}.`{name}`', child, f'{label}.{name}', depth)}"
            for name, child in dtype.fields
        )
        read = f"named_struct({cells})"
        if not dtype.required:
            return f"CASE WHEN {sql} IS NULL THEN NULL ELSE {read} END"
    elif dtype.kind == "array" and dtype.element is not None and _needs_read(dtype.element):
        var = f"`fluid_e{depth}`"
        element = _spark_read(var, dtype.element, f"{label}[]", depth + 1)
        read = f"transform({sql}, {var} -> {element})"
    elif dtype.kind == "map" and dtype.value is not None and _needs_read(dtype.value):
        key, var = f"`fluid_k{depth}`", f"`fluid_v{depth}`"
        value = _spark_read(var, dtype.value, f"{label}[]", depth + 1)
        read = f"transform_values({sql}, ({key}, {var}) -> {value})"
    else:
        read = f"CAST({sql} AS {spark_type(dtype)})"
    if not dtype.required:
        return read
    message = _spark_literal(f"fluid: required field {label} is null in a Kafka record")
    return f"CASE WHEN {sql} IS NULL THEN raise_error({message}) ELSE {read} END"


def spark_source_select(stream: Stream, where: str = "properties.stream") -> str:
    """The Spark query that makes ``source_events`` from the decoded Kafka records.

    The runner registers :data:`SPARK_RECORDS_TABLE`: the Kafka source's columns, with
    ``value`` decoded into a struct and ``key`` decoded into one (a raw key stays the
    record's bytes). Every column is read as its declared type, structs field by field
    by name (:func:`_spark_read`), so a field the decoded record lacks fails the query
    at the job's start. The runner checks the decoded types against the declared ones
    before it runs this, because a Spark cast that loses data returns null instead of
    failing.
    """
    columns = [
        f"{_spark_read(f'`value`.`{name}`', dtype, name)} AS `{name}`"
        for name, dtype in stream.value
    ]
    if stream.key_format == "raw":
        ((name, dtype),) = stream.key
        columns.append(f"{_spark_read('`key`', dtype, f'key {name}')} AS `{name}`")
    else:
        prefix = len(stream.key_prefix)
        columns.extend(
            f"{_spark_read(f'`key`.`{name[prefix:]}`', dtype, f'key {name}')} AS `{name}`"
            for name, dtype in stream.key
        )
    for name, source in stream.metadata:
        if source not in _SPARK_METADATA:
            raise ValueError(
                f"{where}.metadata: Spark's Kafka source does not read {source}; "
                f"it reads {sorted(_SPARK_METADATA)}"
            )
        columns.append(f"{_SPARK_METADATA[source]} AS `{name}`")
    return "SELECT\n  " + ",\n  ".join(columns) + f"\nFROM `{SPARK_RECORDS_TABLE}`"


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
    return replace(dtype, required=not nullable)


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


@dataclass(frozen=True)
class _Override:
    where: str
    source: tuple[Step, ...]
    cast: LogicalType | None
    mask: bool = False


class _Compiler:
    def __init__(
        self, stream: Stream, spec: Any, where: str, target_where: str, dialect: str = "flink"
    ) -> None:
        if dialect not in DIALECTS:
            raise ValueError(f"unsupported mapping dialect {dialect!r}; expected {list(DIALECTS)}")
        self.spark = dialect == "spark"
        self.columns = stream.columns
        self.where = where
        self.target_where = target_where
        self.alias: str | None = None
        self.element: LogicalType | None = None
        self.alias_used = False
        self.join = ""
        # Source structs a row being built is known to read with a value: it is null
        # where they are, so their own nullability does not reach its fields.
        self.present: list[tuple[Step, ...]] = []
        mapping = _object({} if spec is None else spec, where, {"explode", "columns"})
        if "explode" in mapping:
            self._explode(_object(mapping["explode"], f"{where}.explode", {"path", "as"}))
        self.overrides: dict[str, _Override] = {}
        entries = mapping.get("columns", [])
        if not isinstance(entries, list) or ("columns" in mapping and not entries):
            raise ValueError(f"{where}.columns: expected a non-empty list")
        for i, entry in enumerate(entries):
            name, override = self._column(entry, f"{where}.columns[{i}]")
            if name in self.overrides:
                raise ValueError(f"{override.where}.name: {name} is mapped twice")
            self.overrides[name] = override
        # Each struct output path with a mapped field under it, and the first such field,
        # found once instead of scanning every override for every output path.
        self.inside: dict[str, str] = {}
        for name in self.overrides:
            parts = name.split(".")
            for end in range(1, len(parts)):
                self.inside.setdefault(".".join(parts[:end]), name)
        self.used: set[str] = set()

    def _column(self, entry: Any, at: str) -> tuple[str, _Override]:
        """One ``mapping.columns`` entry: its output name and where it is read from."""
        if not self.spark and isinstance(entry, Mapping) and "maskAtIngest" in entry:
            raise ValueError(
                f"{at}.maskAtIngest: hashing at ingest is a Spark runner feature; the "
                "managed Flink lane does not apply it"
            )
        keys = {"name", "from", "cast", "description"} | ({"maskAtIngest"} if self.spark else set())
        item = _object(entry, at, keys)
        name = item.get("name")
        if not isinstance(name, str) or not _TARGET.fullmatch(name):
            raise ValueError(f"{at}.name: expected an output column or a dotted path into one")
        cast = None
        if "cast" in item:
            if str(item["cast"]).strip().lower() in {"struct", "row", "record", "array", "map"}:
                raise ValueError(f"{at}.cast: only a scalar type can be a conversion target")
            cast = parse_type({"type": item["cast"]}, path=f"{at}.cast")
        mask = "maskAtIngest" in item
        if mask and item["maskAtIngest"] != "hash":
            raise ValueError(f"{at}.maskAtIngest: expected hash")
        if mask and cast is not None:
            raise ValueError(f"{at}: a masked column is the hash of its source; drop cast")
        return name, _Override(at, parse_path(item.get("from"), f"{at}.from"), cast, mask)

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
        cells = ", ".join(f"`{name}`" for name in names)
        if self.spark:
            # inline spreads a struct element over columns, as UNNEST does; explode
            # yields a scalar element. Both drop a row whose array is null or empty.
            function = "inline" if element.kind == "struct" else "explode"
            self.join = f"\nLATERAL VIEW {function}({array.sql}) `{_ELEM}` AS {cells}"
        else:
            self.join = f"\nCROSS JOIN UNNEST({array.sql}) AS `{_ELEM}` ({cells})"

    def resolve(self, steps: tuple[Step, ...], where: str) -> _Ref:
        root = steps[0][1]
        rest = steps[1:]
        done = 1
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
                done = 2
            else:
                # UNNEST spreads a struct element over columns, so the whole element is
                # rebuilt from them. A rebuilt row is never null; its fields carry the nulls.
                fields = tuple(
                    (name, _nullable(child, nullable or not child.required))
                    for name, child in element.fields
                )
                rebuilt = LogicalType("struct", True, fields=fields)
                cells = [(name, f"`{_ELEM}`.`{name}`") for name, _ in fields]
                ref = _Ref(self._struct(cells, rebuilt), rebuilt, None)
        elif root in self.columns:
            ref = self._settle(_Ref(f"`{_SRC}`.`{root}`", self.columns[root], (steps[0],)))
        else:
            raise ValueError(f"{where}: {root} is not a column of properties.stream")
        for step in rest:
            ref = self._settle(self._step(ref, step, where, show(steps[:done])))
            done += 1
        return ref

    def _struct(self, cells: Sequence[tuple[str, str]], dtype: LogicalType) -> str:
        """A struct value built from its fields' SQL, never null itself."""
        if self.spark:
            pairs = ", ".join(f"{_spark_literal(name)}, {sql}" for name, sql in cells)
            return f"named_struct({pairs})"
        return f"CAST(ROW({', '.join(sql for _, sql in cells)}) AS {_sql_type(dtype)})"

    def _cast_type(self, dtype: LogicalType) -> str:
        return spark_type(dtype) if self.spark else _sql_type(dtype)

    def _settle(self, ref: _Ref) -> _Ref:
        if ref.origin is not None and ref.origin in self.present:
            return replace(ref, dtype=_nullable(ref.dtype, False))
        return ref

    def _step(self, ref: _Ref, step: Step, where: str, label: str) -> _Ref:
        kind, name = step
        dtype = ref.dtype
        origin = None if ref.origin is None else (*ref.origin, step)
        if kind == "field":
            if dtype.kind != "struct":
                raise ValueError(f"{where}: {label} is {_describe(dtype)}, not a struct")
            fields = dict(dtype.fields)
            if name not in fields:
                raise ValueError(f"{where}: {label} has no field {name}")
            child = fields[name]
            nullable = not dtype.required or not child.required
            return _Ref(f"{ref.sql}.`{name}`", _nullable(child, nullable), origin)
        if dtype.kind != "map" or dtype.value is None:
            raise ValueError(f"{where}: {label} is {_describe(dtype)}, not a map")
        # A key the record does not carry reads as null.
        literal = _spark_literal(name) if self.spark else quote_ansi_string_literal(name)
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
            sql = f"CAST({sql} AS {self._cast_type(cast)})"
            source = _nullable(cast, not source.required)
        difference = _difference(source, target, "")
        if difference is None:
            return sql
        if _widens(source, target) and (source.required or not target.required):
            return f"CAST({sql} AS {self._cast_type(target)})"
        source_path = show(override.source) if override else f"the stream's {name}"
        raise ValueError(f"{where}: {name} cannot be read from {source_path}: {difference}")

    def _mask(self, ref: _Ref, target: LogicalType, override: _Override, name: str) -> str:
        """``maskAtIngest: hash``: the SHA-256 of the source, lowercase hex; null stays null."""
        where = f"{override.where}.maskAtIngest"
        if ref.dtype.kind not in _MASKABLE:
            raise ValueError(
                f"{where}: {show(override.source)} is {_describe(ref.dtype)}; a hash reads "
                f"one of {sorted(_MASKABLE)}"
            )
        if target.kind != "string":
            raise ValueError(f"{where}: {name} is {_describe(target)}; a hash is a string")
        if target.required and not ref.dtype.required:
            raise ValueError(
                f"{where}: {name} is required, but {show(override.source)} can be null"
            )
        value = ref.sql if ref.dtype.kind == "binary" else f"CAST({ref.sql} AS STRING)"
        return f"sha2({value}, 256)"

    def target(self, path: tuple[str, ...], dtype: LogicalType) -> _Out:
        name = ".".join(path)
        override = self.overrides.get(name)
        nested = self.inside.get(name)
        if override is not None:
            if nested:
                raise ValueError(f"{override.where}: {nested} is inside {name}, mapped whole")
            self.used.add(name)
            ref = self.resolve(override.source, f"{override.where}.from")
            if override.mask:
                return _Out(self._mask(ref, dtype, override, name), not ref.dtype.required)
            sql = self._assign(ref, dtype, override, name)
            return _Out(sql, not ref.dtype.required)
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
        return _Out(self._assign(ref, dtype, None, name), not ref.dtype.required)

    def _row(self, path: tuple[str, ...], dtype: LogicalType) -> _Out:
        """A struct output built field by field, from the paths its fields are read from."""
        planned = tuple(
            origin for name, child in dtype.fields for origin in self._plan((*path, name), child)
        )
        parent = self._shared_struct(planned)
        if parent is not None and dtype.required:
            raise ValueError(
                f"{self.target_where}.{'.'.join(path)}: required, but its fields are read "
                f"from {show(parent.origin or ())}, which can be null"
            )
        if parent is not None and parent.origin is not None:
            self.present.append(parent.origin)
        try:
            children = [self.target((*path, name), child) for name, child in dtype.fields]
        finally:
            if parent is not None and parent.origin is not None:
                self.present.pop()
        if self.spark:
            names = [name for name, _ in dtype.fields]
            built = self._struct(list(zip(names, (c.sql for c in children), strict=True)), dtype)
            if parent is None:
                return _Out(built, False)
            # Fields read from one nullable struct: the output is null where that struct is.
            return _Out(f"CASE WHEN {parent.sql} IS NULL THEN NULL ELSE {built} END", True)
        row = f"ROW({', '.join(child.sql for child in children)})"
        if parent is None:
            return _Out(f"CAST({row} AS {_sql_type(dtype)})", False)
        # Fields read from one nullable struct: the output is null where that struct is.
        # Flink 1.20's validator refuses a CAST(ROW(...)) as a CASE branch, so the bare
        # ROW is the branch and the CASE is cast to the output's type.
        case = f"CASE WHEN {parent.sql} IS NULL THEN NULL ELSE {row} END"
        return _Out(f"CAST({case} AS {_sql_type(dtype)})", True)

    def _plan(
        self, path: tuple[str, ...], dtype: LogicalType
    ) -> tuple[tuple[Step, ...] | None, ...]:
        """Where each field of a row to be built is read from, before any of it is checked."""
        name = ".".join(path)
        override = self.overrides.get(name)
        if override is not None:
            steps = override.source
        elif dtype.kind == "struct" and name in self.inside:
            return tuple(o for child, t in dtype.fields for o in self._plan((*path, child), t))
        else:
            steps = tuple(("field", part) for part in path)
        return (steps if steps[0][1] in self.columns and steps[0][1] != self.alias else None,)

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
                try:
                    ref = self.resolve(tuple(prefix), self.where)
                except ValueError:
                    return None  # the fields' own paths report it
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
    dialect: str = "flink",
) -> str:
    """The query forge-cli writes: every output column in the expose's order, checked.

    ``dialect`` is ``flink`` (the managed Flink lane) or ``spark`` (the forge Spark
    runner, Spark 3.5); both read :data:`SOURCE_TABLE` as ``src``.
    """
    compiler = _Compiler(stream, mapping, where, target_where, dialect)
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
