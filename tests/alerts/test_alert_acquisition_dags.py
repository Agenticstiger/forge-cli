# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The acquisition DAGs ``fluid schedule-sync`` writes carry no alert callbacks: a contract with
alert routes gets a warning naming the build, never a silent drop."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluid_build.cli import _acquisition_stage_ext as ext


def _contract(alerts):
    contract = {
        "id": "sales.orders",
        "builds": [
            {
                "id": "land-orders",
                "engine": "dlt",
                "execution": {"trigger": {"type": "schedule", "schedule": "0 * * * *"}},
            }
        ],
    }
    if alerts is not None:
        contract["alerts"] = alerts
    return contract


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ext, "acquisition_builds", lambda c: c["builds"])


def test_routes_the_acquisition_dag_does_not_carry_are_named(
    builds, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    alerts = {"routes": [{"name": "ops", "kind": "slack", "connection": "slack_ops"}]}
    with caplog.at_level(logging.WARNING, logger=ext.LOG.name):
        artifacts = ext.schedule_sync_acquisition(_contract(alerts), tmp_path)
    assert [a.orchestrator for a in artifacts] == ["airflow", "cron"]
    assert any(
        "land-orders" in r.getMessage() and "carries no alerts" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("alerts", [None, {"routes": []}])
def test_no_routes_no_warning(
    builds, tmp_path: Path, caplog: pytest.LogCaptureFixture, alerts
) -> None:
    with caplog.at_level(logging.WARNING, logger=ext.LOG.name):
        ext.schedule_sync_acquisition(_contract(alerts), tmp_path)
    assert not any("carries no alerts" in r.getMessage() for r in caplog.records)
