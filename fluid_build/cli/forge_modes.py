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

"""Mode handlers for `fluid forge`."""

from __future__ import annotations

__all__ = [
    "run_ai_copilot_mode",
    "run_domain_agent_mode",
    "run_guided_mode",
    "run_template_mode",
]


import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_interview import (
    InterviewQuestion,
    run_adaptive_copilot_interview,
)
from fluid_build.cli.forge_copilot_llm_providers import (
    PROVIDER_DISPLAY_NAMES,
    CopilotGenerationError,
    LlmConfig,
    build_llm_run_plan,
    check_llm_readiness,
    detect_provider_from_api_key,
    get_catalog_default,
    get_llm_provider,
    resolve_ollama_model,
    save_api_key_to_keyring,
)
from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context
from fluid_build.cli.forge_dialogs import (
    ask_confirmation,
    ask_dialog_question,
    ask_secret_text,
    print_dialog_status,
)
from fluid_build.cli.forge_ui import (
    print_assumptions_panel,
    print_copilot_intro_panel,
    print_copilot_recovery_panel,
    print_free_tier_guide,
    print_welcome_panel,
)

try:
    from rich.console import Console
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via non-Rich fallbacks
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


def _create_session_llm_config(
    console: Any,
    *,
    default_provider: str = "gemini",
    ask_dialog_question_fn: Callable[[Any, Any], Any] = ask_dialog_question,
    ask_secret_text_fn: Callable[..., Optional[str]] = ask_secret_text,
) -> Optional[LlmConfig]:
    """Collect a session-only LLM configuration via an API-key-first flow.

    The user pastes an API key and the provider is auto-detected from its
    format.  If detection fails, a short follow-up asks which provider the
    key belongs to.  Model and endpoint always use sensible provider defaults
    so the user never has to type them.
    """
    if console:
        console.print(
            "[dim]Paste an API key for OpenAI, Anthropic (Claude), or Google Gemini "
            "and I'll detect the provider automatically.\n"
            "Type [bold]ollama[/bold] if you want to use a local model instead.[/dim]"
        )

    api_key = ask_secret_text_fn(
        console,
        "API key for OpenAI / Anthropic / Gemini (or 'ollama')",
        required=True,
    )

    raw = (api_key or "").strip().lower()

    # --- Ollama shortcut ---
    if raw in ("ollama", "local", "ollama/local", "ollama (local)"):
        provider = get_llm_provider("ollama")
        model = resolve_ollama_model(os.environ)
        endpoint = provider.default_endpoint(model, os.environ)
        print_dialog_status(
            console,
            status="success",
            message=f"Using Ollama -- {model}",
        )
        return LlmConfig(provider="ollama", model=model, endpoint=endpoint, api_key=None)

    if not api_key:
        if console:
            print_dialog_status(
                console,
                status="error",
                message="A hosted provider needs an API key for this run.",
                detail="You can choose a different mode or try Ollama for a local setup.",
            )
        return None

    # --- Auto-detect provider from key format ---
    detected = detect_provider_from_api_key(api_key)
    if detected:
        provider_name = detected
    else:
        # Key format not recognised — ask which provider
        provider_question = InterviewQuestion(
            id="llm_provider",
            field="llm_provider",
            prompt="Which provider is this key for?",
            type="choice",
            choices=[
                {"label": "Google Gemini", "value": "gemini"},
                {"label": "OpenAI", "value": "openai"},
                {"label": "Anthropic (Claude)", "value": "anthropic"},
            ],
            required=True,
            allow_skip=False,
            default=default_provider,
        )
        selection = ask_dialog_question_fn(console, provider_question)
        provider_name = str(selection.value or default_provider or "gemini").strip().lower()

    provider = get_llm_provider(provider_name)
    model = get_catalog_default(provider_name) or provider.default_model
    endpoint = provider.default_endpoint(model, os.environ)
    display = PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)

    if console:
        print_dialog_status(
            console,
            status="success",
            message=f"Detected {display} -- using {model}",
        )

    # Persist the key in the OS keychain so future runs resolve silently.
    if api_key and provider_name != "ollama":
        saved = save_api_key_to_keyring(provider_name, api_key)
        if console:
            if saved:
                print_dialog_status(
                    console,
                    status="info",
                    message="Key saved to your system keychain for future runs.",
                    detail="Use 'fluid ai setup --clear' to reset later.",
                )
            else:
                print_dialog_status(
                    console,
                    status="warning",
                    message="Could not save key to keychain (keyring unavailable).",
                    detail="Set an env var like OPENAI_API_KEY for persistence, or re-run setup next time.",
                )

    return LlmConfig(provider=provider.name, model=model, endpoint=endpoint, api_key=api_key)


# Context-prep helpers (recovery menu + discovery summary +
# industry-skills loader + workspace defaults) physically extracted
# to ``cli/_forge_modes_context.py`` (~292 LOC). Re-exported here so
# existing call sites and test patches keep resolving.
# CI/CD auto-scaffolder — physically extracted to
# ``cli/_forge_ci_scaffolder.py``. The constants + functions are
# re-exported here under the same names so existing imports keep
# resolving.
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
from fluid_build.cli._forge_modes_context import (  # noqa: E402,F401
    _apply_workspace_defaults,
    _choose_recovery_mode,
    _handle_copilot_recovery,
    _load_industry_skills,
    _print_discovery_hint,
    _print_discovery_summary,
    _print_mode_awareness,
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


def run_ai_copilot_mode(
    args: Any,
    logger: logging.Logger,
    *,
    copilot_class: type,
    get_cli_arg_fn: Callable[[Any, str, Any], Any],
    load_context_fn: Callable[..., Dict[str, Any]],
    get_target_directory_fn: Callable[[Any, str], Path],
    context_error_cls: type[Exception],
    build_interview_summary_fn: Callable[[Mapping[str, Any]], Dict[str, Any]],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
    llm_readiness_fn: Callable[[], Any] = check_llm_readiness,
    ask_dialog_question_fn: Callable[[Any, Any], Any] = ask_dialog_question,
    ask_secret_text_fn: Callable[..., Optional[str]] = ask_secret_text,
    route_mode_fn: Optional[Callable[[str], int]] = None,
    fallback_mode_choices: Sequence[Mapping[str, str]] = (),
) -> int:
    """Run Forge with AI copilot assistance."""
    console = console_factory() if console_factory else None

    try:
        copilot = copilot_class()
        is_non_interactive = bool(get_cli_arg_fn(args, "non_interactive", False))

        # Phase 0.2 — detect-first welcome scan. Runs in <50ms (parallel
        # probes) and renders a populated panel so the user sees what we
        # already know before any prompt. Skipped silently in
        # non-interactive runs and for return users (forge_count >= 5).
        # Set FLUID_FORGE_NO_WELCOME=1 to suppress for tests / CI.
        if (
            not is_non_interactive
            and console
            and not bool(os.environ.get("FLUID_FORGE_NO_WELCOME"))
        ):
            try:
                from fluid_build.cli._welcome_scan import (
                    render_welcome,
                    run_welcome_scan,
                )

                _findings = run_welcome_scan()
                render_welcome(_findings, console=console)
            except Exception:  # noqa: BLE001 — welcome must never block forge
                pass
        enable_recovery = bool(get_cli_arg_fn(args, "_enable_copilot_recovery", False))

        # Slice UX-L: performance stats accumulator — populated at
        # each stage and rendered at the end by
        # ``print_forge_performance_summary``.
        import time as _time

        _run_start = _time.monotonic()
        perf_stats: Dict[str, Any] = {
            "streaming": False,
            "discovery_cache_hit": False,
            "discovery_files": 0,
            "discovery_scan_ms": 0,
            "skills_loaded": False,
            "skills_precompiled": False,
            "skills_label": "",
            "interview_skipped": False,
            "generation_attempts": 0,
            "generation_time_s": 0.0,
            "agent_loop_rounds": 0,
            "agent_loop_tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

        context: Dict[str, Any] = {}

        # Inherit workspace defaults (domain, provider, owner) if available.
        _apply_workspace_defaults(context, console)

        # Show mode awareness when inside a workspace with implicit mode.
        implicit_mode = bool(get_cli_arg_fn(args, "_implicit_mode", False))
        if implicit_mode and not is_non_interactive and console:
            _print_mode_awareness(console)

        copilot_options = {
            "llm_provider": get_cli_arg_fn(args, "llm_provider"),
            "llm_model": get_cli_arg_fn(args, "llm_model"),
            "llm_endpoint": get_cli_arg_fn(args, "llm_endpoint"),
            "llm_routing_model": get_cli_arg_fn(args, "llm_routing_model"),
            "llm_routing_endpoint": get_cli_arg_fn(args, "llm_routing_endpoint"),
            "tiered": bool(get_cli_arg_fn(args, "tiered", False)),
            "require_llm": bool(get_cli_arg_fn(args, "require_llm", False)),
            "discover": get_cli_arg_fn(args, "discover", True),
            "discovery_path": get_cli_arg_fn(args, "discovery_path"),
            "memory": get_cli_arg_fn(args, "memory", True),
            "save_memory": get_cli_arg_fn(args, "save_memory", False),
            "non_interactive": is_non_interactive,
            "fragment_first": bool(get_cli_arg_fn(args, "fragments", False)),
            "no_fragments": bool(get_cli_arg_fn(args, "no_fragments", False)),
            "no_generate": bool(get_cli_arg_fn(args, "no_generate", False)),
            # Pre-write preview UX (invariants I1/I4/I5):
            #   auto_yes  — skip the [Y/n] prompt, panel still renders
            #   show_work — stream reasoning/tool calls live as the agent runs
            # ``non_interactive`` implies ``auto_yes`` so headless flows don't
            # block on input.
            "auto_yes": bool(get_cli_arg_fn(args, "yes", False)) or is_non_interactive,
            "show_work": bool(get_cli_arg_fn(args, "show_work", False)),
            # Phase 1 — type-aware authoring. Resolved through the canonical
            # registry at fluid_build.forge.product_types so SDP/Bronze are
            # accepted interchangeably.
            "data_product_type": get_cli_arg_fn(args, "data_product_type"),
            "transform_engine": get_cli_arg_fn(args, "transform_engine"),
            # Phase 0.4 — --refine mode. None means "fresh authoring".
            "refine_contract_path": get_cli_arg_fn(args, "refine"),
            # Phase 3 — composition mode. List of upstream refs (ids or paths).
            "from_product": list(get_cli_arg_fn(args, "from_product", []) or []),
            "from_product_list": get_cli_arg_fn(args, "from_product_list"),
            "from_workspace": list(get_cli_arg_fn(args, "from_workspace", []) or []),
            "also_emit": get_cli_arg_fn(args, "also_emit"),
            # Phase 7 — structural seed from an ODCS / Bitol ODPS document.
            # When --seed-from is set, the FLUID skeleton from the standard
            # is treated as ground truth (schema/quality/qos must not be
            # mutated by the LLM). The post-validation guard in
            # generate_copilot_artifacts enforces this.
            "seed_from": get_cli_arg_fn(args, "seed_from"),
            "seed_no_remote": bool(get_cli_arg_fn(args, "seed_no_remote", False)),
        }

        # Phase 7 — load the structural seed up-front so failures surface
        # before any LLM tokens are spent. The SeedResult rides on
        # context.structural_seed; the runtime picks it up and uses it as
        # the LLM seed_contract + ground-truth diff source.
        _seed_from = copilot_options.get("seed_from")
        if _seed_from:
            try:
                from fluid_build.cli.forge_copilot_seed import load_seed as _load_seed

                _allow_remote = not copilot_options.get("seed_no_remote", False)
                context["structural_seed"] = _load_seed(
                    _seed_from, allow_remote=_allow_remote
                )
                logger.info(
                    "forge_seed_loaded",
                    extra={
                        "path": str(_seed_from),
                        "expose_count": len((context["structural_seed"].fluid.get("exposes") or [])),
                    },
                )
            except Exception as _seed_exc:  # noqa: BLE001 — surface and exit cleanly
                console_error(f"--seed-from failed: {_seed_exc}")
                return 1

        # Phase 3: when --from-product / --from-product-list is set, resolve
        # the upstream products NOW (before we hit the LLM) so violations
        # surface to the user before tokens are spent. The composition
        # context rides on context.composition for the seed builder.
        _from_products: List[str] = list(copilot_options.get("from_product") or [])
        _from_list_path = copilot_options.get("from_product_list")
        if _from_list_path:
            try:
                _list_p = Path(_from_list_path)
                if not _list_p.is_absolute():
                    _list_p = Path.cwd() / _list_p
                if _list_p.exists():
                    _from_products.extend(
                        line.strip()
                        for line in _list_p.read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.strip().startswith("#")
                    )
            except Exception:  # noqa: BLE001
                logger.debug("from_product_list_read_failed", exc_info=True)

        if _from_products:
            try:
                from fluid_build.forge_datamodel.from_data_products import (
                    run_from_data_products,
                )

                target_pt = (
                    copilot_options.get("data_product_type")
                    or context.get("data_product_type")
                    or "ADP"
                )
                _ws_search = [Path(p) for p in (copilot_options.get("from_workspace") or [])]
                composition = run_from_data_products(
                    target_type=str(target_pt),
                    upstream_refs=_from_products,
                    workspace_root=Path.cwd(),
                    extra_search_paths=_ws_search,
                )
                if not composition.is_valid:
                    if console:
                        try:
                            console.print(
                                "[red]Composition rejected — "
                                f"{len(composition.violations)} violation(s):[/red]"
                            )
                            for v in composition.violations[:10]:
                                console.print(f"  · {v}")
                        except Exception:  # noqa: BLE001
                            pass
                    return 1
                context["composition"] = composition.to_prompt_summary()
                # Pre-fill consumes[] so the seed contract carries valid
                # upstream references regardless of what the LLM does.
                context["consumes"] = composition.to_consumes_block()
                if console:
                    try:
                        n = len(composition.upstream_products)
                        console.print(
                            f"[green]✓[/green] composing from {n} upstream "
                            f"product{'s' if n != 1 else ''}: "
                            + ", ".join(p.id for p in composition.upstream_products[:5])
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                logger.exception("from_products_resolution_failed")

        # Refine mode: load the existing contract + the latest receipt and
        # use both to seed the copilot context. The user's *change request*
        # rides on context.refine_request; the seed contract becomes the
        # current contract so the LLM produces a refined-but-bytes-similar
        # output (invariant **I2**).
        _refine_path = copilot_options.get("refine_contract_path")
        if _refine_path:
            try:
                from pathlib import Path as _Path

                import yaml as _yaml

                refine_p = _Path(_refine_path)
                if not refine_p.is_absolute():
                    refine_p = _Path.cwd() / refine_p
                if refine_p.exists():
                    existing = _yaml.safe_load(refine_p.read_text(encoding="utf-8")) or {}
                    context.setdefault("refine_existing_contract", existing)
                    context.setdefault("refine_contract_path", str(refine_p))
                    if console:
                        try:
                            console.print(
                                f"[dim]--refine: loaded {refine_p} as the starting "
                                "point for this iteration.[/dim]"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    if console:
                        try:
                            console.print(
                                f"[yellow]--refine: contract at {refine_p} not found; "
                                "starting from scratch.[/yellow]"
                            )
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                logger.debug("refine_load_failed", exc_info=True)

        # Phase 0.2 — workspace specialization. ``fluid init
        # --workspace-lock SDP|ADP|CDP`` writes
        # ``data_product_type_lock`` into ``fluid.workspace.yaml``.
        # When set: forge defaults to that type and rejects a
        # conflicting --data-product-type so a CDP-locked workspace
        # can't accidentally take an SDP product.
        try:
            from fluid_build.cli.workspace_config import (
                find_workspace_root,
                load_workspace_config,
            )

            _ws_root = find_workspace_root(Path.cwd())
            _ws_lock = load_workspace_config(_ws_root).data_product_type_lock if _ws_root else ""
        except Exception:  # noqa: BLE001
            _ws_lock = ""

        if _ws_lock:
            requested = copilot_options.get("data_product_type")
            if requested and requested.upper() != _ws_lock.upper():
                if console:
                    try:
                        console.print(
                            f"[red]This workspace is locked to {_ws_lock} via "
                            f"data_product_type_lock; --data-product-type "
                            f"{requested!r} conflicts. Either drop the flag, "
                            "match the lock, or unlock the workspace first.[/red]"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return 1
            if not requested:
                copilot_options["data_product_type"] = _ws_lock

        # Project the data_product_type onto the copilot context so the
        # seed contract and prompts both reflect the user's choice.
        _dpt = copilot_options.get("data_product_type")
        if _dpt:
            from fluid_build.forge.product_types import get_product_type as _resolve_pt

            pt = _resolve_pt(str(_dpt))
            if pt is None and console:
                try:
                    console.print(
                        f"[yellow]--data-product-type {_dpt!r} not recognised; "
                        "the copilot will infer the type instead.[/yellow]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            elif pt is not None:
                context.setdefault("data_product_type", pt.code)
                context.setdefault("layer", pt.layer)
                context.setdefault("productType", pt.code)

        context_arg = get_cli_arg_fn(args, "context")
        if context_arg:
            try:
                loaded_context = load_context_fn(
                    context_arg,
                    console,
                    context_error_cls=context_error_cls,
                )
                context.update(loaded_context)
                if console:
                    print_dialog_status(console, status="success", message="Loaded extra context.")
            except context_error_cls as exc:
                if console:
                    print_dialog_status(
                        console,
                        status="error",
                        message=f"Couldn't use the context file: {exc}",
                        detail="Continuing without it for now.",
                    )
                else:
                    logger.warning("Context validation failed: %s", exc)

        if get_cli_arg_fn(args, "provider"):
            context["provider"] = get_cli_arg_fn(args, "provider")
        if get_cli_arg_fn(args, "template"):
            context["template"] = get_cli_arg_fn(args, "template")
        if get_cli_arg_fn(args, "domain") and "domain" not in context:
            context["domain"] = get_cli_arg_fn(args, "domain")
        explicit_target_dir = get_cli_arg_fn(args, "target_dir")
        if explicit_target_dir:
            copilot_options["target_dir"] = str(Path(explicit_target_dir).expanduser())

        if not get_cli_arg_fn(args, "non_interactive", False):
            # Load personal memory (per-engineer preferences)
            try:
                from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

                personal_prefs = load_personal_memory()
                if personal_prefs:
                    # Apply as soft defaults (lower precedence than explicit args).
                    # ``ci_provider`` / ``ci_complexity`` are used later by the
                    # auto-CI hook only in interactive mode.
                    pref_keys = (
                        "preferred_provider",
                        "preferred_engine",
                        "preferred_domain",
                        "owner_team",
                        "preferred_ci_provider",
                        "preferred_ci_complexity",
                    )
                    for key in pref_keys:
                        if personal_prefs.get(key) and key.replace("preferred_", "") not in context:
                            mapped_key = key.replace("preferred_", "")
                            context.setdefault(mapped_key, personal_prefs[key])
                    if console:
                        print_dialog_status(
                            console,
                            status="info",
                            message="Loaded your personal preferences.",
                        )
            except ImportError:
                personal_prefs = None

        force_llm_setup = bool(get_cli_arg_fn(args, "_force_llm_setup", False))
        if not is_non_interactive and (enable_recovery or force_llm_setup):
            needs_setup = force_llm_setup
            readiness_error = None
            if not force_llm_setup:
                readiness = llm_readiness_fn()
                needs_setup = not readiness.ready and readiness.error is not None
                readiness_error = readiness.error if needs_setup else None
            if needs_setup:
                # Synthesise a minimal error to enter the recovery flow.
                if readiness_error is None:
                    readiness_error = CopilotGenerationError(
                        "copilot_llm_setup_needed",
                        "LLM credentials not configured.",
                        suggestions=["Choose a provider and paste a new API key."],
                    )
                recovery_result = _handle_copilot_recovery(
                    args=args,
                    console=console,
                    error=readiness_error,
                    llm_readiness_fn=llm_readiness_fn,
                    route_mode_fn=route_mode_fn,
                    fallback_mode_choices=fallback_mode_choices,
                    ask_dialog_question_fn=ask_dialog_question_fn,
                    ask_secret_text_fn=ask_secret_text_fn,
                )
                if isinstance(recovery_result, int):
                    return recovery_result
                copilot_options.update(recovery_result)

        if not is_non_interactive:
            runtime_inputs = copilot.prepare_runtime_inputs(copilot_options)
            copilot_options.update(runtime_inputs)
            if console:
                llm_cfg = runtime_inputs.get("llm_config")
                if llm_cfg:
                    display = PROVIDER_DISPLAY_NAMES.get(llm_cfg.provider, llm_cfg.provider)
                    plan_suffix = ""
                    if getattr(llm_cfg, "routing_model", None):
                        plan_suffix = f" · routing {llm_cfg.routing_model}"
                    if bool(get_cli_arg_fn(args, "tiered", False)):
                        plan_suffix += " · tiered"
                    console.print(
                        f"[dim]AI: [bold]{display}[/bold] / {llm_cfg.model}  "
                        f"{plan_suffix}  "
                        f"(reset with [bold]fluid ai setup --clear[/bold])[/dim]"
                    )
                    try:
                        run_plan = build_llm_run_plan(
                            llm_cfg,
                            tiered=bool(get_cli_arg_fn(args, "tiered", False)),
                        )
                        logical_stage = next(
                            (
                                stage
                                for stage in run_plan.get("stages", [])
                                if stage.get("stage") == "logical_modeler"
                            ),
                            {},
                        )
                        routing_model = run_plan.get("routing_model")
                        console.print(
                            "[dim]AI plan: interview/self-check use "
                            f"{routing_model}; logical modeling uses "
                            f"{logical_stage.get('model')}; contract + dbt stay deterministic.[/dim]"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                discovery = runtime_inputs.get("discovery_report")
                if discovery:
                    _print_discovery_summary(console, discovery)
                else:
                    _print_discovery_hint(console)
                console.print()
                print_copilot_intro_panel(console)
                console.print(
                    "[dim]I'll help you create the perfect data product by understanding your needs...[/dim]\n"
                )
            capability_warnings = list(runtime_inputs.get("capability_warnings") or [])
            if console and capability_warnings:
                print_dialog_status(
                    console,
                    status="warning",
                    message="Copilot couldn't fully verify some local providers.",
                    detail=(
                        f"{capability_warnings[0]} "
                        "Continuing with best-effort defaults. You can review or override the provider later."
                    ),
                )
            # Show existing data products in workspace before interview
            discovery_report = runtime_inputs["discovery_report"]
            existing_contracts = getattr(discovery_report, "existing_contracts", None) or []
            if existing_contracts and console:
                _show_existing_products(console, existing_contracts)
                # Pass to interview so LLM can detect duplicates
                context["existing_products"] = [
                    {"id": c.get("id", ""), "name": c.get("name", "")} for c in existing_contracts
                ]

            # Resolve preliminary target_dir for early scaffold (samples/ + models/).
            # If --target-dir was provided, use it. Otherwise we'll use a
            # temporary name; the final target_dir is resolved after the
            # interview when we know the project_goal.
            explicit_target = get_cli_arg_fn(args, "target_dir")
            _prelim_target = Path(explicit_target).expanduser() if explicit_target else None

            interview_state = run_adaptive_copilot_interview(
                initial_context=context,
                console=console,
                llm_config=runtime_inputs["llm_config"],
                discovery_report=discovery_report,
                capability_matrix=runtime_inputs["capability_matrix"],
                project_memory=runtime_inputs["project_memory"],
                target_dir=_prelim_target,
                quiet=getattr(args, "quiet", False),
            )
            copilot_options["interview_state"] = interview_state
            context = interview_state.finalize()
            assumptions = list(context.get("assumptions_used") or [])
            if console and assumptions:
                print_assumptions_panel(console, assumptions)
        else:
            for key, value in {
                "project_goal": "Data Analytics Platform",
                "data_sources": "Database tables",
                "use_case": "analytics",
                "complexity": "intermediate",
            }.items():
                context.setdefault(key, value)
            context["interview_summary"] = build_interview_summary_fn(context)

        context = normalize_copilot_context(context)

        # --- Domain auto-detection: load expertise packs transparently ---
        explicit_domain = get_cli_arg_fn(args, "domain", None)

        # BYODA: when --domain points to an unknown agent, offer to scaffold.
        if explicit_domain:
            from fluid_build.cli.forge_agent_specs import (
                AgentSpecError,
                load_user_or_builtin_spec,
                scaffold_user_agent,
            )

            try:
                load_user_or_builtin_spec(explicit_domain)
            except AgentSpecError as spec_err:
                # Show the specific validation error so the user can fix it.
                if console and not is_non_interactive:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"Agent spec error for '{explicit_domain}': {spec_err}",
                    )
                else:
                    logger.warning("Invalid agent spec for %s: %s", explicit_domain, spec_err)
                explicit_domain = None
            except FileNotFoundError:
                if console and not is_non_interactive:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"No agent found for '{explicit_domain}'.",
                    )
                    from fluid_build.cli.forge_dialogs import ask_confirmation

                    if ask_confirmation(console, "Create a custom domain agent?", default=True):
                        try:
                            path = scaffold_user_agent(explicit_domain)
                            print_dialog_status(
                                console,
                                status="success",
                                message=f"Created {path.relative_to(Path.cwd())}",
                            )
                            console.print(
                                f"  Edit the file to customize questions, rules, and suggestions.\n"
                                f"  Then re-run: [bold]fluid forge --domain {explicit_domain}[/bold]\n"
                            )
                        except Exception as scaffold_err:  # noqa: BLE001
                            print_dialog_status(
                                console,
                                status="error",
                                message=f"Could not create agent: {scaffold_err}",
                            )
                        return 0
                    # User declined — continue without domain enrichment.
                    explicit_domain = None
                else:
                    logger.warning("Unknown domain agent: %s (skipping)", explicit_domain)
                    explicit_domain = None

        if explicit_domain or not context.get("domain_expertise"):
            from fluid_build.cli.forge_domain_enrichment import (
                detect_domain,
                enrich_context_with_domain,
            )

            domain = explicit_domain or detect_domain(context)
            logger.debug("Domain detection: explicit=%s, detected=%s", explicit_domain, domain)
            if domain:
                context = enrich_context_with_domain(context, domain)
                if console:
                    print_dialog_status(
                        console,
                        status="info",
                        message=f"Loaded {domain} domain expertise pack.",
                    )

        # --- Team memory: load shared conventions ---
        try:
            from fluid_build.cli.forge_team_memory import (
                TEAM_MEMORY_FILENAME,
                load_team_memory,
            )
            from fluid_build.util.workspace import find_workspace_root

            ws_root = find_workspace_root(Path.cwd()) or Path.cwd()
            team_memory_path = ws_root / ".fluid" / TEAM_MEMORY_FILENAME
            tm = load_team_memory(ws_root)
            if tm is not None:
                perf_stats["team_memory"] = tm.summary_line()
                if console:
                    print_dialog_status(
                        console,
                        status="info",
                        message=f"Loaded team memory ({tm.summary_line()}).",
                    )
            elif team_memory_path.exists() and console:
                # File exists but failed to parse — show actionable error.
                print_dialog_status(
                    console,
                    status="warning",
                    message=f"Could not parse {TEAM_MEMORY_FILENAME}. Check YAML syntax.",
                )
            elif console and not is_non_interactive:
                print_dialog_status(
                    console,
                    status="info",
                    message="No team memory found. Create .fluid/team-memory.yaml to share conventions.",
                )
        except Exception:  # noqa: BLE001
            pass

        # Slice UX-L: populate perf_stats from what we know so far.
        from fluid_build.cli.forge_copilot_llm_providers import streaming_is_enabled

        _llm_cfg = copilot_options.get("llm_config")
        if _llm_cfg:
            perf_stats["provider"] = getattr(_llm_cfg, "provider", "")
            perf_stats["model"] = getattr(_llm_cfg, "model", "")
            perf_stats["routing_model"] = getattr(_llm_cfg, "routing_model", None)
        perf_stats["streaming"] = streaming_is_enabled()
        # Skills info was already set by _load_industry_skills via context.
        if context.get("compiled_skills"):
            perf_stats["skills_loaded"] = True
            perf_stats["skills_precompiled"] = True
            perf_stats["skills_label"] = context["compiled_skills"].get("industry", "loaded")
        elif context.get("industry_skills"):
            perf_stats["skills_loaded"] = True
            perf_stats["skills_precompiled"] = False
            ind = context["industry_skills"].get("industry", {})
            perf_stats["skills_label"] = ind.get("label", "loaded")
        # Interview skip info was set in context by the interview.
        perf_stats["interview_skipped"] = bool(context.get("_interview_skipped"))

        project_name = context.get("project_goal", "my-data-product").lower().replace(" ", "-")
        target_dir = get_target_directory_fn(args, project_name)
        copilot_options["target_dir"] = str(target_dir)

        # Slice UX-H: default fluid forge is now minimal — only
        # contract.fluid.yaml + .fluid/forge-receipt.json land on disk.
        # The legacy ForgeEngine path (extracts/, loads/, transforms/,
        # config/, docs/, tests/, scripts/, requirements.txt,
        # .env.example, README.md, …) runs only when the user explicitly
        # opts in via --scaffold <template>.
        scaffold_template = get_cli_arg_fn(args, "scaffold", None)
        use_agent_loop = bool(get_cli_arg_fn(args, "agent_loop", False)) or bool(
            os.environ.get("FLUID_COPILOT_AGENT_LOOP")
        )

        if scaffold_template:
            success_result = copilot.create_project(
                target_dir,
                context,
                copilot_options,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
            )
            if not success_result:
                return 1
        elif use_agent_loop:
            # Slice UX-K: multi-turn agent loop with tool use.
            success_result = _create_project_agent_loop(
                target_dir=target_dir,
                context=context,
                copilot_options=copilot_options,
                copilot=copilot,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
                logger=logger,
                console=console,
                perf_stats=perf_stats,
            )
            if not success_result:
                return 1
        else:
            success_result = _create_project_minimal(
                copilot=copilot,
                target_dir=target_dir,
                context=context,
                copilot_options=copilot_options,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
                logger=logger,
                console=console,
            )
            if not success_result:
                return 1

        # Surface provenance from the copilot result so the forge
        # receipt can include it (args is the shared namespace between
        # the mode runner and the receipt writer in forge.py).
        if hasattr(copilot, "_last_provenance"):
            args._copilot_provenance = copilot._last_provenance
            score = copilot._last_provenance.get("self_eval_score")
            if score is not None:
                perf_stats["self_eval_score"] = score

        # Post-generation: create data + dbt scaffolding (slice UX-H:
        # gated on --scaffold so the minimal path leaves an empty
        # product dir except for the contract + receipt).
        if scaffold_template:
            _scaffold_data_folder(target_dir, context, console)

        # Post-generation: auto-scaffold a CI/CD pipeline (optional).
        ci_provider, ci_complexity = _scaffold_ci_pipeline(
            args,
            target_dir,
            context,
            console,
            ask_dialog_question_fn=ask_dialog_question_fn,
            get_cli_arg_fn=get_cli_arg_fn,
            dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
        )
        if ci_provider:
            context["ci_provider"] = ci_provider
        if ci_complexity:
            context["ci_complexity"] = ci_complexity

        # Save personal memory (per-engineer preferences)
        try:
            from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

            save_personal_memory(context, console)
        except ImportError:
            pass

        # Slice UX-L: render the performance summary panel.
        perf_stats["generation_time_s"] = round(_time.monotonic() - _run_start, 1)
        try:
            from fluid_build.cli.forge_copilot_llm_providers import get_cumulative_token_usage

            usage = get_cumulative_token_usage()
            perf_stats.update(usage)
        except Exception:  # noqa: BLE001
            pass
        try:
            from fluid_build.cli.forge_ui import print_forge_performance_summary

            print_forge_performance_summary(console, perf_stats)
        except Exception:  # noqa: BLE001 — summary is best-effort
            pass

        return 0
    except KeyboardInterrupt:
        logger.info("AI Copilot cancelled by user")
        return 130
    except CopilotGenerationError as exc:
        logger.exception("AI Copilot mode failed")
        if console:
            console.print(f"[red]❌ AI Copilot failed: {exc.message}[/red]")
            for suggestion in exc.suggestions:
                console.print(f"[dim]• {suggestion}[/dim]")
        else:
            console_error(f"AI Copilot failed: {exc.message}")
            for suggestion in exc.suggestions:
                cprint(f"  • {suggestion}")
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI Copilot mode failed")
        is_key_error = "api_key" in str(exc).lower() or "missing_llm" in str(exc).lower()
        if console:
            console.print(f"[red]AI Copilot failed: {exc}[/red]")
            if is_key_error:
                console.print(
                    "[yellow]Tip: Run 'fluid ai setup' to configure your LLM provider,[/yellow]\n"
                    "[yellow]or use 'fluid forge --blank' / guided mode without AI.[/yellow]"
                )
        return 1


def run_domain_agent_mode(
    args: Any,
    logger: logging.Logger,
    *,
    ai_agents: Mapping[str, type],
    gather_context_fn: Callable[[Any, Any], Dict[str, Any]],
    load_context_fn: Callable[..., Dict[str, Any]],
    get_target_directory_fn: Callable[[Any, str], Path],
    context_error_cls: type[Exception],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Run Forge with a specialized domain agent."""
    console = console_factory() if console_factory else None

    try:
        agent_name = args.agent

        if not agent_name:
            if console and not args.non_interactive and Table is not None:
                console.print("\n[bold blue]🎯 Available Domain Agents[/bold blue]")
                table = Table()
                table.add_column("Agent", style="cyan")
                table.add_column("Domain", style="green")
                table.add_column("Description", style="white")
                for name, agent_class in ai_agents.items():
                    agent_instance = agent_class()
                    table.add_row(name, agent_instance.domain, agent_instance.description)
                console.print(table)

                from fluid_build.cli.forge_copilot_interview import InterviewQuestion
                from fluid_build.cli.forge_dialogs import ask_dialog_question

                selection = ask_dialog_question(
                    console,
                    InterviewQuestion(
                        id="agent",
                        field="agent",
                        prompt="Which agent would you like to use?",
                        type="choice",
                        choices=[{"label": name, "value": name} for name in ai_agents.keys()],
                        required=False,
                        allow_skip=True,
                        default="copilot",
                    ),
                )
                agent_name = selection.value or "copilot"
            else:
                agent_name = "copilot"

        if agent_name not in ai_agents:
            if console:
                console.print(f"[red]❌ Unknown agent: {agent_name}[/red]")
                console.print(f"[dim]Available agents: {', '.join(ai_agents.keys())}[/dim]")
            return 1

        agent = ai_agents[agent_name]()

        if console and not args.non_interactive:
            console.print(f"\n[bold blue]🎯 Starting {agent.name.title()} Domain Agent[/bold blue]")
            console.print(f"[dim]{agent.description}[/dim]\n")

        context: Dict[str, Any] = {}
        if args.context:
            try:
                from fluid_build.cli.forge_validation import validate_context_dict

                loaded_context = load_context_fn(
                    args.context,
                    console,
                    context_error_cls=context_error_cls,
                )
                is_valid, error = validate_context_dict(loaded_context)
                if is_valid:
                    context.update(loaded_context)
                    if console:
                        print_dialog_status(
                            console, status="success", message="Loaded extra context."
                        )
                elif console:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"Context loaded with a warning: {error}",
                    )
            except context_error_cls as exc:
                if console:
                    print_dialog_status(
                        console,
                        status="error",
                        message=f"Couldn't use the context file: {exc}",
                    )

        if not args.non_interactive:
            context.update(gather_context_fn(agent, console))
        else:
            context = {
                "project_goal": f"{agent.domain.title()} Data Product",
                "data_sources": "Various sources",
                "use_case": "analytics",
                "complexity": "intermediate",
            }

        suggestions = agent.analyze_requirements(context)
        if console and not args.non_interactive:
            console.print("\n[bold green]🤖 Agent Analysis Complete[/bold green]\n")
            console.print(
                f"[cyan]Recommended Template:[/cyan] {suggestions.get('recommended_template')}"
            )
            console.print(
                f"[cyan]Recommended Provider:[/cyan] {suggestions.get('recommended_provider')}"
            )
            if suggestions.get("security_requirements"):
                console.print("\n[yellow]🔒 Security Requirements:[/yellow]")
                for requirement in suggestions["security_requirements"][:3]:
                    console.print(f"  • {requirement}")
            console.print()

        project_name = (
            context.get("project_goal", f"{agent.domain}-data-product").lower().replace(" ", "-")
        )
        from fluid_build.cli.forge_validation import sanitize_project_name

        target_dir = get_target_directory_fn(args, sanitize_project_name(project_name))
        success_result = agent.create_project(target_dir, context)
        return 0 if success_result else 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Domain agent mode failed")
        if console:
            console.print(f"[red]❌ Domain agent failed: {exc}[/red]")
        return 1


# Pre-write receipt enrichment — physically extracted to
# ``cli/_receipt_enrichment.py``. Re-exported here under the same
# name so existing test patches on
# ``fluid_build.cli.forge_modes._populate_richer_receipt`` flow
# through to the moved function via the module-attribute-access
# indirection pattern.
from fluid_build.cli._receipt_enrichment import (  # noqa: E402,F401
    _populate_richer_receipt,
)

# ``run_template_mode`` and its 6 internal helpers were physically
# extracted into the ``_template_mode`` sibling module so the
# template-mode logic (~1200 LOC) lives in a dedicated file.
# Re-imported here at top level so existing test patches that
# target ``fluid_build.cli.forge_modes.run_template_mode`` (or any
# of its private helpers) still resolve via the module namespace.
from fluid_build.cli._template_mode import (  # noqa: E402,F401
    _create_project_agent_loop,
    _create_project_minimal,
    _generate_engine_artifacts,
    _generate_schedule_artifacts,
    _scaffold_data_folder,
    _show_existing_products,
    run_template_mode,
)


def run_guided_mode(
    args: Any,
    logger: logging.Logger,
    *,
    get_target_directory_fn: Callable[[Any, str], Path],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Create a data product via 4 quick interactive prompts (no LLM required)."""
    import os

    console = console_factory() if console_factory else None

    # Guard: guided mode requires interactive stdin
    if not sys.stdin.isatty():
        logger.error("Guided mode requires an interactive terminal")
        if console:
            console.print("[red]Guided mode requires an interactive terminal.[/red]")
        return 1

    try:
        from fluid_build.cli.forge_ui import ask_numbered_choice

        if console and RICH_AVAILABLE:
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
    except KeyboardInterrupt:
        logger.info("Guided mode cancelled")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Guided mode failed")
        if console:
            console.print(f"[red]Guided mode failed: {exc}[/red]")
        return 1
