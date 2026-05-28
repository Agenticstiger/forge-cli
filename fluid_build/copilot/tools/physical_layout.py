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

"""Heuristic physical-layout suggester for warehouse tables.

Produces a per-provider clustering / partitioning hint that the
BuilderAgent stitches into the dbt ``{{ config(...) }}`` block.

Provider-specific syntax surveyed (Wave 2 borrow-before-build):

* Snowflake: ``CLUSTER BY (col1, col2)`` — dbt's ``cluster_by``
  config accepts a single column or a list
  (https://docs.getdbt.com/reference/resource-configs/snowflake-configs).
* BigQuery: ``PARTITION BY DATE(col) CLUSTER BY (col1, col2)`` —
  dbt's ``partition_by={"field": ..., "data_type": "date",
  "granularity": "day"}`` + ``cluster_by=[...]``
  (https://docs.getdbt.com/reference/resource-configs/bigquery-configs).
* Athena: ``bucketed_by`` + ``bucket_count`` for Hive tables;
  ``partitioned_by=['bucket(col, n)']`` for Iceberg
  (https://docs.getdbt.com/reference/resource-configs/athena-configs).
* Redshift: ``dist=<col>`` + ``sort=[...]`` + ``sort_type='compound'``
  (https://docs.getdbt.com/reference/resource-configs/redshift-configs).
"""

from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger(__name__)

# Time-like SQL-type prefixes (case-insensitive). Kept broad to catch
# every common warehouse spelling — TIMESTAMP_NTZ (Snowflake),
# DATETIME (BigQuery), TIMESTAMPTZ (Postgres/Redshift), etc.
_TIME_TYPE_PREFIXES: tuple[str, ...] = (
    "TIMESTAMP",
    "DATETIME",
    "DATE",
    "TIME",
)

# Names that strongly suggest a monotonically-increasing time column.
# Order matters — earlier entries win when multiple time columns
# are present. ``event_time`` and ``created_at`` are by far the most
# common in production warehouses.
_MONOTONIC_NAMES: tuple[str, ...] = (
    "event_time",
    "event_timestamp",
    "created_at",
    "ingested_at",
    "ingest_time",
    "load_timestamp",
    "loaded_at",
    "updated_at",
    "modified_at",
    "partition_date",
    "partition_dt",
    "dt",
    "date",
)


def _is_time_type(sql_type: str | None) -> bool:
    if not sql_type:
        return False
    head = str(sql_type).strip().upper()
    return any(head.startswith(prefix) for prefix in _TIME_TYPE_PREFIXES)


def _pick_partition_column(columns: list[dict[str, Any]]) -> str | None:
    """Pick the best time column for partitioning.

    Preference order:
    1. Time-typed columns whose name matches the monotonic-name list
       (ranked by that list's order).
    2. Any time-typed column (first wins).
    3. None.
    """
    time_cols = [c for c in columns if _is_time_type(c.get("type"))]
    if not time_cols:
        return None

    by_name: dict[str, dict[str, Any]] = {
        str(c.get("name") or "").lower(): c for c in time_cols if c.get("name")
    }
    for preferred in _MONOTONIC_NAMES:
        if preferred in by_name:
            return str(by_name[preferred]["name"])

    # Fall back to the first time column with a name.
    for c in time_cols:
        name = c.get("name")
        if name:
            return str(name)
    return None


def _pick_partition_grain(
    source_kind: str | None,
    columns: list[dict[str, Any]],
) -> str:
    """Pick a partition grain — hour | day | month | year.

    Heuristic:
    * Streaming sources → hour (volume per day too large for daily
      partitions on most warehouses).
    * Aggregates (model name suggests rollup) → month.
    * Otherwise default to day (the OLTP-style assumption).
    """
    sk = (source_kind or "").strip().lower()
    if sk in {"streaming", "realtime", "real-time", "real_time", "cdc"}:
        return "hour"
    if sk in {"aggregate", "rollup", "summary", "mart"}:
        return "month"
    # ``columns`` is reserved for future name-based inference (e.g. a
    # ``yyyymm`` column suggests month grain) but not used today.
    del columns
    return "day"


def _rank_clustering_keys(
    query_patterns: list[dict[str, Any]] | None,
    columns: list[dict[str, Any]],
    partition_col: str | None,
    *,
    max_keys: int = 4,
) -> list[str]:
    """Rank clustering keys by query-pattern frequency, or fall back.

    With ``query_patterns``: sum frequency per filter column, drop
    the partition column (already covered by the partition), and
    take the top ``max_keys``.

    Without ``query_patterns``: PKs + the highest-cardinality FK +
    the partition column (in that order). The partition column is
    still included as a fallback signal — some warehouses (e.g.
    Snowflake without explicit micro-partitions) benefit from it.
    """
    partition_lc = partition_col.lower() if partition_col else None

    if query_patterns:
        scores: dict[str, int] = {}
        for pat in query_patterns:
            if not isinstance(pat, dict):
                continue
            freq = int(pat.get("frequency") or 1)
            for col in pat.get("filter_columns") or []:
                if not col:
                    continue
                key = str(col)
                scores[key] = scores.get(key, 0) + freq
        # Stable sort: highest frequency first, ties broken by name
        # for determinism (matters for snapshot-style tests).
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out: list[str] = []
        for col, _ in ranked:
            if partition_lc and col.lower() == partition_lc:
                continue
            out.append(col)
            if len(out) >= max_keys:
                break
        return out

    # No query patterns — fall back to schema-derived signals.
    out_fallback: list[str] = []
    pk_names = [str(c["name"]) for c in columns if c.get("primary_key") and c.get("name")]
    for pk in pk_names:
        if partition_lc and pk.lower() == partition_lc:
            continue
        out_fallback.append(pk)

    fk_cols = [c for c in columns if isinstance(c.get("foreign_key"), dict) and c.get("name")]
    # No real cardinality data — prefer the first FK as the
    # "most-cardinal" stand-in. Schema authors typically list the
    # higher-cardinality FK first; this gives the BuilderAgent a
    # safe default until cardinality stats are wired in.
    if fk_cols:
        first_fk = str(fk_cols[0]["name"])
        if first_fk not in out_fallback and (not partition_lc or first_fk.lower() != partition_lc):
            out_fallback.append(first_fk)

    if partition_col and partition_col not in out_fallback:
        out_fallback.append(partition_col)

    return out_fallback[:max_keys]


def _materialization_hint(
    columns: list[dict[str, Any]],
    source_kind: str | None,
) -> str:
    """Pick a dbt materialization hint.

    * Streaming source → ``incremental`` (append-only ingest pattern).
    * PK-only schema → ``view`` (lookup table; thin and read-mostly).
    * Small table (< 10 columns) → ``table``.
    * Otherwise → ``table`` (safe default).
    """
    sk = (source_kind or "").strip().lower()
    if sk in {"streaming", "realtime", "real-time", "real_time", "cdc"}:
        return "incremental"

    if not columns:
        return "table"

    non_pk = [c for c in columns if not c.get("primary_key")]
    if not non_pk:
        return "view"

    if len(columns) < 10:
        return "table"

    return "table"


def _render_provider_specific(
    provider: str,
    *,
    partition_by: str | None,
    partition_grain: str | None,
    clustering_keys: list[str],
) -> dict[str, Any]:
    """Render provider-specific config text snippets.

    Returns a dict keyed by provider name with the DDL/config snippet
    a human or codegen layer can drop directly into a model file. Kept
    string-typed so we don't pretend to validate provider DDL here.
    """
    p = provider.strip().lower()

    if p == "snowflake":
        if clustering_keys:
            return {"snowflake": f"CLUSTER BY ({', '.join(clustering_keys)})"}
        return {"snowflake": ""}

    if p == "bigquery":
        parts: list[str] = []
        if partition_by:
            grain = (partition_grain or "day").upper()
            # BigQuery's DATE_TRUNC requires the timestamp-typed
            # column to be wrapped; DATE(col) is the canonical
            # day-grain form. For hour/month/year we use TIMESTAMP_TRUNC.
            if grain == "DAY":
                parts.append(f"PARTITION BY DATE({partition_by})")
            else:
                parts.append(f"PARTITION BY TIMESTAMP_TRUNC({partition_by}, {grain})")
        if clustering_keys:
            parts.append(f"CLUSTER BY ({', '.join(clustering_keys)})")
        return {"bigquery": " ".join(parts)}

    if p == "athena":
        # Athena recommendation prefers Iceberg bucket() transforms
        # when we have any clustering key — otherwise nothing.
        # Drop the partition column from the bucket candidate list:
        # bucketing the same column you already partition on is
        # redundant Iceberg-hidden-partition advice.
        bucket_candidates = [
            k for k in clustering_keys if not partition_by or k.lower() != partition_by.lower()
        ]
        if bucket_candidates:
            # Bucket count of 16 is a common conservative default in
            # AWS guidance for small-to-medium tables.
            transforms = [f"bucket({bucket_candidates[0]}, 16)"]
            if partition_by:
                transforms.insert(0, f"day({partition_by})")
            return {"athena": ("partitioned_by=[" + ", ".join(f"'{t}'" for t in transforms) + "]")}
        if partition_by:
            return {"athena": (f"partitioned_by=['day({partition_by})']")}
        return {"athena": ""}

    if p == "redshift":
        parts_rs: list[str] = []
        # First clustering key doubles as DISTKEY — single-column,
        # high-cardinality is the textbook DISTKEY rule.
        if clustering_keys:
            parts_rs.append(f"DISTKEY({clustering_keys[0]})")
        # The rest plus the partition column become the compound
        # SORTKEY. Partition column goes FIRST so time-range scans
        # benefit most.
        sort_cols: list[str] = []
        if partition_by:
            sort_cols.append(partition_by)
        sort_cols.extend(k for k in clustering_keys if k != (sort_cols[0] if sort_cols else None))
        if sort_cols:
            parts_rs.append(f"SORTKEY({', '.join(sort_cols)})")
        return {"redshift": " ".join(parts_rs)}

    # Unknown provider — return an empty stub but record the name so
    # the caller can detect the no-op case.
    return {provider: ""}


def suggest_physical_layout(
    schema: dict[str, Any],
    *,
    provider: str,
    query_patterns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Suggest a physical layout for a model.

    Parameters
    ----------
    schema
        Same shape as :func:`generate_dbt_tests` accepts. Extra
        ``source_kind`` key (one of ``streaming|cdc|oltp|aggregate``)
        steers partition grain + materialization hint.
    provider
        One of ``snowflake | bigquery | athena | redshift``. Anything
        else still returns a structured response but with an empty
        ``provider_specific`` snippet.
    query_patterns
        Optional list. Each element: ``{"filter_columns": [...],
        "join_columns": [...], "frequency": <int>}``. When provided,
        clustering keys are ranked by total frequency across all
        patterns.

    Returns
    -------
    dict
        Shape::

            {
              "clustering_keys": [str, ...],
              "partition_by": str | None,
              "partition_grain": "hour"|"day"|"month"|"year" | None,
              "materialization_hint": "table"|"view"|"incremental",
              "provider_specific": dict,
            }
    """
    columns_in = schema.get("columns") or []
    if not isinstance(columns_in, list):
        columns_in = []
    columns: list[dict[str, Any]] = [c for c in columns_in if isinstance(c, dict)]

    source_kind = schema.get("source_kind")

    partition_by = _pick_partition_column(columns)
    partition_grain = _pick_partition_grain(source_kind, columns) if partition_by else None

    clustering_keys = _rank_clustering_keys(query_patterns, columns, partition_by)

    materialization_hint = _materialization_hint(columns, source_kind)

    provider_specific = _render_provider_specific(
        provider,
        partition_by=partition_by,
        partition_grain=partition_grain,
        clustering_keys=clustering_keys,
    )

    return {
        "clustering_keys": clustering_keys,
        "partition_by": partition_by,
        "partition_grain": partition_grain,
        "materialization_hint": materialization_hint,
        "provider_specific": provider_specific,
    }


__all__ = ["suggest_physical_layout"]
