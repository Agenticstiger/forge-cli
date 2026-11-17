# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Can the declared schema read what the producer's subject holds? ``fluid diff``'s rules.

The Glue Schema Registry has no compatibility dry-run (no API found; registering a
version is the only check it runs), and a Confluent subject in ``NONE`` mode accepts
anything. So forge also compares the two itself, through the rules ``fluid diff`` uses
for a stream (``api/changelog_stream_rules.py``): the subject's schema is the
baseline (what the producer writes) and the declared schema the new reader. A field
the job reads that the subject lacks and a nullable field the job requires are
breaking; a field the job no longer reads is not. A changed type is judged by the
format's own reader rules (:func:`_readable`), not by the table widening ``fluid diff``
applies: Avro reads ``long`` as ``double`` but never a ``date`` as a timestamp, and a
decimal only at the same scale.

Both sides pass through the registry format first (:func:`normalize`), so the
comparison sees what the format can carry: in JSON, ``int`` and ``long`` are both an
``integer``, and are not told apart.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence

from .schemas import avro_columns, avro_schema, json_columns, json_schema


def normalize(columns: Sequence[Mapping[str, Any]], data_format: str) -> List[Dict[str, Any]]:
    """``columns`` as the registry format reads them back."""
    if data_format == "JSON":
        return json_columns(json_schema(columns, title="value"))
    return avro_columns(avro_schema(columns, name="value"))[0]


def _contract(topic: str, fmt: str, columns: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    stream = {"kind": "kafka", "topic": topic, "format": fmt, "schema": list(columns)}
    return {"builds": [{"id": "registry", "properties": {"stream": stream}}]}


def reader_changes(
    declared: Sequence[Mapping[str, Any]],
    writer: Sequence[Mapping[str, Any]],
    *,
    data_format: str,
    topic: str,
) -> List[Any]:
    """The breaking changes between the subject's columns and the declared reader.

    Returns ``api.changelog_types.Change`` objects with severity ``breaking``; the
    path names the declared stream field (``builds[0].properties.stream.schema...``).
    """
    from fluid_build.api.changelog_stream_rules import diff_streams

    reader = normalize(declared, data_format)
    fmt = "json" if data_format == "JSON" else "avro-confluent"
    changes = diff_streams(_contract(topic, fmt, writer), _contract(topic, fmt, reader))
    out = []
    for change in changes:
        if change.kind in _TYPE_KINDS:
            readable = _readable(str(change.before), str(change.after), data_format)
            change.severity = "non_breaking" if readable else "breaking"
            if not readable:
                change.description = f"{change.description}: {_READER[data_format]} reader cannot read it as declared"
        if change.severity == "breaking":
            out.append(change)
    return out


#: The diff's type-change kinds; their severity is ``fluid diff``'s widening table,
#: which is about table columns, so the registry format's own reader rules replace it.
_TYPE_KINDS = frozenset({"stream_column_type_changed", "stream_column_type_widened"})
_READER = {"AVRO": "an Avro", "JSON": "a JSON Schema"}
#: Avro 1.12 schema resolution, "promotions": writer type -> reader types it reads.
_AVRO_PROMOTIONS = {
    "INT": {"LONG", "FLOAT", "DOUBLE"},
    "LONG": {"FLOAT", "DOUBLE"},
    "FLOAT": {"DOUBLE"},
    "STRING": {"BINARY"},
    "BINARY": {"STRING"},
}
_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")


def _json_family(kind: str) -> str:
    if kind in ("INT", "LONG"):
        return "LONG"
    if kind in ("FLOAT", "DOUBLE") or kind.startswith("DECIMAL"):
        return "DOUBLE"
    return kind


def _readable(writer: str, reader: str, data_format: str) -> bool:
    """Whether a reader of type ``reader`` reads values written as ``writer``.

    Avro: the specification's promotions; a decimal is unscaled bytes, so the scale
    must match and the precision may only grow; a date, timestamp or local timestamp
    reads only as itself. JSON Schema: an integer is a number, nothing else converts.
    """
    writer, reader = writer.upper().replace(" ", ""), reader.upper().replace(" ", "")
    if writer == reader:
        return True
    if data_format == "JSON":
        # JSON carries integer and number only: every int is an integer, every float
        # or decimal a number, whichever side was declared more precisely.
        writer, reader = _json_family(writer), _json_family(reader)
        return writer == reader or (writer == "LONG" and reader == "DOUBLE")
    w, r = _DECIMAL.fullmatch(writer), _DECIMAL.fullmatch(reader)
    if w and r:
        return w.group(2) == r.group(2) and int(r.group(1)) >= int(w.group(1))
    return reader in _AVRO_PROMOTIONS.get(writer, set())


def describe(changes: Sequence[Any], limit: int = 8) -> str:
    """The changes as one line each: ``'orders.amount' type changed long -> int``."""
    lines = [str(c.description) for c in changes[:limit]]
    if len(changes) > limit:
        lines.append(f"... and {len(changes) - limit} more")
    return "; ".join(lines)
