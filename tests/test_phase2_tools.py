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

"""Phase 2 tools — discover_workspace_contracts + generate_dlt_source."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fluid_build.cli.forge_copilot_tools import dispatch_tool_call

# ---------------------------------------------------------------------------
# discover_workspace_contracts
# ---------------------------------------------------------------------------


def _write_contract(target: Path, *, name: str, product_type: str, layer: str):
    target.write_text(
        textwrap.dedent(
            f"""
            fluidVersion: '0.7.3'
            kind: DataProduct
            id: x.y.{name}
            name: {name}
            domain: analytics
            metadata:
              layer: {layer}
              productType: {product_type}
              owner:
                team: data
            exposes:
              - exposeId: {name}_output
                kind: table
                contract:
                  schema:
                    - name: id
                      type: integer
            """
        ).strip()
    )


def test_discover_workspace_contracts_lists_all_when_no_filter(tmp_path: Path):
    (tmp_path / "products" / "p1").mkdir(parents=True)
    (tmp_path / "products" / "p2").mkdir(parents=True)
    _write_contract(
        tmp_path / "products" / "p1" / "contract.fluid.yaml",
        name="orders",
        product_type="SDP",
        layer="Bronze",
    )
    _write_contract(
        tmp_path / "products" / "p2" / "contract.fluid.yaml",
        name="customers",
        product_type="ADP",
        layer="Silver",
    )
    result = dispatch_tool_call("discover_workspace_contracts", {}, workspace_root=tmp_path)
    assert result["total"] == 2
    ids = {p["id"] for p in result["products"]}
    assert ids == {"x.y.orders", "x.y.customers"}


def test_discover_workspace_contracts_filters_by_allowed_types(tmp_path: Path):
    (tmp_path / "products" / "p1").mkdir(parents=True)
    (tmp_path / "products" / "p2").mkdir(parents=True)
    (tmp_path / "products" / "p3").mkdir(parents=True)
    _write_contract(
        tmp_path / "products" / "p1" / "contract.fluid.yaml",
        name="orders",
        product_type="SDP",
        layer="Bronze",
    )
    _write_contract(
        tmp_path / "products" / "p2" / "contract.fluid.yaml",
        name="customers",
        product_type="ADP",
        layer="Silver",
    )
    _write_contract(
        tmp_path / "products" / "p3" / "contract.fluid.yaml",
        name="dashboards",
        product_type="CDP",
        layer="Gold",
    )

    # ADP only accepts SDP+ADP upstreams — filter rejects CDP.
    result = dispatch_tool_call(
        "discover_workspace_contracts",
        {"allowed_upstream_types": ["SDP", "ADP"]},
        workspace_root=tmp_path,
    )
    types = {p["productType"] for p in result["products"]}
    assert types == {"SDP", "ADP"}
    assert "CDP" not in types


def test_discover_workspace_contracts_returns_exposes_summary(tmp_path: Path):
    (tmp_path / "p").mkdir()
    _write_contract(
        tmp_path / "p" / "contract.fluid.yaml",
        name="orders",
        product_type="SDP",
        layer="Bronze",
    )
    result = dispatch_tool_call("discover_workspace_contracts", {}, workspace_root=tmp_path)
    p = result["products"][0]
    assert p["exposes"][0]["exposeId"] == "orders_output"
    assert p["exposes"][0]["schema_columns"] == ["id"]


# ---------------------------------------------------------------------------
# generate_dlt_source
# ---------------------------------------------------------------------------


def test_generate_dlt_source_writes_module(tmp_path: Path):
    result = dispatch_tool_call(
        "generate_dlt_source",
        {
            "name": "stripe_prices",
            "api_url": "https://api.stripe.com/v1/prices",
            "description": "Stripe pricing snapshots",
            "auth_kind": "bearer",
        },
        workspace_root=tmp_path,
    )
    assert "module_path" in result
    written = tmp_path / result["module_path"]
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "@dlt.source" in body
    assert "stripe_prices_source" in body
    assert "STRIPE_PRICES_TOKEN" in body
    assert "Bearer" in body  # auth header set


def test_generate_dlt_source_rejects_path_traversal(tmp_path: Path):
    result = dispatch_tool_call(
        "generate_dlt_source",
        {
            "name": "../../etc/passwd",
            "api_url": "https://example.com",
        },
        workspace_root=tmp_path,
    )
    # Sanitised name should produce a safe ``etc_passwd`` filename, NOT
    # write outside workspace.
    assert "module_path" in result or "error" in result
    if "module_path" in result:
        assert ".." not in result["module_path"]
        # And no file landed outside the workspace.
        assert (tmp_path / result["module_path"]).resolve().is_relative_to(tmp_path.resolve())


def test_generate_dlt_source_rejects_non_http_url(tmp_path: Path):
    result = dispatch_tool_call(
        "generate_dlt_source",
        {
            "name": "x",
            "api_url": "file:///etc/passwd",
        },
        workspace_root=tmp_path,
    )
    assert "error" in result
    assert result["error"] == "InvalidApiUrl"


def test_generate_dlt_source_supports_api_key_auth(tmp_path: Path):
    result = dispatch_tool_call(
        "generate_dlt_source",
        {
            "name": "github",
            "api_url": "https://api.github.com",
            "auth_kind": "api_key",
        },
        workspace_root=tmp_path,
    )
    body = (tmp_path / result["module_path"]).read_text(encoding="utf-8")
    assert "X-API-Key" in body


def test_generate_dlt_source_allows_no_auth(tmp_path: Path):
    result = dispatch_tool_call(
        "generate_dlt_source",
        {
            "name": "open_data",
            "api_url": "https://data.example.org/v1",
            "auth_kind": "none",
        },
        workspace_root=tmp_path,
    )
    body = (tmp_path / result["module_path"]).read_text(encoding="utf-8")
    assert "Bearer" not in body
    assert "X-API-Key" not in body
