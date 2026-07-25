# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""respx fixtures for HTTP-mocked external systems.

Each fixture wraps the canonical REST shape of one external service:

- Airbyte server (`/api/v1/sources`, `/destinations`, `/connections`, `/jobs`)
- Kafka Connect REST (`/connectors`, `/connectors/<name>/status`)
- DataHub GMS (`/entities`)
- OpenMetadata (`/api/v1/services`, `/tables`)
- AWS Glue Catalog (boto3 stub via responses or moto)
- Snowflake Horizon (HTTP RPC)
- Marquez (OpenLineage receiver)
- Cosign / Sigstore (synthetic key flows)

Fixtures are deterministic: every call shape is matched, no fall-through, so
tests fail loud if a runner makes an unexpected REST call.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest

try:
    import respx
except ImportError:  # pragma: no cover
    respx = None


def requires_respx(reason: str = "respx not installed") -> Any:
    return pytest.mark.skipif(respx is None, reason=reason)


@pytest.fixture
def airbyte_mock() -> Iterator["AirbyteMockServer"]:
    """In-memory Airbyte API. Tests mutate state via ``airbyte_mock.create_source(...)``;
    runner code under test sees realistic CRUD responses.
    """
    server = AirbyteMockServer()
    with respx.mock(base_url="https://airbyte.test", assert_all_called=False) as router:
        server.attach(router)
        yield server


@pytest.fixture
def kafka_connect_mock() -> Iterator["KafkaConnectMockServer"]:
    server = KafkaConnectMockServer()
    with respx.mock(base_url="http://kafka-connect.test:8083", assert_all_called=False) as router:
        server.attach(router)
        yield server


@pytest.fixture
def datahub_mock() -> Iterator["DataHubMockServer"]:
    server = DataHubMockServer()
    with respx.mock(base_url="https://datahub.test", assert_all_called=False) as router:
        server.attach(router)
        yield server


@pytest.fixture
def marquez_mock() -> Iterator["MarquezMockServer"]:
    server = MarquezMockServer()
    with respx.mock(base_url="https://marquez.test:5000", assert_all_called=False) as router:
        server.attach(router)
        yield server


@pytest.fixture
def openmetadata_mock() -> Iterator["OpenMetadataMockServer"]:
    server = OpenMetadataMockServer()
    with respx.mock(base_url="https://openmetadata.test", assert_all_called=False) as router:
        server.attach(router)
        yield server


@pytest.fixture
def glue_mock() -> Iterator["GlueMockServer"]:
    server = GlueMockServer()
    with respx.mock(
        base_url="https://glue.us-east-1.amazonaws.com", assert_all_called=False
    ) as router:
        server.attach(router)
        yield server


@pytest.fixture
def snowflake_horizon_mock() -> Iterator["SnowflakeHorizonMockServer"]:
    server = SnowflakeHorizonMockServer()
    with respx.mock(
        base_url="https://acme.snowflakecomputing.com", assert_all_called=False
    ) as router:
        server.attach(router)
        yield server


# ── Mock server implementations ─────────────────────────────────────────


class AirbyteMockServer:
    """In-memory Airbyte server. Holds sources/destinations/connections; round-trips
    the typical REST-API shapes the runner uses.
    """

    def __init__(self) -> None:
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.destinations: Dict[str, Dict[str, Any]] = {}
        self.connections: Dict[str, Dict[str, Any]] = {}
        self.jobs: List[Dict[str, Any]] = []
        self.calls: List[str] = []

    def attach(self, router: "respx.Router") -> None:
        # POST /api/v1/sources -> create source
        router.post("/api/v1/sources/create").mock(side_effect=self._create_source)
        router.post("/api/v1/sources/list").mock(side_effect=self._list_sources)
        router.post("/api/v1/sources/discover_schema").mock(side_effect=self._discover_schema)
        router.post("/api/v1/destinations/create").mock(side_effect=self._create_destination)
        router.post("/api/v1/connections/create").mock(side_effect=self._create_connection)
        router.post("/api/v1/connections/sync").mock(side_effect=self._trigger_sync)
        router.post("/api/v1/jobs/get").mock(side_effect=self._get_job)

    def _create_source(self, request: Any) -> Any:
        import httpx

        self.calls.append("create_source")
        body = json.loads(request.content)
        sid = f"src-{len(self.sources) + 1}"
        self.sources[sid] = {**body, "sourceId": sid}
        return httpx.Response(200, json=self.sources[sid])

    def _list_sources(self, request: Any) -> Any:
        import httpx

        self.calls.append("list_sources")
        return httpx.Response(200, json={"sources": list(self.sources.values())})

    def _discover_schema(self, request: Any) -> Any:
        """Return a minimal Airbyte ``catalog`` shape — a single stream
        with one column. Tests that exercise the discover→connection
        path don't care about the schema details, just that the runner
        gets a non-empty catalog with the keys it expects.
        """
        import httpx

        self.calls.append("discover_schema")
        return httpx.Response(
            200,
            json={
                "catalog": {
                    "streams": [
                        {
                            "stream": {
                                "name": "events",
                                "namespace": "public",
                                "jsonSchema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                    },
                                },
                                "supportedSyncModes": [
                                    "full_refresh",
                                    "incremental",
                                ],
                                "sourceDefinedCursor": False,
                                "defaultCursorField": ["updated_at"],
                                "sourceDefinedPrimaryKey": [["id"]],
                            }
                        }
                    ]
                },
                "catalogId": "cat-1",
                "jobInfo": {"id": "discover-job-1", "succeeded": True},
            },
        )

    def _create_destination(self, request: Any) -> Any:
        import httpx

        self.calls.append("create_destination")
        body = json.loads(request.content)
        did = f"dst-{len(self.destinations) + 1}"
        self.destinations[did] = {**body, "destinationId": did}
        return httpx.Response(200, json=self.destinations[did])

    def _create_connection(self, request: Any) -> Any:
        import httpx

        self.calls.append("create_connection")
        body = json.loads(request.content)
        cid = f"conn-{len(self.connections) + 1}"
        self.connections[cid] = {**body, "connectionId": cid, "status": "active"}
        return httpx.Response(200, json=self.connections[cid])

    def _trigger_sync(self, request: Any) -> Any:
        import httpx

        self.calls.append("trigger_sync")
        body = json.loads(request.content)
        job = {
            "job": {
                "id": len(self.jobs) + 1,
                "configType": "sync",
                "configId": body.get("connectionId", "?"),
                "status": "succeeded",
                "createdAt": 1000,
                "updatedAt": 1100,
            }
        }
        self.jobs.append(job)
        return httpx.Response(200, json=job)

    def _get_job(self, request: Any) -> Any:
        import httpx

        self.calls.append("get_job")
        body = json.loads(request.content)
        for j in self.jobs:
            if j["job"]["id"] == body.get("id"):
                return httpx.Response(200, json=j)
        return httpx.Response(404)


class KafkaConnectMockServer:
    """In-memory Kafka Connect cluster."""

    def __init__(self) -> None:
        self.connectors: Dict[str, Dict[str, Any]] = {}
        self.calls: List[str] = []

    def attach(self, router: "respx.Router") -> None:
        router.get("/connectors").mock(side_effect=self._list)
        router.post("/connectors").mock(side_effect=self._create)
        router.get(host="kafka-connect.test", port=8083, path__regex=r"^/connectors/[^/]+$").mock(
            side_effect=self._get
        )
        router.put(
            host="kafka-connect.test", port=8083, path__regex=r"^/connectors/[^/]+/config$"
        ).mock(side_effect=self._update_config)
        router.delete(
            host="kafka-connect.test", port=8083, path__regex=r"^/connectors/[^/]+$"
        ).mock(side_effect=self._delete)
        router.get(
            host="kafka-connect.test", port=8083, path__regex=r"^/connectors/[^/]+/status$"
        ).mock(side_effect=self._status)

    def _list(self, request: Any) -> Any:
        import httpx

        self.calls.append("list")
        return httpx.Response(200, json=list(self.connectors.keys()))

    def _create(self, request: Any) -> Any:
        import httpx

        self.calls.append("create")
        body = json.loads(request.content)
        name = body["name"]
        self.connectors[name] = {"name": name, "config": body.get("config", {}), "type": "source"}
        return httpx.Response(201, json=self.connectors[name])

    def _get(self, request: Any) -> Any:
        import httpx

        self.calls.append("get")
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in self.connectors:
            return httpx.Response(404)
        return httpx.Response(200, json=self.connectors[name])

    def _update_config(self, request: Any) -> Any:
        import httpx

        self.calls.append("update_config")
        # path: /connectors/<name>/config
        name = request.url.path.split("/")[-2]
        if name not in self.connectors:
            return httpx.Response(404)
        self.connectors[name]["config"] = json.loads(request.content)
        return httpx.Response(200, json=self.connectors[name]["config"])

    def _delete(self, request: Any) -> Any:
        import httpx

        self.calls.append("delete")
        name = request.url.path.rsplit("/", 1)[-1]
        self.connectors.pop(name, None)
        return httpx.Response(204)

    def _status(self, request: Any) -> Any:
        import httpx

        self.calls.append("status")
        name = request.url.path.split("/")[-2]
        if name not in self.connectors:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "name": name,
                "connector": {"state": "RUNNING", "worker_id": "test"},
                "tasks": [{"id": 0, "state": "RUNNING"}],
            },
        )


class DataHubMockServer:
    """Captures DataHub GMS entity registrations.

    Three lanes:

    - ``entities`` — legacy Snapshot envelopes posted to
      ``/entities?action=ingest`` (Dataset and similar).
    - ``proposals`` — MetadataChangeProposals posted to
      ``/aspects?action=ingestProposal`` (DataProduct, Domain, and
      anything DataHub only exposes via the MCP API).
    - ``deletes`` — soft-delete URNs posted to
      ``/entities?action=delete``.

    Tests that historically asserted ``len(entities) == N`` keep
    working — the new lanes only capture the new endpoints.
    """

    def __init__(self) -> None:
        self.entities: List[Dict[str, Any]] = []
        self.proposals: List[Dict[str, Any]] = []
        self.deletes: List[str] = []
        self.calls: List[str] = []

    def attach(self, router: "respx.Router") -> None:
        router.post("/entities?action=ingest").mock(side_effect=self._ingest)
        router.post("/aspects?action=ingestProposal").mock(side_effect=self._ingest_proposal)
        router.post("/entities?action=delete").mock(side_effect=self._delete)

    def _ingest(self, request: Any) -> Any:
        import httpx

        self.calls.append("ingest")
        body = json.loads(request.content)
        self.entities.append(body)
        return httpx.Response(200, json={"value": "ok"})

    def _ingest_proposal(self, request: Any) -> Any:
        import httpx

        self.calls.append("ingestProposal")
        body = json.loads(request.content)
        # The aspect payload is JSON-string-wrapped inside the MCP envelope
        # (DataHub's GenericAspect shape). Materialise it back to a dict so
        # tests don't have to double-parse to assert on aspect content.
        proposal = body.get("proposal") or {}
        aspect_str = (proposal.get("aspect") or {}).get("value")
        if isinstance(aspect_str, str):
            try:
                proposal = {**proposal, "_aspect_value": json.loads(aspect_str)}
            except Exception:  # noqa: BLE001 — best-effort, leave raw
                pass
        self.proposals.append(proposal)
        return httpx.Response(200, json={"value": "ok"})

    def _delete(self, request: Any) -> Any:
        import httpx

        self.calls.append("delete")
        body = json.loads(request.content)
        urn = body.get("urn")
        if urn:
            self.deletes.append(urn)
        return httpx.Response(200, json={"value": "ok"})

    # Convenience accessors for tests asserting on the new lanes.
    def proposals_for(self, entity_type: str) -> List[Dict[str, Any]]:
        """Return MCP proposals matching ``entity_type`` (``dataProduct``,
        ``domain``, …)."""
        return [p for p in self.proposals if p.get("entityType") == entity_type]


class MarquezMockServer:
    """Captures OpenLineage events."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def attach(self, router: "respx.Router") -> None:
        router.post("/api/v1/lineage").mock(side_effect=self._lineage)

    def _lineage(self, request: Any) -> Any:
        import httpx

        body = json.loads(request.content)
        self.events.append(body)
        return httpx.Response(201)


class OpenMetadataMockServer:
    """Captures OpenMetadata table registrations and ODCS contract imports.

    Models both halves of the registrar's two-step publish: the Tables API
    and the Data Contracts API that OpenMetadata grew in 1.10
    (``PUT /api/v1/dataContracts/odcs/yaml``). ``data_contracts_available``
    flips the contracts route to 404 so the pre-1.10 degradation path can be
    exercised.
    """

    def __init__(self) -> None:
        self.tables: List[Dict[str, Any]] = []
        self.deletions: List[str] = []
        self.calls: List[str] = []
        #: One entry per ODCS import: {"yaml", "entityId", "entityType", "mode", "headers"}
        self.odcs_contracts: List[Dict[str, Any]] = []
        self.data_contracts_available: bool = True

    def attach(self, router: "respx.Router") -> None:
        router.put("/api/v1/tables").mock(side_effect=self._put_table)
        router.delete(host="openmetadata.test", path__regex=r"^/api/v1/tables/name/.+$").mock(
            side_effect=self._delete_table
        )
        router.get(host="openmetadata.test", path__regex=r"^/api/v1/tables/name/.+$").mock(
            side_effect=self._get_table_by_name
        )
        router.put("/api/v1/dataContracts/odcs/yaml").mock(side_effect=self._put_odcs_contract)

    def _put_table(self, request: Any) -> Any:
        import httpx

        self.calls.append("put_table")
        body = json.loads(request.content)
        self.tables.append(body)
        return httpx.Response(200, json={**body, "id": f"om-{len(self.tables)}"})

    def _delete_table(self, request: Any) -> Any:
        import httpx

        self.calls.append("delete_table")
        name = request.url.path.rsplit("/", 1)[-1]
        self.deletions.append(name)
        return httpx.Response(200)

    def _get_table_by_name(self, request: Any) -> Any:
        """Resolve an FQN to the internal UUID the ODCS import route needs."""
        import httpx

        self.calls.append("get_table_by_name")
        fqn = request.url.path.split("/api/v1/tables/name/", 1)[-1]
        for index, table in enumerate(self.tables, start=1):
            if table.get("fullyQualifiedName") == fqn:
                return httpx.Response(200, json={"id": f"om-{index}", "fullyQualifiedName": fqn})
        return httpx.Response(404, json={"message": "table not found"})

    def _put_odcs_contract(self, request: Any) -> Any:
        import httpx

        if not self.data_contracts_available:
            # Pre-1.10 servers have no Data Contracts entity at all.
            self.calls.append("put_odcs_contract_404")
            return httpx.Response(404, json={"message": "Not Found"})
        self.calls.append("put_odcs_contract")
        params = request.url.params
        self.odcs_contracts.append(
            {
                "yaml": request.content.decode("utf-8"),
                "entityId": params.get("entityId"),
                "entityType": params.get("entityType"),
                "mode": params.get("mode"),
                "headers": dict(request.headers),
            }
        )
        return httpx.Response(200, json={"id": "contract-1"})


class GlueMockServer:
    """Captures Glue Catalog AWS service calls (X-Amz-Target dispatched)."""

    def __init__(self) -> None:
        self.tables: List[Dict[str, Any]] = []
        self.deletions: List[Dict[str, Any]] = []
        self.calls: List[str] = []

    def attach(self, router: "respx.Router") -> None:
        router.post("/").mock(side_effect=self._dispatch)

    def _dispatch(self, request: Any) -> Any:
        import httpx

        target = request.headers.get("x-amz-target", "")
        if target == "AWSGlue.CreateTable":
            self.calls.append("create_table")
            self.tables.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if target == "AWSGlue.DeleteTable":
            self.calls.append("delete_table")
            self.deletions.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"__type": "InvalidAction", "Message": target})


class SnowflakeHorizonMockServer:
    """Captures Snowflake Horizon table CRUD."""

    def __init__(self) -> None:
        self.tables: List[Dict[str, Any]] = []
        self.deletions: List[str] = []
        self.calls: List[str] = []

    def attach(self, router: "respx.Router") -> None:
        router.post(
            host="acme.snowflakecomputing.com",
            path__regex=r"^/api/v2/databases/[^/]+/schemas/[^/]+/tables$",
        ).mock(side_effect=self._post)
        router.delete(
            host="acme.snowflakecomputing.com",
            path__regex=r"^/api/v2/databases/[^/]+/schemas/[^/]+/tables/.+$",
        ).mock(side_effect=self._delete)

    def _post(self, request: Any) -> Any:
        import httpx

        self.calls.append("post_table")
        body = json.loads(request.content)
        self.tables.append(body)
        return httpx.Response(200, json={"name": body.get("name"), "createdOn": "2026-01-01"})

    def _delete(self, request: Any) -> Any:
        import httpx

        self.calls.append("delete_table")
        name = request.url.path.rsplit("/", 1)[-1]
        self.deletions.append(name)
        return httpx.Response(200)
