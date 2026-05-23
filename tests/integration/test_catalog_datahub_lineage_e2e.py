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

"""End-to-end lineage test against a live DataHub:

SDP (Bronze, source-aligned) → ADP (Silver, aggregated) → CDP (Gold,
consumption-aligned), each published via the actual ``fluid publish
--target datahub`` CLI subprocess (not the Python API) so the test
exercises the real entry point an operator types.

What this pins:

* All three v0.7.3 product types (``SDP`` / ``ADP`` / ``CDP``, the
  Data Mesh classification — see schema docs for the Bronze↔SDP /
  Silver↔ADP / Gold↔CDP mapping) ingest cleanly into a real DataHub
  GMS, with ``metadata.productType`` + ``metadata.layer`` surfaced as
  ``DatasetProperties.customProperties`` so the DataHub UI shows them
  on the entity page.
* ``UpstreamLineage`` aspects propagate ``contract.consumes[]`` so the
  three datasets form a navigable lineage chain in the DataHub UI's
  Lineage tab.
* The two CLI invocation shapes — single-contract single-target and
  multi-contract single-target — both work without flag drift.

Unlike :file:`test_catalog_datahub_live.py` this test deliberately does
**not** clean up after itself — the whole point is for an operator to
open ``http://localhost:9002`` (username/password ``datahub`` /
``datahub``) and *see* the SDP→ADP→CDP graph rendered. Re-run-safety
comes from the wall-clock-keyed product IDs: every run creates a fresh
chain, so accumulated runs never collide.

How to run:

    datahub docker quickstart --arch m1   # if not already up
    DATAHUB_GMS_URL=http://localhost:8080 \\
      .venv/bin/python -m pytest \\
      tests/integration/test_catalog_datahub_lineage_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL")
DATAHUB_GMS_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.skipif(
        not DATAHUB_GMS_URL,
        reason="Set DATAHUB_GMS_URL=http://localhost:8080 to enable",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Drive the CLI through a launcher snippet rather than the venv's
# ``fluid`` shim or ``python -m fluid_build.cli`` directly. Reason: the
# venv has the *main* workspace's ``fluid_build`` pip-installed in
# editable mode (via a ``.pth`` file under site-packages). That .pth
# wins over ``PYTHONPATH`` for path-based discovery, which would leak
# the main workspace's code into the subprocess. The launcher prepends
# this worktree to ``sys.path[0]`` *before* importing, guaranteeing
# the subprocess exercises this branch's catalog registry, envelope
# shape, and lineage emission.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_fluid_publish(*args: str, env_extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Invoke ``fluid publish`` in a subprocess against the *worktree's*
    ``fluid_build``, with stdout/stderr captured for assertion."""
    launcher = (
        "import sys; "
        f"sys.path.insert(0, {repr(str(_REPO_ROOT))}); "
        "from fluid_build.cli import main; "
        f"sys.argv = ['fluid', 'publish', *{list(args)!r}]; "
        "sys.exit(main())"
    )
    cmd = [sys.executable, "-c", launcher]
    env = {**os.environ, "DATAHUB_GMS_URL": DATAHUB_GMS_URL or ""}
    if DATAHUB_GMS_TOKEN:
        env["DATAHUB_GMS_TOKEN"] = DATAHUB_GMS_TOKEN
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def _gms_get(urn: str, *, timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    import httpx

    encoded = urllib.parse.quote(urn, safe="")
    headers = {"Accept": "application/json"}
    if DATAHUB_GMS_TOKEN:
        headers["Authorization"] = f"Bearer {DATAHUB_GMS_TOKEN}"
    with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=timeout) as c:
        r = c.get(f"/entities/{encoded}", headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _gms_wait_for_urn(urn: str, *, deadline_seconds: float = 30.0) -> Dict[str, Any]:
    """Poll GMS until the entity has *real* aspects beyond the synthetic
    ``DatasetKey``. Critical: GMS always returns at least ``DatasetKey``
    (built deterministically from the URN structure), so a non-None
    response is *not* proof the entity was actually published. We wait
    for ``DatasetProperties`` specifically — that's the first aspect
    the registrar writes — to confirm the ingest is durable + readable."""
    end = time.time() + deadline_seconds
    last_envelope: Optional[Dict[str, Any]] = None
    last_err: Optional[Exception] = None
    while time.time() < end:
        try:
            envelope = _gms_get(urn)
            if envelope:
                last_envelope = envelope
                snapshot = envelope["value"][
                    "com.linkedin.metadata.snapshot.DatasetSnapshot"
                ]
                aspect_names = {
                    list(a.keys())[0].split(".")[-1] for a in snapshot["aspects"]
                }
                if "DatasetProperties" in aspect_names:
                    return envelope
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(1.0)
    pytest.fail(
        f"URN {urn!r} did not become fully visible within {deadline_seconds:.0f}s; "
        f"last envelope had aspects: "
        f"{[list(a.keys())[0] for a in (last_envelope or {}).get('value', {}).get('com.linkedin.metadata.snapshot.DatasetSnapshot', {}).get('aspects', [])]} "
        f"(last error: {last_err})"
    )


def _aspects(envelope: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten the DatasetSnapshot ``aspects: [{aspectName: payload}, ...]``
    list into a name-keyed dict so tests can assert ``aspects['UpstreamLineage']``
    without iterating."""
    snapshot = envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
    out: Dict[str, Dict[str, Any]] = {}
    for aspect_entry in snapshot["aspects"]:
        for full_name, payload in aspect_entry.items():
            out[full_name.split(".")[-1]] = payload
    return out


# ---------------------------------------------------------------------------
# Fixture: the SDP→ADP→CDP chain on disk
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lineage_chain(tmp_path_factory) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Materialise three v0.7.3 contracts under a shared tmp dir and
    return ``({role: path}, {role: urn})``. Scope=module so the chain is
    built once per test session — every test in this file operates on
    the *same* DataHub entities, which mirrors how an operator would
    publish once and then re-inspect repeatedly.

    Wall-clock-keyed ids (``e2e_t<ts>_sdp`` etc.) make every test run
    create a fresh chain — leaving the previous run's entities visible
    in DataHub for an operator to compare against."""
    ts = int(time.time())
    workdir = tmp_path_factory.mktemp("datahub_lineage")

    # Shared metadata block — keep platform = snowflake so the
    # SchemaField type inference + URN composition exercise the
    # registrar's normal path.
    platform = "snowflake"

    sdp_id = f"e2e.lineage.t{ts}.sdp_orders"
    adp_id = f"e2e.lineage.t{ts}.adp_orders_daily"
    cdp_id = f"e2e.lineage.t{ts}.cdp_orders_revenue"

    sdp = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": sdp_id,
        "name": "Raw Orders (SDP)",
        "description": "Source-aligned data product — raw orders ingested from OLTP",
        "domain": "commerce",
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {"team": "data-platform", "email": "platform@example.test"},
            "tags": ["e2e", "lineage", "sdp"],
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": platform,
                    "format": "snowflake_table",
                    "location": {"path": "RAW.ORDERS"},
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string", "required": True},
                        {"name": "customer_id", "type": "string", "required": True},
                        {"name": "amount_usd", "type": "decimal", "required": True},
                        {"name": "ordered_at", "type": "timestamp", "required": True},
                    ],
                },
            }
        ],
    }
    adp = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": adp_id,
        "name": "Daily Orders Aggregate (ADP)",
        "description": "Aggregated data product — daily totals joined to customers",
        "domain": "commerce",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "data-platform", "email": "platform@example.test"},
            "tags": ["e2e", "lineage", "adp"],
        },
        "consumes": [{"productId": sdp_id, "exposeId": "orders"}],
        "exposes": [
            {
                "exposeId": "orders_daily",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": platform,
                    "format": "snowflake_table",
                    "location": {"path": "AGG.ORDERS_DAILY"},
                },
                "contract": {
                    "schema": [
                        {"name": "order_date", "type": "date", "required": True},
                        {"name": "customer_id", "type": "string", "required": True},
                        {"name": "order_count", "type": "integer", "required": True},
                        {"name": "revenue_usd", "type": "decimal", "required": True},
                    ],
                },
            }
        ],
    }
    cdp = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cdp_id,
        "name": "Monthly Revenue (CDP)",
        "description": "Consumption-aligned data product — exec dashboard input",
        "domain": "commerce",
        "metadata": {
            "layer": "Gold",
            "productType": "CDP",
            "owner": {"team": "data-platform", "email": "platform@example.test"},
            "tags": ["e2e", "lineage", "cdp"],
        },
        "consumes": [{"productId": adp_id, "exposeId": "orders_daily"}],
        "exposes": [
            {
                "exposeId": "revenue_monthly",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": platform,
                    "format": "snowflake_table",
                    "location": {"path": "MART.REVENUE_MONTHLY"},
                },
                "contract": {
                    "schema": [
                        {"name": "month", "type": "string", "required": True},
                        {"name": "revenue_usd", "type": "decimal", "required": True},
                        {"name": "order_count", "type": "integer", "required": True},
                    ],
                },
            }
        ],
    }

    paths = {}
    for role, contract in (("sdp", sdp), ("adp", adp), ("cdp", cdp)):
        p = workdir / f"{role}.fluid.yaml"
        p.write_text(yaml.safe_dump(contract, sort_keys=False))
        paths[role] = p

    urns = {
        "sdp": (
            f"urn:li:dataset:(urn:li:dataPlatform:{platform},"
            f"{sdp_id}.orders,PROD)"
        ),
        "adp": (
            f"urn:li:dataset:(urn:li:dataPlatform:{platform},"
            f"{adp_id}.orders_daily,PROD)"
        ),
        "cdp": (
            f"urn:li:dataset:(urn:li:dataPlatform:{platform},"
            f"{cdp_id}.revenue_monthly,PROD)"
        ),
    }

    return paths, urns


# ---------------------------------------------------------------------------
# All three product types publish cleanly + carry the right metadata
# ---------------------------------------------------------------------------


class TestProductTypeMatrix:
    """Each of the three Data Mesh-aligned product types lands in
    DataHub with the right ``customProperties`` so an analyst browsing
    the UI can tell them apart at a glance."""

    @pytest.mark.parametrize(
        "role,expected_layer,expected_product_type",
        [
            ("sdp", "Bronze", "SDP"),
            ("adp", "Silver", "ADP"),
            ("cdp", "Gold", "CDP"),
        ],
    )
    def test_publish_each_role_lands_with_correct_metadata(
        self, lineage_chain, role, expected_layer, expected_product_type
    ):
        paths, urns = lineage_chain
        result = _run_fluid_publish(str(paths[role]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, (
            f"fluid publish failed for {role}:\n"
            f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

        envelope = _gms_wait_for_urn(urns[role])
        aspects = _aspects(envelope)

        # DatasetProperties.customProperties carries the FLUID-native
        # classification — this is what shows up under "Properties" in
        # the DataHub UI and lets an analyst filter by product type.
        props = aspects["DatasetProperties"].get("customProperties", {})
        assert props.get("fluid_layer") == expected_layer, (
            f"role={role}: expected fluid_layer={expected_layer}, got {props!r}"
        )
        assert props.get("fluid_product_type") == expected_product_type
        assert props.get("fluid_domain") == "commerce"


# ---------------------------------------------------------------------------
# Lineage — consumes[] flows into UpstreamLineage end-to-end
# ---------------------------------------------------------------------------


class TestLineageE2E:
    """SDP→ADP→CDP lineage is navigable in DataHub. Each step's
    ``UpstreamLineage`` aspect points at the previous step's URN, so
    DataHub's Lineage tab renders the chain as a navigable graph."""

    def test_sdp_has_no_upstream(self, lineage_chain):
        """SDPs are source-aligned — they read from external systems,
        not from other FLUID products. ``UpstreamLineage`` must be
        absent (omitted from the snapshot rather than emitted with an
        empty ``upstreams`` list)."""
        paths, urns = lineage_chain
        result = _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        envelope = _gms_wait_for_urn(urns["sdp"])
        aspects = _aspects(envelope)
        assert "UpstreamLineage" not in aspects, (
            f"SDP must not declare upstream FLUID products: {aspects.keys()}"
        )

    def test_adp_upstream_points_at_sdp(self, lineage_chain):
        paths, urns = lineage_chain
        # Publish SDP first so it exists when DataHub resolves the
        # upstream URN — DataHub doesn't reject lineage to unknown URNs,
        # but the UI is less useful when the target is missing.
        _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        result = _run_fluid_publish(str(paths["adp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        envelope = _gms_wait_for_urn(urns["adp"])
        aspects = _aspects(envelope)
        upstreams = aspects.get("UpstreamLineage", {}).get("upstreams", [])
        assert upstreams, "ADP must declare an UpstreamLineage aspect"
        upstream_urns = {u["dataset"] for u in upstreams}
        assert urns["sdp"] in upstream_urns, (
            f"ADP upstream {upstream_urns} should include SDP {urns['sdp']}"
        )
        # TRANSFORMED is the right LineageType for a Silver-tier ADP
        # built from a Bronze-tier SDP via aggregation / join logic.
        assert all(u["type"] == "TRANSFORMED" for u in upstreams)

    def test_cdp_upstream_points_at_adp(self, lineage_chain):
        paths, urns = lineage_chain
        # Build the full chain so the lineage graph is non-degenerate.
        _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        _run_fluid_publish(str(paths["adp"]), "--target", "datahub", "--quiet")
        result = _run_fluid_publish(str(paths["cdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        envelope = _gms_wait_for_urn(urns["cdp"])
        aspects = _aspects(envelope)
        upstream_urns = {
            u["dataset"] for u in aspects.get("UpstreamLineage", {}).get("upstreams", [])
        }
        assert urns["adp"] in upstream_urns


# ---------------------------------------------------------------------------
# CLI invocation-shape matrix — every accepted form of `fluid publish`
# ---------------------------------------------------------------------------


class TestFluidPublishCliShapes:
    """Pin every documented invocation shape of ``fluid publish`` against
    a live DataHub. Each shape ends up at the same registrar code path,
    but the *argument parsing* + target-resolution layers above are
    independent and historically have drifted."""

    def test_single_contract_single_target(self, lineage_chain):
        """The canonical shape: ``fluid publish <file> --target datahub``."""
        paths, urns = lineage_chain
        result = _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr
        _gms_wait_for_urn(urns["sdp"])

    def test_multi_contract_single_target(self, lineage_chain):
        """``fluid publish <a.yaml> <b.yaml> <c.yaml> --target datahub``
        publishes all three to the same target. Validates that the
        glob-expansion + per-target dispatch loop in ``run_async``
        doesn't silently drop contracts."""
        paths, urns = lineage_chain
        result = _run_fluid_publish(
            str(paths["sdp"]),
            str(paths["adp"]),
            str(paths["cdp"]),
            "--target",
            "datahub",
            "--quiet",
        )
        assert result.returncode == 0, result.stderr
        for role in ("sdp", "adp", "cdp"):
            _gms_wait_for_urn(urns[role])

    def test_short_form_flag(self, lineage_chain):
        """``-t datahub`` is the argparse short form. Argparse handles
        ``-t`` / ``--target`` synonymously; this test pins that the
        downstream resolution doesn't case-fold or otherwise mangle."""
        paths, urns = lineage_chain
        result = _run_fluid_publish(str(paths["sdp"]), "-t", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr
        _gms_wait_for_urn(urns["sdp"])

    def test_endpoint_override_via_target_colon_syntax(self, lineage_chain):
        """``--target datahub:http://localhost:8080`` exercises the
        ``NAME:endpoint`` per-invocation override. We unset
        ``DATAHUB_GMS_URL`` for this run so the test fails if the
        override isn't being honoured — the env-var path would have
        masked an override regression."""
        paths, urns = lineage_chain
        # Pass DATAHUB_GMS_URL=garbage to prove the colon override wins
        result = _run_fluid_publish(
            str(paths["sdp"]),
            "--target",
            f"datahub:{DATAHUB_GMS_URL}",
            "--quiet",
            env_extra={"DATAHUB_GMS_URL": "http://intentionally-bogus.invalid"},
        )
        assert result.returncode == 0, (
            f"endpoint-override didn't win over env var:\n"
            f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )
        _gms_wait_for_urn(urns["sdp"])

    def test_dry_run_does_not_publish(self, tmp_path):
        """``--dry-run`` must not actually hit GMS. We use a separate
        product_id (not from the shared chain fixture) so its absence
        from GMS post-run is meaningful."""
        ts = int(time.time() * 1000)
        product_id = f"e2e.dryrun.t{ts}.x"
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": product_id,
            "name": "dry-run probe",
            "description": "must never reach DataHub",
            "domain": "test",
            "metadata": {
                "layer": "Bronze",
                "productType": "SDP",
                "owner": {"team": "data-platform", "email": "x@y.z"},
            },
            "exposes": [
                {
                    "exposeId": "probe",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        p = tmp_path / "dryrun.fluid.yaml"
        p.write_text(yaml.safe_dump(contract))

        result = _run_fluid_publish(
            str(p), "--target", "datahub", "--dry-run", "--quiet"
        )
        assert result.returncode == 0, result.stderr

        # Give GMS a generous beat then assert that no *real* aspects
        # landed. We can't use ``_gms_get(urn) is None`` as the check
        # because GMS synthesises a ``DatasetKey`` aspect from the URN
        # alone — it returns a 200 with a partial snapshot even when
        # nothing was ingested. The honest signal is: aspects MUST NOT
        # include ``DatasetProperties`` (which the registrar would
        # write on a real publish).
        time.sleep(2.0)
        urn = (
            f"urn:li:dataset:(urn:li:dataPlatform:snowflake,"
            f"{product_id}.probe,PROD)"
        )
        envelope = _gms_get(urn)
        if envelope is not None:
            snapshot = envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
            aspect_names = {
                list(a.keys())[0].split(".")[-1] for a in snapshot["aspects"]
            }
            assert "DatasetProperties" not in aspect_names, (
                f"--dry-run wrote DatasetProperties to GMS: aspects={aspect_names}"
            )


# ---------------------------------------------------------------------------
# Schema lands too — DataHub renders column names + types in the UI
# ---------------------------------------------------------------------------


class TestDataProductAndDomainEmissionE2E:
    """The architectural goal of this slice: ``fluid publish`` lands a
    real ``DataProduct`` entity in DataHub (visible under the
    ``/dataProduct/`` UI route), not just the physical Dataset
    backing it. Same goes for the contract's ``domain``."""

    def _gms_search(self, entity_type: str, query: str) -> List[Dict[str, Any]]:
        """Drive the GMS search REST API directly so we don't depend on
        the frontend's authenticated GraphQL surface — the integration
        test should pin behaviour at the same layer the registrar
        writes to."""
        import httpx

        with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=15.0) as c:
            r = c.post(
                "/entities?action=search",
                json={"input": query, "entity": entity_type, "start": 0, "count": 50},
                headers={"Content-Type": "application/json"},
            )
        r.raise_for_status()
        return r.json()["value"].get("entities") or []

    def test_dataproduct_entity_exists_after_publish(self, lineage_chain):
        """Publishing the SDP contract creates a DataHub DataProduct
        entity at ``urn:li:dataProduct:<contract.id>``."""
        paths, _urns = lineage_chain
        result = _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        # Read the DataProduct entity directly via GMS — the search
        # index has Kafka lag, but GET is read-after-write consistent
        # for entities ingested via MCP. Use ``entitiesV2`` because
        # legacy ``entities`` only knows about Snapshot-based types.
        import time
        import urllib.parse

        import httpx

        with open(paths["sdp"]) as fh:
            import yaml as _yaml

            contract = _yaml.safe_load(fh)
        product_urn = f"urn:li:dataProduct:{contract['id']}"

        # Poll up to 30s for the DataProduct to be available — Kafka
        # propagation + MCP processing can take a few seconds.
        deadline = time.time() + 30.0
        envelope = None
        while time.time() < deadline:
            enc = urllib.parse.quote(product_urn, safe="")
            with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=15.0) as c:
                r = c.get(f"/entitiesV2/{enc}")
            if r.status_code == 200:
                body = r.json()
                aspect_names = set((body.get("aspects") or {}).keys())
                # Wait for the real aspect, not just the synthetic key.
                if "dataProductProperties" in aspect_names:
                    envelope = body
                    break
            time.sleep(1.0)
        assert envelope is not None, (
            f"DataProduct {product_urn} did not become readable within 30s"
        )

        props = envelope["aspects"]["dataProductProperties"]["value"]
        # The DataProduct carries the FLUID-native classification so an
        # analyst browsing DataHub's Data Products view can filter by
        # SDP / ADP / CDP without round-tripping back to the contract.
        custom = props.get("customProperties") or {}
        assert custom.get("fluid_product_type") == "SDP"
        assert custom.get("fluid_layer") == "Bronze"
        # ``assets`` lists every expose-as-dataset of the contract so
        # the DataProduct page's Assets tab renders the full backing.
        asset_urns = {a["destinationUrn"] for a in props.get("assets") or []}
        assert (
            "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
            f"{contract['id']}.orders,PROD)"
        ) in asset_urns

    def test_domain_entity_created_from_contract_domain(self, lineage_chain):
        """``contract.domain: commerce`` lands as a Domain entity
        (``urn:li:domain:commerce``) that the DataProduct then links
        to via the ``domains`` aspect — enabling Browse-by-Domain in
        the UI."""
        paths, _urns = lineage_chain
        result = _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        import time
        import urllib.parse

        import httpx

        domain_urn = "urn:li:domain:commerce"
        deadline = time.time() + 30.0
        envelope = None
        while time.time() < deadline:
            enc = urllib.parse.quote(domain_urn, safe="")
            with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=15.0) as c:
                r = c.get(f"/entitiesV2/{enc}")
            if r.status_code == 200:
                body = r.json()
                if "domainProperties" in (body.get("aspects") or {}):
                    envelope = body
                    break
            time.sleep(1.0)
        assert envelope is not None, f"Domain {domain_urn} not readable within 30s"

        props = envelope["aspects"]["domainProperties"]["value"]
        assert props.get("name") == "commerce"


class TestSpecAttachmentE2E:
    """The full FLUID contract, its ODPS rendering, and one ODCS
    contract per asset must land alongside the DataHub entities so
    analysts can pull the canonical specs without leaving the UI.

    Mirrors how ``DataMeshManagerProvider`` distributes the same
    information across DMM's dedicated ``/api/dataproducts/{id}`` and
    ``/api/datacontracts/{id}`` endpoints — DataHub has no separate
    contract surface so we attach the rendered specs inline as
    ``customProperties``.
    """

    @staticmethod
    def _fetch_entity_v2(urn: str, *, deadline_seconds: float = 30.0):
        """Poll ``/entitiesV2/{urn}`` until a non-key aspect is present.
        Reuses the deliberate read-after-write pattern from
        ``TestDataProductAndDomainEmissionE2E`` rather than the search
        index, which has Kafka lag on the order of minutes."""
        import time
        import urllib.parse

        import httpx

        deadline = time.time() + deadline_seconds
        while time.time() < deadline:
            enc = urllib.parse.quote(urn, safe="")
            with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=15.0) as c:
                r = c.get(f"/entitiesV2/{enc}")
            if r.status_code == 200:
                body = r.json()
                aspect_names = set((body.get("aspects") or {}).keys())
                # Wait until at least one real aspect has flushed.
                if aspect_names - {"dataProductKey", "datasetKey", "domainKey"}:
                    return body
            time.sleep(1.0)
        pytest.fail(f"{urn} did not become fully visible within {deadline_seconds:.0f}s")

    def test_dataproduct_carries_fluid_yaml_and_odps_spec(self, lineage_chain):
        """The DataProduct's ``customProperties`` carries the source FLUID
        contract verbatim under ``fluid_contract`` and its ODPS rendering
        under ``odps_spec`` — the two artifacts DMM PUTs as the
        DataProduct body and its ``.odps-bitol.yaml`` companion."""
        import yaml

        paths, _urns = lineage_chain
        # Use the CDP because it exercises the consumes[] field — that's
        # the bit of the FLUID schema most likely to drift through the
        # ODPS conversion.
        with open(paths["cdp"]) as fh:
            contract = yaml.safe_load(fh)
        product_urn = f"urn:li:dataProduct:{contract['id']}"

        result = _run_fluid_publish(str(paths["cdp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        envelope = self._fetch_entity_v2(product_urn)
        props = envelope["aspects"]["dataProductProperties"]["value"]
        custom = {p: v for p, v in (props.get("customProperties") or {}).items()}

        assert "fluid_contract" in custom, (
            "DataProduct must carry the source FLUID contract YAML — same "
            "as DMM's PUT to /api/dataproducts/{id} payload"
        )
        assert "odps_spec" in custom, (
            "DataProduct must carry the ODPS rendering — same as DMM's "
            "companion .odps-bitol.yaml"
        )

        # Parse-back checks: the attached YAML must be valid and carry
        # the contract id so an analyst can confirm provenance without
        # re-running anything.
        round_trip_fluid = yaml.safe_load(custom["fluid_contract"])
        assert round_trip_fluid["id"] == contract["id"]

        round_trip_odps = yaml.safe_load(custom["odps_spec"])
        # ODPS v1.0.0 names the id field ``id`` at the top level, same
        # as fluid, so the equality check is meaningful across the
        # conversion boundary.
        assert round_trip_odps.get("id") == contract["id"]

    def test_dataset_carries_odcs_contract(self, lineage_chain):
        """Each Dataset carries the per-expose ODCS contract under
        ``customProperties.odcs_contract`` — the same per-asset
        payload DMM PUTs to ``/api/datacontracts/{product_id}.{expose_id}``."""
        import yaml

        paths, urns = lineage_chain
        with open(paths["adp"]) as fh:
            contract = yaml.safe_load(fh)

        result = _run_fluid_publish(str(paths["adp"]), "--target", "datahub", "--quiet")
        assert result.returncode == 0, result.stderr

        # Read back the Dataset (snapshot API) and find DatasetProperties
        envelope = _gms_wait_for_urn(urns["adp"])
        snapshot = envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
        props_aspect = next(
            (
                a["com.linkedin.dataset.DatasetProperties"]
                for a in snapshot["aspects"]
                if "com.linkedin.dataset.DatasetProperties" in a
            ),
            None,
        )
        assert props_aspect is not None
        custom = props_aspect.get("customProperties") or {}

        assert "odcs_contract" in custom, (
            "Dataset must carry the per-asset ODCS contract — DMM emits "
            "this to /api/datacontracts/{product_id}.{expose_id}"
        )
        odcs = yaml.safe_load(custom["odcs_contract"])
        # ODCS v3.1 carries the contract id at the top level. The DMM
        # convention pinned at ``_publish_odcs_per_expose`` is
        # ``{product_id}.{expose_id}`` so an analyst can derive the
        # filename / DMM endpoint from the contract id alone.
        assert odcs.get("id") == f"{contract['id']}.orders_daily"


class TestSchemaPropagationE2E:
    """Every column in ``exposes[0].contract.schema`` should land as a
    ``SchemaField`` so the DataHub UI's Schema tab renders correctly."""

    def test_columns_appear_in_schema_metadata(self, lineage_chain):
        paths, urns = lineage_chain
        _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        envelope = _gms_wait_for_urn(urns["sdp"])
        aspects = _aspects(envelope)
        fields = aspects["SchemaMetadata"]["fields"]
        field_paths = {f["fieldPath"] for f in fields}
        assert {"order_id", "customer_id", "amount_usd", "ordered_at"} <= field_paths

    def test_field_types_mapped_to_datahub_primitives(self, lineage_chain):
        """``amount_usd: decimal`` → ``NumberType``; ``ordered_at: timestamp``
        → ``DateType``. The mapping in ``_schema_field_type`` is best-
        effort but the common-case primitives must land on the right
        side."""
        paths, urns = lineage_chain
        _run_fluid_publish(str(paths["sdp"]), "--target", "datahub", "--quiet")
        envelope = _gms_wait_for_urn(urns["sdp"])
        aspects = _aspects(envelope)
        fields_by_path = {f["fieldPath"]: f for f in aspects["SchemaMetadata"]["fields"]}
        amount = fields_by_path["amount_usd"]
        ordered = fields_by_path["ordered_at"]
        assert "NumberType" in str(amount["type"]), f"amount_usd type: {amount['type']!r}"
        assert "DateType" in str(ordered["type"]), f"ordered_at type: {ordered['type']!r}"
