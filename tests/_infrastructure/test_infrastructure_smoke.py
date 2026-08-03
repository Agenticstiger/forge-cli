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

"""Smoke tests for the test infrastructure itself: the fixtures must work
before any of the engine matrices that depend on them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._infrastructure.cosign_mock import CosignMock
from tests._infrastructure.respx_fixtures import (
    AirbyteMockServer,
    DataHubMockServer,
    KafkaConnectMockServer,
    MarquezMockServer,
)

# ── CosignMock ───────────────────────────────────────────────────────────


class TestCosignMock:
    def test_unsigned_image_fails(self):
        c = CosignMock()
        r = c.verify("registry.example/img:1.0", "kms://test")
        assert not r.signed
        assert "no signature" in (r.error or "")

    def test_signed_with_correct_key_passes(self):
        c = CosignMock()
        c.sign("registry.example/img:1.0", "kms://test")
        r = c.verify("registry.example/img:1.0", "kms://test")
        assert r.signed
        assert r.error is None

    def test_signed_with_wrong_key_fails(self):
        c = CosignMock()
        c.sign("registry.example/img:1.0", "kms://test-prod")
        r = c.verify("registry.example/img:1.0", "kms://test-dev")
        assert not r.signed
        assert "different key" in (r.error or "")

    def test_slsa_required_but_missing_fails(self):
        c = CosignMock()
        c.sign("registry.example/img:1.0", "kms://test", slsa=False)
        r = c.verify("registry.example/img:1.0", "kms://test", require_slsa_provenance=True)
        assert not r.signed
        assert "SLSA" in (r.error or "")

    def test_slsa_required_and_present_passes(self):
        c = CosignMock()
        c.sign("registry.example/img:1.0", "kms://test", slsa=True)
        r = c.verify("registry.example/img:1.0", "kms://test", require_slsa_provenance=True)
        assert r.signed
        assert r.slsa_provenance_present


# ── Mock server smoke ────────────────────────────────────────────────────


class TestAirbyteMockSmoke:
    def test_create_source_round_trips(self, airbyte_mock: AirbyteMockServer):
        import httpx

        with httpx.Client(base_url="https://airbyte.test") as client:
            r = client.post(
                "/api/v1/sources/create",
                json={"workspaceId": "w1", "sourceDefinitionId": "src-faker", "name": "test"},
            )
            assert r.status_code == 200
            assert "sourceId" in r.json()
        assert "create_source" in airbyte_mock.calls

    def test_full_lifecycle(self, airbyte_mock: AirbyteMockServer):
        import httpx

        with httpx.Client(base_url="https://airbyte.test") as client:
            sr = client.post(
                "/api/v1/sources/create",
                json={"workspaceId": "w1", "sourceDefinitionId": "x", "name": "s"},
            )
            dr = client.post(
                "/api/v1/destinations/create",
                json={"workspaceId": "w1", "destinationDefinitionId": "y", "name": "d"},
            )
            cr = client.post(
                "/api/v1/connections/create",
                json={
                    "sourceId": sr.json()["sourceId"],
                    "destinationId": dr.json()["destinationId"],
                    "namespaceDefinition": "destination",
                },
            )
            sync = client.post(
                "/api/v1/connections/sync",
                json={"connectionId": cr.json()["connectionId"]},
            )
            assert sync.json()["job"]["status"] == "succeeded"


class TestKafkaConnectMockSmoke:
    def test_create_then_get_then_delete(self, kafka_connect_mock: KafkaConnectMockServer):
        import httpx

        with httpx.Client(base_url="http://kafka-connect.test:8083") as client:
            cr = client.post("/connectors", json={"name": "pg-source", "config": {"a": "b"}})
            assert cr.status_code == 201
            gr = client.get("/connectors/pg-source")
            assert gr.status_code == 200
            sr = client.get("/connectors/pg-source/status")
            assert sr.json()["connector"]["state"] == "RUNNING"
            dr = client.delete("/connectors/pg-source")
            assert dr.status_code == 204


class TestDataHubMockSmoke:
    def test_ingest_captures_entity(self, datahub_mock: DataHubMockServer):
        import httpx

        with httpx.Client(base_url="https://datahub.test") as client:
            client.post(
                "/entities?action=ingest",
                json={"entity": {"value": {"urn": "urn:li:dataset:(forge,bronze.x,PROD)"}}},
            )
        assert len(datahub_mock.entities) == 1


class TestMarquezMockSmoke:
    def test_lineage_event_captured(self, marquez_mock: MarquezMockServer):
        import httpx

        with httpx.Client(base_url="https://marquez.test:5000") as client:
            client.post(
                "/api/v1/lineage",
                json={
                    "eventType": "COMPLETE",
                    "run": {"runId": "r1"},
                    "job": {"namespace": "n", "name": "j"},
                },
            )
        assert len(marquez_mock.events) == 1
