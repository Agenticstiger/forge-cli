# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Mode handlers for `fluid forge` (guided, template). AI mode lives in ``forge_ai``."""

from __future__ import annotations

__all__ = [
    "run_guided_mode",
    "run_template_mode",
]


import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fluid_build._console import literal_error_text
from fluid_build.cli._forge_mode_errors import forge_mode_error_boundary
from fluid_build.cli.console import cprint, success

try:
    from rich.console import Console

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via non-Rich fallbacks
    Console = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    _CI_COMPLEXITY_CHOICES,
    _CI_COMPLEXITY_VALUES,
    _CI_PROVIDER_ALIASES,
    _CI_PROVIDER_CHOICES,
    _CI_PROVIDER_VALUES,
)
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    ci_killswitch_enabled as _ci_killswitch_enabled,
)
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    normalize_ci_provider as _normalize_ci_provider,
)
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    prompt_ci_menu as _prompt_ci_menu,
)
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    resolve_ci_choice as _resolve_ci_choice,
)
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402,F401
    scaffold_ci_pipeline as _scaffold_ci_pipeline_impl,
)


def _scaffold_ci_pipeline(
    args: Any,
    target_dir: Path,
    context: Dict[str, Any],
    console: Any,
    *,
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    get_cli_arg_fn: Callable[[Any, str, Any], Any],
    dry_run: bool,
) -> tuple[Optional[str], Optional[str]]:
    """Thin re-export shim.

    The body lived inline at ~230 LOC before extraction; it now lives
    in :mod:`fluid_build.cli._forge_ci_scaffolder`. Tests that mock
    ``fluid_build.cli.forge_modes._scaffold_ci_pipeline`` keep working
    because we forward through the module-attribute-access pattern.
    """
    return _scaffold_ci_pipeline_impl(
        args,
        target_dir,
        context,
        console,
        ask_dialog_question_fn=ask_dialog_question_fn,
        get_cli_arg_fn=get_cli_arg_fn,
        dry_run=dry_run,
    )


# ``run_template_mode`` lives in the ``_template_mode`` sibling module. Re-imported here so
# test patches that target ``fluid_build.cli.forge_modes.run_template_mode`` still resolve.
from fluid_build.cli._template_mode import run_template_mode  # noqa: E402,F401


def _guided_non_interactive_defaults(args: Any) -> tuple[str, str, str]:
    """Derive ``(product_id, domain, provider)`` for the headless guided path.

    Answers come from flags when supplied, otherwise from the same
    defaults the interactive prompts pre-select (``my-data-product`` /
    ``analytics`` / ``local``). Used by ``--offline --non-interactive``
    so authoring needs neither a TTY nor a network round-trip.
    """
    target_dir = getattr(args, "target_dir", None)
    if target_dir:
        product_id = os.path.basename(str(target_dir).rstrip("/\\")) or "my-data-product"
    else:
        product_id = "my-data-product"
    domain = (getattr(args, "domain", None) or "").strip() or "analytics"
    provider = (getattr(args, "provider", None) or "").strip() or "local"
    return product_id, domain, provider


def run_guided_mode(
    args: Any,
    logger: logging.Logger,
    *,
    get_target_directory_fn: Callable[[Any, str], Path],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Create a data product via 3 quick prompts (no LLM, no network).

    Interactive by default. With ``--non-interactive`` every answer is
    derived from flags + defaults so the flow runs headless and without a
    TTY — this is the path ``fluid forge --offline --non-interactive``
    uses for air-gapped / CI authoring.
    """
    console = console_factory() if console_factory else None
    is_non_interactive = bool(getattr(args, "non_interactive", False))

    # Guard: interactive guided mode requires a TTY. The non-interactive
    # path derives its answers from flags, so it is exempt.
    if not is_non_interactive and not sys.stdin.isatty():
        logger.error("Guided mode requires an interactive terminal")
        if console:
            console.print(
                "[red]Guided mode requires an interactive terminal.[/red]\n"
                "[dim]Add --non-interactive for a prompt-free run.[/dim]"
            )
        return 1

    def _render_guided_error(console: Any, exc: BaseException) -> None:
        if console:
            console.print(literal_error_text(f"Guided mode failed: {exc}", "red"))

    with forge_mode_error_boundary(
        logger,
        console,
        fail_label="Guided mode failed",
        render_error=_render_guided_error,
        cancel_label="Guided mode cancelled",
    ) as boundary:
        from fluid_build.cli.forge_ui import ask_numbered_choice

        if is_non_interactive:
            product_id, domain, provider = _guided_non_interactive_defaults(args)
            owner = os.getenv("USER", "data-team")
            description = f"{product_id.replace('-', ' ').title()} data product"
        elif console and RICH_AVAILABLE:
            from rich.panel import Panel
            from rich.prompt import Prompt

            console.print(
                Panel(
                    "Let's create a new data product in 3 quick steps.\n"
                    "[dim]Press Enter to accept the default for any question.[/dim]",
                    title="Forge — Guided Mode",
                    border_style="cyan",
                )
            )

            # Step 1: Name
            product_id = Prompt.ask(
                "[bold]Step 1/3:[/bold] What do you want to call this data product?",
                default="my-data-product",
            )

            # Step 2: Domain
            domain = ask_numbered_choice(
                console,
                "Step 2/3: What area does this data product belong to?",
                [
                    ("analytics", "Analytics -- dashboards, reports, metrics"),
                    ("data-engineering", "Data Engineering -- pipelines, ETL, transformations"),
                    ("ml", "Machine Learning -- features, models, predictions"),
                    ("governance", "Governance -- data quality, compliance, lineage"),
                ],
                default=1,
            )

            # Step 3: Provider
            provider = ask_numbered_choice(
                console,
                "Step 3/3: Where will this data product run?",
                [
                    ("local", "Local (DuckDB) -- great for getting started"),
                    ("gcp", "Google Cloud (BigQuery)"),
                    ("snowflake", "Snowflake"),
                    ("aws", "AWS (S3 + Glue)"),
                ],
                default=1,
            )

            owner = os.getenv("USER", "data-team")
            description = f"{product_id.replace('-', ' ').title()} data product"
        else:
            product_id = (
                input("Step 1/3 -- Product name [my-data-product]: ").strip() or "my-data-product"
            )

            cprint("\nStep 2/3 -- What area does this belong to?")
            cprint("  1. Analytics")
            cprint("  2. Data Engineering")
            cprint("  3. Machine Learning")
            cprint("  4. Governance")
            d = input("Enter number [1]: ").strip() or "1"
            domain = {"1": "analytics", "2": "data-engineering", "3": "ml", "4": "governance"}.get(
                d, "analytics"
            )

            cprint("\nStep 3/3 -- Where will it run?")
            cprint("  1. Local (DuckDB)")
            cprint("  2. Google Cloud")
            cprint("  3. Snowflake")
            cprint("  4. AWS")
            p = input("Enter number [1]: ").strip() or "1"
            provider = {"1": "local", "2": "gcp", "3": "snowflake", "4": "aws"}.get(p, "local")

            owner = os.getenv("USER", "data-team")
            description = f"{product_id.replace('-', ' ').title()} data product"

        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            create_and_validate_contract,
        )
        from fluid_build.cli.forge_validation import sanitize_project_name

        safe_id = sanitize_project_name(product_id, strict=False)
        target_dir = get_target_directory_fn(args, safe_id)

        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            if console:
                console.print(f"[dim]DRY RUN: Would create {safe_id} in {target_dir}[/dim]")
            return 0

        engine = "dbt" if provider in ("gcp", "local") else "sql"
        contract = build_minimal_contract(
            product_id=safe_id,
            name=product_id.replace("-", " ").title(),
            domain=domain,
            owner=owner,
            description=description,
            engine=engine,
            tags=["guided"],
        )

        contract_path = create_and_validate_contract(contract, target_dir, logger, console)
        if not contract_path:
            return 1

        # Minimal directory scaffolding
        (target_dir / "config").mkdir(exist_ok=True)
        (target_dir / "docs").mkdir(exist_ok=True)
        if engine == "dbt":
            (target_dir / "dbt" / "models").mkdir(parents=True, exist_ok=True)
        else:
            (target_dir / "sql").mkdir(exist_ok=True)

        from fluid_build.cli.forge_contract_factory import DOCS_URL as _DOCS_URL

        if console and RICH_AVAILABLE:
            from rich.panel import Panel

            console.print(
                Panel(
                    f"[green]Created data product:[/green] [bold]{safe_id}[/bold]\n\n"
                    f"  Contract: {contract_path}\n\n"
                    "Next steps:\n"
                    f"  1. cd {target_dir}\n"
                    "  2. Edit contract.fluid.yaml with your builds\n"
                    "  3. fluid validate contract.fluid.yaml\n"
                    "  4. fluid plan contract.fluid.yaml --out runtime/plan.json\n"
                    "  5. fluid apply runtime/plan.json\n\n"
                    f"[dim]Docs: {_DOCS_URL}[/dim]\n"
                    "[dim]Tip: run 'fluid ai setup' to unlock AI Copilot.[/dim]",
                    title="Forge Complete",
                    border_style="green",
                )
            )
        else:
            success(f"Created data product at {target_dir}")
            cprint(f"Docs: {_DOCS_URL}")

        return 0
    return boundary.exit_code
