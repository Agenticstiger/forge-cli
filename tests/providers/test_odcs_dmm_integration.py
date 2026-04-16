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

"""Integration tests: ODCS ↔ Data Mesh Manager seamless publishing.

Covers:
  1. Each expose port gets a ``dataContractId`` in the output port
  2. Lifecycle status is read from expose.lifecycle.state
  3. Per-expose ODCS contracts are previewed in dry-run
  4. ``_publish_odcs_per_expose`` puts one contract per expose
  5. OdcsProvider reads QoS block for slaProperties (FLUID 0.7.1)
  6. OdcsProvider._extract_team falls back to metadata.owner
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from fluid_build.providers.datamesh_manager.datamesh_manager import (
    DataMeshManagerProvider,
)
from fluid_build.providers.odcs.odcs import OdcsProvider

# ---------------------------------------------------------------------------
# Sample fixture: a minimal compiled FLUID 0.7.1 contract (two expose ports)
# ---------------------------------------------------------------------------

_BITCOIN_CONTRACT = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "crypto.bitcoin_prices_gcp_governed",
    "name": "Bitcoin Price Index",
    "description": "Real-time Bitcoin price data.",
    "domain": "finance",
    "metadata": {
        "layer": "Gold",
        "owner": {
            "team": "data-engineering",
            "email": "data-engineering@company.com",
        },
        "status": "active",
    },
    "tags": ["cryptocurrency", "real-time"],
    "exposes": [
        {
            "exposeId": "bitcoin_prices_table",
            "title": "Bitcoin Real-time Price Feed",
            "version": "1.0.0",
            "kind": "table",
            "lifecycle": {"state": "deprecated"},
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {
                    "project": "dust-labs-485011",
                    "dataset": "crypto_data_labeled_1",
                    "table": "bitcoin_prices",
                    "region": "EU",
                },
            },
            "qos": {
                "availability": "99.0%",
                "freshnessSLO": "PT10M",
            },
            "contract": {
                "schema": [
                    {
                        "name": "price_timestamp",
                        "type": "timestamp",
                        "required": True,
                        "description": "UTC timestamp",
                    },
                    {
                        "name": "price_usd",
                        "type": "numeric",
                        "required": True,
                        "description": "BTC price in USD",
                    },
                ]
            },
        },
        {
            "exposeId": "bitcoin_prices_table_v2",
            "title": "Bitcoin Real-time Price Feed (v2)",
            "version": "2.0.0",
            "kind": "table",
            "lifecycle": {"state": "active"},
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {
                    "project": "dust-labs-485011",
                    "dataset": "crypto_data_labeled_1",
                    "table": "bitcoin_prices_v2",
                    "region": "EU",
                },
            },
            "qos": {
                "availability": "99.5%",
                "freshnessSLO": "PT5M",
                "labels": {
                    "support_contact": "data-engineering@company.com",
                    "expiration_date": "2027-03-28",
                },
            },
            "contract": {
                "schema": [
                    {
                        "name": "price_timestamp",
                        "type": "timestamp",
                        "required": True,
                        "description": "UTC timestamp",
                    },
                    {
                        "name": "price_usd",
                        "type": "numeric",
                        "required": True,
                        "description": "BTC price in USD",
                    },
                    {
                        "name": "price_jpy",
                        "type": "numeric",
                        "required": True,
                        "description": "BTC price in JPY",
                    },
                ]
            },
        },
    ],
}

_PRODUCT_ID = "crypto.bitcoin_prices_gcp_governed"


def _make_provider(**kwargs):
    return DataMeshManagerProvider(api_key="test-key", **kwargs)


# ===========================================================================
# 1. Output port dataContractId
# ===========================================================================


class TestOutputPortDataContractId:
    def test_each_expose_gets_data_contract_id(self):
        provider = _make_provider()
        ports = provider._map_output_ports(_BITCOIN_CONTRACT, product_id=_PRODUCT_ID)
        assert len(ports) == 2
        ids = {p["dataContractId"] for p in ports}
        assert f"{_PRODUCT_ID}.bitcoin_prices_table" in ids
        assert f"{_PRODUCT_ID}.bitcoin_prices_table_v2" in ids

    def test_no_product_id_means_no_data_contract_id(self):
        provider = _make_provider()
        ports = provider._map_output_ports(_BITCOIN_CONTRACT)
        for port in ports:
            assert "dataContractId" not in port

    def test_deprecated_expose_has_deprecated_status(self):
        provider = _make_provider()
        ports = provider._map_output_ports(_BITCOIN_CONTRACT, product_id=_PRODUCT_ID)
        v1 = next(p for p in ports if p["id"] == "bitcoin_prices_table")
        assert v1["status"] == "deprecated"

    def test_active_expose_has_active_status(self):
        provider = _make_provider()
        ports = provider._map_output_ports(_BITCOIN_CONTRACT, product_id=_PRODUCT_ID)
        v2 = next(p for p in ports if p["id"] == "bitcoin_prices_table_v2")
        assert v2["status"] == "active"

    def test_to_data_product_output_ports_have_data_contract_id(self):
        provider = _make_provider()
        dp = provider._to_data_product(_BITCOIN_CONTRACT)
        for port in dp["outputPorts"]:
            assert "dataContractId" in port
            assert port["dataContractId"].startswith(_PRODUCT_ID + ".")


# ===========================================================================
# 2. Dry-run includes per-expose ODCS previews
# ===========================================================================


class TestDryRunOdcsPreviews:
    def test_dry_run_without_contract_has_no_odcs_key(self):
        provider = _make_provider()
        result = provider._publish_one(_BITCOIN_CONTRACT, dry_run=True, publish_contract=False)
        assert "odcs_contracts" not in result

    def test_dry_run_with_contract_includes_odcs_previews(self):
        provider = _make_provider()
        result = provider._publish_one(_BITCOIN_CONTRACT, dry_run=True, publish_contract=True)
        assert "odcs_contracts" in result
        previews = result["odcs_contracts"]
        assert len(previews) == 2

        expose_ids = {p["url"].rsplit(".", 1)[-1] for p in previews}
        assert "bitcoin_prices_table" in expose_ids
        assert "bitcoin_prices_table_v2" in expose_ids

    def test_dry_run_odcs_preview_has_valid_odcs_payload(self):
        provider = _make_provider()
        result = provider._publish_one(_BITCOIN_CONTRACT, dry_run=True, publish_contract=True)
        for preview in result["odcs_contracts"]:
            payload = preview["payload"]
            assert "apiVersion" in payload  # ODCS v3.1.0 field
            assert "kind" in payload
            assert payload["kind"] == "DataContract"
            assert "schema" in payload
            assert len(payload["schema"]) > 0


# ===========================================================================
# 3. _publish_odcs_per_expose happy path
# ===========================================================================


class TestPublishOdcsPerExpose:
    def _mock_resp(self, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = ""
        resp.json.return_value = {}
        return resp

    def test_publishes_one_contract_per_expose(self):
        provider = _make_provider()
        mock_resp = self._mock_resp(200)

        with patch.object(provider, "_request", return_value=mock_resp) as mock_req:
            results = provider._publish_odcs_per_expose(_BITCOIN_CONTRACT, _PRODUCT_ID)

        assert len(results) == 2
        assert all(r["success"] for r in results)
        assert mock_req.call_count == 2

        # Check PUT URLs
        urls = [c.args[1] for c in mock_req.call_args_list]
        assert any("bitcoin_prices_table_v2" in u for u in urls)
        assert any(u.endswith("bitcoin_prices_table") for u in urls)

    def test_publishes_with_odcs_json_body(self):
        provider = _make_provider()
        mock_resp = self._mock_resp(200)
        captured_bodies = []

        def fake_request(method, path, *, json_body=None):
            captured_bodies.append(json_body)
            return mock_resp

        with patch.object(provider, "_request", side_effect=fake_request):
            provider._publish_odcs_per_expose(_BITCOIN_CONTRACT, _PRODUCT_ID)

        for body in captured_bodies:
            assert body is not None
            assert "apiVersion" in body
            assert "kind" in body
            assert body["kind"] == "DataContract"
            assert "schema" in body

    def test_result_contains_entropy_data_url(self):
        provider = _make_provider()
        mock_resp = self._mock_resp(200)

        with patch.object(provider, "_request", return_value=mock_resp):
            results = provider._publish_odcs_per_expose(_BITCOIN_CONTRACT, _PRODUCT_ID)

        for r in results:
            assert "entropy-data.com" in r["url"]
            assert r["contract_id"] in r["url"]

    def test_partial_failure_still_publishes_other_exposes(self):
        from fluid_build.providers.base import ProviderError

        provider = _make_provider()
        call_count = [0]

        def flaky_request(method, path, *, json_body=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ProviderError("simulated API error")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            return resp

        with patch.object(provider, "_request", side_effect=flaky_request):
            results = provider._publish_odcs_per_expose(_BITCOIN_CONTRACT, _PRODUCT_ID)

        assert len(results) == 2
        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]
        assert len(successes) == 1
        assert len(failures) == 1
        assert "error" in failures[0]


# ===========================================================================
# 4. _publish_one wires ODCS per-expose on publish_contract=True
# ===========================================================================


class TestPublishOneOdcsIntegration:
    def _stub_provider(self, provider):
        """Patch out all HTTP calls on a provider."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = ""

        def fake_request(method, path, *, json_body=None):
            return mock_resp

        provider._request = fake_request
        provider._ensure_team = lambda fluid, team_id: None
        return provider

    def test_publish_contract_true_calls_odcs_per_expose(self):
        provider = _make_provider()
        self._stub_provider(provider)

        published_contracts = []
        original = provider._publish_odcs_per_expose

        def capturing_publish(fluid, product_id, **kwargs):
            result = original(fluid, product_id, **kwargs)
            published_contracts.extend(result)
            return result

        provider._publish_odcs_per_expose = capturing_publish

        result = provider._publish_one(_BITCOIN_CONTRACT, publish_contract=True)

        assert "odcs_contracts" in result
        assert len(result["odcs_contracts"]) == 2

    def test_publish_contract_false_skips_odcs(self):
        provider = _make_provider()
        self._stub_provider(provider)

        result = provider._publish_one(_BITCOIN_CONTRACT, publish_contract=False)

        assert "odcs_contracts" not in result


# ===========================================================================
# 5. OdcsProvider reads expose-level qos block
# ===========================================================================


class TestOdcsSlaFromQos:
    @staticmethod
    def _sla_value(sla, property_name):
        """Extract the value for a property from the slaProperties list."""
        return next((item["value"] for item in sla if item["property"] == property_name), None)

    def test_qos_availability_parsed_as_float(self):
        prov = OdcsProvider()
        # Scope to the v2 expose that has qos.availability = "99.5%"
        scoped = prov._filter_to_expose(_BITCOIN_CONTRACT, "bitcoin_prices_table_v2")
        sla = prov._extract_sla_properties(scoped)
        assert sla is not None
        # slaProperties is a list of {property, value} dicts
        assert any(item["property"] == "availability" for item in sla)
        avail = self._sla_value(sla, "availability")
        # 99.5% → 0.995
        assert abs(avail - 0.995) < 0.001

    def test_qos_freshness_slo_mapped_to_interval(self):
        prov = OdcsProvider()
        sla = prov._extract_sla_properties(_BITCOIN_CONTRACT)
        assert sla is not None
        # slaProperties is a list of {property, value} dicts
        assert any(item["property"] == "interval" for item in sla)
        interval = self._sla_value(sla, "interval")
        # First expose has PT10M, second has PT5M; first one wins
        assert interval in ("PT10M", "PT5M")

    def test_qos_labels_become_custom_properties(self):
        prov = OdcsProvider()
        # Single-expose scoped via _filter_to_expose
        scoped = prov._filter_to_expose(_BITCOIN_CONTRACT, "bitcoin_prices_table_v2")
        sla = prov._extract_sla_properties(scoped)
        assert sla is not None
        # Labels are embedded in the list as {"property": "label:key", "value": ...}
        assert any(item["property"] == "label:support_contact" for item in sla)

    def test_full_odcs_export_includes_sla(self):
        prov = OdcsProvider()
        odcs = prov.render(_BITCOIN_CONTRACT, expose_id="bitcoin_prices_table_v2")
        assert "slaProperties" in odcs
        sla = odcs["slaProperties"]
        # slaProperties is a list of {property, value} dicts
        assert any(item["property"] == "availability" for item in sla)
        assert any(item["property"] == "interval" for item in sla)


# ===========================================================================
# 6. OdcsProvider._extract_team reads metadata.owner
# ===========================================================================


class TestOdcsTeamFromMetadataOwner:
    def test_team_extracted_from_metadata_owner(self):
        prov = OdcsProvider()
        # _BITCOIN_CONTRACT has owner under metadata.owner, not top-level owner
        team = prov._extract_team(_BITCOIN_CONTRACT)
        assert team is not None
        assert team["name"] == "data-engineering"

    def test_team_prefers_top_level_owner(self):
        prov = OdcsProvider()
        fluid = dict(_BITCOIN_CONTRACT)
        fluid["owner"] = {"team": "platform-team", "email": "platform@company.com"}
        team = prov._extract_team(fluid)
        assert team["name"] == "platform-team"

    def test_full_odcs_export_includes_team(self):
        prov = OdcsProvider()
        odcs = prov.render(_BITCOIN_CONTRACT, expose_id="bitcoin_prices_table_v2")
        assert "team" in odcs
        assert odcs["team"]["name"] == "data-engineering"


# ===========================================================================
# 7. Overlay merge: staging binding.location flows into ODCS servers
# ===========================================================================


class TestOverlayMergeOdcsServers:
    """Ensure staging/env overlays that patch binding.location are reflected
    in the generated ODCS ``servers`` block after a positional list merge."""

    # A minimal FLUID contract whose exposes can be patched by a staging overlay
    _BASE = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "test.overlay_product",
        "name": "Overlay Test Product",
        "domain": "test",
        "exposes": [
            {
                "exposeId": "table_a",
                "lifecycle": {"state": "active"},
                "binding": {
                    "platform": "bigquery",
                    "type": "table",
                    "location": {
                        "project": "prod-project",
                        "dataset": "prod_dataset",
                        "table": "prices",
                        "region": "US",
                    },
                },
                "schema": [{"name": "price", "type": "FLOAT64"}],
            },
            {
                "exposeId": "table_b",
                "lifecycle": {"state": "active"},
                "binding": {
                    "platform": "bigquery",
                    "type": "table",
                    "location": {
                        "project": "prod-project",
                        "dataset": "prod_dataset",
                        "table": "prices_v2",
                        "region": "US",
                    },
                },
                "schema": [{"name": "price", "type": "FLOAT64"}],
            },
        ],
    }

    _STAGING_OVERLAY = {
        "exposes": [
            {
                "binding": {
                    "location": {
                        "project": "staging-project",
                        "dataset": "staging_dataset",
                        "region": "EU",
                    }
                }
            },
            {
                "binding": {
                    "location": {
                        "project": "staging-project",
                        "dataset": "staging_dataset",
                        "region": "EU",
                    }
                }
            },
        ]
    }

    def _apply_overlay(self, base, overlay):
        from fluid_build.loader import _deep_merge

        return _deep_merge(dict(base), overlay)

    def test_merged_exposes_preserve_expose_id(self):
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        assert merged["exposes"][0]["exposeId"] == "table_a"
        assert merged["exposes"][1]["exposeId"] == "table_b"

    def test_merged_exposes_update_location(self):
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        for e in merged["exposes"]:
            loc = e["binding"]["location"]
            assert loc["project"] == "staging-project", f"{e['exposeId']} project wrong"
            assert loc["dataset"] == "staging_dataset", f"{e['exposeId']} dataset wrong"
            assert loc["region"] == "EU"

    def test_merged_exposes_preserve_schema(self):
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        for e in merged["exposes"]:
            assert e.get("schema"), f"{e['exposeId']} lost its schema"

    def test_merged_exposes_preserve_binding_platform(self):
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        for e in merged["exposes"]:
            assert e["binding"]["platform"] == "bigquery"
            # table key is NOT in the overlay → must survive
            assert e["binding"]["location"].get("table") is not None

    def test_odcs_servers_reflect_staging_location(self):
        """After overlay merge, OdcsProvider.render must use staging project/dataset."""
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        prov = OdcsProvider()
        odcs_a = prov.render(merged, expose_id="table_a")
        servers = odcs_a.get("servers", [])
        assert servers, "No servers block in ODCS output"
        for srv in servers:
            assert (
                srv.get("project") == "staging-project"
            ), f"Expected staging-project, got {srv.get('project')}"
            assert (
                srv.get("dataset") == "staging_dataset"
            ), f"Expected staging_dataset, got {srv.get('dataset')}"

    def test_dmm_dry_run_on_staging_overlay_uses_correct_contract_ids(self):
        """After overlay merge, dry-run ODCS previews carry the right contract URL."""
        merged = self._apply_overlay(self._BASE, self._STAGING_OVERLAY)
        provider = _make_provider()
        previews = provider._preview_odcs_per_expose(merged, "test.overlay_product")
        assert len(previews) == 2
        for preview in previews:
            assert "test.overlay_product." in preview["url"]
            payload = preview["payload"]
            servers = payload.get("servers", [])
            assert servers, f"No servers in dry-run payload for {preview['url']}"
            assert servers[0].get("project") == "staging-project"
