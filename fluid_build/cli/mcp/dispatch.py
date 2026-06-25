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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def _build_source_adapter(arguments, *, allow_inline_credentials: bool = False):
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
            arguments, allow_inline_credentials=allow_inline_credentials
        )
    return builder(arguments, allow_inline_credentials=allow_inline_credentials)


def _write_audit_event(*args, **kwargs):
    """Package-level indirection wrapper for ``write_audit_event``.

    Resolves the callable through the package so test monkeypatches on
    ``fluid_build.cli.mcp.write_audit_event`` take effect at the dispatch
    call sites.
    """
    fn = getattr(_pkg(), "write_audit_event", write_audit_event)
    return fn(*args, **kwargs)


def _call_tool(
    name: str,
    arguments: Dict[str, Any],
    *,
    read_only: bool,
    allow_inline_credentials: bool = False,
) -> Dict[str, Any]:
    """Dispatch a named tool to its implementation (sync, runs in worker thread).

    Permission gating is done by the async wrapper before calling this.
    ``read_only`` is passed explicitly rather than read from the policy
    to keep this function synchronous and thread-safe.
    """
    from fluid_build.cli.forge_data_model import diff_logical_models
    from fluid_build.copilot.schemas.stage_outputs import ConceptualRelationship, LogicalDraft

    if name == "read_logical_model":
        path = Path(arguments["path"])
        return LogicalDraft.model_validate_json(path.read_text(encoding="utf-8")).model_dump(
            mode="json", by_alias=True
        )
    if name == "update_entity":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
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
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
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
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
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
            logical = LogicalDraft.model_validate_json(
                Path(arguments["logical_path"]).read_text(encoding="utf-8")
            )
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
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
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
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
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
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
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
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
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
            return _dispatch_forge_from_jdbc_source(arguments)

        # ``resolve_store`` is imported at module level — do NOT
        # re-import inside this branch, otherwise Python treats it
        # as a function-local for the entire ``_call_tool`` body
        # and the search_semantic_memory branch above this one
        # crashes with UnboundLocalError.
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.agents.logical_agent import LogicalAgent

        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
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
            "Example: postgresql://user:pass@host:5432/db"
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


def _build_source_adapter_impl(
    arguments: Dict[str, Any],
    *,
    allow_inline_credentials: bool = False,
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
    if not credential_id and not inline:
        raise RuntimeError(
            "Source-catalog tools require credentials.credential_id "
            "(or credentials.inline for direct CLI callers when the "
            "server is started with --allow-inline-credentials)."
        )
    if source not in _SOURCE_ADAPTERS:
        supported = ", ".join(sorted(_SOURCE_ADAPTERS))
        raise RuntimeError(f"Unknown source-catalog adapter: {source!r}. Supported: {supported}.")
    resolver = CredentialResolver(
        allow_metadata_service=bool(arguments.get("allow_metadata_service", False))
    )
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
