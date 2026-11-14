# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/cli/bootstrap.py
from __future__ import annotations

import argparse
import atexit
import importlib
import logging
import os
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from fluid_build.cli.console import error as console_error

# Import shared utilities. ``build_provider`` is no longer used here; it stays
# importable from this module because existing tests patch it at this path.
from ._common import CLIError, build_provider, load_contract_with_overlay  # noqa: F401

LOG = logging.getLogger("fluid.cli")

# -------------------------
# Build Profiles
# -------------------------
# Profiles control which commands are registered.
# "experimental" (default) enables everything – no behaviour change.
# "stable" restricts to the curated command set.

_STABLE_COMMANDS = frozenset(
    {
        "init",
        "validate",
        "plan",
        "apply",
        "graph",
        # "execute" removed — deprecated subcommand deleted when build_runners
        # landed. Users should call ``fluid apply --build <id>`` instead.
        "verify",
        "export",
        "docs",
        "doctor",
        "providers",
        "plugins",
        "version",
        "test",
    }
)


def _active_profile() -> str:
    """Return the active build profile (default: experimental = all-on)."""
    return os.environ.get("FLUID_BUILD_PROFILE", "experimental").lower()


def is_command_enabled(name: str) -> bool:
    """Check whether *name* should be registered under the active profile."""
    profile = _active_profile()
    if profile == "experimental":
        return True  # everything on – no breaking change
    return name in _STABLE_COMMANDS  # curated set


# -------------------------
# Command Center Reporter (Global)
# -------------------------
_REPORTER: Optional[Any] = None


def get_reporter():
    """Get or create the global Command Center reporter."""
    global _REPORTER
    if _REPORTER is None:
        try:
            from ..observability import CommandCenterConfig, CommandCenterReporter

            config = CommandCenterConfig.from_environment()
            _REPORTER = CommandCenterReporter(config)
            _REPORTER.start()
            # Register shutdown handler
            atexit.register(_REPORTER.stop)
            if config.is_configured():
                LOG.info(f"Command Center integration enabled (url={config.url})")
            else:
                LOG.debug("Command Center not configured")
        except Exception as e:
            LOG.debug(f"Command Center reporter initialization failed: {e}")
            _REPORTER = None
    return _REPORTER


# -------------------------
# Helpers: dynamic imports
# -------------------------
def _imp(mod: str, attr: Optional[str] = None) -> Any:
    """Import module (and optionally attribute) with a clear error surface."""
    m = importlib.import_module(mod)
    return getattr(m, attr) if attr else m


# -------------------------
# Contract IO & Validation
# -------------------------
def validate_contract_obj(contract: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Validate a contract via the jsonschema-backed FluidSchemaManager.

    Falls back to a minimal required-field check if the schema manager
    is unavailable for any reason.
    """
    try:
        schema_manager = _imp("fluid_build.schema_manager")
        manager = schema_manager.FluidSchemaManager()
        result = manager.validate_contract(contract)
        if result.is_valid:
            return True, None
        return False, "\n".join(result.errors) or "contract validation failed"
    except Exception as e:
        LOG.warning("schema_manager_unavailable", extra={"error": str(e)})

    # Minimal baseline
    required = ["fluidVersion", "kind", "id", "name", "metadata"]
    for k in required:
        if k not in contract:
            return False, f"missing required top-level field '{k}'"
    return True, None


# -------------------------
# Command implementations
# -------------------------
def cmd_validate_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    ok, err = validate_contract_obj(contract)
    if not ok:
        logger.error("validate_failed", extra={"error": err})
        console_error(f"Validation failed: {err}")
        return 2
    logger.info("validate_ok")
    return 0


def _register_unavailable(
    sp: argparse._SubParsersAction, command: str, help_text: str, module: str, exc: BaseException
) -> None:
    """Register *command* so that running it says why it cannot run.

    Called when the command's own module failed to import. The fallbacks that
    stood in here used to plan through ``fluid_build.planner`` and draw the
    graph through ``fluid_build.visualize``. Neither module exists, so the
    stand-ins wrote a made-up plan (and ``apply`` applied it) or a two-node
    graph, and exited 0 on a broken install.

    The parser accepts any arguments (no option prefix is recognised), so a
    call carrying the real command's flags still reaches the error below
    instead of an argparse usage message.
    """
    reason = f"{type(exc).__name__}: {exc}"
    p = sp.add_parser(
        command,
        help=f"{help_text} (unavailable: fluid_build.cli.{module} failed to import)",
        prefix_chars="\x01",
        add_help=False,
    )
    p.add_argument("argv", nargs="*")

    def _unavailable(args: argparse.Namespace, logger: logging.Logger) -> int:
        raise CLIError(
            1,
            "command_unavailable",
            {"command": command, "module": f"fluid_build.cli.{module}", "error": reason},
        )

    p.set_defaults(cmd=command, func=_unavailable)


# -------------------------
# Parser wiring
# -------------------------


def _try_register(
    sp: argparse._SubParsersAction, module_name: str, profile_name: str, *, method: str = "register"
) -> bool:
    """Import *module_name* from the cli package and call its register().

    Returns True on success. Respects build profiles and swallows ImportError.
    """
    if not is_command_enabled(profile_name):
        return False
    try:
        mod = importlib.import_module(f".{module_name}", __package__)
        getattr(mod, method)(sp)
        return True
    except ImportError as e:
        LOG.debug("%s_module_unavailable: %s", module_name, e)
        return False


# ────────────────────────────────────────────────────────────────────
# Lazy CLI-command plugin loading (``fluid_build.commands`` group)
# ────────────────────────────────────────────────────────────────────
# Third-party packages register extra ``fluid <name>`` subcommands via the
# ``fluid_build.commands`` entry-point group. Importing every such plugin
# eagerly during ``build_parser()`` drags any heavy module-scope import a
# plugin happens to have (e.g. ``jsonschema``) onto the ``fluid --help`` cold
# path and blows the startup budget (tests/perf/test_startup_budget.py).
#
# We adapt Click's ``LazyGroup`` recipe (Pallets "Complex Applications" docs:
# list command NAMES without importing, import the implementation only inside
# ``get_command``) to argparse. argparse has no lazy ``get_command`` hook, so
# we peek ``sys.argv`` to decide — at parser-build time — whether the invoked
# command is (plausibly) a plugin command, and only THEN import + register the
# real subparser. ``--help`` / a core command / no command all stay fully
# lazy: zero plugin imports. (See also dbt-core#1090 — dbt lazy-loads adapters
# for the same ``--help`` / ``debug`` startup win.)

# Global options that consume a following value token — used by
# ``_requested_command`` to step past ``--provider gcp`` etc. when locating
# the invoked subcommand. Kept in sync with the global options declared in
# ``cli/__init__.py::build_parser``.
_GLOBAL_VALUE_OPTS = frozenset(
    {"--log-level", "--log-file", "--provider", "--project", "--region", "--config-dir"}
)


# The global options that take no value; with ``_GLOBAL_VALUE_OPTS`` this is every
# long option ``build_parser`` declares, which lets an abbreviation be resolved.
_GLOBAL_FLAG_OPTS = frozenset(
    {"--no-color", "--version", "--profile", "--health-check", "--stats", "--safe-mode", "--debug"}
)


def _takes_separate_value(tok: str) -> bool:
    """True when global option *tok* is followed by its value as the next token.

    argparse accepts an unambiguous abbreviation (``--prov gcp``), so one is
    resolved here too; reading it as a flag would take ``gcp`` for the command.
    """
    if "=" in tok or not tok.startswith("--"):
        return False
    if tok in _GLOBAL_VALUE_OPTS:
        return True
    matches = [o for o in _GLOBAL_VALUE_OPTS | _GLOBAL_FLAG_OPTS if o.startswith(tok)]
    return len(matches) == 1 and matches[0] in _GLOBAL_VALUE_OPTS


def _requested_command(argv: List[str]) -> Optional[str]:
    """Return the invoked subcommand name from *argv*, or ``None``.

    Steps past global options (and their values) to the first bare positional
    token — the subcommand. Returns ``None`` for help/version/no-command so the
    ``--help`` cold path never triggers a plugin import.
    """
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok in ("-h", "--help", "help", "--version"):
            return None
        if tok.startswith("-"):
            # Value-taking global option with a separate value → skip both.
            i += 2 if _takes_separate_value(tok) else 1
            continue
        return tok
    return None


def _command_plugin_entry_points() -> List[Any]:
    """Return the allowed ``fluid_build.commands`` entry points (names only).

    Reads entry-point metadata; it never imports plugin code. Each entry is
    gated by the operator allow/block policy (``is_allowed``) BEFORE it can be
    loaded, consistent with the plugin trust boundary.
    """
    import importlib.metadata as _md

    try:
        eps = _md.entry_points(group="fluid_build.commands")
    except TypeError:
        # Python < 3.10 returned a dict of groups, not a kwarg-filtered iter.
        eps = _md.entry_points().get("fluid_build.commands", [])
    from fluid_build.plugin_manager import is_allowed

    allowed: List[Any] = []
    for ep in eps:
        if is_allowed(ep.name):
            allowed.append(ep)
        else:
            LOG.debug("CLI command plugin %s skipped by allow/block policy", ep.name)
    return allowed


def _adapt_plugin_command_func(fn: Any) -> Any:
    """Wrap a plugin ``func`` to the CLI's ``func(args, logger)`` convention.

    The dispatcher (``ProductionCLI._execute_command``) always calls
    ``args.func(args, logger)``. Many third-party command plugins follow the
    one-arg ``func(args)`` shape (Click-style ``run(args)``), which otherwise
    crashes with "takes 1 positional argument but 2 were given". This adapter
    bridges both conventions by inspecting the target's arity.
    """
    import inspect

    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        params = []
    accepts_two = any(p.kind == p.VAR_POSITIONAL for p in params) or (
        sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)) >= 2
    )

    def _dispatch(args: argparse.Namespace, logger: logging.Logger) -> Any:
        return fn(args, logger) if accepts_two else fn(args)

    _dispatch.__fluid_adapted__ = True  # type: ignore[attr-defined]
    return _dispatch


def _wrap_plugin_parser_funcs(parser: argparse.ArgumentParser) -> None:
    """Recursively adapt ``func`` defaults on a plugin-registered subparser tree.

    Walks the parser and every nested ``_SubParsersAction`` choice so that a
    ``func`` set on any leaf is bridged to the CLI dispatch convention (see
    ``_adapt_plugin_command_func``).
    """
    defaults = getattr(parser, "_defaults", {})
    fn = defaults.get("func")
    if callable(fn) and not getattr(fn, "__fluid_adapted__", False):
        parser.set_defaults(func=_adapt_plugin_command_func(fn))
    for action in getattr(parser, "_actions", []):
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _wrap_plugin_parser_funcs(sub)


def _register_command_plugins(
    sp: argparse._SubParsersAction, argv: Optional[List[str]] = None
) -> None:
    """Register external ``fluid_build.commands`` plugins LAZILY.

    A plugin's module + ``register()`` is imported only when its command is
    actually being invoked, detected by peeking ``sys.argv``. On the
    ``fluid --help`` / core-command / no-command paths NO plugin is imported —
    installed plugin names are surfaced separately by ``print_main_help`` from
    entry-point metadata. Discovery and per-plugin load failures are fully
    isolated and logged by exception TYPE only, so a broken plugin can never
    crash the CLI and a credential-shaped substring can never leak.
    """
    try:
        eps = _command_plugin_entry_points()
    except Exception as e:  # noqa: BLE001 - discovery must never break the CLI
        LOG.warning("CLI plugin discovery failed: %s", type(e).__name__)
        return
    if not eps:
        return

    core_names = set(sp.choices)
    requested = _requested_command(sys.argv[1:] if argv is None else argv)
    # Import the real subparsers ONLY when a non-core command is being invoked
    # (i.e. plausibly a plugin command). ``--help`` / a core command / no
    # command all keep the cold path free of plugin imports.
    if not (requested and requested not in core_names):
        return

    for ep in eps:
        before = set(sp.choices)
        try:
            ep.load()(sp)
        except Exception as e:  # noqa: BLE001 - isolate a bad plugin; type only
            # Type-only — never interpolate plugin-supplied exception text.
            LOG.warning("Failed to load CLI plugin %s: %s", ep.name, type(e).__name__)
            continue
        # Bridge any newly-registered command funcs to ``func(args, logger)``.
        for name in set(sp.choices) - before:
            _wrap_plugin_parser_funcs(sp.choices[name])


# ────────────────────────────────────────────────────────────────────
# Lazy CORE commands
# ────────────────────────────────────────────────────────────────────
# ``fluid --help`` and every ``fluid <cmd>`` used to import EVERY command
# module (~800 modules, pydantic, rich, yaml, the IaC plugins, ...) just to
# build a parser whose subcommand it then mostly ignored. The core commands
# now follow the same recipe as the plugin commands above -- Click's
# ``LazyGroup`` ("Complex Applications" docs): the command NAMES and their
# one-line help come from a static table, and a command's module is imported
# only when it is the one invoked.
#
# argparse has no lazy ``get_command`` hook, so ``register_core_commands`` takes
# the argv being parsed: every command gets a cheap stub subparser (name +
# aliases + help, which is all ``fluid --help`` and the "invalid choice" error
# print), and only the command ``_requested_command`` finds in argv gets its
# real ``register()``. With ``argv=None`` the FULL eager tree is built
# (``describe``, the docs drift gate and the tests enumerate it).
#
# Click's docs warn that lazy loading hides import bugs: a command whose module
# fails to import is invisible until someone runs it. Two guards answer that:
# ``tests/cli/test_lazy_core_commands.py`` runs ``<cmd> --help`` for every
# command, and compares this table with the tree the eager path builds, so it
# cannot drift from the real registrations.


class CoreCommand(NamedTuple):
    """One ``cli/<module>.py`` and the subcommands its ``register()`` adds."""

    module: str
    profile: str  # build-profile gate name (see ``is_command_enabled``)
    commands: Tuple[Tuple[str, Any, Tuple[str, ...]], ...]  # (name, help, aliases)
    method: str = "register"
    # Set when a failed import is answered with a stand-in instead of silence:
    # "validate" = the minimal validator; any other text = ``_register_unavailable``.
    fallback: Optional[str] = None


def _cmd(name: str, help_text: Any, *aliases: str) -> Tuple[str, Any, Tuple[str, ...]]:
    return (name, help_text, tuple(aliases))


# Order is the order ``fluid --help`` lists the commands in.
CORE_COMMANDS: Tuple[CoreCommand, ...] = (
    CoreCommand("init", "init", (_cmd("init", "🚀 Create new FLUID project (smart setup)"),)),
    CoreCommand(
        "demo",
        "demo",
        (_cmd("demo", "🚀 Try FLUID in ~30s — zero setup, no API key, local DuckDB"),),
    ),
    CoreCommand(
        "status", "status", (_cmd("status", "📋 One-page summary of the current FLUID product"),)
    ),
    CoreCommand(
        "import_cmd",
        "import",
        (
            _cmd(
                "import",
                "📥 Import an existing project into FLUID (dbt/Terraform/SQL or Meltano/Airbyte/dlt/Singer)",
            ),
        ),
    ),
    CoreCommand(
        "skills_cmd",
        "skills",
        (_cmd("skills", "Manage industry skills for agentic project knowledge"),),
    ),
    CoreCommand(
        "plugins_cmd",
        "plugins",
        (_cmd("plugins", "List installed FLUID plugins by role and their allow/block status"),),
    ),
    CoreCommand(
        "exporters_cmd",
        "exporters",
        (_cmd("exporters", "List spec exporters (contract → ODPS / ODCS standards)"),),
    ),
    CoreCommand(
        "validate",
        "validate",
        (_cmd("validate", "Validate a FLUID contract against official schemas"),),
        fallback="validate",
    ),
    CoreCommand(
        "plan",
        "plan",
        (_cmd("plan", "Generate execution plan from FLUID contract"),),
        fallback="Create an execution plan for a contract",
    ),
    CoreCommand(
        "ship",
        "ship",
        (_cmd("ship", "Run validate → bundle → plan → apply in sequence (the happy path)"),),
    ),
    CoreCommand(
        "apply",
        "apply",
        (_cmd("apply", "Apply a plan or contract against providers with full orchestration"),),
        fallback="Apply a contract",
    ),
    CoreCommand(
        "viz_graph",
        "graph",
        (
            _cmd(
                "viz-graph", "Generate beautiful, interactive data product lineage and build graphs"
            ),
        ),
        fallback="Render the contract as a graph",
    ),
    CoreCommand(
        "verify", "verify", (_cmd("verify", "Verify deployed resources match contract schema"),)
    ),
    CoreCommand("viz_plan", "viz-plan", (_cmd("viz-plan", argparse.SUPPRESS),)),
    CoreCommand(
        "contract_tests",
        "contract-tests",
        (_cmd("contract-tests", "Check a contract's exposed schemas against a saved baseline"),),
    ),
    CoreCommand(
        "contract_validation",
        "contract-validation",
        (
            _cmd(
                "contract-validation", "Validate contract compliance with actual deployed resources"
            ),
        ),
    ),
    CoreCommand(
        "test",
        "test",
        (_cmd("test", "Test a FLUID contract against live data (schema, quality, SLAs)"),),
    ),
    CoreCommand(
        "scaffold_ci", "scaffold-ci", (_cmd("scaffold-ci", "Generate CI pipeline (GitLab/GitHub)"),)
    ),
    CoreCommand(
        "scaffold_ide",
        "scaffold-ide",
        (
            _cmd(
                "scaffold-ide",
                "Generate agentic-IDE config (.kiro/, .cursor/rules/, .claude/, .clinerules/, or generic AGENTS.md + mcp.json) and install the FLUID Agent Skills so any agentic IDE can drive forge-cli.",
            ),
        ),
    ),
    CoreCommand(
        "scaffold_composer",
        "scaffold-composer",
        (_cmd("scaffold-composer", "Generate Cloud Composer DAG from contract"),),
    ),
    CoreCommand(
        "generate_airflow",
        "generate-airflow",
        (_cmd("generate-airflow", "Generate Airflow DAG from FLUID contract (0.7.0+)"),),
    ),
    CoreCommand(
        "generate", "generate", (_cmd("generate", "Generate artifacts from FLUID contract"),)
    ),
    CoreCommand(
        "export",
        "export",
        (_cmd("export", "Export FLUID contract as orchestration code (DAG/pipeline/flow)"),),
    ),
    CoreCommand(
        "docs_build",
        "docs",
        (_cmd("docs", "Generate a static catalog of contracts (index + per-contract pages)"),),
    ),
    CoreCommand(
        "doctor",
        "doctor",
        (_cmd("doctor", "Run built-in health checks and optional extended diagnostics"),),
    ),
    CoreCommand("provider_cmds", "providers", (_cmd("providers", "List discoverable providers"),)),
    CoreCommand(
        "provider_init",
        "provider-init",
        (_cmd("provider-init", "Scaffold a new FLUID provider package"),),
    ),
    CoreCommand(
        "version_cmd", "version", (_cmd("version", "Show version and system information"),)
    ),
    CoreCommand("roadmap_cmd", "roadmap", (_cmd("roadmap", "Show the staged forge roadmap"),)),
    CoreCommand("mcp", "mcp", (_cmd("mcp", "Serve staged forge tools over MCP stdio"),)),
    CoreCommand(
        "export_opds",
        "export-opds",
        (
            _cmd(
                "export-opds",
                "[deprecated alias] Export FLUID → LF/ODPI ODPS v4.1 JSON. Prefer `fluid generate standard --format odps-v4.1`.",
            ),
        ),
    ),
    CoreCommand(
        "opds",
        "opds",
        (
            _cmd(
                "odps",
                "Bitol ODPS v1.0.0 (default) or ODPS v4.1 (LF/ODPI) — export, import, validate",
                "opds",
            ),
        ),
    ),
    CoreCommand(
        "odps_standard",
        "odps-standard",
        (_cmd("odps-bitol", "ODPS-Bitol (Bitol.io Data Product Standard for Entropy Data)"),),
    ),
    CoreCommand(
        "odcs", "odcs", (_cmd("odcs", "ODCS (Open Data Contract Standard - Bitol.io) commands"),)
    ),
    CoreCommand(
        "datamesh_manager",
        "datamesh-manager",
        (_cmd("datamesh-manager", "Publish to Entropy Data / Data Mesh Manager", "dmm"),),
        method="add_parser",
    ),
    CoreCommand("ai_setup", "ai", (_cmd("ai", "Configure AI / LLM settings for Forge Copilot"),)),
    CoreCommand("forge", "forge", (_cmd("forge", "🔨 Create a new data product with AI Copilot"),)),
    CoreCommand("contract", "contract", (_cmd("contract", "Inspect / mutate FLUID contracts"),)),
    CoreCommand(
        "runs", "runs", (_cmd("runs", "Inspect acquisition run history (status / logs / diff)"),)
    ),
    CoreCommand(
        "retention", "retention", (_cmd("retention", "Retention sweep over the state directory"),)
    ),
    CoreCommand(
        "secrets",
        "secrets",
        (_cmd("secrets", "🔐 Manage acquisition-pipeline secrets (login / verify / rotate)"),),
    ),
    CoreCommand(
        "market",
        "market",
        (_cmd("market", "Discover data products from enterprise catalogs and marketplaces"),),
    ),
    CoreCommand(
        "policy",
        "policy",
        (
            _cmd(
                "policy",
                "Policy operations: check · compile · apply  (pipeline stage 8 + static lint)",
            ),
        ),
    ),
    CoreCommand(
        "policy_check",
        "policy-check",
        (_cmd("policy-check", "Validate contract against schema-driven policies"),),
    ),
    CoreCommand(
        "policy_compile",
        "policy-compile",
        (_cmd("policy-compile", "Compile accessPolicy → provider IAM bindings"),),
    ),
    CoreCommand(
        "policy_apply", "policy-apply", (_cmd("policy-apply", "Apply compiled IAM bindings"),)
    ),
    CoreCommand(
        "product_new", "product-new", (_cmd("product-new", "Bootstrap a new product skeleton"),)
    ),
    CoreCommand("auth", "auth", (_cmd("auth", "Provider authentication management"),)),
    CoreCommand("marketplace", "marketplace", (_cmd("marketplace", argparse.SUPPRESS),)),
    CoreCommand(
        "publish", "publish", (_cmd("publish", "Publish data products to enterprise catalogs"),)
    ),
    CoreCommand(
        "genie",
        "genie",
        (_cmd("genie", "Databricks Genie space over several data products (apply · remove)"),),
    ),
    CoreCommand(
        "bundle",
        "bundle",
        (_cmd("bundle", "Resolve $ref pointers and emit a single bundled contract"),),
    ),
    CoreCommand(
        "validate_artifacts",
        "validate-artifacts",
        (
            _cmd(
                "validate-artifacts", "Re-verify MANIFEST + per-format validators on stage-3 output"
            ),
        ),
    ),
    CoreCommand(
        "schedule_sync",
        "schedule-sync",
        (_cmd("schedule-sync", "Push DAG files to a scheduler (pipeline stage 11, Path A)"),),
    ),
    CoreCommand(
        "verify_signature",
        "verify-signature",
        (_cmd("verify-signature", "Verify a Sigstore-cosign-signed tgz bundle"),),
    ),
    CoreCommand(
        "rollback",
        "rollback",
        (_cmd("rollback", "Restore a data product from a prior apply --mode replace snapshot"),),
    ),
    CoreCommand(
        "split",
        "split",
        (_cmd("split", "Split a flat contract into composable fragments with $ref pointers"),),
    ),
    CoreCommand(
        "diff",
        "diff",
        (_cmd("diff", "Compare contract-vs-live (drift) or contract-vs-contract (version)"),),
    ),
    CoreCommand("context", "config", (_cmd("config", "Get/Set default provider/project/region"),)),
    CoreCommand(
        "product_add",
        "product-add",
        (_cmd("product-add", "Add a source/exposure/dq rule to an existing contract"),),
    ),
    CoreCommand(
        "pipeline_generator",
        "generate-pipeline",
        (_cmd("generate-pipeline", "🚀 Generate CI/CD pipeline templates for FLUID projects"),),
    ),
    CoreCommand(
        "ide", "ide", (_cmd("ide", "💻 IDE integration and developer experience features"),)
    ),
    CoreCommand(
        "workspace",
        "workspace",
        (_cmd("workspace", "🤝 Team workspace and collaboration features"),),
    ),
    CoreCommand(
        "stats", "stats", (_cmd("stats", "Aggregate cost / token usage across forge runs"),)
    ),
    CoreCommand(
        "agents_cmd", "agents", (_cmd("agents", "Manage forge agent runs (list / show / prune)"),)
    ),
    CoreCommand(
        "describe_cmd", "describe", (_cmd("describe", "Describe this forge-cli installation"),)
    ),
)


def _core_command_names(entry: CoreCommand) -> List[str]:
    return [n for name, _h, aliases in entry.commands for n in (name, *aliases)]


def _register_stubs(sp: argparse._SubParsersAction, entry: CoreCommand) -> None:
    """Register name + aliases + help only, with no import of ``entry.module``.

    A stub is never meant to run: ``_requested_command`` hands the invoked
    command its real parser. Should one be reached anyway (a global-option
    spelling the argv peek cannot read), it accepts any arguments, marks the
    namespace ``_lazy_stub`` and ``cli.main`` re-parses with the real parser.
    """
    for name, help_text, aliases in entry.commands:
        p = sp.add_parser(
            name,
            help=help_text,
            aliases=list(aliases),
            add_help=False,
            prefix_chars="\x01",
        )
        p.add_argument("argv", nargs="*")
        p.set_defaults(_lazy_stub=True)


def _register_entry(sp: argparse._SubParsersAction, entry: CoreCommand) -> None:
    """Import ``entry.module`` and run its ``register()`` (the eager path)."""
    if not is_command_enabled(entry.profile):
        return
    if entry.fallback is None:
        _try_register(sp, entry.module, entry.profile, method=entry.method)
        return
    try:
        mod = importlib.import_module(f".{entry.module}", __package__)
        getattr(mod, entry.method)(sp)
    except ImportError as e:
        if entry.fallback == "validate":
            LOG.debug("enhanced_validate_unavailable_using_fallback: %s", e)
            v = sp.add_parser("validate", help="Validate a FLUID contract")
            v.add_argument("contract")
            v.add_argument("--env")
            v.set_defaults(func=cmd_validate_run)
        else:
            LOG.warning("%s_module_unavailable: %s", entry.module, e)
            _register_unavailable(sp, entry.commands[0][0], entry.fallback, entry.module, e)


def register_core_commands(
    sp: argparse._SubParsersAction, argv: Optional[List[str]] = None
) -> None:
    """Register the core commands, then the (lazy) command plugins.

    ``argv=None`` registers every command for real. With an argv, only the
    invoked command is real and the rest are stubs (see the block comment above).
    """
    requested = _requested_command(argv) if argv is not None else None
    for entry in CORE_COMMANDS:
        if argv is None or requested in _core_command_names(entry):
            _register_entry(sp, entry)
        elif is_command_enabled(entry.profile):
            _register_stubs(sp, entry)

    # ────────────────────────────────────────────────────────────────────
    # External CLI plugins — discovered via Python entry-points, loaded LAZILY.
    #
    # Any installed package can register an additional ``fluid <name>``
    # subcommand by declaring an entry-point in its ``pyproject.toml``::
    #
    #     [project.entry-points."fluid_build.commands"]
    #     my-cmd = "my_pkg.cli:register"
    #
    # The referenced callable must accept the argparse subparser group and call
    # ``add_parser`` on it. To keep a heavy third-party module-scope import
    # (e.g. ``jsonschema``) off the ``fluid --help`` / ``build_parser()`` cold
    # path, the plugin module is imported (and its ``register()`` called) ONLY
    # when its command is actually being invoked — see
    # ``_register_command_plugins``. Governance, fail-isolation, and type-only
    # logging are preserved there.
    # ────────────────────────────────────────────────────────────────────
    _register_command_plugins(sp, argv)
