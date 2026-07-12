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
import functools
import json
import logging
import os
import sys
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import product_forge_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_banner import print_v2_banner
from fluid_build.cli.forge_copilot_memory import (
    CopilotMemoryStore,
    resolve_copilot_memory_root,
    summarize_copilot_memory,
)
from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context
from fluid_build.cli.forge_ui import print_welcome_panel
from fluid_build.cli.next_steps import print_next_steps

if TYPE_CHECKING:  # resolve annotation names for ruff/type-checkers only
    from fluid_build.cli.forge_copilot_agent import (
        AIAgent,
        CopilotAgentBase,
    )
    from fluid_build.cli.forge_copilot_runtime import (
        CopilotGenerationError,
        CopilotGenerationResult,
    )

# NOTE: The forge AI runtime (``forge_agents`` / ``forge_copilot_*`` /
# ``forge_data_model`` / ``forge_dialogs`` / ``forge_modes`` / ``forge_context``)
# transitively pulls ``httpx`` (LLM providers) and ``jsonschema`` (the
# ``schema_manager`` chain). ``register`` only needs argparse, so every one of
# those heavy module-scope imports is resolved lazily via ``__getattr__`` below
# and stays off the ``fluid --help`` / ``build_parser()`` cold path.
#
# Several of these symbols are patched by tests at module scope
# (``patch("…cli.forge.CopilotAgent")``, ``…resolve_llm_config``,
# ``…build_capability_matrix``, ``…generate_copilot_artifacts``,
# ``…discover_local_context``, ``…_run_copilot``, ``…ask_confirmation``) or
# imported from this module (``from …cli.forge import AIAgent``), so they MUST
# remain resolvable module attributes — that is exactly what ``__getattr__``
# provides. Runtime use sites resolve via ``import fluid_build.cli.forge as
# _self; _self.X`` so those patches flow through. Annotations referencing
# ``CopilotAgentBase`` / ``CopilotGenerationResult`` / ``AIAgent`` etc. are safe
# at module scope because ``from __future__ import annotations`` keeps them as
# lazy strings.

# alias -> (heavy source module, real symbol name)
_DEFERRED_FORGE_IMPORTS: Dict[str, tuple] = {
    "_get_cli_arg": ("forge_context", "get_cli_arg"),
    "_get_target_dir": ("forge_context", "get_target_directory"),
    "_handle_memory": ("forge_context", "handle_memory_management"),
    "_load_ctx": ("forge_context", "load_context"),
    "_resolve_store": ("forge_context", "resolve_memory_store"),
    "AIAgent": ("forge_copilot_agent", "AIAgent"),
    "CopilotAgentBase": ("forge_copilot_agent", "CopilotAgentBase"),
    "recommend_template_for_use_case": (
        "forge_copilot_agent",
        "recommend_template_for_use_case",
    ),
    "build_interview_summary_from_context": (
        "forge_copilot_interview",
        "build_interview_summary_from_context",
    ),
    "get_cumulative_prompt_cache_metrics": (
        "forge_copilot_llm_providers",
        "get_cumulative_prompt_cache_metrics",
    ),
    "reset_token_usage": ("forge_copilot_llm_providers", "reset_token_usage"),
    "CopilotGenerationError": ("forge_copilot_runtime", "CopilotGenerationError"),
    "CopilotGenerationResult": ("forge_copilot_runtime", "CopilotGenerationResult"),
    "build_capability_matrix": ("forge_copilot_runtime", "build_capability_matrix"),
    "discover_local_context": ("forge_copilot_runtime", "discover_local_context"),
    "generate_copilot_artifacts": (
        "forge_copilot_runtime",
        "generate_copilot_artifacts",
    ),
    "normalize_provider_name": ("forge_copilot_runtime", "normalize_provider_name"),
    "normalize_template_name": ("forge_copilot_runtime", "normalize_template_name"),
    "resolve_llm_config": ("forge_copilot_runtime", "resolve_llm_config"),
    "ask_confirmation": ("forge_dialogs", "ask_confirmation"),
    "ask_dialog_question": ("forge_dialogs", "ask_dialog_question"),
    "_scaffold_ci_pipeline": ("forge_modes", "_scaffold_ci_pipeline"),
    "_run_copilot": ("forge_modes", "run_ai_copilot_mode"),
    "_run_guided": ("forge_modes", "run_guided_mode"),
}


def __getattr__(name: str):
    """Lazily resolve the heavy forge AI-runtime imports (PEP 562).

    Keeps ``httpx`` + the ``schema_manager`` → ``jsonschema`` chain off the
    ``fluid --help`` path while exposing the symbols as module attributes so the
    various ``patch("…cli.forge.<symbol>")`` test seams (and
    ``from …cli.forge import AIAgent`` re-exports) keep working.
    """
    spec = _DEFERRED_FORGE_IMPORTS.get(name)
    if spec is not None:
        import importlib

        module = importlib.import_module(f"fluid_build.cli.{spec[0]}")
        return getattr(module, spec[1])
    if name == "CopilotAgent":
        return _build_copilot_agent_class()
    if name == "DOMAIN_AGENTS":
        from fluid_build.cli.forge_agents import DOMAIN_AGENTS

        return DOMAIN_AGENTS
    if name == "DOMAIN_AGENTS_AVAILABLE":
        from fluid_build.cli.forge_agents import DOMAIN_AGENTS

        return bool(DOMAIN_AGENTS)
    if name == "AI_AGENTS":
        return _build_ai_agents()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


@functools.lru_cache(maxsize=1)
def _build_copilot_agent_class() -> type:
    """Build (and cache) the public ``CopilotAgent`` class lazily.

    The class subclasses ``CopilotAgentBase`` from the heavy
    ``forge_copilot_agent`` module — evaluating the base at module scope would
    force that import onto the ``fluid --help`` path, so the class is built on
    first access to ``forge.CopilotAgent`` (via ``__getattr__``) instead.

    Memoised with ``functools.lru_cache(maxsize=1)`` — the repo's standard
    memoisation idiom (cf. ``_load_domain_keywords`` /
    ``_dbt_command_supports_adapter``) — rather than a hand-rolled module global
    + bare ``if`` check. lru_cache is internally lock-guarded, so two threads
    racing on the first access get the *same* class object (stable ``isinstance``
    / identity), and it exposes ``_build_copilot_agent_class.cache_clear()`` as a
    clean reset hook for tests. Patching is unaffected: tests still
    ``patch("…cli.forge.CopilotAgent")`` / ``from …cli.forge import
    CopilotAgent`` — both resolve through ``__getattr__``, which sets/reads a
    real module attribute that *shadows* this builder.
    """
    import fluid_build.cli.forge as _self
    from fluid_build.cli.forge_copilot_agent import CopilotAgentBase

    class CopilotAgent(CopilotAgentBase):
        """Public copilot agent wired to the compatibility aliases in this module."""

        def _resolve_llm_config_dependency(self, options):
            return _self.resolve_llm_config(options)

        def _discover_local_context_dependency(self, options):
            return _self.discover_local_context(
                getattr(options, "discovery_path", None),
                discover=getattr(options, "discover", True),
                workspace_root=Path.cwd(),
                logger=LOG,
            )

        def _build_capability_matrix_dependency(self):
            return _self.build_capability_matrix()

        def _generate_copilot_artifacts_dependency(
            self,
            context: Dict[str, Any],
            *,
            llm_config: Any,
            discovery_report: Any,
            project_memory: Any,
            team_memory: Any = None,
            capability_matrix: Any,
        ) -> "CopilotGenerationResult":
            return _self.generate_copilot_artifacts(
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
                return _self.ask_confirmation(
                    self.console,
                    prompt,
                    default=False,
                    title="🧠 Save Project Memory?",
                    preview=preview,
                    border_style="cyan",
                )
            return super()._ask_confirmation_dependency(prompt, preview)

    return CopilotAgent


def _build_ai_agents():
    import fluid_build.cli.forge as _self

    agents = {"copilot": _self.CopilotAgent}
    domain_agents = _self.DOMAIN_AGENTS
    if domain_agents:
        agents.update(domain_agents)
    return agents


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
    # Import from the LIGHT pure-argparse register module directly (not the
    # heavy ``forge_data_model`` re-export) so building the ``forge data-model``
    # subparser stays off the httpx/jsonschema cold path.
    from fluid_build.cli._forge_data_model_register import register_forge_subcommand

    register_forge_subcommand(forge_subparsers)

    # --- Primary flags ---
    parser.add_argument(
        "--blank",
        action="store_true",
        help="Scaffold an empty contract without AI (no LLM needed)",
    )
    parser.add_argument("--target-dir", "-d", help="Target directory for project creation")
    parser.add_argument("--provider", "-p", help="Infrastructure provider to use")
    parser.add_argument(
        "--data-product-type",
        dest="data_product_type",
        help=(
            "Data Mesh productType (SDP/ADP/CDP) or medallion layer "
            "(Bronze/Silver/Gold) — accepted interchangeably via the "
            "canonical mapping (Bronze↔SDP, Silver↔ADP, Gold↔CDP). "
            "When omitted the copilot infers from the project goal."
        ),
    )
    parser.add_argument(
        "--transform-engine",
        dest="transform_engine",
        help=(
            "Override the transformation engine (dbt / sql / spark / dataform / "
            "dataflow / glue) for ADP / CDP products. SDP products pick the "
            "right acquisition engine via the capability catalog."
        ),
    )
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
        choices=[
            "openai",
            "anthropic",
            "claude",
            "gemini",
            "ollama",
            # Keyless: route the LLM through the IDE (mcp-sampling) or a local
            # coding-agent CLI instead of an API key of forge's own.
            "mcp-sampling",
            "claude-code",
            "codex",
            "cursor",
            "kiro",
        ],
        help=(
            "LLM provider for copilot. Keyless options: mcp-sampling (in-IDE), "
            "claude-code / codex / cursor / kiro (local agent CLI)."
        ),
    )
    parser.add_argument("--llm-model", help="Model identifier for copilot")
    parser.add_argument(
        "--forge-agent-mode",
        dest="forge_agent_mode",
        choices=["envelope", "agentic"],
        help=(
            "Drive mode for coding-agent providers (claude-code/codex/cursor/kiro): "
            "'envelope' (agent returns the contract JSON on stdout; default) or "
            "'agentic' (agent writes contract.fluid.yaml into the workspace)."
        ),
    )
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
        "--prompt-profile",
        dest="prompt_profile",
        metavar="NAME",
        help=(
            "Swap the entire set of default prompt-guidance files "
            "(agent_specs/_defaults/*.yaml) for a named profile under "
            "agent_specs/prompt_profiles/<name>/. Bundled: eu-gdpr-strict, "
            "ai-lab-permissive. Single-name, single-swap (no stacking). The "
            "active profile is stamped into metadata.provenance.prompt_profile. "
            "Env fallback: FLUID_PROMPT_PROFILE."
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
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Run the fully local guided interview with no network calls: "
            "no LLM, no mode picker, no welcome scan, no remote schema "
            "fetch. Templates + schemas are already bundled, so authoring "
            "works air-gapped. Also enabled by FLUID_FORGE_OFFLINE=1 "
            "(mirrors `cargo --offline` / CARGO_NET_OFFLINE and the "
            "existing `fluid validate --offline`). Pair with "
            "--non-interactive for a headless, prompt-free run."
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Watch the discovery path (cwd + any --discovery-path) and "
            "re-generate the contract whenever a source file changes — like "
            "`dbt run --watch` / nodemon. Rapid saves are debounced into a "
            "single regeneration; the contract forge writes itself never "
            "retriggers the loop. Regeneration is non-interactive and "
            "network-free (the offline guided path), so a full interview is "
            "never re-run per change. Ctrl-C stops cleanly. Tune with "
            "FLUID_FORGE_WATCH_INTERVAL / FLUID_FORGE_WATCH_DEBOUNCE (seconds)."
        ),
    )
    parser.add_argument(
        "--seed-from",
        dest="seed_from",
        help=(
            "[experimental — pre-processor only] Structural seed for the LLM. "
            "Accepts a Bitol ODPS file (*.odps.yaml), a directory containing "
            "the ODPS doc + sibling ODCS files (or only ODCS files), or a "
            "lone ODCS file (*.odcs.yaml). The schema/quality/qos from the "
            "seed are treated as ground truth; the LLM fills in builds, "
            "execution, and governance. Today the seed pre-processor "
            "(fluid_build.cli.forge_copilot_seed.load_seed) is callable as "
            "a library; the copilot runtime hand-off + ground-truth diff "
            "guard wiring is on a follow-up commit."
        ),
    )
    parser.add_argument(
        "--seed-allow-remote",
        dest="seed_allow_remote",
        action="store_true",
        help=(
            "[experimental] When --seed-from has http(s) contractId "
            "references, allow http(s) fetch. Default is OFF (since "
            "the May 2026 SSRF hardening); the fetcher rejects "
            "internal/private IPs and pins the validated IP, but "
            "still only enable when you trust the upstream catalog."
        ),
    )
    parser.add_argument(
        "--seed-no-remote",
        dest="seed_no_remote",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated — default is already remote-off
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the pre-write preview confirmation prompt. The cost + file "
            "list is still rendered (invariant: cost is visible before it's "
            "spent), only the [Y/n] prompt is bypassed."
        ),
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help=(
            "Headless agent-drivable preset. Bundles --yes + all "
            "FLUID_FORGE_NO_{PICKER,PREVIEW,WELCOME,STREAMING_PREVIEW}=1 + "
            "emits JSON-Lines progress events (forge.start, forge.done, "
            "forge.contract_written, forge.cost) to stdout so the IDE's "
            "shell tool can parse them. Defaults to --blank when no mode "
            "flag is provided (avoids dropping into the interactive picker "
            "the agent cannot navigate). Use this from inside any agentic "
            "IDE (Kiro, Cursor, Claude Code, Cline)."
        ),
    )
    parser.add_argument(
        "--emit-plan",
        dest="emit_plan",
        action="store_true",
        help=(
            "Emit a one-shot `forge.plan` JSONL event with the structured "
            "checklist of what fields the agent must fill in to complete "
            "the contract (no LLM call, deterministic). Used after "
            "`--agent --blank` so the IDE's agent (which is itself an LLM) "
            "knows exactly what to author. Implies --agent."
        ),
    )
    parser.add_argument(
        "--show-work",
        action="store_true",
        help=(
            "Stream the agent's reasoning and tool calls live as they happen. "
            "Reasoning + transcript also persist to "
            ".fluid/agents/<run-id>/{reasoning.md,transcript.json}."
        ),
    )
    parser.add_argument(
        "--refine",
        nargs="?",
        const="contract.fluid.yaml",
        default=None,
        metavar="CONTRACT_PATH",
        help=(
            "Iterate on an existing contract. Reads the contract + the latest "
            ".fluid/agents/<run-id>/ artifacts and asks the LLM what to change. "
            "Defaults to ./contract.fluid.yaml when no path is given."
        ),
    )
    parser.add_argument(
        "--from-product",
        action="append",
        dest="from_product",
        default=[],
        metavar="ID_OR_PATH",
        help=(
            "Compose this product from an existing upstream product. "
            "Accepts a contract id (e.g. silver.commerce.orders_v1) or a "
            "path to a contract.fluid.yaml. Repeatable; each --from-product "
            "becomes one row in consumes[]."
        ),
    )
    parser.add_argument(
        "--from-product-list",
        dest="from_product_list",
        metavar="FILE",
        help=(
            "Read upstream product ids/paths from a file (one per line). "
            "Combines additively with --from-product."
        ),
    )
    parser.add_argument(
        "--from-workspace",
        dest="from_workspace",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Search this additional workspace path for upstream contracts. "
            "Repeatable; useful when upstream products live in a sibling repo."
        ),
    )
    parser.add_argument(
        "--also-emit",
        dest="also_emit",
        default=None,
        metavar="FORMATS",
        help=(
            "After writing the FLUID contract, also emit additional standards "
            "(comma-separated: odcs, opds, odps). Defaults to 'odcs' for CDP "
            "products and empty otherwise."
        ),
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "Use the browser-based AI setup flow when picking a provider. "
            "Opens the provider's API-key dashboard in your browser; the "
            "key still pastes back into the terminal (preview)."
        ),
    )
    # Gap 5 — apply post-synthesis enrichment artifacts back into the
    # contract (dbt tests, freshness, physical layout). Conservative
    # merge: never overwrites user-set fields. OFF by default; the
    # confirmation prompt is bypassed with --yes.
    parser.add_argument(
        "--apply-enrichment",
        dest="apply_enrichment",
        action="store_true",
        default=False,
        help=(
            "Land the post-synthesis enrichment artifacts (dbt tests, "
            "freshness, physical layout) back into the contract itself. "
            "Shows a unified diff and prompts before writing; pair with "
            "--yes to bypass the prompt. Default OFF: behavior is "
            "unchanged without this flag."
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
    parser.add_argument(
        "--memory-json",
        action="store_true",
        help=(
            "When combined with --show-memory, emit the layered memory dump "
            "as machine-readable JSON instead of a Rich panel."
        ),
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
        "--template",
        dest="scaffold",
        metavar="TEMPLATE",
        default=None,
        help=(
            "Opt into the full ForgeEngine scaffold using one of the built-in "
            "templates (e.g. 'etl_pipeline', 'analytics', 'ml_pipeline', "
            "'streaming', 'starter'). ``--template`` is an alias retained "
            "for parity with ``fluid init --template``. Default: minimal — "
            "only contract.fluid.yaml and .fluid/forge-receipt.json are written."
        ),
    )
    # Mirror the parsed value to ``args.template`` so the mode-picker
    # template branch (which reads ``args.template``) sees it too.
    # argparse aliases only set the dest, not extra attributes.

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

    # --- Resume / time-travel flags (resumability) ---
    # Default: a bare `fluid forge` auto-detects an incomplete run in
    # the cwd and prompts (default = continue). --resume <id> jumps to
    # a specific run; --resume (bare) takes the most recent.
    parser.add_argument(
        "--resume",
        nargs="?",
        const="__RESUME_BARE__",
        default=None,
        metavar="RUN_ID",
        help=(
            "Resume an interrupted forge run. With no argument, picks the "
            "most-recent paused run in this workspace. Pass a run-id (or "
            "unambiguous prefix) to jump to a specific one. See "
            "'fluid agents list' for paused runs."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help=(
            "Skip the auto-detect-and-prompt for paused runs. Always start "
            "fresh, even when a paused run exists in this directory."
        ),
    )
    parser.add_argument(
        "--from-stage",
        dest="from_stage",
        default=None,
        metavar="STAGE",
        help=(
            "Time-travel to a specific stage (requires --resume). Stages: "
            "logical, contract_forge, builder, readme, transformation, "
            "validator, enrichment, judge."
        ),
    )
    parser.add_argument(
        "--fork",
        dest="fork",
        default=None,
        metavar="RUN_ID",
        help=(
            "Like --resume but assigns a new run id and copies stages up "
            "to --from-stage. Requires --from-stage."
        ),
    )
    parser.add_argument(
        "--or-fail",
        dest="or_fail",
        action="store_true",
        default=False,
        help=(
            "Require --resume to find a resumable run; exit 1 if none "
            "exists. Useful for scripts that should fail-fast rather "
            "than start fresh."
        ),
    )

    parser.set_defaults(func=run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ResumeFlagError(Exception):
    """Raised by ``_resolve_resume_args`` on incompatible flag combinations.

    Carries an ``exit_code`` so the dispatcher can return the right
    POSIX code without sniffing the exception message. Defaults to 2
    (usage error per ``argparse`` convention); the ``--or-fail`` policy
    violation overrides to 1 (per spec).
    """

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _resolve_resume_args(
    args: Any,
    *,
    workspace_root: Path,
    console: Optional[Any] = None,
) -> None:
    """Validate resume/fork/from-stage flags and resolve the run-id.

    Mutates ``args``:
      * sets ``args._resume_run_id`` to the resolved run-id (or None)
      * normalises ``args.resume`` (None for absent, "" for bare,
        "<id>" for explicit)
      * sets ``args._resume_explicit`` (True when --resume appeared)

    Raises :class:`_ResumeFlagError` on invalid combinations:
      * --fork without --from-stage (exit 2)
      * --from-stage with an unknown stage name (Did-you-mean) (exit 2)
      * --resume <id> with an unknown id (exit 2)
      * --or-fail without a resumable run (exit 1 — policy violation,
        not a usage error)
    """
    from fluid_build.cli._forge_resume import (
        ResumeError,
        maybe_prompt_resume,
        validate_from_stage,
    )

    # Defensive type-guards — tests that build args via MagicMock have
    # every attribute auto-attr'd to a Mock instance. argparse always
    # passes None / str / bool for these flags; coerce non-conforming
    # values to None/False so the validator never sees a Mock object.
    # Mirrors the same guard pattern in ``_forge_resume.py``.
    def _str_or_none(value: Any) -> Optional[str]:
        return value if isinstance(value, str) else None

    def _bool_or_false(value: Any) -> bool:
        return bool(value) if isinstance(value, bool) else False

    resume_raw = _str_or_none(getattr(args, "resume", None))
    # Argparse stores "__RESUME_BARE__" when the flag appeared without a
    # value; None when it didn't appear; "<id>" when explicit.
    if resume_raw == "__RESUME_BARE__":
        args.resume = None
        args._resume_explicit = True
    elif resume_raw is None:
        args.resume = None
        args._resume_explicit = False
    else:
        args.resume = resume_raw
        args._resume_explicit = True
    # Coerce the other resume-shaped flags too — test fixtures auto-attr
    # them to MagicMock truthies; the policy checks below treat any
    # truthy as "flag set", which corrupts the no-flags path.
    args.no_resume = _bool_or_false(getattr(args, "no_resume", False))
    args.or_fail = _bool_or_false(getattr(args, "or_fail", False))

    # --fork requires --from-stage
    fork_target = getattr(args, "fork", None)
    from_stage = getattr(args, "from_stage", None)
    # Defensive type-guard — tests that build args via MagicMock have
    # ``from_stage`` auto-attr'd to a Mock instance, which then crashes
    # deep in ``validate_from_stage``'s ``.lower()`` call. argparse
    # always passes ``None`` or ``str``; coerce anything else to
    # ``None`` so the validator never sees a non-string.
    if from_stage is not None and not isinstance(from_stage, str):
        from_stage = None
    if isinstance(fork_target, str) and not fork_target.strip():
        fork_target = None
    elif fork_target is not None and not isinstance(fork_target, str):
        fork_target = None
    if fork_target and not from_stage:
        raise _ResumeFlagError(
            "--fork requires --from-stage (which stage of the source run to fork from)"
        )
    # --from-stage requires --resume or --fork
    if from_stage and not (args._resume_explicit or fork_target):
        raise _ResumeFlagError("--from-stage requires --resume <run-id> or --fork <run-id>")
    if from_stage:
        ok, msg = validate_from_stage(from_stage)
        if not ok:
            raise _ResumeFlagError(msg)

    # Resolve the run-id.
    try:
        run_id = maybe_prompt_resume(args, workspace_root=workspace_root)
    except ResumeError as exc:
        # ``--or-fail`` policy violation (no resumable run in cwd) →
        # exit 1 per spec; usage errors (typo'd id, ambiguous prefix,
        # --no-resume + --or-fail) → exit 2 (argparse convention).
        # We distinguish by message prefix so the dispatcher reports
        # the right POSIX code without sniffing exception types.
        msg = str(exc)
        is_policy_violation = (
            getattr(args, "or_fail", False)
            and "No resumable run found" in msg
            and "not found" not in msg  # disambiguates "Run X not found"
        )
        if is_policy_violation:
            raise _ResumeFlagError(msg, exit_code=1) from exc
        raise _ResumeFlagError(msg) from exc

    args._resume_run_id = run_id
    if run_id and console:
        try:
            console.print(
                f"\n[bold cyan]↻ Resuming run {run_id}[/bold cyan]"
                + (f" from stage [bold]{from_stage}[/bold]" if from_stage else "")
            )
        except Exception:  # noqa: BLE001
            pass


def get_target_directory(args, default_name: str = "my-fluid-project") -> Path:
    import fluid_build.cli.forge as _self

    return _self._get_target_dir(args, default_name)


def get_cli_arg(args: Any, name: str, default: Any = None) -> Any:
    import fluid_build.cli.forge as _self

    return _self._get_cli_arg(args, name, default)


def _forge_offline_requested(args: Any) -> bool:
    """True when the offline guided path should run — no network anywhere.

    Two triggers, mirroring ``cargo``'s ``--offline`` flag + its
    ``CARGO_NET_OFFLINE`` env twin (and consistent with the existing
    ``fluid validate --offline``):

    * the ``--offline`` flag (``args.offline``), or
    * ``FLUID_FORGE_OFFLINE`` set to a truthy value in the environment.

    Env parsing matches the rest of the forge toggle surface
    (``_forge_mode_picker``): any non-empty value other than the usual
    falsey spellings (``0`` / ``false`` / ``no`` / ``off``) counts as on.
    """
    if get_cli_arg(args, "offline", False):
        return True
    raw = os.environ.get("FLUID_FORGE_OFFLINE")
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def resolve_memory_store(args, logger: logging.Logger) -> CopilotMemoryStore:
    import fluid_build.cli.forge as _self

    return _self._resolve_store(args, logger, memory_store_class=CopilotMemoryStore)


def handle_memory_management(args, logger: logging.Logger) -> int:
    import fluid_build.cli.forge as _self

    return _self._handle_memory(
        args,
        logger,
        memory_store_class=CopilotMemoryStore,
        console_factory=Console if RICH_AVAILABLE else None,
    )


# ---------------------------------------------------------------------------
# Blank mode — scaffold empty contract, no AI
# ---------------------------------------------------------------------------


def _pick_template_subchoice(console: Any) -> Optional[str]:
    """Sub-picker shown when the user chose ``template`` in the mode menu
    but didn't pre-select one with ``--template``.

    Lists the templates registered with ``forge.core.registry`` so a new
    template auto-appears here without code edits.
    """
    try:
        from fluid_build.cli.forge_ui import ask_numbered_choice
        from fluid_build.forge.core.registry import template_registry
    except Exception as exc:  # noqa: BLE001
        LOG.debug("template_subpicker_unavailable: %s", exc)
        return "starter"

    try:
        names = sorted(template_registry.list_available())
    except Exception:  # noqa: BLE001
        names = ["starter", "analytics", "etl_pipeline", "streaming", "ml_pipeline"]
    if not names:
        return "starter"

    choices = [(name, name.replace("_", " ").title()) for name in names]
    choices.append(("cancel", "Cancel — back to forge"))
    pick = ask_numbered_choice(
        console,
        "Which template?",
        choices,
        default=1,
    )
    if pick == "cancel":
        return None
    return pick


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

    # Agent-mode JSONL: blank-mode bypasses ``_write_forge_receipt`` (no LLM
    # cost to record), so we emit ``forge.contract_written`` directly here
    # to keep the event stream contract consistent.
    if bool(get_cli_arg(args, "agent", False)):
        try:
            size = result_path.stat().st_size
        except OSError:
            size = -1
        _emit_agent_jsonl(
            "forge.contract_written",
            path=str(result_path),
            action="created",
            size=size,
        )

    # In --agent mode we skip the interactive CI-pipeline prompt entirely.
    # The agent can shell-run ``fluid scaffold-ci --system <name>`` separately
    # if it wants CI scaffolding. Without this guard, blank-mode under
    # --agent hits an EOFError trying to read from a closed stdin.
    if not bool(get_cli_arg(args, "agent", False)):
        import fluid_build.cli.forge as _self

        _self._scaffold_ci_pipeline(
            args,
            target_dir,
            {},
            console,
            ask_dialog_question_fn=_self.ask_dialog_question,
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
# Agent-mode helpers (Phase 2B — `fluid forge --agent`)
# ---------------------------------------------------------------------------
#
# These let the IDE's agent shell-run forge cleanly: a single ``--agent``
# flag bundles all FLUID_FORGE_NO_* env vars + ``--yes`` + a default-blank
# fallback so we never drop into the interactive picker. A small JSON-Lines
# event stream over stdout lets the agent parse progress (start / done /
# contract_written / cost) without scraping Rich console output.
#
# Design choice: events go to stdout AS WHOLE JSON OBJECTS PER LINE so the
# agent's parser is ``for line in stdout: try: obj = json.loads(line);
# except: pass``. Rich's banners on stdout don't parse as JSON, so they're
# harmlessly ignored. No need to mute Rich (which would break user-facing
# UX outside ``--agent``).


def _emit_agent_jsonl(event: str, **kvs: Any) -> None:
    """Emit one canonical JSON-Lines event to stdout for agent consumption.

    Only used when ``--agent`` is active; callers gate via ``args.agent``.
    Failures are swallowed so a stdout pipe-closed never poisons the run.
    """
    try:
        payload = {"event": event, "ts": time.time(), **kvs}
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _setup_agent_mode(args: Any) -> None:
    """Mutate env + args so the run is non-interactive and machine-friendly."""
    # Suppress all of forge's interactive UI surfaces so the agent never
    # gets stuck on a prompt it can't answer.
    for var in (
        "FLUID_FORGE_NO_PICKER",
        "FLUID_FORGE_NO_PREVIEW",
        "FLUID_FORGE_NO_WELCOME",
        "FLUID_FORGE_NO_STREAMING_PREVIEW",
    ):
        os.environ.setdefault(var, "1")
    args.yes = True

    # If the agent didn't pick a mode, default to --blank: it's the
    # only mode that succeeds without an LLM round-trip, so it's the
    # safest fallback when invoked under ``--agent --data-product-type X``
    # with no other intent. The agent can override by passing
    # --from-product, --refine, or --template explicitly.
    no_mode = not any(
        [
            bool(getattr(args, "blank", False)),
            bool(getattr(args, "template", None)),
            bool(getattr(args, "scaffold", None)),
            bool(getattr(args, "refine", None)),
            bool(getattr(args, "from_product", None)),
            bool(getattr(args, "from_product_list", None)),
        ]
    )
    if no_mode:
        args.blank = True


def _agent_run_id() -> str:
    """Short, stable run-id for the JSONL event stream (12 hex chars)."""
    import uuid

    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# --emit-plan — deterministic "what to fill in" checklist for the IDE agent
# ---------------------------------------------------------------------------
#
# Architectural note: the IDE's agent already has an LLM (paid for by the
# IDE's subscription). We don't want forge to ALSO run an LLM — that would
# need a second API key. Instead, we emit a deterministic checklist of
# fields the agent must fill in, and let the agent (the LLM in the IDE) do
# the authoring using its own Edit tools. Pattern 1 from AGENT_IDE.md.
#
# The checklist is keyed by productType so SDP / ADP / CDP each get the
# right next-steps (e.g. SDP has acquisition[], ADP has transformations[],
# CDP has consumes[] + transformations[] + access[]).

_PLAN_BY_PRODUCT_TYPE: Dict[str, List[Dict[str, Any]]] = {
    "SDP": [
        {
            "step": "Define acquisition[]",
            "description": "How data is acquired from the source system",
            "fields": ["acquisition[].source", "acquisition[].mode"],
            "mcp_tools": ["list_source_adapters", "list_source_tables", "inspect_source_table"],
        },
        {
            "step": "Define models[].entities[]",
            "description": "Mirror the upstream schema — entity per source table",
            "fields": [
                "models[].name",
                "models[].entities[].name",
                "models[].entities[].fields[]",
                "models[].entities[].primaryKey",
            ],
            "mcp_tools": ["inspect_source_table", "list_source_lineage"],
        },
        {
            "step": "Add quality[]",
            "description": "Declarative quality rules (uniqueness, freshness, schema)",
            "fields": ["quality[].rule", "quality[].entity"],
            "mcp_tools": ["search_semantic_memory"],
        },
    ],
    "ADP": [
        {
            "step": "Define consumes[]",
            "description": "Upstream SDP/ADP products this aggregate is built from",
            "fields": ["consumes[].productId", "consumes[].version"],
            "mcp_tools": ["list_source_lineage", "search_semantic_memory"],
        },
        {
            "step": "Define models[].entities[] for the aggregate",
            "description": "Aggregate-level entities (joined/grouped)",
            "fields": ["models[].entities[].name", "models[].entities[].fields[]"],
            "mcp_tools": ["inspect_source_table", "read_logical_model"],
        },
        {
            "step": "Define transformations[]",
            "description": "dbt model refs, inline SQL, or external .sql files",
            "fields": ["transformations[].engine", "transformations[].sql"],
            "mcp_tools": [],
        },
        {
            "step": "Add quality[]",
            "description": "Aggregate-level quality rules",
            "fields": ["quality[].rule", "quality[].entity"],
            "mcp_tools": [],
        },
    ],
    "CDP": [
        {
            "step": "Define consumes[]",
            "description": "Upstream products feeding this consumer-aligned product",
            "fields": ["consumes[].productId", "consumes[].version"],
            "mcp_tools": ["list_source_lineage", "search_semantic_memory"],
        },
        {
            "step": "Define models[].entities[] for the consumer view",
            "description": "Consumer-facing entities (denormalised, BI-friendly)",
            "fields": ["models[].entities[].name", "models[].entities[].fields[]"],
            "mcp_tools": ["read_logical_model"],
        },
        {
            "step": "Define transformations[]",
            "description": "Joins + denormalisation SQL",
            "fields": ["transformations[].engine", "transformations[].sql"],
            "mcp_tools": [],
        },
        {
            "step": "Define access[]",
            "description": "IAM/GRANT bindings for downstream consumers",
            "fields": ["access[].principal", "access[].grants[]"],
            "mcp_tools": [],
        },
        {
            "step": "Add exposes block (optional)",
            "description": "Agent-policy for downstream AI consumers",
            "fields": ["exposes.agentPolicy.allowedUseCases", "exposes.semantics"],
            "mcp_tools": [],
        },
    ],
}


def _emit_forge_plan(args: Any, contract_path: Optional[Path]) -> None:
    """Emit a single ``forge.plan`` JSONL event with the structured checklist
    for what the IDE's agent should fill in. Deterministic; no LLM call.
    """
    dpt_raw = (getattr(args, "data_product_type", None) or "").strip().upper()
    # Map medallion → DataMesh layer per the canonical mapping in CLAUDE.md.
    layer_map = {"BRONZE": "SDP", "SILVER": "ADP", "GOLD": "CDP"}
    dpt = layer_map.get(dpt_raw, dpt_raw)
    steps = _PLAN_BY_PRODUCT_TYPE.get(dpt, _PLAN_BY_PRODUCT_TYPE["SDP"])
    _emit_agent_jsonl(
        "forge.plan",
        contract_path=str(contract_path) if contract_path else None,
        data_product_type=dpt or None,
        next_steps=steps,
        validation_command=(
            f"fluid validate {contract_path}" if contract_path else "fluid validate <path>"
        ),
        completion_signal=(
            "After filling in the contract, call MCP `validate_contract` to "
            "gate-check, then `fluid bundle && fluid plan` for the pipeline."
        ),
        note=(
            "You (the IDE's agent) are the LLM. Fill in these fields using "
            "your own Edit tool; do NOT shell-run `fluid forge --ai` (that "
            "would need a second LLM API key)."
        ),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(args, logger: logging.Logger) -> int:
    """Agent-mode-aware wrapper around the main forge entry point.

    When ``--agent`` is set, configures the run for headless invocation and
    emits a JSONL event stream so the IDE's shell tool can parse progress.
    """
    # ``--emit-plan`` implies ``--agent`` — the plan event is part of the
    # JSONL stream contract.
    if getattr(args, "emit_plan", False) and not getattr(args, "agent", False):
        args.agent = True

    is_agent = bool(getattr(args, "agent", False))
    if not is_agent:
        return _run_main(args, logger)

    _setup_agent_mode(args)
    run_id = _agent_run_id()
    mode = (
        "from-product"
        if getattr(args, "from_product", None)
        else (
            "refine"
            if getattr(args, "refine", None)
            else (
                "template"
                if getattr(args, "template", None) or getattr(args, "scaffold", None)
                else ("blank" if getattr(args, "blank", False) else "ai")
            )
        )
    )
    _emit_agent_jsonl(
        "forge.start",
        run_id=run_id,
        mode=mode,
        data_product_type=getattr(args, "data_product_type", None),
        target_dir=str(getattr(args, "target_dir", None) or ""),
        emit_plan=bool(getattr(args, "emit_plan", False)),
    )
    rc = 1
    try:
        rc = _run_main(args, logger)
        # On success, optionally emit the structured plan checklist BEFORE
        # forge.done so consumers parsing the stream see the plan in order.
        if rc == 0 and getattr(args, "emit_plan", False):
            target_dir = getattr(args, "target_dir", None)
            contract_path = Path(target_dir) / "contract.fluid.yaml" if target_dir else None
            _emit_forge_plan(args, contract_path)
        return rc
    finally:
        _emit_agent_jsonl("forge.done", run_id=run_id, exit_code=int(rc or 0))


#: Per-process flag for the personal-memory sanitiser.  See
#: ``_sanitize_personal_memory_once`` for the rationale — issue #51 fix.
_PERSONAL_MEMORY_SANITISED: bool = False


def _sanitize_personal_memory_once() -> None:
    """Run the personal-memory poison sanitiser exactly once per process.

    Issue #51 fix: ``_sanitize_existing_personal_memory`` (the on-disk
    cleanup helper introduced by P1a) was previously only invoked from
    ``tests/conftest.py``.  A real engineer whose ``~/.fluid/personal-memory.json``
    got poisoned by an earlier test run (or by hand) never had the
    file rewritten — the in-memory ``_strip_poisoned_values`` cleared
    the values for the current run but the next run would re-read the
    same poison.

    Calling the sanitiser once per process at the entry to ``fluid
    forge`` closes that gap.  The helper is idempotent and safe when
    the file is absent (documented in the helper's docstring).  Best
    effort: any error is logged at DEBUG and never blocks the run.

    The module-level flag avoids repeating the disk read on subsequent
    in-process invocations (notably the test suite, where ``_run_main``
    is called many times per session).
    """
    global _PERSONAL_MEMORY_SANITISED
    if _PERSONAL_MEMORY_SANITISED:
        return
    _PERSONAL_MEMORY_SANITISED = True
    try:
        from fluid_build.cli.forge_copilot_personal_memory import (
            _sanitize_existing_personal_memory,
        )

        _sanitize_existing_personal_memory()
    except Exception as exc:  # noqa: BLE001 — best effort, never blocks the run
        LOG.debug("personal_memory_sanitise_failed: %s", exc)


def _run_main(args, logger: logging.Logger) -> int:
    """Main entry point for ``fluid forge``."""
    _sanitize_personal_memory_once()
    console = Console() if RICH_AVAILABLE else None
    try:
        # --- Prompt profile (single-name, single-swap) ---
        # Resolve from --prompt-profile or FLUID_PROMPT_PROFILE and activate it
        # process-wide BEFORE any prompt is built or contract written, so both
        # the AI system prompt and the provenance stamp observe it. An
        # unknown/unsafe name is a hard, clear error — never a silent fallback.
        from fluid_build.cli.forge_copilot_prompts import (
            PromptProfileError,
            set_prompt_profile,
        )

        _cli_profile = getattr(args, "prompt_profile", None)
        if not isinstance(_cli_profile, str):
            # Robust against non-string args (e.g. a MagicMock in tests); real
            # argparse always yields a str or None.
            _cli_profile = None
        _profile_name = _cli_profile or os.environ.get("FLUID_PROMPT_PROFILE") or None
        try:
            set_prompt_profile(_profile_name)
        except PromptProfileError as exc:
            if console:
                console.print(f"[red]Invalid --prompt-profile:[/red] {exc}")
            else:
                console_error(f"Invalid --prompt-profile: {exc}")
            return 2
        if _profile_name:
            LOG.debug("Forge: prompt profile active: %s", _profile_name)

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

        # --- Watch mode (Trello #69d4c9cb — Forge UX: `fluid forge --watch`) ---
        # Re-generate the contract whenever a watched source file changes.
        # Intercepts before the mode picker / AI setup because watch owns the
        # whole run: it loops, regenerating non-interactively (offline guided,
        # no network) on each debounced change until Ctrl-C.
        if get_cli_arg(args, "watch", False):
            return _run_watch(args, logger)

        # --- Snapshot for the forge receipt ---
        # Scan cwd before any mode runs so the diff catches every file the
        # mode handler wrote.  find_workspace_root is cheap and localised;
        # if this isn't a workspace yet, we fall back to cwd and still
        # produce a receipt.
        scan_root = Path.cwd()
        before_snapshot = snapshot_workspace(scan_root)
        import fluid_build.cli.forge as _self

        _self.reset_token_usage()

        # --- Resume detection + flag validation (Phase R1) ---
        # Validate flag combinations and surface a resume prompt before
        # any other work. The resolved run-id (or None) lands on
        # ``args._resume_run_id`` so downstream coordinators can pick
        # it up from one canonical place.
        try:
            _resolve_resume_args(args, workspace_root=scan_root, console=console)
        except _ResumeFlagError as exc:
            if console:
                console.print(f"[red]{exc}[/red]")
            else:
                console_error(str(exc))
            return exc.exit_code

        # --- Startup prune-hint (non-blocking, once per run) ---
        try:
            from fluid_build.cli._forge_resume import maybe_print_prune_hint

            maybe_print_prune_hint(scan_root)
        except Exception:  # noqa: BLE001 — never block on the hint
            LOG.debug("prune_hint_failed", exc_info=True)

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

        # --- Offline guided mode (Trello #69d4c9ca — Forge UX: offline guided) ---
        # ``--offline`` (or FLUID_FORGE_OFFLINE=1) forces the fully local,
        # no-network authoring path: the heuristic guided interview, no
        # LLM, no mode picker, no welcome scan, no remote schema fetch.
        # Modeled on ``cargo --offline`` / CARGO_NET_OFFLINE and
        # consistent with the existing ``fluid validate --offline``.
        # Templates + schemas are already bundled, so this works
        # air-gapped. An explicit ``--blank`` (already fully offline) is
        # the more specific "empty contract" intent, so it wins and falls
        # through to the blank path below.
        if _forge_offline_requested(args) and not get_cli_arg(args, "blank", False):
            LOG.debug("Forge: offline guided mode selected")
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

        # --- Mode picker (Phase 0.2) ---
        # When the user runs bare ``fluid forge`` with no mode flag and
        # stdin is a TTY, surface the menu of authoring paths instead
        # of dropping straight into AI mode. Pre-highlights the most
        # likely choice from the welcome scan (existing contract →
        # refine; existing products → from_product; otherwise → AI).
        _picker_ran = False
        _picked_mode = "ai"
        try:
            from fluid_build.cli._forge_mode_picker import pick_mode, should_show_picker

            if should_show_picker(args):
                _picked_mode = pick_mode(args, console=console)
                _picker_ran = True
                LOG.debug("Forge: mode picker selected %s", _picked_mode)
                # Print a sticky confirmation so the user always sees
                # which path the run is taking — even when downstream
                # panels (welcome / Ollama detection) push the picker
                # off-screen on short terminals.
                if console:
                    try:
                        _label_map = {
                            "ai": "🧠 AI Copilot",
                            "blank": "🧱 Blank scaffold",
                            "refine": "✏️  Refine existing contract",
                            "from_product": "🔗 Compose from existing products",
                            "template": "📋 Template-based",
                        }
                        console.print(
                            f"\n[bold green]→[/bold green] [bold]{_label_map.get(_picked_mode, _picked_mode)}[/bold] mode\n"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # ``from_product`` mode picker — gather upstream picks
                # interactively so the AI mode below can use them.
                if _picked_mode == "from_product":
                    try:
                        from fluid_build.cli._forge_from_product_picker import (
                            pick_upstream_products,
                        )

                        picks = pick_upstream_products(console=console)
                        if picks:
                            args.from_product = list(picks)
                    except Exception:  # noqa: BLE001 — picker is best-effort
                        LOG.debug("from_product_picker_failed", exc_info=True)
        except Exception:  # noqa: BLE001 — never let the picker break forge
            LOG.debug("mode_picker_failed", exc_info=True)

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

        # Template mode: route to the dedicated handler, no AI required.
        # Triggered by the picker's "template" choice OR by
        # ``--template``/``--scaffold`` on the command line. argparse
        # stores both flags in ``args.scaffold``; ``run_template_mode``
        # reads ``args.template``, so we mirror across them.
        _scaffold_value = getattr(args, "scaffold", None) or getattr(args, "template", None)
        # Only treat as explicit when the value is a real string —
        # tests sometimes pass MagicMock placeholders that are truthy
        # but not actual template names.
        _template_explicit = isinstance(_scaffold_value, str) and bool(_scaffold_value)
        if _template_explicit:
            args.template = _scaffold_value
            args.scaffold = _scaffold_value

        if (_picker_ran and _picked_mode == "template") or _template_explicit:
            LOG.debug(
                "Forge: template mode (picker=%s, explicit=%s)", _picker_ran, _template_explicit
            )
            # Default template when the user hasn't set one explicitly.
            if not getattr(args, "template", None):
                tpl = _pick_template_subchoice(console)
                if tpl is None:
                    return 0
                args.template = tpl
                args.scaffold = tpl
            from fluid_build.cli.forge_modes import (
                run_template_mode as _run_template,
            )

            result = _run_template(
                args,
                logger,
                get_target_directory_fn=get_target_directory,
            )
            if result == 0:
                _write_forge_receipt(
                    flow="template",
                    args=args,
                    before_snapshot=before_snapshot,
                    scan_root=scan_root,
                    logger=logger,
                )
                _print_forge_next_steps(console, args, scan_root)
            return result

        # --- Default: AI Copilot with inline LLM setup ---
        LOG.debug("Forge: copilot mode")
        # Skip the redundant FLUID-Forge welcome panel when the
        # mode picker already showed; the picker IS the welcome.
        if console and not get_cli_arg(args, "non_interactive", False) and not _picker_ran:
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
                    import fluid_build.cli.forge as _self

                    proceed = _self.ask_confirmation(
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
    import fluid_build.cli.forge as _self

    return _self._run_copilot(
        args,
        logger,
        copilot_class=_self.CopilotAgent,
        get_cli_arg_fn=get_cli_arg,
        load_context_fn=load_context,
        get_target_directory_fn=get_target_directory,
        context_error_cls=ContextValidationError,
        build_interview_summary_fn=_self.build_interview_summary_from_context,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def run_guided_mode(args, logger: logging.Logger) -> int:
    """Lightweight guided prompts — no AI needed."""
    import fluid_build.cli.forge as _self

    return _self._run_guided(
        args,
        logger,
        get_target_directory_fn=get_target_directory,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def _watch_regenerate(args, logger: logging.Logger) -> int:
    """One non-interactive, network-free contract regeneration for watch mode.

    A watch loop cannot re-run a full interview per change, so it reuses the
    deterministic **offline guided** path: re-derive answers from flags/defaults
    and (over)write the contract. ``create_and_validate_contract`` overwrites in
    place, so the regeneration is idempotent — safe to run on every change.
    """
    import fluid_build.cli.forge as _self

    # Watch implies headless regeneration — force it so the guided path never
    # blocks on a prompt or fails the interactive-TTY guard.
    args.non_interactive = True
    return _self.run_guided_mode(args, logger)


def _run_watch(args, logger: logging.Logger) -> int:
    """Enter the forge watch loop — regenerate the contract on source change.

    Delegates the loop to ``_forge_watch.run_watch_mode`` (imported lazily so it
    stays off the ``fluid --help`` path) and injects ``_watch_regenerate`` as the
    per-change callback. Resolving through the module (``_self``) keeps the test
    patch seams (``forge._watch_regenerate`` / ``forge.run_guided_mode``) live.
    """
    import fluid_build.cli.forge as _self
    from fluid_build.cli._forge_watch import run_watch_mode

    return run_watch_mode(
        args,
        logger,
        regenerate_fn=_self._watch_regenerate,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def load_context(
    context_input: str,
    console: Optional[Any] = None,
    *,
    context_error_cls: type[Exception] = ContextValidationError,
) -> Dict[str, Any]:
    import fluid_build.cli.forge as _self

    return _self._load_ctx(
        context_input,
        console,
        context_error_cls=context_error_cls,
    )


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
        is_agent = bool(get_cli_arg(args, "agent", False))
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
            # Agent-mode JSONL: one event per changed file. The agent's
            # caller can highlight new contracts in its UI without
            # parsing the receipt.
            if is_agent and entry.action in ("created", "modified"):
                _emit_agent_jsonl(
                    (
                        "forge.contract_written"
                        if str(entry.path).endswith("contract.fluid.yaml")
                        else "forge.file_written"
                    ),
                    path=str(entry.path),
                    action=entry.action,
                    size=entry.size,
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
    import fluid_build.cli.forge as _self

    metrics = _self.get_cumulative_prompt_cache_metrics()
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


__all__ = [
    "AIAgent",
    "AI_AGENTS",  # noqa: F822 — resolved lazily via module __getattr__
    "COMMAND",
    "ContextValidationError",
    "CopilotAgent",  # noqa: F822 — resolved lazily via module __getattr__
    "ForgeError",
    "ForgeMode",
    "InvalidProjectNameError",
    "ProjectGenerationError",
    "get_cli_arg",
    "get_target_directory",
    "handle_memory_management",
    "load_context",
    "register",
    "resolve_memory_store",
    "run",
    "run_ai_copilot_mode",
    "run_guided_mode",
]
