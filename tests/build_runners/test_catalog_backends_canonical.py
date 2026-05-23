# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Cross-backend canonical contract tests.

Every catalog backend reads the same
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
and is supposed to surface the canonical FLUID classification (layer,
product type, domain) plus the rendered specs (fluid YAML, ODPS, per-
asset ODCS) wherever its target catalog natively allows. These tests
pin that invariant for the four registrar-backed plug-ins:

* DataHub → ``customProperties`` on DatasetSnapshot / DataProduct
* OpenMetadata → ``extension`` on Table
* Unity → ``properties`` on Table
* Glue → ``Parameters`` on TableInput
* Snowflake Horizon → markdown blocks inside ``comment``

If any backend silently drops a canonical field, the test fails fast
— the goal of the canonical layer is "add it once, every backend
gets it".
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars import (
    DataHubRegistrar,
    GlueCatalogRegistrar,
    OpenMetadataRegistrar,
    SnowflakeHorizonRegistrar,
    UnityCatalogRegistrar,
)


def _contract() -> Dict[str, Any]:
    return {
        "id": "bronze.canonical",
        "name": "Canonical Test",
        "description": "cross-backend canonical assertion",
        "domain": "commerce",
        "version": "1.2.3",
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {"team": "data-platform", "email": "dp@example.test"},
        },
        "tags": ["e2e"],
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "snowflake"},
                "contract": {
                    "schema": [
                        {"name": "id", "type": "STRING", "required": True},
                        {"name": "email", "type": "STRING"},
                    ],
                },
            }
        ],
    }


@pytest.fixture
def payload() -> CatalogPublicationPayload:
    return CatalogPublicationPayload.from_contract(
        _contract(), classifications={"email": ["pii", "email"]}
    )


# ---------------------------------------------------------------------------
# DataHub — Dataset snapshot + DataProduct MCP
# ---------------------------------------------------------------------------


class TestDataHubCanonicalFields:
    def test_dataset_snapshot_carries_canonical_custom_properties(
        self, payload, datahub_mock
    ):
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        snapshot = datahub_mock.entities[0]["entity"]["value"][
            "com.linkedin.metadata.snapshot.DatasetSnapshot"
        ]
        props_aspect = next(
            a["com.linkedin.dataset.DatasetProperties"]
            for a in snapshot["aspects"]
            if "com.linkedin.dataset.DatasetProperties" in a
        )
        custom = props_aspect["customProperties"]
        assert custom["fluid_layer"] == "Bronze"
        assert custom["fluid_product_type"] == "SDP"
        assert custom["fluid_domain"] == "commerce"
        assert "odcs_contract" in custom and "id: bronze.canonical.orders" in custom["odcs_contract"]

    def test_dataproduct_mcp_carries_fluid_and_odps_specs(
        self, payload, datahub_mock
    ):
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        dp_props = next(
            p["_aspect_value"]
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "dataProductProperties"
        )
        custom = dp_props["customProperties"]
        assert "fluid_contract" in custom
        assert "fluid_product_type" in custom
        assert "odps_spec" in custom


# ---------------------------------------------------------------------------
# OpenMetadata — extension field
# ---------------------------------------------------------------------------


class TestOpenMetadataCanonicalFields:
    def test_extension_carries_canonical_fields(self, payload, openmetadata_mock):
        OpenMetadataRegistrar(base_url="https://openmetadata.test").register_payload(
            payload
        )
        table = openmetadata_mock.tables[0]
        ext = table.get("extension") or {}
        assert ext.get("fluid_layer") == "Bronze"
        assert ext.get("fluid_product_type") == "SDP"
        assert ext.get("fluid_domain") == "commerce"
        assert "fluid_contract" in ext
        assert "odps_spec" in ext
        assert "odcs_contract" in ext


# ---------------------------------------------------------------------------
# Unity — properties map
# ---------------------------------------------------------------------------


class TestUnityCanonicalFields:
    def test_properties_carry_canonical_fields(self, payload, unity_mock):
        UnityCatalogRegistrar(base_url="https://databricks.test").register_payload(
            payload
        )
        table = unity_mock.tables[0]
        props = table.get("properties") or {}
        assert props.get("fluid_layer") == "Bronze"
        assert props.get("fluid_product_type") == "SDP"
        assert props.get("fluid_domain") == "commerce"
        assert "fluid_contract" in props
        assert "odps_spec" in props
        assert "odcs_contract" in props


# ---------------------------------------------------------------------------
# Glue — Parameters map
# ---------------------------------------------------------------------------


class TestGlueCanonicalFields:
    def test_parameters_carry_canonical_fields(self, payload, glue_mock):
        GlueCatalogRegistrar(
            base_url_override="https://glue.us-east-1.amazonaws.com",
            database_name="forge_bronze",
        ).register_payload(payload)
        # Glue's mock stores the whole CreateTable body, so the
        # Parameters map sits inside ``TableInput`` (matching the
        # native Glue API shape).
        table_input = glue_mock.tables[0]["TableInput"]
        params = table_input.get("Parameters") or {}
        assert params.get("fluid_layer") == "Bronze"
        assert params.get("fluid_product_type") == "SDP"
        assert params.get("fluid_domain") == "commerce"
        assert "fluid_contract" in params
        assert "odps_spec" in params
        assert "odcs_contract" in params


# ---------------------------------------------------------------------------
# Snowflake Horizon — markdown blocks inside `comment`
# ---------------------------------------------------------------------------


class TestSnowflakeHorizonCanonicalFields:
    def test_comment_embeds_canonical_fields(self, payload, snowflake_horizon_mock):
        SnowflakeHorizonRegistrar(
            account_url="https://acme.snowflakecomputing.com"
        ).register_payload(payload)
        comment = snowflake_horizon_mock.tables[0]["comment"]
        # The Snowflake backend doesn't have a free-form properties map,
        # so canonical fields get expressed as a "FLUID classification:"
        # bullet block followed by spec-named fenced YAML blocks.
        assert "fluid_layer: Bronze" in comment
        assert "fluid_product_type: SDP" in comment
        assert "fluid_domain: commerce" in comment
        assert "ODCS contract" in comment
        assert "FLUID contract" in comment
        assert "ODPS data product spec" in comment


# ---------------------------------------------------------------------------
# Capability declarations match what backends actually emit
# ---------------------------------------------------------------------------


class TestCapabilityDeclarations:
    """Backend specs declare ``capabilities``; the per-backend tests
    above prove the *behaviour*. This test class pins the *declarations*
    so that ``fluid publish --list-catalogs`` / docs / capability
    queries reflect what each backend actually does."""

    @pytest.fixture
    def backends(self):
        from fluid_build.api.catalog_backend import all_catalog_backend_specs

        return {s.name: s for s in all_catalog_backend_specs()}

    def test_datahub_full_capability_set(self, backends):
        from fluid_build.api.catalog_backend import CatalogCapability

        dh = backends["datahub"]
        assert dh.supports(CatalogCapability.DATA_PRODUCT)
        assert dh.supports(CatalogCapability.DOMAIN)
        assert dh.supports(CatalogCapability.LINEAGE)
        assert dh.supports(CatalogCapability.PER_ASSET_CONTRACT)
        assert dh.supports(CatalogCapability.PRODUCT_SPECS)
        assert dh.supports(CatalogCapability.OWNERSHIP)

    def test_openmetadata_declares_specs_and_per_asset(self, backends):
        from fluid_build.api.catalog_backend import CatalogCapability

        om = backends["openmetadata"]
        assert om.supports(CatalogCapability.CUSTOM_PROPERTIES)
        assert om.supports(CatalogCapability.PER_ASSET_CONTRACT)
        assert om.supports(CatalogCapability.PRODUCT_SPECS)

    def test_unity_and_glue_declare_attachment_capabilities(self, backends):
        """Both have a native string→string map (``properties`` /
        ``Parameters``) that can carry the canonical fields."""
        from fluid_build.api.catalog_backend import CatalogCapability

        for name in ("unity", "glue"):
            spec = backends[name]
            assert spec.supports(CatalogCapability.CUSTOM_PROPERTIES)
            assert spec.supports(CatalogCapability.PER_ASSET_CONTRACT)
            assert spec.supports(CatalogCapability.PRODUCT_SPECS)

    def test_snowflake_horizon_declares_specs_but_not_custom_props(self, backends):
        """Horizon attaches specs as markdown inside ``comment``; that
        satisfies PRODUCT_SPECS / PER_ASSET_CONTRACT but is honest
        about NOT having a free-form key-value map."""
        from fluid_build.api.catalog_backend import CatalogCapability

        sh = backends["snowflake_horizon"]
        assert sh.supports(CatalogCapability.PER_ASSET_CONTRACT)
        assert sh.supports(CatalogCapability.PRODUCT_SPECS)
        assert not sh.supports(CatalogCapability.CUSTOM_PROPERTIES)
