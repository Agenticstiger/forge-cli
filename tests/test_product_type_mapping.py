# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the v0.7.3 productType vocabulary and its layer cross-validation.

Pins the canonical mapping (Bronze↔SDP, Silver↔ADP, Gold↔CDP) and the
behaviour at each of the three validator layers that consume it:

  - ``fluid_build.schema._check_metadata`` (FLUID runtime validator)
  - ``fluid_build.schema_manager._validate_with_jsonschema`` (the path
    that ``fluid validate`` actually hits)
  - ``fluid_build.cli.contract_validation`` (extended-validation pass)

The tests deliberately avoid importing any helper module so they double
as a guard against re-introducing the abstraction the user rejected: the
mapping is enforced by inline 3-line dicts at each call site, and these
tests prove all three sites agree.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

# ── Fixture: minimal v0.7.3 acquisition contract template ─────────────


def _contract(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a minimal valid v0.7.3 acquisition contract with the given metadata."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.test_product",
        "name": "Test Product",
        "domain": "sales",
        "metadata": metadata,
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
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
                        "streams": ["public.x"],
                    },
                    "sink": {"format": "parquet"},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "x",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "./out.parquet"},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


# Canonical mapping the entire codebase agrees on. If this changes, the
# mapping has changed everywhere — bump it deliberately, in lockstep
# across all four call sites (schema.py, schema_manager.py,
# contract_validation.py, discover/emitter.py).
EXPECTED_MAPPING = {"Bronze": "SDP", "Silver": "ADP", "Gold": "CDP"}


# ── Layer 1: schema.py FLUID runtime validator (legacy 0.4.x/0.5.x path) ──
#
# ``fluid_build.schema`` is the fallback validator for legacy contracts;
# v0.7.3 routes through schema_manager._validate_with_jsonschema instead
# (TestSchemaManagerCrossCheck below). We exercise schema.py's
# ``_check_metadata`` helper directly so the cross-check it inherits is
# pinned in case a future legacy contract carries the new fields.


class TestSchemaModuleMetadataCheck:
    """``fluid_build.schema._check_metadata`` enforces the mapping in isolation."""

    def _check(self, metadata: Dict[str, Any]) -> list[str]:
        from fluid_build.schema import _check_metadata

        errors: list[str] = []
        _check_metadata(metadata, errors)
        return errors

    @pytest.mark.parametrize("layer,product_type", list(EXPECTED_MAPPING.items()))
    def test_canonical_pair_no_errors(self, layer: str, product_type: str):
        errors = self._check(
            {
                "layer": layer,
                "productType": product_type,
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        assert not errors, f"canonical {layer}↔{product_type} rejected: {errors}"

    def test_layer_only_no_errors(self):
        assert not self._check({"layer": "Bronze", "owner": {"team": "t", "email": "t@x.y"}})

    def test_product_type_only_no_errors(self):
        assert not self._check({"productType": "SDP", "owner": {"team": "t", "email": "t@x.y"}})

    def test_neither_set_errors(self):
        errors = self._check({"owner": {"team": "t", "email": "t@x.y"}})
        assert errors
        joined = " ".join(errors).lower()
        assert "layer" in joined and "producttype" in joined.replace(" ", "")

    @pytest.mark.parametrize(
        "layer,product_type",
        [("Bronze", "ADP"), ("Bronze", "CDP"), ("Silver", "SDP"), ("Gold", "ADP")],
    )
    def test_inconsistent_pair_errors(self, layer: str, product_type: str):
        errors = self._check(
            {
                "layer": layer,
                "productType": product_type,
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        assert errors
        joined = " ".join(errors).lower()
        assert "disagree" in joined or "inconsistent" in joined

    def test_invalid_product_type_errors(self):
        errors = self._check({"productType": "XYZ", "owner": {"team": "t", "email": "t@x.y"}})
        assert errors and any("productType" in e or "XYZ" in e for e in errors)

    def test_platinum_with_product_type_errors(self):
        errors = self._check(
            {
                "layer": "Platinum",
                "productType": "SDP",
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        assert errors and any("Platinum" in e or "analogue" in e.lower() for e in errors)

    def test_platinum_alone_no_errors(self):
        assert not self._check({"layer": "Platinum", "owner": {"team": "t", "email": "t@x.y"}})


# ── Layer 2: schema_manager.py JSON Schema cross-check ────────────────


class TestSchemaManagerCrossCheck:
    """The path ``fluid validate`` actually hits."""

    def _validate(self, metadata: Dict[str, Any]):
        from fluid_build.schema_manager import FluidSchemaManager

        return FluidSchemaManager().validate_contract(_contract(metadata))

    @pytest.mark.parametrize("layer,product_type", list(EXPECTED_MAPPING.items()))
    def test_canonical_pair_validates(self, layer: str, product_type: str):
        result = self._validate(
            {
                "layer": layer,
                "productType": product_type,
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        assert result.is_valid, f"{layer}↔{product_type}: {result.errors}"

    def test_inconsistent_pair_yields_error(self):
        result = self._validate(
            {
                "layer": "Bronze",
                "productType": "ADP",
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        assert not result.is_valid
        assert any("disagree" in e or "Canonical mapping" in e for e in result.errors)

    def test_invalid_product_type_value(self):
        result = self._validate({"productType": "XYZ", "owner": {"team": "t", "email": "t@x.y"}})
        assert not result.is_valid


# ── Layer 3: cli.contract_validation extended pass ────────────────────


class TestContractValidationPass:
    """The extended-validation surface (``fluid contract-validation``)."""

    def _validate(self, metadata: Dict[str, Any]):
        import json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path

        from fluid_build.cli.contract_validation import (
            ContractValidator,
            ValidationReport,
        )

        # ContractValidator requires a real path — write a tmp file then
        # pass it in. We only invoke the ``_validate_metadata`` helper so
        # the on-disk content doesn't otherwise matter, but the path
        # must exist. ``self.report`` is normally created inside
        # ``validate()`` — bootstrap a minimal one here so the helper can
        # write findings.
        tmp = Path(tempfile.mkstemp(suffix=".fluid.yaml")[1])
        tmp.write_text(json.dumps({"metadata": metadata}))
        v = ContractValidator(contract_path=tmp)
        v.contract = _contract(metadata)
        v.report = ValidationReport(
            contract_path=str(tmp),
            contract_id=v.contract.get("id", ""),
            contract_version=v.contract.get("fluidVersion", ""),
            validation_time=datetime.now(timezone.utc),
            duration=0.0,
        )
        v._validate_metadata()
        return v.report

    def test_consistent_pair_no_errors(self):
        report = self._validate(
            {
                "layer": "Silver",
                "productType": "ADP",
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        product_type_errors = [
            i
            for i in (report.issues or [])
            if "productType" in getattr(i, "path", "") and getattr(i, "severity", "") == "error"
        ]
        assert not product_type_errors, product_type_errors

    def test_inconsistent_pair_emits_error(self):
        report = self._validate(
            {
                "layer": "Silver",
                "productType": "CDP",
                "owner": {"team": "t", "email": "t@x.y"},
            }
        )
        product_type_errors = [
            i
            for i in (report.issues or [])
            if "productType" in getattr(i, "path", "") and getattr(i, "severity", "") == "error"
        ]
        assert product_type_errors, "expected an error on metadata.productType"

    def test_unknown_product_type_emits_error(self):
        report = self._validate({"productType": "ZZZ", "owner": {"team": "t", "email": "t@x.y"}})
        product_type_errors = [
            i for i in (report.issues or []) if "productType" in getattr(i, "path", "")
        ]
        assert product_type_errors


# ── Layer 4: emitter populates both vocabularies ─────────────────────


class TestEmitterPopulatesBothFields:
    """Discover emits Bronze contracts that also carry productType=SDP."""

    def test_emit_contract_includes_product_type(self):
        from fluid_build.cli.discover.emitter import emit_contract
        from fluid_build.cli.discover.registry import (
            DiscoveredColumn,
            DiscoveredStream,
        )

        contract = emit_contract(
            product_id="bronze.example.orders",
            name="Orders",
            domain="sales",
            owner_team="data",
            owner_email="d@x.y",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "h", "database": "d"},
            streams=[
                DiscoveredStream(
                    name="public.orders",
                    columns=[DiscoveredColumn(name="id", type="bigint")],
                )
            ],
        )
        meta = contract["metadata"]
        assert meta["layer"] == "Bronze"
        assert meta["productType"] == "SDP"
