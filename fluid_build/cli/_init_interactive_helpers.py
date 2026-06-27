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

"""``fluid init`` interactive UI helpers — physical extraction.

Lifted from ``cli/init.py`` (the host file was 1920 LOC; the
interactive-prompts block is ~241 LOC of pure UI code with limited
coupling). The exported helpers:

* :func:`_print_welcome_panel`
* :func:`_list_filesystem_templates`
* :func:`_ask_template_name`
* :func:`_ask_industry`
* :func:`_ask_creation_mode`
* :func:`_print_workspace_products`
* :func:`_redirect_existing_workspace`

``init.py`` re-imports each at module top so existing test patches
that target ``fluid_build.cli.init.<helper>`` still resolve via the
namespace. ``RICH_AVAILABLE`` / ``console`` / ``Prompt`` / ``Panel``
are read via attribute access on the canonical ``cli.init`` module
so test-time patches flow through.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fluid_build.cli.console import cprint

# ── Indirection accessors ───────────────────────────────────────────────


def _rich_available() -> bool:
    """Read ``RICH_AVAILABLE`` from ``cli.init`` (canonical home)."""
    from fluid_build.cli import init as _init

    return getattr(_init, "RICH_AVAILABLE", False)


def _get_console():
    """Read ``console`` from ``cli.init`` so test patches flow through."""
    from fluid_build.cli import init as _init

    return getattr(_init, "console", None)


def _get_panel():
    from fluid_build.cli import init as _init

    return getattr(_init, "Panel", None)


def _get_prompt():
    from fluid_build.cli import init as _init

    return getattr(_init, "Prompt", None)


def _mark_first_run_complete() -> None:
    """Forward to the canonical impl in ``cli.init``."""
    from fluid_build.cli import init as _init

    fn = getattr(_init, "_mark_first_run_complete_impl", None)
    if fn is None:
        # Fall back to the public name; tests may have patched it.
        fn = getattr(_init, "_mark_first_run_complete", None)
    if fn is not None and fn is not _mark_first_run_complete:
        fn()


# ── UI helpers ──────────────────────────────────────────────────────────


def _print_welcome_panel() -> None:
    """Show the compact welcome panel for first-time users."""
    if not _rich_available():
        return
    console = _get_console()
    Panel = _get_panel()
    if console is None or Panel is None:
        return
    console.print(
        Panel(
            "[bold]FLUID[/bold] turns a YAML contract into a deployed, governed data "
            "product —\nlike [cyan]terraform plan/apply[/cyan], but for tables, views, "
            "and files.\n\n"
            "[dim]Tip: in a hurry? [bold]fluid init my-project --quickstart[/bold] "
            "ships a working\ncustomer-360 example in ~30 seconds with zero "
            "questions.[/dim]\n\n"
            "Let's set up your first project.\n\n"
            "[dim]Advanced: [bold]fluid init --help[/bold] for cloud providers "
            "(gcp/snowflake/aws).\n"
            "Migrating from dbt/Terraform? See [bold]fluid import[/bold].[/dim]",
            title="Welcome to FLUID",
            border_style="blue",
        )
    )
    console.print()


def _list_filesystem_templates() -> List[str]:
    """Return the list of template names that ``copy_template`` can
    actually find on disk.

    The registry (``simple_forge.list_templates``) returns logical
    template names that don't correspond 1:1 to filesystem directories.
    This helper walks the filesystem directly so the picker only ever
    offers names that exist.
    """
    # Resolve relative to the canonical ``cli.init`` module so the
    # filesystem walk works the same regardless of import path.
    import fluid_build

    templates_dir = Path(fluid_build.__file__).parent / "templates"
    try:
        return sorted(
            p.name
            for p in templates_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
        )
    except (OSError, FileNotFoundError):
        return []


def _ask_template_name() -> Optional[str]:
    """Prompt the user to pick a template."""
    default_name = "customer-360"

    # Resolve via the canonical ``cli.init`` namespace so tests that
    # ``patch.object(init, "_list_filesystem_templates", ...)`` flow
    # through to this caller.
    from fluid_build.cli import init as _init

    list_fn = getattr(_init, "_list_filesystem_templates", _list_filesystem_templates)
    names = list_fn()
    if not names:
        return default_name

    if default_name not in names:
        default_name = names[0]

    if not _rich_available():
        return default_name

    console = _get_console()
    Prompt = _get_prompt()
    if console is None or Prompt is None:
        return default_name

    console.print()
    console.print("[dim]Available templates:[/dim]")
    for i, name in enumerate(names, 1):
        marker = " [dim](default)[/dim]" if name == default_name else ""
        console.print(f"  [bold]{i}.[/bold] {name}{marker}")
    console.print()

    valid_indices = [str(i) for i in range(1, len(names) + 1)]
    default_index = str(names.index(default_name) + 1)
    try:
        choice = Prompt.ask("Choose template", choices=valid_indices, default=default_index)
    except Exception:  # noqa: BLE001 — never crash the init flow over a prompt
        return default_name

    try:
        return names[int(choice) - 1]
    except (ValueError, IndexError):
        return default_name


def _ask_industry(workspace_root: Path) -> Optional[str]:
    """Present the industry picker and generate ``.fluid/skills.yaml``."""
    from fluid_build.cli.industry_skills import generate_skills_file, list_industries

    industries = list_industries()

    if not _rich_available():
        generate_skills_file(None, workspace_root)
        return None

    console = _get_console()
    Prompt = _get_prompt()
    Panel = _get_panel()
    if console is None or Prompt is None or Panel is None:
        generate_skills_file(None, workspace_root)
        return None

    console.print("[dim]What industry is this project for?[/dim]\n")
    for i, ind in enumerate(industries, 1):
        desc = f"  [dim]({ind['description']})[/dim]" if ind["description"] else ""
        console.print(f"  [bold]{i}.[/bold] {ind['label']}{desc}")
    console.print()

    valid = [str(i) for i in range(1, len(industries) + 1)]
    choice = Prompt.ask("Choose", choices=valid, default=str(len(industries)))
    selected = industries[int(choice) - 1]

    industry_key = selected["key"]
    out_path = generate_skills_file(industry_key, workspace_root)

    if industry_key == "other":
        console.print(
            '\n[yellow]No industry-specific skills shipped for "Other".[/yellow]\n'
            "[dim]Agents will work without domain-specific guidance.\n"
            "You can add industry skills later with:[/dim] "
            "[cyan]fluid skills update[/cyan]\n"
        )
    else:
        console.print(
            Panel(
                f"[bold]Generated .fluid/skills.yaml for {selected['label']}[/bold]\n\n"
                "This file contains industry-specific knowledge that\n"
                "all FLUID agents will use:\n"
                f"  [dim]Industry:[/dim]    {selected['label']}\n"
                f"  [dim]File:[/dim]        {out_path.relative_to(workspace_root)}\n\n"
                "Keep this file in version control — your whole\n"
                "team will benefit from shared project context.",
                border_style="green",
            )
        )

    return industry_key


def _detect_ai_available() -> bool:
    """True when an LLM provider is configured (env var / saved config / key).

    Reuses the welcome-scan credential ladder (``FLUID_LLM_PROVIDER`` → saved
    ``ai_config.json`` → AI-credential env vars) so the menu's Enter-default
    never disagrees with the rest of the run. Best-effort: any failure resolves
    to ``False``, which is the safe direction — it defaults the menu to the
    Quickstart path that always reaches a working project without a key.
    """
    try:
        from fluid_build.cli._welcome_scan import _probe_ai_credentials

        return bool(_probe_ai_credentials().get("ai_configured"))
    except Exception:  # noqa: BLE001 — never let detection block the menu
        return False


def _ask_creation_mode(ai_available: Optional[bool] = None) -> str:
    """Present the creation menu and return the selected mode.

    The Enter-key default adapts to context. When **no** LLM provider is
    configured, it defaults to **Quickstart**, so a freshly-installed user who
    just presses Enter reaches a working ``customer-360`` project in ~30s with
    zero setup — the AI path would otherwise dead-end on a missing API key, a
    bad first impression. When AI *is* configured, the richer 'Let AI design
    it' path stays the default. ``ai_available`` is probed via
    :func:`_detect_ai_available` when not supplied (tests inject it).
    """
    if not _rich_available():
        return "quickstart"

    console = _get_console()
    Prompt = _get_prompt()
    Panel = _get_panel()
    if console is None or Prompt is None or Panel is None:
        return "quickstart"

    if ai_available is None:
        ai_available = _detect_ai_available()

    console.print(
        Panel(
            "A [bold]workspace[/bold] is a home for your data products.\n"
            "Each product gets its own folder and contract.\n"
            "You can add more products later with [cyan]fluid forge[/cyan].",
            title="New Project",
            border_style="blue",
        )
    )
    console.print("[dim]How would you like to create your first data product?[/dim]\n")
    if ai_available:
        quickstart_tag = "(customer-360 example, zero questions, ~30s) ← fastest"
        ai_tag = "(recommended — just answer questions)"
        default_choice = "2"
    else:
        quickstart_tag = (
            "(customer-360 example, zero questions, ~30s) ← recommended, no API key needed"
        )
        ai_tag = "(needs an API key — run 'fluid ai setup' first)"
        default_choice = "1"
    console.print(f"  [bold]1.[/bold] Quickstart                 [dim]{quickstart_tag}[/dim]")
    console.print(f"  [bold]2.[/bold] Let AI design it           [dim]{ai_tag}[/dim]")
    console.print(
        "  [bold]3.[/bold] Start from a template     [dim](pre-built, customize later)[/dim]"
    )
    console.print(
        "  [bold]4.[/bold] Empty contract             [dim](for experienced users)[/dim]\n"
    )

    choice = Prompt.ask(
        "Choose",
        choices=["1", "2", "3", "4"],
        default=default_choice,
    )
    fallback = "ai" if ai_available else "quickstart"
    return {"1": "quickstart", "2": "ai", "3": "template", "4": "blank"}.get(choice, fallback)


def _print_workspace_products(existing: List, ws_name: str) -> None:
    """Print the workspace product listing (shared by redirect paths)."""
    console = _get_console()
    if console is None:
        return
    console.print(
        f"[dim]Workspace: [bold]{ws_name}[/bold] ({len(existing)} existing product"
        f"{'s' if len(existing) != 1 else ''})[/dim]"
    )
    for product in existing[:10]:
        meta = []
        if product.expose_count:
            meta.append(f"{product.expose_count} expose{'s' if product.expose_count != 1 else ''}")
        if product.provider:
            meta.append(f"provider: {product.provider}")
        suffix = f"  ({', '.join(meta)})" if meta else ""
        console.print(f"[dim]  • [bold]{product.name}[/bold]{suffix}[/dim]")
        console.print(f"[dim]    {product.path}[/dim]")
    console.print()


def _redirect_existing_workspace(
    existing: List,
    ws_root: Path,
    is_first_time: bool = False,
) -> Optional[str]:
    """Show existing products and redirect the user."""
    from fluid_build.cli import init as _init

    if not _rich_available():
        cprint(f"This is already a FLUID workspace with {len(existing)} product(s).")
        for product in existing[:10]:
            cprint(f"  • {product.name}  ({product.path})")
        cprint("Use 'fluid forge' to add another product.")
        if is_first_time:
            _init._mark_first_run_complete()
        return None

    from fluid_build.cli.workspace_config import load_workspace_config

    console = _get_console()
    if console is None:
        return None

    ws_config = load_workspace_config(ws_root)
    name = ws_config.name or ws_root.name

    if is_first_time:
        _print_welcome_panel()
        _print_workspace_products(existing, name)
        console.print("This workspace is already set up. To add a product:")
        console.print("  [cyan]fluid forge[/cyan]\n")
        console.print("To work with existing products:")
        console.print("  [cyan]fluid validate[/cyan]       [dim]← check all contracts[/dim]")
        console.print("  [cyan]fluid plan[/cyan]           [dim]← generate execution plan[/dim]")
        console.print("  [cyan]fluid doctor[/cyan]         [dim]← check your environment[/dim]")
        _init._mark_first_run_complete()
        return None

    _print_workspace_products(existing, name)
    console.print("To add another product:  [cyan]fluid forge[/cyan]")
    return None


__all__ = [
    "_ask_creation_mode",
    "_ask_industry",
    "_ask_template_name",
    "_list_filesystem_templates",
    "_print_welcome_panel",
    "_print_workspace_products",
    "_redirect_existing_workspace",
]
