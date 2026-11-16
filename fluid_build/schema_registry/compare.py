# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Can the declared schema read what the producer's subject holds? ``fluid diff``'s rules.

The Glue Schema Registry has no compatibility dry-run (no API found; registering a
version is the only check it runs), and a Confluent subject in ``NONE`` mode accepts
anything. So forge also compares the two itself, through the rules ``fluid diff`` uses
for a stream (``api/changelog_stream_rules.py``): the subject's schema is the
baseline (what the producer writes) and the declared schema the new reader. A field
the job reads that the subject lacks, a nullable field the job requires, and a type
the reader cannot widen to are breaking; a field the job no longer reads is not.

Both sides pass through the registry format first (:func:`normalize`), so the
comparison sees what the format can carry: in JSON, ``int`` and ``long`` are both an
``integer``, and are not told apart.
"""

from __future__ import annotations

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
    return [c for c in changes if c.severity == "breaking"]


def describe(changes: Sequence[Any], limit: int = 8) -> str:
    """The changes as one line each: ``'orders.amount' type changed long -> int``."""
    lines = [str(c.description) for c in changes[:limit]]
    if len(changes) > limit:
        lines.append(f"... and {len(changes) - limit} more")
    return "; ".join(lines)
