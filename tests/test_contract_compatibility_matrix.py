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
from fluid_build.providers.odps.odps import OdpsProvider
from fluid_build.providers.odps_standard import OdpsStandardProvider
from fluid_build.schema_manager import FluidSchemaManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts" / "compatibility"

MINIMAL_FIXTURES = [
    ("minimal_057.yaml", "0.5.7"),
    ("minimal_071.yaml", "0.7.1"),
    ("minimal_072.yaml", "0.7.2"),
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
    assert [port["id"] for port in rendered["outputPorts"]] == _expected_output_ids(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), ALL_FIXTURES)
def test_compatibility_fixtures_dmm_dry_run_dps(fixture_name: str, _expected_version: str):
    contract = _load_contract(fixture_name)
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(contract, dry_run=True)
    payload = result["payload"]

    assert payload["id"] == contract["id"]
    assert payload["dataProductSpecification"] == "0.0.1"
    assert payload["teamId"] == contract["metadata"]["owner"]["team"]
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
    assert [port["id"] for port in payload["outputPorts"]] == _expected_output_ids(contract)


@pytest.mark.parametrize(("fixture_name", "_expected_version"), LINEAGE_FIXTURES)
def test_lineage_fixtures_preserve_input_ports_across_odps_exports(
    fixture_name: str, _expected_version: str
):
    contract = _load_contract(fixture_name)
    expected_ids = _expected_input_ids(contract)
    expected_refs = _expected_input_refs(contract)

    odps_standard = OdpsStandardProvider().render(contract)
    dmm_odps = DataMeshManagerProvider(
        api_key="dummy", api_url="https://api.entropy-data.com"
    ).apply(contract, dry_run=True, provider_hint="odps")["payload"]
    official_opds = OdpsProvider().render(contract)["artifacts"]["product"]["_legacy"]

    assert [port["id"] for port in odps_standard["inputPorts"]] == expected_ids
    assert [port["reference"] for port in odps_standard["inputPorts"]] == expected_refs
    assert [port["id"] for port in dmm_odps["inputPorts"]] == expected_ids
    assert [port["reference"] for port in dmm_odps["inputPorts"]] == expected_refs
    assert [port["id"] for port in official_opds["inputPorts"]] == expected_ids
    assert [port["reference"] for port in official_opds["inputPorts"]] == expected_refs
