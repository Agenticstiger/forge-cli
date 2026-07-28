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

"""McpCatalogConnector — discover data products via a catalog's MCP server.

Rather than maintain a bespoke REST/GraphQL client per catalog, this connector
speaks the **Model Context Protocol (MCP)** to *any* catalog that exposes an
MCP server. As of 2026 the major catalogs all ship one:

* **DataHub** — ``acryldata/mcp-server-datahub`` (search / lineage / schema).
* **OpenMetadata** — built-in MCP server since v1.12 with a ``search_metadata``
  tool.
* **Data Mesh Manager** — ``entropy-data/dataproduct-mcp``, purpose-built for
  discovering data products.

This is the read/discover mirror of fluid's MCP *output* port (which serves a
data product's data via MCP): **serve-via-MCP ↔ discover-via-MCP**. The ``mcp``
SDK supplies one unified client (stdio / SSE / streamable-HTTP transports);
per-catalog *profiles* contribute only the thin parts that differ — which tool
performs the search and how to read its rows into :class:`DataProductMetadata`.

Configuration lives in the ``mcp:`` block of the market config (add ``mcp`` to
the ``catalogs`` list to enable it). Catalogs are normally REMOTE, so connect
to a hosted MCP endpoint and read the bearer token from an env var::

    mcp:
      profile: openmetadata     # datahub | openmetadata | datamesh_manager | auto
      transport: streamable_http
      url: https://openmetadata.your-company.com/mcp
      token_env: OPENMETADATA_JWT_TOKEN   # bearer token (PAT/JWT) read from this env var

Or run an official MCP server as a local subprocess (stdio) that itself talks
to your remote catalog — e.g. DataHub, where the server process is local but
``DATAHUB_GMS_URL`` points at your hosted instance::

    mcp:
      profile: datahub
      transport: stdio
      command: uvx
      args: ["mcp-server-datahub"]
      env: {DATAHUB_GMS_URL: "https://your-company.acryl.io/gms"}
      # DATAHUB_GMS_TOKEN is read from the shell environment (inherited).

**Authentication is always environment-sourced — secrets never live in the
config file or the contract.** Remote endpoints take the token from
``token_env`` (default ``Authorization: Bearer``; override via ``auth_header`` /
``auth_scheme`` for shapes like ``x-api-key``). stdio servers inherit the
secret from the shell environment; ``env_from: {SERVER_VAR: SOURCE_ENV_VAR}``
copies a secret from an env var of your choosing into the name the server
expects, so the secret *value* still never lands in a file.

Each operation opens a fresh, self-contained MCP session (connect validates
reachability + that a search tool exists; search runs one call) — a one-shot
``fluid market`` pays a single handshake, simpler and less error-prone than
threading a long-lived session across calls.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fluid_build._mcp_compat import attr as _mcp_attr
from fluid_build.cli.market import (
    BaseCatalogConnector,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    SearchFilters,
)

# Tool-name fragments that, absent an explicit profile match, mark a tool as
# search-capable. Ordered by preference.
_SEARCH_NAME_HINTS: Tuple[str, ...] = ("search", "discover", "find", "query", "list")


@dataclass(frozen=True)
class CatalogProfile:
    """The per-catalog bits that differ between MCP servers.

    Everything else (transport, handshake, tool invocation, result framing) is
    handled uniformly by the MCP SDK, so a profile is intentionally tiny: the
    candidate search-tool names, the argument names for query/limit, and the
    candidate result-row keys for each :class:`DataProductMetadata` field. The
    mapping is *best-effort and defensive* — it degrades gracefully on unknown
    shapes; the Stage-3 live tests refine it against real servers.
    """

    name: str
    search_tools: Tuple[str, ...] = ()
    query_arg: str = "query"
    limit_arg: Optional[str] = "limit"
    extra_args: Dict[str, Any] = field(default_factory=dict)
    # Candidate keys per field — first present, non-empty value wins.
    id_keys: Tuple[str, ...] = ("id", "urn", "fullyQualifiedName", "fqn", "qualifiedName")
    name_keys: Tuple[str, ...] = (
        "name",
        "displayName",
        "title",
        "label",
        "properties.name",
        "info.title",
    )
    desc_keys: Tuple[str, ...] = (
        "description",
        "summary",
        "doc",
        "documentation",
        "properties.description",
        "editableProperties.description",
    )
    domain_keys: Tuple[str, ...] = (
        "domain",
        "domainName",
        "dataDomain",
        "domain.domain.properties.name",
    )
    owner_keys: Tuple[str, ...] = ("owner", "owners", "team", "ownership")
    tags_keys: Tuple[str, ...] = ("tags", "labels", "glossaryTerms", "terms")
    quality_keys: Tuple[str, ...] = ("quality_score", "qualityScore", "trustScore", "trust")
    version_keys: Tuple[str, ...] = ("version", "majorVersion")
    url_keys: Tuple[str, ...] = ("schema_url", "url", "link", "href", "entityUrl")
    layer_keys: Tuple[str, ...] = ("layer", "tier", "stage")
    status_keys: Tuple[str, ...] = ("status", "lifecycle", "lifecycleStatus", "state")
    created_keys: Tuple[str, ...] = ("created_at", "createdAt", "created", "creationDate")
    updated_keys: Tuple[str, ...] = ("updated_at", "updatedAt", "updated", "lastModified")
    # --- Enrichment: per-product detail lookup (the world-class "all metadata"
    #     path). Listing stays shallow/fast; the detail path fetches the rich
    #     entity + its column schema. Left unset → no enrichment (graceful). ---
    detail_tool: Optional[str] = None  # returns a rich entity for one id
    detail_id_arg: str = "urns"
    schema_tool: Optional[str] = None  # returns the asset's column/field schema
    schema_id_arg: str = "urn"
    schema_field_name_keys: Tuple[str, ...] = ("fieldPath", "name", "path", "column")
    schema_field_type_keys: Tuple[str, ...] = (
        "nativeDataType",
        "type",
        "dataType",
        "dataTypeDisplay",
    )
    schema_field_desc_keys: Tuple[str, ...] = ("description", "doc")


# Tuned profiles for the three catalogs fluid already integrates, plus a
# generic ``auto`` that works against any MCP-enabled catalog by heuristic.
PROFILES: Dict[str, CatalogProfile] = {
    "datahub": CatalogProfile(
        name="datahub",
        search_tools=("search", "search_entities", "get_entities", "get_dataset"),
        # Verified live against mcp-server-datahub: the search tool's result-limit
        # arg is ``num_results`` (not ``limit``); get_entities(urns=…) returns the
        # rich entity; list_schema_fields(urn=…) returns column schema.
        limit_arg="num_results",
        detail_tool="get_entities",
        detail_id_arg="urns",
        schema_tool="list_schema_fields",
        schema_id_arg="urn",
    ),
    "openmetadata": CatalogProfile(
        name="openmetadata",
        # Verified live against OpenMetadata 1.12's built-in MCP server: the
        # search tool is ``search_metadata`` (results under "results", each with
        # fullyQualifiedName / name / description / owners), and its result-limit
        # arg is ``size`` (not ``limit``). Detail enrichment via
        # ``get_entity_details(entityType, fqn)`` is a 2-arg shape left for a
        # follow-up; OM's search result is already rich (description + owners).
        search_tools=("search_metadata", "search"),
        limit_arg="size",
    ),
    "datamesh_manager": CatalogProfile(
        name="datamesh_manager",
        search_tools=("search_data_products", "list_data_products", "get_data_products", "search"),
    ),
    "auto": CatalogProfile(name="auto"),
}


def _get_path(row: Dict[str, Any], key: str) -> Any:
    """Resolve a flat or dotted key (``"properties.name"``) into nested dicts."""
    if "." not in key:
        return row.get(key) if isinstance(row, dict) else None
    cur: Any = row
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _first(row: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """Return the first present, non-empty value among ``keys`` (dotted keys ok)."""
    for k in keys:
        val = _get_path(row, k)
        if val not in (None, "", [], {}):
            return val
    return None


def _as_str(value: Any) -> str:
    """Coerce a scalar / list / dict owner-ish value to a display string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or value.get("displayName") or "")
    if isinstance(value, (list, tuple)):
        parts = [_as_str(v) for v in value]
        return ", ".join(p for p in parts if p)
    return str(value)


def _as_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for v in value:
            s = _as_str(v)
            if s:
                out.append(s)
        return out
    return [_as_str(value)]


def _extract_owner(value: Any) -> str:
    """Owner display string, handling the deep ``ownership.owners[].owner``
    envelope catalogs use (DataHub: ``{"owners":[{"owner":{...}}]}``) as well as
    plain strings / lists. Falls back to :func:`_as_str` for simple shapes.
    """
    if isinstance(value, dict) and isinstance(value.get("owners"), list):
        names: List[str] = []
        for entry in value["owners"]:
            owner = entry.get("owner", entry) if isinstance(entry, dict) else entry
            if isinstance(owner, dict):
                name = (
                    _get_path(owner, "properties.displayName")
                    or owner.get("name")
                    or _get_path(owner, "properties.email")
                    or owner.get("urn")
                )
            else:
                name = owner
            name = _as_str(name)
            if name and name not in names:
                names.append(name)
        return ", ".join(names)
    return _as_str(value)


def _parse_dt(value: Any) -> datetime:
    """Best-effort ISO/epoch → aware datetime; falls back to epoch start.

    A discovery result with no timestamp is real metadata we simply don't know
    the date of — we use a stable sentinel (epoch, UTC) rather than fabricating
    "now", so repeated discoveries are deterministic.
    """
    if isinstance(value, (int, float)):
        try:
            # Heuristic: ms vs s epoch.
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value:
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _is_empty(value: Any) -> bool:
    """True for the "unknown"/sentinel values ``_row_to_metadata`` emits for an
    absent field — None, empty string/list/dict, or the epoch timestamp."""
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, datetime) and value == _EPOCH:
        return True
    return False


def _merge_product_metadata(
    base: Optional[DataProductMetadata], overlay: Optional[DataProductMetadata]
) -> Optional[DataProductMetadata]:
    """Make ``--detailed`` a SUPERSET of the listing, never a subset.

    ``base`` is the shallow search row (authoritative for the trust/quality/layer/
    tags/usage/version fields the listing surfaces); ``overlay`` is the rich
    per-product detail entity, which is frequently *sparser* on those fields (it
    carries description + ownership but not quality/tags/layer). So we keep
    ``base`` and apply ``overlay`` only where it genuinely adds:

    * ``description`` / ``owner`` — the detail entity's reason to exist — win when
      present (the listing's summary is the fallback);
    * every other field is **gap-filled** only where ``base`` left it empty, so a
      sparse detail entity can never clobber a value the listing already had.
    """
    if base is None:
        return overlay
    if overlay is None:
        return base

    updates: Dict[str, Any] = {}
    # The detail entity's raison d'être: a richer description + resolved owner.
    for name in ("description", "owner"):
        oval = getattr(overlay, name)
        if not _is_empty(oval) and oval != getattr(base, name):
            updates[name] = oval
    # Everything else: fill only the gaps the shallow row left — never overwrite.
    for f in fields(base):
        if f.name in updates:
            continue
        if _is_empty(getattr(base, f.name)) and not _is_empty(getattr(overlay, f.name)):
            updates[f.name] = getattr(overlay, f.name)
    return replace(base, **updates) if updates else base


def _parse_layer(value: Any) -> DataProductLayer:
    s = _as_str(value).strip().lower()
    for layer in DataProductLayer:
        if s == layer.value:
            return layer
    # Common synonyms seen across catalogs.
    synonyms = {
        "tier1": DataProductLayer.GOLD,
        "tier_1": DataProductLayer.GOLD,
        "curated": DataProductLayer.GOLD,
        "certified": DataProductLayer.GOLD,
        "staging": DataProductLayer.SILVER,
        "refined": DataProductLayer.SILVER,
        "ingest": DataProductLayer.BRONZE,
        "landing": DataProductLayer.RAW,
        "streaming": DataProductLayer.REAL_TIME,
    }
    if s in synonyms:
        return synonyms[s]
    # Neutral default — discovery can't always infer a medallion layer.
    return DataProductLayer.SILVER


def _parse_status(value: Any) -> DataProductStatus:
    s = _as_str(value).strip().lower()
    for status in DataProductStatus:
        if s == status.value:
            return status
    if s in ("published", "approved", "live", "certified"):
        return DataProductStatus.ACTIVE
    if s in ("draft", "wip", "in_development"):
        return DataProductStatus.DEVELOPMENT
    # A product surfaced by a catalog search exists, so default to ACTIVE.
    return DataProductStatus.ACTIVE


def _normalize_quality(value: Any) -> Optional[float]:
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    if q > 1.0:  # percent or 0..100 scale → 0..1
        q = q / 100.0
    return max(0.0, min(1.0, q))


class McpCatalogConnector(BaseCatalogConnector):
    """Discover data products by speaking MCP to a catalog's MCP server."""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__(config, logger)
        profile_name = str(config.get("profile", "auto")).lower()
        self.profile = PROFILES.get(profile_name, PROFILES["auto"])
        # The search tool resolved during connect() and reused by search().
        self._search_tool: Optional[str] = config.get("search_tool")
        self._search_tool_schema: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Session management (overridable for in-memory testing)             #
    # ------------------------------------------------------------------ #
    def _open_session(self):
        """Async context manager yielding an *initialized* MCP ``ClientSession``.

        Tests override this attribute to inject an in-memory server session
        (``mcp.shared.memory.create_connected_server_and_client_session``),
        exercising the full client → tool → mapping path with zero network.
        """
        return self._open_real_session()

    def _headers(self) -> Dict[str, str]:
        """Build request headers for a *remote* MCP endpoint.

        The token is sourced from an environment variable named by ``token_env``
        so the secret is never stored in the market config or the contract. A
        literal ``token`` is accepted for convenience but discouraged. Defaults
        to an ``Authorization: Bearer`` scheme; ``auth_header`` / ``auth_scheme``
        override it for catalogs that use a different shape (e.g. ``x-api-key``).
        """
        headers = {str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}
        token_env = self.config.get("token_env")
        token = self.config.get("token") or (os.environ.get(token_env) if token_env else None)
        if token:
            header_name = self.config.get("auth_header", "Authorization")
            scheme = self.config.get("auth_scheme", "Bearer")
            headers.setdefault(header_name, f"{scheme} {token}".strip())
            self._warn_if_insecure_token_transport()
        return headers

    def _warn_if_insecure_token_transport(self) -> None:
        """Warn (don't fail) before sending an auth token over plaintext HTTP to
        a non-local host — a token in cleartext on the wire is a real leak."""
        url = self.config.get("url") or ""
        if url.startswith("http://"):
            host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
            if host not in ("localhost", "127.0.0.1", "::1", ""):
                self.logger.warning(
                    "MCP auth token will be sent over plaintext HTTP to "
                    f"'{host}'. Use https:// for any remote catalog."
                )

    @asynccontextmanager
    async def _open_real_session(self) -> AsyncIterator[Any]:
        # Borrow the SDK's ``ClientSessionGroup``: given a typed
        # ``*ServerParameters``, it picks the transport (stdio / SSE /
        # streamable-HTTP), runs the ``initialize`` handshake, and tears the
        # connection down on context exit. We don't re-implement any of that —
        # we only translate config into the right params object below.
        try:
            from mcp import ClientSessionGroup
        except ImportError as exc:  # pragma: no cover - mcp is a core dep
            raise RuntimeError(
                "MCP discovery requires the 'mcp' SDK (pip install 'fluid-build')."
            ) from exc

        params = self._build_server_params()
        async with ClientSessionGroup() as group:
            session = await group.connect_to_server(params)
            yield session

    def _build_server_params(self) -> Any:
        """Translate the ``mcp:`` config block into a typed MCP server-params
        object. The SDK's ``ClientSessionGroup`` selects the transport from the
        object's *type*, so this is the only transport-aware code we own."""
        from mcp import StdioServerParameters
        from mcp.client.session_group import SseServerParameters, StreamableHttpParameters

        transport = self.config.get("transport") or (
            "stdio" if self.config.get("command") else "streamable_http"
        )
        if transport == "stdio":
            command = self.config.get("command")
            if not command:
                raise ValueError("mcp stdio transport requires a 'command' (e.g. 'uvx').")
            return StdioServerParameters(
                command=command,
                args=list(self.config.get("args", [])),
                env=self._subprocess_env(),
            )
        url = self._require_url()
        headers = self._headers()
        if transport == "sse":
            return SseServerParameters(url=url, headers=headers)
        if transport in ("streamable_http", "http"):
            return StreamableHttpParameters(url=url, headers=headers)
        raise ValueError(f"Unsupported mcp transport: {transport!r}")

    def _require_url(self) -> str:
        url = self.config.get("url")
        if not url:
            raise ValueError("mcp remote transport requires a 'url'.")
        return url

    def _subprocess_env(self) -> Dict[str, str]:
        """Environment for a local stdio MCP server (e.g. ``mcp-server-datahub``).

        Inherits the caller's environment, so a secret exported in the shell —
        ``DATAHUB_GMS_TOKEN``, ``DATAMESH_MANAGER_API_KEY``, ``OPENMETADATA_JWT_TOKEN``,
        etc. — reaches the server WITHOUT being named in any config file (the
        same model Claude Desktop and the catalog docs use). On top of that:

        * ``env``      — literal NON-secret values (URLs, hosts) to set/override.
        * ``env_from`` — ``{SERVER_VAR: SOURCE_ENV_VAR}``: copy a secret from an
          env var of your choosing into the name the server expects, so the
          secret *value* still never lands in the config file.
        """
        env: Dict[str, str] = {k: str(v) for k, v in os.environ.items()}
        for k, v in (self.config.get("env") or {}).items():
            env[str(k)] = str(v)
        for server_var, source_var in (self.config.get("env_from") or {}).items():
            value = os.environ.get(str(source_var))
            if value is not None:
                env[str(server_var)] = value
        return env

    # ------------------------------------------------------------------ #
    # BaseCatalogConnector overrides                                     #
    # ------------------------------------------------------------------ #
    async def _connect_impl(self) -> bool:
        try:
            async with self._open_session() as session:
                tools = (await session.list_tools()).tools
                tool = self._resolve_search_tool(tools)
                if tool is None:
                    names = ", ".join(t.name for t in tools) or "(none)"
                    self.logger.error(
                        "MCP server exposes no search-capable tool "
                        f"(profile={self.profile.name}, tools=[{names}]); cannot discover."
                    )
                    return False
                self._search_tool = tool.name
                self._search_tool_schema = _mcp_attr(tool, "input_schema", "inputSchema", {}) or {}
                self.logger.info(
                    f"Connected to MCP catalog (profile={self.profile.name}, "
                    f"search tool='{tool.name}')."
                )
                return True
        except Exception as e:  # noqa: BLE001 - surfaced to the resilient base
            self.logger.error(f"Failed to connect to MCP catalog: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        async with self._open_session() as session:
            tool_name = self._search_tool
            if tool_name is None:
                resolved = self._resolve_search_tool((await session.list_tools()).tools)
                if resolved is None:
                    return []
                tool_name = resolved.name
                self._search_tool_schema = (
                    _mcp_attr(resolved, "input_schema", "inputSchema", {}) or {}
                )
            args = self._build_search_args(filters)
            result = await session.call_tool(tool_name, args)
            if _mcp_attr(result, "is_error", "isError", False):
                self.logger.warning(f"MCP search tool '{tool_name}' returned an error result.")
                return []
            rows = self._extract_rows(result)
            products = [self._row_to_metadata(row) for row in rows]
            # Belt-and-suspenders: re-apply filters client-side in case the
            # server ignored some (e.g. domain/layer/quality) — the shared
            # helper keeps semantics identical to every other connector.
            return self._apply_filters(products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        # Most catalog MCP servers don't expose a cheap "count" tool; derive a
        # lightweight stat from an unfiltered search rather than claim numbers
        # we can't substantiate.
        products = await self._search_data_products_impl(SearchFilters(limit=200))
        scored = [p.quality_score for p in products if p.quality_score is not None]
        return {
            "total_products": len(products),
            "avg_quality": round(sum(scored) / len(scored), 3) if scored else None,
            "source": f"mcp:{self.profile.name}",
        }

    async def _get_data_product_impl(self, product_id: str) -> Optional[DataProductMetadata]:
        """Two-phase detail retrieval — the "all metadata" path.

        Listing (:meth:`_search_data_products_impl`) stays shallow + fast; this
        per-product lookup fetches the RICH entity (description, owners, tags,
        quality, …) via the profile's ``detail_tool`` and the underlying data
        asset's column/field schema via ``schema_tool`` — falling back to the
        shallow search result when no detail tool is configured/available.
        """
        async with self._open_session() as session:
            available = {t.name for t in (await session.list_tools()).tools}

            # Phase 0 — the shallow search row carries the trust fields (quality,
            # layer, tags, usage, version) the listing surfaces. Keep it as the
            # base so `--detailed` is a SUPERSET of the listing, never a subset.
            shallow = await self._search_match(session, product_id, available)

            # Phase 1 — the rich entity detail (description, owners, …), which is
            # often sparser than the search row on the trust fields.
            detail: Optional[DataProductMetadata] = None
            if self.profile.detail_tool and self.profile.detail_tool in available:
                entity = await self._fetch_object(
                    session, self.profile.detail_tool, {self.profile.detail_id_arg: product_id}
                )
                if entity:
                    detail = self._row_to_metadata(entity)

            # Merge: shallow base + detail's genuine additions (see helper). When
            # only one source resolves, that one is used as-is.
            product = _merge_product_metadata(shallow, detail)
            if product is None:
                return None

            # Phase 2 — column/field schema of the underlying data asset.
            if self.profile.schema_tool and self.profile.schema_tool in available:
                product.schema_fields = await self._fetch_schema_fields(session, product_id)
            return product

    async def _search_match(
        self, session: Any, product_id: str, available: set
    ) -> Optional[DataProductMetadata]:
        tool = self._search_tool
        if tool is None or tool not in available:
            resolved = self._resolve_search_tool(list((await session.list_tools()).tools))
            if resolved is None:
                return None
            tool = resolved.name
        result = await session.call_tool(tool, self._build_search_args(SearchFilters(limit=200)))
        for row in self._extract_rows(result):
            mapped = self._row_to_metadata(row)
            if mapped.id == product_id:
                return mapped
        return None

    async def _fetch_object(
        self, session: Any, tool_name: str, args: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Call a tool that returns a single entity object; unwrap the common
        ``{"result": {…}}`` / ``{"entity": {…}}`` envelopes."""
        result = await session.call_tool(tool_name, args)
        if _mcp_attr(result, "is_error", "isError", False):
            return None
        payload = self._result_payload(result)
        if isinstance(payload, dict):
            for key in ("result", "entity", "data"):
                inner = payload.get(key)
                if isinstance(inner, dict):
                    return inner
            return payload
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return None

    async def _fetch_schema_fields(self, session: Any, product_id: str) -> List[Dict[str, Any]]:
        """Retrieve the data asset's column/field schema via ``schema_tool``."""
        obj = await self._fetch_object(
            session, self.profile.schema_tool, {self.profile.schema_id_arg: product_id}
        )
        if not isinstance(obj, dict):
            return []
        raw_fields = obj.get("fields") or obj.get("schemaFields") or obj.get("columns") or []
        p = self.profile
        out: List[Dict[str, Any]] = []
        for field_obj in raw_fields:
            if not isinstance(field_obj, dict):
                continue
            name = _as_str(_first(field_obj, p.schema_field_name_keys))
            if not name:
                continue
            entry: Dict[str, Any] = {
                "name": name,
                "type": _as_str(_first(field_obj, p.schema_field_type_keys)),
                "description": _as_str(_first(field_obj, p.schema_field_desc_keys)),
            }
            if "nullable" in field_obj:
                entry["nullable"] = field_obj["nullable"]
            out.append(entry)
        return out

    @staticmethod
    def _result_payload(result: Any) -> Any:
        """Best-effort JSON payload from a CallToolResult (structured or text)."""
        structured = _mcp_attr(result, "structured_content", "structuredContent")
        if structured not in (None, {}):
            return structured
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    continue
        return None

    # ------------------------------------------------------------------ #
    # Tool resolution + result mapping                                   #
    # ------------------------------------------------------------------ #
    def _resolve_search_tool(self, tools: List[Any]) -> Optional[Any]:
        by_name = {t.name: t for t in tools}
        # 1) Explicit override / profile-preferred tool names (in order).
        preferred = (
            (self.config.get("search_tool"),) if self.config.get("search_tool") else ()
        ) + (self.profile.search_tools)
        for cand in preferred:
            if cand in by_name:
                return by_name[cand]
        # 2) Heuristic: a tool whose name contains a search hint.
        for hint in _SEARCH_NAME_HINTS:
            for t in tools:
                if hint in t.name.lower():
                    return t
        return None

    def _build_search_args(self, filters: SearchFilters) -> Dict[str, Any]:
        args: Dict[str, Any] = dict(self.profile.extra_args)
        if self.profile.query_arg:
            args[self.profile.query_arg] = filters.text_query or filters.domain or "*"
        if self.profile.limit_arg and filters.limit:
            args[self.profile.limit_arg] = int(filters.limit)
        # If the tool advertises an input schema, keep only declared args so a
        # stricter server doesn't reject the call on an unexpected parameter.
        props = (self._search_tool_schema or {}).get("properties")
        if isinstance(props, dict) and props:
            args = {k: v for k, v in args.items() if k in props}
            # Ensure at least the query arg survives if the schema names it.
            if self.profile.query_arg in props and self.profile.query_arg not in args:
                args[self.profile.query_arg] = filters.text_query or "*"
        return args

    def _extract_rows(self, result: Any) -> List[Dict[str, Any]]:
        """Pull a list of row dicts from a CallToolResult.

        Prefers ``structuredContent`` (modern SDK), else parses each text
        content block as JSON. Accepts a bare list, a single object, or a dict
        wrapping the list under a common key.
        """
        structured = _mcp_attr(result, "structured_content", "structuredContent")
        if structured is not None:
            rows = self._coerce_rows(structured)
            if rows:
                return rows
        rows: List[Dict[str, Any]] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                continue
            rows.extend(self._coerce_rows(parsed))
        return rows

    @staticmethod
    def _coerce_rows(payload: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            rows = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            for key in (
                "searchResults",  # DataHub searchAcrossEntities
                "results",
                "products",
                "dataProducts",
                "data",
                "entities",
                "items",
                "hits",
            ):
                val = payload.get(key)
                if isinstance(val, list):
                    rows = [r for r in val if isinstance(r, dict)]
                    break
            else:
                rows = [payload]  # a single product object
        return [McpCatalogConnector._unwrap_envelope(r) for r in rows]

    @staticmethod
    def _unwrap_envelope(row: Dict[str, Any]) -> Dict[str, Any]:
        """Unwrap a search-result envelope to the entity it wraps.

        Search APIs commonly box each hit: DataHub ``{"entity": {...}}``,
        GraphQL ``{"node": {...}}``, Elasticsearch ``{"_source": {...}}``. The
        real product fields live inside, so unwrap to them.
        """
        for env_key in ("entity", "node", "_source", "dataProduct", "dataset"):
            inner = row.get(env_key)
            if isinstance(inner, dict):
                return inner
        return row

    def _row_to_metadata(self, row: Dict[str, Any]) -> DataProductMetadata:
        p = self.profile
        ident = _as_str(_first(row, p.id_keys)) or _as_str(_first(row, p.name_keys)) or "unknown"
        name = _as_str(_first(row, p.name_keys)) or ident
        quality = _normalize_quality(_first(row, p.quality_keys))
        usage_stats: Dict[str, Any] = {}
        for pop_key in ("popularity", "usageCount", "queryCount", "views"):
            if pop_key in row:
                usage_stats[pop_key] = row[pop_key]
        return DataProductMetadata(
            id=ident,
            name=name,
            description=_as_str(_first(row, p.desc_keys)),
            domain=_as_str(_first(row, p.domain_keys)),
            owner=_extract_owner(_first(row, p.owner_keys)),
            layer=_parse_layer(_first(row, p.layer_keys)),
            status=_parse_status(_first(row, p.status_keys)),
            version=_as_str(_first(row, p.version_keys)) or "1.0.0",
            created_at=_parse_dt(_first(row, p.created_keys)),
            updated_at=_parse_dt(_first(row, p.updated_keys)),
            tags=_as_tags(_first(row, p.tags_keys)),
            schema_url=_as_str(_first(row, p.url_keys)) or None,
            quality_score=quality,
            usage_stats=usage_stats,
            catalog_source=f"MCP ({p.name})",
            catalog_type="mcp",
        )
