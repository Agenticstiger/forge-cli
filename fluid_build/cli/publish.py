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
from typing import List, Optional

from fluid_build.cli.console import cprint

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

    # Catalog selection
    catalog_group = p.add_argument_group("Catalog Selection")
    catalog_group.add_argument(
        "--catalog",
        "-c",
        default="fluid-command-center",
        help="Target catalog name (default: fluid-command-center)",
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

    # Get catalog config
    catalog_config = config.get_catalog_config(catalog_name)
    if not catalog_config:
        return PublishResult(
            success=False,
            catalog_id=catalog_name,
            asset_id=contract.get("id", str(contract_path)),
            error=f"Catalog '{catalog_name}' not configured",
        )

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
        asset = provider.map_contract_to_asset(contract)
        # Attach raw contract YAML so catalogs can store the full file
        asset.contract_yaml = contract_path.read_text(encoding="utf-8")
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

    if not args.quiet:
        logger.info(f"📤 Publishing {len(contract_paths)} contract(s) to '{args.catalog}'")

    # Publish each contract
    results = []
    for contract_path in contract_paths:
        result = await publish_contract(
            contract_path=contract_path,
            catalog_name=args.catalog,
            config=config,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            skip_health_check=args.skip_health_check,
            verbose=args.verbose,
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

    # Determine exit code
    success_count = sum(1 for r in results if r.success)
    if success_count == 0:
        return 1
    elif success_count < len(results):
        return 2  # Partial success
    else:
        return 0  # All success


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
