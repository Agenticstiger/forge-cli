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

"""``fluid init`` mode handlers (demo / blank / template).

Lifted from ``cli/init.py`` (host file was 1695 LOC). ~480 LOC of
mode-handler logic. References to host-module symbols
(``RICH_AVAILABLE``, ``copy_template``, ``copy_sample_data``,
``init_local_db``, etc.) go through ``_init.X`` so test patches on
``fluid_build.cli.init.<helper>`` flow through.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fluid_build.cli import init as _init
from fluid_build.cli._logging import error, info
from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import workspace_init_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error
from fluid_build.cli.next_steps import print_next_steps
from fluid_build.util.contract import slugify_identifier


def demo_mode(args, logger: logging.Logger) -> int:
    """Scaffold and run a working customer-360 example.

    This is the handler for ``fluid demo``. It scaffolds customer-360
    template files, initializes a local DuckDB database, optionally
    generates an Airflow DAG, and executes the pipeline end-to-end.

    Note: ``fluid init --quickstart`` does NOT route here — it is
    rewritten in ``detect_mode`` to ``--template customer-360 --yes``
    and dispatches through ``template_mode`` (scaffold only, no run).
    """

    project_name = slugify_identifier(args.name, fallback="my-first-product")
    template = "customer-360"  # Default template

    if _init.RICH_AVAILABLE:
        _init.console.print(
            _init.Panel(
                f"🚀 Creating [bold cyan]{project_name}[/bold cyan] with customer analytics...\n\n"
                f"This will create a working data product with sample data.\n"
                f"No cloud account needed - runs locally with DuckDB.",
                title="Quickstart Mode",
                border_style="cyan",
            )
        )
    else:
        cprint(f"🚀 Creating {project_name} with customer analytics...")

    project_dir = Path(project_name)

    # Guard against symlink attacks.
    if project_dir.is_symlink():
        if _init.RICH_AVAILABLE:
            _init.console.print(f"[red]❌ '{project_name}' is a symlink — refusing to write[/red]")
        else:
            console_error(f"'{project_name}' is a symlink — refusing to write")
        return 1

    # Check if directory already exists
    if project_dir.exists() and any(project_dir.iterdir()):
        if _init.RICH_AVAILABLE:
            _init.console.print(
                f"[red]❌ Directory '{project_name}' already exists and is not empty[/red]"
            )
        else:
            console_error(f"Directory '{project_name}' already exists")
        return 1

    if args.dry_run:
        if _init.RICH_AVAILABLE:
            _init.console.print("[yellow]🔍 Dry run - would create:[/yellow]")
            _init.console.print(f"  📁 {project_name}/")
            _init.console.print(f"  📄 {project_name}/contract.fluid.yaml")
            _init.console.print(f"  📊 {project_name}/data/customers.csv")
            _init.console.print(f"  📊 {project_name}/data/orders.csv")
            _init.console.print(f"  💾 {project_name}/.fluid/db.duckdb")
        return 0

    try:
        # Create project directory
        project_dir.mkdir(parents=True, exist_ok=True)

        # Copy template files
        success = _init.copy_template(project_dir, template, logger)
        if not success:
            return 1

        # Copy sample data
        _init.copy_sample_data(project_dir, template, logger)

        # Initialize local database
        _init.init_local_db(project_dir, args.provider, logger)

        # Generate DAG if contract has orchestration config
        has_dag = False
        if not getattr(args, "no_dag", False):
            try:
                import yaml

                contract_path = project_dir / "contract.fluid.yaml"
                if contract_path.exists():
                    with open(contract_path) as f:
                        contract = yaml.safe_load(f)

                    if _init.should_generate_dag(contract, template):
                        has_dag = _init.generate_dag_for_project(
                            project_dir,
                            contract,
                            logger,
                            _init.console if _init.RICH_AVAILABLE else None,
                            template,
                        )
            except Exception as e:
                logger.warning(f"Failed to generate DAG: {e}")

        # Run pipeline if not --no-run
        if not args.no_run and args.provider == "local":
            _init.run_local_pipeline(project_dir, logger)

        # NOTE: CI/CD scaffolding intentionally removed from this path.
        # Users who want Jenkinsfile / GitHub Actions / GitLab CI / Cloud
        # Build configs should run `fluid scaffold-ci` explicitly — init
        # should produce predictable artifacts, not interactively prompt
        # for cloud-platform-specific files.

        # Show next steps
        _init.show_success_message(project_dir, args.provider, logger, has_dag=has_dag)

        return 0

    except Exception as e:
        error(logger, "demo_failed", error=str(e))
        if _init.RICH_AVAILABLE:
            _init.console.print(f"[red]❌ Demo failed: {e}[/red]")
        return 1


def blank_mode(args, logger: logging.Logger) -> int:
    """Empty project skeleton.

    Slice UX-F rewrite: goes directly through
    :func:`fluid_build.cli.forge_contract_factory.build_minimal_contract`
    + :func:`fluid_build.cli.forge_contract_factory.write_contract` so
    the output shape matches what ``fluid forge --blank`` produces —
    v0.7.2 YAML contract with ``metadata.provenance`` envelope
    alongside the workspace config, the ``.gitignore`` template, and
    the init receipt.

    The previous implementation delegated to ``product_new.run``, which
    emitted a v0.7.x JSON contract under ``bronze_<name>/``.
    That left three different scaffolding paths with three different
    contract formats (blank / forge-blank / template).  This slice
    unifies them.
    """
    from fluid_build.cli.artifact_envelope import dump_json_with_envelope
    from fluid_build.cli.artifact_paths import product_forge_receipt_path
    from fluid_build.cli.artifact_receipts import ReceiptBuilder
    from fluid_build.cli.forge_contract_factory import (
        build_minimal_contract,
        create_and_validate_contract,
    )

    project_name = slugify_identifier(args.name, fallback="my-project")
    project_dir = Path(project_name)

    if _init.RICH_AVAILABLE:
        _init.console.print(
            _init.Panel(
                f"🔧 Creating minimal project: [bold]{project_name}[/bold]\n\n"
                f"Empty skeleton with no assumptions.",
                title="Blank Mode",
                border_style="white",
            )
        )
    else:
        cprint(f"🔧 Creating minimal project: {project_name}")

    # Guard against symlink attacks.
    if project_dir.is_symlink():
        if _init.RICH_AVAILABLE:
            _init.console.print(f"[red]❌ '{project_name}' is a symlink — refusing to write[/red]")
        return 1

    if project_dir.exists() and any(project_dir.iterdir()):
        if _init.RICH_AVAILABLE:
            _init.console.print(
                f"[red]❌ Directory '{project_name}' already exists and is not empty[/red]"
            )
        else:
            console_error(f"Directory '{project_name}' already exists and is not empty")
        return 1

    if getattr(args, "dry_run", False):
        preview_lines = [
            f"  📁 {project_name}/",
            f"  📄 {project_name}/contract.fluid.yaml",
            f"  📄 {project_name}/.fluid/forge-receipt.json",
        ]
        if _init.RICH_AVAILABLE:
            _init.console.print("[yellow]🔍 Dry run - would create:[/yellow]")
            for line in preview_lines:
                _init.console.print(line)
        else:
            cprint("Dry run - would create:")
            for line in preview_lines:
                cprint(line)
        return 0

    # Derive a contract id from the workspace name — mirrors what
    # ``fluid forge --blank`` does when no --context is supplied.
    product_slug = slugify_identifier(project_name, fallback="my-data-product")

    contract = build_minimal_contract(
        product_id=product_slug,
        name=project_name,
    )

    result_path = create_and_validate_contract(
        contract,
        project_dir,
        logger,
        console=_init.console if _init.RICH_AVAILABLE else None,
    )
    if result_path is None:
        return 1

    # Write a forge-receipt.json inside the product so `fluid status`,
    # drift detection, and any downstream tooling see the same shape
    # `fluid forge --blank` produces.
    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    builder = ReceiptBuilder(flow="blank", dry_run=False)
    # Record the contract write with a sha256 for drift-awareness.
    import hashlib

    try:
        sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
        size = result_path.stat().st_size
    except OSError:
        sha, size = None, 0
    builder.record_entry(
        path=Path("contract.fluid.yaml"),
        action="create",
        sha256=sha,
        size=size,
    )
    builder.set_inputs(
        blank=True,
        flow="init-blank",
        provider=getattr(args, "provider", None),
        name=project_name,
    )

    try:
        receipt_path = product_forge_receipt_path(project_dir)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_bytes = dump_json_with_envelope(
            builder.build_document().to_payload(),
            kind="ForgeReceipt",
            command=f"fluid init {project_name} --blank",
            tool_version=str(tool_version),
        )
        receipt_path.write_text(receipt_bytes, encoding="utf-8")
        logger.debug("blank_mode_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — receipt is best-effort
        logger.debug("blank_mode_receipt_write_failed", extra={"error": str(exc)})

    if _init.RICH_AVAILABLE:
        _init.console.print(f"\n✅ Created [cyan]{project_name}/contract.fluid.yaml[/cyan]")
        _init.console.print(f"[dim]Next:[/dim] [cyan]cd {project_name} && fluid validate[/cyan]")
    else:
        cprint(f"Created {project_name}/contract.fluid.yaml")

    return 0


def template_mode(args, logger: logging.Logger) -> int:
    """Create from specific template"""

    template_name = args.template
    if not template_name:
        # Defensive: template_mode should never be reached with an empty
        # ``args.template``.  The interactive menu path runs
        # ``_ask_template_name`` before dispatching, and every CLI flag
        # path requires a value.  This guard catches direct callers and
        # prints an actionable error instead of crashing inside
        # ``copy_template`` with a cryptic Path/None TypeError.
        if _init.RICH_AVAILABLE:
            _init.console.print(
                "[red]❌ No template name provided.[/red]\n"
                "[dim]Pass [bold]--template NAME[/bold] or pick one from "
                "[bold]fluid init --list-templates[/bold].[/dim]"
            )
        else:
            console_error("No template name provided. Pass --template NAME.")
        return 1

    project_name = slugify_identifier(args.name or template_name, fallback="my-project")
    project_dir = Path(project_name)

    if _init.RICH_AVAILABLE:
        _init.console.print(
            _init.Panel(
                f"📦 Creating from template: [bold]{template_name}[/bold]\n"
                f"Project: [bold]{project_name}[/bold]",
                title="Template Mode",
                border_style="blue",
            )
        )
    else:
        cprint(f"📦 Creating from template: {template_name}")

    success = _init.copy_template(project_dir, template_name, logger)
    if not success:
        return 1

    # Slice UX-F: after the template files have landed, rewrite the
    # product's contract.fluid.yaml through ``write_contract`` so it
    # carries the ``metadata.provenance`` envelope (slice 4) and
    # record a forge receipt under ``.fluid/forge-receipt.json`` so
    # the product looks the same as one produced by ``fluid forge``.
    _finalise_template_product(
        template_name=template_name,
        project_name=project_name,
        project_dir=project_dir,
        logger=logger,
    )

    if _init.RICH_AVAILABLE:
        _init.console.print(f"\n✅ Created project from {template_name} template")

    return 0


def _finalise_template_product(
    *,
    template_name: str,
    project_name: str,
    project_dir: Path,
    logger: logging.Logger,
) -> None:
    """Post-copy hook: inject provenance envelope + write forge receipt.

    Runs after ``copy_template`` / ``create_from_template`` lands the
    template files.  Best-effort: never raises.  When the template
    doesn't include a ``contract.fluid.yaml``, the provenance step is
    silently skipped.
    """
    import hashlib

    from fluid_build.cli.artifact_envelope import build_envelope, dump_json_with_envelope
    from fluid_build.cli.artifact_paths import product_forge_receipt_path
    from fluid_build.cli.artifact_receipts import ReceiptBuilder

    try:
        import yaml
    except ImportError:  # pragma: no cover — yaml is a hard dep
        return

    contract_path = project_dir / "contract.fluid.yaml"
    if not contract_path.is_file():
        return  # some templates don't ship a contract; nothing to stamp.

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    # Load the template's contract and inject metadata.provenance.
    try:
        doc = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            return

        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = dict(metadata)
        metadata["provenance"] = build_envelope(
            kind="ContractMetadata",
            command=f"fluid init {project_name} --template {template_name}",
            tool_version=str(tool_version),
        )
        doc["metadata"] = metadata

        contract_path.write_text(
            "# FLUID Data Product Contract\n"
            "# Docs: https://fluid-build.dev/docs/contracts\n"
            + yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort
        logger.debug("template_provenance_inject_failed", extra={"error": str(exc)})

    # Write the forge-receipt.json inside the product.
    try:
        builder = ReceiptBuilder(flow="template", dry_run=False)
        try:
            sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
            size = contract_path.stat().st_size
        except OSError:
            sha, size = None, 0
        builder.record_entry(
            path=Path("contract.fluid.yaml"),
            action="create",
            sha256=sha,
            size=size,
        )
        builder.set_inputs(
            template=template_name,
            name=project_name,
        )
        receipt_path = product_forge_receipt_path(project_dir)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            dump_json_with_envelope(
                builder.build_document().to_payload(),
                kind="ForgeReceipt",
                command=f"fluid init {project_name} --template {template_name}",
                tool_version=str(tool_version),
            ),
            encoding="utf-8",
        )
        logger.debug("template_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — receipt is best-effort
        logger.debug("template_receipt_write_failed", extra={"error": str(exc)})


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


#: Filename suffixes that should NEVER be copied from a template source.
#: ``*.old`` files are template-author scratch artifacts (backup copies of
#: old contract shapes), not something the user should see inherit into
#: their new project.
_TEMPLATE_IGNORE_SUFFIXES = (".old", ".bak", ".tmp", ".swp")
_TEMPLATE_IGNORE_NAMES = {"__pycache__", ".DS_Store"}


def _should_copy_template_entry(entry: Path) -> bool:
    """Return True if *entry* should be copied into a new project dir."""
    if entry.name in _TEMPLATE_IGNORE_NAMES:
        return False
    if any(entry.name.endswith(suffix) for suffix in _TEMPLATE_IGNORE_SUFFIXES):
        return False
    return True


def _bridge_init_template_to_forge(
    project_dir: Path, template_name: str, logger: logging.Logger
) -> bool:
    """When ``init --template <X>`` doesn't find a directory template,
    fall through to the forge code-template registry so the same name
    works in both flows.

    Routes through ``forge_modes.run_template_mode`` which now applies
    the v0.7.3 coercion layer to every template's contract output.
    """
    try:
        from fluid_build.forge.core.registry import template_registry
    except Exception:  # noqa: BLE001
        return False

    try:
        if template_registry.get(template_name) is None:
            return False
    except Exception:  # noqa: BLE001
        return False

    project_dir.mkdir(parents=True, exist_ok=True)

    import argparse as _argparse

    bridge_args = _argparse.Namespace(
        target_dir=str(project_dir),
        provider="local",
        template=template_name,
        scaffold=template_name,
        non_interactive=True,
        dry_run=False,
        domain=None,
        data_product_type=None,
    )

    try:
        from fluid_build.cli.forge_modes import run_template_mode as _run_template
    except Exception as exc:  # noqa: BLE001
        logger.debug("forge_template_runner_unavailable: %s", exc)
        return False

    rc = _run_template(
        bridge_args,
        logger,
        get_target_directory_fn=lambda _a, _default: project_dir,
    )
    return rc == 0
