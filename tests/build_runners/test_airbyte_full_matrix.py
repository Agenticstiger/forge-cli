# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Airbyte engine — full matrix (Slice E).

REST mode against ``airbyte_mock`` respx server, image-signature verification
via ``CosignMock``, capability declarations, mode→syncMode mapping,
destination dispatch, full failure-mode coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability
from fluid_build.build_runners.airbyte.runner import (
    KIND_TO_IMAGE,
    AirbyteRunner,
    _destination_image_for_binding,
    execute_airbyte_build,
    map_mode_to_sync_mode,
    resolve_connector_image,
)
from tests._infrastructure.cosign_mock import CosignMock

# ── Helpers ──────────────────────────────────────────────────────────────


def _base_contract(
    *,
    source: Dict[str, Any],
    airbyte_props: Dict[str, Any],
    binding: Dict[str, Any] = None,
) -> Dict[str, Any]:
    expose = {
        "exposeId": "data",
        "kind": "table",
        "binding": binding
        or {"platform": "local", "format": "parquet", "location": {"path": "out.duckdb"}},
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.airbyte_test",
        "name": "Airbyte Test",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "airbyte",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    "airbyte": airbyte_props,
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [expose],
    }


# ── Capability + classvar declarations ──────────────────────────────────


class TestAirbyteCapabilities:
    def test_class_attributes(self):
        r = AirbyteRunner()
        assert r.name == "airbyte"
        assert r.declared_modes == frozenset({"embedded", "bring-your-own", "managed"})

    def test_capabilities(self):
        r = AirbyteRunner()
        for cap in (
            RunnerCapability.FULL_REFRESH,
            RunnerCapability.INCREMENTAL_APPEND,
            RunnerCapability.INCREMENTAL_DEDUP,
            RunnerCapability.CDC,
            RunnerCapability.SCHEMA_DISCOVERY,
        ):
            assert cap in r.declared_capabilities


# ── Connector image resolution ─────────────────────────────────────────


class TestConnectorImageResolution:
    @pytest.mark.parametrize(
        "kind, expected_prefix",
        [
            ("salesforce", "airbyte/source-salesforce"),
            ("stripe", "airbyte/source-stripe"),
            ("github", "airbyte/source-github"),
            ("postgres", "airbyte/source-postgres"),
            ("mysql", "airbyte/source-mysql"),
            ("mongodb", "airbyte/source-mongodb-v2"),
            ("s3", "airbyte/source-s3"),
            ("snowflake", "airbyte/source-snowflake"),
            ("faker", "airbyte/source-faker"),
        ],
    )
    def test_known_kinds(self, kind: str, expected_prefix: str):
        assert resolve_connector_image(kind).startswith(expected_prefix)

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            resolve_connector_image("not-a-real-kind")

    def test_override_wins(self):
        assert (
            resolve_connector_image("salesforce", override="custom/image:1.0") == "custom/image:1.0"
        )


# ── Mode mapping ───────────────────────────────────────────────────────


class TestModeMapping:
    @pytest.mark.parametrize(
        "fluid_mode, airbyte_mode",
        [
            ("full_refresh", "full_refresh"),
            ("incremental_append", "incremental"),
            ("incremental_dedup", "incremental"),
            ("cdc", "incremental"),
            ("streaming", "full_refresh"),  # falls through to default
        ],
    )
    def test_mode_maps(self, fluid_mode: str, airbyte_mode: str):
        assert map_mode_to_sync_mode(fluid_mode) == airbyte_mode


# ── Destination image dispatch ─────────────────────────────────────────


class TestDestinationImage:
    @pytest.mark.parametrize(
        "binding, expected",
        [
            ({"platform": "snowflake", "format": "snowflake_table"}, "destination-snowflake"),
            ({"platform": "gcp", "format": "bigquery_table"}, "destination-bigquery"),
            ({"platform": "aws", "format": "postgres_table"}, "destination-postgres"),
            ({"platform": "local", "format": "parquet"}, "destination-jsonl"),
        ],
    )
    def test_dispatch(self, binding: Dict[str, Any], expected: str):
        assert expected in _destination_image_for_binding(binding)


# ── Image signature verification ───────────────────────────────────────


class TestImageSignatureVerification:
    def test_signed_image_proceeds(self, airbyte_mock, tmp_path: Path):
        verifier = CosignMock()
        verifier.sign("airbyte/source-faker:latest", "kms://test-key")
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {"workspace_id": "w1"},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "image_signature": {"verifier": "cosign", "publicKey": "kms://test-key"},
            },
        )
        rc = execute_airbyte_build(
            contract["builds"][0], contract, tmp_path, image_verifier=verifier, dry_run=False
        )
        assert rc == 0
        assert ("airbyte/source-faker:latest", "kms://test-key", False) in verifier.calls

    def test_unsigned_image_aborts(self, airbyte_mock, tmp_path: Path):
        verifier = CosignMock()  # nothing signed
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "image_signature": {"verifier": "cosign", "publicKey": "kms://test-key"},
            },
        )
        rc = execute_airbyte_build(
            contract["builds"][0], contract, tmp_path, image_verifier=verifier, dry_run=False
        )
        assert rc != 0

    def test_wrong_key_aborts(self, airbyte_mock, tmp_path: Path):
        verifier = CosignMock()
        verifier.sign("airbyte/source-faker:latest", "kms://prod-key")
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "image_signature": {"verifier": "cosign", "publicKey": "kms://dev-key"},
            },
        )
        rc = execute_airbyte_build(
            contract["builds"][0], contract, tmp_path, image_verifier=verifier, dry_run=False
        )
        assert rc != 0

    def test_slsa_required_but_missing_aborts(self, airbyte_mock, tmp_path: Path):
        verifier = CosignMock()
        verifier.sign("airbyte/source-faker:latest", "kms://test", slsa=False)
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "image_signature": {
                    "verifier": "cosign",
                    "publicKey": "kms://test",
                    "slsaProvenance": "required",
                },
            },
        )
        rc = execute_airbyte_build(
            contract["builds"][0], contract, tmp_path, image_verifier=verifier, dry_run=False
        )
        assert rc != 0

    def test_slsa_required_and_present_proceeds(self, airbyte_mock, tmp_path: Path):
        verifier = CosignMock()
        verifier.sign("airbyte/source-faker:latest", "kms://test", slsa=True)
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "image_signature": {
                    "verifier": "cosign",
                    "publicKey": "kms://test",
                    "slsaProvenance": "required",
                },
            },
        )
        rc = execute_airbyte_build(
            contract["builds"][0], contract, tmp_path, image_verifier=verifier, dry_run=False
        )
        assert rc == 0


# ── REST-mode end-to-end ───────────────────────────────────────────────


class TestRestMode:
    def test_full_lifecycle_creates_source_dest_connection_sync(self, airbyte_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {"count": 100},
                "mode": "full_refresh",
                "streams": ["users", "orders"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
                "workspace_id": "ws-1",
            },
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        # All four REST calls were invoked.
        for call in ("create_source", "create_destination", "create_connection", "trigger_sync"):
            assert call in airbyte_mock.calls

    def test_dry_run_does_not_call_rest(self, airbyte_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
            },
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
        assert airbyte_mock.calls == []

    def test_missing_server_url_fails(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={"deployment": {"mode": "bring-your-own"}},
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_run_record_persisted(self, airbyte_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
            },
        )
        execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs = list(
            (tmp_path / ".fluid" / "runs" / contract["id"] / "ingest" / "runs").glob("*.json")
        )
        assert len(runs) == 1
        rec = json.loads(runs[0].read_text())
        assert rec["facets"]["engine"] == "airbyte"
        assert rec["facets"]["mode"] == "rest"
        assert "image_ref" in rec["facets"]


# ── Embedded mode (PyAirbyte not installed in test venv) ──────────────


class TestEmbeddedModeFallback:
    def test_embedded_without_pyairbyte_fails_cleanly(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={"deployment": {"mode": "embedded"}},
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        # PyAirbyte is not installed in the dev venv; runner reports a typed error.
        assert rc != 0


# ── Failure modes ──────────────────────────────────────────────────────


class TestFailureModes:
    def test_unknown_kind_aborts(self, airbyte_mock, tmp_path: Path):
        contract = _base_contract(
            source={"kind": "atlantis", "connection": {}, "mode": "full_refresh", "streams": ["x"]},
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
            },
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_unknown_deployment_mode_fails(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={"deployment": {"mode": "novel-mode"}},
        )
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_missing_source_block(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"}
            },
        )
        del contract["builds"][0]["properties"]["source"]
        rc = execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── Dispatcher integration ────────────────────────────────────────────


class TestAirbyteDispatcher:
    def test_dispatches_to_airbyte(self, airbyte_mock, tmp_path: Path):
        from fluid_build.build_runners.base import (
            ACQUISITION_ENGINES,
            _execute_acquisition_build,
            is_acquisition_build,
        )

        assert "airbyte" in ACQUISITION_ENGINES
        contract = _base_contract(
            source={
                "kind": "faker",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["users"],
            },
            airbyte_props={
                "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
            },
        )
        build = contract["builds"][0]
        assert is_acquisition_build(build)
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=False, sample_rows=None)
        assert rc == 0
