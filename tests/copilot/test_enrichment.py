# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Post-synthesis deterministic enrichment hook."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from fluid_build.copilot.enrichment import (
    ENRICHMENT_DIRNAME,
    enrich_contract,
    enrichment_enabled,
    extract_schemas_from_contract,
    resolve_provider,
    resolve_refresh_cadence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_contract(
    *,
    cadence: str | None = "hourly",
    provider_platform: str = "snowflake",
    columns: list[dict] | None = None,
) -> dict:
    cols = (
        columns
        if columns is not None
        else [
            {"name": "order_id", "type": "BIGINT", "primary_key": True},
            {
                "name": "customer_id",
                "type": "BIGINT",
                "foreignKey": {"to": "customers", "field": "id"},
            },
            {"name": "amount", "type": "DECIMAL(10,2)", "min": 0, "max": 1000000},
            {"name": "status", "type": "VARCHAR", "enum": ["new", "shipped", "delivered"]},
            {"name": "created_at", "type": "TIMESTAMP"},
        ]
    )
    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "ecom.sales.orders",
        "name": "orders",
        "domain": "sales",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "sales-eng"},
        },
        "builds": [{"id": "ingest", "engine": "dbt"}],
        "exposes": [
            {
                "exposeId": "orders_curated",
                "kind": "table",
                "binding": {"platform": provider_platform, "format": "table"},
                "contract": {"schema": cols},
            }
        ],
    }
    if cadence:
        contract["metadata"]["refreshCadence"] = cadence
    return contract


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


def test_extract_schemas_basic():
    contract = _sample_contract()
    schemas = extract_schemas_from_contract(contract)
    assert len(schemas) == 1
    assert schemas[0]["model_name"] == "orders_curated"
    names = [c["name"] for c in schemas[0]["columns"]]
    assert names == ["order_id", "customer_id", "amount", "status", "created_at"]


def test_normalizer_recognises_pk_aliases():
    """Each accepted PK marker turns on primary_key in normalized output."""
    for marker in ("primary", "primaryKey", "primary_key", "pk", "isPrimary"):
        contract = _sample_contract(columns=[{"name": "id", "type": "BIGINT", marker: True}])
        schemas = extract_schemas_from_contract(contract)
        assert (
            schemas[0]["columns"][0].get("primary_key") is True
        ), f"PK marker {marker!r} not picked up"


def test_normalizer_recognises_fk_aliases():
    contract = _sample_contract(
        columns=[
            {
                "name": "customer_id",
                "type": "BIGINT",
                "references": {"to": "customers", "field": "id"},
            }
        ]
    )
    schemas = extract_schemas_from_contract(contract)
    fk = schemas[0]["columns"][0].get("foreign_key")
    assert fk == {"to": "customers", "field": "id"}


def test_normalizer_handles_minimal_columns():
    """FLUID's canonical schema is just {name, type}; should still work."""
    contract = _sample_contract(
        columns=[{"name": "a", "type": "int"}, {"name": "b", "type": "string"}]
    )
    schemas = extract_schemas_from_contract(contract)
    assert len(schemas[0]["columns"]) == 2
    assert all("primary_key" not in c for c in schemas[0]["columns"])


def test_normalizer_handles_empty_exposes():
    contract = {"fluidVersion": "0.7.3", "exposes": []}
    assert extract_schemas_from_contract(contract) == []


def test_normalizer_falls_back_to_models_array():
    """ODCS-style top-level models array is also accepted."""
    contract = {
        "fluidVersion": "0.7.3",
        "models": [{"name": "lookup", "columns": [{"name": "k", "type": "int", "primary": True}]}],
    }
    schemas = extract_schemas_from_contract(contract)
    assert schemas == [
        {"model_name": "lookup", "columns": [{"name": "k", "type": "int", "primary_key": True}]}
    ]


# ---------------------------------------------------------------------------
# Provider + cadence resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("snowflake", "snowflake"),
        ("bigquery", "bigquery"),
        ("gcp", "bigquery"),
        ("athena", "athena"),
        ("aws", "athena"),
        ("redshift", "redshift"),
        ("", "snowflake"),
        ("unknown-engine", "snowflake"),
    ],
)
def test_resolve_provider(platform, expected):
    contract = _sample_contract(provider_platform=platform)
    assert resolve_provider(contract) == expected


def test_resolve_refresh_cadence_metadata():
    contract = _sample_contract(cadence="daily")
    assert resolve_refresh_cadence(contract) == "daily"


def test_resolve_refresh_cadence_missing():
    contract = _sample_contract(cadence=None)
    assert resolve_refresh_cadence(contract) is None


# ---------------------------------------------------------------------------
# enrich_contract — end-to-end
# ---------------------------------------------------------------------------


def test_enrich_contract_runs_all_three_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUID_COPILOT_ENRICHMENT", raising=False)
    monkeypatch.chdir(tmp_path)
    artifacts = enrich_contract(_sample_contract(), run_id="20260527-120000-test01")
    assert artifacts is not None
    # All three tool outputs are present and non-empty for this rich contract.
    assert artifacts["provider"] == "snowflake"
    assert artifacts["refresh_cadence"] == "hourly"
    assert len(artifacts["dbt_tests"]) == 1
    assert artifacts["dbt_tests"][0]["version"] == 2
    assert artifacts["freshness"]["warn_after"]["count"] > 0
    assert len(artifacts["physical_layout"]) == 1


def test_enrich_contract_persists_artifacts(tmp_path):
    artifacts = enrich_contract(
        _sample_contract(),
        run_id="20260527-120000-pers01",
        workspace_root=tmp_path,
    )
    assert artifacts is not None
    out = tmp_path / ".fluid" / "agents" / "20260527-120000-pers01" / ENRICHMENT_DIRNAME
    assert (out / "tests.yml").exists()
    assert (out / "freshness.yml").exists()
    assert (out / "layout.json").exists()
    assert (out / "index.json").exists()
    # Index is a valid JSON pointer to the other files.
    index = json.loads((out / "index.json").read_text())
    assert index["provider"] == "snowflake"
    assert index["files"]["dbt_tests"] == "tests.yml"


def test_enrich_contract_disabled_via_env(monkeypatch):
    monkeypatch.setenv("FLUID_COPILOT_ENRICHMENT", "0")
    assert enrichment_enabled() is False
    assert enrich_contract(_sample_contract()) is None


def test_enrich_contract_handles_sparse_contract(tmp_path, monkeypatch):
    """A bare {name,type} schema + no cadence still returns a dict (artifacts may be empty)."""
    monkeypatch.chdir(tmp_path)
    sparse = _sample_contract(
        cadence=None,
        columns=[{"name": "a", "type": "int"}, {"name": "b", "type": "string"}],
    )
    artifacts = enrich_contract(sparse, run_id="20260527-120000-sp")
    assert artifacts is not None
    assert artifacts["refresh_cadence"] is None
    # Freshness empty when no cadence — but layout and tests still ran.
    assert artifacts["freshness"] == {}
    assert len(artifacts["physical_layout"]) == 1


def test_enrich_contract_fail_open_on_tool_exception(tmp_path, monkeypatch):
    """One failing tool must not poison the other tools' outputs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "fluid_build.copilot.enrichment.generate_dbt_tests",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    artifacts = enrich_contract(_sample_contract(), run_id="20260527-120000-fl01")
    assert artifacts is not None
    assert artifacts["dbt_tests"] == []  # the failing tool yielded nothing
    assert len(artifacts["physical_layout"]) == 1  # but the others still ran
    assert artifacts["freshness"]["warn_after"]["count"] > 0


def test_persist_silently_skips_when_no_run_id(monkeypatch):
    """No run_id, no env, no file ⇒ no exception, no persistence."""
    # Block the run_id resolver so it can't fall back to a generated one.
    monkeypatch.setattr(
        "fluid_build.observability.run_id.get_or_create_run_id",
        lambda **_kw: None,
    )
    monkeypatch.delenv("FLUID_RUN_ID", raising=False)
    # Should still return artifacts; persistence just no-ops.
    artifacts = enrich_contract(_sample_contract(), run_id=None)
    assert artifacts is not None


# ---------------------------------------------------------------------------
# PII pass — H6 fix (name-based pre-classifier)
# ---------------------------------------------------------------------------


def test_enrich_contract_runs_pii_classifier_and_tags_columns(tmp_path):
    """H6 fix: a contract with obvious PII columns gets columns tagged
    in place AND a per-model summary in the artifacts dict."""
    contract = _sample_contract(
        columns=[
            {"name": "order_id", "type": "BIGINT", "primary_key": True},
            {"name": "c_email", "type": "VARCHAR"},
            {"name": "phone_number", "type": "VARCHAR"},
            {"name": "amount", "type": "DECIMAL(10,2)"},
        ]
    )
    artifacts = enrich_contract(contract, run_id="20260527-120000-pii01", workspace_root=tmp_path)
    assert artifacts is not None
    # The PII summary is present and counts the matches.
    assert artifacts["pii_tags"]["totals"] == {"email": 1, "phone": 1}
    # The schema was mutated in place — c_email + phone_number tagged.
    schema = contract["exposes"][0]["contract"]["schema"]
    by_name = {c["name"]: c for c in schema}
    assert "pii-email" in by_name["c_email"]["tags"]
    assert by_name["c_email"]["sensitivity"] == "pii"
    assert by_name["c_email"]["semanticType"] == "email"
    assert "pii-phone" in by_name["phone_number"]["tags"]
    # Non-PII columns untouched.
    assert "tags" not in by_name["amount"] or "pii-" not in str(by_name["amount"].get("tags"))
    assert by_name["amount"].get("sensitivity") in (None,)


def test_enrich_contract_persists_pii_summary(tmp_path):
    """When the PII classifier matches, ``pii.json`` lands in the
    receipt dir alongside the existing artifact files."""
    contract = _sample_contract(
        columns=[
            {"name": "id", "type": "INT"},
            {"name": "email", "type": "VARCHAR"},
            {"name": "ssn", "type": "VARCHAR"},
        ]
    )
    enrich_contract(contract, run_id="20260527-120000-pii02", workspace_root=tmp_path)
    out = tmp_path / ".fluid" / "agents" / "20260527-120000-pii02" / ENRICHMENT_DIRNAME
    assert (out / "pii.json").exists()
    pii_data = json.loads((out / "pii.json").read_text())
    assert pii_data["totals"] == {"email": 1, "ssn": 1}
    # Index is updated to point at pii.json.
    index = json.loads((out / "index.json").read_text())
    assert index["files"]["pii_tags"] == "pii.json"


def test_enrich_contract_pii_kill_switch(tmp_path, monkeypatch):
    """FLUID_COPILOT_PII_CLASSIFIER=0 disables column tagging but still
    runs the other three tools."""
    monkeypatch.setenv("FLUID_COPILOT_PII_CLASSIFIER", "0")
    contract = _sample_contract(
        columns=[{"name": "email", "type": "VARCHAR"}, {"name": "id", "type": "INT"}]
    )
    artifacts = enrich_contract(contract, run_id="20260527-120000-pii03", workspace_root=tmp_path)
    assert artifacts is not None
    # PII totals empty — kill switch active.
    assert artifacts["pii_tags"]["totals"] == {}
    # Schema not mutated.
    schema = contract["exposes"][0]["contract"]["schema"]
    by_name = {c["name"]: c for c in schema}
    assert "tags" not in by_name["email"]
    # But the other tools still ran.
    assert len(artifacts["dbt_tests"]) == 1
    assert len(artifacts["physical_layout"]) == 1


def test_enrich_contract_no_pii_no_pii_json_file(tmp_path):
    """When NO PII matches, ``pii.json`` is NOT written (clean receipt dir)."""
    contract = _sample_contract(
        columns=[
            {"name": "order_id", "type": "BIGINT"},
            {"name": "amount", "type": "DECIMAL(10,2)"},
        ]
    )
    enrich_contract(contract, run_id="20260527-120000-pii04", workspace_root=tmp_path)
    out = tmp_path / ".fluid" / "agents" / "20260527-120000-pii04" / ENRICHMENT_DIRNAME
    assert not (out / "pii.json").exists()
    # Index still exists but file pointer is None.
    index = json.loads((out / "index.json").read_text())
    assert index["files"]["pii_tags"] is None


def test_enrich_contract_pii_failure_does_not_break_other_tools(tmp_path, monkeypatch):
    """If the PII classifier raises, the other tools still produce output."""
    monkeypatch.setattr(
        "fluid_build.copilot.enrichment.classify_contract_schemas",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    artifacts = enrich_contract(
        _sample_contract(), run_id="20260527-120000-pii05", workspace_root=tmp_path
    )
    assert artifacts is not None
    # PII slot defaults to empty summary on failure.
    assert artifacts["pii_tags"] == {"models": [], "totals": {}}
    # Other tools still ran.
    assert len(artifacts["dbt_tests"]) == 1
    assert len(artifacts["physical_layout"]) == 1
