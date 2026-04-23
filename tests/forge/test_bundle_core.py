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

"""Tests for the deterministic tgz bundle builder.

Adversarial bias: every test pins a specific invariant that downstream
pipeline stages depend on. If one of these tests starts passing under a
definition change, that's how we know the bundle format drifted and
downstream hashes are about to diverge.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from fluid_build.forge.core.bundle import (
    MANIFEST_VERSION,
    SOURCE_SENTINEL,
    build_bundle_tgz,
    build_manifest,
    extract_fragments,
    validate_manifest,
    write_tgz,
)

# ---------------------------------------------------------------------------
# extract_fragments
# ---------------------------------------------------------------------------


class TestExtractFragmentsBuilds:
    """builds[N].embeddedLogicPattern.sql → sources/sql/builds_N__{id}.sql."""

    def test_extracts_single_build_sql(self):
        contract = {
            "builds": [
                {
                    "id": "orders-transform",
                    "embeddedLogicPattern": {"sql": "SELECT * FROM raw_orders"},
                }
            ]
        }
        rewritten, sources = extract_fragments(contract)
        assert sources == {
            "sources/sql/builds_0__orders-transform.sql": b"SELECT * FROM raw_orders\n"
        }
        assert rewritten["builds"][0]["embeddedLogicPattern"]["sql"] == {
            SOURCE_SENTINEL: "sources/sql/builds_0__orders-transform.sql"
        }

    def test_extraction_does_not_mutate_input(self):
        contract = {"builds": [{"id": "x", "embeddedLogicPattern": {"sql": "SELECT 1"}}]}
        snapshot = {"builds": [{"id": "x", "embeddedLogicPattern": {"sql": "SELECT 1"}}]}
        _rewritten, _sources = extract_fragments(contract)
        assert contract == snapshot, "extract_fragments must deep-copy; input was mutated"

    def test_multiple_builds_get_distinct_paths(self):
        contract = {
            "builds": [
                {"id": "a", "embeddedLogicPattern": {"sql": "SELECT 1"}},
                {"id": "b", "embeddedLogicPattern": {"sql": "SELECT 2"}},
            ]
        }
        _, sources = extract_fragments(contract)
        assert set(sources.keys()) == {
            "sources/sql/builds_0__a.sql",
            "sources/sql/builds_1__b.sql",
        }

    def test_same_id_different_index_does_not_collide(self):
        """Two builds with the same id at different array positions must not
        collide — the array index prefix is the safety net."""
        contract = {
            "builds": [
                {"id": "default", "embeddedLogicPattern": {"sql": "SELECT 1"}},
                {"id": "default", "embeddedLogicPattern": {"sql": "SELECT 2"}},
            ]
        }
        _, sources = extract_fragments(contract)
        assert set(sources.keys()) == {
            "sources/sql/builds_0__default.sql",
            "sources/sql/builds_1__default.sql",
        }
        # Contents must match their ORIGINAL SQL, not swap.
        assert b"SELECT 1" in sources["sources/sql/builds_0__default.sql"]
        assert b"SELECT 2" in sources["sources/sql/builds_1__default.sql"]

    def test_missing_id_falls_back_to_index(self):
        contract = {"builds": [{"embeddedLogicPattern": {"sql": "SELECT 1"}}]}
        _, sources = extract_fragments(contract)
        # No id → fallback "build0" (includes index + a readable tag)
        assert "sources/sql/builds_0__build0.sql" in sources

    def test_non_string_sql_is_passthrough_not_extracted(self):
        """If ``sql`` is already a ``$source`` pointer (re-bundling case), the
        extractor must treat it as structural data, NOT re-extract it into
        a nested pointer."""
        contract = {
            "builds": [
                {
                    "id": "x",
                    "embeddedLogicPattern": {
                        "sql": {SOURCE_SENTINEL: "sources/sql/builds_0__x.sql"}
                    },
                }
            ]
        }
        _, sources = extract_fragments(contract)
        assert sources == {}, f"expected no extraction; got {sources}"

    def test_build_without_embedded_logic_pattern_ignored(self):
        contract = {"builds": [{"id": "x", "engine": "dbt"}]}
        rewritten, sources = extract_fragments(contract)
        assert sources == {}
        assert rewritten == contract

    def test_builds_not_a_list_ignored(self):
        # Defensive: malformed contract shouldn't crash the extractor.
        contract = {"builds": {"not": "a list"}}
        rewritten, sources = extract_fragments(contract)
        assert sources == {}
        assert rewritten == contract


class TestExtractFragmentsExposes:
    """exposes[N].view.sql → sources/sql/exposes_N__{id}__view.sql; inline
    openapi → sources/openapi/."""

    def test_extracts_view_sql(self):
        contract = {"exposes": [{"id": "orders-api", "view": {"sql": "SELECT id FROM orders"}}]}
        _, sources = extract_fragments(contract)
        assert sources == {
            "sources/sql/exposes_0__orders-api__view.sql": b"SELECT id FROM orders\n"
        }

    def test_extracts_inline_openapi_dict(self):
        spec = {"openapi": "3.0.0", "info": {"title": "Orders API", "version": "1.0"}}
        contract = {"exposes": [{"id": "orders-api", "openapi": spec}]}
        rewritten, sources = extract_fragments(contract)
        assert "sources/openapi/exposes_0__orders-api.yaml" in sources
        assert rewritten["exposes"][0]["openapi"] == {
            SOURCE_SENTINEL: "sources/openapi/exposes_0__orders-api.yaml"
        }
        # Content must be valid YAML of the inline spec
        import yaml

        assert yaml.safe_load(sources["sources/openapi/exposes_0__orders-api.yaml"]) == spec

    def test_openapiref_is_NOT_extracted(self):
        """openapiRef is an external pointer; it's resolved via $ref before
        bundling, not extracted here. The extractor must leave it alone."""
        contract = {"exposes": [{"id": "orders-api", "openapiRef": "https://ex/spec.yaml"}]}
        _rewritten, sources = extract_fragments(contract)
        assert sources == {}

    def test_view_and_openapi_both_extracted(self):
        contract = {
            "exposes": [
                {
                    "id": "orders-api",
                    "view": {"sql": "SELECT 1"},
                    "openapi": "openapi: 3.0.0",
                }
            ]
        }
        _, sources = extract_fragments(contract)
        assert set(sources.keys()) == {
            "sources/sql/exposes_0__orders-api__view.sql",
            "sources/openapi/exposes_0__orders-api.yaml",
        }

    def test_slug_normalizes_spaces_and_parens(self):
        contract = {"exposes": [{"id": "My Expose (v2)", "view": {"sql": "SELECT 1"}}]}
        _, sources = extract_fragments(contract)
        assert "sources/sql/exposes_0__My-Expose-v2__view.sql" in sources


class TestExtractFragmentsMixed:
    def test_builds_and_exposes_in_one_contract(self):
        contract = {
            "id": "test-product",
            "builds": [{"id": "build-a", "embeddedLogicPattern": {"sql": "SELECT 1"}}],
            "exposes": [{"id": "expose-a", "view": {"sql": "SELECT 2"}}],
        }
        rewritten, sources = extract_fragments(contract)
        assert set(sources.keys()) == {
            "sources/sql/builds_0__build-a.sql",
            "sources/sql/exposes_0__expose-a__view.sql",
        }
        # Non-sql keys preserved untouched
        assert rewritten["id"] == "test-product"


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------


class TestBuildManifest:
    def test_per_file_sha256(self):
        files = {"a.txt": b"hello", "b.txt": b"world"}
        m = build_manifest(files)
        assert m["files"]["a.txt"] == ("sha256:" + hashlib.sha256(b"hello").hexdigest())
        assert m["files"]["b.txt"] == ("sha256:" + hashlib.sha256(b"world").hexdigest())

    def test_merkle_is_sha256_of_sorted_path_hash_lines(self):
        files = {"b.txt": b"B", "a.txt": b"A"}
        m = build_manifest(files)
        # Independently compute expected merkle: sorted "path:hash\n" lines
        lines = [
            f"a.txt:sha256:{hashlib.sha256(b'A').hexdigest()}\n",
            f"b.txt:sha256:{hashlib.sha256(b'B').hexdigest()}\n",
        ]
        expected = "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()
        assert m["digest"] == expected

    def test_version_field_present(self):
        assert build_manifest({})["version"] == MANIFEST_VERSION

    def test_contract_id_is_propagated(self):
        m = build_manifest({}, contract_id="my-product")
        assert m["contractId"] == "my-product"


# ---------------------------------------------------------------------------
# write_tgz — deterministic
# ---------------------------------------------------------------------------


class TestWriteTgzDeterminism:
    def test_two_writes_are_byte_identical(self, tmp_path):
        files = {
            "contract.resolved.yaml": b"id: foo\nkey: value\n",
            "sources/sql/a.sql": b"SELECT 1\n",
        }
        a = tmp_path / "a.tgz"
        b = tmp_path / "b.tgz"
        write_tgz(a, files)
        write_tgz(b, files)
        assert (
            a.read_bytes() == b.read_bytes()
        ), "repeated writes of the same files must produce byte-identical tgz"

    def test_output_path_does_not_leak_into_gzip_filename_field(self, tmp_path):
        """The #1 non-determinism trap: gzip stamps the source filename into
        FNAME. If write_tgz accidentally lets that happen, writing the SAME
        files to TWO different paths produces different tgz bytes."""
        files = {"x.txt": b"same data\n"}
        diff_name_1 = tmp_path / "completely-different-name.tgz"
        diff_name_2 = tmp_path / "another-name.tgz"
        write_tgz(diff_name_1, files)
        write_tgz(diff_name_2, files)
        assert diff_name_1.read_bytes() == diff_name_2.read_bytes(), (
            "tgz bytes must not depend on the output file's basename — "
            "gzip FNAME field is a determinism landmine"
        )

    def test_entries_are_sorted_in_tar(self, tmp_path):
        files = {"z.txt": b"zz", "a.txt": b"aa", "m.txt": b"mm"}
        out = tmp_path / "out.tgz"
        write_tgz(out, files)
        with tarfile.open(out, "r:gz") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
        assert names == sorted(names), f"tar entries must be sorted for determinism; got {names}"

    def test_headers_normalized(self, tmp_path):
        files = {"x.txt": b"data\n"}
        out = tmp_path / "out.tgz"
        write_tgz(out, files)
        with tarfile.open(out, "r:gz") as tar:
            m = tar.getmember("x.txt")
        assert m.mtime == 0, f"mtime must be 0 (or SOURCE_DATE_EPOCH); got {m.mtime}"
        assert m.uid == 0 and m.gid == 0, "uid/gid must be 0"
        assert m.uname == "" and m.gname == "", "uname/gname must be blank"
        assert m.mode == 0o644, f"file mode must be 0o644; got {oct(m.mode)}"


# ---------------------------------------------------------------------------
# validate_manifest
# ---------------------------------------------------------------------------


class TestValidateManifest:
    def _build_bundle(self, tmp_path: Path, **kwargs) -> Path:
        contract = kwargs.get(
            "contract",
            {
                "id": "test",
                "builds": [{"id": "b1", "embeddedLogicPattern": {"sql": "SELECT 1"}}],
            },
        )
        out = tmp_path / "b.tgz"
        build_bundle_tgz(contract, out, contract_id="test")
        return out

    def test_clean_bundle_validates(self, tmp_path):
        tgz = self._build_bundle(tmp_path)
        # Must not raise
        validate_manifest(tgz)

    def test_tampered_sql_raises(self, tmp_path):
        tgz = self._build_bundle(tmp_path)

        # Decompress, tamper the SQL fragment, recompress.
        tar_bytes = gzip.decompress(tgz.read_bytes())
        tampered_tar = tar_bytes.replace(b"SELECT 1", b"SELECT 2")
        assert tampered_tar != tar_bytes, "tamper substitution didn't hit"
        tampered_gz = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=tampered_gz, mode="wb", mtime=0) as gz:
            gz.write(tampered_tar)
        tgz.write_bytes(tampered_gz.getvalue())

        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            validate_manifest(tgz)

    def test_missing_manifest_raises(self, tmp_path):
        # Build a tgz WITHOUT a MANIFEST.json
        import tempfile

        out = tmp_path / "nomanifest.tgz"
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo("x.txt")
            info.size = 5
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(b"hello"))
        gz_buf = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(tar_buf.getvalue())
        out.write_bytes(gz_buf.getvalue())

        with pytest.raises(ValueError, match="MANIFEST.json missing"):
            validate_manifest(out)

    def test_extra_file_not_in_manifest_raises(self, tmp_path):
        """Adding a rogue file after bundle must fail validation."""
        tgz = self._build_bundle(tmp_path)
        tar_bytes = gzip.decompress(tgz.read_bytes())

        # Append an unexpected file by rebuilding the tar with an extra entry.
        # Simplest: extract, add, repack.
        src = io.BytesIO(tar_bytes)
        dst = io.BytesIO()
        with tarfile.open(fileobj=src, mode="r") as sr, tarfile.open(fileobj=dst, mode="w") as dw:
            for member in sr.getmembers():
                fh = sr.extractfile(member) if member.isfile() else None
                dw.addfile(member, fh)
            # Add rogue entry
            info = tarfile.TarInfo("rogue.txt")
            info.size = 5
            info.mtime = 0
            dw.addfile(info, io.BytesIO(b"boom!"))
        gz_buf = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(dst.getvalue())
        tgz.write_bytes(gz_buf.getvalue())

        with pytest.raises(ValueError, match="not declared in MANIFEST"):
            validate_manifest(tgz)


# ---------------------------------------------------------------------------
# build_bundle_tgz — end-to-end
# ---------------------------------------------------------------------------


class TestBuildBundleTgzEndToEnd:
    def test_returns_merkle_digest(self, tmp_path):
        contract = {"id": "t", "builds": [{"id": "x", "embeddedLogicPattern": {"sql": "SELECT 1"}}]}
        out = tmp_path / "b.tgz"
        digest = build_bundle_tgz(contract, out, contract_id="t")
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64  # sha256 hex len

    def test_manifest_embeds_contract_id(self, tmp_path):
        contract = {"id": "my-product"}
        out = tmp_path / "b.tgz"
        build_bundle_tgz(contract, out, contract_id="my-product")
        with tarfile.open(out, "r:gz") as tar:
            manifest = json.loads(tar.extractfile("MANIFEST.json").read())
        assert manifest["contractId"] == "my-product"

    def test_contract_resolved_carries_source_sentinels(self, tmp_path):
        contract = {
            "id": "t",
            "builds": [{"id": "x", "embeddedLogicPattern": {"sql": "SELECT 1"}}],
        }
        out = tmp_path / "b.tgz"
        build_bundle_tgz(contract, out)
        with tarfile.open(out, "r:gz") as tar:
            data = tar.extractfile("contract.resolved.json").read()
        resolved = json.loads(data)
        # The sql field should be a $source pointer, not a string
        sql_field = resolved["builds"][0]["embeddedLogicPattern"]["sql"]
        assert sql_field == {SOURCE_SENTINEL: "sources/sql/builds_0__x.sql"}

    def test_repeat_build_is_byte_identical(self, tmp_path):
        contract = {
            "id": "t",
            "builds": [{"id": "x", "embeddedLogicPattern": {"sql": "SELECT 1"}}],
            "exposes": [{"id": "y", "view": {"sql": "SELECT 2"}}],
        }
        a = tmp_path / "a.tgz"
        b = tmp_path / "b.tgz"
        da = build_bundle_tgz(contract, a, contract_id="t")
        db = build_bundle_tgz(contract, b, contract_id="t")
        assert a.read_bytes() == b.read_bytes()
        assert da == db

    def test_manifest_is_valid_json_and_deterministic(self, tmp_path):
        contract = {"id": "t"}
        out = tmp_path / "b.tgz"
        build_bundle_tgz(contract, out, contract_id="t")
        with tarfile.open(out, "r:gz") as tar:
            mb = tar.extractfile("MANIFEST.json").read()
        # Parse check
        parsed = json.loads(mb)
        assert parsed["version"] == MANIFEST_VERSION
        assert parsed["digest"].startswith("sha256:")
        # Canonical: keys sorted
        keys_sorted = sorted(parsed.keys())
        assert (
            list(parsed.keys()) == keys_sorted
        ), "MANIFEST.json keys must be sorted for determinism"


# ---------------------------------------------------------------------------
# CLI integration: product-id-defaulted bundle filename
# ---------------------------------------------------------------------------


class TestCliBundleDefaultFilename:
    """``fluid bundle --format tgz`` without ``--out`` must write to a
    product-named default like ``<product-id>.fluid.bundle.tgz``.

    Bundles travel outside the product folder (CI artifact stores, S3,
    catalog publish); without the product name in the filename a bin of 50
    bundles is 50 × ``bundle.tgz`` with no way to distinguish.
    """

    def _write_contract(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "contract.fluid.yaml"
        p.write_text(body)
        return p

    def test_default_filename_uses_contract_id(self, tmp_path, monkeypatch):
        contract_path = self._write_contract(
            tmp_path,
            "fluidVersion: '0.7.2'\nkind: DataProduct\nid: customer-360\n",
        )

        import argparse
        import logging

        from fluid_build.cli.bundle import run

        args = argparse.Namespace(
            contract=str(contract_path),
            out="-",
            env=None,
            format="tgz",
        )
        logger = logging.getLogger("test")

        rc = run(args, logger)
        assert rc == 0

        expected = tmp_path / "customer-360.fluid.bundle.tgz"
        assert expected.exists(), (
            f"expected default bundle at {expected}; " f"got {list(tmp_path.iterdir())}"
        )

    def test_default_filename_slugifies_nonfilesafe_ids(self, tmp_path):
        # IDs with spaces / parens / uppercase must slugify into something
        # filesystem-safe without colliding with sibling products.
        contract_path = self._write_contract(
            tmp_path,
            "fluidVersion: '0.7.2'\nkind: DataProduct\nid: 'My Product (v2)'\n",
        )

        import argparse
        import logging

        from fluid_build.cli.bundle import run

        args = argparse.Namespace(
            contract=str(contract_path),
            out="-",
            env=None,
            format="tgz",
        )
        logger = logging.getLogger("test")

        rc = run(args, logger)
        assert rc == 0
        assert (tmp_path / "My-Product-v2.fluid.bundle.tgz").exists()

    def test_explicit_out_overrides_default(self, tmp_path):
        contract_path = self._write_contract(
            tmp_path,
            "fluidVersion: '0.7.2'\nkind: DataProduct\nid: x\n",
        )

        import argparse
        import logging

        from fluid_build.cli.bundle import run

        explicit = tmp_path / "custom-path.tgz"
        args = argparse.Namespace(
            contract=str(contract_path),
            out=str(explicit),
            env=None,
            format="tgz",
        )
        logger = logging.getLogger("test")

        rc = run(args, logger)
        assert rc == 0
        assert explicit.exists()
        # The product-id default must NOT have been created too
        assert not (tmp_path / "x.fluid.bundle.tgz").exists()

    def test_missing_contract_id_falls_back_to_contract_name(self, tmp_path):
        # Contract has no id/name at top level — bundle filename must still
        # not crash; falls back to a generic name.
        contract_path = self._write_contract(
            tmp_path,
            "fluidVersion: '0.7.2'\nkind: DataProduct\n",
        )

        import argparse
        import logging

        from fluid_build.cli.bundle import run

        args = argparse.Namespace(
            contract=str(contract_path),
            out="-",
            env=None,
            format="tgz",
        )
        logger = logging.getLogger("test")

        rc = run(args, logger)
        assert rc == 0
        # Should land at the generic fallback name.
        assert (tmp_path / "contract.fluid.bundle.tgz").exists()
