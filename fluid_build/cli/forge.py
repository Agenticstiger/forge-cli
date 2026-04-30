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

"""Public Forge CLI entrypoint — AI-powered data product creation.

After the UX redesign, ``fluid forge`` is the **repeatable** command for
creating new data products inside an existing project.  It defaults to the
AI Copilot interview and has a ``--blank`` escape hatch for users who want
a bare contract scaffold without AI.

First-time project setup lives in ``fluid init``.
"""

from __future__ import annotations

import argparse
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import product_forge_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_agents import DOMAIN_AGENTS
from fluid_build.cli.forge_banner import print_v2_banner
from fluid_build.cli.forge_context import (
    get_cli_arg as _get_cli_arg,
)
from fluid_build.cli.forge_context import (
    get_target_directory as _get_target_dir,
)
from fluid_build.cli.forge_context import (
    handle_memory_management as _handle_memory,
)
from fluid_build.cli.forge_context import (
    load_context as _load_ctx,
)
from fluid_build.cli.forge_context import (
    resolve_memory_store as _resolve_store,
)
from fluid_build.cli.forge_copilot_agent import (
    AIAgent,
    CopilotAgentBase,
    recommend_template_for_use_case,
)
from fluid_build.cli.forge_copilot_interview import build_interview_summary_from_context
from fluid_build.cli.forge_copilot_llm_providers import (
    get_cumulative_prompt_cache_metrics,
    reset_token_usage,
)
from fluid_build.cli.forge_copilot_memory import (
    CopilotMemoryStore,
    resolve_copilot_memory_root,
    summarize_copilot_memory,
)
from fluid_build.cli.forge_copilot_runtime import (
    CopilotGenerationError,
    CopilotGenerationResult,
    build_capability_matrix,
    discover_local_context,
    generate_copilot_artifacts,
    normalize_provider_name,
    normalize_template_name,
    resolve_llm_config,
)
from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context
from fluid_build.cli.forge_data_model import register_forge_subcommand
from fluid_build.cli.forge_dialogs import ask_confirmation, ask_dialog_question
from fluid_build.cli.forge_modes import (
    _scaffold_ci_pipeline,
)
from fluid_build.cli.forge_modes import (
    run_ai_copilot_mode as _run_copilot,
)
from fluid_build.cli.forge_modes import (
    run_guided_mode as _run_guided,
)
from fluid_build.cli.forge_ui import print_welcome_panel
from fluid_build.cli.next_steps import print_next_steps

try:
    from rich.console import Console
    from rich.panel import Panel

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through non-Rich fallbacks
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

from ._common import CLIError

COMMAND = "forge"
LOG = logging.getLogger("fluid.cli.forge")


class ForgeError(CLIError):
    """Base exception for Forge command errors.

    Two positional-argument shapes are accepted so callers using the
    pre-925668a ``ForgeError(exit_code, message)`` form keep working
    alongside the modern ``ForgeError(message, exit_code=...)`` form.
    The keyword-only ``exit_code`` always wins when both are supplied.
    """

    def __init__(
        self,
        *args: Any,
        event: str = "forge_error",
        context: Optional[Dict[str, Any]] = None,
        exit_code: int = 1,
    ):
        if len(args) == 1:
            message = str(args[0])
        elif len(args) == 2:
            # Legacy positional shape ``ForgeError(exit_code, message)``.
            # Accept it so older callers + tests that haven't migrated
            # don't blow up with ``TypeError: takes 2 positional args
            # but 3 were given``.  Keyword ``exit_code`` overrides the
            # positional one when both are supplied.
            exit_code = exit_code if exit_code != 1 else int(args[0])
            message = str(args[1])
        else:
            raise TypeError(
                f"{type(self).__name__} expects 1 (message) or 2 (exit_code, message) "
                f"positional arguments; got {len(args)}"
            )
        payload = {"error": message, **(context or {})}
        super().__init__(exit_code, event, payload)
        self.message = message

    def __str__(self) -> str:
        # ``CLIError`` passes the ``event`` string to ``Exception.__init__``,
        # which makes ``str(err)`` return the event key (``"forge_error"``).
        # Override so callers / tests that look at the rendered string see
        # the operator-facing message instead.
        return self.message


class InvalidProjectNameError(ForgeError):
    """Invalid project name format."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"Invalid project name '{name}': {reason}")


class ProjectGenerationError(ForgeError):
    """Project generation failed."""


class ContextValidationError(ForgeError):
    """Context file validation failed."""


class ForgeMode(Enum):
    """Forge creation modes."""

    AI_COPILOT = "copilot"
    BLANK = "blank"


class CopilotAgent(CopilotAgentBase):
    """Public copilot agent wired to the compatibility aliases in this module."""

    def _resolve_llm_config_dependency(self, options):
        return resolve_llm_config(options)

    def _discover_local_context_dependency(self, options):
        return discover_local_context(
            getattr(options, "discovery_path", None),
            discover=getattr(options, "discover", True),
            workspace_root=Path.cwd(),
            logger=LOG,
        )

    def _build_capability_matrix_dependency(self):
        return build_capability_matrix()

    def _generate_copilot_artifacts_dependency(
        self,
        context: Dict[str, Any],
        *,
        llm_config: Any,
        discovery_report: Any,
        project_memory: Any,
        team_memory: Any = None,
        capability_matrix: Any,
    ) -> CopilotGenerationResult:
        return generate_copilot_artifacts(
            context,
            llm_config=llm_config,
            discovery_report=discovery_report,
            project_memory=project_memory,
            team_memory=team_memory,
            capability_matrix=capability_matrix,
            logger=LOG,
        )

    def _make_memory_store_dependency(self, project_root: Path) -> CopilotMemoryStore:
        return CopilotMemoryStore(project_root, logger=LOG)

    def _ask_confirmation_dependency(self, prompt: str, preview: str) -> bool:
        if self.console and RICH_AVAILABLE:
            return ask_confirmation(
                self.console,
                prompt,
                default=False,
                title="🧠 Save Project Memory?",
                preview=preview,
                border_style="cyan",
            )
        return super()._ask_confirmation_dependency(prompt, preview)


DOMAIN_AGENTS_AVAILABLE = bool(DOMAIN_AGENTS)


def _build_ai_agents():
    agents = {"copilot": CopilotAgent}
    if DOMAIN_AGENTS_AVAILABLE:
        agents.update(DOMAIN_AGENTS)
    return agents


AI_AGENTS = _build_ai_agents()


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction):
    """Register the Forge command — AI-powered data product creation."""
    parser = subparsers.add_parser(
        COMMAND,
        help="🔨 Create a new data product with AI Copilot",
        add_help=False,
    )
    parser.add_argument("--help", "-h", action="store_true", help="Show this help message")
    forge_subparsers = parser.add_subparsers(dest="forge_subcommand")
    register_forge_subcommand(forge_subparsers)

    # --- Primary flags ---
    parser.add_argument(
        "--blank",
        action="store_true",
        help="Scaffold an empty contract without AI (no LLM needed)",
    )
    parser.add_argument("--target-dir", "-d", help="Target directory for project creation")
    parser.add_argument("--provider", "-p", help="Infrastructure provider to use")
    try:
        from fluid_build.cli.forge_agents import get_all_domain_names

        domain_names = ", ".join(get_all_domain_names())
    except Exception:  # noqa: BLE001
        domain_names = "finance, healthcare, retail, telco"
    parser.add_argument(
        "--domain",
        help=f"Domain expertise agent ({domain_names}). "
        "Custom: drop a YAML in .fluid/agents/ — see docs at https://fluid-build.dev/docs/forge/agents",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults without prompting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without generating files",
    )
    parser.add_argument(
        "--context",
        help='Additional context as JSON or file path (e.g., \'{"provider":"gcp","domain":"retail"}\')',
    )

    # --- LLM flags ---
    parser.add_argument(
        "--llm-provider",
        choices=["openai", "anthropic", "claude", "gemini", "ollama"],
        help="LLM provider for copilot",
    )
    parser.add_argument("--llm-model", help="Model identifier for copilot")
    parser.add_argument(
        "--llm-routing-model",
        help="Fast/cheap model for interview clarification and AI self-evaluation",
    )
    parser.add_argument(
        "--llm-routing-endpoint",
        help="HTTP endpoint override for the routing model",
    )
    parser.add_argument(
        "--llm-endpoint",
        help="Exact HTTP endpoint override for the selected LLM adapter",
    )
    parser.add_argument(
        "--tiered",
        action="store_true",
        help="Use provider-local model tiers: deep for modeling, fast for routing",
    )
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Fail if the configured LLM cannot run; do not fall back to non-AI paths.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Bypass the LLM response cache. Useful when iterating on prompts "
            "or comparing model output. (Same flag is on `fluid forge data-model`.)"
        ),
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help=(
            "Force temperature=0 + cache off + tiering off + audit-trail "
            "metadata. Use for byte-stable proof-of-determinism reports."
        ),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Force the heuristic-only forge path; never call any LLM. "
            "Equivalent to `--blank` for the full no-AI experience."
        ),
    )

    # --- Discovery ---
    parser.add_argument(
        "--discover",
        dest="discover",
        action="store_true",
        default=True,
        help="Inspect local files and manifests before generation",
    )
    parser.add_argument(
        "--no-discover",
        dest="discover",
        action="store_false",
        help="Skip local discovery and rely only on explicit context",
    )
    parser.add_argument(
        "--discovery-path",
        help="Additional path to scan for metadata-only discovery",
    )

    # --- Memory ---
    parser.add_argument(
        "--memory",
        dest="memory",
        action="store_true",
        default=True,
        help="Load project-scoped copilot memory",
    )
    parser.add_argument(
        "--no-memory",
        dest="memory",
        action="store_false",
        help="Do not load project-scoped copilot memory for this run",
    )
    parser.add_argument(
        "--save-memory",
        action="store_true",
        help="Persist copilot memory after a successful non-interactive run",
    )
    parser.add_argument(
        "--show-memory",
        action="store_true",
        help="Show the current copilot memory summary and exit",
    )
    parser.add_argument(
        "--reset-memory",
        action="store_true",
        help="Delete the copilot memory file and exit",
    )

    # --- CI/CD auto-scaffolding (post-generation hook) ---
    # Listed values are kept in sync with PipelineProvider in
    # fluid_build/forge/core/pipeline_templates.py plus the control sentinels
    # ``none`` (skip) and ``ask`` (force interactive menu even with memory).
    parser.add_argument(
        "--ci",
        choices=[
            "github_actions",
            "gitlab_ci",
            "azure_devops",
            "jenkins",
            "bitbucket",
            "circleci",
            "circle_ci",
            "tekton",
            "none",
            "ask",
        ],
        default=None,
        help="Auto-generate a CI/CD pipeline after scaffolding (e.g. 'github_actions'); 'none' skips, 'ask' always prompts",
    )
    parser.add_argument(
        "--ci-complexity",
        choices=["basic", "standard", "advanced", "enterprise"],
        default="standard",
        help="Complexity of the auto-generated CI pipeline (default: standard)",
    )
    parser.add_argument(
        "--no-ci",
        action="store_true",
        help="Skip CI/CD pipeline auto-scaffolding (equivalent to --ci none)",
    )

    # --- Scaffolding opt-in (slice UX-H) ---
    # Default: minimal-layout forge run. Only contract.fluid.yaml + the
    # .fluid/ forge receipt land on disk (plus optional CI files). Pass
    # --scaffold <template> to opt back into the legacy full-project tree
    # (extracts/, loads/, transforms/, config/, docs/, tests/, scripts/,
    # requirements.txt, .env.example, README.md, …) produced by the
    # ForgeEngine + opinionated templates.
    parser.add_argument(
        "--scaffold",
        metavar="TEMPLATE",
        default=None,
        help=(
            "Opt into the full ForgeEngine scaffold using one of the built-in "
            "templates (e.g. 'etl_pipeline', 'analytics', 'ml_pipeline', "
            "'streaming', 'starter'). Default: minimal — only contract.fluid.yaml "
            "and .fluid/forge-receipt.json are written."
        ),
    )

    # --- Agent loop opt-in (slice UX-K) ---
    parser.add_argument(
        "--agent-loop",
        action="store_true",
        default=False,
        help=(
            "Use the multi-turn agent loop instead of the single-shot prompt. "
            "The LLM discovers your workspace, picks a template, builds and "
            "validates the contract iteratively via tool calls. "
            "Requires a tool-use-capable model (gpt-4.1-mini, claude-sonnet-4-6, gemini-2.5-flash)."
        ),
    )

    # --- Transformation engine generation ---
    parser.add_argument(
        "--no-generate",
        action="store_true",
        default=False,
        help="Skip transformation engine artifact generation (dbt project, SQL scripts, etc.)",
    )

    # --- Fragment layout control ---
    fragment_group = parser.add_mutually_exclusive_group()
    fragment_group.add_argument(
        "--fragments",
        action="store_true",
        default=False,
        help=(
            "Force fragment-first layout — split the generated contract into "
            "composable files under fragments/ with $ref pointers."
        ),
    )
    fragment_group.add_argument(
        "--no-fragments",
        action="store_true",
        default=False,
        help="Force flat single-file layout (skip automatic fragment splitting).",
    )
    parser.set_defaults(func=run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_target_directory(args, default_name: str = "my-fluid-project") -> Path:
    return _get_target_dir(args, default_name)


def get_cli_arg(args: Any, name: str, default: Any = None) -> Any:
    return _get_cli_arg(args, name, default)


def resolve_memory_store(args, logger: logging.Logger) -> CopilotMemoryStore:
    return _resolve_store(args, logger, memory_store_class=CopilotMemoryStore)


def handle_memory_management(args, logger: logging.Logger) -> int:
    return _handle_memory(
        args,
        logger,
        memory_store_class=CopilotMemoryStore,
        console_factory=Console if RICH_AVAILABLE else None,
    )


# ---------------------------------------------------------------------------
# Blank mode — scaffold empty contract, no AI
# ---------------------------------------------------------------------------


def _run_blank_mode(args: Any, logger: logging.Logger) -> int:
    """Create a minimal empty contract scaffold without AI."""
    from fluid_build.cli.forge_contract_factory import (
        build_minimal_contract,
        create_and_validate_contract,
    )

    console = Console() if RICH_AVAILABLE else None
    target_dir = get_target_directory(args, "my-data-product")

    if get_cli_arg(args, "dry_run", False):
        if console:
            console.print(f"[dim]DRY RUN: Would create empty contract in {target_dir}[/dim]")
        return 0

    contract_path = target_dir / "contract.fluid.yaml"
    if contract_path.exists():
        if console:
            console.print(
                f"[yellow]contract.fluid.yaml already exists in {target_dir}[/yellow]\n"
                "[dim]Delete it first or use a different --target-dir.[/dim]"
            )
        return 1

    contract = build_minimal_contract()
    result_path = create_and_validate_contract(contract, target_dir, logger, console)
    if not result_path:
        return 1

    _scaffold_ci_pipeline(
        args,
        target_dir,
        {},
        console,
        ask_dialog_question_fn=ask_dialog_question,
        get_cli_arg_fn=get_cli_arg,
        dry_run=False,
    )

    _print_next_steps(console, target_dir, result_path)
    return 0


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

_DOCS_URL = "https://fluid-build.dev/docs/contracts"


def _print_forge_next_steps(console: Any, args: Any, scan_root: Path) -> None:
    """Print next steps, auto-detecting fragment-first layout."""
    try:
        fragments_dir = scan_root / "fragments"
        if fragments_dir.is_dir() and any(fragments_dir.rglob("*.yaml")):
            print_next_steps("forge-fragments", console=console, args=args)
            _print_split_bundle_hint(console)
            return
    except Exception:  # noqa: BLE001 — detection is best-effort
        pass
    print_next_steps("forge", console=console, args=args)
    _print_split_bundle_hint(console)


def _print_split_bundle_hint(console: Any) -> None:
    """Show hint about split/bundle after successful forge."""
    hint_text = (
        "Tip: Use 'fluid split' to break this into modular fragments.\n"
        "     FLUID auto-bundles fragments when you run validate, plan, or apply."
    )
    try:
        if console is not None and RICH_AVAILABLE:
            console.print(f"\n[dim]{hint_text}[/dim]")
        else:
            cprint(f"\n{hint_text}")
    except Exception:
        pass


def _print_next_steps(console: Any, target_dir: Path, contract_path: Path) -> None:
    """Show post-creation next steps with doc link."""
    steps = (
        f"[green]Created contract at:[/green] {contract_path}\n\n"
        "Next steps:\n"
        f"  1. cd {target_dir}\n"
        "  2. Edit contract.fluid.yaml\n"
        "  3. fluid validate contract.fluid.yaml\n"
        "  4. fluid plan contract.fluid.yaml --out runtime/plan.json\n"
        "  5. fluid apply runtime/plan.json\n\n"
        "[dim]Tip: Run 'fluid forge' (AI mode) to auto-generate transformation\n"
        "logic from your sample data, or 'fluid generate' after editing\n"
        "the contract manually.[/dim]\n\n"
        f"[dim]Docs: {_DOCS_URL}[/dim]"
    )
    if console and RICH_AVAILABLE:
        console.print(Panel(steps, title="Forge Complete", border_style="green"))
    else:
        cprint(f"Created contract at {contract_path}")
        cprint(f"Docs: {_DOCS_URL}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(args, logger: logging.Logger) -> int:
    """Main entry point for ``fluid forge``."""
    console = Console() if RICH_AVAILABLE else None
    try:
        if getattr(args, "forge_subcommand", None) == "data-model":
            from fluid_build.cli.forge_data_model import run as run_data_model

            quiet = getattr(args, "quiet", False)
            if getattr(args, "data_model_action", None) == "from-intent" and (
                getattr(args, "example", None)
                or getattr(args, "schema", False)
                or getattr(args, "validate_intent", None)
            ):
                quiet = True
            print_v2_banner("forge_data_model", quiet=quiet)
            return run_data_model(args, logger)

        # --- Help ---
        if getattr(args, "help", False):
            if console:
                from .help_formatter import print_forge_help

                print_forge_help()
                return 0
            cprint(
                "Run 'fluid forge' to start the AI Copilot, or 'fluid forge --blank' for an empty contract."
            )
            return 0

        # --- Memory management shortcuts ---
        if get_cli_arg(args, "show_memory", False) or get_cli_arg(args, "reset_memory", False):
            return handle_memory_management(args, logger)

        # --- Snapshot for the forge receipt ---
        # Scan cwd before any mode runs so the diff catches every file the
        # mode handler wrote.  find_workspace_root is cheap and localised;
        # if this isn't a workspace yet, we fall back to cwd and still
        # produce a receipt.
        scan_root = Path.cwd()
        before_snapshot = snapshot_workspace(scan_root)
        reset_token_usage()

        # --- Top-level flag aliases ---
        # ``--no-llm`` and ``--deterministic`` were added at the
        # top-level forge surface so operators can opt out of AI
        # without remembering ``--blank``.  Both currently route
        # through the existing blank-mode path because the AI
        # copilot doesn't yet honour the flags directly; data-model
        # subcommands still consume them natively.  Wiring the AI
        # copilot to read them is tracked as a follow-up.
        if get_cli_arg(args, "no_llm", False) or get_cli_arg(args, "deterministic", False):
            args.blank = True

        # --- Determine effective mode ---
        is_blank = get_cli_arg(args, "blank", False)
        flow = "blank" if is_blank else "copilot"

        if is_blank:
            LOG.debug("Forge: blank mode selected")
            result = _run_blank_mode(args, logger)
            if result == 0:
                _write_forge_receipt(
                    flow=flow,
                    args=args,
                    before_snapshot=before_snapshot,
                    scan_root=scan_root,
                    logger=logger,
                )
                _print_forge_next_steps(console, args, scan_root)
            return result

        # --- Default: AI Copilot with inline LLM setup ---
        LOG.debug("Forge: copilot mode")
        if console and not get_cli_arg(args, "non_interactive", False):
            print_welcome_panel(console)

        # Check LLM readiness; load saved config or offer inline setup
        if not get_cli_arg(args, "non_interactive", False):
            from fluid_build.cli.ai_setup import run_ai_setup_inline

            # Always go through inline setup — it handles all cases:
            # 1. Config file exists with key → loads it, sets env vars, returns config
            # 2. Keyring has key → loads it, sets env vars, returns config
            # 3. Nothing found → prompts user interactively
            llm_config = run_ai_setup_inline(console)
            if llm_config:
                # Inject into args so copilot's resolve_llm_config() finds them
                args.llm_provider = llm_config.provider
                args.llm_model = llm_config.model
                args.llm_endpoint = llm_config.endpoint
                if llm_config.api_key:
                    from fluid_build.cli.ai_setup import set_session_env

                    set_session_env(llm_config.provider, llm_config.api_key)
                LOG.debug("LLM config loaded: provider=%s", llm_config.provider)
            else:
                # AI not available — fall back to guided mode
                if console:
                    proceed = ask_confirmation(
                        console,
                        "Continue with guided mode (no AI)?",
                        default=True,
                    )
                else:
                    proceed = False
                if proceed:
                    result = run_guided_mode(args, logger)
                    if result == 0:
                        _write_forge_receipt(
                            flow="guided",
                            args=args,
                            before_snapshot=before_snapshot,
                            scan_root=scan_root,
                            logger=logger,
                        )
                        _print_forge_next_steps(console, args, scan_root)
                    return result
                if console:
                    console.print(
                        "[yellow]Use 'fluid forge --blank' for a bare contract,[/yellow]\n"
                        "[yellow]or run 'fluid ai setup' to configure an LLM provider.[/yellow]"
                    )
                return 1

        result = run_ai_copilot_mode(args, logger)
        if result == 0:
            _write_forge_receipt(
                flow="copilot",
                args=args,
                before_snapshot=before_snapshot,
                scan_root=scan_root,
                logger=logger,
                provenance=getattr(args, "_copilot_provenance", None),
            )
            _print_forge_next_steps(console, args, scan_root)
        return result

    except KeyboardInterrupt:
        logger.info("Forge cancelled by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        # World-class error UX — every typed Fluid error carries
        # ``suggestions``; we surface those instead of a raw stack
        # trace. Stack trace is logged at DEBUG for triage, but the
        # operator-facing output is just the message + actionable
        # next steps.
        from fluid_build.errors import FluidError

        logger.debug("Forge command failed", exc_info=True)
        suggestions = getattr(exc, "suggestions", None) or []
        message = getattr(exc, "message", None) or str(exc)
        if isinstance(exc, FluidError):
            # Typed Fluid error — clean message + suggestions.
            if console:
                console.print(f"[red]Forge failed:[/red] {message}")
                if suggestions:
                    console.print("[bold]Next actions:[/bold]")
                    for s in suggestions:
                        console.print(f"  • {s}")
            else:
                console_error(f"Forge failed: {message}")
                if suggestions:
                    cprint("Next actions:")
                    for s in suggestions:
                        cprint(f"  - {s}")
        else:
            # Unexpected — full message; suggest doctor.
            logger.exception("Forge command failed")
            if console:
                console.print(
                    f"[red]Forge failed: {exc}[/red]\n"
                    f"[dim]Run 'fluid doctor' to diagnose, or see {_DOCS_URL}[/dim]"
                )
            else:
                console_error(f"Forge failed: {exc}")
                cprint(f"Run 'fluid doctor' to diagnose, or see {_DOCS_URL}")
        return 1


# ---------------------------------------------------------------------------
# Mode wrappers (thin delegation)
# ---------------------------------------------------------------------------


def run_ai_copilot_mode(args, logger: logging.Logger) -> int:
    return _run_copilot(
        args,
        logger,
        copilot_class=CopilotAgent,
        get_cli_arg_fn=get_cli_arg,
        load_context_fn=load_context,
        get_target_directory_fn=get_target_directory,
        context_error_cls=ContextValidationError,
        build_interview_summary_fn=build_interview_summary_from_context,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def run_guided_mode(args, logger: logging.Logger) -> int:
    """Lightweight guided prompts — no AI needed."""
    return _run_guided(
        args,
        logger,
        get_target_directory_fn=get_target_directory,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def load_context(
    context_input: str,
    console: Optional[Any] = None,
    *,
    context_error_cls: type[Exception] = ContextValidationError,
) -> Dict[str, Any]:
    return _load_ctx(
        context_input,
        console,
        context_error_cls=context_error_cls,
    )


def create_legacy_bootstrapper(target_dir: Optional[str] = None, **kwargs):
    """Create a legacy bootstrapper for backward compatibility."""
    from .forge_legacy import ForgeBootstrapper

    return ForgeBootstrapper(target_dir, **kwargs)


def _write_forge_receipt(
    *,
    flow: str,
    args,
    before_snapshot,
    scan_root: Path,
    logger: logging.Logger,
    provenance: Optional[Dict[str, Any]] = None,
) -> None:
    """Write ``<product>/.fluid/forge-receipt.json`` for this run.

    Never raises — receipt writing is best-effort post-success.  The
    forge receipt lives next to the product it describes, so we
    identify the product by diffing the workspace snapshot and scoping
    to the directory of whichever contract was just (re)written.

    If the diff turns up multiple contracts (e.g. an interactive flow
    that touched several products), the receipt is scoped to the most
    recently modified one.
    """
    try:
        after_snapshot = snapshot_workspace(scan_root)
        entries = diff_snapshots(before_snapshot, after_snapshot)
        # Ignore no-op rows — the receipt only records what changed.
        changed = [e for e in entries if e.action != "unchanged"]
        if not changed:
            return

        # Find the product directory for this run — the dir containing
        # the (new or updated) contract.fluid.yaml is the scope root for
        # the receipt.  Fall back to cwd when the flow wrote files
        # without touching any contract (shouldn't happen in practice,
        # but be defensive).
        product_root = _resolve_product_root(changed, scan_root)
        if product_root is None:
            logger.debug("forge_receipt_no_product_root")
            return

        builder = ReceiptBuilder(flow=flow, dry_run=False)
        for entry in changed:
            # Re-anchor the path to the product root so the receipt is
            # self-contained and portable across clones.
            abs_path = (scan_root / entry.path).resolve()
            try:
                rel = abs_path.relative_to(product_root.resolve())
                path_str = str(rel)
            except ValueError:
                path_str = str(abs_path)
            builder.record_entry(
                path=Path(path_str),
                action=entry.action,
                sha256=entry.sha256,
                size=entry.size,
                reason=entry.reason,
            )

        inputs: Dict[str, Any] = {
            "blank": bool(get_cli_arg(args, "blank", False)) or None,
            "non_interactive": bool(get_cli_arg(args, "non_interactive", False)) or None,
            "context": get_cli_arg(args, "context", None),
            "target_dir": get_cli_arg(args, "target_dir", None),
        }
        if provenance:
            inputs["provenance"] = provenance
        builder.set_inputs(**inputs)

        doc = builder.build_document()

        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""

        command = _format_forge_command(args, flow)
        payload = doc.to_payload()
        performance = _forge_performance_payload()
        if performance:
            payload["performance"] = performance
        payload_bytes = dump_json_with_envelope(
            payload,
            kind="ForgeReceipt",
            command=command,
            tool_version=str(tool_version),
        )

        receipt_path = product_forge_receipt_path(product_root)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(payload_bytes, encoding="utf-8")
        logger.debug("forge_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — never abort forge on receipt failure
        logger.debug("forge_receipt_write_failed", extra={"error": str(exc)})


def _forge_performance_payload() -> Dict[str, Any]:
    metrics = get_cumulative_prompt_cache_metrics()
    if metrics.get("total_tokens", 0) <= 0:
        return {}
    return {"prompt_cache": metrics}


def _resolve_product_root(changed_entries, scan_root: Path) -> Optional[Path]:
    """Return the product directory the forge receipt should live under.

    The product is identified by the `contract.fluid.yaml` file that
    appears in the diff.  When multiple contracts changed, the most
    recently modified one wins.
    """
    candidates: List[Path] = []
    for entry in changed_entries:
        if not entry.path.endswith("contract.fluid.yaml"):
            continue
        abs_path = (scan_root / entry.path).resolve()
        candidates.append(abs_path.parent)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Multiple contracts changed — pick the one whose contract file has
    # the newest mtime.  Falls back to the first candidate on stat error.
    def _mtime(parent: Path) -> float:
        try:
            return (parent / "contract.fluid.yaml").stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=_mtime)


def _format_forge_command(args, flow: str) -> str:
    """Build a short human-readable command string for the receipt."""
    parts = ["fluid forge"]
    if get_cli_arg(args, "blank", False):
        parts.append("--blank")
    if get_cli_arg(args, "non_interactive", False):
        parts.append("--non-interactive")
    if get_cli_arg(args, "target_dir", None):
        parts.append(f"--target-dir {args.target_dir}")
    return " ".join(parts)


def get_enhanced_templates():
    """Get enhanced templates for backward compatibility."""
    from ..forge.core.registry import template_registry

    legacy_templates = {}
    for template_name in template_registry.list_available():
        template = template_registry.get(template_name)
        if template:
            metadata = template.get_metadata()
            legacy_templates[template_name] = {
                "name": metadata.name,
                "description": metadata.description,
                "complexity": metadata.complexity.value,
                "provider_support": metadata.provider_support,
                "use_cases": metadata.use_cases,
                "technologies": metadata.technologies,
                "estimated_time": metadata.estimated_time,
                "tags": metadata.tags,
            }
    return legacy_templates


__all__ = [
    "AIAgent",
    "AI_AGENTS",
    "COMMAND",
    "ContextValidationError",
    "CopilotAgent",
    "ForgeError",
    "ForgeMode",
    "InvalidProjectNameError",
    "ProjectGenerationError",
    "create_legacy_bootstrapper",
    "get_cli_arg",
    "get_enhanced_templates",
    "get_target_directory",
    "handle_memory_management",
    "load_context",
    "register",
    "resolve_memory_store",
    "run",
    "run_ai_copilot_mode",
    "run_guided_mode",
]
