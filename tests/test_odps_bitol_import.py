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
import yaml

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE_DIR = FIXTURES / "odps" / "product-bitol"
CONTRACTS_ONLY = FIXTURES / "odps" / "contracts-only"
BROKEN = FIXTURES / "odps" / "product-bitol-broken"


# 13 levels of [*a, *a]: 26 aliases that grow a 30-node file to 49,150 nodes.
_ALIAS_BOMB = "\n".join(
    ["l0: &l0 [lol]"] + [f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]" for i in range(1, 14)]
)


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

    @pytest.mark.parametrize("ports, lenient", [("outputPorts", True), ("inputPorts", False)])
    def test_malformed_contract_id_skips_the_port(
        self, provider: BitolOdpsProvider, ports: str, lenient: bool
    ) -> None:
        # A NUL byte in a contractId made the resolver raise ValueError, which
        # aborted the import even with lenient=True, and even on input ports,
        # which always resolve leniently.
        doc = yaml.safe_load(_bundle_product_path().read_text(encoding="utf-8"))
        doc.setdefault(ports, [{"name": "upstream", "version": "1.0.0"}])[0]["contractId"] = "x\x00"
        fluid = provider.import_contract(doc, base_path=BUNDLE_DIR, lenient=lenient)
        assert len(fluid["exposes"]) == 2

    @pytest.mark.parametrize("contract_id", [False, 0, []], ids=["false", "zero", "empty-list"])
    def test_falsy_non_string_contract_id_fails_a_strict_import(
        self, provider: BitolOdpsProvider, contract_id: object
    ) -> None:
        # `if not cid` skipped these, so a strict import succeeded with the port
        # unresolved, while a truthy non-string failed as not a valid contractId.
        doc = yaml.safe_load(_bundle_product_path().read_text(encoding="utf-8"))
        doc["outputPorts"][0]["contractId"] = contract_id
        with pytest.raises(ProviderError, match="not a valid contractId"):
            provider.import_contract(doc, base_path=BUNDLE_DIR, lenient=False)

    def test_lenient_skip_warning_escapes_the_contract_id(
        self, provider: BitolOdpsProvider
    ) -> None:
        from unittest.mock import patch

        from fluid_build.providers.odps_standard.resolver import ContractResolver

        with patch.object(provider, "logger") as log:
            assert provider._safe_resolve(ContractResolver(), "x\x00", lenient=True) is None
        fmt, contract_id, exc = log.warning.call_args.args
        assert (fmt % (contract_id, exc)).startswith("Skipping unresolved contract 'x\\x00'")

    def test_two_odps_docs_in_directory_raises(self, provider: BitolOdpsProvider) -> None:
        with pytest.raises(ProviderError, match="found 2 ODPS docs"):
            provider.import_directory(BROKEN / "two-odps-docs")

    def test_nonexistent_directory_raises(self, provider: BitolOdpsProvider) -> None:
        with pytest.raises(ProviderError, match="is not a directory"):
            provider.import_directory(FIXTURES / "does-not-exist")


# Over the YAML caps, or not YAML at all.
_UNREADABLE = pytest.mark.parametrize(
    "content", [_ALIAS_BOMB, "apiVersion: v1.0.0\nid: [unclosed\n"], ids=["alias-bomb", "malformed"]
)


class TestUnreadableBundleFile:
    """A directory file that cannot be read is skipped, never silently reclassified."""

    @_UNREADABLE
    def test_unreadable_file_without_a_readable_product_fails_the_directory(
        self, provider: BitolOdpsProvider, tmp_path: Path, content: str
    ) -> None:
        # The unreadable file may be the product: importing the rest as an
        # ODCS-only directory would drop it without an error.
        (tmp_path / "product.odps.yaml").write_text(content, encoding="utf-8")
        for odcs in CONTRACTS_ONLY.glob("*.odcs.yaml"):
            (tmp_path / odcs.name).write_text(odcs.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(ProviderError, match="could not read 1 file"):
            provider.import_directory(tmp_path)

    @_UNREADABLE
    def test_unreadable_file_next_to_a_readable_product_is_skipped(
        self, provider: BitolOdpsProvider, tmp_path: Path, content: str
    ) -> None:
        from unittest.mock import patch

        for path in BUNDLE_DIR.iterdir():
            (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "docker-compose.yaml").write_text(content, encoding="utf-8")
        with patch.object(provider, "logger") as log:
            assert len(provider.import_directory(tmp_path)["exposes"]) == 2
        skipped = [c.args[1] for c in log.warning.call_args_list if c.args[0] == "Skipping %r: %s"]
        assert skipped == [str(tmp_path / "docker-compose.yaml")]

    def test_uppercase_suffix_is_read_as_yaml(
        self, provider: BitolOdpsProvider, tmp_path: Path
    ) -> None:
        # A case-sensitive suffix check parsed PRODUCT.ODPS.YAML as JSON, and the
        # directory imported as ODCS-only, without the product's owner.
        for path in BUNDLE_DIR.iterdir():
            name = path.name.upper() if path.name.endswith(".odps.yaml") else path.name
            (tmp_path / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        assert "owner" in provider.import_directory(tmp_path)
