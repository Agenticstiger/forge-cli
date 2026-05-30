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

"""Tests for the world-class ``fluid market`` UX helpers — actionable
onboarding when no catalog is available, and trust/usage surfacing in the
detailed + JSON views.

Output is captured through the ``cli.market.cprint`` indirection the render
module routes every plain-text print through (so it's deterministic and not
dependent on the console backend).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import fluid_build.cli.market as market_mod
from fluid_build.cli._market_render import (
    _format_usage,
    format_detailed_output,
    format_json_output,
    render_no_catalog_onboarding,
)
from fluid_build.cli.market import DataProductLayer, DataProductMetadata, DataProductStatus


def _capture(monkeypatch) -> list:
    out: list = []
    monkeypatch.setattr(
        market_mod, "cprint", lambda *a, **k: out.append(" ".join(str(x) for x in a))
    )
    return out


def _product(**kw) -> DataProductMetadata:
    base = dict(
        id="p1",
        name="Orders",
        description="Daily orders",
        domain="commerce",
        owner="commerce-team",
        layer=DataProductLayer.GOLD,
        status=DataProductStatus.ACTIVE,
        version="1.0.0",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    base.update(kw)
    return DataProductMetadata(**base)


# --------------------------------------------------------------------------- #
# Onboarding (no usable catalog)                                              #
# --------------------------------------------------------------------------- #
def test_onboarding_no_config_is_actionable(monkeypatch):
    out = _capture(monkeypatch)
    render_no_catalog_onboarding([], console=None)
    blob = "\n".join(out)
    assert "No data catalog is available" in blob
    # The three concrete next steps + the recommended MCP path.
    assert "--config-template" in blob
    assert "--list-catalogs" in blob
    assert "mcp" in blob
    # Reassures on the security model we built.
    assert "environment-sourced" in blob


def test_onboarding_explains_configured_but_unavailable(monkeypatch):
    out = _capture(monkeypatch)
    render_no_catalog_onboarding(["collibra", "mcp"], console=None)
    blob = "\n".join(out)
    assert "Configured catalogs are unavailable: collibra, mcp" in blob
    assert "roadmap" in blob


# --------------------------------------------------------------------------- #
# Trust / usage surfacing                                                     #
# --------------------------------------------------------------------------- #
def test_format_usage_summary():
    assert _format_usage(None) == "N/A"
    assert _format_usage({}) == "N/A"
    assert _format_usage({"popularity": 42}) == "popularity=42"
    got = _format_usage({"usageCount": 7, "other": 1})
    assert "usageCount=7" in got
    # Unknown shape still shows something rather than nothing.
    assert _format_usage({"weird": "x"}) == "weird=x"


def test_detailed_output_surfaces_product_type_and_usage(monkeypatch):
    out = _capture(monkeypatch)
    format_detailed_output(
        _product(product_type="CDP", usage_stats={"popularity": 99}), console=None
    )
    blob = "\n".join(out)
    assert "Product Type: CDP" in blob
    assert "popularity=99" in blob


def test_detailed_output_handles_missing_trust(monkeypatch):
    out = _capture(monkeypatch)
    format_detailed_output(_product(), console=None)
    blob = "\n".join(out)
    assert "Product Type: —" in blob
    assert "Trust / Usage: N/A" in blob


def test_json_output_includes_trust_signals():
    data = json.loads(
        format_json_output([_product(product_type="ADP", usage_stats={"popularity": 5})])
    )
    assert data[0]["product_type"] == "ADP"
    assert data[0]["usage_stats"] == {"popularity": 5}
