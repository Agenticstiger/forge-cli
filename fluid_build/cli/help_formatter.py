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
Custom Rich-formatted help for FLUID CLI

Provides beautiful, colorful help output using Rich library.
"""

from __future__ import annotations

import argparse

from fluid_build import __version__ as _VERSION


try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def print_first_run_help(parser: argparse.ArgumentParser) -> None:
    """
    Compact first-run experience for new users.

    Shown when ~/.fluid does not exist yet.  Focused: three steps, one
    example command, and a pointer to --help for the full reference.
    """
    if not RICH_AVAILABLE:
        parser.print_help()
        return

    console = Console()
    console.print()

    # Welcome — warm but brief
    console.print(
        Panel(
            "[bold bright_cyan]🌊 Welcome to FLUID Forge![/bold bright_cyan]\n\n"
            "[bright_white]Build, deploy and govern data products with declarative contracts.[/bright_white]\n"
            "[dim]No cloud account required — the local provider runs everything on your machine.[/dim]",
            border_style="bright_cyan",
            padding=(1, 2),
            title="[bold bright_white]👋 First Time?[/bold bright_white]",
            title_align="left",
        )
    )
    console.print()

    # Three paths — instant demo / quickstart / AI-designed
    console.print("[bold bright_green]Pick your path:[/bold bright_green]\n")

    console.print("[bold bright_yellow]  Instant[/bold bright_yellow] [dim](one command, ~30s — zero config)[/dim]")
    instant = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    instant.add_column(style="bright_cyan", width=46)
    instant.add_column(style="dim bright_white")
    instant.add_row("fluid demo", "Scaffold + run a working customer-360 example")
    console.print(instant)

    console.print("[bold bright_yellow]  Your project[/bold bright_yellow] [dim](template-based, no AI)[/dim]")
    fast = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    fast.add_column(style="bright_cyan", width=46)
    fast.add_column(style="dim bright_white")
    fast.add_row("fluid init --list-templates", "See what templates are available")
    fast.add_row("fluid init my-project --quickstart", "Scaffold a customer-360 project (run with fluid apply)")
    fast.add_row("cd my-project && fluid apply --yes", "Run the pipeline end-to-end")
    console.print(fast)

    console.print("[bold bright_yellow]  AI-designed[/bold bright_yellow] [dim](recommended for custom work)[/dim]")
    ai = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    ai.add_column(style="bright_cyan", width=46)
    ai.add_column(style="dim bright_white")
    ai.add_row("fluid init my-project", "Answer a few questions")
    ai.add_row("cd my-project && fluid apply --yes", "Run the pipeline end-to-end")
    console.print(ai)
    console.print(
        "  [dim]A free Gemini key works: [bright_cyan]https://aistudio.google.com/apikey[/bright_cyan][/dim]\n"
    )

    # Already-in-a-workspace hint
    console.print(
        "  [dim]Already inside a workspace? Use [bright_cyan]fluid forge[/bright_cyan] "
        "to add another data product to it.[/dim]"
    )
    # Migration-from-legacy hint
    console.print(
        "  [dim]Migrating from dbt or Terraform? Use [bright_cyan]fluid import[/bright_cyan] "
        "to generate FLUID contracts from your existing project.[/dim]\n"
    )

    # Helpful pointers
    console.print(
        Panel(
            "[bright_yellow]fluid doctor[/bright_yellow]                  Check your system is ready\n"
            "[bright_yellow]fluid init --list-templates[/bright_yellow]   See available templates\n"
            "[bright_yellow]fluid import[/bright_yellow]                  Migrate from dbt/Terraform\n"
            "[bright_yellow]fluid --help[/bright_yellow]                  See all commands & options\n"
            "[bright_yellow]fluid <cmd> -h[/bright_yellow]                Help for a specific command\n\n"
            "[dim]📚 Docs:[/dim]  [bright_cyan]https://github.com/open-data-protocol/fluid[/bright_cyan]",
            title="[bold bright_white]What's next?[/bold bright_white]",
            title_align="left",
            border_style="bright_white",
            padding=(1, 2),
        )
    )
    console.print()


def print_main_help(parser: argparse.ArgumentParser) -> None:
    """
    Print polished main help message with Rich formatting.

    Shows the core workflow, generation, enterprise integration,
    quality/governance, and utilities — plus a discovery line for
    the remaining power-user commands.
    """
    if not RICH_AVAILABLE:
        parser.print_help()
        return

    console = Console()
    W = console.width or 80  # terminal width
    console.print()

    # ── Header ──────────────────────────────────────────────────────
    bar = "[dim bright_cyan]" + "━" * (W - 4) + "[/dim bright_cyan]"
    console.print(f"  {bar}")
    console.print(
        "  [bold bright_cyan]🌊  F L U I D   F O R G E[/bold bright_cyan]  "
        f"[dim]v{_VERSION}[/dim]     "
        "[italic bright_white]Declarative data products — from contract to cloud[/italic bright_white]"
    )
    console.print(f"  {bar}")
    console.print()

    # ── Usage ───────────────────────────────────────────────────────
    console.print(
        "  [bold]USAGE[/bold]   "
        "[bright_cyan]fluid[/bright_cyan] "
        "[bright_green]<command>[/bright_green] [dim][options][/dim]        "
        "[dim]Run[/dim] [bright_cyan]fluid <cmd> -h[/bright_cyan] [dim]for per-command help[/dim]"
    )
    console.print()

    # ── helper to build a group ─────────────────────────────────────
    def _section(icon: str, title: str, color: str, rows: list[tuple[str, str]]) -> None:
        tbl = Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            pad_edge=False,
        )
        tbl.add_column(style=f"{color} bold", min_width=24, max_width=24)
        tbl.add_column(style="bright_white")
        for cmd, desc in rows:
            tbl.add_row(f"  {cmd}", desc)
        console.print(f"  {icon} [bold {color}]{title}[/bold {color}]")
        console.print(tbl)
        console.print()

    # ── Core Workflow ───────────────────────────────────────────────
    _section(
        "▸",
        "Core Workflow",
        "bright_blue",
        [
            ("init", "Create a new project  [dim]--template · --blank[/dim]"),
            ("forge", "Create a data product  [dim]AI copilot · template · --blank[/dim]"),
            ("validate", "Check contract syntax and provider rules"),
            ("plan", "Plan execution  [dim]--html · --env · --out[/dim]"),
            ("apply", "Deploy end-to-end  [dim]--yes · --dry-run · --build[/dim]"),
        ],
    )

    # ── Generation ──────────────────────────────────────────────────
    console.print("  ▸ [bold green]Generate[/bold green]")
    gen_tbl = Table(
        show_header=False,
        box=None,
        padding=(0, 1),
        pad_edge=False,
    )
    gen_tbl.add_column(style="green bold", min_width=28, max_width=28)
    gen_tbl.add_column(style="bright_white")
    gen_tbl.add_row("  generate transformation", "dbt, SQL, Spark artifacts")
    gen_tbl.add_row("  generate schedule", "Airflow, Dagster, Prefect DAGs")
    gen_tbl.add_row("  generate ci", "GitHub Actions, GitLab CI pipelines")
    gen_tbl.add_row("  generate standard", "OPDS, ODCS, ODPS, ODPS-Bitol")
    console.print(gen_tbl)
    console.print()

    # ── Integrations ────────────────────────────────────────────────
    _section(
        "▸",
        "Integrations",
        "bright_magenta",
        [
            ("publish", "Publish to enterprise data catalogs"),
            ("market", "Browse & discover data products  [dim]--blueprints[/dim]"),
            ("import", "Import existing dbt/Terraform/SQL projects"),
        ],
    )

    # ── Quality & Governance ────────────────────────────────────────
    _section(
        "▸",
        "Quality & Governance",
        "yellow",
        [
            ("policy-check", "Governance & compliance checks  [dim]--strict[/dim]"),
            ("diff", "Detect drift from deployed state"),
            ("test", "Test contract against live data  [dim]--output json · --strict[/dim]"),
            ("verify", "Confirm deployed state matches the contract"),
        ],
    )

    # ── Utilities & System ──────────────────────────────────────────
    _section(
        "▸",
        "Utilities",
        "bright_white",
        [
            ("config", "Get/set default provider, project, region"),
            ("split", "Split contract into composable fragments"),
            ("bundle", "Bundle fragments into single contract"),
            ("auth", "Manage cloud provider credentials  [dim]login · status · logout[/dim]"),
            ("doctor", "Check system health & dependencies"),
            ("providers", "List available infrastructure providers"),
            ("version", "Show version info"),
        ],
    )

    # ── Quick-start ─────────────────────────────────────────────────
    console.print(f"  {bar}")
    console.print(
        "  [bold bright_green]⚡ Quick Start[/bold bright_green]     "
        "[bright_cyan]fluid init[/bright_cyan]  →  "
        "[bright_cyan]fluid forge[/bright_cyan]  →  "
        "[bright_cyan]fluid validate[/bright_cyan]  →  "
        "[bright_cyan]fluid plan[/bright_cyan]  →  "
        "[bright_cyan]fluid apply[/bright_cyan]"
    )
    console.print(f"  {bar}")

    # ── Footer ──────────────────────────────────────────────────────
    console.print(
        "  [dim]Docs[/dim]  [bright_cyan]https://github.com/open-data-protocol/fluid[/bright_cyan]   "
        "[dim]│[/dim]   "
        "[dim]Health[/dim]  [bright_cyan]fluid doctor[/bright_cyan]"
    )
    console.print(
        "  [dim italic]Made with ❤️  by DustLabs.co.za — building the future of declarative data engineering[/dim italic]"
    )
    console.print()


def print_forge_help() -> None:
    """Print concise, grouped help for the forge command."""
    if not RICH_AVAILABLE:
        return False

    console = Console()
    console.print()

    # Header + usage
    console.print("  [bold bright_cyan]fluid forge[/bold bright_cyan] [dim]— AI-powered data product creation[/dim]")
    console.print()
    console.print("  [bold]USAGE[/bold]   [bright_cyan]fluid forge[/bright_cyan] [dim][OPTIONS][/dim]")
    console.print()

    def _group_table(title: str, rows: list) -> None:
        console.print(f"  [bold bright_yellow]{title}[/bold bright_yellow]")
        tbl = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
        tbl.add_column(style="yellow", min_width=28, max_width=32)
        tbl.add_column(style="bright_white")
        for opt, desc in rows:
            tbl.add_row(f"    {opt}", desc)
        console.print(tbl)
        console.print()

    _group_table("Project", [
        ("--target-dir, -d DIR", "Target directory for project creation"),
        ("--provider, -p NAME", "Infrastructure provider"),
        ("--domain NAME", "Domain hint (finance, healthcare, retail, telco)"),
        ("--blank", "Empty contract without AI (no LLM needed)"),
        ("--dry-run", "Preview without creating files"),
        ("--non-interactive", "Use defaults without prompting"),
        ("--context VALUE", "Additional AI context (JSON string or file path)"),
    ])

    _group_table("AI Config", [
        ("--llm-provider NAME", "LLM provider (openai, anthropic, claude, gemini, ollama)"),
        ("--llm-model NAME", "Model identifier"),
        ("--llm-endpoint URL", "HTTP endpoint override"),
    ])

    _group_table("Discovery", [
        ("--discover", "Inspect local files before generation (default)"),
        ("--no-discover", "Skip local discovery"),
        ("--discovery-path PATH", "Additional path to scan"),
    ])

    _group_table("Memory", [
        ("--memory", "Load copilot memory (default)"),
        ("--no-memory", "Skip copilot memory for this run"),
        ("--save-memory", "Persist memory after successful run"),
        ("--show-memory", "Show memory summary and exit"),
        ("--reset-memory", "Delete memory file and exit"),
    ])

    # Examples
    console.print("  [bold bright_green]Examples[/bold bright_green]")
    examples = [
        ("fluid forge", "AI copilot (interactive)"),
        ("fluid forge --provider gcp", "Target GCP"),
        ("fluid forge --domain finance", "Finance domain expertise"),
        ("fluid forge --llm-provider ollama --llm-model llama3.1", "Use local Ollama model"),
        ("fluid forge --blank --target-dir ./out", "Empty scaffold"),
    ]
    for cmd, desc in examples:
        console.print(f"    [bright_cyan]{cmd}[/bright_cyan]  [dim]{desc}[/dim]")
    console.print()

    return True


# ── Enrichment data for bare commands ────────────────────────────────────
# Keyed by command name.  Values are (description, examples_epilog).
# Only needed for commands whose register() doesn't set these on the parser.
_COMMAND_ENRICHMENT: dict[str, tuple[str, str]] = {
    "init": (
        "Create a new FLUID project — quickstart, AI-designed, template, or empty.",
        (
            "  fluid init my-project                             Interactive (AI-assisted)\n"
            "  fluid init my-project --quickstart                Scaffold customer-360 (run with fluid apply)\n"
            "  fluid init my-project --template ml-features      Start from a template\n"
            "  fluid init --list-templates                       Browse available templates\n"
            "  fluid init my-project --provider snowflake        Target a cloud provider\n"
            "  [dim](Migrating from dbt/Terraform? See 'fluid import'.)[/dim]"
        ),
    ),
    "import": (
        "Import an existing dbt / Terraform / SQL project and generate FLUID "
        "contracts from it. This is the migration path to FLUID.",
        (
            "  fluid import                                Scan current dir\n"
            "  fluid import --dir ./legacy-dbt             Scan a specific dir\n"
            "  fluid import --provider snowflake           Target Snowflake bindings\n"
            "  fluid import --yes                          Skip confirmation prompt"
        ),
    ),
    "forge": (
        "Create a new data product with AI Copilot — interactive interview and "
        "contract generation. Use --blank to skip AI entirely.",
        (
            "  fluid forge                                       Interactive AI interview\n"
            "  fluid forge --blank                               Empty contract, no AI\n"
            "  fluid forge --provider snowflake                  Target a specific cloud\n"
            "  fluid forge --llm-provider gemini                 Use a specific LLM\n"
            "  fluid forge --reset-memory                        Clear project memory\n"
            "  fluid forge --non-interactive --context ctx.json  CI usage"
        ),
    ),
    "demo": (
        "Run a zero-setup demo — scaffold and execute a working customer-360 "
        "example locally with DuckDB. No API key required.",
        (
            "  fluid demo                   Create ./fluid-demo/ and run it\n"
            "  fluid demo my-sample         Use a custom directory name\n"
            "  fluid demo --dry-run         Preview without creating files\n"
            "  fluid demo --no-run          Scaffold only, skip the pipeline"
        ),
    ),
    "apply": (
        "Execute a FLUID contract end-to-end: provision, transform, govern, deploy.",
        "",  # keep existing epilog
    ),
    "plan": (
        "Generate an execution plan showing every action before you commit.",
        (
            "  fluid plan contract.fluid.yaml\n"
            "  fluid plan contract.fluid.yaml --html report.html\n"
            "  fluid plan contract.fluid.yaml --env staging --verbose"
        ),
    ),
    "verify": (
        "Confirm that deployed resources match the contract specification.",
        "",  # keep existing epilog
    ),
    "export": (
        "Export a FLUID contract as executable orchestration code (Airflow, Dagster, Prefect).",
        "",
    ),
    "market": (
        "Discover and search data products across enterprise catalogs and marketplaces.",
        "",  # keep existing epilog
    ),
    "generate-airflow": (
        "Generate an Airflow DAG from a FLUID contract. (Deprecated — use fluid export instead.)",
        "",  # keep existing epilog
    ),
    "execute": (
        "Run build jobs defined in a FLUID contract's execution configuration.",
        "",  # keep existing epilog
    ),
    "product-add": (
        "Append a source, exposure, or data quality check to an existing FLUID contract.",
        (
            "  fluid product-add contract.fluid.yaml source --id raw_events --type table\n"
            "  fluid product-add contract.fluid.yaml exposure --id public_api --location /api/v1\n"
            "  fluid product-add contract.fluid.yaml dq --id freshness_check --type freshness"
        ),
    ),
    "odps-bitol": (
        "Work with ODPS-Bitol format for Entropy Data marketplace integration.",
        "",  # keep existing epilog
    ),
    "validate": (
        "Validate a FLUID contract against schemas, provider rules, and best practices.",
        (
            "  fluid validate contract.fluid.yaml\n"
            "  fluid validate contract.fluid.yaml --env prod\n"
            "  fluid validate contract.fluid.yaml --strict --format json"
        ),
    ),
    "scaffold-ci": (
        "Generate a ready-to-use CI/CD pipeline configuration for GitLab or GitHub Actions.",
        (
            "  fluid scaffold-ci contract.fluid.yaml --system github\n"
            "  fluid scaffold-ci contract.fluid.yaml --system gitlab --out .gitlab-ci.yml"
        ),
    ),
    "export-opds": (
        "Export a FLUID contract to OPDS (Open Data Product Specification) JSON.",
        (
            "  fluid export-opds contract.fluid.yaml\n"
            "  fluid export-opds contract.fluid.yaml --out my-product.opds.json\n"
            "  fluid export-opds contract.fluid.yaml --env prod --out prod-product.json"
        ),
    ),
    "contract-tests": (
        "Run schema compatibility and consumer-impact tests against a contract baseline.",
        (
            "  fluid contract-tests contract.fluid.yaml\n"
            "  fluid contract-tests contract.fluid.yaml --baseline schema-v1.json\n"
            "  fluid contract-tests contract.fluid.yaml --env staging"
        ),
    ),
    "test": (
        "Test a contract against live data — schema checks, quality rules, and SLAs.",
        (
            "  fluid test contract.fluid.yaml\n"
            "  fluid test contract.fluid.yaml --output json\n"
            "  fluid test contract.fluid.yaml --strict --server my-account.snowflakecomputing.com\n"
            "  fluid test contract.fluid.yaml --output junit --output-file results.xml"
        ),
    ),
    "preview": (
        "Validate → Plan → Visualize in one step — without applying any changes.",
        (
            "  fluid preview contract.fluid.yaml\n"
            "  fluid preview contract.fluid.yaml --html report.html\n"
            "  fluid preview contract.fluid.yaml --env dev --out plan.json"
        ),
    ),
    "viz-plan": (
        "Render an interactive HTML visualization of an execution plan.",
        (
            "  fluid viz-plan runtime/plan.json\n"
            "  fluid viz-plan runtime/plan.json --out pipeline.html"
        ),
    ),
    "policy-apply": (
        "Apply compiled IAM / access-policy bindings to the target provider.",
        (
            "  fluid policy-apply runtime/policy/bindings.json\n"
            "  fluid policy-apply bindings.json --mode check   # dry-run\n"
            "  fluid policy-apply bindings.json --mode enforce  # live"
        ),
    ),
    "policy-compile": (
        "Compile the contract's accessPolicy section into provider-native IAM bindings.",
        (
            "  fluid policy-compile contract.fluid.yaml\n"
            "  fluid policy-compile contract.fluid.yaml --env prod\n"
            "  fluid policy-compile contract.fluid.yaml --out runtime/policy/bindings.json"
        ),
    ),
    "scaffold-composer": (
        "Generate a Google Cloud Composer DAG from a FLUID contract.",
        (
            "  fluid scaffold-composer contract.fluid.yaml\n"
            "  fluid scaffold-composer contract.fluid.yaml --out-dir dags/\n"
            "  fluid scaffold-composer contract.fluid.yaml --env prod --out-dir dags/"
        ),
    ),
    "product-new": (
        "Bootstrap a new data-product skeleton with folder structure and starter contract.",
        (
            "  fluid product-new --id gold.customer360_v1\n"
            "  fluid product-new --id silver.events_v2 --out-dir ./data-products/"
        ),
    ),
    "docs": (
        "Auto-generate static documentation site from your data product contracts.",
        (
            "  fluid docs\n"
            "  fluid docs --src ./contracts --out ./site\n"
            "  fluid docs --src /data-products --out /var/www/docs"
        ),
    ),
    "providers": (
        "List all discoverable infrastructure providers and their capabilities.",
        ("  fluid providers"),
    ),
    "config": (
        "Get or set default provider, project, and region for the current workspace.",
        (
            "  fluid config list\n"
            "  fluid config set provider gcp\n"
            "  fluid config set project my-gcp-project\n"
            "  fluid config set region us-central1\n"
            "  fluid config get provider"
        ),
    ),
    "context": (
        "Deprecated: use 'fluid config' instead. Get or set defaults.",
        (
            "  fluid config list\n"
            "  fluid config set provider gcp"
        ),
    ),
    "auth": (
        "Manage authentication credentials for cloud providers (GCP, AWS, Azure, Snowflake, Databricks).",
        (
            "  fluid auth login gcp\n"
            "  fluid auth status\n"
            "  fluid auth logout aws\n"
            "  fluid auth doctor\n"
            "  fluid auth list"
        ),
    ),
    "marketplace": (
        "Search, browse, and instantiate blueprints from the FLUID marketplace.",
        (
            "  fluid marketplace search analytics\n"
            "  fluid marketplace info customer-360\n"
            "  fluid marketplace instantiate customer-360 --out-dir ./my-project\n"
            "  fluid marketplace categories"
        ),
    ),
    "ide": (
        "IDE integration tools — editor setup, language server, shell completions.",
        (
            "  fluid ide setup                    # configure VS Code / JetBrains\n"
            "  fluid ide completion               # install shell tab-completion\n"
            "  fluid ide lsp start                # start language server\n"
            "  fluid ide validate contract.yaml   # per-file validation"
        ),
    ),
    "datamesh-manager": (
        "Publish data products to Entropy Data / Data Mesh Manager. Supports "
        "data products, data contracts, teams, and the full Entropy Data API.",
        (
            "  fluid datamesh-manager publish contract.fluid.yaml\n"
            "  fluid dmm publish contract.yaml --dry-run\n"
            "  fluid dmm publish contract.yaml --with-contract\n"
            "  fluid dmm list\n"
            "  fluid dmm teams"
        ),
    ),
    "diff": (
        "Detect configuration drift by comparing contract (desired state) with actual deployed resources.",
        (
            "  fluid diff contract.fluid.yaml\n"
            "  fluid diff contract.fluid.yaml --env prod\n"
            "  fluid diff contract.fluid.yaml --exit-on-drift   # CI/CD gate\n"
            "  fluid diff contract.fluid.yaml --out drift.json"
        ),
    ),
    "doctor": (
        "Run built-in health checks, with optional extended workspace diagnostics.",
        (
            "  fluid doctor\n"
            "  fluid doctor --verbose\n"
            "  fluid doctor --extended\n"
            "  fluid doctor --features-only"
        ),
    ),
    "admin": (
        "System administration — diagnostics, tests, templates, pipeline scaffolds, registries.",
        (
            "  fluid admin status\n"
            "  fluid admin diagnostics\n"
            "  fluid admin test\n"
            "  fluid admin test-cli\n"
            "  fluid admin templates\n"
            "  fluid admin registry\n"
            "  fluid admin pipeline"
        ),
    ),
    "version": (
        "Display FLUID CLI version, supported spec versions, and system environment info.",
        (
            "  fluid version\n"
            "  fluid version --verbose\n"
            "  fluid version --format json\n"
            "  fluid version --short"
        ),
    ),
    "generate-pipeline": (
        "Create dynamic DataOps CI/CD pipeline configs for GitHub Actions, GitLab CI, Jenkins, and more.",
        (
            "  fluid generate-pipeline --provider github_actions\n"
            "  fluid generate-pipeline --provider gitlab_ci --complexity enterprise\n"
            "  fluid generate-pipeline --interactive\n"
            "  fluid generate-pipeline --provider jenkins --enable-approvals --enable-security-scan"
        ),
    ),
}


def print_command_help(parser: argparse.ArgumentParser, command_name: str) -> None:
    """Print beautiful Rich-formatted help for any individual command."""
    if not RICH_AVAILABLE:
        parser.print_help()
        return

    # Special handling for forge (has its own bespoke layout)
    if command_name == "forge":
        if print_forge_help():
            return

    console = Console()
    W = console.width or 80
    bar = "[dim bright_cyan]" + "━" * (W - 4) + "[/dim bright_cyan]"

    # ── locate the subparser ─────────────────────────────────────────
    subparser = None
    for action in parser._subparsers._actions:
        if isinstance(action, argparse._SubParsersAction):
            if command_name in action.choices:
                subparser = action.choices[command_name]
                break
    if subparser is None:
        parser.print_help()
        return

    # ── resolve description & epilog (enrichment overrides blanks) ──
    desc = subparser.description or ""
    epilog = subparser.epilog or ""
    enrichment = _COMMAND_ENRICHMENT.get(command_name)
    if enrichment:
        if not desc.strip():
            desc = enrichment[0]
        if not epilog.strip():
            epilog = enrichment[1]
    # Compact multi-line descriptions to a single block
    if desc:
        desc = " ".join(desc.split())
        # If description is very long, use just the first sentence
        if len(desc) > 120:
            first_dot = desc.find(". ")
            if first_dot > 0 and first_dot < 120:
                desc = desc[: first_dot + 1]
            else:
                desc = desc[:117] + "..."

    # ── Header ───────────────────────────────────────────────────────
    console.print()
    console.print(f"  {bar}")
    console.print(f"  [bold bright_cyan]🌊 fluid {command_name}[/bold bright_cyan]")
    if desc:
        console.print(f"  [bright_white]{desc}[/bright_white]")
    console.print(f"  {bar}")
    console.print()

    # ── Usage line ───────────────────────────────────────────────────
    # Build a compact usage from the formatter
    usage_parts = [f"[bright_cyan]fluid {command_name}[/bright_cyan]"]
    has_subcommands = False
    sub_choices: list[tuple[str, str]] = []

    for act in subparser._subparsers._actions if subparser._subparsers else []:
        if isinstance(act, argparse._SubParsersAction):
            has_subcommands = True
            # Build help map from _choices_actions (stores the help text)
            help_map: dict[str, str] = {}
            for ca in getattr(act, "_choices_actions", []):
                help_map[ca.dest] = ca.help or ""
            for name in act.choices:
                sub_choices.append((name, help_map.get(name, "")))
            usage_parts.append(f"[bright_green]<{'|'.join(act.choices)}>[/bright_green]")
            break

    # Positional args (except subcommands)
    for grp in subparser._action_groups:
        for act in grp._group_actions:
            if isinstance(act, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            if not act.option_strings:  # positional
                meta = act.metavar or act.dest.upper()
                usage_parts.append(f"[bright_white]{meta}[/bright_white]")

    usage_parts.append("[dim][options][/dim]")
    console.print("  [bold]USAGE[/bold]   " + " ".join(usage_parts))
    console.print()

    # ── Subcommands (if present) ─────────────────────────────────────
    if has_subcommands and sub_choices:
        console.print("  [bold bright_green]▸ Commands[/bold bright_green]")
        tbl = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
        tbl.add_column(style="bright_green bold", min_width=26, max_width=26)
        tbl.add_column(style="bright_white")
        for name, hlp in sub_choices:
            tbl.add_row(f"    {name}", hlp)
        console.print(tbl)
        console.print()

    # ── Arguments & Options ──────────────────────────────────────────
    # Global args inherited from the parent parser — hide from subcommand help
    _GLOBAL_DESTS = frozenset({
        "log_level", "log_file", "project", "region", "config_dir",
        "no_color", "version", "profile", "health_check", "stats",
        "safe_mode", "debug", "cmd",
    })
    # Groups that belong to the parent parser, not the subcommand
    _GLOBAL_GROUPS = frozenset({"production & monitoring"})

    for group in subparser._action_groups:
        title_raw = (group.title or "").strip()
        if title_raw.lower() in _GLOBAL_GROUPS:
            continue

        actions = [
            a
            for a in group._group_actions
            if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))
            and a.dest not in _GLOBAL_DESTS
        ]
        if not actions:
            continue

        # Section title
        low = title_raw.lower()
        if "positional" in low:
            label = "▸ Arguments"
            color = "bright_cyan"
        elif low in ("options", "optional arguments"):
            label = "▸ Options"
            color = "yellow"
        else:
            label = f"▸ {title_raw}"
            color = "bright_yellow"

        console.print(f"  [bold {color}]{label}[/bold {color}]")

        tbl = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
        tbl.add_column(style=f"{color}", min_width=26, max_width=32)
        tbl.add_column(style="bright_white")

        for act in actions:
            # Skip suppressed (deprecated) options.
            if act.help == argparse.SUPPRESS:
                continue

            # Option / positional string
            if act.option_strings:
                opt = ", ".join(act.option_strings)
                if act.metavar:
                    opt += f" [dim]{act.metavar}[/dim]"
                elif act.type and act.type is not bool:
                    opt += f" [dim]{act.dest.upper()}[/dim]"
            else:
                opt = act.metavar or act.dest

            # Help text
            hlp = act.help or ""
            if act.choices and not isinstance(act, argparse._SubParsersAction):
                # Only append choices when they aren't already in the help text
                if not any(str(c) in hlp for c in list(act.choices)[:2]):
                    hlp += "  [dim](" + " · ".join(str(c) for c in act.choices) + ")[/dim]"
            if act.default not in (None, argparse.SUPPRESS, False):
                # Skip if help already mentions this default
                def_str = str(act.default)
                if f"default: {def_str}" not in hlp and f"default:{def_str}" not in hlp:
                    hlp += f"  [dim](default: {def_str})[/dim]"

            tbl.add_row(f"    {opt}", hlp)

        console.print(tbl)
        console.print()

    # ── Examples ─────────────────────────────────────────────────────
    if epilog and epilog.strip():
        lines = epilog.strip().splitlines()
        formatted: list[str] = []
        in_continuation = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                in_continuation = False
                formatted.append("")
                continue
            # Detect start/continuation of a backslash-continued command
            if stripped.endswith("\\"):
                clean = stripped.rstrip("\\").rstrip()
                formatted.append(f"  [bright_cyan]{clean} \\\\[/bright_cyan]")
                in_continuation = True
            elif in_continuation:
                formatted.append(f"    [bright_cyan]{stripped}[/bright_cyan]")
                if not stripped.endswith("\\"):
                    in_continuation = False
            elif stripped.startswith("#"):
                formatted.append(f"  [dim italic]{stripped}[/dim italic]")
            elif stripped.startswith("fluid ") or stripped.startswith("viz-"):
                formatted.append(f"  [bright_cyan]{stripped}[/bright_cyan]")
            else:
                formatted.append(f"  {stripped}")
        console.print("  [bold bright_magenta]▸ Examples[/bold bright_magenta]")
        console.print("\n".join(formatted))
        console.print()

    # ── Footer ───────────────────────────────────────────────────────
    console.print(
        f"  [dim]Run[/dim] [bright_cyan]fluid --help[/bright_cyan] "
        f"[dim]for all commands  │  [/dim]"
        f"[bright_cyan]fluid {command_name} -h[/bright_cyan] [dim](raw argparse)[/dim]"
    )
    console.print()
