# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Long-tail fixture coverage from the Phase 6 plan.

  * **contract-edge.yaml** — string-form ``team`` (ODCS v2 wire format),
    exotic logicalTypes (uuid format, BIGNUMERIC, jsonb, deeply-nested
    structs), and mixed-unit slaProperties (time + percentage + count).
    Known mapper limitations are pinned via xfail markers.
  * **product-bitol-nested/** — ODCS contracts in a ``contracts/`` subdir.
    Exercises the resolver's candidate-path probing across
    ``{<base>, contracts/, odcs/, odcs/contracts/}``.
  * **product-bitol-remote.yaml** — port's ``contractId`` is an https URL.
    Exercises the resolver's http(s) path + ``--no-remote`` rejection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.resolver import (
    ContractResolver,
    RemoteFetchDisabled,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# contract-edge.yaml
# ---------------------------------------------------------------------------


class TestEdgeContract:
    """Long-tail ODCS shapes — string-form team, exotic types, nested structs,
    mixed-unit SLA. Round-trip must not RAISE; some bit-perfect fidelity gaps
    are pinned as xfail until the mappers grow first-class support."""

    @pytest.fixture
    def edge_odcs(self) -> dict:
        return yaml.safe_load((FIXTURES / "odcs/contract-edge.yaml").read_text())

    def test_import_does_not_raise(self, edge_odcs: dict) -> None:
        OdcsProvider().import_contract(edge_odcs)

    def test_render_after_import_does_not_raise(self, edge_odcs: dict) -> None:
        fluid = OdcsProvider().import_contract(edge_odcs)
        OdcsProvider().render(fluid)

    def test_sla_unit_families_survive_import(self, edge_odcs: dict) -> None:
        """Mixed-unit SLA (time + percentage + count) reach the FLUID skeleton."""
        fluid = OdcsProvider().import_contract(edge_odcs)
        text = yaml.dump(fluid)
        assert "maxStaleness" in text or "max_staleness" in text
        assert "availability" in text
        assert "errorRate" in text or "error_rate" in text

    @pytest.mark.xfail(
        strict=False,
        reason="Mapper does not yet pass-through field-level `format` (e.g. uuid). "
        "Fixture pins the gap; flip when mappers/schema.py learns format.",
    )
    def test_field_format_round_trips(self, edge_odcs: dict) -> None:
        rebuilt = OdcsProvider().render(OdcsProvider().import_contract(edge_odcs))
        first_prop = rebuilt["schema"][0]["properties"][0]
        assert first_prop.get("format") == "uuid", "format field lost on round-trip"

    @pytest.mark.xfail(
        strict=False,
        reason="Mapper does not yet pass-through deeply-nested object properties "
        "(struct-in-struct). Fixture pins the gap.",
    )
    def test_nested_object_properties_survive(self, edge_odcs: dict) -> None:
        rebuilt = OdcsProvider().render(OdcsProvider().import_contract(edge_odcs))
        payload = next(
            p for p in rebuilt["schema"][0]["properties"] if p["name"] == "payload"
        )
        assert "properties" in payload
        inner = next(
            p for p in payload["properties"] if p["name"] == "inner_struct"
        )
        assert any(p["name"] == "leaf" for p in inner["properties"])


# ---------------------------------------------------------------------------
# product-bitol-nested/  —  resolver candidate-path probing
# ---------------------------------------------------------------------------


class TestNestedBundleLayout:
    """ODCS in a ``contracts/`` subdir. Resolver must find via candidate-path."""

    BUNDLE = FIXTURES / "odps/product-bitol-nested"

    def test_import_directory_resolves_nested_odcs(self) -> None:
        provider = BitolOdpsProvider()
        fluid = provider.import_directory(self.BUNDLE, allow_remote=False)
        exposes = fluid.get("exposes", [])
        assert len(exposes) == 1
        primary = exposes[0]
        assert (primary.get("exposeId") or primary.get("id")) == "customers"
        contract = primary.get("contract") or {}
        schema = contract.get("schema") or []
        assert schema, "resolver failed to populate schema from nested ODCS"

    def test_resolver_probes_contracts_subdir(self) -> None:
        """ContractResolver's local-probe order includes contracts/ subdir."""
        resolver = ContractResolver(
            base_path=self.BUNDLE,
            allow_remote=False,
            odcs_provider=OdcsProvider(),
        )
        resolved = resolver.resolve("nested.customer.product.customers")
        assert resolved is not None
        assert resolved.source == "local"
        assert "contracts" in str(resolved.origin)


# ---------------------------------------------------------------------------
# product-bitol-remote.yaml  —  http(s) contractId + --no-remote rejection
# ---------------------------------------------------------------------------


class TestRemoteContractFixture:
    """Port's contractId is an https URL. Both code paths exercised."""

    FIXTURE = FIXTURES / "odps/product-bitol-remote.yaml"

    def test_no_remote_raises(self) -> None:
        provider = BitolOdpsProvider()
        with pytest.raises((RemoteFetchDisabled, Exception)) as exc_info:
            provider.import_contract(self.FIXTURE, allow_remote=False)
        msg = str(exc_info.value).lower()
        assert "remote" in msg or "https" in msg or "not found" in msg

    def test_remote_url_resolves_via_mocked_http(self) -> None:
        odcs_yaml = (
            "apiVersion: v3.1.0\n"
            "kind: DataContract\n"
            "id: https://example.invalid/contracts/users-v1.odcs.yaml\n"
            "name: External Users\n"
            "version: 1.0.0\n"
            "status: active\n"
            "schema:\n"
            "  - name: external_users\n"
            "    logicalType: object\n"
            "    physicalType: table\n"
            "    properties:\n"
            "      - name: external_id\n"
            "        logicalType: string\n"
            "        primaryKey: true\n"
            "servers:\n"
            "  - server: prod\n"
            "    type: bigquery\n"
            "    project: ext_project\n"
            "    dataset: ext_dataset\n"
        )

        class _FakeResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getheader(self, name: str, default=None):
                if name.lower() == "content-type":
                    return "application/yaml"
                return default

            @property
            def headers(self):
                return {"Content-Type": "application/yaml"}

        with patch(
            "fluid_build.providers.odps_standard.resolver.urlopen",
            return_value=_FakeResponse(odcs_yaml.encode("utf-8")),
        ):
            provider = BitolOdpsProvider()
            fluid = provider.import_contract(self.FIXTURE, allow_remote=True)

        exposes = fluid.get("exposes", [])
        assert len(exposes) == 1
        primary = exposes[0]
        assert (primary.get("exposeId") or primary.get("id")) == "external_users"
