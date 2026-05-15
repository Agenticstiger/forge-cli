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

"""Phase 3 — from_data_products composition pipeline."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fluid_build.forge_datamodel.from_data_products import (
    load_upstream_products,
    resolve_upstream_paths,
    run_from_data_products,
)


def _write_contract(
    target: Path,
    *,
    cid: str,
    name: str,
    product_type: str,
    layer: str,
    expose_id: str = "main_output",
    columns=(("id", "integer"), ("amount", "decimal")),
):
    """Build a contract via PyYAML — avoids textwrap.dedent gotchas."""
    import yaml as _yaml

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": name,
        "domain": "commerce",
        "metadata": {
            "layer": layer,
            "productType": product_type,
            "owner": {"team": "data"},
        },
        "exposes": [
            {
                "exposeId": expose_id,
                "kind": "table",
                "contract": {
                    "schema": [{"name": n, "type": t, "required": True} for n, t in columns]
                },
            }
        ],
    }
    target.write_text(_yaml.safe_dump(payload, sort_keys=False))


def test_resolve_upstream_paths_handles_path_refs(tmp_path: Path):
    p1 = tmp_path / "products" / "orders" / "contract.fluid.yaml"
    _write_contract(
        p1, cid="x.commerce.orders_v1", name="orders", product_type="SDP", layer="Bronze"
    )
    paths = resolve_upstream_paths(["products/orders/contract.fluid.yaml"], workspace_root=tmp_path)
    assert paths == [p1.resolve()]


def test_resolve_upstream_paths_handles_id_refs(tmp_path: Path):
    p1 = tmp_path / "products" / "orders" / "contract.fluid.yaml"
    _write_contract(
        p1, cid="x.commerce.orders_v1", name="orders", product_type="SDP", layer="Bronze"
    )
    paths = resolve_upstream_paths(["x.commerce.orders_v1"], workspace_root=tmp_path)
    assert paths == [p1.resolve()]


def test_resolve_upstream_paths_skips_unknown_refs(tmp_path: Path):
    paths = resolve_upstream_paths(["nope.does.not.exist"], workspace_root=tmp_path)
    assert paths == []


def test_load_upstream_products_extracts_canonical_pair(tmp_path: Path):
    p1 = tmp_path / "p" / "contract.fluid.yaml"
    _write_contract(p1, cid="x.y.orders_v1", name="orders", product_type="SDP", layer="Bronze")
    products, problems = load_upstream_products([p1])
    assert len(products) == 1
    assert problems == []
    assert products[0].product_type == "SDP"
    assert products[0].layer == "Bronze"
    assert products[0].exposes[0]["exposeId"] == "main_output"


def test_load_upstream_products_canonicalises_layer_only(tmp_path: Path):
    """Contracts with only layer get productType filled in via the registry."""
    p1 = tmp_path / "p" / "contract.fluid.yaml"
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text(
        textwrap.dedent(
            """
            fluidVersion: '0.7.3'
            kind: DataProduct
            id: x.y.orders_v2
            name: orders
            domain: commerce
            metadata:
              layer: Silver
              owner:
                team: data
            exposes: []
            """
        ).strip()
    )
    products, _ = load_upstream_products([p1])
    assert products[0].product_type == "ADP"  # Silver → ADP via registry


def test_run_from_data_products_happy_path(tmp_path: Path):
    p1 = tmp_path / "p1" / "contract.fluid.yaml"
    _write_contract(p1, cid="x.y.orders_v1", name="orders", product_type="SDP", layer="Bronze")
    p2 = tmp_path / "p2" / "contract.fluid.yaml"
    _write_contract(
        p2, cid="x.y.customers_v1", name="customers", product_type="ADP", layer="Silver"
    )

    ctx = run_from_data_products(
        target_type="ADP",
        upstream_refs=["x.y.orders_v1", "x.y.customers_v1"],
        workspace_root=tmp_path,
    )
    assert ctx.is_valid
    assert ctx.target_type == "ADP"
    assert len(ctx.upstream_products) == 2
    assert {p.id for p in ctx.upstream_products} == {"x.y.orders_v1", "x.y.customers_v1"}


def test_run_from_data_products_rejects_invalid_composition(tmp_path: Path):
    """ADP cannot consume CDP — composition rules must reject."""
    p1 = tmp_path / "p1" / "contract.fluid.yaml"
    _write_contract(
        p1, cid="x.y.dashboards_v1", name="dashboards", product_type="CDP", layer="Gold"
    )

    ctx = run_from_data_products(
        target_type="ADP",
        upstream_refs=["x.y.dashboards_v1"],
        workspace_root=tmp_path,
    )
    assert not ctx.is_valid
    assert any("ADP accepts upstreams" in v for v in ctx.violations)


def test_consumes_block_uses_canonical_shape(tmp_path: Path):
    p1 = tmp_path / "p1" / "contract.fluid.yaml"
    _write_contract(p1, cid="x.y.orders_v1", name="orders", product_type="SDP", layer="Bronze")
    ctx = run_from_data_products(
        target_type="ADP",
        upstream_refs=["x.y.orders_v1"],
        workspace_root=tmp_path,
    )
    rows = ctx.to_consumes_block()
    assert rows == [{"productId": "x.y.orders_v1", "exposeId": "main_output"}]


def test_prompt_summary_caps_token_cost(tmp_path: Path):
    """Schema lists capped at 12 columns, exposes capped at 5, products at 10."""
    cols = tuple((f"col_{i}", "string") for i in range(20))  # 20 cols, should cap to 12
    p = tmp_path / "p" / "contract.fluid.yaml"
    _write_contract(p, cid="x.y.big", name="big", product_type="SDP", layer="Bronze", columns=cols)
    ctx = run_from_data_products(
        target_type="ADP",
        upstream_refs=["x.y.big"],
        workspace_root=tmp_path,
    )
    summary = ctx.to_prompt_summary()
    assert len(summary["upstream_products"]) == 1
    assert len(summary["upstream_products"][0]["exposes"][0]["schema"]) == 12


def test_run_from_data_products_flags_missing_refs(tmp_path: Path):
    ctx = run_from_data_products(
        target_type="ADP",
        upstream_refs=["x.y.does_not_exist"],
        workspace_root=tmp_path,
    )
    assert any("could not be resolved" in v for v in ctx.violations)


# ──────────────────────────────────────────────────────────────────────────
# T3.4 — PII classification propagation
# ──────────────────────────────────────────────────────────────────────────


def test_propagate_pii_tags_from_upstream_to_downstream(tmp_path: Path):
    """Pin: when an upstream SDP has columns tagged
    ``classification: pii``, composing an ADP that inherits matching
    column names MUST carry the PII tag through automatically.
    Without this, downstream contracts silently lose compliance
    information."""
    from fluid_build.forge_datamodel.from_data_products.pipeline import (
        UpstreamProduct,
        propagate_pii_classifications,
    )

    upstream = UpstreamProduct(
        id="rwt.sdp.customers",
        name="Customers SDP",
        product_type="SDP",
        layer="Bronze",
        domain="sales",
        contract_path="/tmp/u.yaml",
        exposes=(
            {
                "exposeId": "raw",
                "kind": "table",
                "schema": [
                    {"name": "customer_id", "type": "integer", "required": True},
                    {
                        "name": "email",
                        "type": "string",
                        "required": True,
                        "classification": "pii",
                    },
                    {
                        "name": "ssn",
                        "type": "string",
                        "required": False,
                        "classification": "pii",
                    },
                ],
            },
        ),
    )

    new_contract = {
        "id": "rwt.adp.email_rollup",
        "exposes": [
            {
                "exposeId": "rollup",
                "contract": {
                    "schema": [
                        {"name": "email_domain", "type": "string"},
                        {"name": "email", "type": "string"},  # carries through
                        {"name": "ssn", "type": "string"},  # carries through
                        {"name": "rollup_count", "type": "integer"},
                    ]
                },
            }
        ],
    }

    log = propagate_pii_classifications(new_contract, [upstream])

    cols = new_contract["exposes"][0]["contract"]["schema"]
    by_name = {c["name"]: c for c in cols}
    # Output is written under the schema-canonical ``sensitivity``
    # field. Upstream ``classification`` is accepted as an alias for
    # back-compat with catalog-style contracts.
    assert by_name["email"]["sensitivity"] == "pii"
    assert by_name["ssn"]["sensitivity"] == "pii"
    # Non-matching columns stay untagged.
    assert "sensitivity" not in by_name["email_domain"]
    assert "classification" not in by_name["email_domain"]
    assert "sensitivity" not in by_name["rollup_count"]
    # Log records both propagations.
    assert any("email" in entry for entry in log)
    assert any("ssn" in entry for entry in log)


def test_propagate_pii_does_not_overwrite_explicit_classification(tmp_path: Path):
    """Operator override always wins. If a downstream column already
    declares a classification (even one stricter or weaker than the
    upstream's), the upstream tag must NOT clobber it."""
    from fluid_build.forge_datamodel.from_data_products.pipeline import (
        UpstreamProduct,
        propagate_pii_classifications,
    )

    upstream = UpstreamProduct(
        id="rwt.sdp.x",
        name="X",
        product_type="SDP",
        layer="Bronze",
        domain="sales",
        contract_path="/tmp/u.yaml",
        exposes=(
            {
                "exposeId": "raw",
                "kind": "table",
                "schema": [
                    {"name": "email", "classification": "pii"},
                ],
            },
        ),
    )
    new_contract = {
        "exposes": [
            {
                "contract": {
                    "schema": [
                        {"name": "email", "classification": "internal"},
                    ]
                }
            }
        ],
    }
    propagate_pii_classifications(new_contract, [upstream])
    # Operator's downstream override (whether under ``classification``
    # or ``sensitivity``) must survive untouched.
    out_col = new_contract["exposes"][0]["contract"]["schema"][0]
    assert out_col.get("classification") == "internal"
    assert "sensitivity" not in out_col


def test_propagate_pii_noop_when_upstreams_have_no_tags(tmp_path: Path):
    """When no upstream column carries a classification, the helper
    is a no-op and returns an empty log."""
    from fluid_build.forge_datamodel.from_data_products.pipeline import (
        UpstreamProduct,
        propagate_pii_classifications,
    )

    upstream = UpstreamProduct(
        id="rwt.sdp.x",
        name="X",
        product_type="SDP",
        layer="Bronze",
        domain="sales",
        contract_path="/tmp/u.yaml",
        exposes=(
            {
                "exposeId": "raw",
                "kind": "table",
                "schema": [{"name": "id", "type": "integer"}],
            },
        ),
    )
    new_contract = {
        "exposes": [
            {"contract": {"schema": [{"name": "id"}]}},
        ],
    }
    log = propagate_pii_classifications(new_contract, [upstream])
    assert log == []
    assert "classification" not in new_contract["exposes"][0]["contract"]["schema"][0]


def test_project_exposes_preserves_sensitivity_tag(tmp_path: Path):
    """Pin: ``_project_exposes`` (the prompt-shape projector) MUST
    preserve column-level sensitivity (PII / PHI) so downstream
    propagation has something to read. Both the schema-canonical
    ``sensitivity`` field AND the catalog-style ``classification``
    alias are accepted as input; output is normalised to
    ``sensitivity``."""
    from fluid_build.forge_datamodel.from_data_products.pipeline import (
        _project_exposes,
    )

    raw = [
        {
            "exposeId": "raw",
            "kind": "table",
            "contract": {
                "schema": [
                    # Schema-canonical input
                    {"name": "email", "type": "string", "sensitivity": "pii"},
                    # Catalog-style alias input
                    {"name": "ssn", "type": "string", "classification": "pii"},
                    {"name": "id", "type": "integer"},
                ]
            },
        }
    ]
    projected = _project_exposes(raw)
    by_name = {c["name"]: c for c in projected[0]["schema"]}
    # Both inputs normalise to the canonical ``sensitivity`` field.
    assert by_name["email"]["sensitivity"] == "pii"
    assert by_name["ssn"]["sensitivity"] == "pii"
    # Untagged columns stay clean.
    assert "sensitivity" not in by_name["id"]
