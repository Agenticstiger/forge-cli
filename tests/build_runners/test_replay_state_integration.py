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

"""Pin the ``set_cursor`` → replay-marker integration.

The unit-test layer pins :mod:`build_runners._replay` in isolation;
this layer pins the chokepoint integration: every cursor write that
goes through :class:`FileStateStore.set_cursor` MUST detect a rewind
and emit replay-pending markers for downstream consumers without the
runner having to call ``_replay`` itself. This is what makes the
loud-drift behaviour automatic across all 4 streaming runners
(Kafka-Connect / Debezium / DLT / duckdb) without per-runner wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.api.state import Cursor
from fluid_build.build_runners._state import FileStateStore


def _write_consumer(workspace: Path, *, product_id: str, upstream_id: str) -> Path:
    """Write a minimal ``*.fluid.yaml`` consumer that lists
    ``upstream_id`` in its ``consumes[]``."""
    contract = {
        "id": product_id,
        "consumes": [{"productId": upstream_id, "exposeId": "default"}],
    }
    path = workspace / f"{product_id.replace('.', '_')}.fluid.yaml"
    path.write_text(yaml.safe_dump(contract))
    return path


def _make_store(workspace: Path) -> FileStateStore:
    return FileStateStore(workspace / ".fluid" / "runs")


class TestSetCursorRewindIntegration:
    """The state-store cursor write is the single point all build
    runners go through; replay detection lives there."""

    def test_forward_cursor_does_not_mark_downstream(self, tmp_path: Path):
        store = _make_store(tmp_path)
        _write_consumer(tmp_path, product_id="silver.crm", upstream_id="bronze.crm")
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-01T00:00:00Z", updated_at=""),
        )
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-15T00:00:00Z", updated_at=""),
        )
        marker = tmp_path / ".fluid" / "silver.crm" / "runtime" / "replay-pending.json"
        assert not marker.exists(), "Forward-progress cursor must not trip replay marker"

    def test_rewind_marks_downstream_dirty(self, tmp_path: Path):
        store = _make_store(tmp_path)
        _write_consumer(tmp_path, product_id="silver.crm", upstream_id="bronze.crm")

        # First write establishes the cursor at T+15.
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-15T00:00:00Z", updated_at=""),
        )
        # Second write rewinds to T+1 — a reprocess of the early slice.
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-01T00:00:00Z", updated_at=""),
        )

        marker = tmp_path / ".fluid" / "silver.crm" / "runtime" / "replay-pending.json"
        assert marker.is_file(), "Cursor rewind must emit replay-pending marker"

        payload = json.loads(marker.read_text())
        assert payload["upstream_product_id"] == "bronze.crm"
        assert payload["old_cursor_value"] == "2026-04-15T00:00:00Z"
        assert payload["new_cursor_value"] == "2026-04-01T00:00:00Z"

    def test_rewind_with_no_downstream_does_nothing(self, tmp_path: Path):
        store = _make_store(tmp_path)
        # No consumer contract written — workspace has no downstream.
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-15T00:00:00Z", updated_at=""),
        )
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-01T00:00:00Z", updated_at=""),
        )
        # Walk the .fluid dir; there should be no replay markers anywhere.
        markers = list((tmp_path / ".fluid").rglob("replay-pending.json"))
        assert markers == []

    def test_first_write_skips_detection(self, tmp_path: Path):
        store = _make_store(tmp_path)
        _write_consumer(tmp_path, product_id="silver.crm", upstream_id="bronze.crm")
        # First write — no prior cursor, no rewind possible.
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-15T00:00:00Z", updated_at=""),
        )
        marker = tmp_path / ".fluid" / "silver.crm" / "runtime" / "replay-pending.json"
        assert not marker.exists()

    def test_numeric_cursor_rewind_is_detected(self, tmp_path: Path):
        """Kafka-style integer offsets must trip rewind detection too."""
        store = _make_store(tmp_path)
        _write_consumer(tmp_path, product_id="silver.crm", upstream_id="bronze.crm")
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="100000", updated_at=""),
        )
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="50000", updated_at=""),
        )
        marker = tmp_path / ".fluid" / "silver.crm" / "runtime" / "replay-pending.json"
        assert marker.is_file(), "Numeric offset rewind must emit replay marker"

    def test_multiple_downstream_products_all_marked(self, tmp_path: Path):
        store = _make_store(tmp_path)
        _write_consumer(tmp_path, product_id="silver.crm", upstream_id="bronze.crm")
        _write_consumer(tmp_path, product_id="gold.crm_summary", upstream_id="bronze.crm")
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-15T00:00:00Z", updated_at=""),
        )
        store.set_cursor(
            "bronze.crm",
            "main",
            Cursor(stream="default", value="2026-04-01T00:00:00Z", updated_at=""),
        )
        markers = sorted((tmp_path / ".fluid").rglob("replay-pending.json"))
        assert len(markers) == 2
        product_dirs = {m.parent.parent.name for m in markers}
        assert product_dirs == {"silver.crm", "gold.crm_summary"}
