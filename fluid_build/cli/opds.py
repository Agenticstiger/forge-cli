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

# fluid_build/cli/opds.py
"""
ODPS (Open Data Product Specification) CLI Commands

Provides commands for exporting FLUID contracts to ODPS format with support
for multiple specification versions.

Official Specification:
- v4.1: https://github.com/Open-Data-Product-Initiative/v4.1
- Schema: https://github.com/Open-Data-Product-Initiative/v4.1/blob/main/source/schema/odps.yaml

Standards Compliance:
- ODPS v4.1 (default) - Linux Foundation / Open Data Product Initiative
- Future version support through version parameter
- Full metadata preservation
- Validation against official schema

Note: The CLI command is 'opds' for historical reasons, but this implements
the official ODPS (Open Data Product Specification) standard.

Usage:
    fluid opds export <contract> [--version 4.1] [--out file.json]
    fluid opds validate <odps-file> [--version 4.1]
    fluid opds info [--version 4.1]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error

LOG = logging.getLogger("fluid.cli.opds")

# ODPS specification versions and their schema URLs
ODPS_VERSIONS = {
    "4.1": {
        "spec_url": "https://github.com/Open-Data-Product-Initiative/v4.1",
        "schema_url": "https://github.com/Open-Data-Product-Initiative/v4.1/blob/main/source/schema/odps.json",
        "schema_url_raw": "https://raw.githubusercontent.com/Open-Data-Product-Initiative/v4.1/main/source/schema/odps.json",
        "description": "ODPS v4.1 - Current stable version with full JSON Schema",
        "status": "stable",
        "default": True,
    },
    # Future versions can be added here
    # "5.0": {
    #     "spec_url": "https://github.com/Open-Data-Product-Initiative/v5.0",
    #     "schema_url": "...",
    #     "description": "ODPS v5.0 - Next generation",
    #     "status": "draft",
    #     "default": False
    # }
}

DEFAULT_VERSION = next(v for v, info in ODPS_VERSIONS.items() if info.get("default", False))


def get_version_info(version: str) -> Dict[str, Any]:
    """Get information about a specific ODPS version."""
    if version not in ODPS_VERSIONS:
        available = ", ".join(ODPS_VERSIONS.keys())
        raise ValueError(f"Unsupported ODPS version: {version}. Available: {available}")
    return ODPS_VERSIONS[version]


def cmd_opds_export(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Export FLUID contract to ODPS format.

    Args:
        args.contract: Path to FLUID contract file
        args.version: ODPS specification version (default: 4.1)
        args.out: Output file path or '-' for stdout
        args.env: Optional environment overlay
        args.validate: Validate output against schema (default: true)
        args.pretty: Pretty-print JSON output (default: true)

    Returns:
        0 on success, non-zero on error
    """
    from fluid_build.cli.bootstrap import build_provider, load_contract_with_overlay

    version = getattr(args, "version", DEFAULT_VERSION)
    version_info = get_version_info(version)

    logger.debug(
        "opds_export_start",
        extra={
            "contract": args.contract,
            "version": version,
            "spec": version_info["spec_url"],
            "output": getattr(args, "out", "-"),
        },
    )

    # Load contract
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    except Exception as e:
        logger.error("contract_load_failed", extra={"error": str(e)})
        console_error(f"Error loading contract: {e}")
        return 1

    # Build OPDS provider
    try:
        provider = build_provider("odps", None, None, logger)
    except Exception as e:
        logger.error("provider_build_failed", extra={"error": str(e)})
        console_error(f"Error building OPDS provider: {e}")
        return 1

    # Set version-specific configuration
    provider.opds_version = version
    provider.opds_spec_url = version_info["spec_url"]
    provider.opds_schema_url = version_info["schema_url"]

    # Render to OPDS
    try:
        result = provider.render(contract, out=getattr(args, "out", "-"), fmt="opds")

        # Output handling
        out_path = getattr(args, "out", "-")
        if out_path == "-":
            # Print to stdout with optional pretty printing
            if getattr(args, "pretty", True):
                cprint(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                cprint(json.dumps(result, ensure_ascii=False))
        else:
            logger.debug(
                "opds_export_success",
                extra={
                    "output": out_path,
                    "version": version,
                    "size_bytes": Path(out_path).stat().st_size if Path(out_path).exists() else 0,
                },
            )
            cprint(f"✓ Exported to OPDS v{version}: {out_path}")
            cprint(f"  Specification: {version_info['spec_url']}")

        return 0

    except Exception as e:
        logger.error("opds_export_failed", extra={"error": str(e)})
        console_error(f"Error exporting to OPDS: {e}")
        return 1


def cmd_opds_validate(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Validate an OPDS file against the specification schema.

    Args:
        args.file: Path to OPDS JSON file
        args.version: OPDS specification version to validate against
        args.full_schema: Use full JSON schema validation (default: true)

    Returns:
        0 if valid, non-zero if invalid
    """
    version = getattr(args, "version", DEFAULT_VERSION)
    version_info = get_version_info(version)
    use_full_schema = getattr(args, "full_schema", True)

    logger.info(
        "opds_validate_start",
        extra={
            "file": args.file,
            "version": version,
            "schema": version_info["schema_url"],
            "full_validation": use_full_schema,
        },
    )

    # Load OPDS file
    try:
        path = Path(args.file)
        if not path.exists():
            console_error(f"Error: File not found: {args.file}")
            return 1

        with open(path, encoding="utf-8") as f:
            opds_data = json.load(f)

        # Check if this is a wrapped format (from render())
        if "artifacts" in opds_data and isinstance(opds_data["artifacts"], dict):
            logger.info("opds_validate_unwrap", extra={"format": "wrapped"})
            opds_data = opds_data["artifacts"]
    except Exception as e:
        console_error(f"Error loading OPDS file: {e}")
        return 1

    # Use comprehensive validator if available
    try:
        from fluid_build.providers.odps.validator import validate_opds_structure

        result = validate_opds_structure(
            opds_data,
            version=version,
            use_full_schema=use_full_schema,
            schema_url=version_info.get("schema_url_raw"),
        )

        if not result["valid"]:
            console_error(
                f"✗ OPDS validation failed ({result.get('validation_type', 'unknown')} validation)"
            )
            if result.get("errors"):
                console_error("\nErrors:")
                for error in result["errors"]:
                    console_error(f"  - {error}")
            logger.error("opds_invalid", extra=result)
            return 1

        # Validation successful
        validation_type = result.get("validation_type", "basic")
        cprint(f"✓ OPDS file is valid (v{version} {validation_type} validation)")
        cprint(f"  Data Product: {opds_data.get('dataProductId')}")
        cprint(f"  Name: {opds_data.get('dataProductName')}")
        cprint(f"  Schema Reference: {version_info['schema_url']}")

        if result.get("warnings"):
            cprint("\nWarnings:")
            for warning in result["warnings"]:
                cprint(f"  ⚠ {warning}")

        if validation_type == "full_schema":
            cprint("\n✓ Validated against official JSON Schema")
            cprint(f"  {version_info.get('schema_url_raw', version_info['schema_url'])}")

        logger.info(
            "opds_valid",
            extra={"file": args.file, "version": version, "validation_type": validation_type},
        )
        return 0

    except ImportError:
        logger.warning("opds_validator_unavailable", extra={"message": "Using basic validation"})
        # Fall back to basic validation
        pass

    # Basic validation fallback
    required_fields = ["dataProductId", "dataProductName", "dataProductDescription"]
    missing = [f for f in required_fields if f not in opds_data]

    if missing:
        console_error(f"✗ OPDS validation failed: Missing required fields: {', '.join(missing)}")
        logger.error("opds_invalid", extra={"missing_fields": missing})
        return 1

    # Check version metadata if present
    if "version" in opds_data:
        cprint(f"  OPDS version in file: {opds_data['version']}")

    cprint(f"✓ OPDS file is valid (v{version} basic validation)")
    cprint(f"  Data Product: {opds_data.get('dataProductId')}")
    cprint(f"  Name: {opds_data.get('dataProductName')}")
    cprint(f"  Schema Reference: {version_info['schema_url']}")

    logger.info("opds_valid", extra={"file": args.file, "version": version})
    return 0


def cmd_opds_info(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Display information about OPDS specification versions.

    Args:
        args.version: Optional specific version to show info for
        args.json: Output in JSON format

    Returns:
        0 on success
    """
    if hasattr(args, "version") and args.version:
        # Show info for specific version
        try:
            version_info = get_version_info(args.version)

            if getattr(args, "json", False):
                cprint(json.dumps({args.version: version_info}, indent=2))
            else:
                cprint(f"OPDS Version {args.version}")
                cprint("=" * 60)
                cprint(f"Description:  {version_info['description']}")
                cprint(f"Status:       {version_info['status']}")
                cprint(f"Spec URL:     {version_info['spec_url']}")
                cprint(f"Schema URL:   {version_info['schema_url']}")
                if version_info.get("default"):
                    cprint("Default:      Yes")
        except ValueError as e:
            console_error(str(e))
            return 1
    else:
        # Show all versions
        if getattr(args, "json", False):
            cprint(json.dumps(ODPS_VERSIONS, indent=2))
        else:
            cprint("ODPS (Open Data Product Specification) Versions")
            cprint("=" * 60)
            cprint()

            for version, info in ODPS_VERSIONS.items():
                default_marker = " [DEFAULT]" if info.get("default") else ""
                status_marker = f" ({info['status'].upper()})" if info["status"] != "stable" else ""
                cprint(f"Version {version}{default_marker}{status_marker}")
                cprint(f"  {info['description']}")
                cprint(f"  Spec:   {info['spec_url']}")
                cprint(f"  Schema: {info['schema_url']}")
                cprint()

            cprint("Usage:")
            cprint("  fluid odps export contract.yaml --version 4.1")
            cprint("  fluid odps validate output.json --version 4.1")

    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register ODPS commands with the CLI."""

    # Main ODPS command group
    odps = subparsers.add_parser(
        "odps",
        help="Export and validate ODPS (Open Data Product Specification) format",
        description="""
        Work with ODPS (Open Data Product Specification) format.
        
        The Open Data Product Specification is a vendor-neutral, open-source standard
        for describing data products. This command supports exporting FLUID contracts
        to ODPS format and validating ODPS files.
        
        Official Specification: https://github.com/Open-Data-Product-Initiative
        """,
    )

    odps_sub = odps.add_subparsers(dest="odps_command", help="ODPS operations")

    # odps export
    export = odps_sub.add_parser(
        "export",
        help="Export FLUID contract to ODPS format",
        description="""
        Export a FLUID contract to ODPS (Open Data Product Specification) JSON format.
        
        Supports multiple OPDS specification versions for future compatibility.
        Output can be written to a file or stdout for pipeline integration.
        """,
    )
    export.add_argument("contract", help="Path to FLUID contract file (YAML/JSON)")
    export.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        choices=list(ODPS_VERSIONS.keys()),
        help=f"ODPS specification version (default: {DEFAULT_VERSION})",
    )
    export.add_argument(
        "--out", default="-", help="Output file path, or '-' for stdout (default: stdout)"
    )
    export.add_argument("--env", help="Environment name for overlay application")
    export.add_argument(
        "--validate",
        action="store_true",
        default=True,
        help="Validate output against OPDS schema (default: true)",
    )
    export.add_argument(
        "--no-validate", dest="validate", action="store_false", help="Skip validation of output"
    )
    export.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON output (default: true)",
    )
    export.add_argument(
        "--compact",
        dest="pretty",
        action="store_false",
        help="Compact JSON output (no indentation)",
    )
    export.set_defaults(func=cmd_opds_export)

    # odps validate
    validate = odps_sub.add_parser(
        "validate",
        help="Validate an ODPS file against specification",
        description="""
        Validate an ODPS JSON file against the official specification schema.
        
        Performs structural validation and checks required fields according to
        the ODPS specification version.
        """,
    )
    validate.add_argument("file", help="Path to ODPS JSON file")
    validate.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        choices=list(ODPS_VERSIONS.keys()),
        help=f"ODPS version to validate against (default: {DEFAULT_VERSION})",
    )
    validate.add_argument(
        "--full-schema",
        action="store_true",
        default=True,
        help="Use full JSON schema validation (default: true, requires jsonschema library)",
    )
    validate.add_argument(
        "--no-full-schema",
        dest="full_schema",
        action="store_false",
        help="Skip full JSON schema validation, use basic validation only",
    )
    validate.set_defaults(func=cmd_opds_validate)

    # odps info
    info = odps_sub.add_parser(
        "info",
        help="Display ODPS specification version information",
        description="""
        Show information about supported ODPS specification versions.
        
        Displays specification URLs, schema references, and version status.
        """,
    )
    info.add_argument(
        "--version", choices=list(ODPS_VERSIONS.keys()), help="Show info for specific version only"
    )
    info.add_argument("--json", action="store_true", help="Output in JSON format")
    info.set_defaults(func=cmd_opds_info)
