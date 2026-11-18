# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark stream's dead letters: the records it cannot land, routed instead of failing it.

``properties.stream.deadLetter`` (0.7.7 preview) compiles into the job config's
``deadLetter`` block and its ``sql.check`` query; the runner holds no contract logic:

- **What a dead letter is.** A value or key that is not the declared JSON (Spark's
  corrupt-record column, :data:`CORRUPT_COLUMN`) or not Avro the registry's schema
  decodes (ABRiS's permissive handler turns it into a row of nulls, told apart from a real
  record by :data:`DECODED_MARK`), and a required field
  that is null. :func:`check_select` compiles one ``fluid_error`` reason per record from
  the stream's declared fields, with the same reads as the source query
  (``streaming.mapping.spark_source_select``), so a record the check passes can never
  fail that query.
- **Where it goes.** ``sink: table``: an Iceberg table beside the output,
  ``<output>__dead_letter`` by default, as late events go to ``<output>__late_events``
  (``build_runners/_late_arrival.py``). ``sink: topic``: a Kafka topic on the cluster the
  stream reads, each record with Kafka Connect's error-context headers
  (``__connect.errors.*``, KIP-298).
- **How it is landed.** The output keeps its own query and Iceberg's streaming sink, so
  it stays exactly-once; the dead letters are a second query over the same decoded
  stream, with its own checkpoint inside the stream's. Each of its micro-batches is
  committed to the table at most once (the batch id goes into the snapshot's summary), and
  sent to a topic at least once.
- **What it keeps.** The raw value and key bytes, the reason, the record's topic, partition,
  offset and timestamp. A build that masks fields at ingest (``maskAtIngest``) keeps no raw
  bytes, which would hold those fields in clear; its topic, partition and offset still find
  the record in Kafka while the topic retains it.
- **When the stream stops.** Past ``maxRecordsBeforeAbort`` dead letters since the job
  started, the job fails, after the batch that crossed it has landed, as the acquisition
  lane's ``DLQWriter`` aborts past the same key.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fluid_build.providers._sql_safety import validate_ident

#: The JSON decoder's corrupt-record field (Spark's ``columnNameOfCorruptRecord``).
CORRUPT_COLUMN = "fluid_corrupt_record"
#: The field the runner adds to an Avro reader schema, with ``default: true``: Avro's
#: schema resolution fills it in for every record it decodes, and ABRiS's permissive
#: handler, which replaces a record it cannot read with a row of nulls (ABRiS 6.4.0
#: ``PermissiveRecordExceptionHandler``), leaves it null.
DECODED_MARK = "fluid_decoded"
#: The undecoded bytes the runner keeps beside the decoded value and key.
RAW_VALUE, RAW_KEY = "fluid_raw_value", "fluid_raw_key"
#: The reason a record is a dead letter, or null.
ERROR_COLUMN = "fluid_error"
#: The view the runner registers the decoded records under, for :func:`check_select`.
DECODED_TABLE = "kafka_records_decoded"
#: A dead letter as the table holds it: ``(name, Spark type, required)``.
COLUMNS = (
    ("error", "STRING", True),
    ("raw_value", "BINARY", False),
    ("raw_key", "BINARY", False),
    ("kafka_topic", "STRING", True),
    ("kafka_partition", "INT", True),
    ("kafka_offset", "BIGINT", True),
    ("kafka_timestamp", "TIMESTAMP", False),
    ("dead_lettered_at", "TIMESTAMP", True),
)
#: The same columns as contract schema fields, for the Glue table OpenTofu emits on AWS.
SCHEMA = (
    {
        "name": "error",
        "type": "string",
        "required": True,
        "description": "Why the record is a dead letter: the first check it failed",
    },
    {
        "name": "raw_value",
        "type": "binary",
        "description": "The record's value as it was read; null for a build that masks fields",
    },
    {
        "name": "raw_key",
        "type": "binary",
        "description": "The record's key as it was read; null for a build that masks fields",
    },
    {"name": "kafka_topic", "type": "string", "required": True},
    {"name": "kafka_partition", "type": "integer", "required": True},
    {"name": "kafka_offset", "type": "long", "required": True},
    {"name": "kafka_timestamp", "type": "timestamp"},
    {"name": "dead_lettered_at", "type": "timestamp", "required": True},
)
SINKS = ("table", "topic")
ALERTS = ("schema_violation",)
DEFAULT_MAX_RECORDS = 10_000
_KEYS = {"enabled", "sink", "table", "topic", "maxRecordsBeforeAbort", "alertOn"}


def table_name(target_table: str) -> str:
    """The default dead-letter table: ``<target>__dead_letter``, beside ``__late_events``."""
    return f"{target_table}__dead_letter"


def parse(spec: Any, where: str) -> Optional[dict[str, Any]]:
    """``properties.stream.deadLetter``, checked; ``None`` when absent or ``enabled: false``.

    Raises:
        ValueError: a key or value the schema refuses (for a caller that skips it).
    """
    if spec is None:
        return None
    where = f"{where}.deadLetter"
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(set(spec) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unsupported field(s) {unknown}")
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled: expected true or false")
    if not enabled:
        return None
    sink = spec.get("sink", "table")
    if sink not in SINKS:
        raise ValueError(f"{where}.sink: expected one of {list(SINKS)}")
    table, topic = spec.get("table"), spec.get("topic")
    if sink == "topic":
        if not isinstance(topic, str) or not topic or table is not None:
            raise ValueError(f"{where}: a topic sink names its topic, and no table")
        if not _topic_name(topic):
            raise ValueError(f"{where}.topic: expected a Kafka topic name")
    elif topic is not None:
        raise ValueError(f"{where}.topic: only a topic sink names a topic")
    if table is not None:
        try:
            validate_ident(table)
        except ValueError:
            raise ValueError(f"{where}.table: expected a table name") from None
    cap = spec.get("maxRecordsBeforeAbort", DEFAULT_MAX_RECORDS)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError(f"{where}.maxRecordsBeforeAbort: expected a whole number from 0")
    alerts = spec.get("alertOn", [])
    if not isinstance(alerts, list) or any(a not in ALERTS for a in alerts):
        raise ValueError(f"{where}.alertOn: expected a list of {list(ALERTS)}")
    return {
        "sink": sink,
        "table": table,
        "topic": topic,
        "maxRecordsBeforeAbort": cap,
        "alertOn": sorted(set(alerts)),
    }


def _topic_name(name: str) -> bool:
    import re

    return re.fullmatch(r"[A-Za-z0-9._-]{1,249}", name) is not None and name not in (".", "..")


def _quote(text: str) -> str:
    # The labels are field names (validated identifiers) and fixed words.
    return "'" + text.replace("'", "") + "'"


def _violations(sql: str, dtype: Any, label: str, depth: int = 0) -> list[tuple[str, str]]:
    """``(condition, reason)`` for each required value under ``sql`` that can be null, in
    the order ``streaming.mapping._spark_read`` reads them: a required value, then the
    fields of a struct that is there, then each array element and map value."""
    found: list[tuple[str, str]] = []
    if dtype.required:
        found.append((f"{sql} IS NULL", f"required field {label} is null"))
    if dtype.kind == "struct":
        for name, child in dtype.fields:
            for cond, reason in _violations(f"{sql}.`{name}`", child, f"{label}.{name}", depth):
                found.append((f"({sql} IS NOT NULL AND {cond})", reason))
    elif dtype.kind == "array" and dtype.element is not None:
        var = f"`fluid_e{depth}`"
        for cond, reason in _violations(var, dtype.element, f"{label}[]", depth + 1):
            found.append((f"coalesce(exists({sql}, {var} -> {cond}), false)", reason))
    elif dtype.kind == "map" and dtype.value is not None:
        var = f"`fluid_v{depth}`"
        for cond, reason in _violations(var, dtype.value, f"{label}[]", depth + 1):
            found.append((f"coalesce(exists(map_values({sql}), {var} -> {cond}), false)", reason))
    return found


def _decode_error(
    column: str, raw: str, spec: Optional[Mapping[str, Any]]
) -> list[tuple[str, str]]:
    if spec is None or spec.get("format") == "raw":
        return []
    if spec["format"] == "json":
        return [
            (
                f"`{column}`.`{CORRUPT_COLUMN}` IS NOT NULL",
                f"the {column} is not JSON of the declared fields and types",
            )
        ]
    return [
        (
            f"`{raw}` IS NOT NULL AND `{column}`.`{DECODED_MARK}` IS NULL",
            f"the {column} is not Avro that the registry schema decodes",
        )
    ]


def check_select(stream: Any, decoders: Mapping[str, Any]) -> str:
    """The query that gives each decoded record its dead-letter reason, or null.

    It reads :data:`DECODED_TABLE` (the Kafka records, ``value`` and ``key`` decoded, the
    raw bytes beside them) and adds :data:`ERROR_COLUMN`: the first reason that applies,
    decoding first, then the required fields in the source query's order."""
    checks = _decode_error("value", RAW_VALUE, decoders.get("value"))
    checks += _decode_error("key", RAW_KEY, decoders.get("key"))
    for name, dtype in stream.value:
        checks += _violations(f"`value`.`{name}`", dtype, name)
    if stream.key_format == "raw":
        ((name, dtype),) = stream.key
        checks += _violations("`key`", dtype, f"key {name}")
    else:
        prefix = len(stream.key_prefix)
        for name, dtype in stream.key:
            checks += _violations(f"`key`.`{name[prefix:]}`", dtype, f"key {name}")
    if checks:
        cases = "\n    ".join(f"WHEN {c} THEN {_quote(r)}" for c, r in checks)
        error = f"CASE\n    {cases}\n  END"
    else:
        error = "CAST(NULL AS STRING)"
    return f"SELECT *,\n  {error} AS `{ERROR_COLUMN}`\nFROM `{DECODED_TABLE}`"


def create_statements(catalog: str, database: str, table: str) -> list[str]:
    """The statements a local run creates its dead-letter table with, when nothing has
    (on AWS, OpenTofu creates it): Iceberg v2, :data:`COLUMNS`."""
    name = f"`{catalog}`.`{validate_ident(database)}`"
    columns = ",\n  ".join(
        f"`{column}` {kind}{' NOT NULL' if required else ''}" for column, kind, required in COLUMNS
    )
    return [
        f"CREATE NAMESPACE IF NOT EXISTS {name}",
        f"CREATE TABLE IF NOT EXISTS {name}.`{validate_ident(table)}` (\n  {columns}\n) "
        "USING iceberg TBLPROPERTIES ('format-version' = '2')",
    ]
