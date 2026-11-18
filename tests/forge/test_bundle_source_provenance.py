# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The bundle's ``source`` record, and how far a reader trusts it.

The record is split: ``env`` and the overlay warnings sit in the MANIFEST
``source`` block, the checkout paths in ``<bundle>.provenance.json`` beside the
archive, so the archive's bytes do not depend on ``--out``. Both are outside the
merkle root (like ``contractId``), so readers treat them as hints: a recorded
source contract is used only when it is a relative path to an existing
``.yaml`` / ``.yml`` / ``.json`` file that declares the bundle's id.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build._contract_loader import load_contract_with_overlay, source_contract_dir
from fluid_build.forge.core.bundle import (
    MAX_PROVENANCE_BYTES,
    PROVENANCE_KEYS,
    build_bundle_tgz,
    bundle_source_contract,
    make_bundle_source,
    packet_digest,
    provenance_path,
    read_bundle_provenance,
    read_bundle_source,
    resolve_bundle_source_contract,
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


def _rewrite_manifest_source(tgz: Path, block: Dict[str, Any]) -> None:
    """Edit the MANIFEST source block in place (it is outside the merkle root)."""
    with tarfile.open(tgz, "r:gz") as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    manifest = json.loads(members["MANIFEST.json"])
    manifest["source"] = block
    members["MANIFEST.json"] = json.dumps(manifest).encode("utf-8")
    with tarfile.open(tgz, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)

            tar.addfile(info, io.BytesIO(data))


def _write_sidecar(tgz: Path, recorded: Dict[str, Any], **over: Any) -> None:
    """A provenance file for the bundle as it is now (outside every digest)."""
    document: Dict[str, Any] = {
        "version": "1",
        "packetDigest": packet_digest(tgz),
        "source": recorded,
    }
    document.update(over)
    provenance_path(tgz).write_text(json.dumps(document), encoding="utf-8")


def _rewrite_source(tgz: Path, source: Dict[str, Any]) -> None:
    """Edit the whole record: env into the MANIFEST, the paths into the provenance file."""
    _rewrite_manifest_source(tgz, {k: v for k, v in source.items() if k not in PROVENANCE_KEYS})
    _write_sidecar(tgz, {k: source.get(k) for k in PROVENANCE_KEYS})


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


class TestOverlayThatSetsTheId:
    """An overlay may give a product an env-specific id.

    The bundled contract then carries the OVERLAY's id, and the recorded
    source file declares the base one. Comparing those two refused the one
    contract the bundle was built from, so every stage anchored at the
    bundle's directory instead (data landed under ``runtime/``).
    """

    def _staging_bundle(self, layout: Path) -> Path:
        overlays = layout / "contracts" / "p" / "overlays"
        overlays.mkdir()
        (overlays / "staging.yaml").write_text("id: demo.src_staging\n", encoding="utf-8")
        tgz = layout / "runtime" / "staging.tgz"
        source = make_bundle_source(
            layout / "contracts" / "p" / "contract.fluid.yaml",
            tgz,
            env="staging",
            overlay_path=overlays / "staging.yaml",
        )
        staged = dict(_CONTRACT, id="demo.src_staging")
        build_bundle_tgz(staged, tgz, contract_id="demo.src_staging", source=source)
        return tgz

    def test_the_recorded_contract_resolves_with_its_overlay_applied(self, layout: Path) -> None:
        tgz = self._staging_bundle(layout)
        assert (
            bundle_source_contract(tgz)
            == (layout / "contracts" / "p" / "contract.fluid.yaml").resolve()
        )

    def test_another_env_cannot_borrow_the_overlays_id(self, layout: Path) -> None:
        # The staging overlay is what makes the ids agree: recorded as a
        # different env, the same file is not the bundle's source.
        tgz = self._staging_bundle(layout)
        _rewrite_source(
            tgz,
            {"contract": "../contracts/p/contract.fluid.yaml", "env": "prod", "overlay": None},
        )
        assert bundle_source_contract(tgz) is None

    @pytest.mark.parametrize("env", ["../p/overlays/staging", "overlays/../staging", 7])
    def test_an_env_that_is_not_a_name_is_refused(self, layout: Path, env: Any) -> None:
        tgz = self._staging_bundle(layout)
        _rewrite_source(
            tgz, {"contract": "../contracts/p/contract.fluid.yaml", "env": env, "overlay": None}
        )
        assert bundle_source_contract(tgz) is None


class TestProvenanceFile:
    """The checkout paths live beside the archive, never in it."""

    def _bundle(self, layout: Path, rel: str) -> Path:
        tgz = layout / rel
        tgz.parent.mkdir(parents=True, exist_ok=True)
        source = make_bundle_source(
            layout / "contracts" / "p" / "contract.fluid.yaml", tgz, env="aws"
        )
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src", source=source)
        return tgz

    def test_the_packet_bytes_do_not_depend_on_the_out_depth(self, layout: Path) -> None:
        shallow = self._bundle(layout, "runtime/b.tgz")
        deep = self._bundle(layout, "runtime/x/y/z/b.tgz")
        assert shallow.read_bytes() == deep.read_bytes()
        assert packet_digest(shallow) == packet_digest(deep)
        # Each provenance file names the contract from where its bundle is.
        assert read_bundle_source(shallow)["contract"] == "../contracts/p/contract.fluid.yaml"
        assert read_bundle_source(deep)["contract"] == "../../../../contracts/p/contract.fluid.yaml"
        for tgz in (shallow, deep):
            assert (
                bundle_source_contract(tgz)
                == (layout / "contracts" / "p" / "contract.fluid.yaml").resolve()
            )

    def test_the_manifest_holds_no_checkout_path(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        with tarfile.open(tgz, "r:gz") as tar:
            manifest = json.loads(tar.extractfile("MANIFEST.json").read())
        assert manifest["source"] == {"env": "aws"}
        sidecar = json.loads(provenance_path(tgz).read_text(encoding="utf-8"))
        assert sidecar == {
            "bundleDigest": manifest["digest"],
            "packetDigest": packet_digest(tgz),
            "source": {"contract": "../contracts/p/contract.fluid.yaml", "overlay": None},
            "version": "1",
        }

    def test_a_bundle_copied_without_it_anchors_at_its_own_directory(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        provenance_path(tgz).unlink()
        assert read_bundle_source(tgz) == {"env": "aws", "contract": None, "overlay": None}
        path, why = resolve_bundle_source_contract(tgz)
        assert path is None and "b.tgz.provenance.json" in why

    def test_a_file_naming_another_packet_is_not_read(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        stale = provenance_path(tgz).read_bytes()
        build_bundle_tgz(
            {**_CONTRACT, "name": "Changed"}, tgz, contract_id="demo.src", source={"env": None}
        )
        provenance_path(tgz).write_bytes(stale)  # left over from the first build
        assert read_bundle_provenance(tgz) is None

    @pytest.mark.parametrize(
        "over",
        [{"version": "2"}, {"packetDigest": "sha256:" + "0" * 64}, {"source": "x"}],
    )
    def test_a_file_that_is_not_this_format_is_not_read(
        self, layout: Path, over: Dict[str, Any]
    ) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        _write_sidecar(tgz, {"contract": "../contracts/p/contract.fluid.yaml"}, **over)
        assert read_bundle_provenance(tgz) is None

    @pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b"[]"])
    def test_an_unreadable_file_is_not_read(self, layout: Path, raw: bytes) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        provenance_path(tgz).write_bytes(raw)
        assert read_bundle_provenance(tgz) is None

    def test_an_oversized_file_is_not_read(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        _write_sidecar(
            tgz, {"contract": "../contracts/p/contract.fluid.yaml"}, pad="x" * MAX_PROVENANCE_BYTES
        )
        assert read_bundle_provenance(tgz) is None

    def test_a_value_that_is_not_a_string_reads_as_none(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        _write_sidecar(tgz, {"contract": ["../contracts/p/contract.fluid.yaml"], "overlay": 7})
        assert read_bundle_provenance(tgz) == {"contract": None, "overlay": None}

    def test_a_path_inside_the_manifest_is_never_read(self, layout: Path) -> None:
        # An older bundle recorded the contract in the MANIFEST; it is ignored.
        tgz = self._bundle(layout, "runtime/b.tgz")
        provenance_path(tgz).unlink()
        _rewrite_manifest_source(
            tgz, {"contract": "../contracts/p/contract.fluid.yaml", "env": None, "overlay": None}
        )
        assert read_bundle_source(tgz) == {"env": None, "contract": None, "overlay": None}
        assert bundle_source_contract(tgz) is None

    def test_a_bundle_built_without_a_source_removes_a_stale_file(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        assert provenance_path(tgz).is_file()
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src")
        assert not provenance_path(tgz).exists()
        assert read_bundle_source(tgz) is None

    def test_it_is_written_owner_only(self, layout: Path) -> None:
        tgz = self._bundle(layout, "runtime/b.tgz")
        assert provenance_path(tgz).stat().st_mode & 0o777 == 0o600

    def test_a_symlink_at_the_name_is_replaced_not_followed(self, layout: Path) -> None:
        tgz = layout / "runtime" / "b.tgz"
        victim = layout / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        provenance_path(tgz).symlink_to(victim)
        self._bundle(layout, "runtime/b.tgz")
        assert victim.read_text(encoding="utf-8") == "keep"
        assert not provenance_path(tgz).is_symlink()
        assert read_bundle_provenance(tgz) is not None


class TestSemanticIdentity:
    """The merkle ``digest`` names the resolved contract and its inputs, nothing else."""

    def test_a_changed_inline_sql_changes_it(self, layout: Path) -> None:
        def build(sql: str, name: str) -> str:
            contract = {
                **_CONTRACT,
                "builds": [{"id": "b", "engine": "sql", "properties": {"sql": sql}}],
            }
            return build_bundle_tgz(contract, layout / "runtime" / name, contract_id="demo.src")

        assert build("SELECT 1", "a.tgz") == build("SELECT 1", "b.tgz")
        assert build("SELECT 1", "a.tgz") != build("SELECT 2", "c.tgz")

    def test_a_changed_dbt_model_changes_it(self, layout: Path) -> None:
        def build(model: bytes, name: str) -> str:
            return build_bundle_tgz(
                _CONTRACT,
                layout / "runtime" / name,
                contract_id="demo.src",
                dbt_projects={"b": {"dbt_project.yml": b"name: p\n", "models/m.sql": model}},
            )

        assert build(b"select 1", "a.tgz") == build(b"select 1", "b.tgz")
        assert build(b"select 1", "a.tgz") != build(b"select 2", "c.tgz")


class TestFallbackSaysWhichCheckFailed:
    def test_an_id_mismatch_is_not_reported_as_unrecorded(
        self, layout: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tgz = layout / "runtime" / "b.tgz"
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src")
        _rewrite_source(
            tgz,
            {"contract": "../contracts/other/contract.fluid.yaml", "env": None, "overlay": None},
        )
        caplog.set_level(logging.WARNING)
        assert source_contract_dir(tgz, logging.getLogger("t")) == (layout / "runtime").resolve()
        message = " ".join(r.getMessage() for r in caplog.records)
        assert "declares id 'someone.else'" in message
        assert "'demo.src'" in message
        assert "does not record" not in message

    def test_a_missing_contract_says_so(
        self, layout: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tgz = layout / "runtime" / "b.tgz"
        build_bundle_tgz(_CONTRACT, tgz, contract_id="demo.src")
        _rewrite_source(
            tgz, {"contract": "../contracts/gone/contract.fluid.yaml", "env": None, "overlay": None}
        )
        caplog.set_level(logging.WARNING)
        source_contract_dir(tgz, logging.getLogger("t"))
        assert "which no longer exists" in " ".join(r.getMessage() for r in caplog.records)


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
