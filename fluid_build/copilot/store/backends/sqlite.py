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
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Store, StoreRecord, utc_now
from ..namespaces import normalize_namespace


class SqliteBackend(Store):
    """Store implementation backed by stdlib sqlite3."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or (Path.home() / ".fluid" / "store" / "store.sqlite3")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute("""
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
            """)
        self.conn.commit()

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
