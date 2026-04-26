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

"""JSON file-backed store backend."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.schema_manager import FluidSchemaManager

from ..base import Store, StoreRecord, utc_now


class FileBackend(Store):
    """Persist namespaced records under ``~/.fluid/store``."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        workspace_root: Optional[Path] = None,
        fluid_version: Optional[str] = None,
    ) -> None:
        self.root = (root or (Path.home() / ".fluid" / "store")).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = workspace_root or Path.cwd()
        self.default_fluid_version = fluid_version or FluidSchemaManager.latest_bundled_version()
        # Guard so we only announce the legacy read once per process.
        self._legacy_announced = False

    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        path = self._record_path(ns, key)
        if path.is_file():
            record = self._load_record(path)
            if record is None:
                return None
            if record.expired:
                path.unlink(missing_ok=True)
                return None
            return record

        legacy = self._legacy_record(ns, key)
        if legacy is not None:
            return legacy
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
            fluid_version=fluid_version or self.default_fluid_version,
        )
        path = self._record_path(ns, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        payload = {
            "namespace": record.namespace,
            "key": record.key,
            "value": record.value,
            "metadata": record.metadata,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "fluid_version": record.fluid_version,
        }
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        temp_path.replace(path)
        return record

    def query(
        self,
        ns: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[StoreRecord]:
        records: List[StoreRecord] = []
        ns_dir = self._namespace_dir(ns)
        if ns_dir.is_dir():
            for file_path in sorted(ns_dir.rglob("*.json")):
                record = self._load_record(file_path)
                if record is None or record.expired:
                    continue
                if self._record_matches(record, filter):
                    records.append(record)
                if len(records) >= limit:
                    return records

        legacy = self._legacy_record(ns, "*")
        if legacy is not None and self._record_matches(legacy, filter):
            records.append(legacy)
        return records[:limit]

    def search(
        self,
        ns: str,
        query: str,
        *,
        mode: str = "exact",
        limit: int = 10,
    ) -> List[StoreRecord]:
        if mode == "exact":
            record = self.get(ns, query)
            return [record] if record else []

        needle = (query or "").lower()
        matches: List[StoreRecord] = []
        for record in self.query(ns, limit=max(limit, 1000)):
            haystack = json.dumps(record.value, sort_keys=True, default=str).lower()
            if needle in haystack:
                matches.append(record)
            if len(matches) >= limit:
                break
        return matches

    def clear(self, ns: Optional[str] = None) -> int:
        if ns is None:
            count = sum(1 for _ in self.root.rglob("*.json"))
            shutil.rmtree(self.root, ignore_errors=True)
            self.root.mkdir(parents=True, exist_ok=True)
            return count

        ns_dir = self._namespace_dir(ns)
        count = sum(1 for _ in ns_dir.rglob("*.json")) if ns_dir.is_dir() else 0
        shutil.rmtree(ns_dir, ignore_errors=True)
        return count

    def _namespace_dir(self, ns: str) -> Path:
        return self.root.joinpath(*[segment for segment in ns.split("/") if segment])

    def _record_path(self, ns: str, key: str) -> Path:
        safe_key = key.replace("/", "__")
        return self._namespace_dir(ns) / f"{safe_key}.json"

    def _load_record(self, path: Path) -> Optional[StoreRecord]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return StoreRecord(
            namespace=payload.get("namespace", ""),
            key=payload.get("key", path.stem),
            value=payload.get("value"),
            metadata=payload.get("metadata") or {},
            created_at=self._parse_dt(payload.get("created_at")),
            expires_at=self._parse_dt(payload.get("expires_at")),
            fluid_version=payload.get("fluid_version"),
        )

    def _parse_dt(self, value: Optional[str]):
        if not value:
            return None
        try:
            from datetime import datetime

            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _legacy_record(self, ns: str, key: str) -> Optional[StoreRecord]:
        if ns != "memory/project":
            return None
        legacy_path = self.workspace_root / ".fluid" / "copilot-memory.json"
        if not legacy_path.is_file():
            return None
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not self._legacy_announced:
            # One-shot notice so users can find where future memory writes land.
            sys.stderr.write(
                (
                    f"[fluid] Reading legacy memory from {legacy_path}; "
                    f"new writes go to {self.root / 'memory' / 'project'}."
                )
                + "\n"
            )
            self._legacy_announced = True
        return StoreRecord(
            namespace=ns,
            key=key,
            value=payload,
            metadata={"legacy_path": str(legacy_path)},
            fluid_version=self.default_fluid_version,
        )

    def _record_matches(self, record: StoreRecord, filter: Optional[Dict[str, Any]]) -> bool:
        if not filter:
            return True
        for field, expected in filter.items():
            if field == "key" and record.key != expected:
                return False
            if field == "namespace" and record.namespace != expected:
                return False
            if record.metadata.get(field) != expected and getattr(record, field, None) != expected:
                return False
        return True
