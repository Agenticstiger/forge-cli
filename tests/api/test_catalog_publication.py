# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Canonical publication payload — unit tests.

Pins the shape :class:`CatalogPublicationPayload.from_contract` builds
from a FLUID contract. The payload is the contract between the
orchestrator and every backend translator; if its shape drifts, every
catalog backend silently loses data. These tests are the
fast-feedback guard for that drift.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.api.catalog_publication import (
    CatalogPublicationPayload,
    ColumnPayload,
    LineageEdge,
    OwnerPayload,
)


def _contract(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "bronze.orders",
        "name": "Raw Orders",
        "description": "Source-aligned orders",
        "domain": "commerce",
        "version": "1.0.0",
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {"team": "data-platform", "email": "dp@example.test"},
        },
        "tags": ["pii", "bronze"],
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "snowflake", "location": {"path": "/d/"}},
                "contract": {
                    "schema": [
                        {"name": "id", "type": "STRING", "required": True},
                        {
                            "name": "email",
                            "type": "STRING",
                            "description": "customer email",
                        },
                    ]
                },
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Product mapping
# ---------------------------------------------------------------------------


class TestProductPayload:
    """Top-level product fields project from the FLUID dict cleanly."""

    def test_core_fields(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        p = payload.product
        assert p.product_id == "bronze.orders"
        assert p.name == "Raw Orders"
        assert p.description == "Source-aligned orders"
        assert p.domain == "commerce"
        assert p.version == "1.0.0"
        assert p.layer == "Bronze"
        assert p.product_type == "SDP"
        assert p.tags == ("pii", "bronze")

    def test_owner_normalised(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        assert payload.product.owner == OwnerPayload(team="data-platform", email="dp@example.test")

    def test_missing_owner_yields_none(self):
        contract = _contract()
        contract["metadata"].pop("owner")
        payload = CatalogPublicationPayload.from_contract(contract)
        assert payload.product.owner is None

    def test_domain_stripped(self):
        """``domain`` may be authored with stray whitespace — the
        canonical layer must normalise so translators don't each
        re-implement the same .strip()."""
        contract = _contract(domain="  commerce  ")
        payload = CatalogPublicationPayload.from_contract(contract)
        assert payload.product.domain == "commerce"


# ---------------------------------------------------------------------------
# Asset mapping
# ---------------------------------------------------------------------------


class TestAssetPayload:
    """Every expose becomes exactly one ``AssetPayload`` with schema
    derived from ``contract.exposes[].contract.schema`` and platform
    from the binding."""

    def test_one_asset_per_expose(self):
        contract = _contract()
        contract["exposes"].append(
            {
                "exposeId": "orders_archive",
                "binding": {"platform": "s3"},
                "contract": {"schema": []},
            }
        )
        payload = CatalogPublicationPayload.from_contract(contract)
        assert tuple(a.asset_id for a in payload.assets) == (
            "orders",
            "orders_archive",
        )
        assert payload.assets[1].platform == "s3"

    def test_exposeId_takes_precedence_over_name_id_aliases(self):
        """v0.7.3 ``exposeId`` is the canonical name. Older shapes
        (``name``, ``id``) are accepted as fallbacks for migration but
        ``exposeId`` always wins when present."""
        contract = _contract()
        contract["exposes"][0] = {
            **contract["exposes"][0],
            "exposeId": "orders",
            "name": "should_not_win",
            "id": "should_not_win_either",
        }
        payload = CatalogPublicationPayload.from_contract(contract)
        assert payload.assets[0].asset_id == "orders"

    def test_column_attributes_preserved(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        cols = payload.assets[0].schema
        assert cols == (
            ColumnPayload(name="id", native_type="STRING", required=True),
            ColumnPayload(
                name="email",
                native_type="STRING",
                description="customer email",
            ),
        )

    def test_per_asset_odcs_yaml_rendered(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        odcs = payload.assets[0].odcs_yaml
        assert odcs is not None
        # ODCS top-level fields pinned so any future re-formatter
        # change has to update this assertion deliberately.
        assert "apiVersion: v3.1.0" in odcs
        assert "id: bronze.orders.orders" in odcs


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


class TestLineage:
    def test_consumes_become_canonical_lineage_edges(self):
        contract = _contract(
            consumes=[
                {"productId": "bronze.raw", "exposeId": "src"},
                {"productId": "silver.lkp", "exposeId": "ref", "platform": "bigquery"},
            ]
        )
        payload = CatalogPublicationPayload.from_contract(contract)
        edges = payload.assets[0].upstreams
        assert edges == (
            LineageEdge(
                upstream_product_id="bronze.raw",
                upstream_expose_id="src",
                upstream_platform=None,
                transformation_type="TRANSFORMED",
            ),
            LineageEdge(
                upstream_product_id="silver.lkp",
                upstream_expose_id="ref",
                upstream_platform="bigquery",
                transformation_type="TRANSFORMED",
            ),
        )

    def test_no_consumes_yields_empty_upstreams(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        assert payload.assets[0].upstreams == ()

    def test_malformed_consume_ref_skipped(self):
        """A consume entry missing productId/exposeId must not crash
        the builder — defensive against partial / migrating contracts."""
        contract = _contract(
            consumes=[
                {"productId": "ok.foo", "exposeId": "fine"},
                {"productId": "only_product_id"},  # missing exposeId
                {"exposeId": "only_expose_id"},  # missing productId
                "not_even_a_dict",  # malformed
            ]
        )
        payload = CatalogPublicationPayload.from_contract(contract)
        edges = payload.assets[0].upstreams
        assert len(edges) == 1
        assert edges[0].upstream_product_id == "ok.foo"


# ---------------------------------------------------------------------------
# Specs (fluid + ODPS rendered at build time)
# ---------------------------------------------------------------------------


class TestSpecBundle:
    def test_fluid_yaml_round_trips_contract(self):
        import yaml

        payload = CatalogPublicationPayload.from_contract(_contract())
        assert payload.specs.fluid_yaml is not None
        parsed = yaml.safe_load(payload.specs.fluid_yaml)
        # Round-trip must preserve the contract id verbatim — that's
        # the provenance hook every backend keys off.
        assert parsed["id"] == "bronze.orders"
        assert parsed["metadata"]["productType"] == "SDP"

    def test_odps_yaml_includes_data_product_id(self):
        import yaml

        payload = CatalogPublicationPayload.from_contract(_contract())
        assert payload.specs.odps_yaml is not None
        parsed = yaml.safe_load(payload.specs.odps_yaml)
        assert parsed.get("id") == "bronze.orders"

    def test_renderers_tolerate_minimal_contract(self):
        """A contract missing optional fields must still produce *some*
        payload — render failures degrade to None rather than aborting."""
        payload = CatalogPublicationPayload.from_contract({"id": "x.y", "exposes": []})
        # ODCS needs an expose to render; absence yields None per-asset
        # (we don't even have an asset here). Fluid/ODPS may render
        # or may degrade to None — either is fine, but the call must
        # not raise.
        assert payload.product.product_id == "x.y"
        assert payload.assets == ()


# ---------------------------------------------------------------------------
# Classifications normalised to tuples
# ---------------------------------------------------------------------------


class TestClassifications:
    def test_lists_become_tuples(self):
        payload = CatalogPublicationPayload.from_contract(
            _contract(),
            classifications={"email": ["pii", "email"]},
        )
        assert payload.classifications == {"email": ("pii", "email")}

    def test_empty_classifications_default(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        assert dict(payload.classifications) == {}


# ---------------------------------------------------------------------------
# Immutability — defensive against backend translators that mutate input
# ---------------------------------------------------------------------------


class TestImmutability:
    """The canonical payload is shared across backends; if one backend
    mutated it, others would see corrupted state. ``frozen=True`` on
    every dataclass enforces this at write time."""

    def test_product_payload_is_frozen(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            payload.product.layer = "Silver"  # type: ignore[misc]

    def test_asset_payload_is_frozen(self):
        payload = CatalogPublicationPayload.from_contract(_contract())
        with pytest.raises(Exception):
            payload.assets[0].asset_id = "different"  # type: ignore[misc]
