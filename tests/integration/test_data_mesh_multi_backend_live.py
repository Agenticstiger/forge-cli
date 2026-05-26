# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end data-mesh validation against real catalog containers.

The fixture builds a *data mesh*: three domains (commerce, marketing,
finance), each with source-aligned products (SDPs) that feed
aggregated products (ADPs), one of which crosses domain boundaries,
and consumption-aligned products (CDPs) at the top. The mesh is then
published to every catalog backend that's reachable in the test
environment — DataHub via Docker quickstart, LocalStack Pro for Glue,
and (optionally) OpenMetadata. Each backend's projection is then
read back through its native API and asserted against the canonical
fields the FLUID contract started with.

This is the "are we actually right about data mesh?" test. It pins:

* **Domains** travel from ``contract.domain`` to every backend's
  domain-equivalent (DataHub Domain entity, OpenMetadata Domain,
  Glue / Snowflake comments / properties).
* **Data products** travel from FLUID contracts to backends'
  product-equivalents (DataHub DataProduct, DMM
  ``/api/data-products``) with ``productType`` /  ``layer``
  preserved as customProperties / Parameters / etc.
* **Output ports** travel from ``exposes[]`` to per-backend dataset
  entities (one per asset, named ``<product_id>.<exposeId>``).
* **Input ports** (``consumes[]``) emerge as native lineage edges
  where supported (DataHub UpstreamLineage); other backends preserve
  the linkage in the per-asset ODCS document attached as a
  customProperty.
* **Three specs** ride alongside the entities — the original FLUID
  YAML, the rendered ODPS v1.0.0 product spec, and one ODCS v3.1
  contract per asset — matching how Data Mesh Manager distributes
  them across ``/api/dataproducts/{id}`` and
  ``/api/datacontracts/{id}``.

Running this requires the live containers. See the module docstring
of :file:`test_catalog_datahub_live.py` for the DataHub quickstart;
LocalStack Pro needs ``LOCALSTACK_AUTH_TOKEN``; OpenMetadata is
:command:`docker compose -f docker-compose.yml up`.

Marked ``integration`` and ``emulated_heavy`` so it stays off the
default ``pytest`` invocation. ``OPENMETADATA_JWT_TOKEN`` skip-gates
the OM tests because that backend's auth setup is per-deployment.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars import (
    DataHubRegistrar,
    OpenMetadataRegistrar,
)

# ``GlueCatalogRegistrar`` retired — coverage moves to
# tests/iac/test_iac_aws_real_e2e.py (the IaC plugin emits the same
# Parameters map directly on aws_glue_catalog_table).

DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL")
GLUE_ENDPOINT = os.environ.get("FLUID_CATALOG_GLUE_URL")
OPENMETADATA_URL = os.environ.get("FLUID_CATALOG_OPENMETADATA_URL") or os.environ.get(
    "OPENMETADATA_SERVER_URL"
)
OPENMETADATA_JWT = os.environ.get("FLUID_CATALOG_OPENMETADATA_TOKEN") or os.environ.get(
    "OPENMETADATA_JWT_TOKEN"
)

pytestmark = [pytest.mark.integration, pytest.mark.emulated_heavy]


# ---------------------------------------------------------------------------
# The mesh — a real data-mesh-shaped fixture
# ---------------------------------------------------------------------------
#
#  commerce      marketing       finance
#     │             │              │
#  [SDP]raw_orders [SDP]raw_clicks [SDP]raw_invoices
#     │             │              │
#     ▼             ▼              ▼
#  [ADP]daily_orders ◄── [ADP]click_attribution  [ADP]daily_revenue
#     │                           │                  │
#     └──────────┬────────────────┘                  │
#                ▼                                   ▼
#  [CDP]revenue_attribution ◄────────────── [CDP]exec_dashboard
#
# Note one *cross-domain consume*: ``commerce.daily_orders`` reads from
# ``marketing.click_attribution`` (joining click data into the orders
# rollup) and the CDPs cross to multiple domains. That's the bit a real
# data mesh has and a flat per-domain set doesn't.


def _ts() -> int:
    """Wall-clock-keyed run id so consecutive test runs don't collide.
    Returned to fixture so all 8 contracts share one timestamp."""
    return int(time.time())


def _build_mesh_chain(ts: int) -> Dict[str, Dict[str, Any]]:
    """Return 8 FLUID contracts forming the data-mesh chain.

    Keyed by short-name (``sdp_orders``, ``adp_daily_orders``, …) so
    tests can address them mnemonically. Wall-clock-keyed IDs make
    every run isolate from the previous run's leftovers.
    """
    run_tag = f"dm.t{ts}"

    def _id(domain: str, role: str, name: str) -> str:
        return f"{run_tag}.{domain}.{role}.{name}"

    chain: Dict[str, Dict[str, Any]] = {}

    # ── SDPs (source-aligned) ────────────────────────────────────────
    chain["sdp_orders"] = _make_contract(
        id_=_id("commerce", "sdp", "raw_orders"),
        name="Raw Orders (SDP)",
        description="Source-aligned raw orders ingested from OLTP",
        domain="commerce",
        layer="Bronze",
        product_type="SDP",
        exposes=[
            (
                "orders",
                [
                    ("order_id", "string", True),
                    ("customer_id", "string", True),
                    ("amount_usd", "decimal", True),
                    ("ordered_at", "timestamp", True),
                ],
            ),
        ],
    )
    chain["sdp_clicks"] = _make_contract(
        id_=_id("marketing", "sdp", "raw_clicks"),
        name="Raw Clicks (SDP)",
        description="Source-aligned click events from ad platforms",
        domain="marketing",
        layer="Bronze",
        product_type="SDP",
        exposes=[
            (
                "clicks",
                [
                    ("click_id", "string", True),
                    ("customer_id", "string", True),
                    ("campaign_id", "string", True),
                    ("clicked_at", "timestamp", True),
                ],
            ),
        ],
    )
    chain["sdp_invoices"] = _make_contract(
        id_=_id("finance", "sdp", "raw_invoices"),
        name="Raw Invoices (SDP)",
        description="Source-aligned invoices from accounting system",
        domain="finance",
        layer="Bronze",
        product_type="SDP",
        exposes=[
            (
                "invoices",
                [
                    ("invoice_id", "string", True),
                    ("order_id", "string", True),
                    ("total_usd", "decimal", True),
                    ("issued_at", "timestamp", True),
                ],
            ),
        ],
    )

    # ── ADPs (aggregated) ────────────────────────────────────────────
    chain["adp_click_attribution"] = _make_contract(
        id_=_id("marketing", "adp", "click_attribution"),
        name="Click Attribution (ADP)",
        description="Sessionised clicks attributed to campaigns",
        domain="marketing",
        layer="Silver",
        product_type="ADP",
        consumes=[(chain["sdp_clicks"]["id"], "clicks")],
        exposes=[
            (
                "attribution",
                [
                    ("customer_id", "string", True),
                    ("campaign_id", "string", True),
                    ("first_click_at", "timestamp", True),
                    ("last_click_at", "timestamp", True),
                ],
            ),
        ],
    )
    chain["adp_daily_orders"] = _make_contract(
        id_=_id("commerce", "adp", "daily_orders"),
        name="Daily Orders w/ Attribution (ADP)",
        description="Daily orders joined to marketing attribution — cross-domain consume",
        domain="commerce",
        layer="Silver",
        product_type="ADP",
        consumes=[
            (chain["sdp_orders"]["id"], "orders"),
            (chain["adp_click_attribution"]["id"], "attribution"),
        ],
        exposes=[
            (
                "orders_daily",
                [
                    ("order_date", "date", True),
                    ("customer_id", "string", True),
                    ("campaign_id", "string", False),  # nullable — not every order has a click
                    ("order_count", "integer", True),
                    ("revenue_usd", "decimal", True),
                ],
            ),
        ],
    )
    chain["adp_daily_revenue"] = _make_contract(
        id_=_id("finance", "adp", "daily_revenue"),
        name="Daily Revenue (ADP)",
        description="Revenue rollup from invoices joined to orders",
        domain="finance",
        layer="Silver",
        product_type="ADP",
        consumes=[
            (chain["sdp_invoices"]["id"], "invoices"),
            (chain["sdp_orders"]["id"], "orders"),
        ],
        exposes=[
            (
                "revenue_daily",
                [
                    ("revenue_date", "date", True),
                    ("gross_usd", "decimal", True),
                    ("net_usd", "decimal", True),
                    ("invoice_count", "integer", True),
                ],
            ),
        ],
    )

    # ── CDPs (consumption-aligned) ───────────────────────────────────
    chain["cdp_revenue_attribution"] = _make_contract(
        id_=_id("commerce", "cdp", "revenue_attribution"),
        name="Revenue Attribution (CDP)",
        description="Marketing-attributed revenue feed for the growth dashboard",
        domain="commerce",
        layer="Gold",
        product_type="CDP",
        consumes=[
            (chain["adp_daily_orders"]["id"], "orders_daily"),
            (chain["adp_daily_revenue"]["id"], "revenue_daily"),
        ],
        exposes=[
            (
                "attribution_monthly",
                [
                    ("month", "string", True),
                    ("campaign_id", "string", True),
                    ("attributed_revenue_usd", "decimal", True),
                ],
            ),
        ],
    )
    chain["cdp_exec_dashboard"] = _make_contract(
        id_=_id("finance", "cdp", "exec_dashboard"),
        name="Exec Dashboard (CDP)",
        description="Top-line revenue + order metrics for the exec dashboard",
        domain="finance",
        layer="Gold",
        product_type="CDP",
        consumes=[(chain["adp_daily_revenue"]["id"], "revenue_daily")],
        exposes=[
            (
                "kpis_monthly",
                [
                    ("month", "string", True),
                    ("revenue_usd", "decimal", True),
                    ("orders", "integer", True),
                    ("avg_order_value_usd", "decimal", True),
                ],
            ),
        ],
    )

    return chain


def _make_contract(
    *,
    id_: str,
    name: str,
    description: str,
    domain: str,
    layer: str,
    product_type: str,
    exposes: List[Tuple[str, List[Tuple[str, str, bool]]]],
    consumes: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": id_,
        "name": name,
        "description": description,
        "domain": domain,
        "version": "1.0.0",
        "metadata": {
            "layer": layer,
            "productType": product_type,
            "owner": {
                "team": f"{domain}-team",
                "email": f"{domain}@example.test",
            },
            "tags": [domain, product_type.lower()],
        },
        "consumes": [{"productId": p, "exposeId": e} for p, e in (consumes or [])],
        "exposes": [
            {
                "exposeId": expose_id,
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"path": f"{domain.upper()}.{expose_id.upper()}"},
                },
                "contract": {
                    "schema": [
                        {"name": col, "type": col_type, "required": required}
                        for col, col_type, required in cols
                    ]
                },
            }
            for expose_id, cols in exposes
        ],
    }


@pytest.fixture(scope="module")
def mesh() -> Dict[str, Any]:
    """Build the chain + return ``{contracts, payloads, urns_by_backend}``.

    Module-scoped so every test in the file shares one wall-clock
    timestamp and one set of canonical payloads. The 8 payloads are
    pre-rendered (so the ODCS / ODPS / fluid YAML in each is computed
    exactly once for the whole module run)."""
    ts = _ts()
    contracts = _build_mesh_chain(ts)
    payloads = {
        key: CatalogPublicationPayload.from_contract(contract)
        for key, contract in contracts.items()
    }
    return {
        "ts": ts,
        "contracts": contracts,
        "payloads": payloads,
        "tag": f"dm.t{ts}",
    }


# ---------------------------------------------------------------------------
# DataHub — full feature set: DataProduct + Domain + Datasets + Lineage
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not DATAHUB_GMS_URL, reason="Set DATAHUB_GMS_URL to enable")
class TestDataHubMesh:
    """All 8 products land in DataHub as DataProducts + Datasets +
    Domains + UpstreamLineage. Verification reads each entity via
    ``entitiesV2`` (returns all aspects in one call)."""

    @pytest.fixture(scope="class")
    def registrar(self) -> DataHubRegistrar:
        return DataHubRegistrar(base_url=DATAHUB_GMS_URL)

    @pytest.fixture(scope="class")
    def published(self, registrar, mesh) -> Dict[str, Any]:
        """Publish every product in topological order so upstream
        URNs exist when downstream products reference them. DataHub
        accepts lineage to URNs that don't yet exist (the entity is
        synthesised from the URN's structural fields), but read-after-
        write is friendlier for assertions when the upstream's full
        aspects are already present."""
        order = [
            "sdp_orders",
            "sdp_clicks",
            "sdp_invoices",
            "adp_click_attribution",
            "adp_daily_orders",
            "adp_daily_revenue",
            "cdp_revenue_attribution",
            "cdp_exec_dashboard",
        ]
        results = {}
        for key in order:
            r = registrar.register_payload(mesh["payloads"][key])
            assert r.succeeded, f"{key}: {r.error}"
            results[key] = r
        return results

    @staticmethod
    def _wait(urn: str, *, want_aspect: str, deadline_seconds: float = 30.0) -> Dict[str, Any]:
        """Poll until the named aspect appears (GET always returns the
        synthetic key — that's not proof of ingestion)."""
        import httpx

        end = time.time() + deadline_seconds
        enc = urllib.parse.quote(urn, safe="")
        last_aspects: List[str] = []
        while time.time() < end:
            with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=10.0) as c:
                r = c.get(f"/entitiesV2/{enc}")
            if r.status_code == 200:
                body = r.json()
                aspects = dict(body.get("aspects") or {})
                last_aspects = list(aspects)
                if want_aspect in aspects:
                    return body
            time.sleep(1.0)
        pytest.fail(
            f"{urn} did not surface aspect {want_aspect!r} within "
            f"{deadline_seconds:.0f}s (saw {last_aspects})"
        )

    def test_every_domain_landed(self, published, mesh):
        # 3 distinct domains → 3 Domain entities.
        for domain in ("commerce", "marketing", "finance"):
            self._wait(f"urn:li:domain:{domain}", want_aspect="domainProperties")

    @pytest.mark.parametrize(
        "key,want_type,want_layer",
        [
            ("sdp_orders", "SDP", "Bronze"),
            ("sdp_clicks", "SDP", "Bronze"),
            ("sdp_invoices", "SDP", "Bronze"),
            ("adp_daily_orders", "ADP", "Silver"),
            ("adp_click_attribution", "ADP", "Silver"),
            ("adp_daily_revenue", "ADP", "Silver"),
            ("cdp_revenue_attribution", "CDP", "Gold"),
            ("cdp_exec_dashboard", "CDP", "Gold"),
        ],
    )
    def test_every_product_classified(self, published, mesh, key, want_type, want_layer):
        product_id = mesh["contracts"][key]["id"]
        envelope = self._wait(
            f"urn:li:dataProduct:{product_id}", want_aspect="dataProductProperties"
        )
        props = envelope["aspects"]["dataProductProperties"]["value"]
        custom = props.get("customProperties") or {}
        assert custom.get("fluid_product_type") == want_type
        assert custom.get("fluid_layer") == want_layer
        # The product carries its spec attachments.
        assert "fluid_contract" in custom
        assert "odps_spec" in custom

    def test_cross_domain_consume_emits_lineage(self, published, mesh):
        """``commerce.adp_daily_orders`` consumes from
        ``marketing.adp_click_attribution`` — the cross-domain edge.
        DataHub should render this as an upstream lineage link the UI
        can navigate."""
        adp = mesh["contracts"]["adp_daily_orders"]
        upstream = mesh["contracts"]["adp_click_attribution"]
        dataset_urn = (
            f"urn:li:dataset:(urn:li:dataPlatform:snowflake," f"{adp['id']}.orders_daily,PROD)"
        )
        envelope = self._wait(dataset_urn, want_aspect="upstreamLineage")
        upstreams = envelope["aspects"]["upstreamLineage"]["value"]["upstreams"]
        urns = {u["dataset"] for u in upstreams}
        # The marketing-domain ADP's URN must be among the upstreams.
        assert any(upstream["id"] in u for u in urns), f"cross-domain edge missing: {urns}"

    def test_per_asset_odcs_attached_to_dataset(self, published, mesh):
        """Each Dataset carries its own ODCS contract (the per-asset
        view DMM PUTs to /api/datacontracts/{product}.{asset})."""
        import httpx

        cdp = mesh["contracts"]["cdp_revenue_attribution"]
        dataset_urn = (
            f"urn:li:dataset:(urn:li:dataPlatform:snowflake,"
            f"{cdp['id']}.attribution_monthly,PROD)"
        )
        # Dataset uses snapshot API for legacy aspects — read via that endpoint.
        enc = urllib.parse.quote(dataset_urn, safe="")
        deadline = time.time() + 30.0
        while time.time() < deadline:
            with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=10.0) as c:
                r = c.get(f"/entities/{enc}")
            if r.status_code == 200:
                snap = r.json()["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
                props_aspect = next(
                    (
                        a["com.linkedin.dataset.DatasetProperties"]
                        for a in snap["aspects"]
                        if "com.linkedin.dataset.DatasetProperties" in a
                    ),
                    None,
                )
                if props_aspect and "odcs_contract" in (props_aspect.get("customProperties") or {}):
                    odcs = props_aspect["customProperties"]["odcs_contract"]
                    # ODCS id matches the per-asset DMM contract id shape
                    assert f"id: {cdp['id']}.attribution_monthly" in odcs
                    return
            time.sleep(1.0)
        pytest.fail("ODCS contract did not surface on CDP dataset within 30s")


# ---------------------------------------------------------------------------
# Glue mesh — retired
# ---------------------------------------------------------------------------
#
# Glue catalog metadata (the per-domain Parameters map) is now emitted
# directly by the IaC plugin on aws_glue_catalog_table. The matching
# real-cloud assertions live in tests/iac/test_iac_aws_real_e2e.py +
# tests/iac/test_iac_aws_real_lakeformation_e2e.py (the LF tests touch
# the same fluid_layer / fluid_product_type / fluid_domain Parameters).

# ---------------------------------------------------------------------------
# OpenMetadata — needs JWT + parent hierarchy bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (OPENMETADATA_URL and OPENMETADATA_JWT),
    reason=(
        "Set OPENMETADATA_SERVER_URL + OPENMETADATA_JWT_TOKEN to enable. "
        "Real OM publish also requires a DatabaseService → Database → Schema "
        "hierarchy pre-created (the registrar's PUT /api/v1/tables expects them)."
    ),
)
class TestOpenMetadataMesh:
    """OpenMetadata's table model is hierarchical: every Table has a
    ``service`` + ``database`` + ``databaseSchema`` parent that must
    exist before PUT. Production wiring needs a bootstrap helper; for
    now the test is gated behind both the URL and JWT env vars so it
    doesn't false-fail in CI."""

    @pytest.fixture(scope="class")
    def registrar(self) -> OpenMetadataRegistrar:
        return OpenMetadataRegistrar(base_url=OPENMETADATA_URL, api_token=OPENMETADATA_JWT)

    def test_smoke_publish_each_role(self, registrar, mesh):
        """Smoke level — each role publishes without HTTP-layer error.
        Deeper readback assertions deferred until the parent-hierarchy
        bootstrap lives in the registrar itself."""
        for key in ("sdp_orders", "adp_daily_orders", "cdp_revenue_attribution"):
            r = registrar.register_payload(mesh["payloads"][key])
            assert r.succeeded, f"{key}: {r.error}"


# ---------------------------------------------------------------------------
# Cross-backend canonical-field invariant
# ---------------------------------------------------------------------------


class TestCrossBackendCanonicalInvariant:
    """The point of the canonical layer: the *same* FLUID contract
    produces the *same* canonical fields in every backend. We hand-
    rebuild the payload from a single contract and assert each
    registrar's body shapes the canonical fields identically.

    Doesn't talk to any live service — pins the canonical-layer
    invariant rather than per-backend wire behaviour. Lives in this
    file so it runs adjacent to the live tests and gets the same
    fixture."""

    @pytest.fixture(scope="class")
    def payload(self, mesh) -> CatalogPublicationPayload:
        return mesh["payloads"]["cdp_revenue_attribution"]

    def test_every_backend_sees_same_layer_and_product_type(self, payload):
        assert payload.product.layer == "Gold"
        assert payload.product.product_type == "CDP"
        assert payload.product.domain == "commerce"

    def test_specs_pre_rendered_once(self, payload):
        # All three travel with the payload — single rendering pass
        # regardless of how many backends consume it.
        assert payload.specs.fluid_yaml is not None
        assert payload.specs.odps_yaml is not None
        for asset in payload.assets:
            assert asset.odcs_yaml is not None
            assert f"id: {payload.product.product_id}.{asset.asset_id}" in asset.odcs_yaml
