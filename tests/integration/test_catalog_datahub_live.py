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

"""Live integration test for the unified catalog publish surface
against a real DataHub instance (run via ``datahub docker quickstart``).

What this pins:

* ``fluid publish --target datahub`` actually ingests a dataset into
  the live GMS — the round-trip from ``CATALOG_PROVIDERS["datahub"]``
  → ``RegistrarBackedCatalogProvider`` → ``DataHubRegistrar`` →
  GMS REST works end-to-end.
* The symmetric path (``properties.catalog.register: [datahub]`` in a
  contract) lands the same dataset under the same URN — the two
  surfaces share a backend, not two diverging code paths.
* The plug-in pattern is honest: env-var resolution
  (``DATAHUB_GMS_URL``) flows through ``FluidConfig`` →
  ``apply_env_overrides`` to the registrar exactly like a YAML config
  block would.
* Resilience: an unreachable GMS degrades to a clean
  ``succeeded=False`` result over a real socket rather than crashing
  the publish stage (``TestResilienceLive``) — the end-to-end
  counterpart to the respx-mocked retry unit tests in
  ``tests/build_runners/test_catalog_datahub_resilience.py``.

Gating: marked ``integration`` + ``emulated_heavy``; self-skips when
``DATAHUB_GMS_URL`` is unset so the test never runs in light suites.
Standing up DataHub takes ~5 minutes and ~6 GB of RAM — the gate
matches the project's "Docker / heavy emulators are CI-integration-
stage only" policy.

How to run:

    datahub docker quickstart --arch m1
    DATAHUB_GMS_URL=http://localhost:8080 \\
      .venv/bin/python -m pytest tests/integration/test_catalog_datahub_live.py -v
    datahub docker nuke  # tear down
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

import pytest

DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL")
DATAHUB_GMS_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.skipif(
        not DATAHUB_GMS_URL,
        reason=(
            "Set DATAHUB_GMS_URL=http://localhost:8080 (and start DataHub via "
            "`datahub docker quickstart --arch m1`) to enable this test."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract(product_id: str = "test.unified.x", expose_id: str = "orders") -> Dict[str, Any]:
    """Minimal v0.7.3-shape contract that the registrar can serialise
    into a DataHub Dataset envelope."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id.split(".")[-1],
        "description": "Live DataHub round-trip test",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "team@example.test"},
        },
        "tags": ["test", "integration"],
        "exposes": [
            {
                "exposeId": expose_id,
                "name": expose_id,
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"path": f"/data/{expose_id}/"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "STRING", "description": "row id"},
                        {"name": "email", "type": "STRING", "description": "customer email"},
                        {"name": "amount", "type": "DECIMAL", "description": "order amount"},
                    ],
                    "schemaPolicy": "discover_and_freeze",
                },
            }
        ],
    }


def _expected_urn(product_id: str, expose_id: str, platform: str = "snowflake") -> str:
    """Dataset URN — one per expose. ``DataHubRegistrar._urn`` builds
    this shape for the physical asset backing a contract expose."""
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{product_id}.{expose_id},PROD)"


def _expected_product_urn(product_id: str) -> str:
    """DataProduct URN — one per FLUID contract. This is what
    ``register()`` returns as the primary URN since a FLUID contract
    is fundamentally a data product, and what an operator searches
    for in the DataHub UI."""
    return f"urn:li:dataProduct:{product_id}"


def _gms_get_entity(urn: str, *, timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    """Read an entity from the live GMS. Returns the JSON envelope, or
    ``None`` if the URN doesn't exist. DataHub is eventually consistent
    — callers that just wrote should poll via :func:`_gms_wait_for_urn`
    rather than calling this directly."""
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
    """Poll GMS until *urn* is visible (DataHub is eventually consistent
    through Kafka → ES). Fails the test with a clear message on timeout
    so flaky-CI runs don't get silent false negatives."""
    end = time.time() + deadline_seconds
    last_err: Optional[Exception] = None
    while time.time() < end:
        try:
            envelope = _gms_get_entity(urn)
            if envelope:
                return envelope
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(1.0)
    pytest.fail(
        f"DataHub URN {urn!r} not visible within {deadline_seconds:.0f}s "
        f"(last error: {last_err})"
    )


def _gms_delete(urn: str) -> None:
    """Best-effort soft-delete so successive runs don't pile up state.
    Failure here is non-fatal — the per-test product_id includes a
    timestamp so a stale URN can't collide with a fresh run."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if DATAHUB_GMS_TOKEN:
        headers["Authorization"] = f"Bearer {DATAHUB_GMS_TOKEN}"
    try:
        with httpx.Client(base_url=DATAHUB_GMS_URL, timeout=10.0) as c:
            c.post("/entities?action=delete", json={"urn": urn}, headers=headers)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def unique_product_id() -> str:
    """One product_id per test, scoped by wall-clock — prevents URN
    collision when re-running while a previous run's data is still in
    DataHub. Also makes the URN searchable by timestamp during
    debugging."""
    return f"test.live.unified.t{int(time.time() * 1000)}"


# ---------------------------------------------------------------------------
# Surface A — ``fluid publish --target datahub`` against live GMS
# ---------------------------------------------------------------------------


class TestPublishTargetSurfaceLive:
    def test_publish_target_datahub_lands_in_gms(self, unique_product_id):
        """End-to-end: ``get_catalog_provider('datahub', config).publish(asset)``
        actually creates the dataset in the running DataHub. Asserts
        on the live GMS round-trip — if the registrar drifts away from
        the GMS API shape, this test fails."""
        import yaml

        from fluid_build.providers.catalogs import CatalogAsset, get_catalog_provider

        provider = get_catalog_provider(
            "datahub", {"endpoint": DATAHUB_GMS_URL, "api_token": DATAHUB_GMS_TOKEN}
        )
        contract = _contract(product_id=unique_product_id)
        urn = _expected_urn(unique_product_id, "orders")

        asset = CatalogAsset(
            id=unique_product_id,
            name=unique_product_id,
            description="Live DataHub round-trip test",
            type="dataproduct",
            domain="test",
            owner="data-platform",
            owner_email="team@example.test",
            layer="Bronze",
            tags=["test", "integration"],
            version="1.0.0",
            platform="snowflake",
            location={"path": "/data/orders/"},
            schema=contract["exposes"][0]["contract"]["schema"],
            contract_yaml=yaml.safe_dump(contract),
        )

        try:
            result = asyncio.run(provider.publish(asset))
            assert result.success, f"publish failed: {result.error}"
            # ``catalog_url`` is the DataProduct URN — the FLUID-native
            # entity. The dataset URN is the *asset* the product backs;
            # both must land in DataHub.
            assert result.catalog_url == _expected_product_urn(unique_product_id)

            envelope = _gms_wait_for_urn(urn)
            snapshot = envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
            assert snapshot["urn"] == urn
            # Schema aspect carried our three columns
            schema_aspect = next(
                a for a in snapshot["aspects"] if "com.linkedin.schema.SchemaMetadata" in a
            )
            fields = schema_aspect["com.linkedin.schema.SchemaMetadata"]["fields"]
            field_names = {f["fieldPath"] for f in fields}
            assert {"id", "email", "amount"} <= field_names
        finally:
            _gms_delete(urn)
            _gms_delete(_expected_product_urn(unique_product_id))


# ---------------------------------------------------------------------------
# Surface B — contract.register: [datahub] via publish_acquisition
# ---------------------------------------------------------------------------


class TestRegisterSurfaceLive:
    """The contract-driven acquisition path. Validates that
    ``build_registrar`` resolves ``datahub`` via the plug-in spec,
    constructs a real ``DataHubRegistrar`` with the right config, and
    actually publishes to GMS."""

    def test_register_via_acquisition_path_lands_in_gms(self, unique_product_id, tmp_path):
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        contract = _contract(product_id=unique_product_id)
        contract["builds"] = [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "outputs": ["orders"],
                "properties": {
                    "catalog": {"register": ["datahub"]},
                },
            }
        ]
        urn = _expected_urn(unique_product_id, "orders")

        try:
            # ``publish_acquisition`` pulls the per-target config from
            # ``FluidConfig().get_catalog_config('datahub')`` which in
            # turn reads ``DATAHUB_GMS_URL`` via the spec env-vars.
            results = publish_acquisition(contract, tmp_path)
            assert len(results) == 1, f"unexpected results: {results}"
            assert results[0].succeeded, f"acquisition publish failed: {results[0].error}"
            assert results[0].target == "datahub"
            # The orchestrator surfaces the DataProduct URN (primary
            # entity from ``RegistrationResult.urn``); the dataset URN
            # is still what we read back from GMS for assertion.
            assert results[0].urn == _expected_product_urn(unique_product_id)

            envelope = _gms_wait_for_urn(urn)
            snapshot = envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
            assert snapshot["urn"] == urn
        finally:
            _gms_delete(urn)
            _gms_delete(_expected_product_urn(unique_product_id))


# ---------------------------------------------------------------------------
# Symmetry — both surfaces produce the same URN for the same product
# ---------------------------------------------------------------------------


class TestSurfaceSymmetryLive:
    """The architectural invariant of the unification: regardless of
    which surface the user picks, the URN and the GMS payload are
    identical. Without this we'd have two competing namespaces for
    the same data product."""

    def test_both_surfaces_produce_identical_urn(self, unique_product_id, tmp_path):
        import yaml

        from fluid_build.cli._acquisition_stage_ext import publish_acquisition
        from fluid_build.providers.catalogs import CatalogAsset, get_catalog_provider

        contract = _contract(product_id=unique_product_id)
        urn = _expected_urn(unique_product_id, "orders")

        # Surface A
        provider = get_catalog_provider(
            "datahub", {"endpoint": DATAHUB_GMS_URL, "api_token": DATAHUB_GMS_TOKEN}
        )
        asset = CatalogAsset(
            id=unique_product_id,
            name=unique_product_id,
            description="symmetry test",
            type="dataproduct",
            domain="test",
            owner="data-platform",
            owner_email="team@example.test",
            layer="Bronze",
            tags=[],
            version="1.0.0",
            platform="snowflake",
            location={"path": "/data/orders/"},
            schema=contract["exposes"][0]["contract"]["schema"],
            contract_yaml=yaml.safe_dump(contract),
        )

        try:
            result_a = asyncio.run(provider.publish(asset))
            assert result_a.success, result_a.error

            # Surface B
            acq_contract = _contract(product_id=unique_product_id)
            acq_contract["builds"] = [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "outputs": ["orders"],
                    "properties": {"catalog": {"register": ["datahub"]}},
                }
            ]
            results_b = publish_acquisition(acq_contract, tmp_path)
            assert results_b[0].succeeded, results_b[0].error

            # Same DataProduct URN from both paths — the architectural
            # invariant the unification slice exists to enforce. The
            # backing dataset (its physical asset) also lives at the
            # same URN, verified below via the GMS read-back.
            product_urn = _expected_product_urn(unique_product_id)
            assert result_a.catalog_url == product_urn
            assert results_b[0].urn == product_urn

            envelope = _gms_wait_for_urn(urn)
            assert envelope["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]["urn"] == urn
        finally:
            _gms_delete(urn)
            _gms_delete(_expected_product_urn(unique_product_id))


# ---------------------------------------------------------------------------
# Spec-driven env-var resolution actually reaches the registrar
# ---------------------------------------------------------------------------


class TestEnvVarResolutionLive:
    """``DATAHUB_GMS_URL`` is declared in the
    :class:`CatalogBackendSpec` for ``datahub``. Setting it should
    flow through ``FluidConfig.get_catalog_config('datahub')`` →
    registrar factory → live publish. This test pins that the
    declarative env-var mapping isn't just paperwork — it actually
    drives the runtime."""

    def test_env_var_only_no_yaml_publishes_to_correct_endpoint(
        self, unique_product_id, tmp_path, monkeypatch
    ):
        from fluid_build.config_manager import FluidConfig

        # FluidConfig should resolve datahub's endpoint from
        # DATAHUB_GMS_URL alone (no catalogs.datahub block in YAML).
        config = FluidConfig().get_catalog_config("datahub")
        assert (
            config.get("endpoint") == DATAHUB_GMS_URL
        ), f"env-var resolution didn't reach FluidConfig: {config!r}"

        # Now run the acquisition path which uses FluidConfig internally
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        contract = _contract(product_id=unique_product_id)
        contract["builds"] = [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "outputs": ["orders"],
                "properties": {"catalog": {"register": ["datahub"]}},
            }
        ]
        urn = _expected_urn(unique_product_id, "orders")
        try:
            results = publish_acquisition(contract, tmp_path)
            assert results[0].succeeded, results[0].error
            _gms_wait_for_urn(urn)
        finally:
            _gms_delete(urn)


# ---------------------------------------------------------------------------
# Resilience — the sink degrades cleanly when GMS is unreachable
# ---------------------------------------------------------------------------


class TestResilienceLive:
    """The registrar is a *metadata sink*: a GMS outage must degrade to a
    clean ``succeeded=False`` result, never an exception that could crash
    the publish stage. This drives the REAL registrar over a REAL socket
    (a closed localhost port → connection refused), complementing the
    respx-mocked ``tests/build_runners/test_catalog_datahub_resilience.py``
    unit path with an end-to-end proof against an actual transport."""

    def test_unreachable_gms_returns_clean_failure_without_raising(self):
        import time as _time

        from fluid_build.api.catalog_publication import CatalogPublicationPayload
        from fluid_build.build_runners.catalog_registrars.datahub import (
            DataHubRegistrar,
        )

        # Port 1 is (practically) never listening → immediate connection
        # refused. Tight budget + zero backoff so a real outage can't
        # stall the pipeline; two attempts prove the retry loop still
        # terminates and hands back a failed result.
        registrar = DataHubRegistrar(
            base_url="http://127.0.0.1:1",
            timeout_seconds=2,
            retry_max_attempts=2,
            retry_base_delay=0.0,
            retry_max_delay=0.0,
        )
        payload = CatalogPublicationPayload.from_contract(_contract("test.resilience.dead"), {})

        start = _time.time()
        result = registrar.register_payload(payload)  # must NOT raise
        elapsed = _time.time() - start

        assert result.succeeded is False
        assert result.error  # carries the transport failure reason
        # Bounded: zero backoff + a couple of fast connection-refused
        # round trips should be well under the per-call timeout budget.
        assert elapsed < 20.0
