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

"""Pinning test for the Airbyte embedded-runner loopback-host override.

PyAirbyte embedded mode runs each source connector as a Docker container, so a
contract-declared loopback host (``localhost`` / ``127.0.0.1``) is unreachable
from inside the container — it resolves to the container's own loopback, not
the host. ``_execute_embedded_mode`` must call ``apply_loopback_host_override``
— exactly as the DLT runner already does — to substitute the operator's
``FLUID_RUNNER_HOST_OVERRIDE`` before the connection config is handed to
``ab.get_source``. These tests pin that the override is applied for loopback
hosts and left alone for non-loopback hosts.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, Optional

import pytest

from fluid_build.build_runners.airbyte.runner import execute_airbyte_build

pytestmark = pytest.mark.unit


def _embedded_postgres_contract(host: str = "localhost") -> Dict[str, Any]:
    """An acquisition contract: Airbyte embedded mode, Postgres source on ``host``."""
    source = {
        "kind": "postgres",
        "connection": {
            "host": host,
            "port": 5433,
            "database": "telco_source",
            "username": "airflow",
            "password": "airflow",
        },
        "mode": "full_refresh",
        "streams": ["orders"],
    }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.airbyte_host_override_test",
        "name": "Airbyte Host Override Test",
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
                    "airbyte": {"deployment": {"mode": "embedded"}},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out.duckdb"},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def _run_capturing_get_source(contract: Dict[str, Any], tmp_path, monkeypatch) -> Dict[str, Any]:
    """Drive ``execute_airbyte_build`` in embedded mode with a stand-in PyAirbyte
    whose ``get_source`` captures (then aborts on) the connector config."""
    captured: Dict[str, Any] = {}

    def _fake_get_source(name: str, config: Optional[Dict[str, Any]] = None, **_kw: Any):
        captured["name"] = name
        captured["config"] = dict(config or {})
        # Bail before the runner tries to actually launch a connector container.
        raise RuntimeError("captured — stop before launching the connector")

    # PyAirbyte is not installed in the dev venv; inject a stand-in module so
    # ``import airbyte as ab`` inside _execute_embedded_mode resolves and
    # ``ab.get_source`` is our capturing fake.
    fake_airbyte = types.ModuleType("airbyte")
    fake_airbyte.get_source = _fake_get_source  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airbyte", fake_airbyte)

    try:
        execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    except RuntimeError:
        # Our sentinel may surface if the runner does not swallow it — fine,
        # the config was already captured before the raise.
        pass
    return captured


def test_embedded_mode_applies_loopback_host_override(tmp_path, monkeypatch) -> None:
    """A contract-declared ``localhost`` source host must be rewritten to
    FLUID_RUNNER_HOST_OVERRIDE before the config reaches the Airbyte connector."""
    monkeypatch.setenv("FLUID_RUNNER_HOST_OVERRIDE", "test.docker.host")

    captured = _run_capturing_get_source(
        _embedded_postgres_contract("localhost"), tmp_path, monkeypatch
    )

    assert "config" in captured, "ab.get_source was never reached"
    host = captured["config"].get("host")
    assert host == "test.docker.host", (
        f"loopback host was not overridden — connector config host={host!r} "
        "(expected the FLUID_RUNNER_HOST_OVERRIDE value 'test.docker.host')"
    )
    assert host != "localhost"


def test_embedded_mode_leaves_non_loopback_host_untouched(tmp_path, monkeypatch) -> None:
    """A non-loopback host is passed through unchanged even when the override
    env var is set — the substitution is loopback-only."""
    monkeypatch.setenv("FLUID_RUNNER_HOST_OVERRIDE", "test.docker.host")

    captured = _run_capturing_get_source(
        _embedded_postgres_contract("db.prod.internal"), tmp_path, monkeypatch
    )

    assert "config" in captured, "ab.get_source was never reached"
    assert captured["config"].get("host") == "db.prod.internal"
