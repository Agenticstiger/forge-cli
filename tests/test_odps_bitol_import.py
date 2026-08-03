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

"""Phase 3 — three import entry points + round-trip canary.

Three input shapes converge on one validated FLUID:
  1. Single ODPS file       → BitolOdpsProvider.import_contract()
  2. Directory bundle       → BitolOdpsProvider.import_directory()
  3. Single ODCS file       → OdcsProvider.import_contract()

Plus the canonical canary: a FLUID exported via Bitol → re-imported → exported
again yields a byte-equal ODPS product doc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE_DIR = FIXTURES / "odps" / "product-bitol"
CONTRACTS_ONLY = FIXTURES / "odps" / "contracts-only"
BROKEN = FIXTURES / "odps" / "product-bitol-broken"


def _bundle_product_path() -> Path:
    return next(BUNDLE_DIR.glob("*.odps.yaml"))


def _bundle_first_odcs_path() -> Path:
    return next(BUNDLE_DIR.glob("*.odcs.yaml"))


@pytest.fixture
def provider() -> BitolOdpsProvider:
    prov = BitolOdpsProvider()
    prov.strict_validation = False  # fixtures aren't tuned for strict ODCS schema
    return prov


# ---------------------------------------------------------------------------
# Entry 1: single ODPS file
# ---------------------------------------------------------------------------


class TestImportContract:
    def test_emits_one_expose_per_output_port(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_contract(_bundle_product_path())
        assert len(fluid["exposes"]) == 2

    def test_each_expose_carries_a_populated_schema(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_contract(_bundle_product_path())
        for expose in fluid["exposes"]:
            schema = expose.get("contract", {}).get("schema", [])
            assert schema, f"expose {expose['id']} has no schema"
            assert all("name" in field for field in schema)

    def test_qos_carries_back_from_slaProperties(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_contract(_bundle_product_path())
        # daily_orders had qos.availability + freshnessSLO; sla preservation
        # round-trips into the first expose's qos block.
        daily = next(e for e in fluid["exposes"] if e["id"] == "daily_orders")
        assert "qos" in daily

    def test_metadata_owner_team_present(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_contract(_bundle_product_path())
        assert fluid.get("owner", {}).get("team") == "commerce-team"

    def test_passthrough_source_captured(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_contract(_bundle_product_path())
        pt = (fluid.get("metadata") or {}).get("odps_passthrough") or {}
        assert "source" in pt, "import must preserve the original ODPS doc"


# ---------------------------------------------------------------------------
# Entry 2: directory
# ---------------------------------------------------------------------------


class TestImportDirectory:
    def test_directory_import_matches_file_import(self, provider: BitolOdpsProvider) -> None:
        from_file = provider.import_contract(_bundle_product_path())
        from_dir = provider.import_directory(BUNDLE_DIR)
        assert from_file == from_dir


class TestImportOdcsOnlyDirectory:
    def test_emits_one_expose_per_odcs_file(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_directory(CONTRACTS_ONLY)
        n_files = len(list(CONTRACTS_ONLY.glob("*.odcs.yaml")))
        assert len(fluid["exposes"]) == n_files

    def test_warning_recorded_in_passthrough(self, provider: BitolOdpsProvider) -> None:
        fluid = provider.import_directory(CONTRACTS_ONLY)
        pt = (fluid.get("metadata") or {}).get("odps_passthrough") or {}
        assert pt.get("odcs_only_directory") is True
        assert "odcs_only_warning" in pt


# ---------------------------------------------------------------------------
# Entry 3: lone ODCS file (delegates to OdcsProvider)
# ---------------------------------------------------------------------------


class TestImportLoneOdcs:
    def test_lone_odcs_produces_single_expose(self) -> None:
        fluid = OdcsProvider().import_contract(_bundle_first_odcs_path())
        assert len(fluid["exposes"]) == 1

    def test_lone_odcs_schema_populated(self) -> None:
        fluid = OdcsProvider().import_contract(_bundle_first_odcs_path())
        schema = fluid["exposes"][0]["contract"]["schema"]
        assert schema, "lone ODCS import must produce a populated schema"


# ---------------------------------------------------------------------------
# Round-trip canary: bundle → import → re-export equals original product doc
# ---------------------------------------------------------------------------


class TestBundleRoundTrip:
    def test_product_doc_round_trips_zero_diff(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # Export-then-import-then-re-export must reproduce the product doc.
        fluid_in = provider.import_directory(BUNDLE_DIR)
        bundle = provider.render(fluid_in, out_dir=tmp_path)
        # Compare with the original product doc on disk
        import yaml

        with open(_bundle_product_path()) as f:
            original_product = yaml.safe_load(f)
        assert bundle["product"] == original_product


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


class TestNegative:
    def test_dangling_contract_id_raises_for_output_port(self, provider: BitolOdpsProvider) -> None:
        # Output ports MUST resolve; lenient=False (default) raises
        with pytest.raises(ProviderError):
            provider.import_contract(BROKEN / "dangling-port.odps.yaml", lenient=False)

    def test_dangling_contract_id_lenient_warns(self, provider: BitolOdpsProvider) -> None:
        # With lenient=True, unresolved output ports are warn-and-continue
        fluid = provider.import_contract(BROKEN / "dangling-port.odps.yaml", lenient=True)
        # The expose stub still gets created — just without a contract body
        assert len(fluid["exposes"]) == 1

    def test_two_odps_docs_in_directory_raises(self, provider: BitolOdpsProvider) -> None:
        with pytest.raises(ProviderError, match="found 2 ODPS docs"):
            provider.import_directory(BROKEN / "two-odps-docs")

    def test_nonexistent_directory_raises(self, provider: BitolOdpsProvider) -> None:
        with pytest.raises(ProviderError, match="is not a directory"):
            provider.import_directory(FIXTURES / "does-not-exist")
