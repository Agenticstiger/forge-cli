# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Template-mode runner for ``fluid forge --template``.

Re-exported by ``forge_modes`` so patches targeting
``fluid_build.cli.forge_modes.run_template_mode`` keep resolving.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from fluid_build._console import literal_error_text
from fluid_build.cli._forge_mode_errors import log_forge_failure
from fluid_build.cli.console import error as console_error
from fluid_build.cli.console import success

try:
    from rich.console import Console

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


def run_template_mode(
    args: Any,
    logger: logging.Logger,
    *,
    get_target_directory_fn: Callable[[Any, str], Path],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Run Forge with traditional template mode."""
    console = console_factory() if console_factory else None

    try:
        if console and not args.non_interactive:
            console.print("\n[bold blue]📋 Template Mode[/bold blue]")
            console.print("[dim]Creating project from template...[/dim]\n")

        from datetime import datetime

        from fluid_build.forge.core.engine import ForgeEngine, GenerationContext
        from fluid_build.forge.core.registry import template_registry

        template_name = args.template or "starter"
        target_dir = get_target_directory_fn(args, f"{template_name}-project")
        provider = args.provider or "local"
        template = template_registry.get(template_name)
        if not template:
            available = template_registry.list_available()
            logger.error(
                "Template '%s' not found. Available templates: %s",
                template_name,
                ", ".join(available),
            )
            return 1

        metadata = template.get_metadata()
        context = GenerationContext(
            project_config={
                "name": target_dir.name,
                "description": f"A {template_name} data product",
                "domain": "analytics",
                "owner": "data-team",
                "provider": provider,
            },
            target_dir=target_dir,
            template_metadata=metadata,
            provider_config={"provider": provider},
            user_selections={},
            forge_version="2.0.0",
            creation_time=datetime.now().isoformat(),
        )

        ForgeEngine()
        logger.info("📝 Generating %s project...", template_name)

        if args.dry_run if hasattr(args, "dry_run") else False:
            logger.info("DRY RUN: Would create project in %s", target_dir)
            logger.info("Template: %s", metadata.name)
            logger.info("Description: %s", metadata.description)
            return 0

        target_dir.mkdir(parents=True, exist_ok=True)
        # Templates emit fluid-schema-0.7.x contracts directly via the
        # shared v0.7.3 builder, so no coercion layer is needed.
        contract = template.generate_contract(context)
        import yaml

        with open(target_dir / "contract.fluid.yaml", "w", encoding="utf-8") as handle:
            yaml.dump(contract, handle, default_flow_style=False, sort_keys=False)

        for path_str, content in template.generate_structure(context).items():
            if path_str.endswith("/"):
                (target_dir / path_str.rstrip("/")).mkdir(parents=True, exist_ok=True)

        try:
            template._create_readme(target_dir, context)
        except (AttributeError, TypeError):
            pass

        if console:
            console.print(f"[green]✅ Template project created at {target_dir}[/green]")
        else:
            success(f"Template project created at {target_dir}")

        logger.info("\n📖 Next Steps:")
        logger.info("1. cd %s", target_dir)
        logger.info("2. Review contract.fluid.yaml")
        logger.info("3. fluid validate contract.fluid.yaml")
        return 0
    except Exception as exc:  # noqa: BLE001
        log_forge_failure(logger, "Template mode failed", exc)
        if console:
            console.print(literal_error_text(f"❌ Template mode failed: {exc}", "red"))
        else:
            console_error(f"Template mode failed: {exc}")
        return 1
