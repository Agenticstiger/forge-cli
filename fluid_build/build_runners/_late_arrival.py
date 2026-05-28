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

"""Late-arrival semantics for streaming acquisition runners (Phase-3 #15).

Streaming sources (Kafka Connect / Debezium) commit messages with an
event-time timestamp that's often older than wall-clock by a small
window — late arrivals. The contract's
``WatermarkSpec.allowed_lateness`` (ISO-8601 duration) declares how
much lateness is acceptable. Messages older than
``current_watermark - allowed_lateness`` are routed to a side-output
table (``<target>__late_events``) instead of dropped silently or
silently included in the on-time slice.

This module provides the shared classifier + side-output writer used
by both runners. Each runner calls
:func:`classify_arrival(event_time, current_watermark, allowed_lateness)`
on every message and routes by result:

* ``"on_time"`` — stamped with ``event_time`` into the target table.
* ``"late"`` — appended to the ``__late_events`` side-output with the
  full event payload PLUS a ``_late_arrival_metadata`` block carrying
  the lateness in seconds.
* ``"future"`` — clock skew or bug in the upstream; logged + treated
  as on-time so the runner doesn't lose data on a mis-set clock.

The side-output table follows a stable contract so downstream consumers
can branch on it::

    CREATE TABLE <target>__late_events (
      _late_arrival_metadata STRUCT<
        event_time     TIMESTAMP,
        watermark_at_arrival TIMESTAMP,
        lateness_seconds DOUBLE,
        allowed_lateness_seconds DOUBLE,
        reason         STRING
      >,
      payload                 VARIANT
    );

Operators decide downstream whether to merge late events back into
the main slice (when business logic permits) or quarantine them for
manual review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("fluid.build_runners.late_arrival")


# ISO-8601 duration regex (subset). Supports the common shapes
# operators write: ``PT5M`` (5 minutes), ``PT2H`` (2 hours), ``P1D``
# (1 day), ``PT30S`` (30 seconds), ``PT1H30M`` (1 hour 30 minutes).
_ISO_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_iso_duration(text: Optional[str]) -> Optional[timedelta]:
    """Parse an ISO-8601 duration string into a :class:`timedelta`.

    Returns ``None`` for empty / unparseable input — callers treat
    that as "no late-arrival budget configured" (every message is
    on-time as long as it's <= watermark).

    Supported shapes::

        PT5M        → timedelta(minutes=5)
        PT1H30M     → timedelta(hours=1, minutes=30)
        P1D         → timedelta(days=1)
        PT30S       → timedelta(seconds=30)
        PT1.5S      → timedelta(seconds=1.5)

    More exotic shapes (years, months, weeks, fractional days) are
    rejected because their semantics depend on a calendar reference
    point that streaming runners don't carry.
    """
    if not text:
        return None
    match = _ISO_DURATION_RE.match(text.strip())
    if not match:
        LOG.debug("invalid_iso_duration: text=%r", text)
        return None
    parts = match.groupdict(default="0")
    try:
        td = timedelta(
            days=int(parts["days"] or 0),
            hours=int(parts["hours"] or 0),
            minutes=int(parts["minutes"] or 0),
            seconds=float(parts["seconds"] or 0),
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover — defensive
        LOG.debug("iso_duration_parse_failed: text=%r error=%s", text, exc)
        return None
    # Reject all-zero durations like ``PT`` or ``P`` — the regex accepts
    # them syntactically but the ISO spec requires at least one component.
    # An ``allowed_lateness`` of 0 is meaningless (every late event would
    # match the budget exactly), so callers expect None for "no budget".
    if td.total_seconds() == 0:
        LOG.debug("iso_duration_zero_rejected: text=%r", text)
        return None
    return td


@dataclass(frozen=True)
class ArrivalClassification:
    """Result of classifying a single streaming event.

    Attributes:
        category: ``"on_time"`` / ``"late"`` / ``"future"``.
        lateness_seconds: How many seconds behind the watermark this
            event is. Negative values mean "ahead of watermark"
            (future). Always 0 for events within the on-time window.
        reason: Human-readable explanation for log lines + the
            side-output's ``_late_arrival_metadata.reason`` field.
    """

    category: str
    lateness_seconds: float
    reason: str


def classify_arrival(
    *,
    event_time: datetime,
    current_watermark: datetime,
    allowed_lateness: Optional[timedelta],
) -> ArrivalClassification:
    """Classify one event against the current watermark + lateness budget.

    Returns an :class:`ArrivalClassification`. Callers branch on
    ``.category`` to route the event.

    The watermark is the runner's running maximum event-time minus
    a small jitter buffer (each runner sets its own buffer; typical
    is 0). When ``allowed_lateness`` is ``None``, every event with
    ``event_time <= current_watermark`` is on-time and every event
    with ``event_time > current_watermark`` is "future" (clock skew).
    With a budget set, events within
    ``[current_watermark - allowed_lateness, current_watermark]``
    are still on-time; older events go to the side-output.

    Both timestamps must be timezone-aware. ``datetime.utcnow()`` is
    timezone-naive; callers should use ``datetime.now(timezone.utc)``.
    """
    if event_time.tzinfo is None or current_watermark.tzinfo is None:
        # Defensive: naive timestamps would compare wrong across DST.
        # Promote both to UTC by assuming naive = UTC.
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        if current_watermark.tzinfo is None:
            current_watermark = current_watermark.replace(tzinfo=timezone.utc)

    delta = (current_watermark - event_time).total_seconds()
    if delta < 0:
        # Event is ahead of watermark (clock skew or bug upstream).
        # Treat as on-time so the runner doesn't lose data; log so
        # operators can investigate.
        return ArrivalClassification(
            category="future",
            lateness_seconds=delta,  # negative
            reason=(
                f"event_time {event_time.isoformat()} is "
                f"{abs(delta):.3f}s ahead of watermark "
                f"{current_watermark.isoformat()}"
            ),
        )

    if allowed_lateness is None:
        # No budget: anything with event_time <= watermark is on-time.
        return ArrivalClassification(
            category="on_time",
            lateness_seconds=0.0,
            reason="no allowed_lateness configured; all in-order events accepted",
        )

    budget_seconds = allowed_lateness.total_seconds()
    if delta <= budget_seconds:
        return ArrivalClassification(
            category="on_time",
            lateness_seconds=0.0,
            reason=f"within {budget_seconds:.0f}s lateness budget",
        )

    return ArrivalClassification(
        category="late",
        lateness_seconds=delta,
        reason=(f"{delta:.3f}s late > {budget_seconds:.0f}s budget; routed to side-output"),
    )


def side_output_table_name(target_table: str) -> str:
    """Canonical side-output table name for ``target_table``.

    Pattern: ``<target>__late_events``. Operators querying for late
    events can ``SELECT * FROM <target>__late_events`` without
    hunting through provider-specific naming.
    """
    return f"{target_table}__late_events"


def extract_late_arrival_policy(
    *,
    contract_or_source: Any,
    target_table: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a connector-config-friendly dict from the contract's
    watermark spec.

    Used by streaming runners (Kafka Connect / Debezium / Airbyte) to
    surface the late-arrival policy as connector config so a
    downstream Single-Message-Transform (SMT) or sink-side enforcer
    can read it and route events to the side-output table.

    Returns a dict with stable keys:

    * ``enabled`` (bool) — True when ``allowed_lateness`` is configured.
    * ``allowed_lateness_seconds`` (float) — budget in seconds.
    * ``allowed_lateness_iso`` (str) — original ISO-8601 string.
    * ``side_output_table`` (str) — canonical late-events table name.
    * ``connector_config`` (dict) — keys to merge into a Kafka-Connect
      / Debezium connector config under ``fluid.late_arrival.*``.

    When no policy is configured, returns ``{"enabled": False}`` so
    callers can branch without unpacking.

    Accepts either a :class:`SourceSpec` (via ``ctx.source``) or a raw
    contract dict — for resilience against the multiple shapes the
    runners pass around.
    """
    iso: Optional[str] = None
    # Try SourceSpec.watermark.allowed_lateness path first.
    watermark = getattr(contract_or_source, "watermark", None)
    if watermark is not None and getattr(watermark, "allowed_lateness", None):
        iso = watermark.allowed_lateness  # type: ignore[union-attr]
    elif isinstance(contract_or_source, dict):
        # Walk the contract: builds[].properties.source.watermark.allowedLateness
        builds = contract_or_source.get("builds") or []
        for b in builds:
            wm = (b.get("properties") or {}).get("source", {}).get("watermark") or {}
            if wm.get("allowedLateness"):
                iso = wm["allowedLateness"]
                break

    if not iso:
        return {"enabled": False}

    td = parse_iso_duration(iso)
    if td is None or td.total_seconds() <= 0:
        return {"enabled": False, "allowed_lateness_iso": iso}

    side_table = side_output_table_name(target_table or "events")
    return {
        "enabled": True,
        "allowed_lateness_iso": iso,
        "allowed_lateness_seconds": td.total_seconds(),
        "side_output_table": side_table,
        "connector_config": {
            "fluid.late_arrival.enabled": "true",
            "fluid.late_arrival.allowed_lateness_seconds": str(td.total_seconds()),
            "fluid.late_arrival.side_output_table": side_table,
        },
    }


def _detect_event_time_column(
    schema: List[Dict[str, Any]],
    *,
    explicit: Optional[str] = None,
) -> Optional[str]:
    """Pick the event-time column from a contract schema.

    Resolution order:

    1. ``explicit`` — caller supplied the column name (highest trust).
    2. A column with logical/physical type ``timestamp`` and a name
       matching the canonical event-time vocabulary
       (``event_time``, ``event_at``, ``occurred_at``, ``ts``,
       ``timestamp``, ``created_at``, ``recorded_at``).
    3. The first timestamp-typed column.
    4. ``None`` — caller falls back to "no late-arrival enforcement".

    The schema list shape mirrors the contract's
    ``exposes[].contract.schema``: each entry is a dict with at
    minimum ``name`` and a type hint under ``logicalType`` /
    ``physicalType`` / ``type``.
    """
    if not isinstance(schema, list):
        return None
    if explicit:
        for col in schema:
            if isinstance(col, dict) and (col.get("name") == explicit):
                return explicit
        # Caller named a column not in the schema — trust it anyway,
        # the runner may know better than us.
        return explicit

    canonical_names = {
        "event_time",
        "event_at",
        "occurred_at",
        "ts",
        "timestamp",
        "created_at",
        "recorded_at",
    }

    timestamp_cols: List[str] = []
    for col in schema:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if not isinstance(name, str):
            continue
        col_type = (
            col.get("logicalType") or col.get("physicalType") or col.get("type") or ""
        ).lower()
        if "timestamp" in col_type or col_type in {"datetime", "date"}:
            if name.lower() in canonical_names:
                return name  # short-circuit on canonical match
            timestamp_cols.append(name)

    return timestamp_cols[0] if timestamp_cols else None


def split_late_events_in_duckdb(
    *,
    con: Any,
    source_relation: str,
    side_output_relation: str,
    event_time_column: str,
    allowed_lateness_seconds: float,
) -> Dict[str, int]:
    """Split a duckdb table/view into on-time vs late rows.

    Computes ``watermark = max(event_time)`` over the source. Rows
    where ``event_time < watermark - allowed_lateness`` are appended
    to ``side_output_relation`` (created if missing) and deleted from
    the source. Returns ``{"on_time": int, "late": int}``.

    Idempotent: second call after no new data finds nothing to move.
    Both ``source_relation`` and ``side_output_relation`` must be
    valid duckdb identifiers (caller validates).

    Used by the duckdb runner's post-land hook + DLT post-pipeline
    step. Other Python-side runners (Meltano, Airbyte non-streaming)
    can call this too.
    """
    # ── input validation ────────────────────────────────────────────
    from fluid_build.providers._sql_safety import validate_ident

    validate_ident(source_relation)
    validate_ident(side_output_relation)
    validate_ident(event_time_column)

    if allowed_lateness_seconds <= 0:
        return {"on_time": 0, "late": 0}

    # ── compute watermark ────────────────────────────────────────────
    watermark_row = con.execute(
        f"SELECT max({event_time_column}) FROM {source_relation}"
    ).fetchone()
    watermark = watermark_row[0] if watermark_row else None
    if watermark is None:
        # Empty source — no enforcement possible.
        return {"on_time": 0, "late": 0}

    threshold_expr = (
        f"({event_time_column} < "
        f"(SELECT max({event_time_column}) FROM {source_relation}) "
        f"- INTERVAL '{int(allowed_lateness_seconds)}' SECOND)"
    )

    # ── ensure side-output table exists with same schema ─────────────
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {side_output_relation} AS "
        f"SELECT * FROM {source_relation} WHERE 1=0"
    )

    # ── append late rows to side-output ──────────────────────────────
    late_count = con.execute(
        f"SELECT count(*) FROM {source_relation} WHERE {threshold_expr}"
    ).fetchone()[0]
    if late_count > 0:
        con.execute(
            f"INSERT INTO {side_output_relation} "
            f"SELECT * FROM {source_relation} WHERE {threshold_expr}"
        )
        con.execute(f"DELETE FROM {source_relation} WHERE {threshold_expr}")

    on_time_count = con.execute(f"SELECT count(*) FROM {source_relation}").fetchone()[0]

    return {"on_time": int(on_time_count), "late": int(late_count)}


__all__ = [
    "ArrivalClassification",
    "_detect_event_time_column",
    "classify_arrival",
    "extract_late_arrival_policy",
    "parse_iso_duration",
    "side_output_table_name",
    "split_late_events_in_duckdb",
]
