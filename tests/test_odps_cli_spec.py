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

from fluid_build.cli.odps import (
    LEGACY_SPEC_ODPI_4_1_TOKEN,
    SPEC_BITOL_1_0_0,
    SPEC_ODPI_4_1,
    SPEC_ODPS_4_1,
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

    def test_canonical_odps_4_1_spec_resolves(self) -> None:
        args = Namespace(spec=SPEC_ODPS_4_1)
        assert resolve_spec(args) == SPEC_ODPS_4_1

    def test_legacy_odpi_4_1_token_resolves_with_warning(self, caplog) -> None:
        # ODPI is the org; ODPS is the spec. ``--spec odpi-4.1`` swapped them.
        # Resolver MUST accept the legacy token, return the canonical id, AND
        # emit a WARNING so audit aggregators catch operator scripts that
        # still use the deprecated form.
        args = Namespace(spec=LEGACY_SPEC_ODPI_4_1_TOKEN)
        with caplog.at_level(logging.WARNING, logger="fluid.cli.opds"):
            assert resolve_spec(args) == SPEC_ODPS_4_1
        assert any(
            "deprecated" in rec.message.lower() and "odps-4.1" in rec.message
            for rec in caplog.records
        ), f"expected deprecation WARNING; got {[r.message for r in caplog.records]!r}"

    def test_legacy_version_4_1_resolves_to_odps_4_1_with_warning(self, caplog) -> None:
        args = Namespace(spec=None, version="4.1")
        with caplog.at_level(logging.WARNING, logger="fluid.cli.opds"):
            assert resolve_spec(args) == SPEC_ODPS_4_1
        assert any("deprecated" in rec.message.lower() for rec in caplog.records)

    def test_spec_odpi_back_compat_alias_matches_canonical(self) -> None:
        # Module-level back-compat alias for callers that imported the old name.
        assert SPEC_ODPI_4_1 == SPEC_ODPS_4_1 == "odps-4.1"


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


class TestGenerateStandardFormatRouting:
    """Pin the Bitol-center-stage routing for ``fluid generate standard``.

    Bitol ODPS v1.0.0 is the default ``--format odps``; LF/ODPI ODPS v4.1
    is the opt-in ``--format odps-v4.1``; the historical ``--format opds``
    letter-swap still emits the LF/ODPI v4.1 JSON (its historical default)
    with a deprecation WARNING.
    """

    def test_format_odps_emits_bitol_data_product(self, tmp_path: Path) -> None:
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format

        out = tmp_path / "product.yaml"
        rc = _export_format(
            "odps",
            str(MULTI_EXPOSE_FLUID),
            Namespace(env=None, out=str(out)),
            LOG,
        )
        assert rc == 0
        doc = yaml.safe_load(out.read_text())
        # Bitol DataProduct shape: kind: DataProduct + apiVersion v1.0.0
        assert doc.get("kind") == "DataProduct"
        assert doc.get("apiVersion") == "v1.0.0"
        # MUST NOT be the LF v4.1 wrapper (would have top-level "version: 4.1" + product nested)
        assert doc.get("version") != "4.1"

    def test_format_odps_bitol_alias_resolves_to_bitol(self, tmp_path: Path) -> None:
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format

        out = tmp_path / "explicit.yaml"
        rc = _export_format(
            "odps-bitol",
            str(MULTI_EXPOSE_FLUID),
            Namespace(env=None, out=str(out)),
            LOG,
        )
        assert rc == 0
        doc = yaml.safe_load(out.read_text())
        assert doc.get("kind") == "DataProduct"

    def test_format_odps_v4_1_emits_lf_odpi_spec(self, tmp_path: Path) -> None:
        import json
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format

        out = tmp_path / "product.json"
        rc = _export_format(
            "odps-v4.1",
            str(MULTI_EXPOSE_FLUID),
            Namespace(env=None, out=str(out)),
            LOG,
        )
        assert rc == 0
        doc = json.loads(out.read_text())
        # LF/ODPI v4.1 spec shape: top-level {schema, version, product}
        assert doc.get("version") == "4.1"
        assert "schema" in doc
        assert "product" in doc
        # Must NOT be wrapped in the OdpsProvider envelope:
        assert "artifacts" not in doc, "envelope must be unwrapped"
        assert "opds_version" not in doc, "envelope must be unwrapped"

    def test_format_opds_emits_lf_with_deprecation_warning(self, tmp_path: Path, caplog) -> None:
        import json
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format

        out = tmp_path / "product.json"
        with caplog.at_level(logging.WARNING):
            rc = _export_format(
                "opds",
                str(MULTI_EXPOSE_FLUID),
                Namespace(env=None, out=str(out)),
                LOG,
            )
        assert rc == 0
        doc = json.loads(out.read_text())
        # opds is the deprecated alias of odps-v4.1 — still emits LF/ODPI shape
        assert doc.get("version") == "4.1"
        assert "product" in doc
        # Deprecation event MUST be logged
        assert any(
            "deprecated_format_alias" in rec.message for rec in caplog.records
        ), f"expected deprecated_format_alias event; got {[r.message for r in caplog.records]!r}"


class TestOdpsV41NeverEmitsGarbage:
    """Regression: ``--format odps-v4.1`` must never write the literal ``[]``.

    The pre-2026-06 ``OdpsProvider.render`` caught any mapping exception,
    logged "Failed to process contract", left ``artifacts`` an empty list, and
    let the CLI write the string ``[]`` to disk while exiting 0 — silent
    corruption that looked like success. A common trigger was a bare-string
    ``metadata.owner`` (the extractors indexed it with ``.get()``).
    """

    @staticmethod
    def _string_owner_contract() -> dict:
        """A normal contract whose ``metadata.owner`` is a bare string."""
        return {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "demo.string_owner_v1",
            "name": "String Owner Product",
            "domain": "demo",
            "description": "owner declared as a bare string, not a mapping",
            "metadata": {
                "status": "production",
                "owner": "platform-team",
                "tags": ["demo"],
            },
            "exposes": [
                {
                    "exposeId": "p1",
                    "kind": "table",
                    "description": "primary output",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "D", "schema": "S", "table": "T"},
                    },
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }

    def _write(self, tmp_path: Path, contract: dict) -> Path:
        import json as _json

        cpath = tmp_path / "contract.fluid.yaml"
        cpath.write_text(_json.dumps(contract))  # JSON is valid YAML
        return cpath

    def test_string_owner_exports_valid_non_empty_doc(self, tmp_path: Path) -> None:
        """A bare-string owner now maps cleanly instead of collapsing to []."""
        import json as _json
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format
        from fluid_build.providers.odps.validator import validate_opds_structure

        contract = self._write(tmp_path, self._string_owner_contract())
        out = tmp_path / "product.json"
        rc = _export_format("odps-v4.1", str(contract), Namespace(env=None, out=str(out)), LOG)
        assert rc == 0
        doc = _json.loads(out.read_text())

        # The headline assertion: NOT the literal empty-array garbage.
        assert doc != [], "export wrote the literal '[]' — the swallowed-exception bug is back"
        assert isinstance(doc, dict)
        assert doc.get("version") == "4.1"
        assert "product" in doc
        # The string owner became the DataHolder legalName.
        assert doc["product"]["dataHolder"]["legalName"] == "platform-team"
        # And the whole thing still validates against the vendored LF schema.
        result = validate_opds_structure(doc, version="4.1", use_full_schema=True)
        assert result.get("valid") is True, result.get("errors")

    def test_render_coerces_string_owner_directly(self) -> None:
        from fluid_build.providers.odps.odps import OdpsProvider

        provider = OdpsProvider()
        provider.validate_output = False
        artifact = provider._contract_to_opds(self._string_owner_contract())
        assert artifact["product"]["dataHolder"]["legalName"] == "platform-team"
        assert artifact["product"]["_legacy"]["dataProductOwner"]["name"] == "platform-team"

    def test_total_failure_raises_instead_of_writing_empty_array(self, tmp_path: Path) -> None:
        """When EVERY contract fails to map, render raises — no garbage on disk."""
        from unittest.mock import patch

        from fluid_build.providers.base import ProviderError
        from fluid_build.providers.odps.odps import OdpsProvider

        out = tmp_path / "must-not-exist.json"
        provider = OdpsProvider()
        with patch.object(
            OdpsProvider, "_contract_to_opds", side_effect=ValueError("synthetic unmappable")
        ):
            with pytest.raises(ProviderError) as exc:
                provider.render({"id": "x.y", "name": "Y"}, out=str(out))
        # The real reason is surfaced, not swallowed.
        assert "synthetic unmappable" in str(exc.value)
        assert "no document" in str(exc.value).lower()
        # Crucially: nothing was written.
        assert not out.exists(), "a failed export must not leave a '[]' file behind"

    def test_cli_total_failure_is_nonzero_and_writes_nothing(self, tmp_path: Path) -> None:
        import logging as _logging
        from argparse import Namespace
        from unittest.mock import patch

        from fluid_build.cli import generate_standard
        from fluid_build.cli._common import CLIError
        from fluid_build.providers.odps.odps import OdpsProvider

        contract = self._write(tmp_path, self._string_owner_contract())
        out = tmp_path / "must-not-exist.json"
        args = Namespace(
            standard_format="odps-v4.1",
            contract=str(contract),
            out=str(out),
            env=None,
            list_formats=False,
        )
        with patch.object(OdpsProvider, "_contract_to_opds", side_effect=ValueError("synthetic")):
            with pytest.raises(CLIError) as exc:
                generate_standard.run(args, _logging.getLogger("t"))
        assert exc.value.exit_code != 0
        assert not out.exists()


class TestSupportedFormatsOrdering:
    """The SUPPORTED_FORMATS list and --list output must lead Bitol-first."""

    def test_supported_formats_starts_with_odps(self) -> None:
        from fluid_build.cli.generate_standard import SUPPORTED_FORMATS

        # ``odps`` is the center-stage Bitol default — it must lead the list.
        assert SUPPORTED_FORMATS[0] == "odps"
        # ``odps-bitol`` (explicit alias) immediately after.
        assert SUPPORTED_FORMATS[1] == "odps-bitol"
        # The LF/ODPI option and the deprecated letter-swap come later.
        assert "odps-v4.1" in SUPPORTED_FORMATS
        assert "opds" in SUPPORTED_FORMATS
        # ``opds`` sits at the tail (deprecated).
        assert SUPPORTED_FORMATS[-1] == "opds"

    def test_format_aliases_routing(self) -> None:
        from fluid_build.cli.generate_standard import (
            DEPRECATED_FORMAT_ALIASES,
            FORMAT_ALIASES,
        )

        # odps-bitol → odps (silent, no warning)
        assert FORMAT_ALIASES.get("odps-bitol") == "odps"
        # opds → odps-v4.1 (deprecated; emits the LF/ODPI v4.1 JSON, NOT Bitol)
        assert DEPRECATED_FORMAT_ALIASES.get("opds") == "odps-v4.1"


class TestSchemaRoundTripAcrossFormats:
    """Every ``--format`` emission must validate against the matching upstream
    schema. These are the pins that would have caught the pre-Phase 8 bugs:

    - ``--format odcs`` falling through to a shallow non-conformant skeleton
    - ``--format odps-v4.1`` emitting ``status: "active"`` / ``format: "SQL"``
      outside the LF enum
    - ``--format odps`` (Bitol) emitting structurally invalid product docs

    Each format-→-schema pair runs a real emit + the matching jsonschema
    validator and asserts zero errors.
    """

    def _emit(self, fmt: str, tmp_path: Path, suffix: str) -> Path:
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format

        out = tmp_path / f"product.{suffix}"
        rc = _export_format(
            fmt,
            str(MULTI_EXPOSE_FLUID),
            Namespace(env=None, out=str(out)),
            LOG,
        )
        assert rc == 0, f"emit --format {fmt} failed: rc={rc}"
        return out

    def test_format_odps_bitol_validates_against_bitol_v1_0_0_schema(self, tmp_path: Path) -> None:
        from fluid_build.providers.odps_standard.validation import (
            load_schema,
            validate,
        )

        out = self._emit("odps", tmp_path, "yaml")
        doc = yaml.safe_load(out.read_text())
        schema = load_schema()
        assert schema is not None, "vendored Bitol schema missing"
        # Bitol validate() raises on schema failure; the test passes if it
        # returns silently.
        validate(doc, schema)

    def test_format_odps_v4_1_validates_against_lf_schema(self, tmp_path: Path) -> None:
        import json as _json

        from fluid_build.providers.odps.validator import validate_opds_structure

        out = self._emit("odps-v4.1", tmp_path, "json")
        doc = _json.loads(out.read_text())
        result = validate_opds_structure(doc, version="4.1", use_full_schema=True)
        assert result.get("valid") is True, (
            f"LF/ODPI ODPS v4.1 full-schema validation failed: " f"{result.get('errors')}"
        )

    def test_format_odcs_multidoc_yaml_per_port_validates(self, tmp_path: Path) -> None:
        """Single-file mode emits one YAML doc per port (---separated); all valid."""
        from fluid_build.providers.odcs.validation import collect_errors, load_schema

        out = self._emit("odcs", tmp_path, "yaml")
        docs = list(yaml.safe_load_all(out.read_text()))
        schema = load_schema()
        assert schema is not None, "vendored ODCS schema missing"
        assert len(docs) >= 1, "expected at least one ODCS doc"
        for i, doc in enumerate(docs):
            assert doc.get("kind") == "DataContract", f"doc {i} wrong kind"
            assert doc.get("apiVersion") == "v3.1.0", f"doc {i} wrong apiVersion"
            errors = collect_errors(doc, schema)
            assert errors == [], f"ODCS doc {i} schema errors: {errors}"

    def test_format_odcs_directory_mode_per_port_validates(self, tmp_path: Path) -> None:
        """Directory mode (--out dir/) emits one file per port; all valid."""
        from argparse import Namespace

        from fluid_build.cli.generate_standard import _export_format
        from fluid_build.providers.odcs.validation import collect_errors, load_schema

        out_dir = tmp_path / "ports"
        rc = _export_format(
            "odcs",
            str(MULTI_EXPOSE_FLUID),
            Namespace(env=None, out=str(out_dir) + "/"),
            LOG,
        )
        assert rc == 0
        files = sorted(out_dir.glob("product.odcs.*.yaml"))
        assert len(files) >= 1, "expected per-port files"
        schema = load_schema()
        for fp in files:
            doc = yaml.safe_load(fp.read_text())
            errors = collect_errors(doc, schema)
            assert errors == [], f"{fp.name}: {errors}"


class TestInputPortDuplicateExposeIdHandling:
    """Gap 3 regression: when two ``consumes[]`` share an ``exposeId`` (e.g.
    both upstream products expose ``data_analytics_platform``), the second
    must NOT be silently dropped — they share a base name but resolve to
    different upstream productIds, so the rendered InputPort list needs
    distinguishing names."""

    def test_consumes_with_duplicate_expose_id_keeps_both_input_ports(self, tmp_path: Path) -> None:
        from fluid_build.providers.odps_standard import OdpsStandardProvider

        synthetic = {
            "fluidVersion": "0.7.3",
            "id": "gold.synthetic.cdp",
            "kind": "DataProduct",
            "name": "Synthetic CDP",
            "domain": "synthetic",
            "metadata": {"layer": "Gold", "owner": {"team": "synthetic-team"}},
            "exposes": [
                {
                    "exposeId": "synthetic_out",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
            "consumes": [
                {
                    "productId": "silver.synthetic.upstream_a",
                    "exposeId": "shared_port",
                    "purpose": "upstream A",
                },
                {
                    "productId": "silver.synthetic.upstream_b",
                    "exposeId": "shared_port",  # SAME exposeId as A!
                    "purpose": "upstream B",
                },
            ],
        }

        doc = OdpsStandardProvider().render(synthetic)
        ports = doc.get("inputPorts", []) or []
        assert len(ports) == 2, (
            f"expected 2 inputPorts (both consumes preserved) — got {len(ports)}: "
            f"{[p.get('name') for p in ports]}"
        )
        contract_ids = {p.get("contractId") for p in ports}
        assert "silver.synthetic.upstream_a" in contract_ids
        assert "silver.synthetic.upstream_b" in contract_ids
        # Names must be unique within the product.
        names = [p.get("name") for p in ports]
        assert len(set(names)) == 2, f"inputPort names not unique: {names}"


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
