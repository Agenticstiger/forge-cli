# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Acquisition-pattern extensions for the 11-stage pipeline.

Covers ``policy-apply``, ``verify``, ``publish``, ``schedule-sync``
hooks for ``pattern: acquisition`` builds. Each test exercises one
entry point against a realistic contract + working directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli._acquisition_stage_ext import (
    PublishResult,
    ScheduleArtifact,
    VerifyCheck,
    VerifyResult,
    acquisition_builds,
    is_acquisition_contract,
    latest_run_record,
    policy_apply_acquisition,
    publish_acquisition,
    schedule_sync_acquisition,
    verify_acquisition,
)


def _base_contract(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.orders",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@x.co"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "execution": {
                    "trigger": {"type": "scheduled", "schedule": "0 */4 * * *"},
                    "retry": {"count": 5},
                },
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": "s3://b/x"},
                        "mode": "full_refresh",
                    },
                    "sink": {"format": "parquet"},
                    "delivery": {"dlq": {"maxRecordsBeforeAbort": 50}},
                    "cost": {"budget": {"monthly": {"rows": 1_000_000}}},
                    "catalog": {"register": ["datahub"]},
                },
                "outputs": ["orders_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders_raw",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "/tmp/x.parquet"},
                },
            }
        ],
        "retention": {"runState": "P30D", "runLogs": "P90D"},
        "observability": {
            "alert": {
                "channels": [{"kind": "file", "path": "/tmp/alerts.ndjson"}],
            }
        },
    }
    base.update(overrides)
    return base


def _seed_run_record(
    workdir: Path,
    product_id: str,
    build_id: str,
    *,
    state: str = "SUCCEEDED",
    records_total: int = 100,
    dlq_records: int = 0,
    classifications: Dict[str, Any] = None,
) -> Path:
    runs_dir = workdir / ".fluid" / "runs" / product_id / build_id / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "run_id": "01HXX000000000000000000000",
        "state": state,
        "records_total": records_total,
        "dlq_records": dlq_records,
        "facets": {"engine": "duckdb"},
    }
    if classifications:
        rec["facets"]["classifications"] = classifications
    p = runs_dir / "001.json"
    p.write_text(json.dumps(rec))
    return p


# ── Helpers ───────────────────────────────────────────────────────────────


class TestHelpers:
    def test_acquisition_builds_filters(self):
        contract = _base_contract()
        contract["builds"].append({"id": "transform", "pattern": "embedded-logic"})
        out = acquisition_builds(contract)
        assert len(out) == 1
        assert out[0]["id"] == "ingest"

    def test_is_acquisition_contract(self):
        assert is_acquisition_contract(_base_contract()) is True
        assert (
            is_acquisition_contract({"builds": [{"id": "t", "pattern": "embedded-logic"}]}) is False
        )

    def test_latest_run_record_missing_returns_none(self, tmp_path: Path):
        assert latest_run_record(tmp_path, "x", "y") is None

    def test_latest_run_record_returns_newest(self, tmp_path: Path):
        _seed_run_record(tmp_path, "p", "b", records_total=50)
        rec = latest_run_record(tmp_path, "p", "b")
        assert rec is not None
        assert rec["records_total"] == 50


# ── Stage: verify ────────────────────────────────────────────────────────


class TestVerifyAcquisition:
    def test_no_run_record_fails_check(self, tmp_path: Path):
        contract = _base_contract()
        results = verify_acquisition(contract, tmp_path)
        assert len(results) == 1
        assert results[0].all_passed is False
        names = [c.name for c in results[0].checks]
        assert "run_record_present" in names

    def test_happy_path_passes_all_checks(self, tmp_path: Path):
        contract = _base_contract()
        _seed_run_record(tmp_path, "bronze.orders", "ingest", records_total=100)
        results = verify_acquisition(contract, tmp_path)
        assert len(results) == 1
        assert results[0].all_passed is True
        names = {c.name for c in results[0].checks}
        assert {"run_state_succeeded", "records_landed", "no_unexpected_dlq_overflow"} <= names

    def test_zero_records_fails_records_landed(self, tmp_path: Path):
        contract = _base_contract()
        _seed_run_record(tmp_path, "bronze.orders", "ingest", records_total=0)
        results = verify_acquisition(contract, tmp_path)
        check = next(c for c in results[0].checks if c.name == "records_landed")
        assert check.passed is False

    def test_failed_state_flagged(self, tmp_path: Path):
        contract = _base_contract()
        _seed_run_record(tmp_path, "bronze.orders", "ingest", state="FAILED")
        results = verify_acquisition(contract, tmp_path)
        check = next(c for c in results[0].checks if c.name == "run_state_succeeded")
        assert check.passed is False

    def test_dlq_overflow_flagged(self, tmp_path: Path):
        contract = _base_contract()
        # build's maxRecordsBeforeAbort=50 in fixture
        _seed_run_record(tmp_path, "bronze.orders", "ingest", dlq_records=999)
        results = verify_acquisition(contract, tmp_path)
        check = next(c for c in results[0].checks if c.name == "no_unexpected_dlq_overflow")
        assert check.passed is False
        assert "999" in check.detail

    def test_cost_budget_check_when_under(self, tmp_path: Path):
        contract = _base_contract()
        _seed_run_record(tmp_path, "bronze.orders", "ingest", records_total=500)
        results = verify_acquisition(contract, tmp_path)
        check = next((c for c in results[0].checks if c.name == "cost_within_budget"), None)
        assert check is not None
        assert check.passed is True

    def test_cost_budget_check_when_over(self, tmp_path: Path):
        contract = _base_contract()
        # Cost budget is 1M rows; we report 5M used.
        _seed_run_record(tmp_path, "bronze.orders", "ingest", records_total=5_000_000)
        results = verify_acquisition(contract, tmp_path)
        check = next((c for c in results[0].checks if c.name == "cost_within_budget"), None)
        assert check is not None
        assert check.passed is False

    def test_to_dict_serializable(self, tmp_path: Path):
        _seed_run_record(tmp_path, "bronze.orders", "ingest")
        results = verify_acquisition(_base_contract(), tmp_path)
        d = results[0].to_dict()
        # Round-trips through JSON without error.
        assert json.loads(json.dumps(d))["all_passed"] is True


# ── Stage: publish ────────────────────────────────────────────────────────


class TestPublishAcquisition:
    def test_publish_dispatches_to_registrars(self, tmp_path: Path):
        from fluid_build.api.catalog import RegistrationResult
        from fluid_build.build_runners import _catalog as orch

        # Inject a fake registrar so publish can wire end-to-end.
        registered: list = []

        class _FakeRegistrar:
            target = "datahub"

            def register(self, product_id, expose_id, contract, classifications):
                registered.append((product_id, expose_id, dict(classifications)))
                return RegistrationResult(
                    target="datahub",
                    urn=f"datahub://{product_id}/{expose_id}",
                    succeeded=True,
                )

            def unregister(self, product_id, expose_id):
                return RegistrationResult(target="datahub", urn="", succeeded=True)

        orch.register_registrar("datahub", _FakeRegistrar())
        try:
            _seed_run_record(
                tmp_path,
                "bronze.orders",
                "ingest",
                classifications={"email": ["pii", "email"]},
            )
            results = publish_acquisition(_base_contract(), tmp_path)
            assert len(results) == 1
            assert results[0].succeeded is True
            assert results[0].target == "datahub"
            # Classifications were forwarded.
            assert registered[0][2] == {"email": ["pii", "email"]}
        finally:
            orch._REGISTRY.pop("datahub", None)

    def test_publish_no_targets_yields_no_results(self, tmp_path: Path):
        contract = _base_contract()
        contract["builds"][0]["properties"]["catalog"] = {}
        results = publish_acquisition(contract, tmp_path)
        assert results == []

    def test_publish_missing_registrar_records_failure(self, tmp_path: Path, monkeypatch):
        # No registrar pre-registered for "datahub" AND no built-in env
        # config — the orchestrator records the failure.
        from fluid_build.build_runners import _catalog as orch

        for var in ("FLUID_CATALOG_DATAHUB_URL", "DATAHUB_GMS_URL"):
            monkeypatch.delenv(var, raising=False)
        orch._REGISTRY.pop("datahub", None)
        results = publish_acquisition(_base_contract(), tmp_path)
        assert len(results) == 1
        assert results[0].succeeded is False
        assert "No registrar" in (results[0].error or "")

    def test_publish_auto_wires_builtin_registrar_from_env(self, tmp_path: Path, monkeypatch):
        # A declared target with env config needs no explicit
        # register_registrar() — publish_acquisition wires the built-in.
        from fluid_build.build_runners import _catalog as orch
        from fluid_build.build_runners.catalog_registrars import DataHubRegistrar

        orch._REGISTRY.pop("datahub", None)
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "https://datahub.test")
        try:
            publish_acquisition(_base_contract(), tmp_path)
            registrar = orch.get_registrar("datahub")
            assert isinstance(registrar, DataHubRegistrar)
            assert registrar.base_url == "https://datahub.test"
        finally:
            orch._REGISTRY.pop("datahub", None)


# ── Stage: schedule-sync ─────────────────────────────────────────────────


class TestScheduleSyncAcquisition:
    def test_default_emits_airflow_and_cron(self, tmp_path: Path):
        artifacts = schedule_sync_acquisition(_base_contract(), tmp_path)
        kinds = sorted(a.orchestrator for a in artifacts)
        assert kinds == ["airflow", "cron"]
        # Airflow DAG content includes the cron schedule + apply command.
        airflow = next(a for a in artifacts if a.orchestrator == "airflow")
        body = Path(airflow.artifact_path).read_text()
        assert '"0 */4 * * *"' in body
        assert "BashOperator" in body
        assert "fluid apply contracts/bronze.orders.fluid.yaml --build ingest" in body

    def test_dagster_and_prefect_emitted_when_requested(self, tmp_path: Path):
        artifacts = schedule_sync_acquisition(
            _base_contract(),
            tmp_path,
            orchestrators=["dagster", "prefect"],
        )
        kinds = sorted(a.orchestrator for a in artifacts)
        assert kinds == ["dagster", "prefect"]
        dagster = next(a for a in artifacts if a.orchestrator == "dagster")
        body = Path(dagster.artifact_path).read_text()
        assert "ScheduleDefinition" in body
        assert '"0 */4 * * *"' in body

        prefect = next(a for a in artifacts if a.orchestrator == "prefect")
        body = Path(prefect.artifact_path).read_text()
        assert "CronSchedule" in body

    def test_no_schedule_means_no_artifacts(self, tmp_path: Path):
        contract = _base_contract()
        contract["builds"][0]["execution"]["trigger"] = {"type": "manual"}
        artifacts = schedule_sync_acquisition(contract, tmp_path)
        assert artifacts == []

    def test_dag_id_is_python_safe(self, tmp_path: Path):
        contract = _base_contract()
        # Force an id with dots + dashes — they should be normalized.
        contract["id"] = "bronze.crm-salesforce-accounts"
        artifacts = schedule_sync_acquisition(contract, tmp_path, orchestrators=["airflow"])
        body = Path(artifacts[0].artifact_path).read_text()
        # The dag_id has no dots or dashes.
        assert 'dag_id="fluid_bronze_crm_salesforce_accounts_ingest"' in body

    def test_retry_count_propagates(self, tmp_path: Path):
        artifacts = schedule_sync_acquisition(_base_contract(), tmp_path, orchestrators=["airflow"])
        body = Path(artifacts[0].artifact_path).read_text()
        # Build's execution.retry.count=5 in fixture.
        assert '"retries": 5' in body

    def test_cron_artifact_has_canonical_format(self, tmp_path: Path):
        artifacts = schedule_sync_acquisition(_base_contract(), tmp_path, orchestrators=["cron"])
        body = Path(artifacts[0].artifact_path).read_text()
        assert body.strip().endswith(
            "0 */4 * * * fluid apply contracts/bronze.orders.fluid.yaml --build ingest"
        )


# ── Stage: policy-apply ──────────────────────────────────────────────────


class TestPolicyApplyAcquisition:
    def test_writes_retention_and_alert_files(self, tmp_path: Path):
        results = policy_apply_acquisition(_base_contract(), tmp_path)
        assert len(results) == 1
        applied = set(results[0].actions_applied)
        assert {"retention", "alert_channels", "cost_budget"} <= applied
        # Files materialized under .fluid/policies/<id>/.
        retention_file = tmp_path / ".fluid" / "policies" / "bronze.orders" / "retention.json"
        assert retention_file.exists()
        assert json.loads(retention_file.read_text())["runState"] == "P30D"

        alert_file = tmp_path / ".fluid" / "policies" / "bronze.orders" / "alert_channels.json"
        assert alert_file.exists()
        assert json.loads(alert_file.read_text())["channels"][0]["kind"] == "file"

        cost_file = tmp_path / ".fluid" / "policies" / "bronze.orders" / "ingest_cost_budget.json"
        assert cost_file.exists()

    def test_pii_masking_applied_when_classifications_known(self, tmp_path: Path):
        _seed_run_record(
            tmp_path,
            "bronze.orders",
            "ingest",
            classifications={"email": ["pii", "email"], "ssn": ["pii", "ssn"]},
        )
        results = policy_apply_acquisition(_base_contract(), tmp_path)
        applied = results[0].actions_applied
        # Two columns classified.
        assert any(a == "pii_masking:2_columns" for a in applied)
        masking_file = (
            tmp_path / ".fluid" / "policies" / "bronze.orders" / "ingest_pii_masking.json"
        )
        assert masking_file.exists()
        body = json.loads(masking_file.read_text())
        assert "email" in body["masking"]
        assert "ssn" in body["masking"]

    def test_no_classifications_skips_masking(self, tmp_path: Path):
        results = policy_apply_acquisition(_base_contract(), tmp_path)
        skipped = results[0].skipped
        assert any("pii_masking" in s for s in skipped)
