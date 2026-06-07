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

"""Pin the contract-diff-on-regeneration surface (``fluid forge --refine``).

The refine flow must show WHAT CHANGED vs the existing contract before the
write. The presenter borrows the existing version-diff engine
(``fluid_build.api.changelog.compare_contracts`` + ``render_text`` — the same
classifier behind ``fluid diff --baseline``) rather than re-implementing a
differ; these tests pin that reuse + the gating behaviour.
"""

from __future__ import annotations

import pytest

from fluid_build.cli._preview_panel import render_refine_diff


def _contract(schema, *, name="Orders"):
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.sales.orders_v1",
        "name": name,
        "domain": "sales",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
        "exposes": [{"exposeId": "main", "kind": "table", "schema": schema}],
    }


@pytest.fixture()
def record_console():
    rich_console = pytest.importorskip("rich.console")
    return rich_console.Console(record=True, width=100)


def test_added_column_renders_as_non_breaking(record_console):
    """A regenerated contract that ADDS a column surfaces a non-breaking change."""
    old = _contract([{"name": "id", "type": "string"}])
    new = _contract(
        [
            {"name": "id", "type": "string"},
            {"name": "amount", "type": "number"},
        ]
    )
    rendered = render_refine_diff(old, new, console=record_console)
    assert rendered is True
    out = record_console.export_text()
    assert "amount" in out
    assert "non-breaking" in out.lower()


def test_removed_column_is_flagged_breaking(record_console):
    """Dropping an exposed column is a breaking change for downstream consumers."""
    old = _contract(
        [
            {"name": "id", "type": "string"},
            {"name": "amount", "type": "number"},
        ]
    )
    new = _contract([{"name": "id", "type": "string"}])
    rendered = render_refine_diff(old, new, console=record_console)
    assert rendered is True
    out = record_console.export_text().lower()
    assert "breaking" in out
    assert "amount" in out


def test_identical_contract_renders_no_changes(record_console):
    """Regenerating an unchanged contract says so explicitly (no-op, not a crash)."""
    same = _contract([{"name": "id", "type": "string"}])
    rendered = render_refine_diff(same, dict(same), console=record_console)
    assert rendered is False
    assert "no changes" in record_console.export_text().lower()


def test_missing_baseline_is_silent_noop(record_console):
    """Fresh authoring (no existing contract) must not render a diff block."""
    new = _contract([{"name": "id", "type": "string"}])
    assert render_refine_diff(None, new, console=record_console) is False
    assert record_console.export_text().strip() == ""


def test_never_raises_on_garbage_input(record_console):
    """The presenter is advisory and must never crash the authoring flow."""
    assert render_refine_diff("not-a-mapping", 12345, console=record_console) is False
    assert render_refine_diff({}, {}, console=record_console) is False
