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

"""Compatibility matrix for representative FLUID contract versions.

This suite protects a small set of real fixture contracts across:
  - schema validation
  - ODCS export
  - official OPDS export
  - ODPS-standard export
  - DMM dry-run payload generation

The matrix is intentionally small but durable:
  - minimal 0.5.7
  - minimal 0.7.1
  - minimal 0.7.2
  - lineage 0.7.1
  - lineage 0.7.2
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.providers.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.odcs.odcs import OdcsProvider
from fluid_build.providers.odps_standard import OdpsStandardProvider
from fluid_build.providers.opds.opds import OdpsProvider
from fluid_build.schema_manager import FluidSchemaManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts" / "compatibility"

MINIMAL_FIXTURES = [
    ("minimal_071.yaml", "0.7.1"),
    ("minimal_072.yaml", "0.7.2"),
    ("minimal_073.yaml", "0.7.3"),
]

LINEAGE_FIXTURES = [
    ("lineage_071.yaml", "0.7.1"),
    ("lineage_072.yaml", "0.7.2"),
]

ALL_FIXTURES = MINIMAL_FIXTURES + LINEAGE_FIXTURES


def _load_contract(fixture_name: str) -> dict:
    with (FIXTURE_DIR / fixture_name).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _first_expose_id(contract: dict) -> str:
    expose = contract["exposes"][0]
    return expose.get("id") or expose.get("exposeId")


def _expected_output_ids(contract: dict) -> list[str]:
    return [(expose.get("id") or expose.get("exposeId")) for expose in contract.get("exposes", [])]


def _expected_input_ids(contract: dict) -> list[str]:
    return [
        (consume.get("id") or consume.get("exposeId")) for consume in contract.get("consumes", [])
    ]


def _expected_input_refs(contract: dict) -> list[str]:
    return [
        (consume.get("productId") or consume.get("ref")) for consume in contract.get("consumes", [])
    ]


def _expected_dmm_input_contract_ids(contract: dict) -> list[str]:
    """DMM's ODPS overlay promotes ``consumes: {productId, exposeId}`` references
    to the expose-level ``{productId}.{exposeId}`` published address. See
    ``DataMeshManagerProvider._ensure_odps_input_port_contract_ids`` for the
    promotion rule; other ODPS emitters (OdpsStandardProvider, official
    OPDS) keep the bare product-level reference.
    """
    refs: list[str] = []
    for consume in contract.get("consumes", []):
        explicit = consume.get("contractId") or consume.get("contract_id")
        if explicit:
            refs.append(explicit)
            continue
        reference = consume.get("productId") or consume.get("ref")
        port_id = consume.get("exposeId") or consume.get("id")
        if reference and port_id:
            refs.append(f"{reference}.{port_id}")
        elif reference:
            refs.append(reference)
    return refs


@pytest.mark.parametrize(("fixture_name", "expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_validate_offline(fixture_name: str, expected_version: str):
    contract = _load_contract(fixture_name)

    result = FluidSchemaManager().validate_contract(contract, offline_only=True)

    assert result.is_valid, result.get_summary()
    assert str(result.schema_version) == expected_version


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_export_to_odcs(fixture_name: str, _expected_version: str):
    contract = _load_contract(fixture_name)

    rendered = OdcsProvider().render(contract)

    assert rendered["apiVersion"] == "v3.1.0"
    assert rendered["kind"] == "DataContract"
    assert rendered["id"]
    assert rendered["schema"]
    assert rendered["servers"]


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_export_to_official_opds(fixture_name: str, _expected_version: str):
    contract = _load_contract(fixture_name)

    rendered = OdpsProvider().render(contract)
    product = rendered["artifacts"]["product"]
    legacy = product["_legacy"]

    assert rendered["opds_version"] == "1.0"
    assert rendered["artifacts"]["version"] == "4.1"
    assert legacy["dataProductId"] == contract["id"]
    assert legacy["outputPorts"][0]["id"] == _first_expose_id(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_export_to_odps_standard(fixture_name: str, _expected_version: str):
    contract = _load_contract(fixture_name)

    rendered = OdpsStandardProvider().render(contract)

    assert rendered["apiVersion"] == "v1.0.0"
    assert rendered["kind"] == "DataProduct"
    assert rendered["id"] == contract["id"]
    # ODPS-Bitol v1.0.0 OutputPort (``additionalProperties: false``) forbids
    # ``id``; the expose identifier travels via ``name``. Assert that shape.
    assert [port["name"] for port in rendered["outputPorts"]] == _expected_output_ids(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_dmm_dry_run_dps(fixture_name: str, _expected_version: str):
    """Legacy DPS shape is reachable via explicit
    ``data_product_specification='0.0.1'``. The provider's default
    switched to ODPS in 2026-05 (DMM rejects DPS server-side); this
    test pins the DPS-shape contract for any caller that still
    explicitly opts in."""
    contract = _load_contract(fixture_name)
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(contract, dry_run=True, data_product_specification="0.0.1")
    payload = result["payload"]

    assert payload["id"] == contract["id"]
    assert payload["dataProductSpecification"] == "0.0.1"
    assert payload["teamId"] == contract["metadata"]["owner"]["team"]
    # DPS output ports keep ``id`` (non-ODPS-Bitol shape); unchanged.
    assert [port["id"] for port in payload["outputPorts"]] == _expected_output_ids(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_dmm_dry_run_odps(fixture_name: str, _expected_version: str):
    contract = _load_contract(fixture_name)
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(contract, dry_run=True, provider_hint="odps")
    payload = result["payload"]

    assert payload["apiVersion"] == "v1.0.0"
    assert payload["kind"] == "DataProduct"
    assert payload["id"] == contract["id"]
    assert payload["team"]["name"] == contract["metadata"]["owner"]["team"]
    # ODPS-Bitol v1.0.0 — ``name`` not ``id``.
    assert [port["name"] for port in payload["outputPorts"]] == _expected_output_ids(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), LINEAGE_FIXTURES)
def test_lineage_fixtures_preserve_input_ports_across_odps_exports(
    fixture_name: str, _expected_version: str
):
    contract = _load_contract(fixture_name)
    expected_ids = _expected_input_ids(contract)
    expected_refs = _expected_input_refs(contract)
    odps_standard = OdpsStandardProvider().render(contract)
    dmm_result = DataMeshManagerProvider(
        api_key="dummy", api_url="https://api.entropy-data.com"
    ).apply(contract, dry_run=True, provider_hint="odps")
    dmm_odps = dmm_result["payload"]
    official_opds = OdpsProvider().render(contract)["artifacts"]["product"]["_legacy"]

    # ODPS-Bitol v1.0.0 InputPort forbids ``id`` and ``reference``; the
    # identifier travels via ``name`` and the reference is folded into
    # ``contractId`` (synthesized when no explicit contractId is provided).
    # The raw OdpsStandardProvider uses the bare product reference as the
    # synthetic contractId — no DMM-specific promotion applied.
    assert [port["name"] for port in odps_standard["inputPorts"]] == expected_ids
    assert [port["contractId"] for port in odps_standard["inputPorts"]] == expected_refs
    # DMM removes product-to-product consumes from the ODPS inputPorts and
    # publishes them as first-class Entropy Access agreements instead. This
    # avoids mirroring upstream products as SourceSystems in the DMM graph.
    assert "inputPorts" not in dmm_odps
    access_edges = {
        (
            preview["payload"]["provider"]["dataProductId"],
            preview["payload"]["provider"]["outputPortId"],
        )
        for preview in dmm_result["access_agreements"]
    }
    assert access_edges == set(zip(expected_refs, expected_ids, strict=True))
    # OdpsProvider (legacy OPDS/Linux Foundation emitter) is a DIFFERENT
    # provider — keeps its legacy {id, reference} shape. Not part of the
    # ODPS-Bitol schema contract.
    assert [port["id"] for port in official_opds["inputPorts"]] == expected_ids
    assert [port["reference"] for port in official_opds["inputPorts"]] == expected_refs
