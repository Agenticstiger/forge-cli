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
FLUID Build - Blueprint Marketplace Commands

Commands for interacting with the FLUID Blueprint Marketplace API.
Marketplace blueprints are Jinja2 templates that generate FLUID 0.7.x contracts.

Usage:
    fluid market --blueprints --search analytics
    fluid market --blueprint-id customer-360-etl
    fluid market --blueprint-id customer-360-etl
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

# NOTE: ``requests`` (~107 modules incl urllib3/certifi/charset_normalizer) is
# resolved lazily — at import time it would land on the cold ``fluid --help``
# path (this module is imported at registration). ``_resolve_requests`` performs
# the import on first use and caches it into the module global, and the PEP 562
# ``__getattr__`` exposes ``requests`` as a module attribute so the
# ``patch("…cli.marketplace.requests")`` test seam keeps working. See the A++
# Light CLI startup card.
_REQUESTS_UNRESOLVED = object()  # sentinel: import not yet attempted


def _resolve_requests():
    """Import ``requests`` lazily, caching the module (or ``None``) in globals.

    Honors a test ``patch("…cli.marketplace.requests")``: once the module
    attribute is set, that value is returned unchanged.
    """
    current = globals().get("requests", _REQUESTS_UNRESOLVED)
    if current is not _REQUESTS_UNRESOLVED:
        return current
    try:
        import requests as _requests
    except ImportError:
        _requests = None  # type: ignore[assignment]
    globals()["requests"] = _requests
    return _requests


def __getattr__(name: str):
    if name == "requests":
        return _resolve_requests()
    if name == "get_command_center_client":
        from ._command_center import get_command_center_client

        globals()[name] = get_command_center_client
        return get_command_center_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.syntax import Syntax
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ._common import CLIError
from .console import cprint

# NOTE: ``get_command_center_client`` lives in ``cli._command_center``, which
# imports ``requests`` (~107 modules) at module scope. Importing it here eagerly
# would pull ``requests`` onto the cold ``fluid --help`` path (this module is
# imported at registration). It's resolved lazily via ``__getattr__`` below — and
# exposing it as a module attribute keeps the
# ``patch("…cli.marketplace.get_command_center_client")`` test seam working. See
# the A++ Light CLI startup card.

COMMAND = "marketplace"

# Default API endpoint (can be overridden via env or config)
DEFAULT_API_URL = "http://localhost:8000/api/v1/blueprints-marketplace"
MARKETPLACE_HTTP_TIMEOUT_SECONDS = 10
MARKETPLACE_HTTP_RETRIES = 1

# Fallback options when Command Center unavailable
FALLBACK_OPTIONS = {
    "local": lambda: str(Path.home() / ".fluid" / "blueprints"),
    "public": lambda: os.getenv("FLUID_PUBLIC_REGISTRY"),
    "none": lambda: None,
}

console = Console() if RICH_AVAILABLE else None


def _requests_exception_type(name: str) -> type[BaseException] | None:
    """Return a requests exception class, tolerating patched test doubles."""
    requests = _resolve_requests()
    exceptions = getattr(requests, "exceptions", None)
    exc_type = getattr(exceptions, name, None)
    if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
        return exc_type
    return None


def _is_requests_exception(exc: BaseException, name: str) -> bool:
    exc_type = _requests_exception_type(name)
    return bool(exc_type and isinstance(exc, exc_type))


def _marketplace_request(request_func, url: str, logger: logging.Logger, **kwargs):
    """Run a marketplace HTTP request with bounded timeout and one transient retry."""
    timeout = kwargs.pop("timeout", MARKETPLACE_HTTP_TIMEOUT_SECONDS)
    request_error = _requests_exception_type("RequestException") or Exception
    transient_errors = tuple(
        exc_type
        for exc_type in (
            _requests_exception_type("Timeout"),
            _requests_exception_type("ConnectionError"),
        )
        if exc_type is not None
    )

    for attempt in range(MARKETPLACE_HTTP_RETRIES + 1):
        try:
            return request_func(url, timeout=timeout, **kwargs)
        except request_error as exc:
            is_transient = bool(transient_errors and isinstance(exc, transient_errors))
            if not is_transient or attempt >= MARKETPLACE_HTTP_RETRIES:
                raise
            logger.warning("Marketplace request failed transiently; retrying once: %s", exc)

    raise RuntimeError("unreachable marketplace retry state")  # pragma: no cover


def _marketplace_get(url: str, logger: logging.Logger, **kwargs):
    requests = _resolve_requests()
    return _marketplace_request(requests.get, url, logger, **kwargs)


def _marketplace_post(url: str, logger: logging.Logger, **kwargs):
    requests = _resolve_requests()
    return _marketplace_request(requests.post, url, logger, **kwargs)


def _format_marketplace_error(exc: BaseException) -> str:
    if _is_requests_exception(exc, "Timeout"):
        return (
            "Marketplace API request timed out after "
            f"{MARKETPLACE_HTTP_TIMEOUT_SECONDS}s. Check the API URL or try again."
        )
    if _is_requests_exception(exc, "ConnectionError"):
        return "Could not connect to the marketplace API. Check Command Center or FLUID_API_URL."

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code:
        body = str(getattr(response, "text", "") or "").strip()
        detail = f": {body[:200]}" if body else ""
        return f"Marketplace API returned HTTP {status_code}{detail}"

    return f"Marketplace API request failed: {exc}"


def register(subparsers: argparse._SubParsersAction):
    """Register the marketplace command (deprecated — use 'fluid market --blueprints')."""
    p = subparsers.add_parser(
        COMMAND,
        help=argparse.SUPPRESS,  # hidden — deprecated, use 'fluid market --blueprints'
        description="Deprecated: use 'fluid market --blueprints' instead.",
    )

    marketplace_subparsers = p.add_subparsers(dest="marketplace_action", help="Marketplace actions")

    # Search blueprints
    search_parser = marketplace_subparsers.add_parser(
        "search", help="Search marketplace blueprints"
    )
    search_parser.add_argument("query", nargs="?", help="Search query (keywords, categories)")
    search_parser.add_argument(
        "--category", help="Filter by category (analytics, streaming, ml, governance)"
    )
    search_parser.add_argument("--tags", help="Filter by tags (comma-separated)")
    search_parser.add_argument(
        "--maturity", help="Filter by maturity (experimental, stable, production)"
    )
    search_parser.add_argument(
        "--state", default="published", help="Filter by state (published, draft, deprecated)"
    )
    search_parser.add_argument(
        "--sort", default="downloads", help="Sort by (downloads, updated, name)"
    )
    search_parser.add_argument("--limit", type=int, default=20, help="Max results")

    # Get blueprint info
    info_parser = marketplace_subparsers.add_parser(
        "info", help="Show detailed blueprint information"
    )
    info_parser.add_argument("blueprint_id", help="Blueprint ID (e.g., customer-360-etl)")
    info_parser.add_argument("--version", help="Specific version (default: latest)")
    info_parser.add_argument("--show-template", action="store_true", help="Show Jinja2 template")

    # Instantiate blueprint
    instantiate_parser = marketplace_subparsers.add_parser(
        "instantiate", help="Generate FLUID contract from blueprint"
    )
    instantiate_parser.add_argument("blueprint_id", help="Blueprint ID")
    instantiate_parser.add_argument("--params", help="Parameters as JSON string or file path")
    instantiate_parser.add_argument(
        "--interactive", "-i", action="store_true", help="Interactive parameter wizard"
    )
    instantiate_parser.add_argument("--output", "-o", help="Output file for generated contract")
    instantiate_parser.add_argument(
        "--validate", action="store_true", default=True, help="Validate generated contract"
    )
    instantiate_parser.add_argument(
        "--submit", action="store_true", help="Submit contract to FLUID immediately"
    )

    # List categories
    _categories_parser = marketplace_subparsers.add_parser(  # noqa: F841
        "categories", help="List available blueprint categories"
    )

    p.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    """Run the marketplace command."""
    try:
        if console:
            console.print(
                "[yellow]Note: 'fluid marketplace' is deprecated. "
                "Use 'fluid market --blueprints' instead.[/yellow]\n"
            )
        else:
            cprint(
                "Note: 'fluid marketplace' is deprecated. Use 'fluid market --blueprints' instead.\n"
            )

        if not args.marketplace_action:
            logger.error("Marketplace action required. Use --help for available actions.")
            return 1

        # Bundled blueprints ship in the package and need no registry. Resolve
        # the registry URL only as far as the action actually needs it:
        #   • search       — always list bundled; merge the registry when it is
        #                     reachable, so a missing marketplace is non-fatal.
        #   • bundled id    — served entirely offline; skip the registry probe.
        #   • registry id   — require a reachable registry (original behaviour).
        from fluid_build.cli._market_bundled_blueprints import is_bundled

        action = args.marketplace_action
        bp_id = getattr(args, "blueprint_id", None)

        if action == "search":
            api_url = get_api_url(logger=logger, required=False)
        elif bp_id and is_bundled(bp_id):
            api_url = None
        else:
            api_url = get_api_url(logger=logger, required=True)

        if args.marketplace_action == "search":
            return search_blueprints(args, logger, api_url)
        elif args.marketplace_action == "info":
            return show_blueprint_info(args, logger, api_url)
        elif args.marketplace_action == "instantiate":
            return instantiate_blueprint(args, logger, api_url)
        elif args.marketplace_action == "categories":
            return list_categories(args, logger, api_url)
        else:
            logger.error(f"Unknown marketplace action: {args.marketplace_action}")
            return 1

    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user[/yellow]")
        return 1
    except Exception as e:
        logger.error(f"Marketplace command failed: {e}", exc_info=True)
        console.print(f"[red]❌ Error: {e}[/red]")
        return 1


def get_api_url(logger: Optional[logging.Logger] = None, *, required: bool = True) -> Optional[str]:
    """
    Get API URL with intelligent fallback.

    Priority:
    1. FLUID_API_URL environment variable (override)
    2. Command Center (auto-detected)
    3. Fallback options (local cache, public registry)

    Args:
        logger: Optional logger for diagnostic messages
        required: When True (default), raise ``CLIError`` and print the
            "options to fix" guidance if no registry source is available.
            When False, return ``None`` quietly — used when the caller can
            still serve bundled blueprints without a registry.

    Returns:
        API URL for the blueprint marketplace, or ``None`` when no source is
        available and ``required`` is False.

    Raises:
        CLIError: If no blueprint source is available and ``required`` is True.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Priority 1: Explicit override via environment
    env_url = os.getenv("FLUID_API_URL")
    if env_url:
        logger.debug(f"Using explicit API URL from environment: {env_url}")
        return env_url

    # Priority 2: Command Center (auto-detected)
    import fluid_build.cli.marketplace as _self

    get_command_center_client = _self.get_command_center_client  # honors test patches
    cc = get_command_center_client(logger=logger)
    marketplace_url = cc.get_marketplace_url()

    if marketplace_url:
        console.print("[dim]✓ Using FLUID Command Center marketplace[/dim]")
        return marketplace_url

    # Priority 3: Fallback options
    if cc.url:
        console.print(
            f"[yellow]⚠️  Command Center at {cc.url} is unavailable or marketplace feature not enabled[/yellow]"
        )

    # Try fallback strategies
    fallback_strategy = os.getenv("FLUID_MARKETPLACE_FALLBACK", "local")

    if fallback_strategy in FALLBACK_OPTIONS:
        fallback_url = FALLBACK_OPTIONS[fallback_strategy]()

        if fallback_url:
            if fallback_strategy == "local":
                # Check if local cache exists
                local_path = Path(fallback_url)
                if local_path.exists():
                    console.print(
                        f"[yellow]⚠️  Using local blueprint cache: {fallback_url}[/yellow]"
                    )
                    return f"file://{fallback_url}"
                else:
                    logger.debug(f"Local cache not found: {fallback_url}")
            else:
                console.print(f"[yellow]⚠️  Using fallback registry: {fallback_url}[/yellow]")
                return fallback_url

    # No sources available. When the caller can fall back to bundled
    # blueprints, stay quiet and let it decide what to show.
    if not required:
        logger.debug("No registry source available; caller will fall back to bundled blueprints.")
        return None

    console.print("[red]❌ No blueprint marketplace available[/red]")
    console.print("\n[bold]Options to fix:[/bold]")
    console.print("  1. Start Command Center locally:")
    console.print("     [cyan]docker-compose up -d[/cyan]")
    console.print("\n  2. Set public registry:")
    console.print(
        "     [cyan]export FLUID_PUBLIC_REGISTRY=https://blueprints.fluid.io/api/v1[/cyan]"
    )
    console.print("\n  3. Use local blueprints:")
    console.print(f"     [cyan]mkdir -p {Path.home() / '.fluid' / 'blueprints'}[/cyan]")
    console.print("     [cyan]export FLUID_MARKETPLACE_FALLBACK=local[/cyan]")
    console.print("\n  4. Specify custom API URL:")
    console.print("     [cyan]export FLUID_API_URL=https://your-custom-url[/cyan]")

    raise CLIError(
        1,
        "no_blueprint_marketplace",
        {
            "message": (
                "No blueprint marketplace available — configure Command Center, "
                "FLUID_PUBLIC_REGISTRY, or local blueprints (see the options above)."
            )
        },
    )


def _bundled_blueprints_matching(args) -> list:
    """Bundled blueprints filtered by the same query/category/maturity facets."""
    from fluid_build.cli._market_bundled_blueprints import list_bundled_blueprints

    bundled = list_bundled_blueprints()
    if args.query:
        q = args.query.lower()
        bundled = [
            b
            for b in bundled
            if q in b["id"].lower()
            or q in b.get("name", "").lower()
            or q in b.get("description", "").lower()
            or any(q in t.lower() for t in b.get("tags", []))
        ]
    if args.category:
        bundled = [b for b in bundled if b.get("category") == args.category]
    if args.maturity:
        bundled = [b for b in bundled if b.get("labels", {}).get("maturity") == args.maturity]
    return bundled


def search_blueprints(args, logger: logging.Logger, api_url: str) -> int:
    """Search marketplace blueprints (bundled + registry)."""
    requests = _resolve_requests()
    console.print("[cyan]🔍 Searching marketplace blueprints...[/cyan]\n")

    # Bundled blueprints ship in the package, so discovery works offline and is
    # never empty out of the box — they are listed alongside any registry hits.
    bundled = _bundled_blueprints_matching(args)

    # Build query parameters
    params = {
        "skip": 0,
        "limit": args.limit,
        "sort_by": args.sort,
    }

    if args.query:
        params["query"] = args.query
    if args.category:
        params["category"] = args.category
    if args.tags:
        params["tags"] = args.tags
    if args.maturity:
        params["maturity"] = args.maturity
    if args.state:
        params["state"] = args.state

    registry: list = []
    registry_total = 0
    if api_url:
        try:
            response = _marketplace_get(api_url, logger, params=params)
            response.raise_for_status()
            data = response.json()
            registry = data.get("items", [])
            registry_total = data.get("total", len(registry))
        except requests.exceptions.RequestException as e:
            message = _format_marketplace_error(e)
            # A bundled-only result is still useful; only hard-fail when there
            # is nothing at all to show.
            if not bundled:
                console.print(f"[red]❌ {message}[/red]")
                logger.error("Failed to search blueprints: %s", message, exc_info=True)
                return 1
            console.print(
                f"[dim]Registry unavailable ({message}); showing bundled blueprints only.[/dim]\n"
            )
            logger.info(
                "Marketplace registry unavailable; showing %d bundled blueprint(s).", len(bundled)
            )
    elif bundled:
        console.print(
            "[dim]No registry configured; showing bundled blueprints. "
            "Set FLUID_API_URL or FLUID_PUBLIC_REGISTRY for more.[/dim]\n"
        )

    blueprints = bundled + registry
    if not blueprints:
        console.print("[yellow]No blueprints found matching your criteria.[/yellow]")
        return 0

    # Create results table
    title = f"Blueprint Marketplace ({len(bundled)} bundled + {registry_total} registry)"
    table = Table(title=title)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Category", style="green")
    table.add_column("Maturity", style="yellow")
    table.add_column("Source", style="blue")
    table.add_column("Version", justify="center")

    for bp in blueprints:
        # Extract maturity from labels if available
        maturity = bp.get("labels", {}).get("maturity", "N/A")
        source = "bundled" if bp.get("source") == "bundled" else "registry"

        table.add_row(
            bp["id"],
            bp["name"][:40] + "..." if len(bp["name"]) > 40 else bp["name"],
            bp.get("category", "N/A"),
            maturity,
            source,
            bp.get("version", "N/A"),
        )

    console.print(table)
    console.print("\n[dim]💡 Use 'fluid market --blueprint-id <id>' to see details[/dim]")

    return 0


def show_blueprint_info(args, logger: logging.Logger, api_url: str) -> int:
    """Show detailed blueprint information."""
    requests = _resolve_requests()
    console.print(f"[cyan]ℹ️  Fetching blueprint: {args.blueprint_id}...[/cyan]\n")

    from fluid_build.cli._market_bundled_blueprints import get_bundled_blueprint, is_bundled

    if is_bundled(args.blueprint_id):
        # Bundled blueprint — no registry round-trip; it has the same dict shape.
        bp = get_bundled_blueprint(args.blueprint_id)
    else:
        params = {}
        if getattr(args, "version", None):
            params["version"] = args.version
        try:
            response = _marketplace_get(f"{api_url}/{args.blueprint_id}", logger, params=params)
            response.raise_for_status()
            bp = response.json()
        except requests.exceptions.RequestException as e:
            message = _format_marketplace_error(e)
            console.print(f"[red]❌ {message}[/red]")
            logger.error("Failed to get blueprint info: %s", message, exc_info=True)
            return 1

    # Display blueprint header
    console.print(
        Panel(
            f"[bold cyan]{bp['name']}[/bold cyan]\n"
            f"[dim]{bp.get('description', 'No description available')}[/dim]",
            title=f"📦 {bp['id']} v{bp['version']}",
        )
    )

    # Metadata table
    metadata_table = Table(show_header=False, box=None, padding=(0, 2))
    metadata_table.add_column("Field", style="cyan")
    metadata_table.add_column("Value")

    # Extract metadata from correct fields
    maturity = bp.get("labels", {}).get("maturity", "N/A")
    author_name = bp.get("author", {}).get("name", bp.get("author_name", "N/A"))
    org = bp.get("author", {}).get("organization", bp.get("organization", "N/A"))
    license_info = bp.get("labels", {}).get("license", bp.get("license", "N/A"))

    metadata_table.add_row("Category", bp.get("category", "N/A"))
    metadata_table.add_row("Maturity", maturity)
    metadata_table.add_row("Source", "bundled" if bp.get("source") == "bundled" else "registry")
    metadata_table.add_row("State", bp.get("state", "N/A"))
    metadata_table.add_row("Author", author_name)
    metadata_table.add_row("Organization", org)
    metadata_table.add_row("License", license_info)
    metadata_table.add_row("Downloads", str(bp.get("download_count", 0)))
    metadata_table.add_row("Usage Count", str(bp.get("usage_count", 0)))

    # Success rate comes as decimal 0.0-1.0
    success_rate = bp.get("success_rate")
    if success_rate is not None:
        # If it's > 1, it's already a percentage
        if success_rate > 1:
            metadata_table.add_row("Success Rate", f"{success_rate:.1f}%")
        else:
            metadata_table.add_row("Success Rate", f"{success_rate:.1%}")

    console.print(metadata_table)
    console.print()

    # Parameters table
    params_table = Table(title="Parameters")
    params_table.add_column("Name", style="cyan")
    params_table.add_column("Type", style="green")
    params_table.add_column("Required", justify="center")
    params_table.add_column("Description")

    # Parameters are in spec.parameters for BlueprintDetail
    param_list = bp.get("spec", {}).get("parameters", bp.get("parameters", []))
    for param in param_list:
        params_table.add_row(
            param["name"],
            param["type"],
            "✓" if param.get("required") else "",
            (
                param.get("description", "")[:50] + "..."
                if len(param.get("description", "")) > 50
                else param.get("description", "")
            ),
        )

    console.print(params_table)
    console.print()

    # Tags and labels
    if bp.get("tags"):
        console.print(f"[bold]Tags:[/bold] {', '.join(bp['tags'])}")

    # Show template if requested
    if getattr(args, "show_template", False):
        console.print("\n[bold]Contract Template:[/bold]")
        syntax = Syntax(bp["contract_template"], "jinja2", theme="monokai", line_numbers=True)
        console.print(syntax)

    console.print(
        f"\n[dim]💡 Use 'fluid market --blueprint-id {args.blueprint_id}' to generate a contract[/dim]"
    )

    return 0


def instantiate_blueprint(args, logger: logging.Logger, api_url: str) -> int:
    """Instantiate blueprint to generate FLUID contract."""
    from fluid_build.cli._market_bundled_blueprints import (
        get_bundled_blueprint,
        is_bundled,
        render_bundled_contract,
    )

    requests = _resolve_requests()
    console.print(f"[cyan]⚙️  Instantiating blueprint: {args.blueprint_id}...[/cyan]\n")

    bundled_bp = get_bundled_blueprint(args.blueprint_id) if is_bundled(args.blueprint_id) else None

    # Get blueprint info first (bundled blueprints carry the same dict shape and
    # need no registry round-trip).
    if bundled_bp is not None:
        bp = bundled_bp
    else:
        try:
            response = _marketplace_get(f"{api_url}/{args.blueprint_id}", logger)
            response.raise_for_status()
            bp = response.json()
        except requests.exceptions.RequestException as e:
            message = _format_marketplace_error(e)
            console.print(f"[red]❌ Failed to fetch blueprint: {message}[/red]")
            logger.error("Failed to fetch blueprint: %s", message, exc_info=True)
            return 1

    # Get parameters
    parameters = {}

    if args.params:
        # Load from JSON string or file
        if Path(args.params).is_file():
            with open(args.params, encoding="utf-8") as f:
                parameters = json.load(f)
        else:
            parameters = json.loads(args.params)
    elif args.interactive:
        # Interactive wizard
        console.print(
            Panel(f"[bold]Parameter Wizard[/bold]\nFill in parameters for {bp['name']}", title="🧙")
        )
        param_list = bp.get("spec", {}).get("parameters", bp.get("parameters", []))
        parameters = interactive_parameter_wizard(param_list)
    else:
        console.print("[yellow]⚠️  No parameters provided. Use --params or --interactive.[/yellow]")
        return 1

    # Render the contract. Bundled blueprints render client-side with a
    # sandboxed Jinja2 environment; registry blueprints are rendered
    # server-side via POST /{id}/instantiate.
    if bundled_bp is not None:
        try:
            contract = render_bundled_contract(bundled_bp, parameters)
        except ValueError as e:
            console.print(f"[red]❌ {e}[/red]")
            logger.error("Bundled blueprint render failed: %s", e)
            return 1
        cost_estimate = "N/A (rendered locally)"
    else:
        # Instantiate blueprint (matches BlueprintInstantiateRequest model)
        payload = {
            "instance_name": bp.get("id", "instance") + "-" + str(int(time.time())),
            "parameters": parameters,
            "user_context": {},
            "deployment_context": {},
        }
        try:
            response = _marketplace_post(
                f"{api_url}/{args.blueprint_id}/instantiate", logger, json=payload
            )
            response.raise_for_status()
            result = response.json()
            contract = result["contract"]
            cost_estimate = result.get("cost_estimate", "N/A")
        except requests.exceptions.RequestException as e:
            message = _format_marketplace_error(e)
            console.print(f"[red]❌ Failed to instantiate blueprint: {message}[/red]")
            logger.error("Blueprint instantiation failed: %s", message, exc_info=True)
            return 1

    console.print("[green]✅ Contract generated successfully![/green]\n")

    # Show generated contract
    console.print(Panel("Generated FLUID Contract", style="green"))
    syntax = Syntax(json.dumps(contract, indent=2), "json", theme="monokai", line_numbers=True)
    console.print(syntax)

    # Save to file if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2)
        console.print(f"\n[green]💾 Saved to: {output_path}[/green]")

    # Submit if requested
    if getattr(args, "submit", False):
        if Confirm.ask("\n[bold]Submit contract to FLUID?[/bold]"):
            console.print("[yellow]🚀 Submitting contract... (not implemented yet)[/yellow]")
            # TODO: Implement contract submission

    console.print(f"\n[dim]Cost estimate: {cost_estimate}[/dim]")

    return 0


def interactive_parameter_wizard(parameters: list) -> Dict[str, Any]:
    """Interactive wizard for filling blueprint parameters."""
    result = {}

    for param in parameters:
        name = param["name"]
        param_type = param["type"]
        required = param.get("required", False)
        description = param.get("description", "")
        default = param.get("default")
        example = param.get("example")
        enum = param.get("enum", [])

        # Build prompt
        prompt_text = f"[cyan]{name}[/cyan]"
        if description:
            prompt_text += f" [dim]({description})[/dim]"

        # Show constraints
        constraints = []
        if required:
            constraints.append("required")
        if enum:
            constraints.append(f"choices: {', '.join(map(str, enum))}")
        if default:
            constraints.append(f"default: {default}")
        elif example:
            constraints.append(f"example: {example}")

        if constraints:
            prompt_text += f"\n  [yellow]{' | '.join(constraints)}[/yellow]"

        console.print(f"\n{prompt_text}")

        # Get value based on type
        if enum:
            # Enum - use choices
            from rich.prompt import Prompt

            value = Prompt.ask(
                "  Value", choices=[str(v) for v in enum], default=str(default) if default else None
            )
        elif param_type == "boolean":
            value = Confirm.ask("  Value", default=default if default is not None else False)
        elif param_type in ["array", "object"]:
            # JSON input
            value_str = Prompt.ask("  Value (JSON)", default=json.dumps(default) if default else "")
            if value_str:
                value = json.loads(value_str)
            else:
                value = default
        else:
            # String, integer, number
            default_str = str(default) if default is not None else (str(example) if example else "")
            value_str = Prompt.ask("  Value", default=default_str)

            if param_type == "integer":
                value = int(value_str) if value_str else default
            elif param_type == "number":
                value = float(value_str) if value_str else default
            else:
                value = value_str if value_str else default

        if value is not None:
            result[name] = value
        elif required:
            console.print(f"[red]❌ Required parameter '{name}' cannot be empty[/red]")
            raise CLIError(2, "missing_blueprint_parameter", {"parameter": name})

    return result


def list_categories(args, logger: logging.Logger, api_url: str) -> int:
    """List available blueprint categories."""
    categories = {
        "analytics": "ETL, data warehousing, reporting",
        "streaming": "Real-time processing, event streams",
        "ml": "Training, inference, feature engineering",
        "governance": "Data quality, compliance, audit",
        "integration": "API connectors, data sync",
        "monitoring": "Observability, alerting, dashboards",
    }

    console.print("[bold cyan]Blueprint Categories:[/bold cyan]\n")

    for cat, desc in categories.items():
        console.print(f"  [green]•[/green] [cyan]{cat}[/cyan] - {desc}")

    console.print(
        "\n[dim]💡 Use 'fluid market --blueprints --search --category <name>' to filter[/dim]"
    )

    return 0
