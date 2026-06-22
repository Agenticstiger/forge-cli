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

"""
ODPS CLI — two specifications under one user-facing command.

Two distinct standards both abbreviated **ODPS** are dispatched via ``--spec``:

- ``bitol-1.0.0`` (**default**) — Bitol Open Data Product **Standard** v1.0.0.
  Bidirectional. Export emits 1 ODPS doc + N sibling ``<contractId>.odcs.yaml``
  files (canonical Bitol fragments layout). Import accepts an ODPS file,
  a directory bundle, or a lone ODCS file. Backed by
  :class:`fluid_build.providers.odps_standard.BitolOdpsProvider`.

- ``odps-4.1`` — Open Data Product **Specification** v4.1 (Linux Foundation,
  hosted by the Open Data Product Initiative / ODPI). Export-only single JSON
  document. Backed by the ``fluid_build.providers.odps`` provider.

The canonical CLI surface is ``fluid odps``; ``fluid opds`` is accepted as
a deprecated letter-swapped alias. The legacy ``--spec odpi-4.1`` and
``--version 4.1`` flags still work as hidden deprecated aliases for
``--spec odps-4.1``.

Usage::

    fluid odps export <contract> [--spec bitol-1.0.0|odps-4.1] [--out file] [--out-dir DIR]
    fluid odps import <path>     [--spec bitol-1.0.0] [--allow-remote] [--lenient] [-o OUT]
    fluid odps validate <file>   [--spec ...]
    fluid odps info              [--spec ...]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error

# Logger name intentionally preserved as ``"fluid.cli.opds"`` even though
# the canonical module path is now ``fluid_build.cli.odps`` (renamed
# 2026-05). External log aggregators / SIEM rules / dashboards key on this
# string; renaming would silently break alert pipelines. Tests at
# ``tests/test_odps_cli_spec.py::TestResolveSpec`` also pin the name.
LOG = logging.getLogger("fluid.cli.opds")


# --- spec dispatcher constants ---------------------------------------------

SPEC_BITOL_1_0_0 = "bitol-1.0.0"
SPEC_ODPS_4_1 = "odps-4.1"
DEFAULT_SPEC = SPEC_BITOL_1_0_0
SUPPORTED_SPECS = (SPEC_BITOL_1_0_0, SPEC_ODPS_4_1)

# Deprecated alias for SPEC_ODPS_4_1. The v4.1 spec is "Open Data Product
# Specification" (acronym: ODPS); ODPI is the *organization* (Open Data
# Product Initiative) that hosts it. The earlier ``odpi-4.1`` token swapped
# spec name for org name. Accepted on input via resolve_spec() and the
# argparse choices, but every emission of the canonical spec id uses
# SPEC_ODPS_4_1.
SPEC_ODPI_4_1 = SPEC_ODPS_4_1  # back-compat alias — do not remove
LEGACY_SPEC_ODPI_4_1_TOKEN = "odpi-4.1"

BITOL_SPEC_URL = "https://github.com/bitol-io/open-data-product-standard"
# These URLs legitimately reference the ODPI GitHub org (the org IS named
# "Open Data Product Initiative"). The variable names use ODPS_4_1 to match
# the canonical spec acronym; ODPI_4_1_* aliases remain for back-compat.
ODPS_4_1_SPEC_URL = "https://github.com/Open-Data-Product-Initiative/v4.1"
ODPS_4_1_SCHEMA_URL = (
    "https://github.com/Open-Data-Product-Initiative/v4.1/blob/main/source/schema/odps.json"
)
ODPS_4_1_SCHEMA_URL_RAW = "https://raw.githubusercontent.com/Open-Data-Product-Initiative/v4.1/main/source/schema/odps.json"

# Back-compat aliases for the URL constants — kept so external scripts and
# tests that imported ``ODPI_4_1_SPEC_URL`` continue to resolve.
ODPI_4_1_SPEC_URL = ODPS_4_1_SPEC_URL
ODPI_4_1_SCHEMA_URL = ODPS_4_1_SCHEMA_URL
ODPI_4_1_SCHEMA_URL_RAW = ODPS_4_1_SCHEMA_URL_RAW

# Back-compat shape for the legacy ``--version`` flag and info display.
ODPS_VERSIONS: Dict[str, Dict[str, Any]] = {
    "4.1": {
        "spec_url": ODPS_4_1_SPEC_URL,
        "schema_url": ODPS_4_1_SCHEMA_URL,
        "schema_url_raw": ODPS_4_1_SCHEMA_URL_RAW,
        "description": "ODPS v4.1 — Open Data Product Specification (Linux Foundation / ODPI)",
        "status": "stable",
        "default": True,
    },
}

# Legacy alias — pre-Phase-4 code used ``DEFAULT_VERSION`` to mean "the
# only supported ODPS version". Kept as a re-export so callers that
# imported it (e.g. tests/test_opds_ext.py) don't have to be rewritten.
# New code should use ``DEFAULT_SPEC`` and the ``--spec`` flag instead.
DEFAULT_VERSION = "4.1"


def resolve_spec(args: argparse.Namespace) -> str:
    """Resolve the active ``--spec`` from CLI args.

    Precedence: ``--spec`` > legacy ``--version 4.1`` > default.
    Emits a deprecation warning when a legacy form (``--version`` or
    ``--spec odpi-4.1``) is the only signal.
    """
    spec = getattr(args, "spec", None)
    if isinstance(spec, str):
        if spec == LEGACY_SPEC_ODPI_4_1_TOKEN:
            LOG.warning(
                "--spec odpi-4.1 is deprecated; use --spec odps-4.1 instead "
                "(ODPS is the spec name; ODPI is the organisation name)"
            )
            return SPEC_ODPS_4_1
        if spec in SUPPORTED_SPECS:
            return spec
    legacy_version = getattr(args, "version", None)
    if isinstance(legacy_version, str) and legacy_version == "4.1":
        LOG.warning("--version 4.1 is deprecated; use --spec odps-4.1 instead")
        return SPEC_ODPS_4_1
    return DEFAULT_SPEC


def get_version_info(version: str) -> Dict[str, Any]:
    """Get information about a specific ODPS version (legacy --version support)."""
    if version not in ODPS_VERSIONS:
        available = ", ".join(ODPS_VERSIONS.keys())
        raise ValueError(f"Unsupported ODPS version: {version}. Available: {available}")
    return ODPS_VERSIONS[version]


# --- export ---------------------------------------------------------------


def cmd_opds_export(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Export a FLUID contract to ODPS — dispatched by ``--spec``."""
    from fluid_build.cli.bootstrap import load_contract_with_overlay

    spec = resolve_spec(args)
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    except Exception as e:
        logger.error("contract_load_failed", extra={"error": str(e)})
        console_error(f"Error loading contract: {e}")
        return 1

    if spec == SPEC_BITOL_1_0_0:
        return _export_bitol(args, contract, logger)
    if spec == SPEC_ODPS_4_1:
        return _export_odps_v4_1(args, contract, logger)
    console_error(f"Unsupported --spec {spec!r}; supported: {SUPPORTED_SPECS}")
    return 2


def _export_bitol(
    args: argparse.Namespace, contract: Dict[str, Any], logger: logging.Logger
) -> int:
    """Bitol ODPS v1.0.0 export — 1 ODPS doc + N sibling ODCS contracts."""
    from fluid_build.providers.odps_standard import BitolOdpsProvider

    out = getattr(args, "out", "-")
    out_dir = getattr(args, "out_dir", None)
    validate_strict = getattr(args, "validate_strict", True)
    fmt = (getattr(args, "format", "yaml") or "yaml").lower()

    provider = BitolOdpsProvider()
    provider.strict_validation = bool(validate_strict)

    try:
        bundle = provider.render(
            contract,
            out=out if out and out != "-" and not out_dir else None,
            out_dir=out_dir,
            fmt=fmt,
        )
    except Exception as e:
        logger.error("opds_export_failed", extra={"error": str(e), "spec": SPEC_BITOL_1_0_0})
        console_error(f"Error exporting to Bitol ODPS: {e}")
        return 1

    if out == "-" and not out_dir:
        # Stdout dump of the product doc — JSON for unambiguous pipeline use.
        cprint(json.dumps(bundle["product"], indent=2, ensure_ascii=False))
    else:
        contract_count = len(bundle.get("contracts") or {})
        location = out_dir if out_dir else out
        cprint(
            f"✓ Exported Bitol ODPS v1.0.0: 1 product + "
            f"{contract_count} ODCS contract(s) → {location}"
        )
    return 0


def _export_odps_v4_1(
    args: argparse.Namespace, contract: Dict[str, Any], logger: logging.Logger
) -> int:
    """ODPS v4.1 (LF/ODPI) export path (single JSON document)."""
    from fluid_build.cli.bootstrap import build_provider

    try:
        provider = build_provider("odps", None, None, logger)
    except Exception as e:
        logger.error("provider_build_failed", extra={"error": str(e)})
        console_error(f"Error building ODPS v4.1 provider: {e}")
        return 1

    provider.opds_version = "4.1"
    provider.opds_spec_url = ODPS_4_1_SPEC_URL
    provider.opds_schema_url = ODPS_4_1_SCHEMA_URL

    out_path = getattr(args, "out", "-")
    try:
        result = provider.render(contract, out=out_path, fmt="opds")
        if out_path == "-":
            if getattr(args, "pretty", True):
                cprint(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                cprint(json.dumps(result, ensure_ascii=False))
        else:
            cprint(f"✓ Exported to ODPS v4.1 (LF/ODPI): {out_path}")
            cprint(f"  Specification: {ODPS_4_1_SPEC_URL}")
        return 0
    except Exception as e:
        logger.error("opds_export_failed", extra={"error": str(e), "spec": SPEC_ODPS_4_1})
        console_error(f"Error exporting to ODPS v4.1: {e}")
        return 1


# Back-compat alias — earlier code/tests imported ``_export_odpi_v4_1``.
_export_odpi_v4_1 = _export_odps_v4_1


# --- import ---------------------------------------------------------------


def cmd_opds_import(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Import an ODPS / ODCS file or directory → one validated FLUID contract.

    Input dispatch:
      - Path ends in ``.odcs.yaml``/``.odcs.yml``/``.odcs.json`` →
        :meth:`OdcsProvider.import_contract` (single-expose FLUID).
      - Path is a file with ``kind: DataProduct`` (or ``.odps.yaml``) →
        :meth:`BitolOdpsProvider.import_contract` with resolver.
      - Path is a directory → :meth:`BitolOdpsProvider.import_directory`.

    ``--spec odps-4.1`` is reserved for the LF/ODPI provider, which is
    export-only and rejects import with a clear error.
    """
    spec = resolve_spec(args)
    if spec == SPEC_ODPS_4_1:
        console_error(
            "ODPS v4.1 (LF/ODPI) is export-only; import is supported only for --spec bitol-1.0.0"
        )
        return 2

    in_path = Path(args.path)
    if not in_path.exists():
        console_error(f"Input path not found: {in_path}")
        return 1

    # Default to remote-off (SSRF defence). --allow-remote opts in;
    # --no-remote remains accepted as a no-op for back-compat.
    allow_remote = bool(getattr(args, "allow_remote", False))
    if getattr(args, "no_remote", False) and allow_remote:
        console_error(
            "--no-remote and --allow-remote are mutually exclusive; "
            "honouring --no-remote (default behaviour)."
        )
        allow_remote = False
    lenient = bool(getattr(args, "lenient", False))
    out = getattr(args, "out", None)
    fmt = (getattr(args, "format", "yaml") or "yaml").lower()

    try:
        fluid = _dispatch_import(in_path, allow_remote=allow_remote, lenient=lenient)
    except Exception as e:
        logger.error("opds_import_failed", extra={"error": str(e), "path": str(in_path)})
        console_error(f"Error importing: {e}")
        return 1

    if out and out != "-":
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            out_path.write_text(json.dumps(fluid, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            import yaml

            out_path.write_text(
                yaml.dump(fluid, default_flow_style=False, sort_keys=False), encoding="utf-8"
            )
        cprint(f"✓ Imported {in_path} → {out_path}")
    else:
        if fmt == "json":
            cprint(json.dumps(fluid, indent=2, ensure_ascii=False))
        else:
            import yaml

            cprint(yaml.dump(fluid, default_flow_style=False, sort_keys=False))
    return 0


def _dispatch_import(path: Path, *, allow_remote: bool, lenient: bool) -> Dict[str, Any]:
    """Choose the right import method based on the input path's shape/content."""
    if path.is_dir():
        from fluid_build.providers.odps_standard import BitolOdpsProvider

        return BitolOdpsProvider().import_directory(
            path, allow_remote=allow_remote, lenient=lenient
        )

    suffixes = {s.lower() for s in path.suffixes}
    name_lower = path.name.lower()

    if ".odcs" in suffixes or name_lower.endswith((".odcs.yaml", ".odcs.yml", ".odcs.json")):
        from fluid_build.providers.odcs import OdcsProvider

        return OdcsProvider().import_contract(path)

    if ".odps" in suffixes or name_lower.endswith((".odps.yaml", ".odps.yml", ".odps.json")):
        from fluid_build.providers.odps_standard import BitolOdpsProvider

        return BitolOdpsProvider().import_contract(path, allow_remote=allow_remote, lenient=lenient)

    # Last resort: sniff the file's ``kind`` field
    from fluid_build.providers.odcs.io import read_input

    data = read_input(path)
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind == "DataProduct":
        from fluid_build.providers.odps_standard import BitolOdpsProvider

        return BitolOdpsProvider().import_contract(path, allow_remote=allow_remote, lenient=lenient)
    if kind == "DataContract":
        from fluid_build.providers.odcs import OdcsProvider

        return OdcsProvider().import_contract(path)

    raise ValueError(
        f"Cannot determine input type for {path}: "
        f"expected a directory, *.odps.yaml, *.odcs.yaml, or a file with "
        f"kind: DataProduct or kind: DataContract"
    )


# --- validate / info ------------------------------------------------------


def cmd_opds_validate(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Validate an ODPS file against the chosen spec."""
    spec = resolve_spec(args)
    path = Path(args.file)
    if not path.exists():
        console_error(f"Error: File not found: {args.file}")
        return 1

    if spec == SPEC_BITOL_1_0_0:
        from fluid_build.providers.base import ProviderError
        from fluid_build.providers.odps_standard import BitolOdpsProvider
        from fluid_build.providers.odps_standard.io import read_input

        try:
            odps_data = read_input(path)
        except Exception as e:
            console_error(f"Error loading ODPS file: {e}")
            return 1
        if not isinstance(odps_data, dict):
            console_error(f"Error: {path} did not parse as a mapping")
            return 1
        try:
            BitolOdpsProvider().validate_product(odps_data)
        except ProviderError as e:
            console_error(f"✗ Bitol ODPS validation failed: {e}")
            return 1
        cprint(f"✓ Bitol ODPS v1.0.0 file is valid: {path}")
        cprint(f"  Spec: {BITOL_SPEC_URL}")
        return 0

    # ODPS v4.1 (LF/ODPI) — structural validator
    try:
        from fluid_build.providers.odps.validator import validate_opds_structure

        with open(path, encoding="utf-8") as f:
            opds_data = json.load(f)
        if "artifacts" in opds_data and isinstance(opds_data["artifacts"], dict):
            opds_data = opds_data["artifacts"]
        result = validate_opds_structure(
            opds_data,
            version="4.1",
            use_full_schema=getattr(args, "full_schema", True),
            schema_url=ODPS_4_1_SCHEMA_URL_RAW,
        )
        if not result.get("valid"):
            console_error(
                f"✗ ODPS v4.1 validation failed ({result.get('validation_type', 'unknown')})"
            )
            for err in result.get("errors", []) or []:
                console_error(f"  - {err}")
            return 1
        cprint(f"✓ ODPS v4.1 file is valid: {path}")
        cprint(f"  Schema: {ODPS_4_1_SCHEMA_URL}")
        return 0
    except ImportError:
        console_error("ODPS v4.1 validator not available")
        return 1
    except Exception as e:
        console_error(f"Error validating ODPS v4.1 file: {e}")
        return 1


def cmd_opds_info(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Show spec information for Bitol ODPS or ODPS v4.1 (LF/ODPI)."""
    # Legacy ``--version`` path: validate against ODPS_VERSIONS first so
    # garbage values still produce the historical "unsupported version"
    # error rather than silently routing to the default.
    legacy_version = getattr(args, "version", None)
    if isinstance(legacy_version, str) and legacy_version not in ODPS_VERSIONS:
        console_error(
            f"Unsupported ODPS version: {legacy_version}. "
            f"Available: {', '.join(ODPS_VERSIONS.keys())}"
        )
        return 1

    spec = resolve_spec(args)
    if getattr(args, "json", False):
        if spec == SPEC_BITOL_1_0_0:
            info = {
                "spec": SPEC_BITOL_1_0_0,
                "name": "Bitol Open Data Product Standard",
                "version": "1.0.0",
                "spec_url": BITOL_SPEC_URL,
                "media_type": "application/odps+yaml;version=1.0.0",
                "license": "Apache-2.0",
            }
        else:
            info = ODPS_VERSIONS["4.1"]
        cprint(json.dumps(info, indent=2))
        return 0

    if spec == SPEC_BITOL_1_0_0:
        cprint("Bitol Open Data Product Standard v1.0.0")
        cprint("=" * 60)
        cprint(f"Spec URL:   {BITOL_SPEC_URL}")
        cprint("Media type: application/odps+yaml;version=1.0.0")
        cprint("License:    Apache-2.0")
    else:
        info = ODPS_VERSIONS["4.1"]
        cprint("ODPS Version 4.1 (Linux Foundation / ODPI)")
        cprint("=" * 60)
        cprint(f"Description: {info['description']}")
        cprint(f"Status:      {info['status']}")
        cprint(f"Spec URL:    {info['spec_url']}")
        cprint(f"Schema URL:  {info['schema_url']}")
    return 0


# --- argparse wiring ------------------------------------------------------


def _cmd_odps_help(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Default handler when ``fluid odps`` is invoked without a subcommand."""
    cprint(
        "Usage: fluid odps <subcommand> [options]\n\n"
        "Subcommands:\n"
        "  export    Export a FLUID contract to ODPS (Bitol v1.0.0 default; "
        "--spec odps-4.1 for LF/ODPI)\n"
        "  import    Import an ODPS / ODCS file or directory → one FLUID contract\n"
        "  validate  Validate an ODPS file against the chosen spec\n"
        "  info      Show spec information for Bitol ODPS or LF/ODPI ODPS v4.1\n\n"
        "Two distinct standards share the ODPS acronym; pick via --spec:\n"
        "  --spec bitol-1.0.0  Bitol Open Data Product Standard v1.0.0 (DEFAULT)\n"
        "  --spec odps-4.1     LF/ODPI Open Data Product Specification v4.1\n\n"
        "Examples:\n"
        "  fluid odps info\n"
        "  fluid odps export contract.fluid.yaml --out-dir bundle/\n"
        "  fluid odps export contract.fluid.yaml --spec odps-4.1 --out lf.json\n"
        "  fluid odps validate bundle/<product>.odps.yaml\n"
    )
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register ODPS commands with the CLI."""

    odps = subparsers.add_parser(
        "odps",
        aliases=["opds"],  # ``opds`` is a deprecated letter-swapped alias
        help="Bitol ODPS v1.0.0 (default) or ODPS v4.1 (LF/ODPI) — export, import, validate",
        description=(
            "Work with the ODPS family of standards. Two specifications, dispatched via --spec:\n"
            "  bitol-1.0.0  Bitol Open Data Product Standard v1.0.0 (default, bidirectional).\n"
            "  odps-4.1     Open Data Product Specification v4.1 (Linux Foundation / ODPI,\n"
            "               export-only).\n\n"
            f"Bitol ODPS:  {BITOL_SPEC_URL}\n"
            f"ODPS v4.1:   {ODPS_4_1_SPEC_URL}"
        ),
    )
    # When invoked as ``fluid odps`` with no subcommand, print a friendly
    # help summary instead of the generic dispatcher "No command function
    # found" error.
    odps.set_defaults(func=_cmd_odps_help)

    odps_sub = odps.add_subparsers(dest="odps_command", help="ODPS operations")
    # Argparse choices accept the canonical id + the deprecated ``odpi-4.1``
    # alias; resolve_spec() emits a WARNING when the legacy form is used.
    _SPEC_CHOICES = list(SUPPORTED_SPECS) + [LEGACY_SPEC_ODPI_4_1_TOKEN]

    # --- export
    export = odps_sub.add_parser(
        "export",
        help="Export FLUID contract to ODPS",
        description=(
            "Export a FLUID contract to ODPS. --spec selects the target spec; default is "
            "bitol-1.0.0 (1 ODPS doc + N sibling ODCS contracts). --spec odps-4.1 emits a "
            "single ODPS v4.1 (LF/ODPI) JSON document."
        ),
    )
    export.add_argument("contract", help="Path to FLUID contract file (YAML/JSON)")
    export.add_argument(
        "--spec",
        default=None,
        choices=_SPEC_CHOICES,
        help=f"Target specification (default: {DEFAULT_SPEC}).",
    )
    # Deprecated alias — kept for back-compat. Hidden from --help.
    export.add_argument(
        "--version",
        default=None,
        choices=list(ODPS_VERSIONS.keys()),
        help=argparse.SUPPRESS,
    )
    export.add_argument(
        "--out", default="-", help="Output file path, or '-' for stdout (default: stdout)"
    )
    export.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        help=(
            "(bitol-1.0.0 only) Directory for the ODPS doc + per-port ODCS files. "
            "Mutually exclusive with --out."
        ),
    )
    export.add_argument(
        "--format",
        "-f",
        default="yaml",
        choices=["yaml", "json"],
        help="Output format for file/dir writes (default: yaml). Stdout always uses JSON.",
    )
    export.add_argument("--env", help="Environment name for overlay application")
    export.add_argument(
        "--validate-strict",
        dest="validate_strict",
        action="store_true",
        default=True,
        help="(bitol-1.0.0) Validate emitted docs against vendored schemas (default: true).",
    )
    export.add_argument(
        "--no-validate-strict",
        dest="validate_strict",
        action="store_false",
        help="(bitol-1.0.0) Downgrade schema validation to warnings.",
    )
    export.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="(odps-4.1) Pretty-print JSON output (default: true)",
    )
    export.add_argument(
        "--compact",
        dest="pretty",
        action="store_false",
        help="(odps-4.1) Compact JSON output (no indentation).",
    )
    export.set_defaults(func=cmd_opds_export)

    # --- import (Bitol ODPS only)
    importp = odps_sub.add_parser(
        "import",
        help="Import an ODPS / ODCS file or directory → one FLUID contract",
        description=(
            "Import a Bitol ODPS product file, a directory containing the ODPS doc + "
            "sibling ODCS files (or just ODCS files), or a single ODCS contract file. "
            "Always emits one validated FLUID contract."
        ),
    )
    importp.add_argument(
        "path",
        help="Input path — a .odps.yaml file, a directory, or a .odcs.yaml file.",
    )
    importp.add_argument(
        "--spec",
        default=None,
        choices=_SPEC_CHOICES,
        help=f"Specification (default: {DEFAULT_SPEC}). odps-4.1 is export-only.",
    )
    importp.add_argument(
        "-o", "--out", default=None, help="Output FLUID file path (default: stdout)"
    )
    importp.add_argument(
        "-f", "--format", default="yaml", choices=["yaml", "json"], help="Output format"
    )
    importp.add_argument(
        "--allow-remote",
        dest="allow_remote",
        action="store_true",
        help=(
            "Allow http(s) fetch when resolving contractId references. "
            "Default is OFF (since the May 2026 SSRF hardening). The "
            "fetcher rejects internal/private IPs and pins the validated "
            "IP at the TCP layer; even so, only enable when you trust "
            "the upstream catalog."
        ),
    )
    importp.add_argument(
        "--no-remote",
        dest="no_remote",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated — default is already remote-off
    )
    importp.add_argument(
        "--lenient",
        action="store_true",
        help="Downgrade output-port resolution failures to warnings (input ports are always lenient).",
    )
    importp.set_defaults(func=cmd_opds_import)

    # --- validate
    validate = odps_sub.add_parser(
        "validate",
        help="Validate an ODPS file against the chosen spec",
    )
    validate.add_argument("file", help="Path to ODPS YAML/JSON file")
    validate.add_argument(
        "--spec",
        default=None,
        choices=_SPEC_CHOICES,
        help=f"Specification (default: {DEFAULT_SPEC}).",
    )
    validate.add_argument(
        "--version",
        default=None,
        choices=list(ODPS_VERSIONS.keys()),
        help=argparse.SUPPRESS,
    )
    validate.add_argument(
        "--full-schema",
        action="store_true",
        default=True,
        help="(odps-4.1) Use full JSON schema validation (default: true)",
    )
    validate.add_argument(
        "--no-full-schema",
        dest="full_schema",
        action="store_false",
        help="(odps-4.1) Skip full JSON schema validation, basic only",
    )
    validate.set_defaults(func=cmd_opds_validate)

    # --- info
    info = odps_sub.add_parser(
        "info",
        help="Display spec information",
    )
    info.add_argument(
        "--spec",
        default=None,
        choices=_SPEC_CHOICES,
        help=f"Specification (default: {DEFAULT_SPEC}).",
    )
    info.add_argument(
        "--version",
        default=None,
        choices=list(ODPS_VERSIONS.keys()),
        help=argparse.SUPPRESS,
    )
    info.add_argument("--json", action="store_true", help="Output in JSON format")
    info.set_defaults(func=cmd_opds_info)
