# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
MCP sync dispatch layer — ``_call_tool`` and all helper functions.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from fluid_build.cli.mcp.models import _CATALOG_SOURCE_LIST
from fluid_build.copilot.store.audit_trail import write_audit_event
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
            contract = _contract_file(arguments["contract_path"])
        return validator.validate(logical=logical, contract=contract).model_dump(
            mode="json", by_alias=True
        )
    if name == "diff_models":
        return diff_logical_models(Path(arguments["old"]), Path(arguments["new"]))
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

    raise RuntimeError(f"Unknown tool {name}")


def _contract_file(path_arg: str) -> Dict[str, Any]:
    """The contract at ``path_arg`` as ``fluid validate`` sees it: composed.

    Read raw, a contract kept in fragments holds ``{$ref: ...}`` where its
    builds and exposes are. The composition confines its fragments to the ref
    root, which a workspace ``ref_root`` or ``FLUID_REF_ROOT`` may widen past
    the server's ``--readable-paths``. Under an active policy that root is
    capped to the readable path holding the contract before any fragment is
    opened, so a ref outside it fails as an escape, quoting nothing of the
    file; every file read is then checked again. Without a policy (no server
    running) only the ref root applies.
    """
    from fluid_build import loader
    from fluid_build.cli.mcp.policy import _path_is_allowed

    path = Path(path_arg)
    try:
        policy = _pkg()._policy()
    except RuntimeError:  # not served: no policy to hold the reads to
        policy = None
    ref_root = None
    if policy is not None:
        ref_root = _readable_ref_root(path, policy.readable_paths)
    composed = loader.compose_contract(path, ref_root=ref_root)
    if policy is not None:
        for file in composed.files:
            if not _path_is_allowed(Path(file).resolve(), policy.readable_paths):
                raise PermissionError(
                    f"contract {path_arg} composes {file}, which is outside --readable-paths"
                )
    if not isinstance(composed.document, dict):
        raise RuntimeError(f"Contract at {path_arg!r} did not parse to a dict")
    return composed.document


def _readable_ref_root(path: Path, readable: Tuple[Path, ...]) -> Optional[Path]:
    """The ref root to compose ``path`` under so it reads nothing outside ``readable``:
    ``None`` (the root it would get) when that root is readable, else the readable path
    that holds the contract, which narrows it."""
    from fluid_build import loader
    from fluid_build.cli.mcp.policy import _path_is_allowed

    # The root it would get if it named another file (reads_outside): one that
    # names none reads nothing past its own directory anyway.
    root = loader._effective_ref_root(path, {}, None, reads_outside=True).ref_root
    if _path_is_allowed(Path(root).resolve(), readable):
        return None
    contract = path.resolve()
    holders = [r.resolve() for r in readable if r.is_dir() and _path_is_allowed(contract, (r,))]
    # The deepest readable directory holding the contract: the narrowest cap. A
    # readable path that names files leaves the contract's own directory, and the
    # check of every file read after the composition.
    return max(holders, key=lambda r: len(r.parts)) if holders else contract.parent


# ---------------------------------------------------------------------------
# V1.5 source-catalog dispatch helpers.
# ---------------------------------------------------------------------------


def _list_source_adapters() -> List[Dict[str, Any]]:
    """Enumerate the source-catalog adapters the catalog tools can reach.

    The list is static: it reflects what code is shipped, not what the operator has
    configured (``fluid ai status`` lists configured credentials). Each adapter lives in
    ``fluid_build.copilot.catalog.<name>`` and follows the patterns in
    ``catalog._patterns``. JDBC databases are not listed: no MCP tool reads them since
    ``forge_from_source`` was dropped (2026-11-13); ``fluid forge data-model from-source
    --uri`` still does.
    """
    return [
        {"name": name, "status": "available", "kind": "catalog"} for name in _CATALOG_SOURCE_LIST
    ]


# Single source of truth for catalog dispatch. Every adapter
# implements ``CatalogAdapter.from_resolver`` so we just need the
# class reference here. New adapters land by adding one entry.
#
# JDBC sources (postgres / postgresql / mysql / sqlite) are NOT in this
# table. ``_build_source_adapter`` only handles catalog sources; the
# catalog-only tools (list_source_tables, inspect_source_table,
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
