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
Data Mesh Manager CLI command.

Publish FLUID contracts to Entropy Data / Data Mesh Manager.

Usage:
  fluid datamesh-manager publish contract.fluid.yaml
  fluid datamesh-manager publish contract.fluid.yaml --dry-run
  fluid datamesh-manager publish contract.fluid.yaml --with-contract
  fluid datamesh-manager list
  fluid datamesh-manager get <product-id>
  fluid datamesh-manager delete <product-id>
  fluid dmm publish contract.fluid.yaml          # short alias
"""

from __future__ import annotations

import json
import logging

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from fluid_build.cli.bootstrap import load_contract_with_overlay
from fluid_build.cli.validate import run_on_contract_dict
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider


def add_parser(subparsers):
    """Add datamesh-manager subcommand."""
    parser = subparsers.add_parser(
        "datamesh-manager",
        aliases=["dmm"],
        help="Publish to Entropy Data / Data Mesh Manager",
    )

    dmm_sub = parser.add_subparsers(dest="dmm_command")

    # --- publish -----------------------------------------------------------
    pub = dmm_sub.add_parser("publish", help="Publish data product to Entropy Data")
    pub.add_argument("contract", help="Path to FLUID contract file")
    pub.add_argument("-o", "--overlay", help="Path to overlay file")
    pub.add_argument("--team-id", help="Team ID (default: from contract owner)")
    pub.add_argument("--dry-run", action="store_true", help="Preview without publishing")
    pub.add_argument(
        "--with-contract",
        action="store_true",
        help="Also publish a companion data contract",
    )
    pub.add_argument(
        "--no-create-team",
        action="store_true",
        help="Don't auto-create team if missing",
    )
    pub.add_argument(
        "--contract-format",
        choices=["odcs", "dcs"],
        default="odcs",
        help=(
            "Data contract format: 'odcs' (Open Data Contract Standard v3.1.0, "
            "default) or 'dcs' (Data Contract Specification 0.9.3, deprecated)"
        ),
    )
    pub.add_argument(
        "--data-product-spec",
        help=(
            "Override dataProductSpecification value sent to Entropy Data "
            "(e.g. 'odps' or '0.0.1')."
        ),
    )
    pub.add_argument(
        "--validate-generated-contracts",
        action="store_true",
        help="Validate generated ODCS contracts locally before PUT.",
    )
    pub.add_argument(
        "--validation-mode",
        choices=["warn", "strict"],
        default="warn",
        help=(
            "Validation behavior for generated contracts: "
            "'warn' logs validation issues and continues; 'strict' fails invalid contracts."
        ),
    )
    pub.add_argument(
        "--fail-on-contract-error",
        action="store_true",
        help="Return non-zero exit code if any ODCS contract publish fails.",
    )
    pub.add_argument(
        "--api-key",
        help="Entropy Data API key (or set DMM_API_KEY env var)",
    )
    pub.add_argument(
        "--api-url",
        help="API base URL (default: https://api.entropy-data.com)",
    )
    pub.set_defaults(func=_cmd_publish)

    # --- list --------------------------------------------------------------
    ls = dmm_sub.add_parser("list", help="List all data products")
    ls.add_argument("--api-key", help="Entropy Data API key")
    ls.add_argument("--api-url", help="API base URL")
    ls.add_argument(
        "--format",
        "-f",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    ls.set_defaults(func=_cmd_list)

    # --- get ---------------------------------------------------------------
    gt = dmm_sub.add_parser("get", help="Get a data product by ID")
    gt.add_argument("product_id", help="Data product ID")
    gt.add_argument("--api-key", help="Entropy Data API key")
    gt.add_argument("--api-url", help="API base URL")
    gt.set_defaults(func=_cmd_get)

    # --- delete ------------------------------------------------------------
    dl = dmm_sub.add_parser("delete", help="Delete a data product")
    dl.add_argument("product_id", help="Data product ID")
    dl.add_argument("--api-key", help="Entropy Data API key")
    dl.add_argument("--api-url", help="API base URL")
    dl.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    dl.set_defaults(func=_cmd_delete)

    # --- teams -------------------------------------------------------------
    tm = dmm_sub.add_parser("teams", help="List all teams")
    tm.add_argument("--api-key", help="Entropy Data API key")
    tm.add_argument("--api-url", help="API base URL")
    tm.add_argument(
        "--format",
        "-f",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    tm.set_defaults(func=_cmd_teams)

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _make_provider(args) -> DataMeshManagerProvider:
    """Instantiate provider from CLI args / env vars."""
    kwargs = {}
    if getattr(args, "api_key", None):
        kwargs["api_key"] = args.api_key
    if getattr(args, "api_url", None):
        kwargs["api_url"] = args.api_url
    return DataMeshManagerProvider(**kwargs)


def _validate_fluid_contract(contract: dict, validation_mode: str, logger: logging.Logger) -> int:
    """Validate an already-loaded FLUID contract on the publish path.

    **Validation target is the contract's own declared ``fluidVersion``**,
    not a hardcoded master version. A contract with ``fluidVersion: 0.5.7``
    is validated against ``fluid-schema-0.5.7.json``, a 0.7.1 contract
    against 0.7.1, a 0.7.2 contract against 0.7.2, and so on — whichever
    bundled schema matches. This is the backward-compatibility guarantee:
    upgrading the CLI never invalidates a contract that was valid against
    its own declared version. The CLI acts as coordinator for the whole
    FLUID version range, not just the latest.

    Delegates to :func:`fluid_build.cli.validate.run_on_contract_dict`, the
    public one-call wrapper around the native ``fluid validate`` flow, and
    translates its exit code into publish-specific semantics:

      * ``strict`` — a non-zero exit code aborts publish
      * ``warn``   — a non-zero exit code is logged and publish proceeds
        (errors have already been printed by the native output path),
        preserving backward compatibility for contracts that carry
        extension fields the bundled schema doesn't yet recognize
    """
    try:
        _result, rc = run_on_contract_dict(contract, strict=False, logger=logger)
    except Exception as exc:  # noqa: BLE001
        log_method = logger.error if validation_mode == "strict" else logger.warning
        log_method(
            "fluid_contract_validation_failed_to_run type=%s msg=%s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        if validation_mode == "strict":
            console_error(f"Error: FLUID schema validation could not run: {exc}")
            return 1
        return 0

    if rc == 0:
        return 0

    if validation_mode == "strict":
        console_error(
            "Publish aborted: contract does not conform to the bundled FLUID schema. "
            "Re-run with --validation-mode warn to publish anyway."
        )
        return rc

    cprint("⚠️  Publishing despite FLUID schema errors (--validation-mode is 'warn').")
    return 0


def _cmd_publish(args, logger=None):
    """Execute publish command."""
    log = logger or logging.getLogger(__name__)
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "overlay", None), log)

        # Enforce the CLI's role as master coordinator: the loaded FLUID
        # contract must conform to ``fluid-schema-0.7.2.json`` (or whatever
        # ``fluidVersion`` it declares) BEFORE any provider payload is
        # constructed. Delegates to the native validation + output
        # formatters so we never re-implement what ``fluid validate`` does.
        # Gated by ``--validation-mode`` (strict aborts on errors; warn logs
        # and continues, preserving backward compatibility).
        validation_mode = getattr(args, "validation_mode", "warn")
        rc = _validate_fluid_contract(contract, validation_mode, log)
        if rc != 0:
            return rc

        provider = _make_provider(args)

        data_product_spec = getattr(args, "data_product_spec", None)
        provider_hint = getattr(args, "provider", None)

        result = provider.apply(
            contract,
            dry_run=args.dry_run,
            team_id=getattr(args, "team_id", None),
            create_team=not getattr(args, "no_create_team", False),
            publish_contract=getattr(args, "with_contract", False),
            contract_format=getattr(args, "contract_format", "odcs"),
            data_product_specification=data_product_spec,
            provider_hint=provider_hint,
            validate_generated_contracts=getattr(args, "validate_generated_contracts", False),
            validation_mode=getattr(args, "validation_mode", "warn"),
        )

        if args.dry_run:
            _print_dry_run(result)
            return 0

        _print_publish_result(result)
        return _publish_exit_code(result, args)

    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1
    except Exception as exc:
        console_error(f"Error: {exc}")
        return 1


def _publish_exit_code(result, args) -> int:
    """Calculate publish exit code based on ODCS per-contract outcomes."""
    odcs_contracts = result.get("odcs_contracts", [])
    if not isinstance(odcs_contracts, list):
        return 0

    validation_mode = getattr(args, "validation_mode", "warn")
    fail_on_contract_error = getattr(args, "fail_on_contract_error", False)

    if validation_mode == "strict":
        if any(contract.get("valid") is False for contract in odcs_contracts):
            return 1

    if fail_on_contract_error:
        if any(contract.get("success") is False for contract in odcs_contracts):
            return 1

    return 0


def _failure_reason(odcs_result):
    if odcs_result.get("valid") is False:
        return "VALIDATION_FAILED"
    if odcs_result.get("success") is False:
        return "HTTP_FAILED"
    return ""


def _print_publish_result(result):
    """Print a successful publish result."""
    product_id = result.get("product_id", "?")
    url = result.get("url", "")
    if RICH_AVAILABLE:
        console = Console()
        lines = [f"[green]✅ Published:[/green] [bold]{product_id}[/bold]"]
        if url:
            lines.append(f"[dim]View at:[/dim] {url}")
        # Legacy single contract (kept for backward compatibility)
        dc = result.get("data_contract")
        if dc:
            lines.append(f"[green]📄 Contract:[/green] {dc.get('contract_id', '?')}")
            if dc.get("url"):
                lines.append(f"[dim]View at:[/dim] {dc['url']}")
        # Per-expose ODCS contracts
        for odcs in result.get("odcs_contracts", []):
            status_icon = "✅" if odcs.get("success") else "❌"
            lines.append(f"[green]{status_icon} ODCS:[/green] {odcs.get('contract_id', '?')}")
            if odcs.get("url"):
                lines.append(f"[dim]View at:[/dim] {odcs['url']}")
            reason = _failure_reason(odcs)
            if reason:
                lines.append(f"[yellow]Reason:[/yellow] {reason}")
            if odcs.get("validation_error"):
                lines.append(f"[red]Validation:[/red] {odcs['validation_error']}")
            if not odcs.get("success") and odcs.get("error"):
                lines.append(f"[red]Error:[/red] {odcs['error']}")
        console.print(Panel("\n".join(lines), title="Data Mesh Manager", border_style="green"))
    else:
        success(f"Published data product: {product_id}")
        if url:
            cprint(f"   View at: {url}")
        dc = result.get("data_contract")
        if dc:
            cprint(f"📄 Published data contract: {dc.get('contract_id', '?')}")
            if dc.get("url"):
                cprint(f"   View at: {dc['url']}")
        for odcs in result.get("odcs_contracts", []):
            icon = "✅" if odcs.get("success") else "❌"
            cprint(f"{icon} ODCS contract: {odcs.get('contract_id', '?')}")
            if odcs.get("url"):
                cprint(f"   View at: {odcs['url']}")
            reason = _failure_reason(odcs)
            if reason:
                cprint(f"   Reason: {reason}")
            if odcs.get("validation_error"):
                cprint(f"   Validation: {odcs['validation_error']}")
            if not odcs.get("success") and odcs.get("error"):
                cprint(f"   Error: {odcs['error']}")


def _cmd_list(args, logger=None):
    """List all data products."""
    try:
        provider = _make_provider(args)
        products = provider.list_products()
        fmt = getattr(args, "format", "table")

        if fmt == "json":
            cprint(json.dumps(products, indent=2))
            return 0

        if RICH_AVAILABLE:
            console = Console()
            table = Table(title="Entropy Data — Data Products")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="bold")
            table.add_column("Status")
            table.add_column("Team")
            for p in products:
                info = p.get("info", {})
                table.add_row(
                    info.get("id", "?"),
                    info.get("name", "?"),
                    info.get("status", "?"),
                    p.get("teamId", "?"),
                )
            console.print(table)
        else:
            for p in products:
                info = p.get("info", {})
                cprint(f"  {info.get('id', '?'):30s}  {info.get('name', '?')}")
        return 0

    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_get(args, logger=None):
    """Get a single data product."""
    try:
        provider = _make_provider(args)
        product = provider.verify(args.product_id)
        cprint(json.dumps(product, indent=2))
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_delete(args, logger=None):
    """Delete a data product."""
    try:
        if not getattr(args, "yes", False):
            confirm = input(f"Delete data product '{args.product_id}'? [y/N] ")
            if confirm.lower() not in ("y", "yes"):
                cprint("Cancelled.")
                return 0

        provider = _make_provider(args)
        ok = provider.delete(args.product_id)
        if ok:
            cprint(f"Deleted: {args.product_id}")
        else:
            console_error(f"Failed to delete: {args.product_id}")
            return 1
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_teams(args, logger=None):
    """List teams."""
    try:
        provider = _make_provider(args)
        teams = provider.list_teams()
        fmt = getattr(args, "format", "table")

        if fmt == "json":
            cprint(json.dumps(teams, indent=2))
            return 0

        if RICH_AVAILABLE:
            console = Console()
            table = Table(title="Entropy Data — Teams")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="bold")
            for t in teams:
                table.add_row(t.get("id", "?"), t.get("name", "?"))
            console.print(table)
        else:
            for t in teams:
                cprint(f"  {t.get('id', '?'):30s}  {t.get('name', '?')}")
        return 0

    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _print_dry_run(result):
    """Print a dry-run preview."""
    if RICH_AVAILABLE:
        console = Console()
        payload = result.get("payload", {})
        console.print(
            Panel(
                f"[bold]Method:[/bold] {result.get('method', 'PUT')}\n"
                f"[bold]URL:[/bold]    {result.get('url', '?')}\n\n"
                f"[bold]Payload:[/bold]\n{json.dumps(payload, indent=2)}",
                title="[yellow]Dry Run Preview[/yellow]",
                border_style="yellow",
            )
        )
        # Show per-expose ODCS contract previews
        for odcs in result.get("odcs_contracts", []):
            console.print(
                Panel(
                    f"[bold]Method:[/bold] {odcs.get('method', 'PUT')}\n"
                    f"[bold]URL:[/bold]    {odcs.get('url', '?')}\n\n"
                    f"[bold]ODCS Payload:[/bold]\n{json.dumps(odcs.get('payload', {}), indent=2)}",
                    title="[yellow]ODCS Contract Dry Run[/yellow]",
                    border_style="yellow",
                )
            )
    else:
        cprint("=== Dry Run Preview ===")
        cprint(f"Method: {result.get('method', 'PUT')}")
        cprint(f"URL:    {result.get('url', '?')}")
        cprint()
        cprint(json.dumps(result.get("payload", {}), indent=2))
        for odcs in result.get("odcs_contracts", []):
            cprint()
            cprint("=== ODCS Contract Dry Run ===")
            cprint(f"Method: {odcs.get('method', 'PUT')}")
            cprint(f"URL:    {odcs.get('url', '?')}")
            cprint()
            cprint(json.dumps(odcs.get("payload", {}), indent=2))
