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

* DataHub → first-class entities (``DataContract`` per asset with
  ``rawContract``; native ``domains`` + ``globalTags`` aspects;
  ``institutionalMemory`` for spec links). Only small typed FLUID
  metadata lives in ``customProperties``.
* OpenMetadata → ``extension`` on Table
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
    def test_dataset_snapshot_carries_only_small_typed_fluid_properties(
        self, payload, datahub_mock
    ):
        """Dataset.customProperties should carry ONLY small typed FLUID
        metadata — never the multi-KB ODCS YAML blob (that's a
        first-class ``DataContract`` entity now) and never the domain
        string (that's a first-class ``Domain`` entity link).
        """
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
        # Small typed values stay (dot-notation per DataHub convention).
        assert custom["fluid.layer"] == "Bronze"
        assert custom["fluid.productType"] == "SDP"
        # YAML blob + redundant domain string must be gone.
        assert "odcs_contract" not in custom
        assert "fluid_domain" not in custom and "fluid.domain" not in custom

    def test_dataproduct_mcp_does_not_inline_yaml_blobs(self, payload, datahub_mock):
        """DataProduct.customProperties should NOT carry the source
        FLUID or ODPS YAML — those are linked via
        ``institutionalMemory`` + ``externalUrl`` instead. Only the
        small typed FLUID metadata belongs here.
        """
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        dp_props = next(
            p["_aspect_value"]
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "dataProductProperties"
        )
        custom = dp_props["customProperties"]
        assert custom["fluid.productType"] == "SDP"
        assert custom["fluid.layer"] == "Bronze"
        assert custom["fluid.version"] == "1.2.3"
        # YAML blobs + redundant domain / tag strings are gone.
        for forbidden in (
            "fluid_contract",
            "odps_spec",
            "fluid_domain",
            "fluid.domain",
            "fluid_tag.e2e",
        ):
            assert forbidden not in custom, f"{forbidden!r} should not appear in customProperties"

    def test_data_contract_entity_is_emitted_per_asset(self, payload, datahub_mock):
        """ODCS lives on a first-class DataContract entity bound to
        the dataset URN — NOT in customProperties. The UI shows it
        as a Data Contract tab on the dataset page."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        proposals = datahub_mock.proposals_for("dataContract")
        # Two proposals per asset: dataContractProperties + dataContractStatus.
        prop = next(p for p in proposals if p.get("aspectName") == "dataContractProperties")
        assert prop["entityUrn"] == "urn:li:dataContract:bronze.canonical.orders"
        body = prop["_aspect_value"]
        assert body["entity"] == (
            "urn:li:dataset:(urn:li:dataPlatform:snowflake,bronze.canonical.orders,PROD)"
        )
        assert "rawContract" in body
        assert "id: bronze.canonical.orders" in body["rawContract"]
        # Status pinned ACTIVE so the UI doesn't show "PENDING" for
        # what's actually a published contract.
        status = next(p for p in proposals if p.get("aspectName") == "dataContractStatus")
        assert status["_aspect_value"]["state"] == "ACTIVE"

    def test_global_tags_aspect_replaces_fluid_tag_map(self, payload, datahub_mock):
        """Product tags travel on the native ``globalTags`` aspect
        (auto-creating Tag entities) instead of a ``fluid_tag.<name>``
        customProperties map."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        tag_props = [
            p
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "globalTags"
        ]
        assert tag_props, "globalTags aspect must be emitted when contract has tags"
        tags = tag_props[0]["_aspect_value"]["tags"]
        assert {"tag": "urn:li:tag:e2e"} in tags

    def test_institutional_memory_links_to_spec_source(self, payload, datahub_mock):
        """When ``spec_source_base_url`` is configured, the registrar
        emits ``institutionalMemory`` links to the source FLUID + ODPS
        YAML documents instead of inlining them in customProperties.
        ``externalUrl`` on dataProductProperties points at the
        primary source-of-truth FLUID contract.
        """
        DataHubRegistrar(
            base_url="https://datahub.test",
            spec_source_base_url="https://github.com/org/repo/blob/main/products",
        ).register_payload(payload)
        memory_props = [
            p
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "institutionalMemory"
        ]
        assert memory_props, "institutionalMemory must be emitted when spec_source_base_url is set"
        elements = memory_props[0]["_aspect_value"]["elements"]
        urls = {e["url"] for e in elements}
        assert (
            "https://github.com/org/repo/blob/main/products/bronze.canonical/contract.fluid.yaml"
            in urls
        )
        assert (
            "https://github.com/org/repo/blob/main/products/bronze.canonical/spec.odps.yaml" in urls
        )
        # externalUrl on dataProductProperties is the primary "source"
        # link DataHub's UI shows in the product header.
        dp_props = next(
            p["_aspect_value"]
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "dataProductProperties"
        )
        assert (
            dp_props["externalUrl"]
            == "https://github.com/org/repo/blob/main/products/bronze.canonical/contract.fluid.yaml"
        )

    def test_dataset_institutional_memory_links_to_per_asset_odcs(self, payload, datahub_mock):
        """Dataset gets its own ``InstitutionalMemory`` link pointing
        at the per-asset ODCS YAML. This is the OSS-renderable
        workaround for DataHub OSS not exposing
        ``DataContractProperties.rawContract`` in its GraphQL schema —
        without this link, OSS operators have no clickable path from
        the dataset page to read the contract document.
        """
        DataHubRegistrar(
            base_url="https://datahub.test",
            spec_source_base_url="https://github.com/org/repo/blob/main/products",
        ).register_payload(payload)
        snapshot = datahub_mock.entities[0]["entity"]["value"][
            "com.linkedin.metadata.snapshot.DatasetSnapshot"
        ]
        memory_aspects = [
            a["com.linkedin.common.InstitutionalMemory"]
            for a in snapshot["aspects"]
            if "com.linkedin.common.InstitutionalMemory" in a
        ]
        assert memory_aspects, "dataset must carry an InstitutionalMemory link to its ODCS"
        elements = memory_aspects[0]["elements"]
        assert len(elements) == 1
        assert (
            elements[0]["url"]
            == "https://github.com/org/repo/blob/main/products/bronze.canonical/orders.odcs.yaml"
        )

    def test_dataset_no_memory_without_base_url(self, payload, datahub_mock):
        """No base URL → no Dataset institutionalMemory link."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        snapshot = datahub_mock.entities[0]["entity"]["value"][
            "com.linkedin.metadata.snapshot.DatasetSnapshot"
        ]
        memory_aspects = [
            a for a in snapshot["aspects"] if "com.linkedin.common.InstitutionalMemory" in a
        ]
        assert not memory_aspects

    def test_no_institutional_memory_without_base_url(self, payload, datahub_mock):
        """Without ``spec_source_base_url`` we must NOT emit a dangling
        link — better to skip the aspect entirely."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        memory_props = [
            p
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "institutionalMemory"
        ]
        assert not memory_props

    def test_structured_property_definitions_bootstrap(self, payload, datahub_mock):
        """``fluid.layer`` and ``fluid.productType`` definitions get
        upserted on the first publish so DataHub recognises them as
        typed, allowed-values metadata (not just opaque strings)."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        defs = [
            p
            for p in datahub_mock.proposals_for("structuredProperty")
            if p.get("aspectName") == "propertyDefinition"
        ]
        ids = {p["entityUrn"] for p in defs}
        assert "urn:li:structuredProperty:fluid.layer" in ids
        assert "urn:li:structuredProperty:fluid.productType" in ids
        # Allowed values must be exactly the closed enum we model
        # against the schema.
        layer_def = next(p["_aspect_value"] for p in defs if p["entityUrn"].endswith("fluid.layer"))
        allowed_layers = {av["value"]["string"] for av in layer_def["allowedValues"]}
        assert allowed_layers == {"Bronze", "Silver", "Gold"}
        ptype_def = next(
            p["_aspect_value"] for p in defs if p["entityUrn"].endswith("fluid.productType")
        )
        allowed_ptypes = {av["value"]["string"] for av in ptype_def["allowedValues"]}
        assert allowed_ptypes == {"SDP", "ADP", "CDP"}

    def test_structured_property_values_attached_to_data_product(self, payload, datahub_mock):
        """The DataProduct gets a ``structuredProperties`` aspect
        assigning Bronze + SDP from our test contract."""
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
        attachments = [
            p
            for p in datahub_mock.proposals_for("dataProduct")
            if p.get("aspectName") == "structuredProperties"
        ]
        assert attachments, "DataProduct should get a structuredProperties aspect"
        props = attachments[0]["_aspect_value"]["properties"]
        by_urn = {p["propertyUrn"]: p for p in props}
        assert by_urn["urn:li:structuredProperty:fluid.layer"]["values"] == [{"string": "Bronze"}]
        assert by_urn["urn:li:structuredProperty:fluid.productType"]["values"] == [
            {"string": "SDP"}
        ]

    def test_assertions_emitted_from_odcs_quality_rules(self, datahub_mock):
        """ODCS field-level rules translate into DataHub Assertion
        entities. Two authoring styles must both work:

        * ``required: true`` (the simplest authoring) — OdcsProvider
          auto-expands this into a library quality rule with
          ``metric: nullValues, mustBe: 0`` which our translator
          recognises as a NOT_NULL field assertion.
        * an explicit ``quality[]`` library rule with ``rule: unique``
          (hand-authored Bitol-style) — translates to a UNIQUE field
          assertion.
        """
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        contract_with_quality = {
            "id": "bronze.canonical",
            "name": "Canonical Test",
            "description": "with quality rules",
            "domain": "commerce",
            "version": "1.0.0",
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
                            {
                                "name": "order_id",
                                "type": "STRING",
                                "required": True,
                                # Hand-authored Bitol unique rule — exercises
                                # the explicit-quality-rule code path that
                                # OdcsProvider's expansion doesn't cover.
                                "quality": [
                                    {
                                        "type": "library",
                                        "rule": "unique",
                                        "dimension": "uniqueness",
                                        "description": "order_id must be unique",
                                    }
                                ],
                            },
                            {"name": "amount", "type": "NUMBER", "required": True},
                        ]
                    },
                }
            ],
        }
        payload2 = CatalogPublicationPayload.from_contract(contract_with_quality, {})
        DataHubRegistrar(base_url="https://datahub.test").register_payload(payload2)

        # Three Assertion MCPs: order_id NOT_NULL + order_id UNIQUE + amount NOT_NULL
        assertion_props = [
            p
            for p in datahub_mock.proposals_for("assertion")
            if p.get("aspectName") == "assertionInfo"
        ]
        assert len(assertion_props) == 3
        urns = {p["entityUrn"] for p in assertion_props}
        # Every URN is deterministic and includes the kind so re-runs upsert.
        assert any("not_null.order_id" in u for u in urns)
        assert any("unique.order_id" in u for u in urns)
        assert any("not_null.amount" in u for u in urns)

        # Each assertionInfo body targets the right dataset.
        for prop in assertion_props:
            info = prop["_aspect_value"]
            assert info["type"] == "FIELD"
            assert info["fieldAssertion"]["entity"] == (
                "urn:li:dataset:(urn:li:dataPlatform:snowflake,bronze.canonical.orders,PROD)"
            )

        # DataContract.dataQuality lists every assertion URN.
        dc_props = next(
            p["_aspect_value"]
            for p in datahub_mock.proposals_for("dataContract")
            if p.get("aspectName") == "dataContractProperties"
        )
        bundled = {entry["assertion"] for entry in dc_props["dataQuality"]}
        assert bundled == urns

    def test_structured_property_bootstrap_failure_falls_back(self, payload):
        """If the server doesn't support structuredProperty (older
        DataHub), the registrar should mark the capability unsupported,
        keep publishing through customProperties, and not poison the
        whole publish over the missing feature."""
        import json

        import httpx
        import respx

        from fluid_build.build_runners.catalog_registrars._datahub_structured_properties import (  # noqa: F401
            structured_property_urn,
        )

        with respx.mock(assert_all_called=False) as mock:
            # Reject structuredProperty MCPs (simulating an older server).
            def _ingest(request):
                body = json.loads(request.content)
                entity_urn = body.get("proposal", {}).get("entityUrn", "")
                if entity_urn.startswith("urn:li:structuredProperty:"):
                    return httpx.Response(400, text="entity type not registered")
                return httpx.Response(200, json={"urn": entity_urn})

            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                side_effect=_ingest
            )
            mock.post("https://datahub.test/entities?action=ingest").mock(
                return_value=httpx.Response(200, json={})
            )
            result = DataHubRegistrar(base_url="https://datahub.test").register_payload(payload)
            # Overall publish still succeeded — capability detection
            # converted the rejection into a graceful fallback.
            assert result.succeeded is True

            # No structuredProperty assignment MCPs should fire on
            # subsequent entities (cache says "unsupported").
            sp_attaches = [
                c
                for c in mock.calls
                if c.request.url.path == "/aspects?action=ingestProposal"
                and '"aspectName": "structuredProperties"' in c.request.content.decode("utf-8")
            ]
            assert (
                sp_attaches == []
            ), "structuredProperties attach must not fire when bootstrap failed"


# ---------------------------------------------------------------------------
# OpenMetadata — extension field
# ---------------------------------------------------------------------------


class TestOpenMetadataCanonicalFields:
    def test_extension_carries_canonical_fields(self, payload, openmetadata_mock):
        OpenMetadataRegistrar(base_url="https://openmetadata.test").register_payload(payload)
        table = openmetadata_mock.tables[0]
        ext = table.get("extension") or {}
        assert ext.get("fluid_layer") == "Bronze"
        assert ext.get("fluid_product_type") == "SDP"
        assert ext.get("fluid_domain") == "commerce"
        assert "fluid_contract" in ext
        assert "odps_spec" in ext
        assert "odcs_contract" in ext


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

    def test_glue_declares_attachment_capabilities(self, backends):
        """Glue's native string→string ``Parameters`` map carries the
        canonical fields."""
        from fluid_build.api.catalog_backend import CatalogCapability

        spec = backends["glue"]
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
