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

"""ODPS bundle filenames cannot traverse outside ``--out-dir``.

``_write_bundle`` names files from document-controlled ids (the product id
and the per-port contract ids). An imported foreign document can carry a
hostile id, so the stems are gated and the final paths containment-checked
before any write. Legitimate FLUID ids must keep their exact canonical
filenames because the import-side ContractResolver looks siblings up by
``<contractId>.odcs.<fmt>``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.providers._path_safety import contained_path, safe_filename_stem
from fluid_build.providers.base import ProviderError
from fluid_build.providers.odps_standard.provider import BitolOdpsProvider

pytestmark = pytest.mark.unit


class TestSafeBundleStem:
    @pytest.mark.parametrize("legit", ["gold.orders", "silver.demo.orders", "a", "x_1-b"])
    def test_schema_valid_ids_pass_verbatim(self, legit):
        """The canonical sibling layout depends on this: the resolver looks
        up <contractId>.odcs.<fmt> by the exact id."""
        assert safe_filename_stem(legit, "product") == legit

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../../tmp/evil",
            "..",
            "a/b/c",
            "..\\..\\windows",
            "/etc/passwd",
            "x:stream",
            "evil\x00null",
        ],
    )
    def test_hostile_ids_lose_every_separator(self, hostile):
        stem = safe_filename_stem(hostile, "product")
        assert "/" not in stem and "\\" not in stem and ":" not in stem
        assert not stem.startswith(".")
        assert "\x00" not in stem

    def test_empty_and_dot_only_ids_fall_back(self):
        assert safe_filename_stem("", "product").startswith("product_")
        assert safe_filename_stem(None, "product").startswith("product_")
        assert safe_filename_stem("...", "contract").startswith("contract_")

    def test_distinct_hostile_ids_do_not_collide(self):
        """Without the digest, a/b and a_b both clean to a_b and the
        second write silently clobbers the first."""
        assert safe_filename_stem("a/b", "p") != safe_filename_stem("a_b", "p")

    def test_sanitised_stems_are_deterministic(self):
        assert safe_filename_stem("../x", "p") == safe_filename_stem("../x", "p")


class TestContainment:
    def test_normal_filename_is_allowed(self, tmp_path):
        assert contained_path(tmp_path, "gold.orders.odps.yaml").parent == tmp_path.resolve()

    def test_escaping_path_raises(self, tmp_path):
        with pytest.raises(ProviderError, match="outside the output directory"):
            contained_path(tmp_path, "../escape.odps.yaml")


class TestWriteBundleEndToEnd:
    def _hostile_product(self, product_id: str) -> Dict[str, Any]:
        return {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": product_id,
            "name": "evil",
            "status": "active",
        }

    def test_traversal_id_cannot_escape_out_dir(self, tmp_path):
        out_dir = tmp_path / "bundle"
        provider = BitolOdpsProvider()
        product = self._hostile_product("../../escape")
        provider._write_bundle(product, {}, out_dir, "yaml")

        written = [p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()]
        assert written, "the bundle must still be written, just safely"
        for path in written:
            assert str(path).startswith("bundle" + os.sep), f"escaped out_dir: {path}"

    def test_hostile_contract_id_cannot_escape_out_dir(self, tmp_path):
        out_dir = tmp_path / "bundle"
        provider = BitolOdpsProvider()
        product = self._hostile_product("ok.product")
        contracts = {"../../escape.contract": {"apiVersion": "v3.1.0", "kind": "DataContract"}}
        provider._write_bundle(product, contracts, out_dir, "yaml")

        for path in [p for p in tmp_path.rglob("*") if p.is_file()]:
            assert str(path.relative_to(tmp_path)).startswith("bundle" + os.sep)

    def test_legitimate_bundle_layout_is_byte_identical(self, tmp_path):
        """The fix must not rename any legitimately-identified file."""
        out_dir = tmp_path / "bundle"
        provider = BitolOdpsProvider()
        product = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "gold.orders",
            "name": "orders",
            "status": "active",
        }
        contracts = {"gold.orders.events": {"apiVersion": "v3.1.0", "kind": "DataContract"}}
        provider._write_bundle(product, contracts, out_dir, "yaml")
        names = sorted(p.name for p in out_dir.iterdir())
        assert names == ["gold.orders.events.odcs.yaml", "gold.orders.odps.yaml"]


class TestRoundTripStillWorks:
    def test_export_import_round_trip_via_out_dir(self, tmp_path):
        """The sibling-file convention survives the gate end to end."""
        contract = {
            "fluidVersion": "0.7.6",
            "kind": "DataProduct",
            "id": "silver.orders",
            "name": "orders",
            "metadata": {"name": "orders", "version": "1.0.0", "status": "active"},
            "exposes": [
                {
                    "exposeId": "orders",
                    "kind": "table",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "DB", "schema": "PUBLIC", "table": "ORDERS"},
                    },
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        provider = BitolOdpsProvider()
        out_dir = tmp_path / "bundle"
        provider.render(contract, out_dir=out_dir)
        assert (out_dir / "silver.orders.odps.yaml").exists()

        imported = provider.import_directory(out_dir)
        assert imported["contract"]["id"] == "silver.orders"


class TestOtherEmittersAreGated:
    """The same bug lived in two more places found by adversarial review.

    The per-port ODCS writer was the exploitable one: `fluid generate
    artifacts` does not gate on `fluid validate`, so a contract with a
    traversal exposeId escaped `--out` for real (verified before the fix).
    """

    def _traversal_contract(self) -> Dict[str, Any]:
        return {
            "fluidVersion": "0.7.6",
            "kind": "DataProduct",
            "id": "gold.evil",
            "name": "evil",
            "metadata": {"name": "evil", "version": "1.0.0", "status": "active"},
            "exposes": [
                {
                    "exposeId": "../../../../../../escape",
                    "kind": "table",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "DB", "schema": "PUBLIC", "table": "T"},
                    },
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }

    def test_render_all_ports_cannot_escape(self, tmp_path):
        from fluid_build.providers.odcs.provider import OdcsProvider

        out_dir = tmp_path / "odcs"
        out_dir.mkdir()
        OdcsProvider().render_all_ports(self._traversal_contract(), out_dir=out_dir, fmt="yaml")
        for path in [p for p in tmp_path.rglob("*") if p.is_file()]:
            assert str(path.relative_to(tmp_path)).startswith("odcs" + os.sep)

    def test_fanout_reports_the_paths_actually_written(self, tmp_path):
        """Deriving names from the raw id made the manifest reference
        phantom files, which broke the integrity gate."""
        import logging

        import yaml as _yaml

        from fluid_build.forge.core.artifact_fanout import _emit_odcs

        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(_yaml.safe_dump(self._traversal_contract()), encoding="utf-8")
        out_dir = tmp_path / "out"
        written = _emit_odcs(contract_path, out_dir, logging.getLogger(__name__))
        assert written
        for path in written:
            assert path.exists(), f"manifest would reference a phantom file: {path}"
            assert path.resolve().is_relative_to(out_dir.resolve())
