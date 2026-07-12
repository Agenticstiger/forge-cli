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

"""Pydantic args schemas for the forge copilot tool registry.

Lifted from ``cli/forge_copilot_tools.py`` (host file was 1614 LOC).
~260 LOC of pure :class:`pydantic.BaseModel` definitions used by the
``@forge_tool`` decorators in the host file. Re-imported there so
``args_schema=DiscoverWorkspaceArgs`` and the rest keep resolving at
decoration time.

These models are part of the wire shape between the LLM and the
forge copilot — JSON Schema is derived from them and shipped to the
provider's tool-use endpoint. Adding a new field is a wire-breaking
change; default-valued additions are safe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DiscoverWorkspaceArgs(BaseModel):
    """Args for the ``discover_workspace`` tool.

    The tool takes no arguments — the effective scope is the
    caller-provided ``workspace_root`` (SECURITY_REVIEW S-004). The
    former ``workspace_path`` field was a no-op the impl ignored
    (SECURITY_REVIEW I8); advertising a parameter the impl discards
    is misleading to the LLM, so it has been removed. ``extra=ignore``
    means stale clients that still pass ``workspace_path`` (or any
    other field) are silently absorbed rather than rejected.
    """

    model_config = {"extra": "ignore"}


class ReadSampleSchemaArgs(BaseModel):
    path: str = Field(
        description="Absolute or relative path to the sample file.",
    )


class ListTemplatesArgs(BaseModel):
    use_case: str = Field(
        default="",
        description="Optional use-case hint (analytics, etl_pipeline, streaming, ml_pipeline).",
    )
    domain: str = Field(
        default="",
        description="Optional domain hint (finance, healthcare, retail, telco).",
    )


class ProposeContractArgs(BaseModel):
    context: Dict[str, Any] = Field(
        description="User context with project_goal, data_sources, use_case, etc.",
    )
    template: str = Field(
        default="starter",
        description="Template id from the capability matrix (e.g. 'starter', 'analytics').",
    )
    provider: str = Field(
        default="local",
        description="Provider id (e.g. 'local', 'gcp', 'aws', 'snowflake').",
    )

    # SECURITY_REVIEW I4: the ``context`` field is already typed as an
    # arbitrary ``Dict[str, Any]`` so the LLM has all the free-form
    # nesting it needs *inside* a known field. Top-level ``extra=allow``
    # let an LLM smuggle unknown sibling fields past the
    # ``additionalProperties=False`` advertised in the derived JSON
    # Schema. Nothing in the codebase reads top-level extras off this
    # model (``_dispatch_propose_contract`` only touches ``context`` /
    # ``template`` / ``provider``), so ``ignore`` drops them silently
    # without breaking stale clients.
    model_config = {"extra": "ignore"}


class ValidateContractArgs(BaseModel):
    contract: Dict[str, Any] = Field(
        description="The FLUID contract to validate.",
    )

    # SECURITY_REVIEW I4: ``ignore`` (not ``allow``) — drop unknown
    # top-level fields so the LLM can't smuggle data past the
    # advertised ``additionalProperties=False``. The ``contract`` dict
    # itself stays free-form.
    model_config = {"extra": "ignore"}


class ListSchedulersArgs(BaseModel):
    """No arguments — pass ``{}``."""

    model_config = {"extra": "ignore"}


class DiscoverWorkspaceContractsArgs(BaseModel):
    """Args for ``discover_workspace_contracts``.

    Phase 2: catalog-aware picker. Walks the workspace for existing
    ``contract.fluid.yaml`` files, parses metadata + schemas, and
    returns structured records the LLM uses to build correct
    ``consumes[]`` references for ADP/CDP composition.

    The optional ``allowed_upstream_types`` filter lets the picker
    surface only candidates that are valid for the target product
    type (per fluid_build.forge.product_types.allowed_upstream_types).
    """

    allowed_upstream_types: List[str] = Field(
        default_factory=list,
        description=(
            "Filter to upstream products of these productType codes "
            "(SDP/ADP/CDP). Empty list means no filter."
        ),
    )
    max_results: int = Field(
        default=50,
        description="Cap on returned products (default 50, hard max 200).",
    )


class ReadUpstreamSchemaArgs(BaseModel):
    """Args for ``read_upstream_schema``.

    Phase 3.1: ADP/CDP composition agents need the FULL upstream schema
    (column names, types, required flags, descriptions, classifications)
    to author correct join keys + transforms. ``discover_workspace_contracts``
    returns only column names; this tool returns the rich shape for one
    product / expose pair.

    The lookup is deterministic and security-confined to
    ``workspace_root`` (no external network or filesystem access).
    """

    product_id: str = Field(
        description=(
            "FLUID contract id of the upstream product (e.g. "
            "'bronze.crm.orders_v1'). Resolves to the contract.fluid.yaml "
            "under workspace_root that declares this id."
        )
    )
    expose_id: Optional[str] = Field(
        default=None,
        description=(
            "Specific expose to read. When omitted, returns every expose the product publishes."
        ),
    )
    include_classifications: bool = Field(
        default=True,
        description=(
            "Include column-level classification tags (pii, phi, internal, "
            "etc.) when present. Composition agents use this to propagate "
            "tags through joins."
        ),
    )


class ReadLogicalModelArgs(BaseModel):
    """Args for ``read_logical_model`` (Phase 3.5).

    Reads a logical-model sidecar (`<contract>.model.json`) so the
    in-process copilot has the same view as MCP clients (Cursor /
    Claude Desktop) that already advertise this tool.
    """

    path: str = Field(
        description=(
            "Path to the ``.model.json`` logical sidecar, relative to "
            "the workspace root. Resolved + confined to the workspace; "
            "absolute paths and ``..`` segments are rejected."
        )
    )


class SearchSemanticMemoryArgs(BaseModel):
    """Args for ``search_semantic_memory`` (Phase 3.4).

    Looks up prior forged products in the semantic memory store
    (gated by ``FLUID_COPILOT_SEMANTIC_MEMORY``). Composition agents
    call this when an entity / relationship feels familiar — there may
    be a past model that can be a starting point.
    """

    query: str = Field(
        description=(
            "Plain-text query. Hybrid search across past forge episodes; "
            "matches against contract id / domain / column names / "
            "transformation hints."
        )
    )
    limit: int = Field(
        default=3,
        description="Cap on returned matches (default 3, hard max 10).",
    )


class EstimateCostArgs(BaseModel):
    """Args for ``estimate_cost`` (Phase 3.3).

    Returns the projected USD cost for a planned LLM call. Composition
    agents call this BEFORE firing a large prompt so they can either
    (a) compact the prompt, (b) downshift to a cheaper tier, or
    (c) abort cleanly when ``FLUID_COST_LIMIT_USD`` would be exceeded.
    """

    provider: str = Field(
        description=(
            "LLM provider name (openai / anthropic / gemini / ollama / "
            "groq / bedrock / azure / vertex_ai / mistral / cohere)."
        )
    )
    model: str = Field(description="Model id within the provider (e.g. 'gpt-4.1-mini').")
    input_tokens: int = Field(
        default=0,
        description="Estimated input tokens (system + user prompt + tool turns).",
    )
    output_tokens: int = Field(
        default=0,
        description="Estimated output tokens (the model's reply).",
    )


class CheckPiiClassificationArgs(BaseModel):
    """Args for ``check_pii_classification`` (Phase 3.2).

    Walks the upstream chain (via ``consumes[]``) for a column and
    returns the highest sensitivity tag seen. Composition agents call
    this BEFORE projecting the column into a downstream contract so
    PII tags propagate end-to-end.
    """

    product_id: str = Field(description="Product whose column we're checking.")
    column_name: str = Field(description="Column name on one of the product's exposes.")
    expose_id: Optional[str] = Field(
        default=None,
        description="Expose to scope the search to. Omit to search every expose.",
    )
    walk_upstreams: bool = Field(
        default=True,
        description=(
            "When True (default), follow consumes[] chains and return the "
            "highest-sensitivity tag any upstream column with the same "
            "name carries. Composition agents need this — a downstream "
            "ADP can't loosen an upstream's PII tag."
        ),
    )


class GenerateDltSourceArgs(BaseModel):
    """Args for ``generate_dlt_source`` — LLM-native dlt source generator.

    Phase 2: SDP custom-source generation. Given the user's description
    of an external system, the agent emits a Python file under
    ``sources/`` that uses the dlt framework. The contract's build block
    references the module via ``builds[].properties.source.connection.module``.
    """

    name: str = Field(
        description=(
            "Identifier for the source module (e.g. 'stripe_prices'). "
            "Becomes the relative path ``sources/<name>.py`` and the "
            "function name ``<name>_source``."
        )
    )
    api_url: str = Field(
        description="Base URL of the external API (HTTPS preferred).",
    )
    description: str = Field(
        default="",
        description="Plain-text description of what this source acquires.",
    )
    auth_kind: str = Field(
        default="bearer",
        description=(
            "Authentication style: bearer / basic / api_key / none. The "
            "secret comes from an env var named after the source (e.g. "
            "STRIPE_PRICES_TOKEN)."
        ),
    )


class WebFetchArgs(BaseModel):
    """Args for the opt-in ``web_fetch`` tool (FLUID_AGENT_WEB_TOOLS).

    The tool retrieves a single ``http(s)`` URL through the codebase's
    SSRF-safe fetch primitive (``util.safe_http``) — private / loopback /
    link-local / metadata addresses and non-http(s) schemes are refused
    *before* any request is issued, and the connection is DNS-pinned to
    the validated IP so a rebind between check and connect cannot reach a
    private host. Returns the decoded text/HTML body (size-capped).
    """

    url: str = Field(
        description=(
            "Absolute http(s):// URL to fetch. Private, loopback, "
            "link-local, and cloud-metadata (169.254.169.254) addresses "
            "are rejected by the SSRF guard; ftp/file/data/etc. schemes "
            "are refused."
        )
    )

    model_config = {"extra": "ignore"}


class WebSearchArgs(BaseModel):
    """Args for the opt-in ``web_search`` tool (FLUID_AGENT_WEB_TOOLS).

    Runs a web search through a pluggable provider (Tavily or Brave),
    selected by whichever provider API key is present in the environment
    (``TAVILY_API_KEY`` / ``BRAVE_API_KEY``). When no provider key is
    configured the tool returns a typed ``not configured`` result rather
    than crashing the agent loop.
    """

    query: str = Field(description="Plain-text search query.")
    max_results: int = Field(
        default=5,
        description="Cap on returned results (default 5, hard max 10).",
    )

    model_config = {"extra": "ignore"}
