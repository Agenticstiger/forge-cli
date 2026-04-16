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

# fluid_build/cli/odps_standard.py
"""
ODPS-Bitol (Bitol.io Data Product Standard) CLI Commands

Commands for exporting FLUID contracts to ODPS-Bitol v1.0 format.

Note: This is Bitol.io's proprietary ODPS variant for Entropy Data marketplace.
Different from the official ODPS v4.1 (Linux Foundation) - use 'opds' command for that.

Specification: https://github.com/bitol-io/open-data-product-standard
"""

import json
import logging
from pathlib import Path
from typing import Optional

import click

from fluid_build.cli.console import cprint
from fluid_build.loader import load_contract
from fluid_build.providers.odps_standard import OdpsStandardProvider


@click.group(name="odps-bitol")
def odps_bitol_cli():
    """
    ODPS-Bitol (Bitol.io Data Product Standard) commands.

    Export FLUID contracts to ODPS-Bitol v1.0.0 format for data marketplace
    integration with platforms like Entropy Data.

    Note: For the official ODPS v4.1 (Linux Foundation), use 'fluid odps' command.
    """
    pass


@odps_bitol_cli.command(name="export")
@click.argument("contract", type=click.Path(exists=True))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: <contract-name>-odps.yaml)",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["yaml", "json"], case_sensitive=False),
    default="yaml",
    help="Output format (default: yaml)",
)
@click.option(
    "--include-custom/--no-custom", default=True, help="Include custom properties (default: true)"
)
def export_command(contract: str, output: Optional[str], format: str, include_custom: bool):
    """
    Export FLUID contract to ODPS-Bitol format.

    Example:
        fluid odps-bitol export my-contract.yaml
        fluid odps-bitol export my-contract.yaml -o product.yaml
        fluid odps-bitol export my-contract.yaml -f json
    """
    logger = logging.getLogger(__name__)

    try:
        # Load FLUID contract
        click.echo(f"Loading FLUID contract: {contract}")
        fluid_contract = load_contract(contract)

        # Configure provider
        provider = OdpsStandardProvider()
        if not include_custom:
            provider.include_custom_properties = False

        # Generate output path if not specified
        if not output:
            contract_path = Path(contract)
            output = contract_path.stem + f"-odps.{format}"

        # Export to ODPS
        click.echo(f"Exporting to ODPS v{provider.odps_version}...")
        odps_product = provider.render(fluid_contract, out=output, fmt=format)

        click.echo(f"✓ Successfully exported to {output}")

        # Show summary
        output_ports = odps_product.get("outputPorts", [])
        click.echo(f"\nData Product: {odps_product.get('name')}")
        click.echo(f"ID: {odps_product.get('id')}")
        click.echo(f"Status: {odps_product.get('status')}")
        click.echo(f"Output Ports: {len(output_ports)}")

        if output_ports:
            click.echo("\nOutput Ports:")
            for port in output_ports:
                click.echo(f"  - {port.get('name')} (v{port.get('version')}) - {port.get('type')}")

    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        click.echo(f"✗ Export failed: {e}", err=True)
        raise click.Abort()


@odps_bitol_cli.command(name="validate")
@click.argument("odps_file", type=click.Path(exists=True))
def validate_command(odps_file: str):
    """
    Validate ODPS data product file.

    Example:
        fluid odps validate product.yaml
    """
    logger = logging.getLogger(__name__)

    try:
        # Load ODPS file
        click.echo(f"Loading ODPS file: {odps_file}")

        file_path = Path(odps_file)
        with open(file_path) as f:
            if file_path.suffix in (".yaml", ".yml"):
                import yaml

                odps_data = yaml.safe_load(f)
            else:
                odps_data = json.load(f)

        # Validate required fields
        required_fields = ["apiVersion", "kind", "id", "name", "status"]
        missing_fields = []

        for field in required_fields:
            if field not in odps_data:
                missing_fields.append(field)

        if missing_fields:
            click.echo(
                f"✗ Validation failed: Missing required fields: {', '.join(missing_fields)}",
                err=True,
            )
            raise click.Abort()

        # Check apiVersion
        api_version = odps_data.get("apiVersion")
        if api_version != "v1.0.0":
            click.echo(f"⚠ Warning: apiVersion is {api_version}, expected v1.0.0", err=True)

        # Check kind
        kind = odps_data.get("kind")
        if kind != "DataProduct":
            click.echo(f"✗ Validation failed: kind must be 'DataProduct', got '{kind}'", err=True)
            raise click.Abort()

        # Validate output ports
        output_ports = odps_data.get("outputPorts", [])
        if not output_ports:
            click.echo("⚠ Warning: No output ports defined", err=True)

        for i, port in enumerate(output_ports):
            if "name" not in port:
                click.echo(f"✗ Validation failed: Output port {i} missing 'name' field", err=True)
                raise click.Abort()

        click.echo("✓ Validation passed")
        click.echo(f"\nData Product: {odps_data.get('name')}")
        click.echo(f"ID: {odps_data.get('id')}")
        click.echo(f"Status: {odps_data.get('status')}")
        click.echo(f"Output Ports: {len(output_ports)}")

    except click.Abort:
        raise
    except Exception as e:
        logger.error(f"Validation failed: {e}", exc_info=True)
        click.echo(f"✗ Validation failed: {e}", err=True)
        raise click.Abort()


@odps_bitol_cli.command(name="info")
def info_command():
    """
    Show ODPS provider information.
    """
    provider = OdpsStandardProvider()

    click.echo("ODPS (Open Data Product Standard) Provider")
    click.echo("=" * 50)
    click.echo(f"Version: {provider.odps_version}")
    click.echo(f"Specification: {provider.odps_spec_url}")
    click.echo("\nCapabilities:")

    caps = provider.capabilities()
    for cap, enabled in caps.items():
        status = "✓" if enabled else "✗"
        click.echo(f"  {status} {cap}")

    click.echo("\nSupported Export Formats:")
    click.echo("  - YAML")
    click.echo("  - JSON")

    click.echo("\nUsage:")
    click.echo("  fluid odps-bitol export <contract.yaml>")
    click.echo("  fluid odps-bitol validate <product.yaml>")


if __name__ == "__main__":
    odps_bitol_cli()


def register(subparsers) -> None:
    """Register ODPS-Bitol commands with the main CLI."""
    # Get the click command and convert to argparse subparser
    # For now, we'll register it as a simple argparse command

    odps_bitol = subparsers.add_parser(
        "odps-bitol",
        help="ODPS-Bitol (Bitol.io Data Product Standard for Entropy Data)",
        description="""
        Work with ODPS-Bitol (Bitol.io Data Product Standard) format.
        
        ODPS-Bitol is used for data marketplace integration with Entropy Data.
        This command supports exporting FLUID contracts to ODPS-Bitol format.
        
        Note: For the official ODPS v4.1 (Linux Foundation), use 'fluid odps' command.
        
        Specification: https://github.com/bitol-io/open-data-product-standard
        """,
    )

    odps_bitol_sub = odps_bitol.add_subparsers(
        dest="odps_bitol_command", help="ODPS-Bitol operations"
    )

    # odps-bitol export
    export = odps_bitol_sub.add_parser("export", help="Export FLUID contract to ODPS-Bitol format")
    export.add_argument("contract", help="Path to FLUID contract file")
    export.add_argument("--output", "-o", help="Output file path")
    export.add_argument(
        "--format", "-f", choices=["yaml", "json"], default="yaml", help="Output format"
    )
    export.add_argument("--no-custom", action="store_true", help="Exclude custom properties")
    export.set_defaults(func=lambda args, logger=None: _run_odps_export(args))

    # odps-bitol validate
    validate = odps_bitol_sub.add_parser("validate", help="Validate ODPS-Bitol data product file")
    validate.add_argument("odps_file", help="Path to ODPS-Bitol-Bitol file")
    validate.set_defaults(func=lambda args, logger=None: _run_odps_validate(args))

    # odps-bitol info
    info = odps_bitol_sub.add_parser("info", help="Show ODPS-Bitol provider information")
    info.set_defaults(func=lambda args, logger=None: _run_odps_info(args))


def _run_odps_export(args):
    """Run ODPS export command."""
    import logging

    from fluid_build.cli.bootstrap import load_contract_with_overlay

    logger = logging.getLogger(__name__)

    # Load contract
    fluid_contract = load_contract_with_overlay(args.contract, None, logger)

    # Configure provider
    provider = OdpsStandardProvider()
    if args.no_custom:
        provider.include_custom_properties = False

    # Generate output path if not specified
    if not args.output:
        from pathlib import Path

        contract_path = Path(args.contract)
        args.output = contract_path.stem + f"-odps.{args.format}"

    # Export
    provider.render(fluid_contract, out=args.output, fmt=args.format)
    cprint(f"✓ Exported to {args.output}")
    return 0


def _run_odps_validate(args):
    """Run ODPS validate command."""
    import json
    from pathlib import Path

    file_path = Path(args.odps_file)
    with open(file_path) as f:
        if file_path.suffix in (".yaml", ".yml"):
            import yaml

            odps_data = yaml.safe_load(f)
        else:
            odps_data = json.load(f)

    # Validate required fields
    required_fields = ["apiVersion", "kind", "id", "name", "status"]
    missing = [f for f in required_fields if f not in odps_data]

    if missing:
        cprint(f"✗ Validation failed: Missing fields: {', '.join(missing)}")
        return 1

    if odps_data.get("kind") != "DataProduct":
        cprint("✗ Validation failed: kind must be 'DataProduct'")
        return 1

    cprint("✓ Validation passed")
    return 0


def _run_odps_info(args):
    """Run ODPS info command."""
    provider = OdpsStandardProvider()
    cprint("ODPS (Open Data Product Standard) Provider")
    cprint("=" * 50)
    cprint(f"Version: {provider.odps_version}")
    cprint(f"Specification: {provider.odps_spec_url}")
    return 0
