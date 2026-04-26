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

"""Smoke tests for audit_trail and history archives.

Both modules are tiny, shipped-early (v1.1+ per roadmap), and wired
into ``fluid forge data-model`` today. Coverage confirms the round-trip
shape:

* ``write_audit_event`` emits a parseable JSON file under the audit
  root with the expected event + payload fields.
* ``archive_snapshot`` buckets by contract-hash, auto-increments
  versions, and writes well-formed JSON containing contract + logical
  model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.copilot.store.history import archive_snapshot, history_root


class TestAuditTrail:
    def test_write_audit_event_persists_json_document(self, tmp_path: Path):
        path = write_audit_event(
            "forge.data-model.from-intent",
            payload={"intent": "orders domain", "technique": "dimensional"},
            root=tmp_path,
        )
        assert path.exists()
        assert path.suffix == ".json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["event"] == "forge.data-model.from-intent"
        assert doc["payload"] == {"intent": "orders domain", "technique": "dimensional"}
        # Timestamp must be a valid ISO-8601 UTC string.
        from datetime import datetime

        parsed = datetime.fromisoformat(doc["timestamp_utc"])
        assert parsed.tzinfo is not None

    def test_multiple_events_produce_distinct_files(self, tmp_path: Path):
        p1 = write_audit_event("evt-a", payload={"i": 1}, root=tmp_path)
        p2 = write_audit_event("evt-b", payload={"i": 2}, root=tmp_path)
        assert p1 != p2
        assert p1.parent == p2.parent == tmp_path


class TestHistoryArchive:
    def test_archive_snapshot_buckets_by_contract_hash(self, tmp_path: Path):
        contract = {"model_id": "orders", "version": "0.7.2", "entities": []}
        path = archive_snapshot(contract=contract, root=tmp_path)
        assert path.exists()
        # Bucket dir is a 16-hex sha256 prefix.
        bucket_name = path.parent.name
        assert len(bucket_name) == 16
        assert all(c in "0123456789abcdef" for c in bucket_name)

    def test_archive_snapshot_autoincrements_within_bucket(self, tmp_path: Path):
        contract = {"model_id": "orders", "version": "0.7.2"}
        p1 = archive_snapshot(contract=contract, root=tmp_path)
        p2 = archive_snapshot(contract=contract, root=tmp_path)
        p3 = archive_snapshot(contract=contract, root=tmp_path)
        # Same bucket, versions 1/2/3.
        assert p1.parent == p2.parent == p3.parent
        assert {p1.stem, p2.stem, p3.stem} == {"1", "2", "3"}

    def test_archive_snapshot_records_meta_and_payload(self, tmp_path: Path):
        contract = {"model_id": "customers"}
        logical = {"technique": "data_vault_2", "hubs": [{"name": "hub_customer"}]}
        path = archive_snapshot(contract=contract, logical_model=logical, root=tmp_path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["meta"]["version"] == 1
        assert doc["meta"]["contract_hash"] == path.parent.name
        assert doc["contract"] == contract
        assert doc["logical_model"] == logical

    def test_history_root_defaults_under_dot_fluid(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = history_root()
        assert root == tmp_path / ".fluid" / "store" / "history"

    def test_different_contracts_produce_different_buckets(self, tmp_path: Path):
        p1 = archive_snapshot(contract={"id": "a"}, root=tmp_path)
        p2 = archive_snapshot(contract={"id": "b"}, root=tmp_path)
        assert p1.parent != p2.parent
