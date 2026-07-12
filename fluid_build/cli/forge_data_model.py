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

"""Nested ``fluid forge data-model`` command."""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml

from fluid_build.cli.console import cprint
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.copilot.store.factory import resolve_store
from fluid_build.copilot.store.history import archive_snapshot
from fluid_build.forge_datamodel.emit.coverage import compute_canonical_coverage
from fluid_build.forge_datamodel.emit.ddl import emit_ddl_files
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.model_doc import emit_model_markdown
from fluid_build.forge_datamodel.emit.osi_sidecar import emit_osi_yaml
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator
from fluid_build.forge_datamodel.emit.variants import emit_dimensional_variants
from fluid_build.forge_datamodel.from_intent.intent_loader import (
    IntentValidationError,
    load_business_intent,
    render_intent_example,
    render_intent_schema_json,
)

if TYPE_CHECKING:  # resolve annotation names for ruff/type-checkers only
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
    from fluid_build.copilot.agents.base import StageSession

# NOTE: ``register_forge_subcommand`` (in the light ``_forge_data_model_register``
# module) imports THIS module to bind the ``run_X_command`` handlers as argparse
# ``set_defaults`` defaults — so building the ``fluid forge data-model`` subparser
# imports ``forge_data_model``. The heavy runtime deps below pull ``httpx`` (LLM
# providers) and ``jsonschema`` (the ``schema_manager`` chain via
# ``forge_contract_factory`` + the from_ddl/from_intent pipelines + the copilot
# agents). They are needed only inside the handler BODIES, so they are resolved
# lazily via ``__getattr__`` (PEP 562) and stay off the ``fluid --help`` /
# ``build_parser()`` cold path. Runtime use sites resolve via
# ``import fluid_build.cli.forge_data_model as _self; _self.X`` (and
# ``except _self.CopilotGenerationError`` / ``raise _self.CopilotGenerationError``)
# so the symbols are real attributes at call time. Annotations referencing
# ``StageSession`` / ``CopilotGenerationError`` are safe at module scope because
# ``from __future__ import annotations`` keeps them as lazy strings.

# attribute name -> (heavy source module, real symbol name)
_DEFERRED_DATA_MODEL_IMPORTS = {
    "write_contract": ("fluid_build.cli.forge_contract_factory", "write_contract"),
    "CopilotGenerationError": (
        "fluid_build.cli.forge_copilot_llm_providers",
        "CopilotGenerationError",
    ),
    "resolve_llm_config": (
        "fluid_build.cli.forge_copilot_llm_providers",
        "resolve_llm_config",
    ),
    "StageSession": ("fluid_build.copilot.agents.base", "StageSession"),
    "AgentExecutionError": ("fluid_build.copilot.agents.errors", "AgentExecutionError"),
    "run_from_ddl": ("fluid_build.forge_datamodel.from_ddl.pipeline", "run_from_ddl"),
    "run_from_intent": (
        "fluid_build.forge_datamodel.from_intent.pipeline",
        "run_from_intent",
    ),
}


def __getattr__(name: str):
    """Lazily resolve the heavy data-model runtime deps (PEP 562).

    Keeps ``httpx`` + the ``schema_manager`` → ``jsonschema`` chain off the
    ``fluid --help`` path while exposing the symbols as module attributes so
    ``except _self.CopilotGenerationError`` / ``raise`` / ``_self.run_from_ddl``
    keep resolving (and any ``patch("…cli.forge_data_model.<symbol>")`` seam
    flows through).
    """
    spec = _DEFERRED_DATA_MODEL_IMPORTS.get(name)
    if spec is not None:
        import importlib

        module = importlib.import_module(spec[0])
        return getattr(module, spec[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


COMMAND = "data-model"
_SAFE_ARTIFACT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# Argparse subparser registration — physically extracted to
# ``cli/_forge_data_model_register.py`` (~280 LOC). Re-exported so
# ``register(subparsers)`` glue keeps working.
from fluid_build.cli._forge_data_model_register import (  # noqa: E402,F401
    register_forge_subcommand,
)


def _add_quiet_arg(parser: argparse.ArgumentParser) -> None:
    """Register the ``--quiet`` / ``-q`` flag.

    Used by every ``fluid forge data-model`` subcommand that surfaces
    the v2-preview banner. Honoured by ``forge_banner.print_v2_banner``
    via the ``quiet=getattr(args, "quiet", False)`` path; the env-var
    suppression (``FLUID_QUIET=1``, ``FLUID_NONINTERACTIVE=1``) remains
    in place and stacks with this flag — either suppresses the banner.
    """
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the v2-preview banner (also honours $FLUID_QUIET / $FLUID_NONINTERACTIVE).",
    )


def _add_common_generation_args(
    parser: argparse.ArgumentParser, *, output_required: bool = True
) -> None:
    _add_quiet_arg(parser)
    # Choices come from the pluggable technique registry (built-in data_vault_2 /
    # dimensional / flat / custom + any ``fluid_build.modeling_techniques`` plugins).
    # ``type`` normalizes aliases (data-vault-2 -> data_vault_2, kimball ->
    # dimensional) so they're accepted, while ``choices`` shows only the clean
    # canonical names.
    from fluid_build.copilot.modeling_techniques import list_modeling_techniques

    parser.add_argument(
        "--modeling-technique",
        "--technique",
        dest="technique",
        type=_normalize_technique,
        choices=list_modeling_techniques(),
        default=None,
        help=(
            "Modeling technique: data_vault_2, dimensional, flat (source-aligned "
            "1:1), custom (bring-your-own logical model via --logical-model), or a "
            "registered plugin technique. Aliases like data-vault-2 / kimball are accepted."
        ),
    )
    parser.add_argument(
        "--logical-model",
        dest="logical_model",
        default=None,
        help=(
            "Path to a logical model (.model.json) used verbatim with "
            "--modeling-technique custom (no reshaping)."
        ),
    )
    parser.add_argument("--output", "-o", required=output_required, help="Output contract path")
    parser.add_argument(
        "--transformation-engine",
        "--engine",
        dest="engine",
        choices=["dbt", "sql", "python", "spark", "custom"],
        default="dbt",
        help="Transformation engine hint stamped into the emitted contract",
    )
    parser.add_argument(
        "--review", action="store_true", help="Open the logical sidecar for review in $EDITOR"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview the forged artifacts without writing files"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Disable staged LLM cache reads/writes"
    )
    parser.add_argument(
        "--tiered", action="store_true", help="Use per-stage model tiers when an LLM is configured"
    )
    parser.add_argument("--emit-ddl-dir", help="Write generated DDL files for the logical model")
    parser.add_argument(
        "--emit-dimensional-variants",
        help="Write star/snowflake/galaxy/flat dimensional sidecars to this directory",
    )
    parser.add_argument(
        "--emit-model-doc",
        dest="emit_model_doc",
        action="store_true",
        default=True,
        help="Write a Mermaid + Markdown data model document next to the contract (default)",
    )
    parser.add_argument(
        "--no-emit-model-doc",
        dest="emit_model_doc",
        action="store_false",
        help="Do not write the Markdown data model document; the .model.json sidecar is still written",
    )
    parser.add_argument(
        "--emit-osi-sidecar",
        action="store_true",
        default=True,
        help="Write a standalone *.semantics.osi.yaml file alongside the contract",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Force deterministic settings (cache off, tiering off) and emit audit metadata",
    )
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Fail if the configured LLM cannot run; do not fall back to heuristics.",
    )
    # Accepts the built-ins plus any pip-installed fluid_build.llm_providers
    # plugin; discovery is lazy so --help never scans entry points.
    from fluid_build.cli._llm_provider_plugins import LlmProviderChoices

    parser.add_argument(
        "--llm-provider",
        choices=LlmProviderChoices(),
        help=(
            "Optional LLM provider override for the staged modeler (incl. keyless "
            "agents and third-party fluid_build.llm_providers plugins)"
        ),
    )
    parser.add_argument("--llm-model", help="Optional LLM model override for the staged modeler")
    parser.add_argument("--llm-endpoint", help="Optional LLM endpoint override")
    parser.add_argument(
        "--llm-routing-model",
        help="Optional fast/cheap model for clarification and AI self-evaluation",
    )
    parser.add_argument("--llm-routing-endpoint", help="Optional routing-model endpoint override")
    parser.add_argument(
        "--llm-timeout-seconds",
        type=int,
        help="Provider HTTP timeout for staged LLM calls (defaults to 120).",
    )
    parser.add_argument(
        "--industry",
        default=None,
        help=(
            "Optional industry identifier (e.g. 'telecommunications', 'retail', "
            "'healthcare', 'finance'). When set, the validator lints the forged "
            "model against the industry pack's canonical skeleton and warns on "
            "missing entities or naming drift."
        ),
    )
    parser.add_argument(
        "--allow-semantic-warnings",
        action="store_true",
        help="Allow artifact writes when canonical industry coverage still has warnings.",
    )


def run(args: Any, logger: logging.Logger) -> int:
    """Dispatch helper used by ``fluid_build.cli.forge``.

    Wraps the per-action dispatch so the run-level cost tracker is
    reset at start and the cost summary prints at end. The summary is
    suppressed under ``--quiet`` and is a no-op when no LLM calls
    were recorded (heuristic / cache-hit-only runs).
    """
    func = getattr(args, "data_model_func", None)
    if func is None:
        # No subcommand — render an intuitive guide instead of the
        # bare argparse "the following arguments are required" error.
        return _render_data_model_guide()
    # Reset the run-level cost tracker so the summary reflects only
    # this invocation, not any prior in-process calls. Importing here
    # avoids paying the cost-module import on every CLI startup.
    from fluid_build.copilot.cost import print_cost_summary, reset_run_tracker

    reset_run_tracker()
    try:
        return func(args, logger)
    finally:
        # Always print, even on failure — operators want the cost of
        # a *failed* run too (failed runs still hit the LLM).
        print_cost_summary(quiet=getattr(args, "quiet", False))
        # Sprint #5 — surface the pre-emit conformance summary if
        # the coordinator stamped one on the active session. The
        # coordinator writes ``pre_emit_conformance_summary`` to
        # ``session.capability_matrix`` after every Builder run;
        # without surfacing it in the receipt block, operators
        # never see "conformance: ✓ all 4 standards clean" in their
        # CLI output. This closes the shipped-but-inert gap from
        # the post-V1.5 audit.
        try:
            _print_pre_emit_conformance_summary(
                quiet=getattr(args, "quiet", False),
            )
        except Exception:  # pragma: no cover — defensive
            pass


def _print_pre_emit_conformance_summary(*, quiet: bool = False) -> None:
    """Print the per-run pre-emit conformance summary line.

    Reads from :func:`fluid_build.copilot.cost.get_pre_emit_conformance_summary`
    (which the coordinator stamps after every Builder run). No-op
    when ``quiet`` is set or the summary is empty.
    """
    if quiet:
        return
    from fluid_build.copilot.cost import get_pre_emit_conformance_summary

    summary = get_pre_emit_conformance_summary()
    if not summary:
        return
    cprint(f"  {summary}")


def run_learn_command(args: Any, logger: logging.Logger) -> int:
    """Item 6 — capture operator edits and persist to memory/semantic.

    Diffs ``--original`` against ``--edited`` (both Fluid contracts)
    and writes the resulting :class:`OperatorEdit` records under the
    ``operator_edit:<name>:<ts>`` key so the next forge of a similar
    intent can retrieve and bias toward operator preferences.
    """
    from fluid_build.copilot.learning import (
        compute_edits,
        record_operator_edits,
    )

    original_path = Path(args.original)
    edited_path = Path(args.edited)
    if not original_path.is_file():
        cprint(f"[red]learn failed:[/red] {original_path} not found")
        return 1
    if not edited_path.is_file():
        cprint(f"[red]learn failed:[/red] {edited_path} not found")
        return 1
    try:
        before = yaml.safe_load(original_path.read_text(encoding="utf-8"))
        after = yaml.safe_load(edited_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        cprint(f"[red]learn failed:[/red] could not parse YAML: {exc}")
        return 1

    edits = compute_edits(before=before or {}, after=after or {})
    if not edits:
        cprint("[yellow]No edits found between the two contracts.[/yellow] Nothing recorded.")
        return 0

    contract_name = args.name or original_path.stem.replace(".fluid", "") or "unnamed"
    session = _build_session(args, workspace_root=edited_path.parent, logger=logger)
    record_operator_edits(
        store=session.store,
        contract_name=contract_name,
        edits=edits,
        context={
            "original_path": str(original_path),
            "edited_path": str(edited_path),
        },
    )
    if not getattr(args, "quiet", False):
        cprint(
            f"[green]✓[/green] Recorded {len(edits)} operator edit(s) "
            f"for contract {contract_name!r} in memory/semantic."
        )
        # One-line preview of the most-changed paths so the
        # operator sees what was captured without needing to dig.
        preview = edits[:5]
        for edit in preview:
            cprint(f"  • {edit.kind:<8} {edit.path}")
        if len(edits) > 5:
            cprint(f"  • ... and {len(edits) - 5} more")
    return 0


def run_from_ddl_command(args: Any, logger: logging.Logger) -> int:
    # Bind the heavy runtime deps as locals (resolves via the module
    # ``__getattr__`` lazy-import seam) so the bare-name ``except`` / call
    # sites below keep working off the light cold path.
    from fluid_build.cli.forge_data_model import (
        AgentExecutionError,
        CopilotGenerationError,
        run_from_ddl,
    )

    rc = _maybe_run_custom_technique(args, logger)
    if rc is not None:
        return rc
    output_path = Path(args.output)
    try:
        session = _build_session(args, workspace_root=output_path.parent, logger=logger)
    except CopilotGenerationError as exc:
        _print_copilot_generation_error(exc)
        return 1
    ddl_texts = [Path(path).read_text(encoding="utf-8") for path in args.ddl]
    name = output_path.stem.replace(".fluid", "") or Path(args.ddl[0]).stem
    technique = _normalize_technique(args.technique) or "data_vault_2"
    # H2 — surface the offline/heuristic banner BEFORE the run so the
    # user knows quality will be lower than the --require-llm path.
    _print_heuristic_only_banner(session=session, technique=technique, args=args, logger=logger)
    import time as _time

    _started_at = _time.time()
    try:
        result = run_from_ddl(
            session,
            name=name,
            ddl_texts=ddl_texts,
            technique=technique,
            source_type=args.source_type,
            engine=args.engine,
        )
    except ValueError as exc:
        cprint(f"[red]from-ddl failed:[/red] {exc}")
        return 1
    except AgentExecutionError as exc:
        cprint(f"[red]from-ddl failed:[/red] {exc}")
        return 1
    # H22 — always write a per-run cost.json so deterministic runs are
    # visible to ``fluid stats``.
    _persist_run_cost_receipt(
        session=session,
        output_path=output_path,
        wall_clock_seconds=_time.time() - _started_at,
        logger=logger,
    )
    logical = result.coordinator.logical
    contract = _finalize_contract(
        logical=logical,
        output_path=output_path,
        engine=args.engine,
        contract=result.coordinator.contract,
    )
    # H2 — stamp a warning into the contract when the dimensional
    # heuristic ran without an LLM, so the contract itself records
    # the caveat (validation is schema-only and cannot catch
    # structural mis-classification).
    if session.llm_config is None and not session.require_llm:
        _stamp_heuristic_warning_on_contract(contract, technique=technique)
    return _write_or_report(
        args,
        logger,
        logical=logical,
        contract=contract,
        validation=FluidContractValidator().validate(
            logical=logical,
            contract=contract,
            industry_pack=session.industry_pack,
        ),
        industry_pack=session.industry_pack,
    )


def run_from_intent_command(args: Any, logger: logging.Logger) -> int:
    # Bind the heavy runtime deps as locals (resolves via the module
    # ``__getattr__`` lazy-import seam) so the bare-name ``except`` / call
    # sites below keep working off the light cold path.
    from fluid_build.cli.forge_data_model import (
        AgentExecutionError,
        CopilotGenerationError,
        run_from_intent,
    )

    rc = _maybe_run_custom_technique(args, logger)
    if rc is not None:
        return rc
    if getattr(args, "example", None):
        try:
            sys.stdout.write(render_intent_example(str(args.example)))
        except IntentValidationError as exc:
            cprint(f"[red]from-intent failed:[/red] {exc}")
            return 1
        return 0

    if getattr(args, "schema", False):
        sys.stdout.write(render_intent_schema_json())
        return 0

    if getattr(args, "validate_intent", None):
        try:
            load_business_intent(args.validate_intent)
        except IntentValidationError as exc:
            cprint(f"[red]intent validation failed:[/red] {exc}")
            return 1
        cprint(f"[green]Intent file is valid[/green] {args.validate_intent}")
        return 0

    intent_file = getattr(args, "intent_file", None)
    if not intent_file:
        cprint(
            "[red]from-intent failed:[/red] provide an intent file, "
            "or use --example / --schema / --validate"
        )
        return 2
    if not getattr(args, "output", None):
        cprint("[red]from-intent failed:[/red] --output is required when forging artifacts")
        return 2

    output_path = Path(args.output)
    try:
        session = _build_session(args, workspace_root=output_path.parent, logger=logger)
    except CopilotGenerationError as exc:
        _print_copilot_generation_error(exc)
        return 1
    try:
        intent = load_business_intent(intent_file)
    except IntentValidationError as exc:
        cprint(f"[red]from-intent failed:[/red] {exc}")
        return 1

    technique = _normalize_technique(
        args.technique or (intent.modeling.technique if intent.modeling else None)
    )
    technique = technique or "data_vault_2"
    args.selected_modeling_technique = technique
    args.intent_source_path = str(intent_file)
    # H2 — banner before run (dimensional heuristic only, no LLM).
    _print_heuristic_only_banner(session=session, technique=technique, args=args, logger=logger)
    import time as _time

    _started_at = _time.time()
    try:
        result = run_from_intent(
            session,
            intent=intent,
            technique=technique,
            engine=args.engine,
        )
    except AgentExecutionError as exc:
        cprint(f"[red]from-intent failed:[/red] {exc}")
        return 1
    # H22 — always write a per-run cost.json so deterministic runs are
    # visible to ``fluid stats``.
    _persist_run_cost_receipt(
        session=session,
        output_path=output_path,
        wall_clock_seconds=_time.time() - _started_at,
        logger=logger,
    )
    logical = result.coordinator.logical
    contract = _finalize_contract(
        logical=logical,
        output_path=output_path,
        engine=args.engine,
        contract=result.coordinator.contract,
    )
    # H2 — heuristic warning on the contract itself.
    if session.llm_config is None and not session.require_llm:
        _stamp_heuristic_warning_on_contract(contract, technique=technique)
    return _write_or_report(
        args,
        logger,
        logical=logical,
        contract=contract,
        validation=FluidContractValidator().validate(
            logical=logical,
            contract=contract,
            industry_pack=session.industry_pack,
        ),
        industry_pack=session.industry_pack,
    )


def _print_copilot_generation_error(exc: CopilotGenerationError) -> None:
    cprint(f"[red]{exc.event}:[/red] {exc.message}")
    for suggestion in exc.suggestions:
        cprint(f"  - {suggestion}")


def run_from_source_command(args: Any, logger: logging.Logger) -> int:
    """V1.5 — forge from a configured metadata-source catalog OR a
    JDBC-introspectable database.

    The CLI surface mirrors ``run_from_intent_command`` /
    ``run_from_ddl_command`` so the staged-pipeline output (the
    ``.fluid.yaml`` contract + ``.model.json`` sidecar + optional
    OSI sidecar / DDL emit / dimensional variants) lands the same
    way regardless of how the user fed input in.

    Branches:

    * ``--source <catalog>`` (snowflake, unity, bigquery, dataplex,
      glue, datahub, datamesh_manager) — the credential-resolver-built
      catalog adapter. Same guarantees as the MCP ``forge_from_source``
      tool — no credentials ever leak into the audit trail.
    * ``--source <jdbc>`` (postgres, postgresql, mysql, sqlite) —
      a duckdb-extension-based introspection path. Reads ``--uri``,
      attaches the source via ``INSTALL <ext>; LOAD <ext>; ATTACH``,
      enumerates tables + types, returns the same staged-pipeline
      output. No credential resolver: the URI carries everything.

    Internally calls ``run_from_catalog`` from
    ``forge_datamodel.from_catalog.pipeline`` so the MCP tool and
    the CLI subcommand share one staged-pipeline path.
    """
    # Bind the heavy runtime dep as a local (resolves via the module
    # ``__getattr__`` lazy-import seam) so the bare-name ``except`` site below
    # keeps working off the light cold path.
    from fluid_build.cli.forge_data_model import CopilotGenerationError

    # Bring-your-own logical model (issue #248): --modeling-technique custom
    # + --logical-model short-circuits the source pipeline entirely.
    rc = _maybe_run_custom_technique(args, logger)
    if rc is not None:
        return rc
    if not getattr(args, "source", None):
        cprint(
            "[red]from-source requires --source <catalog|jdbc>[/red] (or "
            "--modeling-technique custom with --logical-model to forge from a "
            "supplied logical model instead)."
        )
        return 1
    # JDBC sources — branch out early to a duckdb-attach helper. The
    # rest of the function below handles catalog sources. The JDBC set
    # lives in the shared source registry (single source of truth).
    from fluid_build.copilot.catalog.source_registry import is_jdbc_source

    if is_jdbc_source(args.source):
        return _run_from_jdbc_source(args, logger)

    # Lazy imports keep ``fluid forge --help`` cold-start fast: the
    # catalog SDKs only load when this command actually runs.
    from fluid_build.copilot.catalog.credentials import CredentialResolver
    from fluid_build.copilot.catalog.models import CatalogScope
    from fluid_build.forge_datamodel.from_catalog.pipeline import run_from_catalog

    output_path = Path(args.output)
    try:
        session = _build_session(args, workspace_root=output_path.parent, logger=logger)
    except CopilotGenerationError as exc:
        _print_copilot_generation_error(exc)
        return 1

    # Resolver honours the V1.5 plan's A/B/B picks: keyring + YAML
    # primary, env vars fallback, metadata-service only when
    # --allow-metadata-service is set.
    resolver = CredentialResolver(
        allow_metadata_service=getattr(args, "allow_metadata_service", False),
    )
    adapter = _build_catalog_adapter(
        source=args.source,
        resolver=resolver,
        credential_id=args.credential_id,
    )

    # Scope: pull the (database, schema, catalog, tables) bundle off
    # the parsed args. The Pydantic model validates required fields
    # per the catalog's expectations (BQ requires schema_name,
    # Glue requires database, etc.) — adapters surface a typed
    # CatalogConfigError with the missing-field message when needed.
    scope = CatalogScope.model_validate(
        {
            k: v
            for k, v in {
                "database": args.database,
                "schema_name": args.schema_name,
                "catalog": args.catalog,
                "tables": args.tables or [],
            }.items()
            if v is not None
        }
    )

    # Default the model name to the schema / catalog scope.
    model_name = args.name or args.schema_name or args.catalog or args.database or "forged_model"
    technique = _normalize_technique(args.technique) or "data_vault_2"

    # V1.5 Gap 5 — auto-detect industry from catalog tags when the
    # operator didn't pass ``--industry``. We do a one-pass list
    # of tables (lightweight; the per-table detail fetch is what
    # the staged pipeline does later), aggregate tag-driven
    # industry votes, and load the matching IndustryPack.
    #
    # The auto-detect is silent when no tags match — falls through
    # to the existing legacy behaviour (no industry-pack lint).
    # When a match fires, we print a one-line message so the
    # operator knows their catalog tags drove a modeling choice.
    if session.industry_pack is None:
        from fluid_build.copilot.industry.compiler import (
            IndustryPackCompiler,
            detect_industry_from_catalog_tables,
        )

        try:
            peek_tables = adapter.list_tables(scope)
        except Exception:
            # Adapter failure here means the staged pipeline is
            # going to fail anyway — let the pipeline surface the
            # error with the typed-exception suggestions (rather
            # than swallowing a connection error in the
            # auto-detect path).
            peek_tables = []
        detected = detect_industry_from_catalog_tables(peek_tables)
        if detected:
            try:
                pack = IndustryPackCompiler().compile(detected, technique=technique)
                session.industry_pack = pack
                cprint(
                    f"Detected industry: [cyan]{detected}[/cyan] "
                    f"(from catalog tags). Pack loaded for skeleton + lint."
                )
            except Exception as exc:  # pragma: no cover — defensive
                # Pack compilation failure shouldn't poison the
                # forge — log and continue.
                logger.debug("fluid.copilot.catalog.industry_autodetect.failed: %s", exc)

    import time as _time

    _started_at = _time.time()
    try:
        result = run_from_catalog(
            session,
            name=model_name,
            adapter=adapter,
            scope=scope,
            technique=technique,
            engine=args.engine,
        )
    except Exception as exc:  # noqa: BLE001 — surface the typed error cleanly
        # Catalog errors carry actionable suggestions in the
        # ``suggestions`` attribute; surface them so the operator
        # has a clear next-action.
        suggestions = getattr(exc, "suggestions", None)
        cprint(f"[red]from-source failed:[/red] {exc}")
        if suggestions:
            cprint("Suggestions:")
            for s in suggestions:
                cprint(f"  • {s}")
        return 1

    # H22 — persist cost.json under .fluid/agents/<run-id>/ even for
    # deterministic (no-LLM) runs so the run is visible to
    # ``fluid stats``. Best-effort: a write failure here MUST NOT
    # poison an otherwise-successful forge.
    _persist_run_cost_receipt(
        session=session,
        output_path=output_path,
        wall_clock_seconds=_time.time() - _started_at,
        logger=logger,
    )

    logical = result.coordinator.logical
    contract = _finalize_contract(
        logical=logical,
        output_path=output_path,
        engine=args.engine,
        contract=result.coordinator.contract,
    )
    return _write_or_report(
        args,
        logger,
        logical=logical,
        contract=contract,
        validation=FluidContractValidator().validate(
            logical=logical,
            contract=contract,
            industry_pack=session.industry_pack,
        ),
        industry_pack=session.industry_pack,
    )


def _persist_run_cost_receipt(
    *,
    session: Any,
    output_path: Path,
    wall_clock_seconds: float,
    logger: logging.Logger,
) -> None:
    """Write a per-run ``cost.json`` under ``.fluid/agents/<run-id>/``.

    Closes the H22 gap: the from-source / data-model pipeline was
    skipping cost.json entirely when the run was deterministic (no
    LLM call), so ``fluid stats`` reported "0 runs" even though the
    coordinator's manifest plainly recorded a completed forge.

    Idempotent: ``RunCostTracker.persist_to_run_dir`` always writes a
    receipt (including ``mode="deterministic"`` + zero counts for runs
    that never invoked the LLM). The run-id comes from the staged
    coordinator's session stamp; when unavailable we mint a synthetic
    one so the cost-tracker still has somewhere to land.

    Best-effort: any exception is swallowed at DEBUG. The forge has
    already succeeded by the time we get here; a receipt write
    failure must not turn that into an error.
    """
    try:
        run_id = getattr(session, "run_id", "") or ""
        if not run_id:
            from fluid_build.copilot.agents._coordinator_helpers import (
                new_run_id as _mint,
            )

            run_id = _mint()
        workspace_root = (
            getattr(session, "workspace_root", None) or output_path.parent or Path.cwd()
        )
        run_dir = Path(workspace_root) / ".fluid" / "agents" / run_id
        from fluid_build.copilot.cost import get_run_tracker

        get_run_tracker().persist_to_run_dir(
            run_dir,
            wall_clock_seconds=wall_clock_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — never block on a receipt write
        logger.debug("cost_receipt_persist_failed: %s", exc)


# JDBC-source path (postgres / mysql / sqlite via duckdb-extension
# scanner) — physically extracted to ``cli/_forge_data_model_jdbc.py``.
# Re-exported here so the dispatcher in ``run_from_source_command``
# keeps resolving the bare names.
from fluid_build.cli._forge_data_model_jdbc import (  # noqa: E402,F401
    _map_jdbc_type_to_logical,
    _run_from_jdbc_source,
)


def _build_catalog_adapter(
    *,
    source: str,
    resolver: Any,
    credential_id: Optional[str],
) -> Any:
    """Construct the right adapter for ``source`` via its
    ``from_resolver`` classmethod.

    Resolves the class through the shared source registry
    (``copilot.catalog.source_registry``) — the single source of truth
    shared with the MCP layer — so built-in AND plugin
    (``fluid_build.source_adapters``) catalog adapters dispatch
    identically. The registry module is intentionally lightweight (no
    ``cli.mcp`` import) so the CLI doesn't pull in yaml + history +
    audit_trail just to look up an adapter class.
    """
    from fluid_build.copilot.catalog.source_registry import resolve_catalog_adapter_class

    cls = resolve_catalog_adapter_class(source)
    return cls.from_resolver(resolver, credential_id=credential_id)


def run_validate_command(args: Any, logger: logging.Logger) -> int:
    validator = FluidContractValidator()
    target = Path(args.path)
    if not target.exists():
        # Surface missing-file errors with a clean one-liner instead of a
        # raw Python traceback — the old behavior leaked
        # ``FileNotFoundError`` straight to the user via the generic
        # ``forge run`` fallback, which is terrible UX for a CLI that
        # advertises "fluid forge data-model validate <path>".
        cprint(f"[red]validate failed:[/red] {target} does not exist")
        return 1

    logical = None
    contract = None
    try:
        if target.suffix == ".json":
            logical = LogicalDraft.model_validate_json(target.read_text(encoding="utf-8"))
            candidate_contract = target.with_name(target.name[: -len(".model.json")])
            if candidate_contract.exists():
                contract = yaml.safe_load(candidate_contract.read_text(encoding="utf-8"))
        else:
            contract = yaml.safe_load(target.read_text(encoding="utf-8"))
            model_path = target.with_name(f"{target.name}.model.json")
            if model_path.exists():
                logical = LogicalDraft.model_validate_json(model_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValueError) as exc:
        cprint(f"[red]validate failed:[/red] {target} is not a valid contract/sidecar ({exc})")
        return 1

    report = validator.validate(logical=logical, contract=contract)
    _print_validation_report(report)
    return 0 if report.passes_schema else 1


def run_diff_command(args: Any, logger: logging.Logger) -> int:
    summary = diff_logical_models(Path(args.old), Path(args.new))
    changes = summary.get("changes") or []
    if not changes:
        cprint("[green]No structural differences detected.[/green]")
        return 0
    cprint("[cyan]Structural diff[/cyan]")
    for change in changes:
        cprint(f"  - {change}")
    return 0


def run_dump_ddl_command(args: Any, logger: logging.Logger) -> int:
    """Dump Snowflake DDL for a database.schema to a .sql file.

    Thin CLI adapter on top of
    :func:`fluid_build.forge_datamodel.from_ddl.snowflake_dumper.dump_schema_ddl_to_file`.
    Keeps the heavy lifting (soft-import of snowflake-connector, quoting,
    GET_DDL invocation) in the dumper module so this function stays
    argparse-shaped.
    """
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import (
        dump_schema_ddl_to_file,
    )

    output = Path(args.output)
    try:
        result = dump_schema_ddl_to_file(
            database=args.database,
            schema=args.schema,
            output=output,
            tables=args.tables,
            role=args.role,
            warehouse=args.warehouse,
        )
    except RuntimeError as exc:
        cprint(f"[red]dump-ddl failed:[/red] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("dump_ddl_failed: %s", exc)
        cprint(f"[red]dump-ddl failed:[/red] {exc}")
        return 1

    cprint(
        f"[green]Wrote {result.table_count} table(s) of DDL[/green] "
        f"from {result.database}.{result.schema} → {output}"
    )
    return 0


def _build_session(args: Any, *, workspace_root: Path, logger: logging.Logger) -> StageSession:
    # Bind the heavy runtime deps as locals (resolves via the module
    # ``__getattr__`` lazy-import seam) so the bare-name ``raise`` / constructor
    # sites below keep working off the light cold path.
    from fluid_build.cli.forge_data_model import CopilotGenerationError, StageSession

    require_llm = bool(getattr(args, "require_llm", False))
    if getattr(args, "deterministic", False) and require_llm:
        raise CopilotGenerationError(
            "copilot_conflicting_llm_modes",
            "--deterministic disables LLM calls, so it cannot be combined with --require-llm.",
            suggestions=[
                "Use --deterministic for byte-stable heuristic replay",
                "Use --require-llm for strict provider-integration testing",
            ],
        )
    if getattr(args, "deterministic", False):
        logger.info("deterministic mode enabled; staged LLM calls are disabled")
        llm_config = None
    else:
        llm_config = _resolve_optional_llm_config(args, logger)
    if require_llm and llm_config is None:
        raise CopilotGenerationError(
            "copilot_missing_required_llm",
            "--require-llm was set but no LLM provider/model was configured.",
            suggestions=[
                "Pass --llm-provider and the provider API key",
                "Or unset --require-llm for heuristic fallback runs",
            ],
        )
    store = resolve_store(workspace_root=workspace_root)
    industry_pack = _resolve_optional_industry_pack(args, logger)
    requested_tiered = getattr(args, "tiered", False) and not getattr(args, "deterministic", False)
    effective_tiered = _maybe_collapse_tiered_mode(requested_tiered, llm_config, logger)
    return StageSession(
        store=store,
        workspace_root=workspace_root,
        llm_config=llm_config,
        tiered=effective_tiered,
        no_cache=getattr(args, "no_cache", False) or getattr(args, "deterministic", False),
        industry_pack=industry_pack,
        require_llm=require_llm,
    )


def _maybe_collapse_tiered_mode(
    requested_tiered: bool,
    llm_config: Any,
    logger: logging.Logger,
) -> bool:
    """Honour the plan's "tiered collapse with one-line warning" contract.

    When the user opts in to ``--tiered`` but the resolved provider's
    catalog exposes a single model across every tier (the plan calls
    out Ollama explicitly: today
    ``deep == balanced == llama3.1`` and only ``fast`` differs;
    user-edited catalogs may flatten further), forge-cli must:

    * silently downgrade to single-model mode so the staged pipeline
      doesn't repeatedly resolve "deep" / "balanced" labels that point
      at the same model, and
    * emit one human-readable warning so operators know the flag was
      observed but had no effect.

    The warning fires through the standard ``logging`` module so CI
    captures it without polluting stdout, and the function returns the
    *effective* tiered flag the caller should write into
    :class:`StageSession`.
    """
    if not requested_tiered or llm_config is None:
        return requested_tiered
    from fluid_build.cli.forge_copilot_llm_providers import has_distinct_tier_models

    provider_name = getattr(llm_config, "provider", None)
    if provider_name and has_distinct_tier_models(provider_name):
        return True
    logger.warning(
        "tiered_mode_collapsed: provider=%s has no distinct tier models in "
        "llm_models.json — falling back to single-model mode for this run "
        "(no behavior change beyond suppressing the misleading per-stage "
        "tier labels). Set distinct ``deep``/``balanced``/``fast`` entries "
        "under ``tiers.%s`` to opt back in.",
        provider_name,
        provider_name,
    )
    return False


def _resolve_optional_industry_pack(args: Any, logger: logging.Logger):
    """Compile the ``--industry`` flag into an :class:`IndustryPack` if given.

    Returns ``None`` when no flag is supplied or when compilation fails —
    downstream stages treat a missing pack as "no skeleton lint", which
    preserves today's behavior for users who don't opt in.
    """
    industry = getattr(args, "industry", None)
    if not industry:
        return None
    technique = _normalize_technique(getattr(args, "technique", None)) or "data_vault_2"
    try:
        from fluid_build.copilot.industry import IndustryPackCompiler

        return IndustryPackCompiler().compile(industry, technique=technique)
    except Exception as exc:  # noqa: BLE001
        logger.warning("industry_pack_compile_failed: industry=%s error=%s", industry, exc)
        return None


def _resolve_optional_llm_config(args: Any, logger: logging.Logger):
    # Bind the heavy runtime dep as a local (resolves via the module
    # ``__getattr__`` lazy-import seam) so this stays off the light cold path.
    from fluid_build.cli.forge_data_model import resolve_llm_config

    explicit = any(
        getattr(args, attr, None) for attr in ("llm_provider", "llm_model", "llm_endpoint")
    )
    if not explicit:
        return None
    try:
        config = resolve_llm_config(args)
    except Exception as exc:  # noqa: BLE001
        logger.error("could_not_resolve_llm_config: %s", exc)
        raise
    # Surface known capability gaps for the resolved (provider, model)
    # combination *before* the run starts. ``staged_pipeline`` is the
    # right profile for ``fluid forge data-model`` — each stage is a
    # single LLM call, so we only require structured-output
    # enforcement (not tool_use). ``FLUID_QUIET`` / ``FLUID_NONINTERACTIVE``
    # suppress the print but still record warnings to telemetry.
    try:
        import os as _os

        from fluid_build.copilot.agents.capability_catalog import (
            emit_degradation_warnings,
        )

        quiet = (
            bool(getattr(args, "quiet", False))
            or _os.environ.get("FLUID_QUIET") == "1"
            or _os.environ.get("FLUID_NONINTERACTIVE") == "1"
        )
        warnings = emit_degradation_warnings(
            provider=config.provider,
            model=config.model,
            usage_profile="staged_pipeline",
            quiet=quiet,
        )
        if warnings:
            logger.info(
                "capability_warnings_count=%d provider=%s model=%s",
                len(warnings),
                config.provider,
                config.model,
            )
    except Exception as exc:  # noqa: BLE001 — diagnostic, never block
        logger.debug("capability_warning_emit_failed: %s", exc)
    return config


def _normalize_technique(value: Optional[str]) -> Optional[str]:
    # Delegate to the pluggable registry so aliases for built-in AND plugin
    # techniques (e.g. data-vault-2 -> data_vault_2, kimball -> dimensional)
    # resolve to their canonical name. See issue #248.
    from fluid_build.copilot.modeling_techniques import normalize_technique

    return normalize_technique(value)


def _maybe_run_custom_technique(args: Any, logger: logging.Logger) -> Optional[int]:
    """Bring-your-own-model path (issue #248).

    When ``--modeling-technique custom`` is selected, load the user-supplied
    logical model from ``--logical-model`` and emit a contract from it
    **verbatim** — no modeler, no reshaping. Returns an exit code when it
    handled the run, or ``None`` to let the caller proceed with normal
    source-driven modeling.

    Reuses the same ``_finalize_contract`` + ``_write_or_report`` machinery as
    the source-driven path so the emitted artifacts (contract + sidecar + model
    doc + validation report) are identical in shape.
    """
    if _normalize_technique(getattr(args, "technique", None)) != "custom":
        return None
    model_path = getattr(args, "logical_model", None)
    if not model_path:
        cprint(
            "[red]--modeling-technique custom requires --logical-model <path>[/red] "
            "(a .model.json logical model to use verbatim)."
        )
        return 1
    path = Path(model_path)
    if not path.exists():
        cprint(f"[red]--logical-model not found:[/red] {path}")
        return 1
    try:
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — surface the validation error cleanly
        cprint(f"[red]--logical-model is not a valid logical model:[/red] {exc}")
        return 1
    engine = getattr(args, "engine", None) or "dbt"
    output_path = Path(args.output)
    contract = _finalize_contract(
        logical=logical,
        output_path=output_path,
        engine=engine,
        contract=build_contract_from_logical(logical, build_engine=engine),
    )
    return _write_or_report(
        args,
        logger,
        logical=logical,
        contract=contract,
        validation=FluidContractValidator().validate(logical=logical, contract=contract),
        industry_pack=None,
    )


def _print_heuristic_only_banner(
    *, session: StageSession, technique: str, args: Any, logger: logging.Logger
) -> bool:
    """Emit a single-line banner when the dimensional heuristic is about to run.

    UX audit H2 finding: the default heuristic-only path silently
    produces structurally-wrong dimensional models (IDs as measures,
    line-item facts classified as dims, no dim_date). The DV2
    heuristic is solid — only the dimensional path needs the warning.

    Returns ``True`` when the banner was emitted (so callers can also
    stamp ``metadata.warnings`` on the contract).
    """
    # Strict-LLM and "LLM configured" runs need no banner — they get
    # real LLM review.
    if session.llm_config is not None or session.require_llm:
        return False
    # Only the dimensional path is affected by H2. DV2 heuristic is solid.
    if technique != "dimensional":
        return False
    # Silence in non-interactive / deterministic / CI usage so the
    # banner doesn't pollute byte-stable receipts.
    if getattr(args, "deterministic", False) or getattr(args, "quiet", False):
        return False
    cprint(
        "[yellow]note:[/yellow] running offline (no LLM). "
        "Dimensional classification is heuristic-only — for "
        "higher-fidelity fact/dim split, add [bright_cyan]--require-llm[/bright_cyan] "
        "or set [bright_cyan]FLUID_LLM_PROVIDER=...[/bright_cyan]."
    )
    return True


def _stamp_heuristic_warning_on_contract(contract: dict, *, technique: str) -> None:
    """Stamp a heuristic-only warning into the contract labels.

    The audit (H2) flagged that ``Validation passed (score=9)`` was
    misleading: validation is schema-only — it cannot catch
    structurally-wrong classification (line-item table marked as a
    dim, IDs marked as measures). We surface this honestly in the
    contract so downstream consumers see the caveat.

    Uses ``labels.heuristicWarning`` (and ``labels.qualityCaveat``) —
    not ``metadata.warnings``, because the v0.7.x metadata schema is
    ``additionalProperties: false`` and would reject an unknown
    ``warnings`` key.
    """
    if technique != "dimensional":
        return
    labels = contract.setdefault("labels", {})
    if not isinstance(labels, dict):
        return
    labels["heuristicMode"] = "dimensional"
    labels["heuristicWarning"] = (
        "dimensional classification is heuristic-only; review fact/dim "
        "split, measures (no SUM(id) shapes), and dim_date coverage "
        "before production use. Add --require-llm for LLM-assisted "
        "review."
    )


def _finalize_contract(
    *,
    logical: LogicalDraft,
    output_path: Path,
    engine: str,
    contract: Optional[dict] = None,
) -> dict:
    contract = (
        dict(contract)
        if contract is not None
        else build_contract_from_logical(logical, build_engine=engine)
    )
    contract.setdefault("labels", {})
    contract["labels"] = dict(contract["labels"])
    contract["labels"]["modelSidecar"] = f"{output_path.name}.model.json"
    contract["labels"]["modelDoc"] = f"{output_path.name}.model.md"
    return contract


def _write_or_report(
    args: Any,
    logger: logging.Logger,
    *,
    logical: LogicalDraft,
    contract: dict,
    validation: Any,
    industry_pack: Any = None,
) -> int:
    # Bind the heavy runtime dep as a local (resolves via the module
    # ``__getattr__`` lazy-import seam) so this stays off the light cold path.
    from fluid_build.cli.forge_data_model import write_contract

    _print_validation_report(validation)
    if industry_pack is not None:
        _print_canonical_coverage(logical, industry_pack)
    if not validation.passes_schema:
        return 1
    if not getattr(args, "allow_semantic_warnings", False) and _has_canonical_gaps(
        logical, industry_pack
    ):
        cprint(
            "[red]Semantic coverage gate failed:[/red] canonical industry "
            "coverage is incomplete. Re-run with --allow-semantic-warnings "
            "to write advisory artifacts anyway."
        )
        return 1

    output_path = Path(args.output)
    sidecar_path = output_path.with_name(f"{output_path.name}.model.json")
    model_doc_path = output_path.with_name(f"{output_path.name}.model.md")
    if getattr(args, "dry_run", False):
        _print_intent_acceptance(args, logical=logical)
        cprint(f"[green]DRY RUN[/green] Would write contract to {output_path}")
        cprint(f"[green]DRY RUN[/green] Would write logical sidecar to {sidecar_path}")
        if getattr(args, "emit_model_doc", True):
            cprint(f"[green]DRY RUN[/green] Would write model document to {model_doc_path}")
        _print_next_transformation_step(args, output_path=output_path)
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        logical.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    if getattr(args, "review", False):
        reviewed = review_logical_model(sidecar_path, logger, contract_path=output_path)
        if reviewed is not None:
            logical = reviewed
            reviewed_contract = build_contract_from_logical(logical, build_engine=args.engine)
            reviewed_contract.setdefault("labels", {}).update(contract.get("labels", {}))
            contract = _finalize_contract(
                logical=logical,
                output_path=output_path,
                engine=args.engine,
                contract=reviewed_contract,
            )
            validation = FluidContractValidator().validate(logical=logical, contract=contract)
            _print_validation_report(validation)
            if not validation.passes_schema:
                return 1
            sidecar_path.write_text(
                logical.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
            )

    if getattr(args, "emit_model_doc", True):
        model_doc_path.write_text(emit_model_markdown(logical), encoding="utf-8")
    else:
        (contract.get("labels") or {}).pop("modelDoc", None)

    write_contract(contract, output_path, command="fluid forge data-model")
    _write_auxiliary_artifacts(args, output_path=output_path, logical=logical)
    try:
        archive_snapshot(
            contract=contract,
            logical_model=logical.model_dump(mode="json", by_alias=True),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("forge_data_model_history_archive_failed: %s", exc)
    try:
        write_audit_event(
            "forge_data_model",
            payload={
                "output_path": str(output_path),
                "technique": logical.technique,
                "deterministic": bool(getattr(args, "deterministic", False)),
                "agentic_mode": contract.get("labels", {}).get("agenticMode"),
                "agentic_fallback_used": contract.get("labels", {}).get("agenticFallbackUsed"),
                "agentic_fallback_stages": contract.get("labels", {}).get("agenticFallbackStages"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("forge_data_model_audit_write_failed: %s", exc)
    if contract.get("labels", {}).get("agenticFallbackUsed") == "true":
        stages = contract.get("labels", {}).get("agenticFallbackStages", "unknown")
        cprint(f"[yellow]Agentic fallback used[/yellow] (stages={stages})")
    _print_intent_acceptance(args, logical=logical)
    cprint(f"[green]Wrote contract[/green] {output_path}")
    cprint(f"[green]Wrote logical sidecar[/green] {sidecar_path}")
    if getattr(args, "emit_model_doc", True):
        cprint(f"[green]Wrote model document[/green] {model_doc_path}")
    _print_next_transformation_step(args, output_path=output_path)
    return 0


def _print_intent_acceptance(args: Any, *, logical: LogicalDraft) -> None:
    if not getattr(args, "intent_source_path", None):
        return
    cprint(f"[green]Intent file accepted[/green] {args.intent_source_path}")
    technique = getattr(args, "selected_modeling_technique", None) or logical.technique
    cprint(f"Selected modeling technique: {technique}")


def _print_next_transformation_step(args: Any, *, output_path: Path) -> None:
    if not getattr(args, "intent_source_path", None):
        return
    base = _contract_base_name(output_path)
    out_dir = f"./dbt_{_safe_artifact_slug(base)}"
    command = f"fluid generate transformation {output_path} -o {out_dir}"
    if getattr(args, "engine", "dbt") == "dbt":
        command += " --dbt-validate"
    cprint(f"Next: {command}")


def _contract_base_name(output_path: Path) -> str:
    name = output_path.name
    if name.endswith(".fluid.yaml"):
        return name[: -len(".fluid.yaml")]
    if name.endswith(".fluid.yml"):
        return name[: -len(".fluid.yml")]
    return output_path.stem or "model"


def _safe_artifact_slug(value: str) -> str:
    return _SAFE_ARTIFACT_FILENAME_RE.sub("_", value).strip("._") or "model"


def review_logical_model(
    sidecar_path: Path,
    logger: logging.Logger,
    *,
    contract_path: Optional[Path] = None,
) -> Optional[LogicalDraft]:
    if contract_path is not None:
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fluid_build.cli",
                    "viz-graph",
                    str(contract_path),
                    "--quiet",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("review_gate_viz_graph_failed: %s", exc)
    editor = os.environ.get("EDITOR")
    if not editor:
        cprint(
            "[yellow]Review requested but $EDITOR is not set; skipping interactive review.[/yellow]"
        )
        return None
    subprocess.run([editor, str(sidecar_path)], check=False)
    try:
        return LogicalDraft.model_validate_json(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("review_gate_invalid_sidecar: %s", exc)
        cprint("[red]Edited logical sidecar is invalid JSON or no longer matches the schema.[/red]")
        return None


def diff_logical_models(old_path: Path, new_path: Path) -> dict:
    return __import__(
        "fluid_build.forge_datamodel.diff", fromlist=["diff_logical_models"]
    ).diff_logical_models(old_path, new_path)


def _write_auxiliary_artifacts(args: Any, *, output_path: Path, logical: LogicalDraft) -> None:
    if getattr(args, "emit_osi_sidecar", True):
        osi_path = output_path.with_suffix(output_path.suffix + ".semantics.osi.yaml")
        osi_path.write_text(emit_osi_yaml(logical), encoding="utf-8")
        cprint(f"[green]Wrote OSI sidecar[/green] {osi_path}")

    ddl_dir = getattr(args, "emit_ddl_dir", None)
    if ddl_dir:
        ddl_path = Path(ddl_dir)
        ddl_path.mkdir(parents=True, exist_ok=True)
        used_names: set[str] = set()
        for name, content in emit_ddl_files(logical).items():
            _safe_child_path(ddl_path, name, used_names=used_names).write_text(
                content, encoding="utf-8"
            )
        cprint(f"[green]Wrote DDL files[/green] {ddl_path}")

    variants_dir = getattr(args, "emit_dimensional_variants", None)
    if variants_dir:
        variant_path = Path(variants_dir)
        variant_path.mkdir(parents=True, exist_ok=True)
        used_names: set[str] = set()
        for name, content in emit_dimensional_variants(logical).items():
            _safe_child_path(variant_path, name, used_names=used_names).write_text(
                content, encoding="utf-8"
            )
        cprint(f"[green]Wrote dimensional variants[/green] {variant_path}")


def _safe_child_path(root: Path, filename: str, *, used_names: set[str]) -> Path:
    """Return a sanitized artifact path guaranteed to stay under ``root``."""

    root = Path(root).resolve()
    raw_name = str(filename or "").replace("\\", "/")
    leaf = Path(raw_name).name
    safe_name = _SAFE_ARTIFACT_FILENAME_RE.sub("_", leaf).strip("._")
    if not safe_name:
        safe_name = "artifact"

    candidate_name = safe_name
    suffix = 2
    while candidate_name.lower() in used_names:
        ext = "".join(Path(safe_name).suffixes)
        stem = safe_name[: -len(ext)] if ext else safe_name
        stem = stem or "artifact"
        candidate_name = f"{stem}_{suffix}{ext}"
        suffix += 1
    used_names.add(candidate_name.lower())

    target = (root / candidate_name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Unsafe auxiliary artifact filename {filename!r}: resolved outside {root}"
        ) from exc
    return target


def _print_validation_report(report: Any) -> None:
    if report.passes_schema:
        cprint(f"[green]Validation passed[/green] (score={report.score})")
        return
    cprint(f"[red]Validation failed[/red] (score={report.score})")
    for issue in report.issues:
        prefix = f"{issue.severity.upper()}:"
        if issue.field:
            cprint(f"  {prefix} [{issue.field}] {issue.message}")
        else:
            cprint(f"  {prefix} {issue.message}")


def _print_canonical_coverage(logical: LogicalDraft, industry_pack: Any) -> None:
    """Print the canonical-model coverage block after validation.

    Silent when the pack has no seed skeleton — the summary is purely
    informational, never a gate. Colour signals: green header + ticks
    when every expected entity is present (possibly via drifted name),
    yellow when at least one group is missing canonical entities.
    """
    summary = compute_canonical_coverage(logical, industry_pack)
    if summary is None:
        return
    rendered = summary.render()
    if not rendered:
        return
    colour = "green" if summary.is_clean else "yellow"
    cprint(f"[{colour}]{rendered}[/{colour}]")


def _has_canonical_gaps(logical: LogicalDraft, industry_pack: Any) -> bool:
    if industry_pack is None:
        return False
    summary = compute_canonical_coverage(logical, industry_pack)
    return summary is not None and not summary.is_clean


# ---------------------------------------------------------------------
# Friendly fallback when the user typed ``fluid forge data-model``
# without a subcommand.  Replaces the bare-bones argparse error with
# a Rich panel + cwd-aware "Recommended:" hint.
# ---------------------------------------------------------------------


def _render_data_model_guide() -> int:
    """Render an intuitive guide for ``fluid forge data-model`` with no
    subcommand.  Detects DDL files, intent files, and configured
    metadata sources in the cwd / ``~/.fluid/sources.yaml`` and
    promotes the most-relevant subcommand."""

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        hint_from_first_match,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="from-intent",
            description="Forge from a YAML/JSON business intent file (recommended for greenfield).",
            example="fluid forge data-model from-intent intent.yaml -o contract.fluid.yaml",
        ),
        SubcommandEntry(
            name="from-ddl",
            description="Forge from existing DDL files (CREATE TABLE statements).",
            example="fluid forge data-model from-ddl --ddl schema.sql -o contract.fluid.yaml",
        ),
        SubcommandEntry(
            name="from-source",
            description=(
                "Forge from a configured metadata catalog "
                "(Snowflake / Unity / BigQuery / Dataplex / Glue / DataHub / DMM)."
            ),
            example=(
                "fluid forge data-model from-source --source snowflake "
                "--credential-id <name> --database <db> --schema <schema> "
                "-o contract.fluid.yaml"
            ),
        ),
        SubcommandEntry(
            name="validate",
            description="Validate a forged contract or logical sidecar.",
            example="fluid forge data-model validate contract.fluid.yaml",
        ),
        SubcommandEntry(
            name="diff",
            description="Diff two forged logical sidecars.",
            example="fluid forge data-model diff a.model.json b.model.json",
        ),
        SubcommandEntry(
            name="learn",
            description=(
                "Capture operator edits between an original forged contract "
                "and a hand-edited version into memory/semantic."
            ),
            example=(
                "fluid forge data-model learn "
                "--original orig.fluid.yaml --edited final.fluid.yaml --name my_model"
            ),
        ),
        SubcommandEntry(
            name="dump-ddl",
            description=(
                "Dump DDL from a Snowflake database.schema to a .sql file "
                "(useful as input for from-ddl)."
            ),
            example=(
                "fluid forge data-model dump-ddl "
                "--database <DB> --schema <SCHEMA> --output schema.sql"
            ),
        ),
    ]

    def _detect_hint() -> Any:
        from pathlib import Path

        from fluid_build.cli._subcommand_guide import SubcommandHint

        cwd = Path.cwd()
        # Intent files explicitly describe the data product the user wants.
        intent_candidates = (
            list(cwd.glob("*.intent.yaml"))
            + list(cwd.glob("intent.yaml"))
            + list(cwd.glob("intent.yml"))
            + list(cwd.glob("intent.json"))
        )
        if intent_candidates:
            return SubcommandHint(
                subcommand="from-intent",
                rationale=(
                    f"found intent file ({intent_candidates[0].name}) in the current directory."
                ),
            )
        # DDL files are the next-best signal.
        ddl_candidates = list(cwd.glob("*.sql"))
        if ddl_candidates:
            return SubcommandHint(
                subcommand="from-ddl",
                rationale=(f"found {len(ddl_candidates)} DDL file(s) in the current directory."),
            )
        # Configured metadata sources are the next leverage point —
        # fewest-keystrokes path to a real forge once nothing else is local.
        sources_yaml = Path.home() / ".fluid" / "sources.yaml"
        if sources_yaml.is_file():
            return SubcommandHint(
                subcommand="from-source",
                rationale=(
                    "you have a metadata-source catalog configured in ~/.fluid/sources.yaml."
                ),
            )
        return None

    guide = SubcommandGuide(
        command_path="fluid forge data-model",
        headline=(
            "Forge a reviewable data model contract from one of several "
            "input shapes.  Pick the path that matches what you have."
        ),
        entries=entries,
        hint_provider=_detect_hint,
        quick_start=(
            "fluid forge data-model from-intent --example "
            "(prints a starter intent.yaml; also try --schema or --validate)"
        ),
    )
    return render_subcommand_guide(guide)
