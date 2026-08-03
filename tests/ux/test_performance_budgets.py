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

"""Performance budgets — assert key UX-felt operations stay snappy.

These bounds are deliberately generous (CI machines vary). The goal is to
catch regressions, not to police absolute speed. A 5x degradation surfaces
loudly; a 10% slowdown is allowed to pass.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fluid_build.api.schema import SchemaColumn, SchemaPolicy
from fluid_build.api.state import Cursor
from fluid_build.build_runners._acquisition_common import utc_now_iso
from fluid_build.build_runners._fingerprint import fingerprint_from_columns
from fluid_build.build_runners._schema_evolution import resolve
from fluid_build.build_runners._state import FileStateStore
from fluid_build.cli.ops.doctor import DoctorScope, run_doctor


class TestPerformanceBudgets:
    def test_validate_only_under_3s(self):
        from fluid_build.schema_manager import FluidSchemaManager

        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.perf",
            "name": "Perf",
            "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "x@y.z"}},
            "builds": [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "capabilities": ["full_refresh"],
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": "/tmp/x"},
                            "mode": "full_refresh",
                            "reader": {"format": "csv", "options": {"header": True}},
                        },
                        "sink": {"format": "parquet"},
                    },
                    "outputs": ["d"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "d",
                    "kind": "table",
                    "binding": {"platform": "local", "format": "parquet"},
                    "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
                }
            ],
        }
        mgr = FluidSchemaManager()
        t0 = time.perf_counter()
        mgr.validate_contract(contract, "0.7.3", offline_only=True)
        elapsed = time.perf_counter() - t0
        # Generous 3s ceiling; typical run is under 50ms.
        assert elapsed < 3.0, f"validate took {elapsed:.3f}s (>3s budget)"

    def test_state_store_round_trip_under_500ms(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        cursor = Cursor(stream="orders", value={"hwm": "2026-01-01"}, updated_at=utc_now_iso())
        t0 = time.perf_counter()
        for i in range(100):
            cursor = Cursor(stream=f"s{i}", value={"i": i}, updated_at=utc_now_iso())
            store.set_cursor("p", "b", cursor)
            store.get_cursor("p", "b", f"s{i}")
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"100 cursor round-trips took {elapsed:.3f}s"

    def test_fingerprint_under_100ms_for_50_columns(self):
        cols = [{"name": f"c{i}", "type": "varchar"} for i in range(50)]
        t0 = time.perf_counter()
        for _ in range(100):
            fingerprint_from_columns(cols)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"100 fingerprint calls took {elapsed:.3f}s"

    def test_schema_evolution_decision_under_50ms(self):
        baseline = [SchemaColumn(name=f"c{i}", type="int") for i in range(50)]
        current = baseline + [SchemaColumn(name="extra", type="varchar")]
        t0 = time.perf_counter()
        for _ in range(100):
            resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"100 schema-evolution resolves took {elapsed:.3f}s"

    def test_doctor_all_scope_under_3s(self):
        t0 = time.perf_counter()
        report = run_doctor(DoctorScope.ALL)
        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, f"doctor all took {elapsed:.3f}s"
        assert report.results, "doctor returned no results"
