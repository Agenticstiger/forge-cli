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

# fluid_build/cli/bootstrap.py
from __future__ import annotations

import argparse
import atexit
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.cli.console import error as console_error

# Import shared utilities
from ._common import build_provider, load_contract_with_overlay

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
# Planner + Fallback plan
# -------------------------
def plan_contract(contract: Dict[str, Any], provider_name: Optional[str]) -> Dict[str, Any]:
    """Use fluid_build.planner.plan_actions if available; else a reasonable fallback plan."""
    try:
        planner = _imp("fluid_build.planner")
        plan_actions = getattr(planner, "plan_actions", None)
        if plan_actions:
            return plan_actions(contract, provider_name)  # type: ignore[no-any-return]
    except Exception as e:
        LOG.warning("planner_unavailable_using_fallback", extra={"error": str(e), "actions": 2})

    # Fallback: simple, deterministic actions
    exposes = contract.get("exposes", []) or []
    target = exposes[0] if exposes else {}
    location = target.get("location") or {}
    fmt = (location.get("format") if isinstance(location, dict) else None) or ""

    actions: List[Dict[str, Any]] = []
    # Basic dataset + table ensures
    props = (location.get("properties", {}) or {}) if isinstance(location, dict) else {}
    dataset = props.get("dataset")
    table = props.get("table")
    if dataset:
        actions.append({"op": "ensure_dataset", "name": dataset})
    if table:
        actions.append(
            {
                "op": "ensure_table",
                "dataset": dataset,
                "table": table,
                "schema": target.get("schema"),
            }
        )

    # If local/file, add a demo artifact copy to show something tangible
    if fmt in ("file", "local_file"):
        out_dir = Path("runtime/out")
        out_dir.mkdir(parents=True, exist_ok=True)
        actions.append(
            {
                "op": "copy",
                "src": "demo_artifact.csv",
                "dst": str(out_dir / "demo_artifact.csv"),
                "optional": True,
            }
        )

    return {"actions": actions, "provider": (provider_name or "unknown")}


# -------------------------
# Small IO helpers
# -------------------------
def _write_json(path: str, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _print_json(obj: Any) -> None:
    try:
        # Rich pretty JSON if available (stderr console kept by main)
        from rich.console import Console  # type: ignore

        Console().print_json(data=obj)
    except Exception:
        sys.stdout.write(json.dumps(obj, indent=2) + "\n")


# -------------------------
# Export / Render helpers
# -------------------------
def _provider_supports_render(provider: Any) -> bool:
    try:
        caps = provider.capabilities()
        return bool(caps.get("render"))
    except Exception:
        # Fallback heuristic: exporters usually identify as odps/opds
        return getattr(provider, "name", "").lower() in {"odps", "opds"}


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


def cmd_plan_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    plan = plan_contract(contract, getattr(args, "provider", None))
    if not plan or not isinstance(plan, dict):
        logger.warning(
            "planner_fallback_stub",
            extra={"actions": len(plan.get("actions", [])) if isinstance(plan, dict) else 0},
        )
    _write_json(args.out, plan)
    logger.info("plan_ok", extra={"out": args.out, "actions": len(plan.get("actions", []))})
    return 0


def cmd_apply_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    If the provider is an exporter (supports render()), treat apply as an export.
    Otherwise: plan → provider.apply(actions). Print to stdout when --out -.
    """
    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    provider = build_provider(
        getattr(args, "provider", None),
        getattr(args, "project", None),
        getattr(args, "region", None),
        logger,
    )

    # Exporter path (e.g., odps/opds)
    if _provider_supports_render(provider):
        out = getattr(args, "out", "-")
        result = provider.render(contract, out=out if out else "-", fmt=None)
        if out == "-" or not out:
            _print_json(result)
            logger.info("apply_ok", extra={"result": "stdout"})
        else:
            _write_json(out, result)
            logger.info("apply_ok", extra={"result": "written", "path": out})
        return 0

    # Normal plan+apply
    plan = plan_contract(contract, getattr(args, "provider", None))
    actions = plan.get("actions", [])

    # Prefer provider.apply(actions)
    if hasattr(provider, "apply"):
        apply_result = provider.apply(actions)  # expected ApplyResult or dict
        # Normalize for stdout write
        if hasattr(apply_result, "to_json"):
            payload = json.loads(apply_result.to_json())
        else:
            payload = apply_result
    else:
        # Legacy minimal protocol
        applied = 0
        for i, a in enumerate(actions):
            if a.get("op") == "copy":
                try:
                    dst = Path(a["dst"])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src = Path(a["src"])
                    if not src.exists():
                        dst.write_text("demo,data\n", encoding="utf-8")
                    else:
                        dst.write_bytes(src.read_bytes())
                    applied += 1
                except Exception as e:
                    logger.error("local_copy_failed", extra={"i": i, "error": str(e)})
        payload = {
            "provider": getattr(provider, "name", "local"),
            "applied": applied,
            "failed": len(actions) - applied,
            "results": [],
        }

    out = getattr(args, "out", None)
    if out and out != "-":
        _write_json(out, payload)
        logger.info("apply_ok", extra={"result": "written", "path": out})
    else:
        _print_json(payload)
        logger.info("apply_ok", extra={"result": "stdout"})
    return 0


def cmd_graph_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    # Try to import a graph helper; otherwise write a trivial DOT
    try:
        graph = _imp("fluid_build.visualize")
        emit_dot = getattr(graph, "emit_contract_dot", None)
        if emit_dot:
            contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
            dot = emit_dot(contract)  # type: ignore[call-arg]
            if args.out == "-":
                sys.stdout.write(dot)
            else:
                Path(args.out).write_text(dot, encoding="utf-8")
            logger.info("graph_ok", extra={"out": args.out})
            return 0
    except Exception as e:
        logger.warning("graph_helper_missing", extra={"error": str(e)})

    # fallback DOT
    dot = 'digraph G { rankdir=LR; "contract" -> "expose"; }'
    if args.out == "-":
        sys.stdout.write(dot)
    else:
        Path(args.out).write_text(dot, encoding="utf-8")
    logger.info("graph_ok_fallback", extra={"out": args.out})
    return 0


def cmd_visualize_plan_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Build a quick plan graph (PNG + HTML) from a CONTRACT (more useful than DOT-in/DOT-out).
    Requires graphviz `dot`. Falls back to minimal HTML if missing.
    """
    try:
        import graphviz  # noqa: F401

        has_dot = True
    except Exception as e:
        logger.warning("graphviz_missing", extra={"error": str(e)})
        has_dot = False

    # Build actions from contract to be consistent with provider behavior
    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    plan = plan_contract(contract, getattr(args, "provider", None))
    actions = plan.get("actions", [])

    # Emit DOT
    dot_lines = [
        "digraph plan {",
        "  rankdir=LR;",
        '  node [shape=box, style="rounded,filled", fillcolor="#eef2ff"];',
    ]
    for i, a in enumerate(actions):
        label = a.get("op", "op")
        dot_lines.append(f"  n{i} [label={json.dumps(label)} tooltip={json.dumps(json.dumps(a))}];")
        if i > 0:
            dot_lines.append(f"  n{i - 1} -> n{i};")
    dot_lines.append("}")
    dot_src = "\n".join(dot_lines)

    out_dir = Path(args.out or "runtime/plan_viz")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.dot").write_text(dot_src, encoding="utf-8")

    if has_dot:
        try:
            from subprocess import PIPE, run

            png = out_dir / "plan.png"
            run(
                ["dot", "-Tpng", str(out_dir / "plan.dot"), "-o", str(png)],
                check=True,
                stdout=PIPE,
                stderr=PIPE,
            )
            html = out_dir / "index.html"
            html.write_text(
                "<!doctype html><meta charset='utf-8'><title>FLUID Plan</title>"
                "<h1>Plan</h1><p><img src='plan.png' alt='plan graph'></p>",
                encoding="utf-8",
            )
            logger.info("visualize_plan_ok", extra={"out": str(out_dir)})
            return 0
        except Exception as e:
            logger.error("visualize_plan_failed", extra={"error": str(e)})
            # fall through to minimal HTML

    # Minimal HTML fallback
    (out_dir / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>FLUID Plan</title>"
        "<h1>Plan DOT</h1><pre>" + dot_src + "</pre>",
        encoding="utf-8",
    )
    logger.info("visualize_plan_ok_fallback", extra={"out": str(out_dir)})
    return 0


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


def register_core_commands(sp: argparse._SubParsersAction) -> None:
    # --- Core commands (with inline fallbacks for backwards compat) ---

    # 🚀 FLUID Init - Universal Onboarding
    _try_register(sp, "init", "init")

    # 🚀 FLUID Demo - zero-setup working example (customer-360)
    _try_register(sp, "demo", "demo")

    # 📋 FLUID Status - one-page summary of the current product (slice UX-A)
    _try_register(sp, "status", "status")

    # 📥 FLUID Import - migrate dbt/Terraform/SQL projects to FLUID
    _try_register(sp, "import_cmd", "import")

    # 🎯 Industry Skills
    _try_register(sp, "skills_cmd", "skills")

    # 🔌 Plugins — inspect installed plugins by role + allow/block status
    _try_register(sp, "plugins_cmd", "plugins")

    # validate (enhanced → fallback)
    if is_command_enabled("validate"):
        try:
            from . import validate

            validate.register(sp)
        except ImportError as e:
            LOG.debug("enhanced_validate_unavailable_using_fallback: %s", e)
            v = sp.add_parser("validate", help="Validate a FLUID contract")
            v.add_argument("contract")
            v.add_argument("--env")
            v.set_defaults(func=cmd_validate_run)

    # plan (enhanced → fallback)
    if is_command_enabled("plan"):
        try:
            from . import plan

            plan.register(sp)
        except ImportError as e:
            LOG.debug("enhanced_plan_unavailable_using_fallback: %s", e)
            pl = sp.add_parser("plan", help="Create an execution plan for a contract")
            pl.add_argument("contract")
            pl.add_argument("--env")
            pl.add_argument("--out", required=True)
            pl.set_defaults(func=cmd_plan_run)

    # ship (UX hardening — validate → bundle → plan → apply macro)
    _try_register(sp, "ship", "ship")

    # apply (enhanced → fallback)
    if is_command_enabled("apply"):
        try:
            from . import apply

            apply.register(sp)
        except ImportError as e:
            LOG.debug("enhanced_apply_unavailable_using_fallback: %s", e)
            ap = sp.add_parser(
                "apply", help="Apply a contract (or export if provider supports render)"
            )
            ap.add_argument("contract")
            ap.add_argument("--env")
            ap.add_argument("--out", default="-")
            ap.set_defaults(func=cmd_apply_run)

    # viz-graph (enhanced → fallback)
    if is_command_enabled("graph"):
        try:
            from . import viz_graph

            viz_graph.register(sp)
        except ImportError as e:
            LOG.debug("enhanced_viz_graph_unavailable_using_fallback: %s", e)
            gr = sp.add_parser("graph", help="Emit Graphviz DOT of the contract DAG")
            gr.add_argument("contract")
            gr.add_argument("--env")
            gr.add_argument("--out", default="-")
            gr.set_defaults(func=cmd_graph_run)

    # --- Simple registrations (profile-gated via _try_register) ---
    # ``fluid execute`` was deleted when build_runners landed. Its engine code
    # moved to ``fluid_build.build_runners``; ``fluid apply --build <id>`` is
    # the canonical entry point.
    _try_register(sp, "verify", "verify")
    _try_register(sp, "viz_plan", "viz-plan")
    _try_register(sp, "contract_tests", "contract-tests")
    _try_register(sp, "contract_validation", "contract-validation")
    _try_register(sp, "test", "test")
    _try_register(sp, "scaffold_ci", "scaffold-ci")
    _try_register(sp, "scaffold_ide", "scaffold-ide")
    _try_register(sp, "scaffold_composer", "scaffold-composer")
    _try_register(sp, "generate_airflow", "generate-airflow")
    _try_register(sp, "generate", "generate")
    _try_register(sp, "export", "export")
    _try_register(sp, "docs_build", "docs")
    _try_register(sp, "doctor", "doctor")
    _try_register(sp, "provider_cmds", "providers")
    _try_register(sp, "provider_init", "provider-init")
    _try_register(sp, "version_cmd", "version")
    _try_register(sp, "roadmap_cmd", "roadmap")
    _try_register(sp, "memory_cmd", "memory")
    _try_register(sp, "mcp", "mcp")
    _try_register(sp, "export_opds", "export-opds")
    _try_register(sp, "opds", "opds")
    _try_register(sp, "odps_standard", "odps-standard")
    _try_register(sp, "odcs", "odcs")
    _try_register(sp, "datamesh_manager", "datamesh-manager", method="add_parser")
    _try_register(sp, "ai_setup", "ai")
    _try_register(sp, "forge", "forge")
    _try_register(sp, "contract", "contract")
    # Day-2 ops surface for source-aligned acquisition contracts.
    # `fluid runs {status,logs,diff}` for run-record introspection;
    # `fluid retention sweep` for state-root cleanup. Kept under their own
    # umbrellas so the existing top-level `status` / `doctor` / `auth`
    # commands aren't shadowed.
    _try_register(sp, "runs", "runs")
    _try_register(sp, "retention", "retention")
    _try_register(sp, "secrets", "secrets")
    _try_register(sp, "market", "market")
    # ``fluid policy {check,compile,apply}`` — unified umbrella that
    # groups the three policy verbs under one subcommand. The legacy
    # hyphenated top-level forms below remain registered for
    # backward-compat (one-release deprecation window); both surfaces
    # share the same ``_add_arguments`` helpers in the underlying
    # policy_* modules, so options stay in sync automatically.
    _try_register(sp, "policy", "policy")
    _try_register(sp, "policy_check", "policy-check")
    _try_register(sp, "policy_compile", "policy-compile")
    _try_register(sp, "policy_apply", "policy-apply")
    _try_register(sp, "product_new", "product-new")
    _try_register(sp, "auth", "auth")
    _try_register(sp, "marketplace", "marketplace")  # deprecated — use market --blueprints
    _try_register(sp, "publish", "publish")
    _try_register(sp, "bundle", "bundle")
    _try_register(sp, "validate_artifacts", "validate-artifacts")
    _try_register(sp, "schedule_sync", "schedule-sync")
    _try_register(sp, "verify_signature", "verify-signature")
    _try_register(sp, "rollback", "rollback")
    # The hidden ``fluid compile`` alias was removed when the 11-stage
    # pipeline landed. Users running ``fluid compile`` now get the standard
    # argparse "invalid choice" error, which is the desired signal.
    _try_register(sp, "split", "split")
    _try_register(sp, "diff", "diff")
    _try_register(sp, "context", "config")  # canonical: ``fluid config``
    _try_register(sp, "product_add", "product-add")
    _try_register(sp, "pipeline_generator", "generate-pipeline")
    # copilot removed — AI is built into forge
    _try_register(sp, "ide", "ide")
    _try_register(sp, "workspace", "workspace")
    _try_register(sp, "stats", "stats")
    # ``fluid agents`` — manage forge agent runs (list / show / prune).
    # New in the resumability landing; mirrors ``stats`` registration.
    _try_register(sp, "agents_cmd", "agents")
    _try_register(sp, "describe_cmd", "describe")

    # ────────────────────────────────────────────────────────────────────
    # External CLI plugins — discovered via Python entry-points.
    #
    # Any installed package can register an additional ``fluid <name>``
    # subcommand by declaring an entry-point in its ``pyproject.toml``::
    #
    #     [project.entry-points."fluid_build.commands"]
    #     my-cmd = "my_pkg.cli:register"
    #
    # The referenced callable must accept the argparse subparser group and
    # call ``add_parser`` on it. Failures are logged at WARNING level so a
    # broken plugin can never break ``fluid`` itself.
    # ────────────────────────────────────────────────────────────────────
    # Governance: each command plugin is gated by the operator allow/block
    # policy (``is_allowed``) BEFORE it is loaded, and load/discovery failures
    # are logged by exception TYPE only — plugin-supplied exception text never
    # reaches the log handler, so a credential-shaped substring cannot leak.
    try:
        import importlib.metadata as _md

        try:
            _eps = _md.entry_points(group="fluid_build.commands")
        except TypeError:
            # Python < 3.10 returned a dict of groups, not a kwarg-filtered iter.
            _eps = _md.entry_points().get("fluid_build.commands", [])
        from fluid_build.plugin_manager import is_allowed

        for _ep in _eps:
            if not is_allowed(_ep.name):
                LOG.debug("CLI command plugin %s skipped by allow/block policy", _ep.name)
                continue
            try:
                _ep.load()(sp)
            except Exception as _e:
                # Type-only — never interpolate plugin-supplied exception text.
                LOG.warning("Failed to load CLI plugin %s: %s", _ep.name, type(_e).__name__)
    except Exception as _e:
        LOG.warning("CLI plugin discovery failed: %s", type(_e).__name__)
