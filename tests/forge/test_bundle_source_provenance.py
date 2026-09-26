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

"""The MANIFEST ``source`` block, and how far a reader trusts it.

The block sits outside the merkle root (like ``contractId``), so readers
treat it as a hint: a recorded source contract is used only when it is a
relative path to an existing ``.yaml`` / ``.yml`` / ``.json`` file.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build._contract_loader import load_contract_with_overlay
from fluid_build.forge.core.bundle import (
    build_bundle_tgz,
    bundle_source_contract,
    make_bundle_source,
    read_bundle_source,
    validate_manifest,
)
from fluid_build.util.binding_paths import anchor_binding_paths, resolve_binding_path

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "demo.src",
    "name": "Src",
    "exposes": [
        {
            "exposeId": "rows",
            "binding": {"platform": "local", "location": {"path": "./out/rows.parquet"}},
        }
    ],
}


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    (tmp_path / "contracts" / "p").mkdir(parents=True)
    (tmp_path / "contracts" / "p" / "contract.fluid.yaml").write_text(
        "id: demo.src\nkind: DataProduct\n", encoding="utf-8"
    )
    # A contract-shaped file that is NOT the one the bundle was built from.
    (tmp_path / "contracts" / "other").mkdir()
    (tmp_path / "contracts" / "other" / "contract.fluid.yaml").write_text(
        "id: someone.else\nkind: DataProduct\n", encoding="utf-8"
    )
    (tmp_path / "runtime").mkdir()
    return tmp_path


def _rewrite_source(tgz: Path, source: Dict[str, Any]) -> None:
    """Edit the MANIFEST source block in place (it is outside the merkle root)."""
    with tarfile.open(tgz, "r:gz") as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    manifest = json.loads(members["MANIFEST.json"])
    manifest["source"] = source
    members["MANIFEST.json"] = json.dumps(manifest).encode("utf-8")
    with tarfile.open(tgz, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)

            tar.addfile(info, io.BytesIO(data))


class TestSourceBlock:
    def test_relative_to_the_bundle_directory(self, layout: Path) -> None:
        src = make_bundle_source(
            layout / "contracts" / "p" / "contract.fluid.yaml",
            layout / "runtime" / "b.tgz",
            env="aws",
            overlay_path=layout / "contracts" / "p" / "overlays" / "aws.yaml",
        )
        assert src == {
            "contract": "../contracts/p/contract.fluid.yaml",
            "env": "aws",
            "overlay": "overlays/aws.yaml",
        }

    def test_bundle_resolves_its_recorded_contract(self, layout: Path) -> None:
        tgz = layout / "runtime" / "b.tgz"
        source = make_bundle_source(layout / "contracts" / "p" / "contract.fluid.yaml", tgz)
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src", source=source)
        validate_manifest(tgz)  # the block does not disturb the tamper gate
        assert read_bundle_source(tgz) == source
        assert (
            bundle_source_contract(tgz)
            == (layout / "contracts" / "p" / "contract.fluid.yaml").resolve()
        )

    @pytest.mark.parametrize(
        "recorded",
        [
            "/etc/passwd",
            "../contracts/p/missing.fluid.yaml",
            "../contracts/p/../p/contract.fluid.txt",
            "C:\\\\contracts\\\\p\\\\contract.fluid.yaml",
            "",
            None,
            "../contracts/other/contract.fluid.yaml",
        ],
    )
    def test_an_unusable_recorded_contract_is_ignored(self, layout: Path, recorded: Any) -> None:
        tgz = layout / "runtime" / "b.tgz"
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src")
        _rewrite_source(tgz, {"contract": recorded, "env": None, "overlay": None})
        assert bundle_source_contract(tgz) is None

    def test_a_bundle_without_the_block_warns_when_an_env_is_asked_for(
        self, layout: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tgz = layout / "runtime" / "legacy.tgz"
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src")  # no source recorded
        caplog.set_level(logging.INFO)
        contract = load_contract_with_overlay(str(tgz), "aws", logging.getLogger("t"))
        assert contract["id"] == "demo.src"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("does not record the environment" in r.getMessage() for r in warnings)


class TestBindingPaths:
    def test_relative_path_joins_the_anchor(self, tmp_path: Path) -> None:
        assert resolve_binding_path("./out/x.parquet", tmp_path) == str(
            tmp_path / "./out/x.parquet"
        )

    @pytest.mark.parametrize("path", ["s3://b/k", "gs://b/k", "azure://c/k", "file:///x"])
    def test_remote_uris_are_untouched(self, tmp_path: Path, path: str) -> None:
        assert resolve_binding_path(path, tmp_path) == path

    def test_absolute_path_and_no_anchor_are_untouched(self, tmp_path: Path) -> None:
        assert resolve_binding_path("/abs/x.csv", tmp_path) == "/abs/x.csv"
        assert resolve_binding_path("out/x.csv", None) == "out/x.csv"

    def test_anchoring_a_contract_copies_it(self, tmp_path: Path) -> None:
        anchored = anchor_binding_paths(_CONTRACT, tmp_path)
        assert anchored["exposes"][0]["binding"]["location"]["path"] == str(
            tmp_path / "./out/rows.parquet"
        )
        assert _CONTRACT["exposes"][0]["binding"]["location"]["path"] == "./out/rows.parquet"
