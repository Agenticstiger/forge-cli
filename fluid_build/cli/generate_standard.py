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

"""``fluid generate standard`` subcommand.

Exports FLUID contracts to industry-standard data product formats.

Supported formats:
    opds        Open Data Product Specification (OPDS)
    odcs        Open Data Contract Standard v3.1 (Bitol.io)
    odps        ODPS v4.1 (Linux Foundation)
    odps-bitol  ODPS-Bitol v1.0 (Entropy Data marketplace)

Usage:
    fluid generate standard contract.fluid.yaml --format opds
    fluid generate standard contract.fluid.yaml --format odps-bitol
    fluid generate standard --list
"""

from __future__ import annotations

import argparse
import logging
import os

from fluid_build.cli.console import cprint

from ._common import CLIError, load_contract_with_overlay, write_json
from ._logging import info

SUPPORTED_FORMATS = ["opds", "odcs", "odps", "odps-bitol"]

DEFAULT_OUTPUTS = {
    "opds": "runtime/exports/product.opds.json",
    "odcs": "runtime/exports/product.odcs.yaml",
    "odps": "runtime/exports/product.odps.yaml",
    "odps-bitol": "runtime/exports/product.odps-bitol.yaml",
}


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "standard",
        help="Export to data product standards (OPDS, ODCS, ODPS, ODPS-Bitol)",
        description=(
            "Export a FLUID contract to an industry-standard data product format.\n\n"
            "Supported formats:\n"
            "  opds        Open Data Product Specification\n"
            "  odcs        Open Data Contract Standard v3.1 (Bitol.io)\n"
            "  odps        ODPS v4.1 (Linux Foundation)\n"
            "  odps-bitol  ODPS-Bitol v1.0 (Entropy Data marketplace)\n"
        ),
        epilog="""Examples:
  fluid generate standard contract.fluid.yaml --format opds
  fluid generate standard contract.fluid.yaml --format odps-bitol -o out.yaml
  fluid generate standard --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument(
        "--format",
        "-f",
        dest="standard_format",
        choices=SUPPORTED_FORMATS,
        help="Target standard format",
    )
    p.add_argument("--out", "-o", help="Output file path (auto-detected if not set)")
    p.add_argument("--env", help="Environment overlay")
    p.add_argument(
        "--list", dest="list_formats", action="store_true", help="List supported formats"
    )
    p.set_defaults(generate_sub="standard", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate standard``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    if getattr(args, "list_formats", False):
        cprint("Supported data product standard formats:\n")
        for fmt in SUPPORTED_FORMATS:
            cprint(f"  {fmt:<14} {_format_description(fmt)}")
        cprint("\nUsage: fluid generate standard contract.fluid.yaml --format <format>")
        return 0

    fmt = getattr(args, "standard_format", None)
    if not fmt:
        cprint("Error: --format is required. Use --list to see available formats.")
        return 1

    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    try:
        return _export_format(fmt, contract_path, args, logger)
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, f"generate_standard_{fmt}_failed", {"error": str(e)})


def _format_description(fmt: str) -> str:
    descriptions = {
        "opds": "Open Data Product Specification (OPDS)",
        "odcs": "Open Data Contract Standard v3.1 (Bitol.io)",
        "odps": "ODPS v4.1 (Linux Foundation)",
        "odps-bitol": "ODPS-Bitol v1.0 (Entropy Data marketplace)",
    }
    return descriptions.get(fmt, fmt)


def _export_format(fmt: str, contract_path: str, args, logger: logging.Logger) -> int:
    """Delegate to the appropriate exporter."""
    env = getattr(args, "env", None)
    out = getattr(args, "out", None) or DEFAULT_OUTPUTS.get(
        fmt, f"runtime/exports/product.{fmt}.yaml"
    )

    if fmt == "opds":
        return _export_opds(contract_path, env, out, logger)
    elif fmt == "odcs":
        return _export_odcs(contract_path, env, out, logger)
    elif fmt == "odps":
        return _export_odps(contract_path, env, out, logger)
    elif fmt == "odps-bitol":
        return _export_odps_bitol(contract_path, env, out, logger)
    else:
        cprint(f"Unknown format: {fmt}")
        return 1


def _export_opds(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to OPDS format — delegates to existing export_opds logic."""
    c = load_contract_with_overlay(contract_path, env, logger)
    try:
        from fluid_build.providers.odps.odps import OdpsProvider

        export = OdpsProvider.to_odps(c)
    except Exception:
        export = {
            "specVersion": "1.0",
            "id": c.get("id"),
            "title": c.get("name"),
            "owner": c.get("metadata", {}).get("owner", {}),
            "domain": c.get("domain"),
            "exposes": c.get("exposes", []),
        }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_json(out, export)
    info(logger, "generate_standard_opds_ok", out=out)
    return 0


def _export_odcs(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to ODCS format — delegates to existing odcs module."""
    try:
        from .odcs import export_odcs

        return export_odcs(contract_path, env, out, logger)
    except ImportError:
        # Fallback: basic ODCS structure
        c = load_contract_with_overlay(contract_path, env, logger)
        import yaml as _yaml

        odcs = {
            "kind": "DataContract",
            "apiVersion": "v3.1.0",
            "id": c.get("id"),
            "info": {
                "title": c.get("name"),
                "version": c.get("fluidVersion", "0.7.3"),
                "owner": c.get("metadata", {}).get("owner", {}),
            },
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(odcs, f, default_flow_style=False)
        info(logger, "generate_standard_odcs_ok", out=out)
        return 0


def _export_odps(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to ODPS v4.1 (Linux Foundation) format."""
    try:
        from .opds import run as opds_run

        # Create a namespace-like object for the existing command
        class _Args:
            pass

        a = _Args()
        a.contract = contract_path
        a.env = env
        a.out = out
        return opds_run(a, logger)
    except ImportError:
        c = load_contract_with_overlay(contract_path, env, logger)
        import yaml as _yaml

        odps = {
            "specVersion": "4.1",
            "id": c.get("id"),
            "name": c.get("name"),
            "domain": c.get("domain"),
            "owner": c.get("metadata", {}).get("owner", {}),
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(odps, f, default_flow_style=False)
        info(logger, "generate_standard_odps_ok", out=out)
        return 0


def _export_odps_bitol(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to Bitol ODPS v1.0.0 — bare product file at ``out``.

    Bare product only (no per-port ODCS siblings). For the canonical Bitol
    fragments bundle (1 ODPS + N ``<contractId>.odcs.yaml`` together in
    one directory) call ``BitolOdpsProvider.render(out_dir=...)`` directly
    or use ``fluid generate artifacts --emit odps-bitol`` which routes
    through ``artifact_fanout._emit_odps_bitol``.
    """
    try:
        from fluid_build.loader import load_contract
        from fluid_build.providers.odps_standard import BitolOdpsProvider

        c = load_contract(contract_path)
        provider = BitolOdpsProvider()
        provider.strict_validation = False  # bare-product flow shouldn't enforce sibling validation
        bundle = provider.render(c)
        product = bundle["product"]

        os.makedirs(os.path.dirname(out), exist_ok=True)
        import yaml as _yaml

        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(product, f, default_flow_style=False)
        info(logger, "generate_standard_odps_bitol_ok", out=out)
        return 0
    except ImportError:
        # Fallback
        c = load_contract_with_overlay(contract_path, env, logger)
        import yaml as _yaml

        odps_bitol = {
            "dataProductSpecification": "1.0.0",
            "info": {
                "title": c.get("name"),
                "x-fluidId": c.get("id"),
            },
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(odps_bitol, f, default_flow_style=False)
        info(logger, "generate_standard_odps_bitol_ok", out=out)
        return 0
