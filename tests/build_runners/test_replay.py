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

"""ADP auto-replay on upstream reprocess (Phase-3 #13).

Pin every layer of the loud-drift detection:

* Cursor-rewind detection across numeric / string-timestamp shapes.
* Downstream walk that finds products by ``consumes[].productId``.
* Marker write / read / clear lifecycle.

Without these tests, a refactor that silently flipped the
comparison operator (``<`` → ``<=``) would go unnoticed until a
production cursor gets stuck on its boundary — exactly the failure
mode the loud-drift design is supposed to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.build_runners._replay import (
    REPLAY_MARKER_FILENAME,
    clear_dirty_marker,
    detect_cursor_rewind,
    find_downstream_products,
    list_dirty_products,
    mark_downstream_dirty,
)


class TestDetectCursorRewind:
    def test_numeric_rewind(self):
        assert detect_cursor_rewind(old_cursor_value=100, new_cursor_value=80)

    def test_numeric_advance(self):
        assert not detect_cursor_rewind(old_cursor_value=80, new_cursor_value=100)

    def test_numeric_unchanged(self):
        assert not detect_cursor_rewind(old_cursor_value=100, new_cursor_value=100)

    def test_iso_timestamp_rewind(self):
        assert detect_cursor_rewind(
            old_cursor_value="2026-04-30T00:00:00Z",
            new_cursor_value="2026-04-15T00:00:00Z",
        )

    def test_iso_timestamp_advance(self):
        assert not detect_cursor_rewind(
            old_cursor_value="2026-04-15T00:00:00Z",
            new_cursor_value="2026-04-30T00:00:00Z",
        )

    def test_none_old_no_rewind(self):
        """First-ever cursor write — there's no ``old`` to compare to."""
        assert not detect_cursor_rewind(old_cursor_value=None, new_cursor_value=100)

    def test_mixed_types_dont_crash(self):
        """Defensive — a malformed cursor pair shouldn't raise."""
        assert not detect_cursor_rewind(old_cursor_value=100, new_cursor_value="abc")


def _make_contract(workspace: Path, product_id: str, consumes: list) -> Path:
    """Helper: write a minimal v0.7.3 contract to the workspace."""
    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id.replace(".", "_"),
        "domain": "test",
        "metadata": {"layer": "Silver", "productType": "ADP"},
        "consumes": consumes,
        "builds": [],
        "exposes": [],
    }
    path = workspace / f"{product_id.replace('.', '_')}" / "contract.fluid.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract))
    return path


class TestFindDownstreamProducts:
    def test_finds_one_consumer(self, tmp_path: Path):
        _make_contract(
            tmp_path,
            "silver.crm.customer_360",
            [{"productId": "bronze.crm.customers", "exposeId": "customers"}],
        )
        matches = find_downstream_products(tmp_path, "bronze.crm.customers")
        assert len(matches) == 1
        assert matches[0]["product_id"] == "silver.crm.customer_360"

    def test_finds_multiple_consumers(self, tmp_path: Path):
        _make_contract(
            tmp_path,
            "silver.crm.customer_360",
            [{"productId": "bronze.crm.customers", "exposeId": "customers"}],
        )
        _make_contract(
            tmp_path,
            "silver.fin.churn_score",
            [{"productId": "bronze.crm.customers", "exposeId": "customers"}],
        )
        matches = find_downstream_products(tmp_path, "bronze.crm.customers")
        ids = sorted(m["product_id"] for m in matches)
        assert ids == ["silver.crm.customer_360", "silver.fin.churn_score"]

    def test_skips_unrelated_products(self, tmp_path: Path):
        _make_contract(
            tmp_path,
            "silver.unrelated",
            [{"productId": "bronze.other.source", "exposeId": "x"}],
        )
        matches = find_downstream_products(tmp_path, "bronze.crm.customers")
        assert matches == []

    def test_handles_broken_yaml_gracefully(self, tmp_path: Path):
        """One broken contract shouldn't abort the walk."""
        _make_contract(
            tmp_path,
            "silver.good",
            [{"productId": "bronze.crm.customers", "exposeId": "x"}],
        )
        broken = tmp_path / "broken" / "contract.fluid.yaml"
        broken.parent.mkdir()
        broken.write_text("this: is: not: valid: yaml: [\n")
        matches = find_downstream_products(tmp_path, "bronze.crm.customers")
        assert len(matches) == 1


class TestMarkerLifecycle:
    def test_write_read_clear(self, tmp_path: Path):
        # 1. Two consumers exist.
        _make_contract(
            tmp_path,
            "silver.crm.customer_360",
            [{"productId": "bronze.crm.customers", "exposeId": "customers"}],
        )
        _make_contract(
            tmp_path,
            "silver.fin.churn_score",
            [{"productId": "bronze.crm.customers", "exposeId": "customers"}],
        )

        # 2. Cursor rewinds — mark dirty.
        marked = mark_downstream_dirty(
            workspace_root=tmp_path,
            upstream_product_id="bronze.crm.customers",
            upstream_build_id="main_build",
            upstream_stream="default",
            old_cursor_value="2026-04-30T00:00:00Z",
            new_cursor_value="2026-04-15T00:00:00Z",
        )
        assert sorted(marked) == [
            "silver.crm.customer_360",
            "silver.fin.churn_score",
        ]

        # 3. status reads the markers.
        dirty = list_dirty_products(tmp_path)
        assert len(dirty) == 2
        marker = next(d["marker"] for d in dirty if d["product_id"] == "silver.crm.customer_360")
        assert marker["upstream_product_id"] == "bronze.crm.customers"
        assert marker["old_cursor_value"] == "2026-04-30T00:00:00Z"
        assert marker["new_cursor_value"] == "2026-04-15T00:00:00Z"
        assert "detected_at" in marker

        # 4. ``apply --replay`` clears the marker.
        cleared = clear_dirty_marker(tmp_path, "silver.crm.customer_360")
        assert cleared is True
        dirty_after = list_dirty_products(tmp_path)
        assert len(dirty_after) == 1
        assert dirty_after[0]["product_id"] == "silver.fin.churn_score"

    def test_clear_idempotent(self, tmp_path: Path):
        """Clearing a non-existent marker returns False, doesn't raise."""
        assert clear_dirty_marker(tmp_path, "nonexistent.product") is False

    def test_no_workspace_dir_returns_empty(self, tmp_path: Path):
        """``list_dirty_products`` on a fresh workspace returns []
        (no ``.fluid/`` dir yet)."""
        assert list_dirty_products(tmp_path) == []
