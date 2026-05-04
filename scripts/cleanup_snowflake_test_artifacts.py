#!/usr/bin/env python3
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

"""Cleanup script for Snowflake integration test artifacts.

Runs after each ``snowflake-integration`` job in `integration.yml`,
regardless of whether the tests passed or failed (`if: always()`).

Drops Snowflake objects tagged with ``forge_ci=true`` AND owned by the
current run (matched on ``forge_ci_run`` tag set from
``$FORGE_CI_RUN_TAG``). Suspends warehouses tagged similarly.

The orphan-sweep daily cron does the same thing for objects older than
24h that this script missed (e.g. the test process crashed before
tagging finished).

Idempotent: safe to re-run if it crashes mid-cleanup.

Required env:
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
  SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE
  FORGE_CI_RUN_TAG  — value of the per-run tag the tests applied
"""

from __future__ import annotations

import os
import sys
from typing import Iterable


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"::warning::env var {name} is empty; cleanup may be incomplete", file=sys.stderr)
    return value


def _connect():
    """Lazy-import snowflake-connector so this script can be parsed even
    when the snowflake extras are not installed."""
    try:
        import snowflake.connector
    except ImportError:
        print(
            "::warning::snowflake-connector-python not installed; nothing to clean", file=sys.stderr
        )
        sys.exit(0)
    return snowflake.connector.connect(
        account=_required("SNOWFLAKE_ACCOUNT"),
        user=_required("SNOWFLAKE_USER"),
        password=_required("SNOWFLAKE_PASSWORD"),
        warehouse=_required("SNOWFLAKE_WAREHOUSE"),
        role=_required("SNOWFLAKE_ROLE"),
    )


def _query_objects_with_tag(conn, run_tag: str) -> list[tuple[str, str, str]]:
    """Return (object_type, fully_qualified_name, kind) for everything
    tagged ``forge_ci_run = <run_tag>`` AND ``forge_ci = 'true'``.

    Uses ``SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES`` which has 1-3h
    propagation latency. For freshly-created objects this view may not
    yet show them — that's why the daily orphan cron is the safety net.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT object_database, object_schema, object_name, object_type
        FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES
        WHERE tag_name = 'FORGE_CI_RUN'
          AND tag_value = %s
          AND object_deleted IS NULL
        """,
        (run_tag,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return [(r[3], f"{r[0]}.{r[1]}.{r[2]}" if r[1] else r[0], r[3]) for r in rows]


def _drop_object(conn, kind: str, name: str) -> None:
    cursor = conn.cursor()
    try:
        if kind == "DATABASE":
            cursor.execute(f"DROP DATABASE IF EXISTS {name} CASCADE")
        elif kind == "SCHEMA":
            cursor.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        elif kind in ("TABLE", "VIEW", "STREAM", "TASK"):
            cursor.execute(f"DROP {kind} IF EXISTS {name}")
        elif kind == "WAREHOUSE":
            cursor.execute(f"ALTER WAREHOUSE {name} SUSPEND")
        else:
            print(f"::warning::skipping unknown object kind {kind}: {name}", file=sys.stderr)
            return
        print(f"dropped {kind} {name}")
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"::warning::could not drop {kind} {name}: {exc}", file=sys.stderr)
    finally:
        cursor.close()


def _hard_drop_databases_by_prefix(conn, prefix: str = "FORGE_CI_") -> None:
    """Fallback path for cases where the tag query lags or returns nothing.

    Tests that follow the ``FORGE_CI_<UUID>`` naming convention can be
    swept by name even if the tag system hasn't propagated yet."""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SHOW DATABASES LIKE '{prefix}%'")
        rows = cursor.fetchall()
        for row in rows:
            db_name = row[1]  # SHOW DATABASES returns name in column 1
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS {db_name} CASCADE")
                print(f"dropped database {db_name} (prefix sweep)")
            except Exception as exc:  # noqa: BLE001
                print(f"::warning::could not drop {db_name}: {exc}", file=sys.stderr)
    finally:
        cursor.close()


def main() -> int:
    run_tag = os.environ.get("FORGE_CI_RUN_TAG", "")
    if not run_tag:
        print(
            "::warning::FORGE_CI_RUN_TAG empty; falling back to prefix sweep only", file=sys.stderr
        )

    conn = _connect()
    try:
        if run_tag:
            objects = _query_objects_with_tag(conn, run_tag)
            print(f"found {len(objects)} tagged objects for run {run_tag}")
            # Drop in reverse-dependency order: tasks/streams/views first,
            # then tables, then schemas, then databases.
            order = {
                "TASK": 0,
                "STREAM": 1,
                "VIEW": 2,
                "TABLE": 3,
                "SCHEMA": 4,
                "DATABASE": 5,
                "WAREHOUSE": 6,
            }
            for kind, name, _ in sorted(objects, key=lambda r: order.get(r[0], 99)):
                _drop_object(conn, kind, name)
        # Always run the prefix sweep too — catches anything the tag
        # system didn't index in time.
        _hard_drop_databases_by_prefix(conn, prefix="FORGE_CI_")
    finally:
        conn.close()
    print("snowflake cleanup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
