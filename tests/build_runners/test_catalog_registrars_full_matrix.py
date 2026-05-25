# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Catalog registrars — full matrix (Slice I).

5 registrars × {register, unregister} × {success, failure, classifications,
schema details}. Each registrar drives an HTTP-mocked endpoint via respx.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.build_runners._catalog import (
    CatalogPlan,
    register_all,
    register_registrar,
)
from fluid_build.build_runners.catalog_registrars import (
    DataHubRegistrar,
    GlueCatalogRegistrar,
    OpenMetadataRegistrar,
    SnowflakeHorizonRegistrar,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _contract_with_columns(
    *, columns: List[Dict[str, Any]], platform: str = "snowflake", description: str = "Bronze test"
) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.x",
        "name": "x",
        "description": description,
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform", "email": "x@y.z"}},
        "tags": ["pii", "bronze"],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": platform,
                    "format": "snowflake_table",
                    "location": {"path": "/data/orders/"},
                },
                "contract": {
                    "schema": columns,
                    "schemaPolicy": "discover_and_freeze",
                },
            }
        ],
    }


# ── DataHub ─────────────────────────────────────────────────────────────


class TestDataHubRegistrar:
    def test_register_success(self, datahub_mock):
        """A successful registration writes:

        * a ``DatasetSnapshot`` (legacy ``/entities?action=ingest``) for
          the expose's physical asset — captured by ``mock.entities``;
        * one or more MCPs (``/aspects?action=ingestProposal``) for the
          DataProduct itself (and a Domain when the contract has one) —
          captured by ``mock.proposals``.

        The returned URN is the **DataProduct** URN, not the dataset
        URN, because a FLUID contract is fundamentally a data product
        — that's what an operator searches for in the UI. The dataset
        URN is preserved in ``result.metadata['dataset_urn']`` for
        callers that still need it.
        """
        registrar = DataHubRegistrar(base_url="https://datahub.test")
        contract = _contract_with_columns(
            columns=[
                {"name": "id", "type": "STRING", "description": "row id"},
                {"name": "email", "type": "STRING"},
            ]
        )
        result = registrar.register("bronze.x", "orders", contract, {"email": ["email"]})
        assert result.succeeded
        assert result.urn == "urn:li:dataProduct:bronze.x"
        assert "snowflake" in result.metadata["dataset_urn"]
        assert datahub_mock.entities, "DataHub mock recorded dataset snapshot"
        assert datahub_mock.proposals_for(
            "dataProduct"
        ), "DataHub mock recorded DataProduct MCP proposal"

    def test_classifications_become_glossary_terms(self, datahub_mock):
        """``classifications`` map column → list of glossary-term labels.
        Each label is emitted as a ``GlossaryTermAssociation`` record
        with a ``urn:li:glossaryTerm:<label>`` URN — that's the shape
        the real DataHub GMS accepts (live integration test pins this;
        the old assertion on a bare-string list silently passed because
        the mock never validated against the GMS schema)."""
        registrar = DataHubRegistrar(base_url="https://datahub.test")
        contract = _contract_with_columns(
            columns=[{"name": "email", "type": "STRING"}, {"name": "phone", "type": "STRING"}]
        )
        registrar.register(
            "bronze.x",
            "orders",
            contract,
            {"email": ["email"], "phone": ["phone", "pii"]},
        )
        envelope = datahub_mock.entities[0]
        snapshot = envelope["entity"]["value"]["com.linkedin.metadata.snapshot.DatasetSnapshot"]
        schema_aspect = next(
            a for a in snapshot["aspects"] if "com.linkedin.schema.SchemaMetadata" in a
        )
        fields = schema_aspect["com.linkedin.schema.SchemaMetadata"]["fields"]
        email_field = next(f for f in fields if f["fieldPath"] == "email")
        phone_field = next(f for f in fields if f["fieldPath"] == "phone")
        email_urns = {t["urn"] for t in email_field["glossaryTerms"]["terms"]}
        phone_urns = {t["urn"] for t in phone_field["glossaryTerms"]["terms"]}
        assert "urn:li:glossaryTerm:email" in email_urns
        assert "urn:li:glossaryTerm:phone" in phone_urns
        assert "urn:li:glossaryTerm:pii" in phone_urns

    def test_register_failure_on_network_error(self, monkeypatch):
        registrar = DataHubRegistrar(base_url="http://no-such-host.invalid:9")
        # No respx fixture → request will fail at httpx layer.
        result = registrar.register("bronze.x", "orders", _contract_with_columns(columns=[]), {})
        assert not result.succeeded
        assert result.error is not None

    def test_unregister_soft_deletes_dataset_and_dataproduct(self, datahub_mock):
        """``unregister`` mirrors ``register``: it soft-deletes both the
        DataProduct and its dataset asset so the UI no longer surfaces
        either. The DataHub mock's ``deletes`` lane records every
        delete URN so the test asserts on both shapes."""
        registrar = DataHubRegistrar(base_url="https://datahub.test")
        result = registrar.unregister("bronze.x", "orders")
        assert result.succeeded
        assert "urn:li:dataProduct:bronze.x" in datahub_mock.deletes
        assert any("urn:li:dataset:" in u and "bronze.x.orders" in u for u in datahub_mock.deletes)


# ── OpenMetadata ────────────────────────────────────────────────────────


class TestOpenMetadataRegistrar:
    def test_register_success(self, openmetadata_mock):
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        contract = _contract_with_columns(
            columns=[
                {"name": "id", "type": "string"},
                {"name": "amount", "type": "decimal"},
            ]
        )
        result = registrar.register("bronze.x", "orders", contract, {"id": ["primary_key"]})
        assert result.succeeded
        assert openmetadata_mock.tables
        table = openmetadata_mock.tables[0]
        assert table["name"] == "orders"
        assert table["fullyQualifiedName"] == "forge.bronze.x.orders"

    def test_columns_carry_pii_tags(self, openmetadata_mock):
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        contract = _contract_with_columns(columns=[{"name": "email", "type": "string"}])
        registrar.register("bronze.x", "orders", contract, {"email": ["email", "pii"]})
        cols = openmetadata_mock.tables[0]["columns"]
        email = next(c for c in cols if c["name"] == "email")
        tag_fqns = {t["tagFQN"] for t in email["tags"]}
        assert "PII.email" in tag_fqns
        assert "PII.pii" in tag_fqns

    def test_unregister(self, openmetadata_mock):
        registrar = OpenMetadataRegistrar(base_url="https://openmetadata.test")
        result = registrar.unregister("bronze.x", "orders")
        assert result.succeeded
        assert "bronze.x.orders" in openmetadata_mock.deletions[0]


# ── AWS Glue ────────────────────────────────────────────────────────────


class TestGlueRegistrar:
    def test_register_success(self, glue_mock):
        registrar = GlueCatalogRegistrar(
            region="us-east-1",
            database_name="forge_bronze",
            base_url_override="https://glue.us-east-1.amazonaws.com",
        )
        result = registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "id", "type": "int"}]),
            {},
        )
        assert result.succeeded
        assert glue_mock.tables
        body = glue_mock.tables[0]
        assert body["DatabaseName"] == "forge_bronze"
        assert body["TableInput"]["Name"] == "orders"

    def test_pii_classifications_become_table_parameters(self, glue_mock):
        registrar = GlueCatalogRegistrar(base_url_override="https://glue.us-east-1.amazonaws.com")
        registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "email", "type": "string"}]),
            {"email": ["email", "pii"]},
        )
        params = glue_mock.tables[0]["TableInput"]["Parameters"]
        assert "forge.pii.email" in params

    def test_unregister(self, glue_mock):
        registrar = GlueCatalogRegistrar(base_url_override="https://glue.us-east-1.amazonaws.com")
        result = registrar.unregister("bronze.x", "orders")
        assert result.succeeded
        assert glue_mock.deletions

    def test_no_boto3_in_module(self):
        """Glue registrar must not import boto3 — sticks to plain HTTP."""
        import re
        from pathlib import Path

        import fluid_build.build_runners.catalog_registrars.glue as mod

        src = Path(mod.__file__).read_text()
        # Real imports look like `import boto3` or `from boto3.X import ...`.
        for pattern in (r"^\s*import\s+boto3\b", r"^\s*from\s+boto3(\.|\s)"):
            assert (
                re.search(pattern, src, flags=re.MULTILINE) is None
            ), f"boto3 import leaked: {pattern}"


# ── Snowflake Horizon ───────────────────────────────────────────────────


class TestSnowflakeHorizonRegistrar:
    def test_register_success(self, snowflake_horizon_mock):
        registrar = SnowflakeHorizonRegistrar(
            account_url="https://acme.snowflakecomputing.com",
            database="FORGE",
            schema="BRONZE",
        )
        result = registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "id", "type": "varchar"}]),
            {},
        )
        assert result.succeeded
        assert "snowflake://FORGE.BRONZE.ORDERS" == result.urn
        body = snowflake_horizon_mock.tables[0]
        assert body["name"] == "ORDERS"

    def test_columns_uppercased(self, snowflake_horizon_mock):
        registrar = SnowflakeHorizonRegistrar(account_url="https://acme.snowflakecomputing.com")
        registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "email", "type": "varchar"}]),
            {"email": ["email"]},
        )
        col = snowflake_horizon_mock.tables[0]["columns"][0]
        assert col["name"] == "EMAIL"
        assert col["datatype"] == "VARCHAR"

    def test_classifications_become_tags(self, snowflake_horizon_mock):
        registrar = SnowflakeHorizonRegistrar(account_url="https://acme.snowflakecomputing.com")
        registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "email", "type": "varchar"}]),
            {"email": ["pii", "email"]},
        )
        col = snowflake_horizon_mock.tables[0]["columns"][0]
        assert "pii" in col["tags"]
        assert "email" in col["tags"]

    def test_unregister(self, snowflake_horizon_mock):
        registrar = SnowflakeHorizonRegistrar(account_url="https://acme.snowflakecomputing.com")
        result = registrar.unregister("bronze.x", "orders")
        assert result.succeeded


# ── Dispatcher integration via _catalog.register_all ───────────────────


class TestRegisterAllDispatcher:
    def test_register_all_routes_to_each_registered_target(
        self, datahub_mock, openmetadata_mock, monkeypatch
    ):
        register_registrar("datahub", DataHubRegistrar(base_url="https://datahub.test"))
        register_registrar(
            "openmetadata", OpenMetadataRegistrar(base_url="https://openmetadata.test")
        )
        plan = CatalogPlan(targets=["datahub", "openmetadata"])
        outcome = register_all(
            plan,
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "id", "type": "string"}]),
            {},
        )
        assert len(outcome.results) == 2
        assert all(r.succeeded for r in outcome.results), outcome.results

    def test_unknown_target_records_failure(self):
        plan = CatalogPlan(targets=["nonexistent-catalog"])
        outcome = register_all(plan, "x", "y", {"exposes": []}, {})
        assert outcome.failed
        assert "No registrar configured" in outcome.failed[0].error

    def test_register_all_empty_plan_returns_empty_outcome(self):
        outcome = register_all(CatalogPlan(targets=[]), "x", "y", {"exposes": []}, {})
        assert outcome.results == []


# ── build_registrar factory + publish-path auto-wiring ────────────────────


class TestBuildRegistrarFactory:
    """``build_registrar`` resolves built-in registrars from env config."""

    def test_unconfigured_target_returns_none(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        for var in ("FLUID_CATALOG_DATAHUB_URL", "DATAHUB_GMS_URL"):
            monkeypatch.delenv(var, raising=False)
        assert build_registrar("datahub") is None

    def test_unknown_target_returns_none(self):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        assert build_registrar("nonexistent-catalog") is None

    def test_datahub_built_from_fluid_env(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "https://dh.example")
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_TOKEN", "tok")
        reg = build_registrar("datahub")
        assert isinstance(reg, DataHubRegistrar)
        assert reg.base_url == "https://dh.example"
        assert reg.api_token == "tok"

    def test_datahub_honours_native_env(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        monkeypatch.delenv("FLUID_CATALOG_DATAHUB_URL", raising=False)
        monkeypatch.setenv("DATAHUB_GMS_URL", "https://gms.example")
        reg = build_registrar("datahub")
        assert isinstance(reg, DataHubRegistrar)
        assert reg.base_url == "https://gms.example"

    def test_glue_honours_aws_region(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        for var in ("FLUID_CATALOG_GLUE_REGION", "AWS_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
        reg = build_registrar("glue")
        assert isinstance(reg, GlueCatalogRegistrar)
        assert reg.region == "eu-west-2"

    def test_unity_honours_databricks_env(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        for var in ("FLUID_CATALOG_UNITY_URL", "FLUID_CATALOG_UNITY_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DATABRICKS_HOST", "https://dbc.example")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dbtok")
        reg = build_registrar("unity")
        assert isinstance(reg, UnityCatalogRegistrar)
        assert reg.base_url == "https://dbc.example"
        assert reg.workspace_token == "dbtok"

    def test_register_all_resolves_builtin_when_unregistered(self, datahub_mock, monkeypatch):
        """A planned target with env config dispatches end-to-end without an
        explicit register_registrar() — proves the publish-path wiring."""
        from fluid_build.build_runners import _catalog as orch
        from fluid_build.cli._acquisition_stage_ext import _ensure_builtin_registrars

        orch._REGISTRY.pop("datahub", None)
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "https://datahub.test")
        try:
            _ensure_builtin_registrars(["datahub"])
            assert isinstance(orch.get_registrar("datahub"), DataHubRegistrar)
            outcome = register_all(
                CatalogPlan(targets=["datahub"]),
                "bronze.x",
                "orders",
                _contract_with_columns(columns=[{"name": "id", "type": "string"}]),
                {},
            )
            assert all(r.succeeded for r in outcome.results), outcome.results
        finally:
            orch._REGISTRY.pop("datahub", None)

    def test_explicit_registration_wins_over_builtin(self, monkeypatch):
        """An explicitly-registered registrar is not replaced by the built-in."""
        from fluid_build.build_runners import _catalog as orch
        from fluid_build.cli._acquisition_stage_ext import _ensure_builtin_registrars

        sentinel = DataHubRegistrar(base_url="https://explicit.test")
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "https://builtin.test")
        register_registrar("datahub", sentinel)
        try:
            _ensure_builtin_registrars(["datahub"])
            assert orch.get_registrar("datahub") is sentinel
        finally:
            orch._REGISTRY.pop("datahub", None)


# ── Cross-target: every registrar produces a ``RegistrationResult`` ────


class TestProtocolConformance:
    @pytest.mark.parametrize(
        "registrar_factory",
        [
            lambda: DataHubRegistrar(base_url="https://datahub.test"),
            lambda: OpenMetadataRegistrar(base_url="https://openmetadata.test"),
            lambda: GlueCatalogRegistrar(base_url_override="https://glue.us-east-1.amazonaws.com"),
            lambda: SnowflakeHorizonRegistrar(account_url="https://acme.snowflakecomputing.com"),
        ],
    )
    def test_register_returns_registration_result(
        self,
        registrar_factory,
        datahub_mock,
        openmetadata_mock,
        glue_mock,
        snowflake_horizon_mock,
    ):
        registrar: CatalogRegistrar = registrar_factory()
        result = registrar.register(
            "bronze.x",
            "orders",
            _contract_with_columns(columns=[{"name": "id", "type": "string"}]),
            {},
        )
        assert isinstance(result, RegistrationResult)
        assert result.target == registrar.target
        assert result.succeeded, result.error
