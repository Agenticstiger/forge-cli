# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The registry schema forge derives from a column list, and the columns a registry holds.

Both directions go through forge's one type mapping, ``streaming/types.py::parse_type``,
so a contract type the stream lanes refuse is refused here too.

Columns to registry schema (:func:`avro_schema`, :func:`json_schema`):

* Avro follows the Avro 1.12 specification and the conventions datacontract-cli's
  ODCS->Avro exporter uses (MIT; ``datacontract/export/avro_exporter.py``): a nullable
  column is ``["null", T]`` with ``default: null``; ``binary`` is ``bytes``; ``date`` is
  ``int`` + ``logicalType: date``; ``timestamp`` is ``local-timestamp-*`` and
  ``timestamp_ltz`` is ``timestamp-*`` (millis for precision 0-3, else micros); a
  decimal is ``bytes`` + ``decimal``; a struct is a record, an array an array, a map a map.
* JSON Schema is draft-07, which Confluent's and Glue's JSON Schema support both read:
  int and long are ``integer``, float, double and decimal ``number``, a date or a
  timestamp a ``string`` with ``format``, binary a base64 ``string``. A nullable column
  adds ``"null"`` to its ``type``. Undeclared fields stay allowed, as a ``json`` stream
  ignores them.

:func:`schema_text` is the canonical text: keys sorted, no whitespace, so the same
columns always give the same bytes and a plan digest stays stable.

Protobuf (:func:`protobuf_schema`, produced subjects only) is canonical proto3 text:

* Scalars: string, ``int32`` (int), ``int64`` (long), float, double, bool, bytes.
* Well-known types, borrowed from Kafka Connect's ``ProtobufConverter`` mapping in
  confluentinc/schema-registry (``ProtobufData``: Date -> ``google.type.Date``,
  Timestamp -> ``google.protobuf.Timestamp``, Decimal -> ``confluent.type.Decimal``):
  ``date`` is ``google.type.Date`` and ``timestamp``/``timestamp_ltz`` are
  ``google.protobuf.Timestamp``, for both registry kinds. A ``decimal`` is the kind's
  ``protobuf_decimal`` (``confluent.type.Decimal`` for Confluent, which has
  ``confluent/type/decimal.proto`` built in); Glue resolves no decimal type for Protobuf,
  so a decimal column in a Glue Protobuf subject is refused. An ``import`` line is written
  only for a type the schema uses.
* A struct is a nested message, an array a ``repeated`` field, a map a ``map<string, V>``.
  An array or map of arrays or maps has no Protobuf field and is refused.
* A nullable scalar or well-known-type field is ``optional``; a required one has no label.
* Field numbers are the author's: ``protobuf: {field: N}`` on every column, because a
  number names the field on the wire and must never change once data is written. They are
  unique within a message, 1 to 536870911, outside the 19000-19999 range Protobuf reserves.
  Fields are written in declared column order, nested messages first.

The language rules are protobuf.dev's (proto3 language guide, field numbers and field
labels).

Registry schema to columns (:func:`columns_from_registry`): the inverse, for the
plan-time comparison and for seeding ``stream.schema`` from a subject. A union other
than ``[null, T]`` and a JSON Schema ``$ref`` outside the document have no column type,
and raise :class:`UnsupportedSchema` instead of being guessed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.streaming.types import LogicalType, parse_fields

JSON_SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"
_AVRO_NAME = re.compile(r"[^A-Za-z0-9_]")


class UnsupportedSchema(ValueError):
    """A registry schema with a construct that has no FLUID column type."""


def avro_name(text: str) -> str:
    """``text`` as an Avro name: ``[A-Za-z_][A-Za-z0-9_]*``."""
    name = _AVRO_NAME.sub("_", text) or "_"
    return name if not name[0].isdigit() else f"_{name}"


def schema_text(schema: Any) -> str:
    """The canonical text of a schema document: sorted keys, no whitespace."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── columns → Avro ──────────────────────────────────────────────────────────────


def _avro_type(
    dtype: LogicalType, path: str, names: Mapping[str, str], used: Dict[str, str], ns: str
) -> Any:
    """``dtype`` as Avro. ``ns`` is the enclosing namespace a nested record inherits, so
    ``used`` holds every record's full name and none is defined twice (Avro refuses that)."""
    kind = dtype.kind
    if kind == "struct":
        writer = names.get(path)
        if writer and writer in used:
            # The producer reuses one record type here: refer to it by its name, as the
            # writer schema does, so Avro resolution matches the two records by name.
            return writer if dtype.required else ["null", writer]
        if writer:
            full = writer
        else:
            base = avro_name(path.replace(".", "_"))
            full = f"{ns}.{base}" if ns else base
            if full in used:
                base = f"{base}_{len(used)}"
                full = f"{ns}.{base}" if ns else base
        used[full] = path
        inner_ns = full.rpartition(".")[0]
        record: Dict[str, Any] = {
            "type": "record",
            "name": full.rpartition(".")[2],
            "fields": _avro_fields(dtype.fields, path, names, used, inner_ns),
        }
        if writer and inner_ns != ns:
            record["namespace"] = inner_ns
        result: Any = record
    elif kind == "array" and dtype.element is not None:
        result = {
            "type": "array",
            "items": _avro_type(dtype.element, f"{path}[]", names, used, ns),
        }
    elif kind == "map" and dtype.value is not None:
        result = {
            "type": "map",
            "values": _avro_type(dtype.value, f"{path}{{}}", names, used, ns),
        }
    elif kind == "decimal":
        result = {
            "type": "bytes",
            "logicalType": "decimal",
            "precision": dtype.precision,
            "scale": dtype.scale,
        }
    elif kind == "date":
        result = {"type": "int", "logicalType": "date"}
    elif kind in ("timestamp", "timestamp_ltz"):
        unit = "millis" if dtype.precision is not None and dtype.precision <= 3 else "micros"
        prefix = "local-timestamp" if kind == "timestamp" else "timestamp"
        result = {"type": "long", "logicalType": f"{prefix}-{unit}"}
    else:
        result = {"binary": "bytes"}.get(kind, kind)
    return result if dtype.required else ["null", result]


def _avro_fields(
    fields: Sequence[Tuple[str, LogicalType]],
    parent: str,
    names: Mapping[str, str],
    used: Dict[str, str],
    ns: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name, dtype in fields:
        path = f"{parent}.{name}" if parent else name
        entry: Dict[str, Any] = {"name": name, "type": _avro_type(dtype, path, names, used, ns)}
        if not dtype.required:
            entry["default"] = None
        out.append(entry)
    return out


def avro_schema(
    columns: Sequence[Mapping[str, Any]],
    *,
    name: str,
    namespace: Optional[str] = None,
    names: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """The Avro record schema of ``columns``.

    ``name`` and ``namespace`` name the top record. ``names`` maps a struct column's
    dotted path to the full name a nested record should take: the producer's, when
    checking against its subject, because Avro schema resolution matches records by
    name. A struct without an entry is named after its path.
    """
    fields = parse_fields(list(columns), path="schema")
    top = avro_name(name)
    # The top record's own name is taken too: a struct column named like it would
    # otherwise define a second record with the same full name.
    used: Dict[str, str] = {f"{namespace}.{top}" if namespace else top: ""}
    record: Dict[str, Any] = {
        "type": "record",
        "name": top,
        "fields": _avro_fields(fields, "", names or {}, used, namespace or ""),
    }
    if namespace:
        record["namespace"] = namespace
    return record


# ── columns → JSON Schema ───────────────────────────────────────────────────────


def _json_type(dtype: LogicalType) -> Dict[str, Any]:
    kind = dtype.kind
    if kind == "struct":
        result: Dict[str, Any] = {"type": "object", **_json_properties(dtype.fields)}
    elif kind == "array" and dtype.element is not None:
        result = {"type": "array", "items": _json_type(dtype.element)}
    elif kind == "map" and dtype.value is not None:
        result = {"type": "object", "additionalProperties": _json_type(dtype.value)}
    elif kind in ("int", "long"):
        result = {"type": "integer"}
    elif kind in ("float", "double", "decimal"):
        result = {"type": "number"}
    elif kind == "boolean":
        result = {"type": "boolean"}
    elif kind == "binary":
        result = {"type": "string", "contentEncoding": "base64"}
    elif kind == "date":
        result = {"type": "string", "format": "date"}
    elif kind in ("timestamp", "timestamp_ltz"):
        result = {"type": "string", "format": "date-time"}
    else:
        result = {"type": "string"}
    if not dtype.required:
        result["type"] = [result["type"], "null"]
    return result


def _json_properties(fields: Sequence[Tuple[str, LogicalType]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"properties": {name: _json_type(dtype) for name, dtype in fields}}
    required = [name for name, dtype in fields if dtype.required]
    if required:
        body["required"] = required
    return body


def json_schema(columns: Sequence[Mapping[str, Any]], *, title: str) -> Dict[str, Any]:
    """The draft-07 JSON Schema of ``columns``, an object titled ``title``."""
    fields = parse_fields(list(columns), path="schema")
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "title": title,
        "type": "object",
        **_json_properties(fields),
    }


# ── columns → Protobuf ──────────────────────────────────────────────────────────

_PROTO_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_PROTO_SCALAR = {
    "string": "string",
    "int": "int32",
    "long": "int64",
    "float": "float",
    "double": "double",
    "boolean": "bool",
    "binary": "bytes",
}
_PROTO_WELL_KNOWN = {
    "date": ("google.type.Date", "google/type/date.proto"),
    "timestamp": ("google.protobuf.Timestamp", "google/protobuf/timestamp.proto"),
    "timestamp_ltz": ("google.protobuf.Timestamp", "google/protobuf/timestamp.proto"),
}
_PROTO_MAX_FIELD = 536870911
_PROTO_RESERVED = range(19000, 20000)


def proto_identifier(text: str) -> str:
    """``text`` as a proto identifier: ``[A-Za-z][A-Za-z0-9_]*`` (Avro name, then an ``f`` prefix)."""
    name = avro_name(text)
    return name if name[0].isalpha() else f"f{name}"


def _upper_camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


class _ProtoWriter:
    def __init__(self, decimal: Optional[Tuple[str, str]], decimal_hint: str) -> None:
        self.decimal = decimal
        self.decimal_hint = decimal_hint
        self.imports: set = set()

    def leaf(self, dtype: LogicalType, path: str) -> Tuple[str, bool]:
        """``(type text, is scalar or well-known)`` of a non-struct, non-container type."""
        kind = dtype.kind
        if kind in _PROTO_SCALAR:
            return _PROTO_SCALAR[kind], True
        if kind in _PROTO_WELL_KNOWN:
            name, imp = _PROTO_WELL_KNOWN[kind]
            self.imports.add(imp)
            return name, True
        if kind == "decimal":
            if self.decimal is None:
                raise ValueError(
                    f"{path}: {self.decimal_hint or 'this registry has no Protobuf decimal type'}"
                )
            self.imports.add(self.decimal[1])
            return self.decimal[0], True
        raise ValueError(f"{path}: a {kind} column has no Protobuf type")

    def number(self, raw: Mapping[str, Any], path: str) -> int:
        block = raw.get("protobuf")
        number = block.get("field") if isinstance(block, Mapping) else None
        if number is None:
            raise ValueError(
                f"{path}: a Protobuf column needs protobuf.field, its field number "
                "(a number names the field on the wire and never changes once data is written)"
            )
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"{path}: protobuf.field must be an integer, not {number!r}")
        if not 1 <= number <= _PROTO_MAX_FIELD or number in _PROTO_RESERVED:
            raise ValueError(
                f"{path}: protobuf.field {number} is out of range: 1 to {_PROTO_MAX_FIELD}, "
                "except 19000 to 19999, which Protobuf reserves"
            )
        return number

    def message(
        self,
        name: str,
        fields: Sequence[Tuple[str, LogicalType]],
        raws: Sequence[Mapping[str, Any]],
        parent: str,
        depth: int,
    ) -> List[str]:
        """The lines of ``message name { ... }`` at ``depth`` levels of indentation."""
        pad = "  " * depth
        paths = [f"{parent}.{n}" if parent else n for n, _ in fields]
        numbers: List[int] = []
        owner: Dict[int, str] = {}
        for (fname, _), raw, path in zip(fields, raws, paths, strict=True):
            if not _PROTO_IDENT.fullmatch(fname):
                raise ValueError(f"{path}: {fname!r} is not a Protobuf identifier")
            number = self.number(raw, path)
            if number in owner:
                raise ValueError(
                    f"{path}: protobuf.field {number} is also {owner[number]}'s; "
                    "a field number is unique within its message"
                )
            owner[number] = path
            numbers.append(number)

        field_names = {n for n, _ in fields}
        taken: set = set()
        nested: List[str] = []
        body: List[str] = []
        for (fname, dtype), raw, path, number in zip(fields, raws, paths, numbers, strict=True):
            holder, holder_raw, holder_path = dtype, raw, path
            label = ""
            if dtype.kind == "array" and dtype.element is not None:
                holder, holder_raw, holder_path = dtype.element, raw["element"], f"{path}[]"
                if holder.kind in ("array", "map"):
                    raise ValueError(
                        f"{path}: an array of arrays/maps has no Protobuf field; "
                        "wrap it in a struct"
                    )
                label = "repeated "
            elif dtype.kind == "map" and dtype.value is not None:
                holder, holder_raw, holder_path = dtype.value, raw["value"], f"{path}{{}}"
                if holder.kind in ("array", "map"):
                    raise ValueError(
                        f"{path}: a map of arrays/maps has no Protobuf field; wrap it in a struct"
                    )
            if holder.kind == "struct":
                msg = _upper_camel(fname)
                if msg in taken or msg in field_names:
                    msg = f"{msg}_{number}"
                    if msg in taken or msg in field_names:
                        raise ValueError(f"{path}: the nested message name {msg!r} is taken")
                taken.add(msg)
                inner = self.message(
                    msg,
                    holder.fields,
                    holder_raw["fields"],
                    holder_path,
                    depth + 1,
                )
                nested.extend(inner)
                type_text, simple = msg, False
            else:
                type_text, simple = self.leaf(holder, holder_path)
            if dtype.kind == "map":
                body.append(f"{pad}  map<string, {type_text}> {fname} = {number};")
            elif label:
                body.append(f"{pad}  repeated {type_text} {fname} = {number};")
            else:
                optional = "optional " if simple and not dtype.required else ""
                body.append(f"{pad}  {optional}{type_text} {fname} = {number};")
        return [f"{pad}message {name} {{", *nested, *body, f"{pad}}}"]


def protobuf_schema(
    columns: Sequence[Mapping[str, Any]],
    *,
    name: str,
    package: str,
    decimal: Optional[Tuple[str, str]] = None,
    decimal_hint: str = "",
) -> str:
    """The proto3 text of ``columns``: one top message ``name`` in ``package``.

    ``decimal`` is ``(type name, import path)`` for a ``decimal`` column, or ``None`` for a
    registry that has none (the column then raises :class:`ValueError` with
    ``decimal_hint``). Every column, nested struct fields included, needs
    ``protobuf: {field: N}``. Deterministic: the same columns give the same bytes.
    """
    fields = parse_fields(list(columns), path="schema")
    top = proto_identifier(name)
    segments = [proto_identifier(part) for part in package.split(".") if part]
    if not segments:
        raise ValueError("a Protobuf schema needs a package")
    writer = _ProtoWriter(decimal, decimal_hint)
    message = writer.message(top, fields, list(columns), "", 0)
    lines = ['syntax = "proto3";', "", f"package {'.'.join(segments)};", ""]
    if writer.imports:
        lines += [f'import "{path}";' for path in sorted(writer.imports)]
        lines.append("")
    lines += message
    return "\n".join(lines) + "\n"


#: The value formats a consumed subject is checked in, as the registry names them.
_REGISTRY_FORMATS = {
    "json": "JSON",
    "json-confluent": "JSON",
    "avro": "AVRO",
    "avro-confluent": "AVRO",
}


def registry_format(stream_format: str) -> Optional[str]:
    """The registry's schema type for a stream's value format: ``AVRO`` or ``JSON``.

    ``None`` for any other format: the check refuses it, never reads it as Avro. The
    0.7.7 schema allows only ``json`` and ``avro-confluent`` on a stream, and ``fluid
    plan`` validates first, so only an unvalidated contract gets here with another.
    """
    return _REGISTRY_FORMATS.get(stream_format)


def derive(
    columns: Sequence[Mapping[str, Any]],
    data_format: str,
    *,
    name: str,
    namespace: Optional[str] = None,
    names: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """The ``AVRO`` or ``JSON`` schema document of ``columns``."""
    if data_format == "JSON":
        return json_schema(columns, title=name)
    return avro_schema(columns, name=name, namespace=namespace, names=names)


# ── Avro → columns ──────────────────────────────────────────────────────────────

_AVRO_PRIMITIVE = {
    "string": "string",
    "int": "int",
    "long": "long",
    "float": "float",
    "double": "double",
    "boolean": "boolean",
    "bytes": "binary",
}
_AVRO_LOGICAL = {
    "date": "date",
    "timestamp-millis": "timestamp_ltz",
    "timestamp-micros": "timestamp_ltz",
    "timestamp-nanos": "timestamp_ltz",
    "local-timestamp-millis": "timestamp",
    "local-timestamp-micros": "timestamp",
    "local-timestamp-nanos": "timestamp",
}


class _AvroReader:
    def __init__(self, lenient: bool = False) -> None:
        self.named: Dict[str, Any] = {}
        self.record_names: Dict[str, str] = {}
        #: ``lenient``: a field with no column type is left out and named here, by path.
        self.lenient = lenient
        self.skipped: Dict[str, str] = {}

    def _full(self, node: Mapping[str, Any], namespace: str) -> str:
        name = str(node.get("name") or "")
        if "." in name:
            return name
        ns = node.get("namespace", namespace) or ""
        return f"{ns}.{name}" if ns else name

    def type_spec(self, node: Any, path: str, namespace: str, depth: int = 0) -> Dict[str, Any]:
        if depth > 32:
            raise UnsupportedSchema(f"{path}: nested deeper than 32")
        required = True
        if isinstance(node, list):
            branches = [b for b in node if b != "null"]
            if len(branches) != 1 or len(node) - len(branches) > 1:
                raise UnsupportedSchema(f"{path}: a union other than [null, T] has no column type")
            required = len(branches) == len(node)
            node = branches[0]
        spec = self._non_union(node, path, namespace, depth)
        spec["required"] = required
        return spec

    def _non_union(self, node: Any, path: str, namespace: str, depth: int) -> Dict[str, Any]:
        if isinstance(node, str):
            if node in _AVRO_PRIMITIVE:
                return {"type": _AVRO_PRIMITIVE[node]}
            full = node if "." in node or not namespace else f"{namespace}.{node}"
            named = self.named.get(full) or self.named.get(node)
            if named is None:
                raise UnsupportedSchema(f"{path}: unknown Avro type {node!r}")
            return self._non_union(named, path, namespace, depth)
        if not isinstance(node, Mapping):
            raise UnsupportedSchema(f"{path}: not an Avro schema")
        logical = node.get("logicalType")
        kind = node.get("type")
        if logical == "decimal" and kind in ("bytes", "fixed"):
            if kind == "fixed":
                self.named[self._full(node, namespace)] = node
            precision, scale = node.get("precision"), node.get("scale", 0)
            if not isinstance(precision, int) or not isinstance(scale, int):
                raise UnsupportedSchema(f"{path}: a decimal needs integer precision and scale")
            return {"type": f"decimal({precision},{scale})"}
        if isinstance(logical, str) and logical in _AVRO_LOGICAL:
            return {"type": _AVRO_LOGICAL[logical]}
        if kind == "record":
            full = self._full(node, namespace)
            self.named[full] = node
            self.record_names[path] = full
            inner = full.rpartition(".")[0]
            return {"type": "struct", "fields": self.fields(node, path, inner, depth + 1)}
        if kind == "enum":
            self.named[self._full(node, namespace)] = node
            return {"type": "string"}
        if kind == "fixed":
            self.named[self._full(node, namespace)] = node
            return {"type": "binary"}
        if kind == "array":
            return {
                "type": "array",
                "element": self.type_spec(node.get("items"), f"{path}[]", namespace, depth + 1),
            }
        if kind == "map":
            return {
                "type": "map",
                "key": {"type": "string", "required": True},
                "value": self.type_spec(node.get("values"), f"{path}{{}}", namespace, depth + 1),
            }
        if isinstance(kind, (str, list, Mapping)):
            return self._non_union(kind, path, namespace, depth + 1)
        raise UnsupportedSchema(f"{path}: not an Avro schema")

    def fields(
        self, record: Mapping[str, Any], path: str, namespace: str, depth: int
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for entry in record.get("fields") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                raise UnsupportedSchema(f"{path or '$'}: a record field has no name")
            name = entry["name"]
            child = f"{path}.{name}" if path else name
            try:
                spec = self.type_spec(entry.get("type"), child, namespace, depth)
            except UnsupportedSchema as exc:
                if not self.lenient:
                    raise
                self.skipped[child] = str(exc)
                continue
            column: Dict[str, Any] = {"name": name, **spec}
            if isinstance(entry.get("doc"), str):
                column["description"] = entry["doc"]
            out.append(column)
        return out


def avro_columns(schema: Any) -> Tuple[List[Dict[str, Any]], str, Dict[str, str]]:
    """An Avro record schema's columns, its full name, and each nested record's name by path."""
    if isinstance(schema, str):
        schema = json.loads(schema)
    if not isinstance(schema, Mapping) or schema.get("type") != "record":
        raise UnsupportedSchema("$: the value schema is not an Avro record")
    reader = _AvroReader()
    full = reader._full(schema, "")
    reader.named[full] = schema
    columns = reader.fields(schema, "", full.rpartition(".")[0], 0)
    return columns, full, dict(reader.record_names)


# ── JSON Schema → columns ───────────────────────────────────────────────────────


def _json_ref(root: Mapping[str, Any], ref: str, path: str) -> Mapping[str, Any]:
    node: Any = root
    if not ref.startswith("#/"):
        raise UnsupportedSchema(f"{path}: a $ref outside the document ({ref!r})")
    for part in ref[2:].split("/"):
        node = (
            node.get(part.replace("~1", "/").replace("~0", "~"))
            if isinstance(node, Mapping)
            else None
        )
    if not isinstance(node, Mapping):
        raise UnsupportedSchema(f"{path}: $ref {ref!r} does not resolve")
    return node


def _json_spec(root: Mapping[str, Any], node: Any, path: str, depth: int = 0) -> Dict[str, Any]:
    if depth > 32:
        raise UnsupportedSchema(f"{path}: nested deeper than 32")
    if not isinstance(node, Mapping):
        raise UnsupportedSchema(f"{path}: not a JSON Schema object")
    if "$ref" in node:
        return _json_spec(root, _json_ref(root, str(node["$ref"]), path), path, depth + 1)
    nullable = False
    for combiner in ("oneOf", "anyOf"):
        if combiner in node:
            branches = [
                b
                for b in node[combiner]
                if not (isinstance(b, Mapping) and b.get("type") == "null")
            ]
            if len(branches) != 1:
                raise UnsupportedSchema(
                    f"{path}: {combiner} other than [null, T] has no column type"
                )
            spec = _json_spec(root, branches[0], path, depth + 1)
            spec["required"] = False
            return spec
    kind = node.get("type")
    if isinstance(kind, list):
        types = [t for t in kind if t != "null"]
        nullable = len(types) != len(kind)
        if len(types) != 1:
            raise UnsupportedSchema(f"{path}: a type list other than [T, null] has no column type")
        kind = types[0]
    spec: Dict[str, Any]
    if kind == "object":
        if "properties" in node or not isinstance(node.get("additionalProperties"), Mapping):
            spec = {"type": "struct", "fields": _json_fields(root, node, path, depth + 1)}
        else:
            spec = {
                "type": "map",
                "key": {"type": "string", "required": True},
                "value": _json_spec(root, node["additionalProperties"], f"{path}{{}}", depth + 1),
            }
    elif kind == "array":
        spec = {
            "type": "array",
            "element": _json_spec(root, node.get("items"), f"{path}[]", depth + 1),
        }
    elif kind == "integer":
        spec = {"type": "long"}
    elif kind == "number":
        spec = {"type": "double"}
    elif kind == "boolean":
        spec = {"type": "boolean"}
    elif kind == "string":
        fmt = node.get("format")
        if fmt == "date":
            spec = {"type": "date"}
        elif fmt == "date-time":
            spec = {"type": "timestamp_ltz"}
        elif node.get("contentEncoding") == "base64":
            spec = {"type": "binary"}
        else:
            spec = {"type": "string"}
    else:
        raise UnsupportedSchema(f"{path}: JSON Schema type {kind!r} has no column type")
    spec["required"] = not nullable
    return spec


def _json_fields(
    root: Mapping[str, Any],
    node: Mapping[str, Any],
    path: str,
    depth: int,
    skipped: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    properties = node.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise UnsupportedSchema(f"{path or '$'}: properties is not an object")
    required = set(node.get("required") or [])
    out: List[Dict[str, Any]] = []
    for name, child in properties.items():
        child_path = f"{path}.{name}" if path else str(name)
        try:
            spec = _json_spec(root, child, child_path, depth)
        except UnsupportedSchema as exc:
            if skipped is None:
                raise
            skipped[child_path] = str(exc)
            continue
        spec["required"] = spec["required"] and name in required
        column: Dict[str, Any] = {"name": str(name), **spec}
        if isinstance(child, Mapping) and isinstance(child.get("description"), str):
            column["description"] = child["description"]
        out.append(column)
    return out


def json_columns(schema: Any) -> List[Dict[str, Any]]:
    """A JSON Schema object's properties as columns."""
    if isinstance(schema, str):
        schema = json.loads(schema)
    if not isinstance(schema, Mapping) or schema.get("type") not in ("object", ["object"]):
        raise UnsupportedSchema("$: the value schema is not a JSON Schema object")
    return _json_fields(schema, schema, "", 0)


def columns_from_registry(schema: Any, data_format: str) -> List[Dict[str, Any]]:
    """The columns of a registry schema of type ``AVRO`` or ``JSON``."""
    fmt = data_format.upper()
    if fmt == "AVRO":
        return avro_columns(schema)[0]
    if fmt == "JSON":
        return json_columns(schema)
    raise UnsupportedSchema(f"$: forge reads AVRO and JSON subjects, not {data_format}")


def writer_columns(
    schema: Any, data_format: str
) -> Tuple[List[Dict[str, Any]], str, Dict[str, str], Dict[str, str]]:
    """A producer's schema for comparison: ``(columns, full name, record names, skipped)``.

    Lenient: a field with no column type (a union of two types, an untyped JSON
    property) is left out and listed in ``skipped`` by path with the reason, so it
    fails the comparison only if the declared schema reads it.
    """
    if isinstance(schema, str):
        schema = json.loads(schema)
    skipped: Dict[str, str] = {}
    if data_format.upper() == "JSON":
        if not isinstance(schema, Mapping) or schema.get("type") not in ("object", ["object"]):
            raise UnsupportedSchema("$: the value schema is not a JSON Schema object")
        return _json_fields(schema, schema, "", 0, skipped), "", {}, skipped
    if not isinstance(schema, Mapping) or schema.get("type") != "record":
        raise UnsupportedSchema("$: the value schema is not an Avro record")
    reader = _AvroReader(lenient=True)
    full = reader._full(schema, "")
    reader.named[full] = schema
    columns = reader.fields(schema, "", full.rpartition(".")[0], 0)
    return columns, full, dict(reader.record_names), reader.skipped


def column_paths(columns: Sequence[Mapping[str, Any]], prefix: str = "") -> List[str]:
    """Every field path ``columns`` declare, as :func:`writer_columns` names them."""
    paths: List[str] = []

    def walk(spec: Mapping[str, Any], path: str) -> None:
        paths.append(path)
        for field in spec.get("fields") or []:
            if isinstance(field, Mapping) and isinstance(field.get("name"), str):
                walk(field, f"{path}.{field['name']}")
        if isinstance(spec.get("element"), Mapping):
            walk(spec["element"], f"{path}[]")
        if isinstance(spec.get("value"), Mapping):
            walk(spec["value"], f"{path}{{}}")

    for column in columns:
        if isinstance(column, Mapping) and isinstance(column.get("name"), str):
            walk(column, f"{prefix}{column['name']}")
    return paths
