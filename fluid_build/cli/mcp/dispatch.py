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
MCP sync dispatch layer — ``_call_tool`` and all helper functions.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import argparse as _argparse
import json
import logging
import os
import re
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import yaml

from fluid_build.cli.mcp.models import (
    _CATALOG_SOURCE_LIST,
    _JDBC_SOURCE_LIST,
    TOOL_CAPABILITIES,
)
from fluid_build.cli.mcp.policy import _path_is_writable
from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.copilot.store.factory import resolve_store
from fluid_build.copilot.store.history import archive_snapshot
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

logger = logging.getLogger(__name__)

# Re-export policy getter so callers don't need a separate import.
from fluid_build.cli.mcp.policy import _policy  # noqa: E402, F401


def _pkg():
    """Return the ``fluid_build.cli.mcp`` package module.

    Dispatch resolves a few patchable seams (``write_audit_event``,
    ``_build_source_adapter``) through the package object so that
    ``monkeypatch.setattr("fluid_build.cli.mcp.<name>", ...)`` in tests flows
    through to the call sites here — the post-split equivalent of the
    monolith's single-module global lookup (the dispatch code and the patch
    target used to share one module namespace). Pinned by
    tests/copilot/catalog/test_catalog_mcp_tools.py.
    """
    import fluid_build.cli.mcp as _mcp_pkg

    return _mcp_pkg


def _build_source_adapter(
    arguments,
    *,
    allow_inline_credentials: bool = False,
    allow_metadata_service: bool = False,
):
    """Package-level indirection wrapper for the real adapter builder.

    Delegates to :func:`_build_source_adapter_impl`, but resolves it through
    the package module so a ``monkeypatch.setattr`` on
    ``fluid_build.cli.mcp._build_source_adapter`` replaces what the dispatch
    paths actually call.
    """
    builder = getattr(_pkg(), "_build_source_adapter", None)
    # If the package attribute is this very wrapper (un-patched), fall through
    # to the real implementation to avoid infinite recursion.
    if builder is None or builder is _build_source_adapter:
        return _build_source_adapter_impl(
            arguments,
            allow_inline_credentials=allow_inline_credentials,
            allow_metadata_service=allow_metadata_service,
        )
    return builder(
        arguments,
        allow_inline_credentials=allow_inline_credentials,
        allow_metadata_service=allow_metadata_service,
    )


def _write_audit_event(*args, **kwargs):
    """Package-level indirection wrapper for ``write_audit_event``.

    Resolves the callable through the package so test monkeypatches on
    ``fluid_build.cli.mcp.write_audit_event`` take effect at the dispatch
    call sites.
    """
    fn = getattr(_pkg(), "write_audit_event", write_audit_event)
    return fn(*args, **kwargs)


class LogicalDraftError(RuntimeError):
    """A sidecar could not be read or parsed; carries no raw exception text."""

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message

    def as_result(self) -> Dict[str, Any]:
        """The typed tool-result shape the hardened twin returns."""
        return {"error": self.error, "message": self.message}


def _load_logical_draft(path: Path):
    """Read + parse a ``.model.json`` sidecar, or raise a sanitised error.

    Typed-tool-error invariant (issue #392): the detail — absolute host
    paths and errno text from ``OSError``, and, from pydantic's
    ``ValidationError``, a truncated echo of the *file contents* — is
    logged server-side and never round-tripped into the LLM's tool-use
    context. The hardened in-process twin
    (``forge_copilot_tools._dispatch_read_logical_model``) already did
    this; the ``fluid mcp serve`` path called ``LogicalDraft
    .model_validate_json(path.read_text(...))`` bare, so the same tool
    leaked through a different entry point.

    Raises:
        LogicalDraftError: with a message safe to show a model. The
            caller decides whether to surface it as a typed result dict
            (read path) or let it propagate (mutating tools).
    """
    from fluid_build.copilot.schemas.stage_outputs import LogicalDraft

    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("read_logical_model: failed to read %s: %s", path, exc, exc_info=True)
        raise LogicalDraftError(
            type(exc).__name__, f"could not read {path.name} — see server logs"
        ) from exc

    try:
        return LogicalDraft.model_validate_json(body)
    except Exception as exc:  # pydantic ValidationError (also covers invalid JSON)
        logger.warning("read_logical_model: invalid sidecar %s: %s", path, exc, exc_info=True)
        raise LogicalDraftError(
            type(exc).__name__, f"{path.name} is not a valid logical model — see server logs"
        ) from exc


def _call_tool(
    name: str,
    arguments: Dict[str, Any],
    *,
    read_only: bool,
    allow_inline_credentials: bool = False,
    allow_metadata_service: bool = False,
) -> Dict[str, Any]:
    """Dispatch a named tool to its implementation (sync, runs in worker thread).

    Permission gating is done by the async wrapper before calling this.
    ``read_only`` is passed explicitly rather than read from the policy
    to keep this function synchronous and thread-safe.
    """
    from fluid_build.cli.forge_data_model import diff_logical_models
    from fluid_build.copilot.schemas.stage_outputs import ConceptualRelationship

    # Every source-catalog tool builds its adapter under the operator's two
    # credential gates (``--allow-inline-credentials``, ``--allow-metadata-service``).
    build_adapter = partial(
        _build_source_adapter,
        allow_inline_credentials=allow_inline_credentials,
        allow_metadata_service=allow_metadata_service,
    )

    if name == "read_logical_model":
        path = Path(arguments["path"])
        try:
            return _load_logical_draft(path).model_dump(mode="json", by_alias=True)
        except LogicalDraftError as exc:
            return exc.as_result()
    if name == "update_entity":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = _load_logical_draft(path)
        before = logical.model_dump(mode="json", by_alias=True)
        target = arguments["entity"]
        updates = arguments.get("updates") or {}
        if logical.conceptual:
            for entity in logical.conceptual.entities:
                if entity.name == target:
                    for key, value in updates.items():
                        if hasattr(entity, key):
                            setattr(entity, key, value)
        path.write_text(logical.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        archive_snapshot(contract={}, logical_model=before)
        _write_audit_event("mcp_update_entity", payload={"path": str(path), "entity": target})
        return {"updated": True}
    if name == "add_relationship":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = _load_logical_draft(path)
        if logical.conceptual is None:
            raise RuntimeError("Logical model has no conceptual section")
        before = logical.model_dump(mode="json", by_alias=True)
        logical.conceptual.relationships.append(ConceptualRelationship(**arguments["relationship"]))
        path.write_text(logical.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        archive_snapshot(contract={}, logical_model=before)
        _write_audit_event("mcp_add_relationship", payload={"path": str(path)})
        return {"updated": True}
    if name == "regenerate_physical":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = _load_logical_draft(path)
        contract = build_contract_from_logical(
            logical, build_engine=str(arguments.get("engine") or "dbt")
        )
        contract_path = Path(arguments.get("contract_path") or f"{path}.fluid.yaml")
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        archive_snapshot(
            contract=contract, logical_model=logical.model_dump(mode="json", by_alias=True)
        )
        _write_audit_event(
            "mcp_regenerate_physical",
            payload={"path": str(path), "contract_path": str(contract_path)},
        )
        return {"contract_path": str(contract_path)}
    if name == "validate_contract":
        validator = FluidContractValidator()
        logical = None
        contract = None
        if arguments.get("logical_path"):
            logical = _load_logical_draft(Path(arguments["logical_path"]))
        if arguments.get("contract_path"):
            contract = yaml.safe_load(Path(arguments["contract_path"]).read_text(encoding="utf-8"))
        return validator.validate(logical=logical, contract=contract).model_dump(
            mode="json", by_alias=True
        )
    if name == "diff_models":
        return diff_logical_models(Path(arguments["old"]), Path(arguments["new"]))
    if name == "search_semantic_memory":
        store = resolve_store(workspace_root=Path.cwd())
        results = store.search(
            "memory/semantic",
            str(arguments.get("query") or ""),
            mode=str(arguments.get("mode") or "hybrid"),
            limit=int(arguments.get("limit") or 5),
        )
        return {"results": [record.value for record in results]}

    # ---------------------------------------------------------------
    # V1.5 — metadata source-catalog tools.
    # ---------------------------------------------------------------
    if name == "list_source_adapters":
        return {
            "adapters": _list_source_adapters(),
        }

    if name == "list_source_tables":
        adapter = build_adapter(arguments)
        scope = _scope_from_args(arguments)
        tables = adapter.list_tables(scope)
        _write_audit_event(
            "mcp_list_source_tables",
            payload={
                **adapter.audit_context(),
                "scope": scope.model_dump(mode="json", by_alias=True),
                "result_count": len(tables),
            },
        )
        return {"tables": [t.model_dump(mode="json", by_alias=True) for t in tables]}

    if name == "inspect_source_table":
        adapter = build_adapter(arguments)
        fqn = str(arguments.get("fqn") or "")
        if not fqn:
            raise RuntimeError("inspect_source_table requires 'fqn'")
        table = adapter.get_table(fqn)
        _write_audit_event(
            "mcp_inspect_source_table",
            payload={**adapter.audit_context(), "fqn": fqn},
        )
        return table.model_dump(mode="json", by_alias=True)

    if name == "list_source_lineage":
        adapter = build_adapter(arguments)
        fqn = str(arguments.get("fqn") or "")
        if not fqn:
            raise RuntimeError("list_source_lineage requires 'fqn'")
        lineage = adapter.get_lineage(fqn)
        _write_audit_event(
            "mcp_list_source_lineage",
            payload={**adapter.audit_context(), "fqn": fqn},
        )
        return lineage.model_dump(mode="json", by_alias=True)

    if name == "list_source_glossary":
        adapter = build_adapter(arguments)
        scope = _scope_from_args(arguments)
        terms = adapter.list_glossary_terms(scope)
        _write_audit_event(
            "mcp_list_source_glossary",
            payload={**adapter.audit_context()},
        )
        return {"terms": [t.model_dump(mode="json", by_alias=True) for t in terms]}

    if name == "forge_from_source":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")

        # JDBC sources route through a separate code path that doesn't
        # need a credential resolver — the URI carries everything. This
        # mirrors ``cli/forge_data_model.py::run_from_source_command``
        # which forks ``--source <jdbc>`` early to ``_run_from_jdbc_source``.
        # MCP gets the same one-shot synthesis (no separate connect step).
        jdbc_kinds = {"postgres", "postgresql", "mysql", "sqlite"}
        source_value = str(arguments.get("source") or "").lower().strip()
        if source_value in jdbc_kinds:
            # A JDBC source reaches a database — and this host's credentials,
            # files and identity — just as a catalog credential block does, so
            # it is classified under the same operator gates before any
            # connection is attempted.
            _gate_jdbc_source(
                source_value,
                arguments.get("uri"),
                allow_inline_credentials=allow_inline_credentials,
                allow_metadata_service=allow_metadata_service,
            )
            return _dispatch_forge_from_jdbc_source(arguments)

        # ``resolve_store`` is imported at module level — do NOT
        # re-import inside this branch, otherwise Python treats it
        # as a function-local for the entire ``_call_tool`` body
        # and the search_semantic_memory branch above this one
        # crashes with UnboundLocalError.
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.agents.logical_agent import LogicalAgent

        adapter = build_adapter(arguments)
        scope = _scope_from_args(arguments)
        technique = str(arguments.get("technique") or "data_vault_2")
        engine = str(arguments.get("engine") or "dbt")
        model_name = str(arguments.get("name") or scope.schema_name or "forged_model")
        output_path = arguments.get("output_path")
        if not output_path:
            raise RuntimeError("forge_from_source requires 'output_path'")

        store = resolve_store(workspace_root=Path.cwd())
        session = StageSession(store=store)
        logical = LogicalAgent().from_catalog(
            session,
            name=model_name,
            adapter=adapter,
            scope=scope,
            technique=technique,
        )
        sidecar_payload = logical.model_dump(mode="json", by_alias=True)
        contract = build_contract_from_logical(logical, build_engine=engine)

        contract_path = Path(str(output_path))
        sidecar_path = (
            Path(str(arguments.get("logical_path")))
            if arguments.get("logical_path")
            else contract_path.with_name(f"{contract_path.name}.model.json")
        )
        contract.setdefault("labels", {})
        contract["labels"] = dict(contract["labels"])
        contract["labels"]["modelSidecar"] = sidecar_path.name

        validation = FluidContractValidator().validate(logical=logical, contract=contract)
        if not validation.passes_schema:
            raise RuntimeError(
                "forge_from_source produced an invalid contract: "
                + "; ".join(issue.message for issue in validation.issues[:5])
            )

        contract_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(
            json.dumps(sidecar_payload, indent=2, default=str), encoding="utf-8"
        )
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        archive_snapshot(contract=contract, logical_model=sidecar_payload)

        _write_audit_event(
            "mcp_forge_from_source",
            payload={
                **adapter.audit_context(),
                "scope": scope.model_dump(mode="json", by_alias=True),
                "technique": technique,
                "model_name": model_name,
                "contract_path": str(contract_path),
                "sidecar_path": str(sidecar_path) if sidecar_path else None,
                "table_count": (
                    len(logical.dimensional.facts) + len(logical.dimensional.dimensions)
                    if logical.dimensional
                    else (len(logical.dv2.hubs) if logical.dv2 else 0)
                ),
            },
        )
        return {
            "logical": sidecar_payload,
            "contract_path": str(contract_path),
            "sidecar_path": str(sidecar_path) if sidecar_path else None,
            "validation": validation.model_dump(mode="json", by_alias=True),
        }

    # ``forge_run`` is NOT dispatched through ``_call_tool`` — it's a FastMCP
    # tool with its own async implementation (see ``forge_run`` above) so it
    # can call ``ctx.session.create_message`` for the sampling round-trip.
    # Any caller that lands here with name='forge_run' is a bug.

    if name == "score_contract_quality":
        return _dispatch_score_contract_quality(arguments)
    if name == "enrich_contract_suggestions":
        return _dispatch_enrich_contract_suggestions(arguments)
    raise RuntimeError(f"Unknown tool {name}")


def _resolve_contract_argument(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either ``contract_path`` (filesystem) or ``contract`` (inline dict).

    Path takes precedence when both supplied. The path read is what the
    capability's ``read_path_args`` policy gates on; inline dicts come
    straight from the MCP caller (command_center / IDE) without
    filesystem confinement.
    """
    path_arg = arguments.get("contract_path")
    inline = arguments.get("contract")
    if path_arg:
        text = Path(path_arg).read_text(encoding="utf-8")
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Contract at {path_arg!r} did not parse to a dict")
        return loaded
    if inline is not None:
        if not isinstance(inline, dict):
            raise RuntimeError("'contract' argument must be a dict")
        return inline
    raise RuntimeError("Pass either 'contract_path' or 'contract'")


def _dispatch_score_contract_quality(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :class:`JudgeAgent`. Read-only — no contract writes."""
    from fluid_build.copilot.agents.judge_agent import JudgeAgent

    contract = _resolve_contract_argument(arguments)
    build_artifacts: Optional[Dict[str, Any]] = None
    if bool(arguments.get("include_artifacts")):
        from fluid_build.copilot.enrichment import enrich_contract as _enrich

        try:
            build_artifacts = _enrich(contract)
        except Exception:  # noqa: BLE001 — judging without artifacts is still valid
            build_artifacts = None
    result = JudgeAgent().judge(contract, build_artifacts=build_artifacts)
    return {
        "total": result.total,
        "axes": {axis: score.score for axis, score in result.axes.items()},
        "axis_reasoning": {axis: score.reasoning for axis, score in result.axes.items()},
        "axis_suggestions": {axis: list(score.suggestions) for axis, score in result.axes.items()},
        "model": result.model,
        "critique_applied": bool(getattr(result, "critique_applied", False)),
        "max_total": len(result.axes) * 5,
    }


def _dispatch_enrich_contract_suggestions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :func:`enrich_contract`. Read-only — returns
    suggestions; does not apply them to the contract on disk."""
    from fluid_build.copilot.enrichment import enrich_contract as _enrich

    contract = _resolve_contract_argument(arguments)
    artifacts = _enrich(contract)
    if artifacts is None:
        return {"enabled": False, "artifacts": None}
    return {"enabled": True, "artifacts": artifacts}


# ---------------------------------------------------------------------------
# V1.5 source-catalog dispatch helpers.
# ---------------------------------------------------------------------------


def _list_source_adapters() -> List[Dict[str, Any]]:
    """Enumerate the source-catalog + JDBC adapters this build of
    forge-cli can dispatch to.

    The list is static — it reflects what code is shipped, not what
    the operator has configured. To list configured *credentials*
    (which catalogs the operator has actually set up), use the
    ``fluid ai status`` CLI surface (Sprint C). The MCP tool is
    deliberately inventory-only: it tells the LLM which catalog
    types are reachable, not which specific credentials are saved.

    Catalog adapters (kind=catalog) are implemented in
    ``fluid_build.copilot.catalog.<name>`` and follow the 9 patterns
    in ``catalog._patterns``. JDBC adapters (kind=jdbc) route through
    :mod:`fluid_build.cli._forge_data_model_jdbc` — duckdb-extension
    introspection over a ``--uri`` payload.

    Future adapters (Apache Atlas, Alation, Microsoft Purview, …) get
    added here when they land — and inherit the same patterns
    automatically.
    """
    catalog_entries = [
        {"name": name, "status": "available", "kind": "catalog"} for name in _CATALOG_SOURCE_LIST
    ]
    jdbc_entries = [
        {"name": name, "status": "available", "kind": "jdbc"} for name in _JDBC_SOURCE_LIST
    ]
    return catalog_entries + jdbc_entries


# Single source of truth for catalog dispatch. Every adapter
# implements ``CatalogAdapter.from_resolver`` so we just need the
# class reference here. New adapters land by adding one entry.
#
# JDBC sources (postgres / postgresql / mysql / sqlite) are NOT in this
# table — they route through ``_dispatch_forge_from_jdbc_source`` which
# uses duckdb-extension introspection rather than a credential-resolver
# adapter. ``_build_source_adapter`` only handles catalog sources;
# ``forge_from_source`` short-circuits to the JDBC path before calling
# it. The catalog-only tools (list_source_tables, inspect_source_table,
# list_source_lineage, list_source_glossary) reject JDBC sources via
# the ``Literal[...]`` enum on their Annotated signatures.
_SOURCE_ADAPTERS: Dict[str, str] = {
    "snowflake": "fluid_build.copilot.catalog.snowflake:SnowflakeCatalogAdapter",
    "unity": "fluid_build.copilot.catalog.unity:UnityCatalogAdapter",
    "bigquery": "fluid_build.copilot.catalog.bigquery:BigQueryCatalogAdapter",
    "dataplex": "fluid_build.copilot.catalog.dataplex:DataplexCatalogAdapter",
    "glue": "fluid_build.copilot.catalog.glue:GlueCatalogAdapter",
    "datahub": "fluid_build.copilot.catalog.datahub:DataHubCatalogAdapter",
    "datamesh_manager": (
        "fluid_build.copilot.catalog.datamesh_manager:DataMeshManagerCatalogAdapter"
    ),
}


def _dispatch_forge_from_jdbc_source(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :func:`_run_from_jdbc_source` (the CLI's JDBC path).

    JDBC sources (postgres / postgresql / mysql / sqlite) carry credentials
    inline in a ``--uri`` payload, so they bypass the credential resolver
    entirely. This shim builds a minimal argparse-Namespace shaped like the
    ``fluid forge data-model from-source`` parser produces, dispatches to
    the shared duckdb-attach helper, and re-reads the produced contract so
    the MCP caller gets the same dict shape catalog sources return.

    Audit event: ``mcp_forge_from_jdbc_source`` is written (no credentials —
    URI password is masked; only the source kind + output path land in the
    forensic trail).
    """
    from fluid_build.cli._forge_data_model_jdbc import _run_from_jdbc_source

    source = str(arguments.get("source") or "").lower().strip()
    uri = arguments.get("uri")
    output_path = arguments.get("output_path")
    if not uri:
        raise RuntimeError(
            f"forge_from_source --source {source!r} requires 'uri'. "
            "Example: postgresql://user:pass@host:5432/db"  # pragma: allowlist secret
        )
    if not output_path:
        raise RuntimeError("forge_from_source requires 'output_path'")

    # The JDBC helper consumes ``args.source``, ``args.uri``, ``args.output``,
    # ``args.name``, ``args.schema_name``, ``args.tables``. Build an
    # argparse Namespace with exactly those attributes — leaving the rest
    # absent is fine since the helper uses ``getattr(args, ..., None)``.
    scope = arguments.get("scope") or {}
    if not isinstance(scope, dict):
        scope = {}
    namespace = _argparse.Namespace(
        source=source,
        uri=str(uri),
        output=str(output_path),
        name=arguments.get("name"),
        schema_name=scope.get("schema") or scope.get("schema_name"),
        tables=scope.get("tables") or None,
    )
    jdbc_logger = logging.getLogger("fluid.mcp.forge_from_jdbc_source")
    # No operator_supplied here, ever: the URI is the client's, so a password
    # connection stays pinned to its own material and never presents this
    # host's client certificate or libpq settings.
    rc = _run_from_jdbc_source(namespace, jdbc_logger)
    if rc != 0:
        raise RuntimeError(
            f"forge_from_source: JDBC introspection failed for source={source!r} "
            f"(exit_code={rc}). See server logs."
        )

    # Re-read the emitted contract so the MCP caller gets a structured
    # response (mirroring the catalog branch's shape).
    contract_path = Path(str(output_path))
    contract_text = contract_path.read_text(encoding="utf-8")
    contract_data = yaml.safe_load(contract_text) or {}

    _write_audit_event(
        "mcp_forge_from_jdbc_source",
        payload={
            "source": source,
            "output_path": str(contract_path),
            # Deliberately NOT logging the URI — passwords can be embedded.
        },
    )

    return {
        "kind": "jdbc",
        "source": source,
        "contract_path": str(contract_path),
        "contract_exists": contract_path.is_file(),
        "table_count": len(contract_data.get("exposes") or []),
    }


def _import_adapter_class(dotted_path: str) -> Any:
    """Resolve ``module.path:ClassName`` into the actual class.

    Lazy import keeps ``fluid --help`` cold-start fast — only the
    requested adapter's module is imported. Pattern 4 applied at the
    dispatch layer.
    """
    module_path, class_name = dotted_path.split(":", 1)
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


class McpToolRefusal(RuntimeError):
    """An anticipated refusal whose message must reach the MCP client intact.

    The server layer re-raises it as the SDK's ``ToolError`` (see
    :func:`fluid_build._mcp_compat.get_tool_error`); on SDK 2.x a bare
    ``RuntimeError`` reaches the client only as "Error executing tool <name>",
    which would hide which server flag the operator needs.
    """


def _metadata_service_opt_in(arguments: Dict[str, Any], *, allow_metadata_service: bool) -> bool:
    """Return whether this call may use the metadata-service credential fallback.

    The client's ``allow_metadata_service`` argument can only *request* the
    fallback. Granting it is the operator's call, made at server start with
    ``fluid mcp serve --allow-metadata-service`` or with
    ``FLUID_ALLOW_METADATA_SERVICE=1`` in the server's own environment (the
    same switch :class:`CredentialResolver` honours). A request without that
    opt-in is refused, never silently ignored, so the caller learns why.

    Same shape as the inline-credentials gate below, and as the operator-wins
    precedence of github-mcp-server's ``--read-only`` / ``GITHUB_READ_ONLY``
    (docs/server-configuration.md): a per-request value never relaxes a
    server-side setting.
    """
    if not arguments.get("allow_metadata_service"):
        return False
    if _operator_allows_metadata_service(allow_metadata_service):
        return True
    raise McpToolRefusal(
        "Source-catalog tools refused allow_metadata_service over MCP: the "
        "cloud metadata-service credential fallback (instance role, workload "
        "identity, gcloud ADC) uses this host's ambient cloud identity and "
        "must be enabled by the operator. Retry without allow_metadata_service "
        "and pass credentials.credential_id, or restart the server with "
        "--allow-metadata-service (or FLUID_ALLOW_METADATA_SERVICE=1 in the "
        "server's environment)."
    )


def _operator_allows_metadata_service(allow_metadata_service: bool) -> bool:
    """The operator's opt-in: ``--allow-metadata-service`` or the env var."""
    return allow_metadata_service or os.environ.get("FLUID_ALLOW_METADATA_SERVICE") == "1"


_JDBC_URI_SCHEMES = {
    "postgres": ("postgres", "postgresql"),
    "mysql": ("mysql",),
    "sqlite": ("sqlite",),
}

# An absolute path on Windows, which libpq also reads as a socket directory.
_WINDOWS_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


def _scan_to(text: str, pos: int, stops: str) -> int:
    """The index of the first character at or after ``pos`` that is in ``stops``."""
    while pos < len(text) and text[pos] not in stops:
        pos += 1
    return pos


def _libpq_uri_parts(uri: str) -> Tuple[str, str, List[str]]:
    """``(user, password, hosts)`` of a postgres URI, read the way libpq reads it.

    Adapted from ``conninfo_uri_parse_options`` in PostgreSQL's
    ``src/interfaces/libpq/fe-connect.c`` (PostgreSQL License): the
    ``user:password`` part ends at the first ``@`` before any ``/``, and is
    percent-decoded. ``hosts`` is :func:`_libpq_hosts` of the rest.
    """
    rest = uri.partition("://")[2]
    user = password = ""
    if "@" in rest[: _scan_to(rest, 0, "/")]:
        userinfo, _, rest = rest.partition("@")
        user, _, password = userinfo.partition(":")
    return unquote(user), unquote(password), _libpq_hosts(rest)


def _libpq_hosts(rest: str) -> List[str]:
    """Every host libpq would take from ``rest``, the part of a URI after its user information.

    Adapted from ``conninfo_uri_parse_options`` / ``conninfo_uri_parse_params``
    in PostgreSQL's ``src/interfaces/libpq/fe-connect.c`` (PostgreSQL License):
    the host list is a run of comma-separated ``host[:port]`` items that ends at
    ``/`` or ``?``, where an item that opens with ``[`` is an IPv6 address up to
    ``]``; query parameters are ``key=value`` pairs split on ``&``; and every
    part is percent-decoded (``+`` is not a space). libpq decodes the host part
    as one string and only then splits it on ``,``, so an encoded comma
    (``%2C``) separates hosts like a bare one: ``db%2C`` is ``db`` and then the
    default socket. The result is every item of the decoded host list plus every
    comma-separated item of a ``host=`` or ``hostaddr=`` parameter. A spelling
    libpq would refuse is read leniently: this only classifies, it never
    connects.
    """
    hosts: List[str] = []
    pos = 0
    while True:
        if rest.startswith("[", pos):
            end = _scan_to(rest, pos, "]")
            host = rest[pos + 1 : end]
            pos = end + 1
        else:
            end = _scan_to(rest, pos, ":/?,")
            host = rest[pos:end]
            pos = end
        if rest.startswith(":", pos):
            pos = _scan_to(rest, pos + 1, "/?,")
        hosts.extend(unquote(host).split(","))
        if not rest.startswith(",", pos):
            break
        pos += 1
    for pair in rest[pos:].partition("?")[2].split("&"):
        key, _, value = pair.partition("=")
        if unquote(key) in ("host", "hostaddr"):
            hosts.extend(unquote(value).split(","))
    return hosts


# Removed from a URL before it is split (CPython's
# ``urllib.parse._UNSAFE_URL_BYTES_TO_REMOVE``, bpo-43882).
_URLLIB_REMOVES = str.maketrans("", "", "\t\r\n")


def _urllib_host_part(uri: str) -> str:
    """``uri`` from its host part on, where urllib, and so the attach, finds it.

    Mirrors ``urllib.parse.urlsplit`` and ``_NetlocResultMixinStr._hostinfo``
    (CPython ``Lib/urllib/parse.py``): the netloc runs from ``//`` to the first
    ``/``, ``?`` or ``#``, and the user information inside it to its *last*
    ``@``. libpq ends the user information at the first ``@`` instead, so an
    ``@`` typed in a password moves libpq's host but not urllib's.
    """
    rest = uri.translate(_URLLIB_REMOVES).partition("://")[2]
    return rest[rest.rfind("@", 0, _scan_to(rest, 0, "/?#")) + 1 :]


def _more_than_one_at_before_the_host(uri: str) -> bool:
    """Whether readers can disagree on where ``uri``'s password ends."""
    rest = uri.partition("://")[2]
    return rest.count("@", 0, _scan_to(rest, 0, "/")) > 1


def _libpq_local_socket(host: str) -> bool:
    """Whether libpq reaches ``host`` through one of this host's unix sockets.

    libpq's ``host`` rules: an empty host is the default socket directory, a
    host that looks like an absolute path is a socket directory (on Windows a
    drive-letter path too), and one that starts with ``@`` is an
    abstract-namespace socket. A blank host counts as empty here, which is
    stricter than libpq (it looks ``" "`` up as a host name).
    """
    host = host.strip()
    return not host or host.startswith(("/", "\\", "@")) or bool(_WINDOWS_DRIVE_PATH.match(host))


def _gate_jdbc_source(
    source: str,
    uri: Optional[str],
    *,
    allow_inline_credentials: bool,
    allow_metadata_service: bool,
) -> None:
    """Classify a ``forge_from_source`` JDBC request like a catalog credential block.

    A JDBC URI (``postgres`` / ``postgresql`` / ``mysql`` / ``sqlite``) reaches
    a database over the LLM-facing wire, so it is gated exactly as an inline
    catalog credential is:

    * a **password in the URI** is the caller's own inline material — it needs
      ``--allow-inline-credentials`` (the same gate inline blocks need), and the
      connection is then pinned to that password (``_jdbc_introspect``);
    * **no password** means the connection would fall back to THIS host's own
      database credentials (``PGPASSWORD`` / ``~/.pgpass`` / a libpq service /
      ``MYSQL_PWD``) and send them to the host the client chose — the
      host-identity case, so it needs ``--allow-metadata-service``;
    * a **sqlite** URI names a file on THIS host's disk, which an MCP client
      must not be able to choose, so it needs ``--allow-metadata-service`` too;
    * a URI with **no user name** authenticates as this host's login name
      (``PGUSER`` / the OS user), a mysql URI naming **localhost** goes
      through this host's unix socket, where socket authentication identifies
      the server's OS user, and so does a **postgres host that is empty, blank
      or a socket path** — an empty URI host, an empty element of a host list
      (``db,`` / ``,db``, also with the comma percent-encoded: ``db%2C``), a
      percent-encoded path (``%2Fvar%2Frun%2Fpostgresql``),
      an ``@`` abstract socket, or such a value in a ``host=`` / ``hostaddr=``
      query parameter: all are this host's identity, so they need
      ``--allow-metadata-service`` as well as the inline opt-in for a password.

    A postgres host is the one the attach connects to. The attach reads the URI
    with urllib, whose user information runs to the last ``@`` before the
    host, and builds key=value pairs from its fields, so the raw URI never
    reaches libpq: ``user:Summer2024@@db`` connects over TCP to ``db``. The host
    list urllib gives it is checked, and so is that same host part with its
    query, read with libpq's stricter rules (percent-decoding, ``host=``; see
    :func:`_libpq_hosts`). A URI urllib cannot read never connects; it is
    classified on libpq's own reading of the whole URI
    (:func:`_libpq_uri_parts`), and a refusal there says so when more than one
    ``@`` makes the end of the password ambiguous. A mysql URI urllib cannot
    give a host and port has no such second reading; it is refused whatever
    the flags, since none would make it connect.

    The URI must be a string whose scheme matches the source
    (``postgres://`` / ``postgresql://``, ``mysql://``, ``sqlite://``).

    Refused otherwise with a typed :class:`McpToolRefusal` naming the exact
    flag, so the refusal reaches the client intact. ``allow_metadata_service``
    here is the operator's opt-in; the client's argument of that name is not
    read on the JDBC path. Nor is ``credentials.credential_id``, a catalog-source
    field: the JDBC path connects with ``uri`` alone, so no refusal here offers
    a saved source. The operator's own CLI (``fluid forge data-model
    from-source`` / ``fluid init --discover``) never passes through this gate.
    """
    from fluid_build.cli.discover._jdbc_introspect import (
        _MISSING_HOSTNAME,
        _normalize_kind,
        _parse_uri,
        _uri_problem,
    )

    if not uri:
        # The dispatcher raises its own actionable "requires 'uri'" error.
        return
    if not isinstance(uri, str):
        raise McpToolRefusal("forge_from_source refused a JDBC source: 'uri' must be a string.")
    kind = _normalize_kind(source)  # raises ValueError on an unsupported kind
    schemes = _JDBC_URI_SCHEMES[kind]
    scheme, sep, _rest = uri.partition("://")
    if not sep or scheme.lower() not in schemes:
        raise McpToolRefusal(
            f"forge_from_source refused a {source} source: the URI scheme must be "
            f"{' or '.join(s + '://' for s in schemes)}."
        )

    if kind == "sqlite":
        if not _operator_allows_metadata_service(allow_metadata_service):
            raise McpToolRefusal(
                "forge_from_source refused a sqlite source over MCP: the URI names a "
                "file on this server's disk, and an MCP client must not choose a "
                "server-side path. Run it from the CLI (`fluid forge data-model "
                "from-source`), or restart the server with --allow-metadata-service (or "
                "FLUID_ALLOW_METADATA_SERVICE=1 in the server's environment) to allow a "
                "client-named server path."
            )
        return

    local_socket = False
    ambiguous_at = False
    if kind == "postgres":
        parsed: Optional[Dict[str, Any]]
        try:
            parsed = _parse_uri(uri, kind)
        except ValueError:
            parsed = None
        if parsed is None:
            # The attach reads the URI with this same parser, so it would stop
            # before connecting. The client still learns which flag libpq's
            # own reading of the URI needs, instead of an unclassified error.
            libpq_user, libpq_password, hosts = _libpq_uri_parts(uri)
            parsed = {"user": libpq_user, "password": libpq_password}
            ambiguous_at = _more_than_one_at_before_the_host(uri)
        else:
            # The attach never hands the URI to libpq: it builds key=value
            # pairs from urllib's fields. So the hosts are the list it connects
            # to, plus that same host part (and its query) read with libpq's
            # rules, which is stricter: it percent-decodes and reads host=.
            hosts = str(parsed.get("host") or "").split(",")
            hosts += _libpq_hosts(_urllib_host_part(uri))
        local_socket = any(_libpq_local_socket(host) for host in hosts)
    else:
        problem = _uri_problem(uri)
        if problem is not None:
            # No second reading would name a host here, and the attach would
            # refuse the URI before connecting, so no flag can make it usable,
            # and the refusal does not ask for one. _uri_problem never quotes the
            # URI, and returns rather than raises, so this refusal carries
            # neither the password nor urllib's error.
            hint = ""
            if problem == _MISSING_HOSTNAME:
                # A mysql client reads an empty host as localhost: the same
                # host-identity class as a URI naming it.
                hint = (
                    " A mysql client reads an empty host as localhost, this server's "
                    "unix socket, which needs --allow-metadata-service; name the "
                    "database server's host (127.0.0.1 for TCP on this machine)."
                )
            raise McpToolRefusal(
                f"forge_from_source refused a mysql source over MCP: the URI has no "
                f"usable host and port: {problem}. No flag would make it connect, and "
                f"nothing was contacted.{hint}"
            )
        parsed = _parse_uri(uri, kind)
    has_password = bool(parsed.get("password"))
    host_identity = None
    if not parsed.get("user"):
        host_identity = (
            "the URI has no user name, so the connection would log in as this host's "
            "own login name (PGUSER or the OS user)"
        )
    elif kind == "mysql" and str(parsed.get("host") or "").rstrip(".").lower() == "localhost":
        host_identity = (
            "a mysql 'localhost' is reached through this host's unix socket, where "
            "socket authentication identifies the server's OS user (use 127.0.0.1 "
            "for TCP)"
        )
    elif local_socket and ambiguous_at:
        host_identity = (
            "the URI has more than one '@' before its host, so where the password ends "
            "is ambiguous. libpq ends it at the first '@' and then reads a host that is "
            "empty, blank or a socket path: this host's unix socket, where peer or trust "
            "authentication identifies the server's OS user. urllib, which this server's "
            "attach uses, cannot read the URI, so the attach would refuse it before "
            "connecting"
        )
    elif local_socket:
        host_identity = (
            "a postgres host that is empty, blank or a socket path (in the host list, "
            "or in a host or hostaddr parameter) is this host's unix socket, where peer "
            "or trust authentication identifies the server's OS user whatever password "
            "the URI carries"
        )
    if host_identity is not None:
        if not _operator_allows_metadata_service(allow_metadata_service):
            raise McpToolRefusal(
                f"forge_from_source refused a JDBC source over MCP: {host_identity}. "
                "Run it from the CLI (`fluid forge data-model from-source`), or restart "
                "the server with --allow-metadata-service (or "
                "FLUID_ALLOW_METADATA_SERVICE=1 in the server's environment)."
            )
        if not has_password:
            return
    if has_password:
        if not allow_inline_credentials:
            raise McpToolRefusal(
                "forge_from_source refused an inline JDBC credential over MCP: the URI "
                "carries a password, a secret that would travel over the LLM-facing "
                "wire. Run it from the CLI (`fluid forge data-model from-source`), or "
                "restart the server with --allow-inline-credentials if the caller is a "
                "trusted in-process CLI harness."
            )
        return

    # No password: the connection would authenticate with this host's own
    # credential material and reach the host the client named.
    if not _operator_allows_metadata_service(allow_metadata_service):
        raise McpToolRefusal(
            "forge_from_source refused a JDBC source with no password over MCP: with no "
            "password in the URI, the connection falls back to this host's own database "
            "credentials (PGPASSWORD, ~/.pgpass, a libpq service or MYSQL_PWD) and would "
            "send them to the host the client named. Put the caller's own password in "
            "the URI (with --allow-inline-credentials), run it from the CLI (`fluid "
            "forge data-model from-source`), or restart the server with "
            "--allow-metadata-service (or FLUID_ALLOW_METADATA_SERVICE=1 in the server's "
            "environment)."
        )


def _gate_inline_host_identity(resolver: Any) -> None:
    """Refuse inline credentials that authenticate as this host, on ``resolver``.

    ``--allow-inline-credentials`` lets a caller send a credential block, but a
    block can reach what this host holds as surely as ``allow_metadata_service``
    does: through its ``auth_method`` (or its default: Glue ``iam_role``,
    BigQuery / Dataplex ``adc``), through blank keys (an SDK reads an empty key
    as "none supplied" and falls back to its default chain), through a path
    (the SDK reads that file, gcloud ADC or a mounted key, from this host's
    disk), or through this host's configuration (a netrc login for the endpoint
    it names, DataHub settings). That stays the operator's decision, so the
    check runs on the typed credential the resolver produced, where the model's
    defaults have been applied and blank values read as absent, before any
    adapter or SDK sees it. The classification is
    :func:`~fluid_build.copilot.catalog.credentials.host_identity_reason`,
    which fails closed: only a block whose own material is a value its SDK uses
    as given passes. The client's own ``allow_metadata_service`` argument never
    stands in for the operator, and a saved source (``credential_id``) is the
    operator's configuration, so it is not checked.

    A block that passes is handed to the adapter pinned to its own material
    (:func:`~fluid_build.copilot.catalog.credentials.pin_to_own_material`): an
    SDK that would let this host's environment or config files choose the
    credential (``DATABRICKS_AUTH_TYPE`` over a Unity token) or probe this
    host's cloud identity (the Snowflake login's AWS STS call) is told not to.

    Wraps the instance's ``resolve`` rather than subclassing, so the adapter's
    ``from_resolver`` contract is unchanged and this module still imports the
    catalog package lazily.
    """
    from fluid_build.copilot.catalog.credentials import (
        host_identity_reason,
        inline_credential_forms,
        pin_to_own_material,
    )

    resolve = resolver.resolve

    def _resolve(**kwargs: Any) -> Any:
        credentials = resolve(**kwargs)
        if not kwargs.get("inline_credentials"):
            return credentials
        reason = host_identity_reason(credentials)
        if reason is not None:
            catalog = kwargs.get("catalog_name") or "catalog"
            instead = inline_credential_forms(credentials)
            raise McpToolRefusal(
                f"Source-catalog tools refused inline {catalog} credentials: the block "
                f"{reason}, so it would authenticate as this host rather than as the "
                "caller. With --allow-inline-credentials alone, a block must carry the "
                "caller's own credential as values, never a path to a file on this "
                "server, and nothing this host holds may stand in for it."
                + (f" {instead}" if instead else "")
                + " Otherwise pass credentials.credential_id (a source the operator "
                "saved with fluid ai setup), or restart the server with "
                "--allow-metadata-service (or FLUID_ALLOW_METADATA_SERVICE=1 in the "
                "server's environment)."
            )
        return pin_to_own_material(credentials)

    resolver.resolve = _resolve


def _build_source_adapter_impl(
    arguments: Dict[str, Any],
    *,
    allow_inline_credentials: bool = False,
    allow_metadata_service: bool = False,
) -> Any:
    """Resolve the right adapter from MCP tool arguments.

    Every catalog tool's ``arguments`` MUST include:

    * ``source``: which catalog (``"snowflake"`` / ``"unity"`` /
      ``"bigquery"`` / ``"dataplex"`` / ``"glue"`` / ``"datahub"``
      / ``"datamesh_manager"``).
    * ``credentials.credential_id``: how to authenticate. The MCP
      server NEVER accepts a credential value via the LLM-facing
      wire — only a ``credential_id`` pointing at a saved entry in
      the keyring + ``~/.fluid/sources.yaml``.

    A trusted in-process CLI harness can opt into accepting
    ``credentials.inline = {...}`` by starting the server with
    ``fluid mcp serve --allow-inline-credentials``. The default
    rejects ``inline`` so a malicious LLM client cannot push raw
    secrets through the tool surface.

    Likewise ``allow_metadata_service: true`` (the cloud metadata-service
    / ADC fallback) is refused unless the server was started with
    ``--allow-metadata-service`` or ``FLUID_ALLOW_METADATA_SERVICE=1`` —
    see :func:`_metadata_service_opt_in`. The same opt-in governs inline
    credentials whose ``auth_method`` selects the host's identity — see
    :func:`_gate_inline_host_identity`.

    The resolver merges keyring + ``~/.fluid/sources.yaml`` into a
    typed Credentials object the adapter consumes. Each adapter's
    ``from_resolver`` classmethod is the canonical entry point.
    """
    from fluid_build.copilot.catalog.credentials import CredentialResolver

    source = str(arguments.get("source") or "").lower().strip()
    if not source:
        supported = ", ".join(sorted(_SOURCE_ADAPTERS))
        raise RuntimeError(f"Source-catalog tools require 'source' (one of: {supported}).")
    credentials_arg = arguments.get("credentials") or {}
    credential_id = credentials_arg.get("credential_id")
    inline = credentials_arg.get("inline")
    # SECURITY: refuse raw inline secrets unless the operator
    # explicitly enabled them at server startup.  The MCP wire is
    # normally LLM-facing; the documented contract is that secrets
    # are looked up via ``credential_id`` against the local
    # keyring + sources.yaml, never sent over the wire.
    if inline and not allow_inline_credentials:
        raise RuntimeError(
            "Source-catalog tools refused inline credentials over MCP. "
            "Pass credentials.credential_id (a name configured via "
            "`fluid ai setup --source <catalog> --name <name>`) instead, or restart the "
            "server with --allow-inline-credentials if the caller is "
            "a trusted in-process CLI harness."
        )
    use_metadata_service = _metadata_service_opt_in(
        arguments, allow_metadata_service=allow_metadata_service
    )
    if not credential_id and not inline:
        raise RuntimeError(
            "Source-catalog tools require credentials.credential_id "
            "(or credentials.inline for direct CLI callers when the "
            "server is started with --allow-inline-credentials)."
        )
    if source not in _SOURCE_ADAPTERS:
        supported = ", ".join(sorted(_SOURCE_ADAPTERS))
        raise RuntimeError(f"Unknown source-catalog adapter: {source!r}. Supported: {supported}.")
    resolver = CredentialResolver(allow_metadata_service=use_metadata_service)
    if inline and not _operator_allows_metadata_service(allow_metadata_service):
        _gate_inline_host_identity(resolver)
    adapter_cls = _import_adapter_class(_SOURCE_ADAPTERS[source])
    return adapter_cls.from_resolver(
        resolver, credential_id=credential_id, inline_credentials=inline
    )


def _scope_from_args(arguments: Dict[str, Any]) -> Any:
    """Build a ``CatalogScope`` from MCP tool arguments.

    Accepts the JSON shape::

        {"scope": {"database": "DEMO_DB", "schema": "SEEDED", "tables": [...]}}

    or a flat shape with the scope fields at the top level. Both
    are tolerated to keep the LLM-facing schema forgiving.
    """
    from fluid_build.copilot.catalog.models import CatalogScope

    raw = arguments.get("scope")
    if isinstance(raw, dict):
        return CatalogScope.model_validate(raw)
    # Flat fallback: pull individual keys.
    flat = {
        k: arguments[k]
        for k in ("database", "schema", "schema_name", "catalog", "tables")
        if k in arguments
    }
    return CatalogScope.model_validate(flat)


def _run_forge_inproc(
    mode: str,
    target_dir: str,
    data_product_type: Optional[str],
    from_products: Optional[List[str]],
) -> Dict[str, Any]:
    """Run ``fluid forge`` in-process (sync). Called from ``forge_run`` via
    ``asyncio.to_thread``. ``MCPSamplingProvider`` (if used) routes back to
    the IDE's LLM via the sampling-context bridge.

    Critical: ``fluid forge --agent`` writes JSON-Lines progress events to
    stdout, and stdout IS the MCP wire — the MCP client's JSON-RPC parser
    rejects anything that isn't a well-formed ``JSONRPCMessage``. We capture
    forge's stdout for the duration of the run and surface the parsed events
    in the tool result (so Claude Code / Cursor / Kiro see structured forge
    progress alongside the standard ``exit_code`` + ``contract_path`` fields)
    without polluting the MCP wire.
    """
    import contextlib
    import io

    import fluid_build.cli.mcp as _mcp_pkg
    from fluid_build.cli import forge as forge_mod

    # Defense-in-depth: re-validate ``target_dir`` against the active
    # ``--writable-paths`` policy. The async tool wrapper at ``forge_run``
    # already gates via ``check_tool_permission`` with the full argument
    # dict, but a future regression in that wrapper (e.g. forgetting to
    # plumb a new argument) must NOT silently let an attacker-controlled
    # path through to ``mkdir(parents=True)`` + ``write_text``. This
    # second check is the belt-and-braces fail-closed gate.
    #
    # Resolve ``_policy`` through the PACKAGE module object (``_mcp_pkg``,
    # imported above), NOT a function-local ``from ...policy import _policy``.
    # The sandbox tests patch the package-level symbol
    # (``monkeypatch.setattr(mcp_mod, "_policy", ...)``); a local re-import
    # would re-bind to the original and bypass the patch, silently defeating
    # the writable-paths confinement check this gate exists to enforce. Pinned
    # by tests/cli/test_mcp_forge_run_permission.py.
    policy = _mcp_pkg._policy()
    resolved = Path(str(target_dir)).expanduser().resolve()
    if not _path_is_writable(resolved, policy.writable_paths):
        roots_str = ", ".join(str(p) for p in policy.writable_paths)
        raise PermissionError(
            f"forge_run: target_dir {resolved} is not within any "
            f"--writable-paths root ({roots_str})"
        )

    parser = _argparse.ArgumentParser()
    sp = parser.add_subparsers()
    forge_mod.register(sp)

    argv = ["forge", "--agent", "-d", str(target_dir)]
    if data_product_type:
        argv += ["--data-product-type", str(data_product_type)]
    if mode == "blank":
        argv += ["--blank"]
    elif mode == "ai":
        for fp in from_products or []:
            argv += ["--from-product", str(fp)]
        argv += ["--llm-provider", "mcp-sampling"]

    args = parser.parse_args(argv)
    forge_logger = logging.getLogger("fluid.mcp.forge_run")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        rc = forge_mod.run(args, forge_logger)
    raw_stdout = captured.getvalue()

    # Forge's --agent mode emits one JSON object per line. Parse them out;
    # everything else (Rich console banners, etc.) is best-effort discarded
    # since stdout was redirected to suppress it from the wire.
    events: List[Dict[str, Any]] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("event"), str):
            events.append(obj)

    contract_path = Path(target_dir) / "contract.fluid.yaml"
    return {
        "mode": mode,
        "exit_code": rc,
        "target_dir": str(target_dir),
        "contract_path": str(contract_path),
        "contract_exists": contract_path.is_file(),
        "events": events,
    }
