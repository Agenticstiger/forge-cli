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

"""SQLite-backed staged store."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Store, StoreRecord, utc_now
from ..namespaces import normalize_namespace

_log = logging.getLogger(__name__)

# Schema version stamped onto the database via ``PRAGMA user_version``.
# Bump this whenever ``_MIGRATIONS`` grows. Each entry in
# ``_MIGRATIONS`` is the SQL to run when moving FROM version N TO N+1,
# so version 1 is "initial schema" and any future version M > 1 is the
# delta needed to bring a v(M-1) store up to vM.
#
# Borrowed pattern: SQLite's built-in ``user_version`` pragma, exactly
# the shape described in
# https://gluer.org/blog/sqlites-user_version-pragma-for-schema-versioning/
# and https://levlaz.org/sqlite-db-migrations-with-pragma-user_version/.
# Stdlib only — no Alembic / yoyo dependency for ~20 lines of code.
_SCHEMA_VERSION = 1

# Each entry is a list of SQL statements to apply when stepping the
# store FROM ``version - 1`` TO ``version``. Adding a column later
# (the prime reason the previous ``CREATE TABLE IF NOT EXISTS``-only
# init was a footgun) means appending a new key here and bumping
# ``_SCHEMA_VERSION``. Example for a hypothetical v2 add::
#
#     _MIGRATIONS[2] = [
#         "alter table store add column owner text",
#     ]
_MIGRATIONS: Dict[int, List[str]] = {
    1: [
        """
        create table if not exists store (
            namespace text not null,
            key text not null,
            value_blob text not null,
            metadata text,
            created_at text not null,
            expires_at text,
            fluid_version text,
            primary key (namespace, key)
        )
        """,
    ],
}


class SqliteBackend(Store):
    """Store implementation backed by stdlib sqlite3."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or (Path.home() / ".fluid" / "store" / "store.sqlite3")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Bring the database up to ``_SCHEMA_VERSION`` via PRAGMA.

        Read the current ``user_version``; apply every pending migration
        in numeric order; bump ``user_version`` only after the
        migration's SQL succeeds. Each migration runs inside a
        transaction so a half-applied migration won't corrupt the
        store: if any statement raises, the transaction rolls back and
        ``user_version`` stays at the prior value, so re-running the
        constructor retries from the same start point.

        If we encounter a store from a *future* CLI (``user_version``
        > ``_SCHEMA_VERSION``) we log a warning and leave it alone —
        rather than rewinding, we trust the human operator to
        upgrade the CLI before pointing it at the same store.
        """
        current = self._current_user_version()

        if current > _SCHEMA_VERSION:
            _log.warning(
                "Sqlite store at %s reports schema version %d but this "
                "CLI only knows about version %d. Proceeding read-only "
                "is recommended; upgrade fluid-build before writing.",
                self.path,
                current,
                _SCHEMA_VERSION,
            )
            return

        if current == _SCHEMA_VERSION:
            return

        # Apply each pending migration, bumping ``user_version`` only
        # once the migration's SQL has succeeded so a half-applied
        # migration won't leave the store in an undefined state.
        for version in range(current + 1, _SCHEMA_VERSION + 1):
            statements = _MIGRATIONS.get(version, [])
            try:
                with self.conn:  # implicit transaction; commit on success, rollback on raise
                    for stmt in statements:
                        self.conn.execute(stmt)
                    # ``PRAGMA`` can't be parameterised — use an
                    # f-string but only with the int constant.
                    self.conn.execute(f"pragma user_version = {int(version)}")
            except sqlite3.Error as exc:
                _log.error(
                    "Sqlite store migration to v%d failed at %s: %s. " "user_version stays at %d.",
                    version,
                    self.path,
                    exc,
                    self._current_user_version(),
                )
                raise

    def _current_user_version(self) -> int:
        row = self.conn.execute("pragma user_version").fetchone()
        if row is None:
            return 0
        # ``row`` is a sqlite3.Row when row_factory is set; index by 0
        # to support both Row and plain tuple shapes.
        return int(row[0])

    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        row = self.conn.execute(
            "select * from store where namespace = ? and key = ?",
            (ns, key),
        ).fetchone()
        record = self._row_to_record(row)
        if record and record.expired:
            self.clear(ns)
            return None
        return record

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
        self.conn.execute(
            """
            insert into store(namespace, key, value_blob, metadata, created_at, expires_at, fluid_version)
            values(?, ?, ?, ?, ?, ?, ?)
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
                created_at.isoformat(),
                expires_at.isoformat() if expires_at else None,
                fluid_version,
            ),
        )
        self.conn.commit()
        return record

    def query(
        self, ns: str, *, filter: Optional[Dict[str, Any]] = None, limit: int = 10
    ) -> List[StoreRecord]:
        normalized = normalize_namespace(ns)
        rows = self.conn.execute(
            """
            select * from store
            where namespace = ? or namespace like ?
            order by created_at desc
            limit ?
            """,
            (normalized, f"{normalized}/%", limit),
        ).fetchall()
        records = [record for row in rows if (record := self._row_to_record(row)) is not None]
        if not filter:
            return records
        matched = []
        for record in records:
            if all(
                record.metadata.get(key) == value or getattr(record, key, None) == value
                for key, value in filter.items()
            ):
                matched.append(record)
        return matched

    def search(
        self, ns: str, query: str, *, mode: str = "exact", limit: int = 10
    ) -> List[StoreRecord]:
        if mode == "exact":
            record = self.get(ns, query)
            return [record] if record else []
        needle = (query or "").lower()
        matches = []
        for record in self.query(ns, limit=1000):
            haystack = json.dumps(record.value, default=str).lower()
            if needle in haystack:
                matches.append(record)
            if len(matches) >= limit:
                break
        return matches

    def clear(self, ns: Optional[str] = None) -> int:
        if ns is None:
            count = self.conn.execute("select count(*) from store").fetchone()[0]
            self.conn.execute("delete from store")
        else:
            normalized = normalize_namespace(ns)
            count = self.conn.execute(
                "select count(*) from store where namespace = ? or namespace like ?",
                (normalized, f"{normalized}/%"),
            ).fetchone()[0]
            self.conn.execute(
                "delete from store where namespace = ? or namespace like ?",
                (normalized, f"{normalized}/%"),
            )
        self.conn.commit()
        return int(count)

    def _row_to_record(self, row: Optional[sqlite3.Row]) -> Optional[StoreRecord]:
        if row is None:
            return None
        from datetime import datetime

        return StoreRecord(
            namespace=row["namespace"],
            key=row["key"],
            value=json.loads(row["value_blob"]),
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            fluid_version=row["fluid_version"],
        )
