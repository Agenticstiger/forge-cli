# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Phase 4 — ``fluid opds`` CLI integration tests.

Covers the Bitol ODPS v1.0.0 dispatcher (the only supported spec), the
``--out-dir`` bundle writes, the import dispatch by input type, and the
``--no-remote`` flag.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from fluid_build.cli.opds import (
    SPEC_BITOL_1_0_0,
    cmd_opds_export,
    cmd_opds_import,
    resolve_spec,
)

LOG = logging.getLogger(__name__)

FIXTURES = Path(__file__).parent / "fixtures"
MULTI_EXPOSE_FLUID = FIXTURES / "fluid" / "contract-multi-expose.fluid.yaml"
BUNDLE_DIR = FIXTURES / "odps" / "product-bitol"


# ---------------------------------------------------------------------------
# resolve_spec — flag precedence
# ---------------------------------------------------------------------------


class TestResolveSpec:
    def test_default_is_bitol(self) -> None:
        args = Namespace(spec=None)
        assert resolve_spec(args) == SPEC_BITOL_1_0_0

    def test_explicit_bitol_spec(self) -> None:
        args = Namespace(spec=SPEC_BITOL_1_0_0)
        assert resolve_spec(args) == SPEC_BITOL_1_0_0

    def test_unknown_spec_falls_back_to_default(self) -> None:
        args = Namespace(spec="something-else")
        assert resolve_spec(args) == SPEC_BITOL_1_0_0


# ---------------------------------------------------------------------------
# Export — Bitol path
# ---------------------------------------------------------------------------


class TestExportBitol:
    def test_export_bundle_to_out_dir(self, tmp_path: Path) -> None:
        args = Namespace(
            contract=str(MULTI_EXPOSE_FLUID),
            spec=SPEC_BITOL_1_0_0,
            out="-",
            out_dir=str(tmp_path),
            format="yaml",
            env=None,
            validate_strict=False,
        )
        rc = cmd_opds_export(args, LOG)
        assert rc == 0
        odps_files = list(tmp_path.glob("*.odps.yaml"))
        odcs_files = list(tmp_path.glob("*.odcs.yaml"))
        assert len(odps_files) == 1
        assert len(odcs_files) >= 1

    def test_export_bundle_emits_valid_product(self, tmp_path: Path) -> None:
        from fluid_build.providers.odps_standard.validation import (
            load_schema,
            validate,
        )

        args = Namespace(
            contract=str(MULTI_EXPOSE_FLUID),
            spec=SPEC_BITOL_1_0_0,
            out="-",
            out_dir=str(tmp_path),
            format="yaml",
            env=None,
            validate_strict=False,
        )
        cmd_opds_export(args, LOG)

        product_path = next(tmp_path.glob("*.odps.yaml"))
        with open(product_path) as f:
            product = yaml.safe_load(f)
        validate(product, load_schema())  # raises on failure


# ---------------------------------------------------------------------------
# Import dispatch — file / directory / lone ODCS
# ---------------------------------------------------------------------------


def _import_args(path: Path, *, out: Path | None = None, **overrides) -> Namespace:
    base = dict(
        path=str(path),
        spec=SPEC_BITOL_1_0_0,
        out=str(out) if out else None,
        format="yaml",
        no_remote=False,
        lenient=False,
    )
    base.update(overrides)
    return Namespace(**base)


class TestImportDispatch:
    def test_import_odps_file(self, tmp_path: Path) -> None:
        out = tmp_path / "out.fluid.yaml"
        product = next(BUNDLE_DIR.glob("*.odps.yaml"))
        args = _import_args(product, out=out)
        assert cmd_opds_import(args, LOG) == 0
        with open(out) as f:
            fluid = yaml.safe_load(f)
        assert len(fluid["exposes"]) == 2

    def test_import_directory(self, tmp_path: Path) -> None:
        out = tmp_path / "from-dir.fluid.yaml"
        args = _import_args(BUNDLE_DIR, out=out)
        assert cmd_opds_import(args, LOG) == 0
        with open(out) as f:
            fluid = yaml.safe_load(f)
        assert len(fluid["exposes"]) == 2

    def test_import_lone_odcs_file(self, tmp_path: Path) -> None:
        out = tmp_path / "from-odcs.fluid.yaml"
        odcs = next(BUNDLE_DIR.glob("*.odcs.yaml"))
        args = _import_args(odcs, out=out)
        assert cmd_opds_import(args, LOG) == 0
        with open(out) as f:
            fluid = yaml.safe_load(f)
        assert len(fluid["exposes"]) == 1

    def test_import_nonexistent_path_returns_1(self, tmp_path: Path) -> None:
        args = _import_args(tmp_path / "does-not-exist.odps.yaml")
        rc = cmd_opds_import(args, LOG)
        assert rc == 1

    def test_directory_with_two_odps_docs_returns_1(self) -> None:
        broken = FIXTURES / "odps" / "product-bitol-broken" / "two-odps-docs"
        args = _import_args(broken)
        rc = cmd_opds_import(args, LOG)
        assert rc == 1


class TestImportDispatchYieldsEquivalentFluid:
    def test_file_and_directory_imports_are_equivalent(self, tmp_path: Path) -> None:
        out_file = tmp_path / "from-file.fluid.yaml"
        out_dir = tmp_path / "from-dir.fluid.yaml"
        product = next(BUNDLE_DIR.glob("*.odps.yaml"))
        cmd_opds_import(_import_args(product, out=out_file), LOG)
        cmd_opds_import(_import_args(BUNDLE_DIR, out=out_dir), LOG)
        with open(out_file) as f:
            file_fluid = yaml.safe_load(f)
        with open(out_dir) as f:
            dir_fluid = yaml.safe_load(f)
        assert file_fluid == dir_fluid


# ---------------------------------------------------------------------------
# --no-remote propagation
# ---------------------------------------------------------------------------


class TestNoRemoteFlag:
    def test_no_remote_flag_blocks_url_contract_ids(self, tmp_path: Path) -> None:
        # Construct a synthetic ODPS doc whose port contractId is a URL — the
        # resolver will only attempt http(s) fetch; with --no-remote it must
        # fail loud rather than hit the network.
        odps_doc = tmp_path / "synth.odps.yaml"
        odps_doc.write_text(
            yaml.dump(
                {
                    "apiVersion": "v1.0.0",
                    "kind": "DataProduct",
                    "id": "synth.product",
                    "name": "synth-product",
                    "version": "1.0.0",
                    "status": "draft",
                    "outputPorts": [
                        {
                            "name": "remote_only",
                            "version": "1.0.0",
                            "contractId": "https://example.invalid/c.odcs.yaml",
                        }
                    ],
                }
            )
        )
        args = _import_args(odps_doc, out=tmp_path / "fluid.yaml", no_remote=True)
        rc = cmd_opds_import(args, LOG)
        assert rc == 1  # output-port resolution required, no remote allowed
