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

# ruff: noqa: F821 — this helper resolves host-module symbols
# (LlmConfig, BUILTIN_LLM_PROVIDERS, etc.) at call-time via a _host()
# indirection accessor; ruff cannot statically see those bindings.
"""Template-mode runner — physical extraction from ``forge_modes.py``.

Houses :func:`run_template_mode` plus the 6 helpers exclusive to the
template path (``_create_project_agent_loop``,
``_create_project_minimal``, ``_generate_engine_artifacts``,
``_generate_schedule_artifacts``, ``_scaffold_data_folder``,
``_show_existing_products``).

The shared ``_populate_richer_receipt`` helper stays on ``forge_modes``
and is accessed via the ``_fm`` indirection so test patches that target
``fluid_build.cli.forge_modes._populate_richer_receipt`` still
resolve at call-time.

Test patches that target ``fluid_build.cli.forge_modes.run_template_mode``
also still work — ``forge_modes`` re-imports it from this module at
top level, so the symbol is bound on both namespaces.
"""

from __future__ import annotations

# ── Stdlib ──
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

# ── Project ──
# ``_fm`` indirection: ``forge_modes`` re-imports the symbols defined
# here at module top, AND keeps the ``_populate_richer_receipt`` helper
# that this module references via ``_fm.<name>`` so test patches that
# target ``fluid_build.cli.forge_modes.<name>`` resolve at call time.
from fluid_build.cli import forge_modes as _fm  # noqa: E402
from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
)
from fluid_build.cli.forge_dialogs import (
    ask_confirmation,
    ask_dialog_question,
    print_dialog_status,
)

try:
    from rich.console import Console
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
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

        with open(target_dir / "contract.fluid.yaml", "w") as handle:
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
        logger.exception("Template mode failed")
        if console:
            console.print(f"[red]❌ Template mode failed: {exc}[/red]")
        else:
            console_error(f"Template mode failed: {exc}")
        return 1


def _create_project_agent_loop(
    *,
    target_dir: Path,
    context: Dict[str, Any],
    copilot_options: Dict[str, Any],
    copilot: Any,
    dry_run: bool,
    logger: logging.Logger,
    console: Any,
    perf_stats: Optional[Dict[str, Any]] = None,
) -> bool:
    """Slice UX-K: run the multi-turn agent loop with tool use.

    This is the ``--agent-loop`` path.  Instead of the single-shot
    prompt + repair-retry loop, the LLM calls tools iteratively to
    discover the workspace, pick a template, build and validate the
    contract.  The final result is the same shape as the minimal path
    — ``contract.fluid.yaml`` written via ``write_contract``.
    """
    from fluid_build.cli.forge_contract_factory import write_contract
    from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop
    from fluid_build.cli.forge_copilot_llm_providers import (
        CopilotGenerationError,
        resolve_llm_config,
    )
    from fluid_build.util.workspace import find_workspace_root

    try:
        llm_config = copilot_options.get("llm_config")
        if not llm_config:
            llm_config = resolve_llm_config(
                type("Args", (), copilot_options)(),
                environ=None,
            )

        if console:
            try:
                console.print(
                    "[cyan]Running in agent-loop mode[/cyan] [dim](multi-turn tool use)[/dim]\n"
                )
            except Exception:  # noqa: BLE001
                pass

        # SECURITY_REVIEW S-003/S-004: determine the workspace root at
        # the CLI-invoked entry point (not in the agent loop) so it
        # reflects the human operator's intent. find_workspace_root
        # walks up from cwd looking for a fluid project marker; we
        # fall back to cwd if nothing is found.
        ws_root = find_workspace_root(Path.cwd()) or Path.cwd()

        # Phase 0.4: spin up the preview panel BEFORE the agent loop
        # so iteration-level transcript/reasoning persists incrementally.
        # This is the I1 (interruptible authoring) hook — Ctrl-C anywhere
        # in the loop leaves a recoverable trace under .fluid/agents/.
        from fluid_build.cli._preview_panel import PreviewPanel as _PreviewPanel
        from fluid_build.cli._preview_panel import new_run_id as _new_run_id

        _agent_loop_panel: Optional[_PreviewPanel] = None
        if not bool(os.environ.get("FLUID_FORGE_NO_PREVIEW")):
            try:
                _agent_loop_panel = _PreviewPanel(run_id=_new_run_id(), target_dir=target_dir)
                _agent_loop_panel.persist_artifacts()
            except Exception:  # noqa: BLE001 — never block the loop on telemetry
                logger.debug("agent_loop_panel_init_failed", exc_info=True)
                _agent_loop_panel = None

        result = run_copilot_agent_loop(
            context=context,
            llm_config=llm_config,
            project_memory=copilot_options.get("project_memory"),
            capability_matrix=copilot_options.get("capability_matrix"),
            console=console,
            perf_stats=perf_stats,
            workspace_root=ws_root,
            preview_panel=_agent_loop_panel,
            show_work=bool(copilot_options.get("show_work", False)),
        )

        contract = result.get("contract")
        if not contract:
            logger.error("Agent loop returned no contract")
            if console:
                try:
                    console.print("[red]Agent loop did not produce a contract.[/red]")
                except Exception:  # noqa: BLE001
                    pass
            return False

        if dry_run:
            if console:
                try:
                    console.print(
                        f"[dim]DRY RUN: would write {target_dir}/contract.fluid.yaml[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return True

        target_dir.mkdir(parents=True, exist_ok=True)
        contract_path = target_dir / "contract.fluid.yaml"
        write_contract(contract, contract_path, command="fluid forge --agent-loop")

        if console:
            try:
                console.print(f"\n[green]Wrote[/green] [cyan]{contract_path}[/cyan]")
            except Exception:  # noqa: BLE001
                pass
        return True

    except CopilotGenerationError as exc:
        logger.exception("Agent loop failed")
        if console:
            try:
                console.print(f"[red]{exc.message}[/red]")
                for sug in getattr(exc, "suggestions", []) or []:
                    console.print(f"[dim]{sug}[/dim]")
            except Exception:  # noqa: BLE001
                pass
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent loop crashed")
        if console:
            try:
                console.print(f"[red]Agent loop failed: {exc}[/red]")
            except Exception:  # noqa: BLE001
                pass
        return False


def _create_project_minimal(
    *,
    copilot: Any,
    target_dir: Path,
    context: Dict[str, Any],
    copilot_options: Dict[str, Any],
    dry_run: bool,
    logger: logging.Logger,
    console: Any,
) -> bool:
    """Slice UX-H minimal path — run the copilot LLM without ForgeEngine.

    The AI copilot flow still runs the full interview, hits the LLM,
    validates the result, and produces a :class:`CopilotGenerationResult`
    — exactly the same behavior as ``CopilotAgent.create_project``.
    The only difference is where the generated contract lands:

    * Legacy path: ``ForgeEngine`` materialises a full opinionated
      project tree (``extracts/``, ``loads/``, ``transforms/``,
      ``config/``, ``docs/``, ``tests/``, ``scripts/``,
      ``requirements.txt``, ``.env.example``, ``README.md``, …).

    * Minimal path (this function): the generated contract is written
      verbatim to ``<target_dir>/contract.fluid.yaml`` via
      :func:`fluid_build.cli.forge_contract_factory.write_contract`,
      which injects the slice-4 ``metadata.provenance`` envelope.  No
      other files land on disk from here — the outer
      ``_scaffold_ci_pipeline`` call still writes optional CI files,
      and the ``forge.py::run`` caller still writes
      ``.fluid/forge-receipt.json``.

    Returns ``True`` on success, ``False`` on failure.  Never raises —
    errors are logged and a red panel is shown to the user.
    """
    from pydantic import BaseModel

    from fluid_build.cli.forge_contract_factory import write_contract
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    def _write_ai_work_receipt(generation_result: Any) -> Optional[Path]:
        """Persist a concise, user-facing AI run receipt."""
        ai_run_plan = getattr(generation_result, "ai_run_plan", None)
        provenance = getattr(generation_result, "provenance", None) or {}
        if not ai_run_plan and not provenance:
            return None
        payload = {
            "kind": "ForgeAIWorkReceipt",
            "runPlan": ai_run_plan or provenance.get("ai_run_plan"),
            "agentEvents": list(provenance.get("agent_events") or []),
            "fallbackUsed": bool(provenance.get("fallback_used", False)),
            "fallbackEvents": list(provenance.get("fallback_events") or []),
            "repairUsed": bool(provenance.get("repair_used", False)),
            "repairEvents": list(provenance.get("repair_events") or []),
            "provenance": provenance,
        }
        receipt_path = target_dir / ".fluid" / "ai-work-receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return receipt_path

    def _serialize_logical_model(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                json.loads(value)
            except Exception:  # noqa: BLE001
                return None
            return value
        if isinstance(value, BaseModel):
            try:
                return value.model_dump_json(indent=2, by_alias=True)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, dict):
            try:
                return json.dumps(value, indent=2, sort_keys=True)
            except TypeError:
                return None
        if isinstance(value, list):
            try:
                return json.dumps(value, indent=2)
            except TypeError:
                return None
        return None

    import time as _time

    _minimal_started_at = _time.time()

    try:
        # Context is already normalized by the caller (run_ai_copilot_mode).
        options = dict(copilot_options or {})
        options.setdefault("target_dir", str(target_dir))

        # Index upstream products so the LLM can emit real dbt SQL with
        # correct source identifiers and join keys. Honors the anchor
        # workspace plus ``FLUID_UPSTREAM_CONTRACTS`` (colon-separated),
        # used for upstream contracts living in a different repository
        # from the current product. Cheap: filesystem walk of <= 4 levels.
        try:
            from fluid_build.util.upstream_discovery import (
                discover_upstream_products,
                project_upstream_for_prompt,
            )

            upstream_raw = discover_upstream_products(target_dir)
            if upstream_raw:
                upstream_projection = project_upstream_for_prompt(upstream_raw)
                if upstream_projection:
                    context["upstream_products"] = upstream_projection
                    logger.debug(
                        "upstream_products_indexed: count=%d",
                        len(upstream_projection),
                    )
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.debug("upstream_discovery_failed: %s", exc)

        if dry_run:
            if console:
                try:
                    console.print(
                        f"[dim]DRY RUN: would generate and write {target_dir}/contract.fluid.yaml[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.info("DRY RUN: would generate minimal AI contract in %s", target_dir)
            return True

        try:
            generation_result = copilot.generate_project_artifacts(context, options)
        except CopilotGenerationError as generation_error:
            recovered_result = copilot._attempt_generation_recovery(
                context=context,
                options=options,
                error=generation_error,
            )
            if recovered_result is None:
                raise
            context = normalize_copilot_context(
                options.get("interview_state").normalized_context
                if options.get("interview_state")
                else context
            )
            generation_result = recovered_result

        suggestions = generation_result.suggestions
        contract = generation_result.contract

        # PII propagation: when the user composed via ``--from-product``,
        # re-resolve the upstream products and stamp matching column
        # sensitivity tags onto the AI-generated contract. The LLM
        # rarely carries column-level tags through reliably; the helper
        # makes the propagation deterministic and auditable.
        from_product_refs = list(copilot_options.get("from_product") or [])
        if contract and from_product_refs:
            try:
                from fluid_build.forge_datamodel.from_data_products import (
                    load_upstream_products,
                    propagate_pii_classifications,
                    resolve_upstream_paths,
                )

                _ws = list(copilot_options.get("from_workspace") or [])
                upstream_paths = resolve_upstream_paths(
                    from_product_refs,
                    workspace_root=Path.cwd(),
                    extra_search_paths=[Path(p) for p in _ws],
                )
                upstream_products, _problems = load_upstream_products(upstream_paths)
                pii_log = propagate_pii_classifications(contract, upstream_products)
                if pii_log:
                    logger.info(
                        "pii_propagation: %d tag(s) applied: %s",
                        len(pii_log),
                        "; ".join(pii_log[:5]),
                    )
            except Exception as exc:  # noqa: BLE001 — never fail the run on propagation
                logger.warning("pii_propagation_failed: %s", exc, exc_info=True)

        # Stash provenance on the copilot object so the caller can
        # include it in the forge receipt.
        if getattr(generation_result, "provenance", None):
            copilot._last_provenance = generation_result.provenance

        # Optional UI: reuse the copilot's own analysis panel so the
        # minimal path has feature parity with the engine path aside
        # from filesystem output.
        try:
            copilot._show_ai_analysis(context, suggestions, generation_result)
        except Exception as exc:  # noqa: BLE001 — UI must never fail the run
            logger.debug("copilot_show_ai_analysis_failed", extra={"error": str(exc)})

        # Write the LLM-generated contract using the slice-4 envelope
        # writer.  write_contract injects metadata.provenance with the
        # correct 'fluid forge' command string.
        target_dir.mkdir(parents=True, exist_ok=True)
        contract_path = target_dir / "contract.fluid.yaml"
        sidecar_path = target_dir / f"{contract_path.name}.model.json"

        # ── Fragment layout decision ─────────────────────────────
        from fluid_build.cli.forge_contract_fragments import (
            is_complex_enough_for_fragments,
            split_contract_to_fragments,
        )

        force_fragments = options.get("fragment_first", False)
        force_flat = options.get("no_fragments", False)
        existing_fragments = (target_dir / "fragments").is_dir()

        use_fragments = False
        if force_fragments:
            use_fragments = True
        elif force_flat:
            use_fragments = False
        elif existing_fragments:
            use_fragments = True
        else:
            use_fragments = is_complex_enough_for_fragments(contract)

        # ── Transformation engine artifact generation ────────────
        # If the contract has a builds[].engine and we have a registered
        # generator, produce engine artifacts (dbt project, SQL scripts,
        # etc.) and write them alongside the contract.  This runs BEFORE
        # ``write_contract`` because the engine may mutate the contract
        # (e.g. setting ``builds[].repository`` to point at the generated
        # project directory so ``fluid apply --build`` can find it).
        engine_files = _generate_engine_artifacts(
            contract,
            target_dir=target_dir,
            context=context,
            discovery_report=generation_result.discovery_report,
            logger=logger,
            console=console,
            no_generate=options.get("no_generate", False),
        )

        # Schedule generation — produce Airflow DAGs, Dagster pipelines,
        # Prefect flows alongside the contract.
        schedule_files = _generate_schedule_artifacts(
            contract,
            target_dir=target_dir,
            context=context,
            logger=logger,
            console=console,
            no_generate=options.get("no_generate", False),
        )

        # ── Pre-write preview (Phase 0.1, invariants I1/I4/I5) ──────
        # Build a PreviewPanel with every file we're about to write +
        # the cost we already spent. Persist the artifact stack
        # (cost.json / reasoning.md / transcript.json) BEFORE the
        # confirmation prompt so Ctrl-C at the prompt loses nothing.
        # Set FLUID_FORGE_NO_PREVIEW=1 to bypass (CI / scripts).
        from fluid_build.cli._preview_panel import (
            PreviewPanel,
            capture_cost_snapshot,
            confirm,
            new_run_id,
            render_completion,
        )

        _preview_enabled = not bool(os.environ.get("FLUID_FORGE_NO_PREVIEW"))

        if use_fragments:
            root_contract, fragment_files = split_contract_to_fragments(contract)
        else:
            fragment_files = {}
            root_contract = contract

        _preview_panel: Optional[PreviewPanel] = None
        if _preview_enabled:
            import yaml as _yaml

            _run_id = new_run_id()
            _preview_panel = PreviewPanel(run_id=_run_id, target_dir=target_dir)
            try:
                _preview_panel.add_file(
                    str(contract_path.relative_to(target_dir)),
                    _yaml.safe_dump(root_contract, sort_keys=False),
                )
            except Exception:  # noqa: BLE001 — preview must never crash the run
                logger.debug("preview_panel_contract_serialise_failed", exc_info=True)
            for rel_path, content in fragment_files.items():
                _preview_panel.add_file(rel_path, content)
            for rel_path, content in (engine_files or {}).items():
                _preview_panel.add_file(rel_path, content)
            for rel_path, content in (schedule_files or {}).items():
                _preview_panel.add_file(rel_path, content)
            for rel_path, content in (generation_result.additional_files or {}).items():
                _preview_panel.add_file(rel_path, content)

            _llm_provider_name = (getattr(generation_result, "provenance", None) or {}).get(
                "llm_provider", ""
            )
            _llm_model_name = (getattr(generation_result, "provenance", None) or {}).get(
                "llm_model", ""
            )
            _preview_panel.cost = capture_cost_snapshot(
                provider=_llm_provider_name,
                model=_llm_model_name,
                started_at=options.get("_started_at", _minimal_started_at),
            )
            for attempt in getattr(generation_result, "attempt_reports", None) or []:
                _preview_panel.append_transcript(
                    {
                        "kind": "generation_attempt",
                        "attempt": getattr(attempt, "attempt", None),
                        "validation_errors": list(getattr(attempt, "validation_errors", []) or []),
                        "validation_warnings": list(
                            getattr(attempt, "validation_warnings", []) or []
                        ),
                    }
                )
            # Phase 3 #6 — richer receipts. Pull every interview turn,
            # every assumption, every tool call into the panel so the
            # forge-receipt.json captures the full provenance, not just
            # one row. The 'why this contract?' question can now be
            # answered a year from now from the receipt alone.
            _fm._populate_richer_receipt(
                panel=_preview_panel,
                contract=contract,
                generation_result=generation_result,
                context=context,
                logger=logger,
            )
            _preview_panel.persist_artifacts()

            if not confirm(_preview_panel, auto_yes=bool(options.get("auto_yes", False))):
                _preview_panel.cleanup_run_dir()
                if console:
                    try:
                        console.print("[yellow]Aborted at preview. No files were written.[/yellow]")
                    except Exception:  # noqa: BLE001
                        pass
                return False

        # ── Commit the preview ──────────────────────────────────────
        if use_fragments:
            write_contract(root_contract, contract_path, command="fluid forge")
            for rel_path, content in fragment_files.items():
                fpath = target_dir / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
        else:
            write_contract(contract, contract_path, command="fluid forge")

        logical_model = getattr(generation_result, "logical_model", None)
        serialized_logical = _serialize_logical_model(logical_model)
        if serialized_logical is not None:
            sidecar_path.write_text(serialized_logical, encoding="utf-8")
            if str(context.get("review_data_model", "")).lower() == "true":
                try:
                    from fluid_build.cli.forge_data_model import review_logical_model

                    reviewed = review_logical_model(
                        sidecar_path,
                        logger,
                        contract_path=contract_path,
                    )
                    if reviewed is not None:
                        sidecar_path.write_text(
                            reviewed.model_dump_json(indent=2, by_alias=True),
                            encoding="utf-8",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("logical_review_failed: %s", exc)

        # Write additional files (dbt models, SQL, etc.) — these were
        # previously ignored in the minimal path.
        #
        # Merge order matters.  Engine skeletons land first, then
        # schedule files, then the LLM's additional_files — so when the
        # LLM ships a real dbt mart SQL at the same path the engine
        # emitted a TODO skeleton, the LLM content wins.  Infrastructure
        # files the LLM never touches (dbt_project.yml, profiles.yml,
        # sources.yml) still come from the engine.
        additional_files: Dict[str, str] = {}
        additional_files.update(engine_files)
        additional_files.update(schedule_files)
        llm_files = generation_result.additional_files or {}
        if llm_files:
            additional_files.update(llm_files)
        if additional_files:
            for rel_path, content in additional_files.items():
                fpath = target_dir / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")

        ai_receipt_path = _write_ai_work_receipt(generation_result)

        # Persist project memory the same way the AI mode does, so
        # subsequent forge runs in this product have the full history.
        try:
            copilot._maybe_save_project_memory(
                target_dir=target_dir,
                context=context,
                suggestions=suggestions,
                generation_result=generation_result,
                copilot_options=options,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — memory save is best-effort
            logger.debug("copilot_memory_save_failed", extra={"error": str(exc)})

        # ── Tell the user what happened ──────────────────────────
        if console:
            try:
                console.print(f"\n[green]✅ Wrote[/green] [cyan]{contract_path}[/cyan]")
                if fragment_files:
                    console.print(
                        f"[green]   + {len(fragment_files)} fragments under fragments/[/green]"
                    )
                    for rel_path in sorted(fragment_files):
                        console.print(f"[dim]     {rel_path}[/dim]")
                    console.print("\n[bold]📦 Layout: Fragment-first (modular)[/bold]")
                    console.print(
                        "[dim]   Your contract was split into composable fragments under fragments/.[/dim]"
                    )
                    console.print(
                        "[dim]   • fluid bundle        — reassemble into a single contract[/dim]"
                    )
                    console.print(
                        "[dim]   • --no-fragments      — next forge will produce a single file instead[/dim]"
                    )
                elif not force_flat and is_complex_enough_for_fragments(contract):
                    console.print("\n[bold]📦 Layout: Single file[/bold]")
                    console.print(
                        "[dim]   For larger contracts, fragments help with reuse and team collaboration.[/dim]"
                    )
                    console.print(
                        "[dim]   • fluid split         — break into composable fragments[/dim]"
                    )
                    console.print(
                        "[dim]   • --fragments          — next forge will auto-split[/dim]"
                    )
                if additional_files:
                    n = len(additional_files)
                    console.print(f"[green]   + {n} additional file{'s' if n != 1 else ''}[/green]")
                if ai_receipt_path:
                    console.print("[green]   + AI work receipt[/green]")
                if engine_files:
                    console.print(
                        "[dim]   Tip: use 'fluid generate transformation' to re-generate transformations.[/dim]"
                    )
                if schedule_files:
                    console.print(
                        "[dim]   Tip: use 'fluid generate schedule' to re-generate your schedule.[/dim]"
                    )
            except Exception:  # noqa: BLE001
                pass

        if fragment_files:
            context["_authoring_mode"] = "fragment-first"

        # ── Final receipt + completion ritual (Phase 0.1) ───────────
        if _preview_panel is not None:
            try:
                # Refresh cost — additional LLM calls may have run between
                # the preview and the writes (engine generators, schedule
                # synthesis, self-eval, etc.).
                _preview_panel.cost = capture_cost_snapshot(
                    provider=_preview_panel.cost.provider,
                    model=_preview_panel.cost.model,
                    started_at=options.get("_started_at", _minimal_started_at),
                )
                _preview_panel.persist_artifacts()
                _preview_panel.write_receipt()
                if console:
                    render_completion(_preview_panel, console=console)
            except Exception:  # noqa: BLE001 — receipt is best-effort
                logger.debug("preview_panel_finalise_failed", exc_info=True)

        # Phase 3 — ODCS / OPDS / ODPS auto-chain. ``--also-emit odcs`` runs
        # ``fluid generate standard --format odcs`` against the freshly
        # written contract and lands the export at ``<contract-id>.odcs.yaml``
        # next to the contract. CDP products default to emitting ODCS so
        # consumption-aligned products always ship with a standardised
        # contract for the consumer-facing toolchain.
        try:
            also_emit = options.get("also_emit")
            md = contract.get("metadata") or {}
            if also_emit is None and md.get("productType") == "CDP":
                also_emit = "odcs"
            if also_emit:
                from fluid_build.cli.generate_standard import _export_format

                ns = type(
                    "Args",
                    (),
                    {"env": None, "out": None, "format": None},
                )
                contract_id = str(contract.get("id") or "product").replace("/", "_")
                for fmt in [s.strip().lower() for s in str(also_emit).split(",") if s.strip()]:
                    if fmt not in ("odcs", "opds", "odps", "odps-bitol"):
                        continue
                    out_name = f"{contract_id}.{fmt}.yaml"
                    out_path = str(target_dir / out_name)
                    ns.format = fmt
                    ns.out = out_path
                    rc = _export_format(fmt, str(contract_path), ns, logger)
                    if rc == 0 and console:
                        try:
                            console.print(f"[green]   + {fmt.upper()} export[/green] {out_name}")
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            logger.debug("also_emit_failed", exc_info=True)

        # Phase 3 — emit a OTel ``forge.invocation`` span attribute so
        # operators can compare cost / type / engine across runs. Free
        # product analytics when OTEL_EXPORTER_OTLP_ENDPOINT is set;
        # no-op when it isn't (the helper is a no-op span).
        try:
            from fluid_build.observability.tracing import traced_span

            attrs: Dict[str, Any] = {
                "fluid.flow": "forge",
                "fluid.data_product_type": (contract.get("metadata") or {}).get("productType", ""),
                "fluid.layer": (contract.get("metadata") or {}).get("layer", ""),
                "fluid.transform_engine": ((contract.get("builds") or [{}])[0].get("engine", "")),
                "fluid.from_data_products": bool(context.get("composition")),
                "fluid.refine_mode": bool(options.get("refine_contract_path")),
            }
            if _preview_panel is not None and _preview_panel.cost.total_usd is not None:
                attrs["fluid.cost_usd"] = round(_preview_panel.cost.total_usd, 4)
                attrs["fluid.tokens"] = _preview_panel.cost.total_tokens
            # Phase 0.6 #9 — UX telemetry on the same span. Lets
            # operators correlate user-visible UX (questions_asked,
            # inferences_used, time_to_first_panel) with cost/repair
            # metrics so future UX iterations are evidence-based.
            try:
                from fluid_build.cli._ux_telemetry import get_telemetry

                attrs.update(get_telemetry().to_span_attributes())
            except Exception:  # noqa: BLE001
                pass
            with traced_span("forge.invocation", attributes=attrs):
                pass
        except Exception:  # noqa: BLE001 — telemetry must never block forge
            logger.debug("forge_invocation_span_failed", exc_info=True)

        # Bump usage counter so future runs detect the return user.
        try:
            from fluid_build.cli._welcome_scan import bump_forge_count

            bump_forge_count()
        except Exception:  # noqa: BLE001
            pass

        return True

    except CopilotGenerationError as exc:
        logger.exception("AI Copilot minimal flow failed")
        if console:
            try:
                console.print(f"[red]❌ {exc.message}[/red]")
                for suggestion in getattr(exc, "suggestions", []) or []:
                    console.print(f"[dim]• {suggestion}[/dim]")
            except Exception:  # noqa: BLE001
                pass
        else:
            console_error(exc.message)
        return False
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        logger.exception("AI Copilot minimal flow crashed")
        if console:
            try:
                console.print(f"[red]❌ Failed to create project: {exc}[/red]")
            except Exception:  # noqa: BLE001
                pass
        else:
            console_error(f"Failed to create project: {exc}")
        return False


def _generate_engine_artifacts(
    contract: Dict[str, Any],
    *,
    target_dir: Path,
    context: Dict[str, Any],
    discovery_report: Any,
    logger: logging.Logger,
    console: Any,
    no_generate: bool = False,
) -> Dict[str, str]:
    """Generate transformation engine artifacts from the contract.

    Returns a dict of {relative_path: content} for files to write under
    target_dir.  Returns empty dict if generation is skipped or no engine
    is available.
    """
    if no_generate:
        return {}

    try:
        from fluid_build.engines import get_engine, has_engine
        from fluid_build.util.contract import get_build_engine, get_builds

        builds = get_builds(contract)
        if not builds:
            return {}

        build = builds[0]
        engine_name = get_build_engine(build) or context.get("build_engine", "")
        if not engine_name:
            return {}

        # Map provider-specific engine names to base engine names
        engine_map = {"dbt-bigquery": "dbt", "dbt-duckdb": "dbt"}
        resolved_name = engine_map.get(engine_name, engine_name)

        if not has_engine(resolved_name):
            if console:
                try:
                    console.print(
                        f"\n[dim]Engine '{engine_name}' is set in your contract. "
                        f"Transformation artifact generation is not yet available for this engine.\n"
                        f"You can write your transformation code manually, or use 'fluid generate' later.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {}

        engine = get_engine(resolved_name)
        if engine is None:
            return {}

        # Validate
        issues = engine.validate(contract, build)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            if logger:
                for issue in errors:
                    logger.warning("engine_validation: %s", issue)
            return {}

        # Build schema_context from discovery report
        schema_context = None
        if discovery_report and hasattr(discovery_report, "sample_files"):
            schemas = {}
            for sample in discovery_report.sample_files:
                if sample.get("columns"):
                    # Use the filename without extension as key
                    path_str = sample.get("path", "")
                    name = Path(path_str).stem if path_str else "unknown"
                    schemas[name] = {"columns": sample["columns"]}
            if schemas:
                schema_context = {"schemas": schemas}

        # Build transformation intent from domain expertise if available
        transformation_intent = None
        domain_expertise = context.get("domain_expertise", {})
        modeling_standards = domain_expertise.get("data_modeling_standards")
        if modeling_standards:
            from fluid_build.engines.base import TransformationIntent

            transformation_intent = TransformationIntent(
                canonical_model=domain_expertise.get("domain"),
                user_data_model=modeling_standards,
                data_modeling_technique=context.get("data_modeling_technique"),
            )

        # Include user-supplied data models from discovery
        if (
            transformation_intent is None
            and discovery_report
            and hasattr(discovery_report, "user_data_models")
            and discovery_report.user_data_models
        ):
            from fluid_build.engines.base import TransformationIntent

            # Merge all user model schemas into one dict
            merged_cols = {}
            for model in discovery_report.user_data_models:
                merged_cols.update(model.get("columns", {}))
            transformation_intent = TransformationIntent(
                user_data_model=merged_cols,
                data_modeling_technique=context.get("data_modeling_technique"),
            )

        # Even when neither domain expertise nor a user data model is
        # available, we still want the engine to see the modeling
        # technique so it can pick the right skeleton shape in the
        # fallback path.  Build a minimal intent carrying just that.
        if transformation_intent is None and context.get("data_modeling_technique"):
            from fluid_build.engines.base import TransformationIntent

            transformation_intent = TransformationIntent(
                data_modeling_technique=context["data_modeling_technique"],
            )

        # Stamp the chosen technique onto the contract so downstream
        # CLIs (``fluid generate transformation``) can surface it
        # in their banner without re-running the interview. We use the
        # top-level ``labels`` map because FLUID 0.7.2 ``metadata``
        # declares ``additionalProperties: false`` — only ``labels``
        # accepts arbitrary string-valued keys.
        _technique = context.get("data_modeling_technique")
        if _technique:
            labels = contract.setdefault("labels", {})
            labels["dataModelingTechnique"] = _technique

        # Generate artifacts under the repository path (or default).
        # When the build doesn't specify a repository we pick a
        # conventional sub-directory and persist it back on the build so
        # ``fluid apply --build`` can locate the generated project.
        repository_explicit = build.get("repository")
        if repository_explicit:
            repository = repository_explicit
            if repository.startswith("./"):
                repository = repository[2:]
        else:
            repository = f"{resolved_name}_project"
            build["repository"] = f"./{repository}"

        files = engine.generate(
            contract,
            build,
            schema_context=schema_context,
            transformation_intent=transformation_intent,
            workspace_root=target_dir,
        )

        # Prefix all paths with the repository directory
        prefixed = {f"{repository}/{rel_path}": content for rel_path, content in files.items()}

        if console and prefixed:
            try:
                console.print(
                    f"\n[green]Generated {len(prefixed)} transformation files[/green] "
                    f"[dim]({resolved_name} engine → {repository}/)[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass

        return prefixed

    except ImportError:
        # engines module not available — skip silently
        return {}
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("engine_artifact_generation_failed: %s", exc)
        return {}


def _generate_schedule_artifacts(
    contract: Dict[str, Any],
    *,
    target_dir: Path,
    context: Dict[str, Any],
    logger: logging.Logger,
    console: Any,
    no_generate: bool = False,
) -> Dict[str, str]:
    """Generate schedule engine artifacts from the contract.

    Returns a dict of {relative_path: content} for files to write under
    target_dir.  Returns empty dict if generation is skipped or no scheduler
    is available.
    """
    if no_generate:
        return {}

    try:
        from fluid_build.schedulers import get_scheduler, has_scheduler

        # Resolve scheduler name from contract or interview context
        orchestration = contract.get("orchestration", {})
        scheduler_name = orchestration.get("engine") or context.get("schedule_engine", "")
        if not scheduler_name:
            return {}

        # Synthesize orchestration from builds when the contract lacks one.
        # The LLM generates builds (transformations) but not orchestration
        # (scheduling), so we derive tasks from the build steps.
        if not orchestration and contract.get("builds"):
            from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

            synthesized = synthesize_orchestration_from_builds(
                contract,
                scheduler_name,
                provider=context.get("provider", ""),
            )
            if synthesized:
                contract = {**contract, "orchestration": synthesized}
                orchestration = synthesized
                if console:
                    try:
                        n = len(synthesized.get("tasks", []))
                        console.print(
                            f"\n[dim]Synthesized {scheduler_name} schedule from "
                            f"{n} build step{'s' if n != 1 else ''}[/dim]"
                        )
                    except Exception:  # noqa: BLE001
                        pass

        # Skip if BYOS path is set (user has their own schedule)
        if context.get("byos_path"):
            if console:
                try:
                    byos = context["byos_path"]
                    console.print(f"\n[green]Using existing schedule:[/green] [dim]{byos}[/dim]")
                except Exception:  # noqa: BLE001
                    pass
            return {}

        if not has_scheduler(scheduler_name):
            if console:
                try:
                    console.print(
                        f"\n[dim]Scheduler '{scheduler_name}' is set in your contract. "
                        f"Schedule artifact generation is not yet available for this scheduler.\n"
                        f"You can write your schedule code manually, or use "
                        f"'fluid generate schedule' later.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {}

        scheduler = get_scheduler(scheduler_name)
        if scheduler is None:
            return {}

        # Validate
        issues = scheduler.validate(contract)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            if logger:
                for issue in errors:
                    logger.warning("scheduler_validation: %s", issue)
            return {}

        # Resolve provider and config
        provider = contract.get("provider", context.get("provider", ""))
        provider_config: Dict[str, Any] = {}
        metadata = contract.get("metadata", {})
        if provider == "gcp":
            provider_config = {
                "project": metadata.get("gcp_project", "my-project"),
                "region": metadata.get("gcp_region", "us-central1"),
            }
        elif provider == "aws":
            provider_config = {
                "region": metadata.get("aws_region", "us-east-1"),
            }
        elif provider == "snowflake":
            provider_config = {
                "connection_id": metadata.get("snowflake_connection_id", "snowflake_default"),
            }

        # Generate
        files = scheduler.generate(
            contract,
            provider=provider,
            provider_config=provider_config,
        )

        # Prefix all paths with a dags/ directory
        output_dir = {"airflow": "dags", "dagster": "pipelines", "prefect": "flows"}.get(
            scheduler_name, "schedules"
        )
        prefixed = {f"{output_dir}/{rel_path}": content for rel_path, content in files.items()}

        if console and prefixed:
            try:
                console.print(
                    f"\n[green]Generated {len(prefixed)} schedule files[/green] "
                    f"[dim]({scheduler_name} scheduler → {output_dir}/)[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass

        return prefixed

    except ImportError:
        # schedulers module not available — skip silently
        return {}
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("schedule_artifact_generation_failed: %s", exc)
        return {}


def _scaffold_data_folder(target_dir: Path, context: dict, console: Any) -> None:
    """Create data/ and optional dbt/ scaffolding after copilot generation."""
    try:
        # Always create data/ folder with guidance
        data_dir = target_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".gitkeep").touch(exist_ok=True)
        readme = data_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Sample Data\n\n"
                "Place sample data files here (CSV, JSON, Parquet).\n"
                "Forge will use them to infer schemas and enrich your contract.\n\n"
                "Re-run `fluid forge` after adding files for richer generation.\n",
                encoding="utf-8",
            )

        # Create dbt scaffolding if data_modeling was requested
        if context.get("data_modeling"):
            dbt_dir = target_dir / "dbt"
            (dbt_dir / "models" / "staging").mkdir(parents=True, exist_ok=True)
            (dbt_dir / "models" / "marts").mkdir(parents=True, exist_ok=True)

            # dbt_project.yml
            project_name = target_dir.name.replace("-", "_")
            dbt_project = dbt_dir / "dbt_project.yml"
            if not dbt_project.exists():
                import yaml as _yaml

                dbt_config = {
                    "name": project_name,
                    "version": "1.0.0",
                    "config-version": 2,
                    "model-paths": ["models"],
                    "target-path": "target",
                    "clean-targets": ["target", "dbt_packages"],
                }
                dbt_project.write_text(
                    _yaml.dump(dbt_config, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )

            if console and RICH_AVAILABLE:
                console.print(
                    "\n[green]Created dbt project scaffolding:[/green]\n"
                    "  dbt/models/staging/  [dim](stg_ source models)[/dim]\n"
                    "  dbt/models/marts/    [dim](fct_ and dim_ tables)[/dim]\n"
                    "  dbt/dbt_project.yml\n\n"
                    "[dim]Add sample data to data/ and re-run forge for richer contracts.[/dim]"
                )
        elif console and RICH_AVAILABLE:
            console.print(
                "\n[dim]Tip: Add sample data to data/ and re-run forge for richer contracts.[/dim]"
            )
    except OSError as exc:
        if console and RICH_AVAILABLE:
            console.print(f"[yellow]Could not create scaffolding: {exc}[/yellow]")


def _show_existing_products(console: Any, existing_contracts: list) -> None:
    """Display a table of data products already in the workspace."""
    if not console or not RICH_AVAILABLE or not existing_contracts:
        return
    try:
        from rich.table import Table as RichTable

        table = RichTable(
            title="Existing Data Products in Workspace",
            border_style="dim",
            show_lines=False,
        )
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="white")
        table.add_column("Provider", style="green")

        for i, contract in enumerate(existing_contracts[:10], 1):
            providers = ", ".join(contract.get("providers") or ["—"])
            table.add_row(
                str(i),
                contract.get("id", "—"),
                contract.get("name", "—"),
                providers,
            )

        console.print()
        console.print(table)
        console.print(
            f"[dim]{len(existing_contracts)} data product(s) found. "
            "The AI will check for duplicates during the interview.[/dim]\n"
        )
    except ImportError:
        pass
