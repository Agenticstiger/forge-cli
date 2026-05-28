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

"""Optional Postgres-backed staged store."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..base import Store, StoreRecord, utc_now
from ..namespaces import normalize_namespace

_log = logging.getLogger(__name__)


def _redact_dsn(dsn: str) -> str:
    """Strip the password (and any URL-encoded secret) from a libpq URL.

    Accepts both ``postgres://user:secret@host:5432/db`` and the
    keyword form ``host=... user=... password=secret dbname=...``.
    Returns a representation safe to log or stash on the instance —
    the original string is only ever passed to :func:`psycopg.connect`.
    """

    if not dsn:
        return dsn
    redacted = re.sub(
        r"(?P<scheme>[a-zA-Z+]+://[^:/@]+:)[^@/]+(?P<host>@)",
        r"\g<scheme>***\g<host>",
        dsn,
    )
    redacted = re.sub(
        r"(?i)(\bpassword\s*=\s*)('([^']*)'|\"([^\"]*)\"|\S+)",
        r"\1***",
        redacted,
    )
    return redacted


class PostgresBackend(Store):
    """Store implementation backed by psycopg when available.

    The plaintext DSN is passed once to ``psycopg.connect`` and never
    persisted on the instance — only the redacted form survives, so a
    stray ``repr(backend)`` or log statement cannot leak credentials.
    """

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                'psycopg is required for PostgresBackend. Install with: pip install "data-product-forge[postgres]"'
            ) from exc
        self._psycopg = psycopg
        # Stash the redacted form (safe to log) and connect with the
        # plaintext one in a single statement; the local ``dsn`` goes
        # out of scope once ``__init__`` returns.
        self.dsn = _redact_dsn(dsn)
        self.conn = psycopg.connect(dsn)
        self._init_db()

    def __repr__(self) -> str:  # pragma: no cover — defensive
        return f"PostgresBackend(dsn={self.dsn!r})"

    def _init_db(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists fluid_store (
                    namespace text not null,
                    key text not null,
                    value_blob jsonb not null,
                    metadata jsonb,
                    created_at timestamptz not null,
                    expires_at timestamptz,
                    fluid_version text,
                    primary key (namespace, key)
                )
                """
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Per-operation guards
    # ------------------------------------------------------------------
    # Each public method is wrapped so a transient connection drop (DB
    # restart, network blip, etc.) degrades to the documented
    # store-miss shape (``None`` / ``[]`` / ``0``) instead of
    # surfacing a raw ``psycopg.OperationalError`` mid-forge. The
    # constructor is hardened by ``factory._safe_store_init``; these
    # guards keep an already-resolved backend honest across the run.
    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "select namespace, key, value_blob, metadata, created_at, expires_at, fluid_version from fluid_store where namespace = %s and key = %s",
                    (ns, key),
                )
                row = cur.fetchone()
            return self._row_to_record(row)
        except Exception as exc:  # noqa: BLE001 - degrade to miss
            _log.debug(
                "PostgresBackend.get(%r, %r) failed (%s: %s); returning None",
                ns,
                key,
                type(exc).__name__,
                exc,
            )
            return None

    def put(
        self,
        ns: str,
        key: str,
        value: Any,
        *,
        ttl: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        fluid_version: Optional[str] = None,
    ) -> StoreRecord:
        created_at = utc_now()
        expires_at = created_at + timedelta(seconds=ttl) if ttl else None
        record = StoreRecord(
            namespace=ns,
            key=key,
            value=value,
            metadata=metadata or {},
            created_at=created_at,
            expires_at=expires_at,
            fluid_version=fluid_version,
        )
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fluid_store(namespace, key, value_blob, metadata, created_at, expires_at, fluid_version)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict(namespace, key) do update set
                        value_blob = excluded.value_blob,
                        metadata = excluded.metadata,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        fluid_version = excluded.fluid_version
                    """,
                    (
                        ns,
                        key,
                        json.dumps(value, default=str),
                        json.dumps(metadata or {}, default=str),
                        created_at,
                        expires_at,
                        fluid_version,
                    ),
                )
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001 - degrade
            _log.debug(
                "PostgresBackend.put(%r, %r) failed (%s: %s); " "returning unpersisted record",
                ns,
                key,
                type(exc).__name__,
                exc,
            )
            # Rollback the failed transaction so the connection is
            # usable for subsequent operations; ignore secondary
            # errors during rollback.
            try:
                self.conn.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
        return record

    def query(
        self, ns: str, *, filter: Optional[Dict[str, Any]] = None, limit: int = 10
    ) -> List[StoreRecord]:
        normalized = normalize_namespace(ns)
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    select namespace, key, value_blob, metadata, created_at, expires_at, fluid_version
                    from fluid_store
                    where namespace = %s or namespace like %s
                    order by created_at desc
                    limit %s
                    """,
                    (normalized, f"{normalized}/%", limit),
                )
                rows = cur.fetchall()
            return [record for row in rows if (record := self._row_to_record(row)) is not None]
        except Exception as exc:  # noqa: BLE001 - degrade
            _log.debug(
                "PostgresBackend.query(%r) failed (%s: %s); returning empty list",
                ns,
                type(exc).__name__,
                exc,
            )
            return []

    def search(
        self, ns: str, query: str, *, mode: str = "exact", limit: int = 10
    ) -> List[StoreRecord]:
        try:
            if mode == "exact":
                record = self.get(ns, query)
                return [record] if record else []
            needle = (query or "").lower()
            matches: List[StoreRecord] = []
            for record in self.query(ns, limit=1000):
                haystack = json.dumps(record.value, default=str).lower()
                if needle in haystack:
                    matches.append(record)
                if len(matches) >= limit:
                    break
            return matches
        except Exception as exc:  # noqa: BLE001 - degrade
            _log.debug(
                "PostgresBackend.search(%r, %r, mode=%r) failed (%s: %s); " "returning empty list",
                ns,
                query,
                mode,
                type(exc).__name__,
                exc,
            )
            return []

    def clear(self, ns: Optional[str] = None) -> int:
        try:
            with self.conn.cursor() as cur:
                if ns is None:
                    cur.execute("select count(*) from fluid_store")
                    count = cur.fetchone()[0]
                    cur.execute("delete from fluid_store")
                else:
                    normalized = normalize_namespace(ns)
                    cur.execute(
                        "select count(*) from fluid_store where namespace = %s or namespace like %s",
                        (normalized, f"{normalized}/%"),
                    )
                    count = cur.fetchone()[0]
                    cur.execute(
                        "delete from fluid_store where namespace = %s or namespace like %s",
                        (normalized, f"{normalized}/%"),
                    )
            self.conn.commit()
            return int(count)
        except Exception as exc:  # noqa: BLE001 - degrade
            _log.debug(
                "PostgresBackend.clear(%r) failed (%s: %s); returning 0",
                ns,
                type(exc).__name__,
                exc,
            )
            try:
                self.conn.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return 0

    def _row_to_record(self, row: Any) -> Optional[StoreRecord]:
        if row is None:
            return None
        namespace, key, value_blob, metadata, created_at, expires_at, fluid_version = row
        return StoreRecord(
            namespace=namespace,
            key=key,
            value=value_blob,
            metadata=metadata or {},
            created_at=created_at,
            expires_at=expires_at,
            fluid_version=fluid_version,
        )
