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
import sys

from fluid_build.cli.console import cprint, cprint_json, success
from fluid_build.cli.console import error as console_error

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from typing import TYPE_CHECKING

from fluid_build.cli.bootstrap import load_contract_with_overlay
from fluid_build.providers.base import ProviderError

if TYPE_CHECKING:  # resolve annotation names for ruff/type-checkers only
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

# NOTE: ``DataMeshManagerProvider`` (pulls ``requests`` — ~107 modules with its
# transitive deps) and ``validate.run_on_contract_dict`` (pulls
# ``schema_manager`` / ``jsonschema``) are imported lazily inside the handlers
# below. ``add_parser`` runs on every ``fluid --help``, so this module must stay
# light. See the A++ Light CLI startup card. Annotations referencing
# ``DataMeshManagerProvider`` are safe at module scope because
# ``from __future__ import annotations`` keeps them as lazy strings.


def __getattr__(name: str):
    """Lazily resolve ``DataMeshManagerProvider`` (PEP 562).

    Exposes it as a module attribute so the lazy import is transparent and the
    ``patch("…cli.datamesh_manager.DataMeshManagerProvider")`` test seam keeps
    working, without pulling ``requests`` onto the ``fluid --help`` path.
    """
    if name == "DataMeshManagerProvider":
        from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

        return DataMeshManagerProvider
    if name == "run_on_contract_dict":
        from fluid_build.cli.validate import run_on_contract_dict

        return run_on_contract_dict
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
            "Override dataProductSpecification value sent to Entropy Data (e.g. 'odps' or '0.0.1')."
        ),
    )
    pub.add_argument(
        "--odps-lineage-mode",
        choices=["contract", "source-system"],
        help=(
            "ODPS input lineage strategy. 'contract' uses inputPorts[].contractId "
            "for product-to-product lineage; 'source-system' enables legacy "
            "SourceSystem compatibility."
        ),
    )
    pub.add_argument(
        "--auto-approve-access",
        action="store_true",
        default=None,  # sentinel — let env var DMM_AUTO_APPROVE_ACCESS take effect
        help=(
            "Auto-approve product-to-product Access agreements generated from "
            "consumes[]. WITHOUT this flag, DMM creates the agreements in "
            "'pending' status and the lineage graph in the UI stays empty "
            "until you approve them manually. Equivalent to setting "
            "DMM_AUTO_APPROVE_ACCESS=true. Recommended for local sandboxes / "
            "lab demos; production should leave pending for human review."
        ),
    )
    pub.add_argument(
        "--no-auto-approve-access",
        dest="auto_approve_access",
        action="store_false",
        help=(
            "Force pending Access agreements (no auto-approve) even when "
            "DMM_AUTO_APPROVE_ACCESS=true in env. Use to override a sandbox-wide default."
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

    # --- wipe --------------------------------------------------------------
    wp = dmm_sub.add_parser(
        "wipe",
        help="Delete EVERY DataProduct in the tenant (multi-pass FK-aware)",
    )
    wp.add_argument("--api-key", help="Entropy Data API key")
    wp.add_argument("--api-url", help="API base URL")
    wp.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    wp.add_argument(
        "--max-passes",
        type=int,
        default=8,
        help="Max delete passes before giving up on residual FK locks (default: 8)",
    )
    wp.set_defaults(func=_cmd_wipe)

    # --- list-contracts ----------------------------------------------------
    lc = dmm_sub.add_parser("list-contracts", help="List all data contracts")
    lc.add_argument("--api-key", help="Entropy Data API key")
    lc.add_argument("--api-url", help="API base URL")
    lc.add_argument(
        "--format",
        "-f",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    lc.set_defaults(func=_cmd_list_contracts)

    # --- get-contract ------------------------------------------------------
    gc = dmm_sub.add_parser("get-contract", help="Get a data contract by ID")
    gc.add_argument("contract_id", help="Data contract ID")
    gc.add_argument("--api-key", help="Entropy Data API key")
    gc.add_argument("--api-url", help="API base URL")
    gc.set_defaults(func=_cmd_get_contract)

    # --- delete-contract ---------------------------------------------------
    dc = dmm_sub.add_parser("delete-contract", help="Delete a data contract by ID")
    dc.add_argument("contract_id", help="Data contract ID")
    dc.add_argument("--api-key", help="Entropy Data API key")
    dc.add_argument("--api-url", help="API base URL")
    dc.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    dc.set_defaults(func=_cmd_delete_contract)

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _make_provider(args) -> DataMeshManagerProvider:
    """Instantiate provider from CLI args / env vars."""
    # Resolve via this module's own attribute (lazy, PEP 562 ``__getattr__``)
    # so the ``patch("…cli.datamesh_manager.DataMeshManagerProvider")`` test
    # seam is honored and ``requests`` stays off the cold ``--help`` path.
    import fluid_build.cli.datamesh_manager as _self

    provider_cls = _self.DataMeshManagerProvider

    kwargs = {}
    if getattr(args, "api_key", None):
        kwargs["api_key"] = args.api_key
    if getattr(args, "api_url", None):
        kwargs["api_url"] = args.api_url
    if getattr(args, "odps_lineage_mode", None):
        kwargs["odps_lineage_mode"] = args.odps_lineage_mode
    if getattr(args, "auto_approve_access", False):
        kwargs["auto_approve_access"] = True
    return provider_cls(**kwargs)


def _validate_fluid_contract(contract: dict, validation_mode: str, logger: logging.Logger) -> int:
    """Validate an already-loaded FLUID contract on the publish path.

    **Validation target is the contract's own declared ``fluidVersion``**,
    within the 0.7.x line. A 0.7.1 contract validates against
    ``fluid-schema-0.7.1.json``, a 0.7.2 contract against 0.7.2, a 0.7.3
    contract against 0.7.3 — whichever bundled schema matches. Pre-0.7
    contracts (0.4.0, 0.5.x) are no longer supported; operators on
    those should run a one-time migration to 0.7.3.

    Delegates to :func:`fluid_build.cli.validate.run_on_contract_dict`, the
    public one-call wrapper around the native ``fluid validate`` flow, and
    translates its exit code into publish-specific semantics:

      * ``strict`` — a non-zero exit code aborts publish
      * ``warn``   — a non-zero exit code is logged and publish proceeds
        (errors have already been printed by the native output path),
        preserving backward compatibility for contracts that carry
        extension fields the bundled schema doesn't yet recognize
    """
    # Resolve via this module's own attribute (lazy PEP 562 ``__getattr__``) so
    # the ``patch.object(dmm_mod, "run_on_contract_dict")`` test seam is honored
    # and ``schema_manager`` stays off the cold ``--help`` path.
    import fluid_build.cli.datamesh_manager as _self

    try:
        _result, rc = _self.run_on_contract_dict(contract, strict=False, logger=logger)
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
            odps_lineage_mode=getattr(args, "odps_lineage_mode", None),
            auto_approve_access=getattr(args, "auto_approve_access", False),
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
        # Per-expose ODCS contracts (one per expose; the legacy
        # single-contract ``data_contract`` field has been deleted).
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
        # Access agreements (product-to-product lineage). Surface count +
        # approval state, and if any are pending point the operator at the
        # flag — DMM only renders lineage from APPROVED agreements, so a
        # silent "pending" pile is the most common cause of an empty
        # lineage graph in the UI.
        access = result.get("access_agreements") or []
        if access:
            ok = sum(1 for a in access if a.get("success"))
            approved = sum(1 for a in access if a.get("auto_approved"))
            pending = ok - approved
            lines.append("")
            if approved == ok:
                lines.append(
                    f"[green]🔗 Lineage:[/green] {approved}/{ok} Access agreements "
                    f"auto-approved (lineage will render in DMM UI)"
                )
            elif pending > 0:
                lines.append(
                    f"[yellow]🔗 Lineage:[/yellow] {ok} Access agreement(s) created "
                    f"({approved} approved, [bold]{pending} pending[/bold])"
                )
                lines.append(
                    "[yellow]   ↳ DMM only renders lineage from APPROVED agreements.[/yellow]"
                )
                lines.append(
                    "[yellow]     Re-publish with [bold]--auto-approve-access[/bold] "
                    "(or [bold]DMM_AUTO_APPROVE_ACCESS=true[/bold]) to render lineage now.[/yellow]"
                )
            for ag in access:
                if not ag.get("success"):
                    aid = ag.get("access_id", "?")
                    lines.append(f"[red]❌ Access:[/red] {aid}")
                    if ag.get("error"):
                        lines.append(f"[red]Error:[/red] {ag['error']}")
        console.print(Panel("\n".join(lines), title="Data Mesh Manager", border_style="green"))
    else:
        success(f"Published data product: {product_id}")
        if url:
            cprint(f"   View at: {url}")
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
            # Bypass Rich console entirely — its line-wrapping injects literal
            # newlines into JSON string values which breaks ``json.loads`` for
            # callers piping the output. Write directly to stdout.
            sys.stdout.write(json.dumps(products, indent=2))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0

        if RICH_AVAILABLE:
            console = Console()
            table = Table(title="Entropy Data — Data Products")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="bold")
            table.add_column("Status")
            table.add_column("Team")
            for p in products:
                # DMM / Entropy Data API v1 returns fields at the top
                # level of each product (``id``, ``name``, ``status``,
                # ``team.name``). The legacy v0 shape nested them under
                # ``info`` and used ``teamId``. Read both so the CLI
                # works against either deployment.
                info = p.get("info") or {}
                team_obj = p.get("team") if isinstance(p.get("team"), dict) else {}
                table.add_row(
                    p.get("id") or info.get("id") or "?",
                    p.get("name") or info.get("name") or "?",
                    p.get("status") or info.get("status") or "?",
                    team_obj.get("name") or p.get("teamId") or "?",
                )
            console.print(table)
        else:
            for p in products:
                info = p.get("info") or {}
                pid = p.get("id") or info.get("id") or "?"
                pname = p.get("name") or info.get("name") or "?"
                cprint(f"  {pid:30s}  {pname}")
        return 0

    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_get(args, logger=None):
    """Get a single data product."""
    try:
        provider = _make_provider(args)
        product = provider.verify(args.product_id)
        cprint_json(json.dumps(product, indent=2))
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _find_consumers(provider: DataMeshManagerProvider, product_id: str) -> list[str]:
    """Return product IDs whose ``consumes[].productId`` references ``product_id``.

    Used to enrich the ``422 Cannot delete because data product is in use``
    error with the list of products holding the FK lock.
    """
    consumers: list[str] = []
    try:
        for prod in provider.list_products():
            for ip in prod.get("inputPorts") or []:
                cid = ip.get("contractId") or ""
                if cid == product_id or cid.startswith(f"{product_id}."):
                    pid = prod.get("id")
                    if pid and pid not in consumers:
                        consumers.append(pid)
                        break
    except Exception:  # noqa: BLE001 — best-effort enrichment, never raises
        pass
    return consumers


def _cmd_delete(args, logger=None):
    """Delete a data product."""
    try:
        if not getattr(args, "yes", False):
            confirm = input(f"Delete data product '{args.product_id}'? [y/N] ")
            if confirm.lower() not in ("y", "yes"):
                cprint("Cancelled.")
                return 0

        provider = _make_provider(args)
        try:
            ok = provider.delete(args.product_id)
        except ProviderError as exc:
            # On DMM's 422 "in use" FK lock, enrich with the consumer list so
            # the operator knows what to delete first (or use --cascade).
            msg = str(exc)
            if "in use" in msg or "422" in msg:
                consumers = _find_consumers(provider, args.product_id)
                if consumers:
                    console_error(
                        f"Cannot delete '{args.product_id}' — referenced by:\n  "
                        + "\n  ".join(f"- {c}" for c in consumers)
                        + "\nDelete the consumers first or use `fluid dmm wipe --cascade`."
                    )
                    return 1
            raise

        if ok:
            cprint(f"Deleted: {args.product_id}")
        else:
            console_error(f"Failed to delete: {args.product_id}")
            return 1
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_wipe(args, logger=None):
    """Mass-delete every DataProduct in the tenant.

    Multi-pass: DMM rejects deletes of products that are referenced by other
    products' ``consumes[]``. Successive passes drain consumer→producer.
    """
    try:
        if not getattr(args, "yes", False):
            confirm = input(
                "Delete EVERY DataProduct in this DMM tenant? This cannot be undone. [y/N] "
            )
            if confirm.lower() not in ("y", "yes"):
                cprint("Cancelled.")
                return 0

        provider = _make_provider(args)
        products = provider.list_products()
        if not products:
            cprint("(tenant already empty)")
            return 0

        cprint(f"Wiping {len(products)} DataProduct(s) from {provider.api_url} ...")
        remaining = list(products)
        deleted = 0
        max_passes = max(1, int(getattr(args, "max_passes", 0)) or 8)
        for attempt in range(1, max_passes + 1):
            if not remaining:
                break
            cprint(f"  -- pass {attempt}: {len(remaining)} remaining --")
            next_round = []
            progress = 0
            for prod in remaining:
                pid = prod.get("id")
                if not pid:
                    continue
                try:
                    provider.delete(pid)
                    cprint(f"    ✓ {pid}")
                    deleted += 1
                    progress += 1
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "in use" in msg or "422" in msg:
                        next_round.append(prod)
                    else:
                        console_error(f"    ✗ {pid}: {msg[:200]}")
            if progress == 0 and next_round:
                cprint(f"  ! no progress; {len(next_round)} still in use after pass {attempt}")
                for prod in next_round:
                    pid = prod.get("id", "?")
                    consumers = _find_consumers(provider, pid)
                    if consumers:
                        cprint(f"    ✗ {pid} blocked by: {', '.join(consumers)}")
                    else:
                        cprint(f"    ✗ {pid}: in use (DMM rejects delete; no visible consumer)")
                break
            remaining = next_round

        final = len(provider.list_products())
        cprint(f"Done: {deleted} deleted, {final} remain.")
        return 0 if final == 0 else 1
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
            cprint_json(json.dumps(teams, indent=2))
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
        cprint_json(json.dumps(result.get("payload", {}), indent=2))
        for odcs in result.get("odcs_contracts", []):
            cprint()
            cprint("=== ODCS Contract Dry Run ===")
            cprint(f"Method: {odcs.get('method', 'PUT')}")
            cprint(f"URL:    {odcs.get('url', '?')}")
            cprint()
            cprint_json(json.dumps(odcs.get("payload", {}), indent=2))


# ---------------------------------------------------------------------------
# DataContract commands (gap 6 — parity with DataProduct surface)
# ---------------------------------------------------------------------------


def _list_contracts(provider: DataMeshManagerProvider) -> list[dict]:
    """List all data contracts in the tenant via the REST API."""
    resp = provider._request("GET", "/api/datacontracts")
    data = resp.json() if callable(getattr(resp, "json", None)) else resp
    return data if isinstance(data, list) else []


def _cmd_list_contracts(args, logger=None):
    """List all data contracts."""
    try:
        provider = _make_provider(args)
        contracts = _list_contracts(provider)
        fmt = getattr(args, "format", "table")

        if fmt == "json":
            sys.stdout.write(json.dumps(contracts, indent=2))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0

        if RICH_AVAILABLE:
            console = Console()
            table = Table(title="Entropy Data — Data Contracts")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="bold")
            table.add_column("API Version")
            table.add_column("Status")
            for c in contracts:
                table.add_row(
                    str(c.get("id", "?")),
                    str(
                        c.get("info", {}).get("title")
                        if isinstance(c.get("info"), dict)
                        else c.get("name", "?")
                    ),
                    str(c.get("apiVersion", "?")),
                    str(c.get("status", "?")),
                )
            console.print(table)
            console.print(f"\n[dim]Total: {len(contracts)} contract(s)[/dim]")
        else:
            for c in contracts:
                cprint(f"  {c.get('id', '?')}")
            cprint(f"\nTotal: {len(contracts)} contract(s)")
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_get_contract(args, logger=None):
    """Get a data contract by ID."""
    try:
        provider = _make_provider(args)
        resp = provider._request("GET", f"/api/datacontracts/{args.contract_id}")
        data = resp.json() if callable(getattr(resp, "json", None)) else resp
        sys.stdout.write(json.dumps(data, indent=2, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1


def _cmd_delete_contract(args, logger=None):
    """Delete a data contract by ID."""
    try:
        if not getattr(args, "yes", False):
            confirm = input(f"Delete data contract '{args.contract_id}'? [y/N] ")
            if confirm.lower() not in ("y", "yes"):
                cprint("Cancelled.")
                return 0
        provider = _make_provider(args)
        provider._request("DELETE", f"/api/datacontracts/{args.contract_id}")
        cprint(f"Deleted contract: {args.contract_id}")
        return 0
    except ProviderError as exc:
        console_error(f"Error: {exc}")
        return 1
