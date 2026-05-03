# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pin the Airbyte runner's REST contract against the canonical Airbyte API.

Airbyte 1.x ships as a k8s-first deployment (``abctl install`` uses Kind);
running the full server stack in plain docker-compose hits Kubernetes
secret-initializer paths that no longer no-op for OSS Docker. Older 0.50.x
images that did support docker-compose are amd64-only — slow under Rosetta
on Apple Silicon and not a sane CI dependency.

So instead of standing up a fragile real server, this module verifies the
runner against Airbyte's published REST schema by intercepting every HTTP
call the runner makes and asserting:

  - the URL path matches what Airbyte's OpenAPI declares,
  - the JSON body has the required keys + types per Airbyte's API,
  - the runner correctly polls /jobs/get to terminal state,
  - the run record carries the records-synced count from /jobs/get.

This is a stronger guarantee than the mock used in
``scripts/airbyte_mock.py`` (which only checks the runner's happy path)
because it pins the EXACT request shape — a future refactor that drops a
required field will fail loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ── Recorded request log ──────────────────────────────────────────────


class _RecordingHttpxClient:
    """Stand-in for httpx.Client that records every POST and replays
    canonical Airbyte responses.

    Each test inspects ``self.calls`` to assert the runner sent the
    right URLs + bodies in the right order.
    """

    WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
    SOURCE_ID = "00000000-0000-0000-0000-000000000002"
    DEST_ID = "00000000-0000-0000-0000-000000000003"
    CONN_ID = "00000000-0000-0000-0000-000000000004"
    JOB_ID = 4242

    def __init__(self, base_url: str = "", **kwargs: Any) -> None:
        self.base_url = base_url
        self.calls: List[Dict[str, Any]] = []
        self._jobs_calls = 0

    def post(self, path: str, json: Dict[str, Any] | None = None, **_: Any):
        body = dict(json or {})
        self.calls.append({"method": "POST", "path": path, "body": body})
        if path.endswith("/sources/create"):
            return _resp({"sourceId": self.SOURCE_ID})
        if path.endswith("/destinations/create"):
            return _resp({"destinationId": self.DEST_ID})
        if path.endswith("/sources/discover_schema"):
            # Mirror the canonical response shape Airbyte returns for a
            # postgres source: a catalog with one stream that carries a
            # jsonSchema + supportedSyncModes. The runner needs both to
            # build a valid /connections/create body.
            return _resp(
                {
                    "catalog": {
                        "streams": [
                            {
                                "stream": {
                                    "name": "orders",
                                    "namespace": "public",
                                    "jsonSchema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "customer_id": {"type": "integer"},
                                        },
                                    },
                                    "supportedSyncModes": [
                                        "full_refresh",
                                        "incremental",
                                    ],
                                    "defaultCursorField": [],
                                    "sourceDefinedPrimaryKey": [["id"]],
                                }
                            }
                        ]
                    },
                    "jobInfo": {"succeeded": True},
                }
            )
        if path.endswith("/connections/create"):
            return _resp({"connectionId": self.CONN_ID})
        if path.endswith("/connections/sync"):
            # Realistic: returns "running"; runner must poll to discover
            # the terminal state.
            return _resp({"job": {"id": self.JOB_ID, "status": "running"}})
        if path.endswith("/jobs/get"):
            # First poll: still running. Subsequent: succeeded with 7 records.
            self._jobs_calls += 1
            if self._jobs_calls >= 2:
                return _resp(
                    {
                        "job": {"id": self.JOB_ID, "status": "succeeded"},
                        "attempts": [{"attempt": {"recordsSynced": 7}}],
                    }
                )
            return _resp(
                {
                    "job": {"id": self.JOB_ID, "status": "running"},
                    "attempts": [],
                }
            )
        return _resp({}, status_code=404)

    def close(self) -> None:
        pass


class _Resp:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


def _resp(payload: Dict[str, Any], status_code: int = 200) -> _Resp:
    return _Resp(payload, status_code)


# ── Fixture: minimal contract that drives the runner ──────────────────


def _contract_with_airbyte_props(airbyte_props: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.airbyte_contract_test",
        "name": "Airbyte Contract",
        "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@x.y"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "airbyte",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {
                            "host": "h",
                            "port": 5432,
                            "database": "d",
                            "user": "u",
                            "password": "p",
                        },
                        "mode": "full_refresh",
                        "streams": ["orders"],
                    },
                    "sink": {"format": "jsonl"},
                    "airbyte": airbyte_props,
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "local", "format": "jsonl"},
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


# ── Tests ──────────────────────────────────────────────────────────────


class TestAirbyteRestContract:
    """The runner must hit Airbyte's published REST surface in the right order."""

    def _run(self, tmp_path: Path):
        from fluid_build.build_runners.airbyte.runner import execute_airbyte_build

        contract = _contract_with_airbyte_props(
            {
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://airbyte:8001",
                    "poll_interval_seconds": 0.0,
                    "job_timeout_seconds": 5,
                },
                "source_definition_id": "decd338e-5647-4c0b-adf4-da0e75f5a750",
                "destination_definition_id": "a625d593-bba5-4a1c-a53d-2d246268a155",
                "workspace_id": _RecordingHttpxClient.WORKSPACE_ID,
            }
        )

        client = _RecordingHttpxClient()
        with patch("httpx.Client", return_value=client):
            rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        return rc, client.calls

    def test_returns_success_after_polling(self, tmp_path: Path):
        rc, calls = self._run(tmp_path)
        assert rc == 0
        # Find the run record on disk and confirm records_total propagated.
        record_files = list((tmp_path / ".fluid" / "runs").rglob("ingest/runs/*.json"))
        assert record_files, "runner should have persisted a run record"
        import json

        rec = json.loads(record_files[0].read_text())
        assert rec["state"] == "succeeded"
        assert rec["records_total"] == 7

    def test_request_sequence_matches_airbyte_api(self, tmp_path: Path):
        _, calls = self._run(tmp_path)
        paths = [c["path"] for c in calls]
        # Canonical sequence per Airbyte's OSS REST API: source +
        # destination created, then discover_schema (Airbyte 1.x
        # createConnection won't accept a hand-rolled stream catalog —
        # jsonSchema + supportedSyncModes must come from the connector),
        # then connection + sync, then poll /jobs/get to terminal.
        assert paths[0].endswith("/sources/create")
        assert paths[1].endswith("/destinations/create")
        assert paths[2].endswith("/sources/discover_schema")
        assert paths[3].endswith("/connections/create")
        assert paths[4].endswith("/connections/sync")
        assert any(p.endswith("/jobs/get") for p in paths[5:])

    def test_source_create_body_carries_required_fields(self, tmp_path: Path):
        _, calls = self._run(tmp_path)
        src = next(c for c in calls if c["path"].endswith("/sources/create"))
        body = src["body"]
        # Airbyte's POST /sources/create requires these keys.
        assert "workspaceId" in body
        assert "name" in body
        assert "sourceDefinitionId" in body
        assert "connectionConfiguration" in body
        assert isinstance(body["connectionConfiguration"], dict)
        # Connection config copied through from the contract.
        assert body["connectionConfiguration"]["host"] == "h"
        assert body["connectionConfiguration"]["database"] == "d"

    def test_connection_create_body_uses_canonical_sync_mode(self, tmp_path: Path):
        _, calls = self._run(tmp_path)
        conn = next(c for c in calls if c["path"].endswith("/connections/create"))
        sync = conn["body"]["syncCatalog"]["streams"][0]
        assert sync["stream"]["name"] == "orders"
        # The runner now passes the discovered stream through verbatim
        # (jsonSchema + supportedSyncModes intact) — that's the contract
        # Airbyte 1.x's CatalogValidator enforces.
        assert "jsonSchema" in sync["stream"]
        assert sync["config"]["selected"] is True
        assert sync["config"]["syncMode"] == "full_refresh"
        assert sync["config"]["destinationSyncMode"] == "append"

    def test_polling_consumes_jobs_get_until_terminal(self, tmp_path: Path):
        _, calls = self._run(tmp_path)
        get_calls = [c for c in calls if c["path"].endswith("/jobs/get")]
        # Mock returns "running" once then "succeeded"; runner must call
        # at least twice to observe the transition.
        assert len(get_calls) >= 2
        # Body is just the job id Airbyte expects.
        assert get_calls[0]["body"] == {"id": _RecordingHttpxClient.JOB_ID}

    def test_running_status_is_not_treated_as_failure(self, tmp_path: Path):
        """Regression test for the bug that landed in pre-fix runner: when
        ``/connections/sync`` returned ``status: running`` the runner used
        to short-circuit to RunState.FAILED. With the polling fix it must
        keep polling until terminal and report succeeded."""
        rc, calls = self._run(tmp_path)
        assert rc == 0, "runner regressed to treating /sync 'running' as failure"
        assert any(c["path"].endswith("/jobs/get") for c in calls)


# ── _build_destination_config smart-default mapper ────────────────────


class TestBuildDestinationConfig:
    """The mapper that fills /destinations/create's connectionConfiguration
    when the contract doesn't pass an explicit ``destination_config``.

    Real Airbyte rejects /destinations/create with 422 unless the body
    matches the destination connector's spec exactly. These tests pin
    the smart defaults for the three common cases (local-file /
    Postgres / S3) so a future refactor doesn't regress them.
    """

    def test_local_jsonl_uses_destination_path_under_slash_local(self):
        from fluid_build.build_runners.airbyte.runner import (
            _build_destination_config,
        )

        cfg = _build_destination_config(
            {"platform": "local", "format": "jsonl", "location": {"path": "./out.jsonl"}}
        )
        # Local JSON connector expects ``destination_path`` rooted at /local.
        assert cfg["destination_path"] == "/local/out.jsonl"

    def test_postgres_destination_maps_to_jdbc_keys(self):
        from fluid_build.build_runners.airbyte.runner import (
            _build_destination_config,
        )

        cfg = _build_destination_config(
            {
                "platform": "snowflake",  # platform irrelevant when format=postgres
                "format": "postgres",
                "location": {
                    "host": "h",
                    "port": 5432,
                    "database": "d",
                    "username": "u",
                    "password": "p",
                },
            }
        )
        assert cfg["host"] == "h"
        assert cfg["port"] == 5432
        assert cfg["database"] == "d"
        assert cfg["username"] == "u"
        assert cfg["password"] == "p"
        assert cfg["ssl_mode"] == {"mode": "disable"}
        assert cfg["tunnel_method"] == {"tunnel_method": "NO_TUNNEL"}

    def test_s3_destination_passes_location_through(self):
        from fluid_build.build_runners.airbyte.runner import (
            _build_destination_config,
        )

        cfg = _build_destination_config(
            {
                "platform": "s3",
                "format": "s3-parquet",
                "location": {
                    "s3_bucket_name": "warehouse",
                    "s3_bucket_path": "bronze/orders",
                    "s3_bucket_region": "eu-west-1",
                },
            }
        )
        assert cfg["s3_bucket_name"] == "warehouse"
        assert cfg["s3_bucket_path"] == "bronze/orders"
        assert cfg["s3_bucket_region"] == "eu-west-1"

    def test_path_relative_dot_slash_is_normalized(self):
        from fluid_build.build_runners.airbyte.runner import (
            _build_destination_config,
        )

        for raw in ("./out", "out", "/out"):
            cfg = _build_destination_config(
                {"platform": "local", "format": "jsonl", "location": {"path": raw}}
            )
            assert cfg["destination_path"] == "/local/out", raw

    def test_explicit_destination_config_overrides_mapper(self, tmp_path: Path):
        """When the contract sets ``properties.airbyte.destination_config``,
        the runner uses it raw — bypassing the mapper. Verified end-to-end
        through ``execute_airbyte_build``."""
        from unittest.mock import patch

        from fluid_build.build_runners.airbyte.runner import execute_airbyte_build

        client = _RecordingHttpxClient()
        contract = _contract_with_airbyte_props(
            {
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://airbyte:8001",
                    "poll_interval_seconds": 0.0,
                    "job_timeout_seconds": 5,
                },
                "source_definition_id": "decd338e-5647-4c0b-adf4-da0e75f5a750",
                "destination_definition_id": "a625d593-bba5-4a1c-a53d-2d246268a155",
                "workspace_id": _RecordingHttpxClient.WORKSPACE_ID,
                "destination_config": {
                    "destination_path": "/custom/path",
                    "extra_field": "passthrough",
                },
            }
        )
        with patch("httpx.Client", return_value=client):
            execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        dest = next(c for c in client.calls if c["path"].endswith("/destinations/create"))
        assert dest["body"]["connectionConfiguration"] == {
            "destination_path": "/custom/path",
            "extra_field": "passthrough",
        }
