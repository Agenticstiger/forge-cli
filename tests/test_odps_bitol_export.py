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

"""Phase 2 — Bitol ODPS provider export tests.

Coverage:
- **Validity**: emitted ODPS doc validates against the vendored
  odps-product-schema-v1.0.0.json; every per-port ODCS contract validates
  against odcs-schema-v3.1.0.json.
- **Linking invariant**: ``port.contractId == contracts[contractId]["id"]``
  exactly, for every output port. The renderer asserts this internally too;
  this test catches any future regression.
- **Layout**: ``out_dir`` writes 1 ODPS doc + N ODCS siblings with the
  expected file names; ports carry only ``contractId`` references (no
  embedded contract bodies).
- **Negative**: duplicate port names raise before any file is written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs.validation import load_schema as load_odcs_schema
from fluid_build.providers.odcs.validation import validate as validate_odcs
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.validation import load_schema as load_odps_schema
from fluid_build.providers.odps_standard.validation import validate as validate_odps

FIXTURE = Path(__file__).parent / "fixtures" / "fluid" / "contract-multi-expose.fluid.yaml"


@pytest.fixture(scope="module")
def fluid() -> dict:
    with open(FIXTURE) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def bundle(fluid: dict) -> dict:
    prov = BitolOdpsProvider()
    prov.strict_validation = False  # validity is tested explicitly below
    return prov.render(fluid)


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------


class TestValidity:
    def test_odps_doc_validates_against_vendored_schema(self, bundle: dict) -> None:
        schema = load_odps_schema()
        assert schema is not None, "Vendored ODPS schema must be present"
        validate_odps(bundle["product"], schema)  # raises on failure

    def test_every_odcs_contract_validates(self, bundle: dict) -> None:
        schema = load_odcs_schema()
        assert schema is not None
        for contract_id, odcs in bundle["contracts"].items():
            validate_odcs(odcs, schema)


# ---------------------------------------------------------------------------
# Linking invariant
# ---------------------------------------------------------------------------


class TestLinkingInvariant:
    def test_every_port_contract_id_matches_emitted_odcs_id(self, bundle: dict) -> None:
        product = bundle["product"]
        contracts = bundle["contracts"]
        for port in product.get("outputPorts", []):
            cid = port["contractId"]
            assert cid in contracts, f"port {port['name']} references missing contract {cid}"
            assert (
                contracts[cid]["id"] == cid
            ), f"contract.id {contracts[cid]['id']!r} != port.contractId {cid!r}"

    def test_contract_id_follows_convention(self, bundle: dict) -> None:
        product_id = bundle["product"]["id"]
        for port in bundle["product"].get("outputPorts", []):
            assert port["contractId"].startswith(
                f"{product_id}."
            ), f"contractId must be `{{productId}}.<port>` — got {port['contractId']!r}"


# ---------------------------------------------------------------------------
# File layout (out_dir mode)
# ---------------------------------------------------------------------------


class TestOutDirLayout:
    def test_writes_one_product_plus_n_contracts(self, fluid: dict, tmp_path: Path) -> None:
        prov = BitolOdpsProvider()
        prov.strict_validation = False
        prov.render(fluid, out_dir=tmp_path)

        odps_files = list(tmp_path.glob("*.odps.yaml"))
        odcs_files = list(tmp_path.glob("*.odcs.yaml"))
        expected_contracts = len(fluid["exposes"])
        assert len(odps_files) == 1, f"expected 1 ODPS doc, got {len(odps_files)}"
        assert (
            len(odcs_files) == expected_contracts
        ), f"expected {expected_contracts} ODCS siblings, got {len(odcs_files)}"

    def test_product_doc_carries_only_references_no_embedded_contracts(
        self, fluid: dict, tmp_path: Path
    ) -> None:
        prov = BitolOdpsProvider()
        prov.strict_validation = False
        prov.render(fluid, out_dir=tmp_path)

        odps_file = next(tmp_path.glob("*.odps.yaml"))
        with open(odps_file) as f:
            product = yaml.safe_load(f)
        for port in product.get("outputPorts", []):
            # Bitol fragments mode: only contractId references — no inline body
            assert (
                "contract" not in port
            ), "fragments mode must not embed contract bodies in the ODPS doc"

    def test_odcs_filenames_match_contract_ids(self, fluid: dict, tmp_path: Path) -> None:
        prov = BitolOdpsProvider()
        prov.strict_validation = False
        bundle = prov.render(fluid, out_dir=tmp_path)
        for contract_id in bundle["contracts"]:
            expected = tmp_path / f"{contract_id}.odcs.yaml"
            assert expected.exists(), f"expected {expected.name}"


# ---------------------------------------------------------------------------
# Coverage — FLUID expose fields land in the right ODCS slots
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_qos_lands_in_odcs_slaProperties(self, bundle: dict) -> None:
        contracts = bundle["contracts"]
        daily = contracts["commerce.orders-product.daily_orders"]
        sla = daily.get("slaProperties", [])
        properties = {entry["property"] for entry in sla}
        assert "availability" in properties
        assert "interval" in properties  # from qos.freshnessSLO

    def test_owner_lands_in_odcs_team_object(self, bundle: dict) -> None:
        team = bundle["contracts"]["commerce.orders-product.daily_orders"]["team"]
        assert team["name"] == "commerce-team"
        member = team["members"][0]
        assert member["username"] == "alice@acme.com"

    def test_schema_fields_appear_in_properties(self, bundle: dict) -> None:
        daily = bundle["contracts"]["commerce.orders-product.daily_orders"]
        props = daily["schema"][0]["properties"]
        names = {p["name"] for p in props}
        assert names == {"order_date", "order_count", "total_amount"}


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


class TestNegative:
    def test_duplicate_expose_id_raises_before_writing(self, fluid: dict, tmp_path: Path) -> None:
        bad = yaml.safe_load(yaml.dump(fluid))
        # Force a duplicate output-port name
        bad["exposes"].append(dict(bad["exposes"][0]))
        prov = BitolOdpsProvider()
        prov.strict_validation = False
        with pytest.raises(ProviderError, match="Duplicate output port name"):
            prov.render(bad, out_dir=tmp_path)
        # No files should have been written
        assert list(tmp_path.iterdir()) == []
