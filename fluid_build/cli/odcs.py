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

# fluid_build/cli/odcs.py
"""
ODCS (Open Data Contract Standard) CLI Commands

Commands for bidirectional conversion between FLUID and ODCS formats.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import click

from fluid_build.cli.console import cprint
from fluid_build.loader import load_contract
from fluid_build.providers.odcs import OdcsProvider


@click.group(name="odcs")
def odcs_cli():
    """
    ODCS (Open Data Contract Standard) commands.

    Bidirectional conversion between FLUID and ODCS v3.1.0 format.
    """
    pass


@odcs_cli.command(name="export")
@click.argument("contract", type=click.Path(exists=True))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: <contract-name>-odcs.yaml). Ignored when --per-port is used.",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["yaml", "json"], case_sensitive=False),
    default="yaml",
    help="Output format (default: yaml)",
)
@click.option("--quality/--no-quality", default=True, help="Include quality checks (default: true)")
@click.option("--sla/--no-sla", default=True, help="Include SLA properties (default: true)")
@click.option(
    "--expose-id",
    "-e",
    default=None,
    metavar="EXPOSE_ID",
    help=(
        "Export a single output port by its exposeId. "
        "Produces one ODCS contract scoped to that port with id "
        "'<product-id>.<exposeId>'. Mutually exclusive with --per-port."
    ),
)
@click.option(
    "--per-port",
    is_flag=True,
    default=False,
    help=(
        "Export every output port as a separate ODCS file "
        "(product.odcs.<exposeId>.<format> written to the same directory "
        "as --output, or the current directory if --output is omitted). "
        "Mutually exclusive with --expose-id."
    ),
)
def export_command(
    contract: str,
    output: Optional[str],
    format: str,
    quality: bool,
    sla: bool,
    expose_id: Optional[str],
    per_port: bool,
):
    """
    Export FLUID contract to ODCS format.

    Example:
        fluid odcs export my-contract.yaml
        fluid odcs export my-contract.yaml -o contract.json -f json
        fluid odcs export my-contract.yaml --expose-id bitcoin_prices_table
        fluid odcs export my-contract.yaml --per-port -o standards/product.odcs.yaml
        fluid odcs export my-contract.yaml --no-quality
    """
    logger = logging.getLogger(__name__)

    if expose_id and per_port:
        raise click.UsageError("--expose-id and --per-port are mutually exclusive.")

    try:
        # Load FLUID contract
        click.echo(f"Loading FLUID contract: {contract}")
        fluid_contract = load_contract(contract)

        # Configure provider
        provider = OdcsProvider()
        provider.include_quality_checks = quality
        provider.include_sla = sla

        click.echo(f"Exporting to ODCS v{provider.odcs_version}...")

        # ── Per-port mode ────────────────────────────────────────────────────
        if per_port:
            out_dir = Path(output).parent if output else Path(".")
            out_dir.mkdir(parents=True, exist_ok=True)
            results = provider.render_all_ports(fluid_contract, out_dir=out_dir, fmt=format)
            if not results:
                click.echo("WARNING: No exposes found in contract — nothing exported.", err=True)
                return
            for eid, odcs in results:
                out_path = out_dir / f"product.odcs.{eid}.{format}"
                click.echo(f"✓ Exported {eid} → {out_path}")
                click.echo(
                    f"  ID: {odcs.get('id')}  status: {odcs.get('status')}  version: {odcs.get('version')}"
                )
            return

        # ── Single-expose mode ───────────────────────────────────────────────
        if expose_id:
            if not output:
                contract_path = Path(contract)
                output = contract_path.stem + f"-odcs-{expose_id}.{format}"
            odcs_contract = provider.render(
                fluid_contract, out=output, fmt=format, expose_id=expose_id
            )
            click.echo(f"✓ Successfully exported expose '{expose_id}' to {output}")
        else:
            # ── Legacy mode: all exposes merged into one file ────────────────
            if not output:
                contract_path = Path(contract)
                output = contract_path.stem + f"-odcs.{format}"
            odcs_contract = provider.render(fluid_contract, out=output, fmt=format)
            click.echo(f"✓ Successfully exported to {output}")

        # Show summary
        schema = odcs_contract.get("schema", [])
        servers = odcs_contract.get("servers", [])

        click.echo(f"\nData Contract: {odcs_contract.get('name', odcs_contract.get('id'))}")
        click.echo(f"ID: {odcs_contract.get('id')}")
        click.echo(f"Version: {odcs_contract.get('version')}")
        click.echo(f"Status: {odcs_contract.get('status')}")
        click.echo(f"Schema Fields: {len(schema)}")
        click.echo(f"Servers: {len(servers)}")

        if servers:
            click.echo("\nServers:")
            for server in servers:
                click.echo(f"  - {server.get('name')} ({server.get('type')})")

    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        click.echo(f"✗ Export failed: {e}", err=True)
        raise click.Abort()


@odcs_cli.command(name="import")
@click.argument("odcs_file", type=click.Path(exists=True))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: <contract-name>-fluid.yaml)",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["yaml", "json"], case_sensitive=False),
    default="yaml",
    help="Output format (default: yaml)",
)
def import_command(odcs_file: str, output: Optional[str], format: str):
    """
    Import ODCS contract to FLUID format.

    Example:
        fluid odcs import contract.yaml
        fluid odcs import contract.json -o my-contract.yaml
    """
    logger = logging.getLogger(__name__)

    try:
        # Import ODCS contract
        click.echo(f"Loading ODCS contract: {odcs_file}")
        provider = OdcsProvider()
        fluid_contract = provider.import_contract(odcs_file)

        # Generate output path if not specified
        if not output:
            odcs_path = Path(odcs_file)
            output = odcs_path.stem + f"-fluid.{format}"

        # Write FLUID contract
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            if format == "yaml":
                import yaml

                yaml.dump(fluid_contract, f, default_flow_style=False, sort_keys=False)
            else:
                json.dump(fluid_contract, f, indent=2)

        click.echo(f"✓ Successfully imported to {output}")

        # Show summary
        exposes = fluid_contract.get("exposes", [])
        expects = fluid_contract.get("expects", [])

        click.echo(f"\nFLUID Contract: {fluid_contract['metadata']['name']}")
        click.echo(f"ID: {fluid_contract['contract']['id']}")
        click.echo(f"Version: {fluid_contract['metadata']['version']}")
        click.echo(f"Status: {fluid_contract['metadata']['status']}")
        click.echo(f"Exposes: {len(exposes)}")
        click.echo(f"Expects: {len(expects)}")

    except Exception as e:
        logger.error(f"Import failed: {e}", exc_info=True)
        click.echo(f"✗ Import failed: {e}", err=True)
        raise click.Abort()


@odcs_cli.command(name="validate")
@click.argument("odcs_file", type=click.Path(exists=True))
def validate_command(odcs_file: str):
    """
    Validate ODCS contract file against JSON Schema.

    Example:
        fluid odcs validate contract.yaml
    """
    logger = logging.getLogger(__name__)

    try:
        # Load ODCS file
        click.echo(f"Loading ODCS file: {odcs_file}")

        from fluid_build.cli._common import load_yaml_json

        file_path = Path(odcs_file)
        odcs_data = load_yaml_json(file_path)

        # Validate using provider
        provider = OdcsProvider()
        provider.validate_contract(odcs_data)

        click.echo("✓ Validation passed")

        # Show summary
        schema = odcs_data.get("schema", [])
        servers = odcs_data.get("servers", [])

        click.echo(f"\nData Contract: {odcs_data.get('name', odcs_data.get('id'))}")
        click.echo(f"ID: {odcs_data.get('id')}")
        click.echo(f"Version: {odcs_data.get('version')}")
        click.echo(f"API Version: {odcs_data.get('apiVersion')}")
        click.echo(f"Status: {odcs_data.get('status')}")
        click.echo(f"Schema Fields: {len(schema)}")
        click.echo(f"Servers: {len(servers)}")

    except Exception as e:
        logger.error(f"Validation failed: {e}", exc_info=True)
        click.echo(f"✗ Validation failed: {e}", err=True)
        raise click.Abort()


@odcs_cli.command(name="info")
def info_command():
    """
    Show ODCS provider information.
    """
    provider = OdcsProvider()

    click.echo("ODCS (Open Data Contract Standard) Provider")
    click.echo("=" * 50)
    click.echo(f"Version: {provider.odcs_version}")
    click.echo(f"Specification: {provider.odcs_spec_url}")
    click.echo("\nCapabilities:")

    caps = provider.capabilities()
    for cap, enabled in caps.items():
        status = "✓" if enabled else "✗"
        click.echo(f"  {status} {cap}")

    click.echo("\nSupported Formats:")
    click.echo("  - YAML (import/export)")
    click.echo("  - JSON (import/export)")

    click.echo("\nJSON Schema:")
    if provider.schema:
        click.echo(f"  ✓ Loaded (v{provider.odcs_version})")
    else:
        click.echo("  ✗ Not found")

    click.echo("\nUsage:")
    click.echo("  fluid odcs export <contract.yaml>      # FLUID → ODCS")
    click.echo("  fluid odcs import <contract.yaml>      # ODCS → FLUID")
    click.echo("  fluid odcs validate <contract.yaml>    # Validate ODCS")


if __name__ == "__main__":
    odcs_cli()


def register(subparsers) -> None:
    """Register ODCS commands with the main CLI."""

    odcs = subparsers.add_parser(
        "odcs",
        help="ODCS (Open Data Contract Standard - Bitol.io) commands",
        description="""
        Work with ODCS (Open Data Contract Standard) format from Bitol.io.
        
        ODCS provides bidirectional conversion between FLUID and ODCS formats,
        supporting data contract schema, quality, and SLA specifications.
        
        Official Specification: https://github.com/bitol-io/open-data-contract-standard
        """,
    )

    odcs_sub = odcs.add_subparsers(dest="odcs_command", help="ODCS operations")

    # odcs export
    export = odcs_sub.add_parser("export", help="Export FLUID contract to ODCS format")
    export.add_argument("contract", help="Path to FLUID contract file")
    export.add_argument("--output", "-o", help="Output file path")
    export.add_argument(
        "--format", "-f", choices=["yaml", "json"], default="yaml", help="Output format"
    )
    export.add_argument("--no-quality", action="store_true", help="Exclude quality checks")
    export.add_argument("--no-sla", action="store_true", help="Exclude SLA properties")
    export.set_defaults(func=lambda args, logger=None: _run_odcs_export(args))

    # odcs import
    import_cmd = odcs_sub.add_parser("import", help="Import ODCS contract to FLUID format")
    import_cmd.add_argument("odcs_file", help="Path to ODCS contract file")
    import_cmd.add_argument("--output", "-o", help="Output file path")
    import_cmd.add_argument(
        "--format", "-f", choices=["yaml", "json"], default="yaml", help="Output format"
    )
    import_cmd.set_defaults(func=lambda args, logger=None: _run_odcs_import(args))

    # odcs validate
    validate = odcs_sub.add_parser(
        "validate",
        help="Validate ODCS contract file",
        description=(
            "Validate an ODCS YAML/JSON file. Default: jsonschema first-pass + "
            "vowl second-pass (when installed via `pip install fluid-build[odcs-strict]`). "
            "All violations are collected — exit code is 1 if any check fails."
        ),
    )
    validate.add_argument("odcs_file", help="Path to ODCS file")
    validate.add_argument(
        "--report",
        dest="report",
        default=None,
        help="Write structured JSON validation report to this path (for CI gates).",
    )
    validate.add_argument(
        "--roundtrip",
        action="store_true",
        help="Also verify the contract round-trips losslessly through "
        "OdcsProvider.import_contract → render and report any diff.",
    )
    validate.add_argument(
        "--no-vowl",
        dest="vowl",
        action="store_false",
        default=True,
        help="Skip the vowl second-pass even if installed.",
    )
    validate.set_defaults(func=lambda args, logger=None: _run_odcs_validate(args))

    # odcs info
    info = odcs_sub.add_parser("info", help="Show ODCS provider information")
    info.set_defaults(func=lambda args, logger=None: _run_odcs_info(args))


def _run_odcs_export(args):
    """Run ODCS export command."""
    import logging

    from fluid_build.cli.bootstrap import load_contract_with_overlay

    logger = logging.getLogger(__name__)

    # Load contract
    fluid_contract = load_contract_with_overlay(args.contract, None, logger)

    # Configure provider
    provider = OdcsProvider()
    if args.no_quality:
        provider.include_quality_checks = False
    if args.no_sla:
        provider.include_sla = False

    # Generate output path if not specified
    if not args.output:
        from pathlib import Path

        contract_path = Path(args.contract)
        args.output = contract_path.stem + f"-odcs.{args.format}"

    # Export
    provider.render(fluid_contract, out=args.output, fmt=args.format)
    cprint(f"✓ Exported to {args.output}")
    return 0


def _run_odcs_import(args):
    """Run ODCS import command."""
    import json
    from pathlib import Path

    # Import ODCS
    provider = OdcsProvider()
    fluid_contract = provider.import_contract(args.odcs_file)

    # Generate output path if not specified
    if not args.output:
        odcs_path = Path(args.odcs_file)
        args.output = odcs_path.stem + f"-fluid.{args.format}"

    # Write FLUID contract
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        if args.format == "yaml":
            import yaml

            yaml.dump(fluid_contract, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(fluid_contract, f, indent=2)

    cprint(f"✓ Imported to {args.output}")
    return 0


def _run_odcs_validate(args):
    """Run ODCS validate command — multi-pass with structured report.

    Passes:
      1. jsonschema against the vendored ODCS v3.1.0 schema. ALL violations
         collected via collect_errors().
      2. vowl second-pass (when installed via the ``odcs-strict`` extra).
         Catches semantic issues jsonschema misses (format validation,
         unevaluated-properties enforcement, …).
      3. (opt-in) round-trip: import → render → diff. Catches mapper
         regressions that don't break the spec but lose data.

    Exit code is 1 iff any pass surfaces a violation. With ``--report PATH``
    every result is also serialised to a JSON file for CI gates.
    """
    import json
    from pathlib import Path

    from fluid_build.cli._common import load_yaml_json
    from fluid_build.providers.odcs.validation import (
        collect_errors,
        load_schema,
        roundtrip_check,
        validate_via_vowl,
    )

    file_path = Path(args.odcs_file)
    odcs_data = load_yaml_json(file_path)

    report: dict = {
        "file": str(file_path),
        "passes": {},
        "ok": True,
    }

    # Pass 1 — jsonschema (multi-error)
    schema = load_schema()
    if schema:
        errors = collect_errors(odcs_data, schema)
        report["passes"]["jsonschema"] = {
            "ok": not errors,
            "errors": errors,
        }
        if errors:
            report["ok"] = False
            cprint(f"✗ jsonschema: {len(errors)} error(s)")
            for e in errors[:10]:
                cprint(f"   {e['path'] or '<root>'}: {e['message'][:120]}")
            if len(errors) > 10:
                cprint(f"   ... +{len(errors) - 10} more")
        else:
            cprint("✓ jsonschema: clean")
    else:
        report["passes"]["jsonschema"] = {
            "ok": False,
            "errors": [],
            "skipped": "schema not vendored",
        }

    # Pass 2 — vowl (opt-in; defaults on when installed)
    vowl_enabled = getattr(args, "vowl", True)
    if vowl_enabled:
        try:
            diag = validate_via_vowl(odcs_data)
        except Exception as exc:  # noqa: BLE001 — vowl bundles its own errors
            report["passes"]["vowl"] = {"ok": False, "error": str(exc)[:300]}
            report["ok"] = False
            cprint(f"✗ vowl: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            if diag is None:
                report["passes"]["vowl"] = {"ok": True, "skipped": "vowl not installed"}
                cprint("○ vowl: not installed (pip install 'fluid-build[odcs-strict]')")
            else:
                report["passes"]["vowl"] = {"ok": True, "diagnostics": diag}
                cprint(
                    f"✓ vowl: api={diag.get('api_version')} "
                    f"schemas={len(diag.get('schemas', []))} "
                    f"checks={diag.get('total_checks')}"
                )

    # Pass 3 — round-trip (opt-in)
    if getattr(args, "roundtrip", False):
        try:
            rt = OdcsProvider().roundtrip_check(odcs_data)
        except Exception as exc:  # noqa: BLE001
            report["passes"]["roundtrip"] = {"ok": False, "error": str(exc)[:300]}
            report["ok"] = False
            cprint(f"✗ roundtrip: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            report["passes"]["roundtrip"] = rt
            if rt["equal"]:
                cprint("✓ roundtrip: lossless")
            else:
                report["ok"] = False
                cprint(
                    f"✗ roundtrip: missing={len(rt['missing'])} "
                    f"extra={len(rt['extra'])} changed={len(rt['changed'])}"
                )

    # Optional structured report (CI gate)
    if getattr(args, "report", None):
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        cprint(f"📄 Report: {report_path}")

    return 0 if report["ok"] else 1


def _run_odcs_info(args):
    """Run ODCS info command."""
    provider = OdcsProvider()
    cprint("ODCS (Open Data Contract Standard) Provider")
    cprint("=" * 50)
    cprint(f"Version: {provider.odcs_version}")
    cprint(f"Specification: {provider.odcs_spec_url}")
    cprint(f"Schema: {'Loaded' if provider.schema else 'Not found'}")
    return 0
