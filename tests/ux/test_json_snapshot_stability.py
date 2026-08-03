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

"""JSON output stability for ``--json`` mode and persisted artifacts.

Two snapshots:
1. The error-catalog JSON shape (``cls.as_json()``) — locks the wire shape.
2. Discovered-source contract emitter — same input → same dict every time.

Breaking either should require an explicit version bump.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.cli._errors import CapabilityMismatchError, SchemaValidationError
from fluid_build.cli.discover.emitter import emit_contract
from fluid_build.cli.discover.registry import DiscoveredColumn, DiscoveredStream

# ── Error catalog snapshots ────────────────────────────────────────────


_SCHEMA_ERROR_SNAPSHOT_KEYS = {"code", "doc", "extras", "fix", "what", "where", "why"}


class TestErrorCatalogJsonSnapshot:
    def test_schema_validation_error_keys_locked(self):
        e = SchemaValidationError.for_field(
            contract_path="contract.fluid.yaml",
            line=14,
            col=5,
            field_path="builds[0].properties.source.kind",
            message="missing required field",
        )
        decoded = json.loads(e.as_json())
        assert set(decoded.keys()) == _SCHEMA_ERROR_SNAPSHOT_KEYS

    def test_capability_mismatch_keys_locked(self):
        e = CapabilityMismatchError.for_runner(
            runner_name="duckdb", asked=["cdc"], declared=["full_refresh"]
        )
        decoded = json.loads(e.as_json())
        assert set(decoded.keys()) == _SCHEMA_ERROR_SNAPSHOT_KEYS

    def test_serialisation_is_stable_across_calls(self):
        e = SchemaValidationError(what="x", why="y", fix="z", doc="d")
        a = e.as_json()
        b = e.as_json()
        c = e.as_json()
        assert a == b == c


# ── Emitter snapshot ───────────────────────────────────────────────────


class TestDiscoverEmitterSnapshot:
    def test_same_input_same_contract(self):
        streams = [
            DiscoveredStream(
                name="public.orders",
                columns=[
                    DiscoveredColumn(name="id", type="bigint", nullable=False),
                    DiscoveredColumn(name="amount", type="decimal", nullable=True),
                ],
            ),
        ]
        kwargs = dict(
            product_id="bronze.test_orders",
            name="Test Orders",
            domain="sales",
            owner_team="data-platform",
            owner_email="dp@x.y",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "x"},
            streams=streams,
        )
        a = emit_contract(**kwargs)
        b = emit_contract(**kwargs)
        # Deep equality.
        assert a == b
        # And JSON-stable.
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
