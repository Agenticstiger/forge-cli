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
FLUID Publish Command - Register Data Products to Catalogs

This command publishes FLUID contracts as assets to enterprise data catalogs,
making them discoverable for other teams and data consumers.

Workflow:
1. Load and validate FLUID contract
2. Map contract to catalog asset format
3. Publish to configured catalog(s)
4. Verify publication success
5. Display catalog URL

Features:
- Upsert logic (create or update existing)
- Retry with exponential backoff
- Circuit breaker for fault tolerance
- Health checking before publish
- Metrics collection
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from fluid_build.cli.console import cprint
from fluid_build.observability.tracing import traced_stage as _traced_stage

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ..config_manager import FluidConfig
from ..loader import load_contract
from ..providers.catalogs import PublishResult, get_catalog_provider
from ..providers.common import metrics_collector
from ._common import hydrate_dotenv, resolve_contract_env_templates

COMMAND = "publish"
logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction):
    """Register the publish command"""
    p = subparsers.add_parser(
        COMMAND,
        help="Publish data products to enterprise catalogs",
        epilog="""
📤 FLUID Publish - Register Data Products to Catalogs

The publish command registers your FLUID data products in enterprise catalogs,
making them discoverable for other teams, AI agents, and data consumers.

Examples:
  # Publish to default catalog (FLUID Command Center)
  fluid publish contract.fluid.yaml

  # Publish to specific catalog
  fluid publish contract.fluid.yaml --catalog fluid-command-center

  # Publish multiple contracts
  fluid publish customer-*.fluid.yaml

  # Dry run (validate without publishing)
  fluid publish contract.fluid.yaml --dry-run

  # Verify publication without publishing again
  fluid publish contract.fluid.yaml --verify-only

  # Publish with custom endpoint
  FLUID_CC_ENDPOINT=https://catalog.company.com fluid publish contract.fluid.yaml

Workflow:
  1. validate → Ensure contract is valid
  2. apply    → Deploy infrastructure (optional, can be separate)
  3. publish  → Register in catalog
  4. market   → Verify discoverability

Configuration:
  Set catalog config in ~/.fluid/config.yaml:
  
  catalogs:
    fluid-command-center:
      endpoint: https://catalog.company.com
      auth:
        type: api_key
      enabled: true

  Or use environment variables:
    FLUID_CC_ENDPOINT=https://catalog.company.com
    FLUID_API_KEY=fluid_xxxxx

Authentication:
  API Key:    Set FLUID_API_KEY environment variable
  Bearer:     Set FLUID_BEARER_TOKEN environment variable
  Basic:      Configure username/password in config file

The publish command enables the full data product lifecycle: develop → deploy → register → discover.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Contract file(s)
    p.add_argument(
        "contract_files",
        nargs="+",
        help="FLUID contract file(s) to publish (supports glob patterns)",
    )

    # Catalog / target selection
    catalog_group = p.add_argument_group("Catalog / Target Selection")
    catalog_group.add_argument(
        "--target",
        "-t",
        action="append",
        default=None,
        metavar="NAME[:ENDPOINT]",
        help=(
            "Target catalog to publish to. Format: ``<name>`` or "
            "``<name>:<endpoint>`` (endpoint override for this target only). "
            "Repeatable — ``--target command-center --target datahub`` "
            "publishes to both. Default: fluid-command-center. Per the "
            "11-stage pipeline design (perfect-pipeline.html), this is the "
            "canonical flag going forward; ``--catalog`` is a deprecated "
            "alias kept for one release."
        ),
    )
    catalog_group.add_argument(
        "--catalog",
        "-c",
        default=None,
        help=(
            "DEPRECATED: use --target instead (single-catalog form only). "
            "Kept for one release for back-compat. A warning is logged when "
            "used."
        ),
    )
    catalog_group.add_argument(
        "--list-catalogs", action="store_true", help="List configured catalogs and exit"
    )

    # Operation modes
    mode_group = p.add_argument_group("Operation Modes")
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate contract and show what would be published without actually publishing",
    )
    mode_group.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify if contract is already published (no create/update)",
    )
    mode_group.add_argument(
        "--force", action="store_true", help="Force update even if asset exists and is unchanged"
    )

    # Output options
    output_group = p.add_argument_group("Output Options")
    output_group.add_argument(
        "--format",
        "-f",
        choices=["text", "json", "yaml"],
        default="text",
        help="Output format (default: text)",
    )
    output_group.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output with detailed metrics"
    )
    output_group.add_argument(
        "--quiet", "-q", action="store_true", help="Minimal output (only errors and final result)"
    )

    # Advanced options
    advanced_group = p.add_argument_group("Advanced Options")
    advanced_group.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Skip catalog health check before publishing",
    )
    advanced_group.add_argument(
        "--show-metrics", action="store_true", help="Show detailed metrics after publish"
    )

    p.set_defaults(cmd=COMMAND, func=run)


async def publish_contract(
    contract_path: Path,
    catalog_name: str,
    config: FluidConfig,
    dry_run: bool = False,
    verify_only: bool = False,
    skip_health_check: bool = False,
    verbose: bool = False,
    endpoint_override: Optional[str] = None,
) -> PublishResult:
    """Publish a single contract to catalog

    Args:
        contract_path: Path to contract file
        catalog_name: Name of target catalog
        config: Configuration manager
        dry_run: If True, validate only without publishing
        verify_only: If True, only verify existence
        skip_health_check: If True, skip pre-publish health check
        verbose: If True, show detailed progress
        endpoint_override: When set, overrides the ``endpoint`` in the
            catalog config for this call only. Sourced from the
            ``--target name:endpoint`` CLI form.

    Returns:
        PublishResult with success/failure details
    """
    if verbose:
        logger.info(f"📄 Loading contract: {contract_path}")

    # Load contract
    try:
        contract = load_contract(str(contract_path))
    except Exception as e:
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=str(contract_path),
            error=f"Failed to load contract: {e}",
        )

    # Resolve {{ env.VAR }} templates across the whole contract before the
    # catalog adapter forwards it downstream. Without this, raw placeholders
    # land in the DMM server block (plan/apply resolve them per-string at the
    # Snowflake boundary — publish has no such boundary).
    contract = resolve_contract_env_templates(contract)

    # Get catalog config
    catalog_config = config.get_catalog_config(catalog_name)
    if not catalog_config and not endpoint_override:
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=contract.get("id", str(contract_path)),
            error=f"Catalog '{catalog_name}' not configured",
        )

    # Apply per-invocation endpoint override (from ``--target name:endpoint``).
    # Shallow-copy so we don't mutate the shared config dict across targets.
    # The override is intentionally allowed to seed an otherwise-empty
    # config so ``--target datahub:https://datahub.company.com`` works
    # without a prior YAML / env-var setup.
    if endpoint_override:
        catalog_config = dict(catalog_config) if catalog_config else {}
        catalog_config["endpoint"] = endpoint_override
        if verbose:
            logger.info(f"🔧 {catalog_name}: endpoint overridden to {endpoint_override}")

    if not catalog_config.get("enabled", True):
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=contract.get("id", str(contract_path)),
            error=f"Catalog '{catalog_name}' is disabled in configuration",
        )

    # Create provider instance
    try:
        provider = get_catalog_provider(catalog_name, catalog_config)
    except Exception as e:
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=contract.get("id", str(contract_path)),
            error=f"Failed to create catalog provider: {e}",
        )

    # Map contract to asset
    try:
        import yaml as _yaml

        asset = provider.map_contract_to_asset(contract)
        # Attach the env-resolved contract YAML so downstream catalogs parse the
        # same values the dict pass has — reading contract_path.read_text()
        # would re-introduce the raw ``{{ env.VAR }}`` placeholders.
        asset.contract_yaml = _yaml.safe_dump(contract, sort_keys=False)
    except Exception as e:
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=contract.get("id", str(contract_path)),
            error=f"Failed to map contract to asset: {e}",
        )

    if verbose:
        logger.info(f"📦 Mapped contract to asset: {asset.name} (ID: {asset.id})")

    # Verify-only mode
    if verify_only:
        exists = await provider.verify(asset.id)
        return PublishResult(
            success=exists,
            catalog_id=catalog_name,
            asset_id=asset.id,
            error=None if exists else "Asset not found in catalog",
            details={"verified": exists, "operation": "verify"},
        )

    # Dry-run mode
    if dry_run:
        is_valid, error_msg = provider.validate_asset(asset)
        return PublishResult(
            success=is_valid,
            catalog_id=catalog_name,
            asset_id=asset.id,
            error=error_msg,
            details={"dry_run": True, "valid": is_valid},
        )

    # Health check (unless skipped)
    if not skip_health_check:
        if verbose:
            logger.info("🏥 Checking catalog health...")

        is_healthy = await provider.health_check()
        if not is_healthy:
            return PublishResult(
                success=False,
                catalog_id=catalog_name,
                asset_id=asset.id,
                error="Catalog health check failed - endpoint not accessible",
            )

    # Publish!
    if verbose:
        logger.info(f"🚀 Publishing to {catalog_name}...")

    result = await provider.publish(asset)
    return result


def format_results(
    results: List[PublishResult], format: str = "text", console: Optional[Console] = None
) -> str:
    """Format publish results

    Args:
        results: List of publish results
        format: Output format (text, json, yaml)
        console: Rich console for formatted output

    Returns:
        Formatted output string
    """
    if format == "json":
        import json

        return json.dumps(
            [
                {
                    "success": r.success,
                    "catalog_id": r.catalog_id,
                    "asset_id": r.asset_id,
                    "catalog_url": r.catalog_url,
                    "error": r.error,
                    "details": r.details,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in results
            ],
            indent=2,
        )

    elif format == "yaml":
        import yaml

        return yaml.dump(
            [
                {
                    "success": r.success,
                    "catalog_id": r.catalog_id,
                    "asset_id": r.asset_id,
                    "catalog_url": r.catalog_url,
                    "error": r.error,
                    "details": r.details,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in results
            ]
        )

    else:  # text format
        if console and RICH_AVAILABLE:
            table = Table(title="📤 Publish Results")
            table.add_column("Asset ID", style="cyan")
            table.add_column("Status", style="bold")
            table.add_column("Catalog", style="magenta")
            table.add_column("Details")

            for r in results:
                status = "✅ Success" if r.success else "❌ Failed"
                details = r.catalog_url if r.success else r.error
                table.add_row(r.asset_id, status, r.catalog_id, details or "")

            console.print(table)
            return ""
        else:
            # Plain text output
            output = []
            output.append("=" * 80)
            output.append("📤 Publish Results")
            output.append("=" * 80)
            for r in results:
                output.append(f"\nAsset: {r.asset_id}")
                output.append(f"  Status: {'✅ Success' if r.success else '❌ Failed'}")
                output.append(f"  Catalog: {r.catalog_id}")
                if r.success and r.catalog_url:
                    output.append(f"  URL: {r.catalog_url}")
                if r.error:
                    output.append(f"  Error: {r.error}")
            output.append("=" * 80)
            return "\n".join(output)


async def run_async(args, logger: logging.Logger) -> int:
    """Async main execution logic"""
    # Hydrate os.environ from project dotenv files and FLUID_SECRETS_FILE before
    # FluidConfig reads catalog credentials. fluid apply gets this for free via
    # the credential resolver chain; publish has no such chain, so a subprocess
    # that only sources a launchpad (which exports only FLUID_SECRETS_FILE)
    # would otherwise see empty DMM_API_KEY and fail the health check.
    hydrate_dotenv(Path.cwd(), environment=getattr(args, "env", None))

    config = FluidConfig()
    console = Console() if RICH_AVAILABLE else None

    # Handle --list-catalogs
    if args.list_catalogs:
        catalogs = config.get_catalog_config()
        if console:
            table = Table(title="🗂️ Configured Catalogs")
            table.add_column("Name", style="cyan")
            table.add_column("Endpoint", style="magenta")
            table.add_column("Enabled", style="bold")
            table.add_column("Auth Type")

            for name, conf in catalogs.items():
                enabled = "✅ Yes" if conf.get("enabled", True) else "❌ No"
                auth_type = conf.get("auth", {}).get("type", "none")
                table.add_row(name, conf.get("endpoint", "N/A"), enabled, auth_type)

            console.print(table)
        else:
            cprint("\n🗂️ Configured Catalogs:")
            cprint("=" * 80)
            for name, conf in catalogs.items():
                cprint(f"\n{name}:")
                cprint(f"  Endpoint: {conf.get('endpoint', 'N/A')}")
                cprint(f"  Enabled: {'Yes' if conf.get('enabled', True) else 'No'}")
                cprint(f"  Auth: {conf.get('auth', {}).get('type', 'none')}")
        return 0

    # Expand glob patterns in contract files
    from glob import glob

    contract_paths = []
    for pattern in args.contract_files:
        matches = glob(pattern)
        if matches:
            contract_paths.extend([Path(m) for m in matches])
        else:
            contract_paths.append(Path(pattern))

    if not contract_paths:
        logger.error("No contract files specified")
        return 1

    # Validate contract files exist
    invalid_paths = [p for p in contract_paths if not p.exists()]
    if invalid_paths:
        logger.error(f"Contract files not found: {', '.join(str(p) for p in invalid_paths)}")
        return 1

    # F1 / F6: validate every (glob-expanded) contract path through the
    # platform-aware path validator — traversal, forbidden system paths,
    # symlink — before ``load_contract`` / ``load_contract_with_overlay``
    # touches it. Glob *patterns* are expanded above; the concrete paths
    # they resolve to are screened here.
    from fluid_build.cli.core import FluidCLIError as _FluidCLIError
    from fluid_build.cli.security import validate_cli_path

    try:
        contract_paths = [
            validate_cli_path(p, mode="read", file_type="contract") for p in contract_paths
        ]
    except _FluidCLIError as exc:
        logger.error(f"Rejected contract path: {exc.message}")
        return 1

    # Resolve the target list from --target (new) + --catalog (deprecated).
    # Format: list of (name, endpoint_override or None). If neither flag is
    # set, default to a single fluid-command-center target — matches the
    # pre-11-stage-pipeline behavior so existing ``fluid publish X.yaml``
    # invocations keep working.
    targets: List[Tuple[str, Optional[str]]] = []
    if args.target:
        for raw in args.target:
            if not raw:
                continue
            name, sep, endpoint = raw.partition(":")
            targets.append((name.strip(), endpoint.strip() or None))
    if args.catalog:
        logger.warning(
            "--catalog is deprecated; use --target instead. "
            "--catalog will be removed in the next release."
        )
        targets.append((args.catalog, None))
    if not targets:
        targets.append(("fluid-command-center", None))

    if not args.quiet:
        target_summary = ", ".join(f"{n}{':' + e if e else ''}" for n, e in targets)
        logger.info(
            f"📤 Publishing {len(contract_paths)} contract(s) "
            f"to {len(targets)} target(s): {target_summary}"
        )

    # Publish each contract to every target. Endpoint overrides (from the
    # ``--target name:endpoint`` form) apply to that target only; other
    # targets resolve their endpoint via the normal config lookup.
    results = []
    for contract_path in contract_paths:
        for target_name, endpoint_override in targets:
            result = await publish_contract(
                contract_path=contract_path,
                catalog_name=target_name,
                config=config,
                dry_run=args.dry_run,
                verify_only=args.verify_only,
                skip_health_check=args.skip_health_check,
                verbose=args.verbose,
                endpoint_override=endpoint_override,
            )
            results.append(result)

    # Display results
    output = format_results(results, args.format, console)
    if output:
        cprint(output)

    # Show metrics if requested
    if args.show_metrics:
        metrics = metrics_collector.get_summary()
        if console:
            panel = Panel.fit(
                f"Total Requests: {metrics['total_requests']}\n"
                f"Success Rate: {metrics['success_rate']}%\n"
                f"Total Failures: {metrics['total_failures']}\n"
                f"Health Score: {metrics_collector.get_health_score():.2%}",
                title="📊 Metrics",
                border_style="blue",
            )
            console.print(panel)
        else:
            cprint("\n📊 Metrics:")
            cprint(f"  Total Requests: {metrics['total_requests']}")
            cprint(f"  Success Rate: {metrics['success_rate']}%")
            cprint(f"  Total Failures: {metrics['total_failures']}")

    # ── Acquisition pattern: catalog auto-registration ─────────────────
    # Bronze contracts with ``properties.catalog.register`` push their
    # exposes through the acquisition catalog dispatcher in addition to
    # the standard publish flow above. Failures don't block publish
    # exit code — they're surfaced via the warning channel because
    # catalog auto-registration is observability, not correctness.
    try:
        from fluid_build.cli._acquisition_stage_ext import (
            is_acquisition_contract,
            publish_acquisition,
        )

        for cp in contract_paths:
            try:
                from fluid_build.loader import load_contract_with_overlay

                contract = load_contract_with_overlay(str(cp), getattr(args, "env", None), logger)
            except Exception:
                continue
            if not is_acquisition_contract(contract):
                continue
            acq_results = publish_acquisition(contract, Path.cwd())
            for r in acq_results:
                icon = "✅" if r.succeeded else "⚠️"
                cprint(
                    f"  {icon} acquisition publish "
                    f"{r.product_id}/{r.expose_id} → {r.target}"
                    + (f"  [{r.error}]" if r.error else "")
                )
    except Exception as exc:  # noqa: BLE001 — publish must not crash the CLI
        logger.warning(f"Acquisition catalog dispatch skipped: {exc}")

    # Determine exit code
    success_count = sum(1 for r in results if r.success)
    if success_count == 0:
        return 1
    elif success_count < len(results):
        return 2  # Partial success
    else:
        return 0  # All success


@_traced_stage("publish")
def run(args, logger: logging.Logger) -> int:
    """Main entry point for publish command"""
    try:
        return asyncio.run(run_async(args, logger))
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Publish failed: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1
