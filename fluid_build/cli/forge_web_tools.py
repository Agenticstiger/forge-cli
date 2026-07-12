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

"""Opt-in ``web_search`` + ``web_fetch`` tools for the forge agent loop.

The forge copilot agent loop can call tools (``forge_copilot_tools``).
This module adds two **network-egress** tools — one to fetch a single
URL and one to run a web search — that are **off by default** and only
surface when the operator opts in with ``FLUID_AGENT_WEB_TOOLS=1``. The
gate is modelled 1:1 on the dbt-MCP delegate (:mod:`cli.dbt_mcp`):
``is_enabled`` reads the env flag, ``*_tool_definitions`` returns ``[]``
when disabled, and ``dispatch_*`` mirrors the typed-error contract so a
failure never bubbles a raw exception into the LLM context.

Security posture (the whole reason this is gated):

* **``web_fetch`` is SSRF-safe by construction.** Every fetch routes
  through :mod:`fluid_build.util.safe_http` — the codebase's single,
  hardened HTTP surface. That primitive:
    - allows only ``http`` / ``https`` schemes (ftp/file/data/gopher/…
      are refused at URL validation);
    - resolves the hostname and refuses **any** non-public answer —
      loopback (127.0.0.0/8, ::1), RFC-1918 private (10/8, 172.16/12,
      192.168/16), link-local (169.254/16 incl. the cloud-metadata
      endpoint 169.254.169.254), CG-NAT, IPv4-mapped-IPv6, ULA
      (fc00::/7), and the extended reserved/TEST-NET/6to4/NAT64 ranges;
    - **pins the connection to the validated IP** via httpx's
      ``sni_hostname`` extension so a DNS rebind between the check and
      the connect cannot land on a private host (defeats the classic
      resolve-then-connect TOCTOU);
    - does not follow redirects by default (no bearer-leak second hop);
    - streams with a hard byte cap.
  The guard is asserted **before** any request is issued, so a rejected
  URL never touches the network.

* **``web_search`` never fabricates results and never leaks keys.** It
  selects a provider purely from which API key is present in the
  environment; with no key it returns a typed "not configured" result
  (the loop keeps going). The provider endpoints are fixed public hosts,
  the request is still made through the SSRF-safe client
  (``require_https``), and the key travels in a header — the redactor
  (``observability.secret_redactor``) masks the Tavily/Brave key shapes
  so they never reach logs.

Borrow-before-build receipts:
  * SSRF model — "resolve once, connect to the pinned IP, never trust a
    second resolution" — is the canonical fix documented across the SSRF
    literature (Stripe Smokescreen, requests-hardened, the httpx DNS-pin
    recipe in encode/httpx#2811). We reuse the in-repo implementation of
    exactly that (``util.safe_http``) rather than re-deriving it.
  * The tool shape (name + description + JSON-schema + typed result) and
    the env-gated "return [] when off" delegate follow LangChain's
    web-search/web-fetch tool contract and this repo's own dbt-MCP gate.
  * Provider wire shapes: Tavily ``POST https://api.tavily.com/search``
    (``Authorization: Bearer tvly-…``); Brave
    ``GET https://api.search.brave.com/res/v1/web/search``
    (``X-Subscription-Token: …``).

Env vars:

* ``FLUID_AGENT_WEB_TOOLS`` — ``1``/``true`` to expose both tools
  (default off → both ABSENT from ``get_tool_definitions``).
* ``TAVILY_API_KEY``        — enables the Tavily search provider.
* ``BRAVE_API_KEY`` / ``BRAVE_SEARCH_API_KEY`` — enables the Brave
  search provider (used only when no Tavily key is set).
* ``FLUID_WEB_SEARCH_PROVIDER`` — force ``tavily`` / ``brave`` instead of
  auto-selecting by key presence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Optional

LOG = logging.getLogger("fluid.cli.forge_web_tools")

_TRUTHY = {"1", "true", "yes", "on"}

# Tool names — kept as a tuple so both the gate predicate and the
# definition builder read from one source of truth.
WEB_TOOL_NAMES = ("web_search", "web_fetch")

# web_fetch caps. The raw download is streamed + hard-capped by
# safe_http; we additionally truncate the *decoded* text so a large but
# in-cap page cannot blow the LLM context window.
MAX_FETCH_BYTES = 3 * 1024 * 1024  # 3 MiB downloaded
MAX_FETCH_CHARS = 100_000  # decoded characters returned to the model
FETCH_TIMEOUT_SECONDS = 20.0

# web_search caps.
SEARCH_TIMEOUT_SECONDS = 15.0
_MAX_SEARCH_RESULTS = 10

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


# ---------------------------------------------------------------------------
# Gate (mirrors cli.dbt_mcp)
# ---------------------------------------------------------------------------
def is_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the web tools are opted in via ``FLUID_AGENT_WEB_TOOLS``."""
    env = env if env is not None else os.environ
    return str(env.get("FLUID_AGENT_WEB_TOOLS", "")).strip().lower() in _TRUTHY


def is_web_tool(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when *name* is a web tool and the delegate is enabled."""
    return bool(name) and name in WEB_TOOL_NAMES and is_enabled(env)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
def _input_schema(model: Any) -> Dict[str, Any]:
    """Derive the LLM-facing JSON Schema from a Pydantic args model.

    Matches ``forge_tool.ForgeTool.input_schema`` — the args model is the
    single source of truth and unknown fields are rejected.
    """
    schema = model.model_json_schema()
    schema.setdefault("additionalProperties", False)
    return schema


def web_tool_definitions(env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Forge-shaped tool defs for the LLM tool list.

    Returns ``[]`` when the delegate is disabled (default) so the two
    tools are simply ABSENT from ``get_tool_definitions`` — the exact
    gate semantics of the dbt-MCP delegate.
    """
    if not is_enabled(env):
        return []
    from fluid_build.cli._forge_copilot_tool_args import WebFetchArgs, WebSearchArgs

    return [
        {
            "name": "web_fetch",
            "description": (
                "Fetch a single http(s) URL and return its decoded "
                "text/HTML body (size-capped). SSRF-safe: private, "
                "loopback, link-local, and cloud-metadata addresses are "
                "refused before any request is made, and the connection "
                "is pinned to the validated IP. Use for reading a known "
                "documentation / API / spec page."
            ),
            "input_schema": _input_schema(WebFetchArgs),
        },
        {
            "name": "web_search",
            "description": (
                "Run a web search and return ranked results "
                "({title, url, snippet}). Uses a pluggable provider "
                "(Tavily or Brave) selected by the configured API key; "
                "if none is configured it returns a typed "
                "'not configured' result rather than failing the run."
            ),
            "input_schema": _input_schema(WebSearchArgs),
        },
    ]


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------
def _charset_from_content_type(content_type: str) -> str:
    """Extract the charset from a ``Content-Type`` header (default utf-8)."""
    for part in content_type.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            candidate = part.split("=", 1)[1].strip().strip('"').strip("'")
            if candidate:
                return candidate
    return "utf-8"


def _web_fetch(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch one URL through the SSRF-safe primitive and return its body.

    Every reject path returns the repo's typed-tool-error shape
    ``{"error": <ExcName>, "message": …}``; the SSRF guard fires before
    any request is issued.
    """
    from fluid_build.cli._forge_copilot_tool_args import WebFetchArgs
    from fluid_build.util.safe_http import UnsafeURLError, assert_safe_url, fetch_bytes

    try:
        args = WebFetchArgs.model_validate(arguments)
    except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError etc.
        return {
            "error": "ToolValidationError",
            "message": f"web_fetch got invalid args: {exc}",
        }

    url = (args.url or "").strip()
    if not url:
        return {"error": "InvalidArgs", "message": "url is required"}

    # SSRF gate FIRST — validate + resolve + public-IP check before any
    # request. A rejected URL (private/loopback/link-local/metadata or a
    # non-http(s) scheme, incl. a public hostname that resolves to a
    # private IP — DNS rebind) raises here, so the network is never
    # touched. ``UnsafeURLError`` messages are self-authored and safe to
    # surface to the model (they name the class of block, not a secret).
    try:
        assert_safe_url(url)
    except UnsafeURLError as exc:
        LOG.warning("web_fetch refused unsafe URL: %s", type(exc).__name__)
        return {"error": "UnsafeURLError", "message": str(exc)}

    try:
        status, headers, body = fetch_bytes(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            max_bytes=MAX_FETCH_BYTES,
        )
    except UnsafeURLError as exc:
        # Belt-and-suspenders: fetch_bytes re-validates at connect time
        # (the DNS-pin hook). Should be unreachable after assert_safe_url,
        # but if a rebind slips between the two resolutions it is caught
        # here too.
        LOG.warning("web_fetch blocked at connect time: %s", type(exc).__name__)
        return {"error": "UnsafeURLError", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — httpx / decode / network
        # Do NOT echo the raw exception (may embed the URL, headers, or
        # transport internals) — typed class name + generic message only.
        LOG.warning("web_fetch failed for %s: %s", url, type(exc).__name__, exc_info=True)
        return {
            "error": type(exc).__name__,
            "message": "web_fetch failed — see server logs",
        }

    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value
            break

    charset = _charset_from_content_type(content_type)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        # Unknown charset label — fall back to utf-8.
        text = body.decode("utf-8", errors="replace")

    truncated = len(text) > MAX_FETCH_CHARS
    if truncated:
        text = text[:MAX_FETCH_CHARS]

    return {
        "url": url,
        "status": status,
        "content_type": content_type,
        "bytes": len(body),
        "truncated": truncated,
        "text": text,
    }


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
def _select_search_provider(env: Mapping[str, str]) -> Optional[str]:
    """Return ``"tavily"`` / ``"brave"`` / ``None`` given the environment.

    ``FLUID_WEB_SEARCH_PROVIDER`` forces a provider (only honoured when
    the matching key is present); otherwise Tavily wins when its key is
    set, else Brave.
    """
    forced = str(env.get("FLUID_WEB_SEARCH_PROVIDER", "")).strip().lower()
    tavily_key = env.get("TAVILY_API_KEY")
    brave_key = env.get("BRAVE_API_KEY") or env.get("BRAVE_SEARCH_API_KEY")

    if forced == "tavily" and tavily_key:
        return "tavily"
    if forced == "brave" and brave_key:
        return "brave"
    if forced in ("tavily", "brave"):
        # Explicitly requested but its key is missing → treat as
        # unconfigured (fall through to the typed "not configured").
        return None
    if tavily_key:
        return "tavily"
    if brave_key:
        return "brave"
    return None


def _search_tavily(query: str, max_results: int, api_key: str) -> List[Dict[str, Any]]:
    """Call Tavily and return ``[{title, url, snippet}]``."""
    from fluid_build.util.safe_http import safe_httpx_client

    with safe_httpx_client(require_https=True, timeout=SEARCH_TIMEOUT_SECONDS) as client:
        resp = client.post(
            _TAVILY_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        payload = resp.json()

    results: List[Dict[str, Any]] = []
    for item in (payload.get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or "",
            }
        )
    return results


def _search_brave(query: str, max_results: int, api_key: str) -> List[Dict[str, Any]]:
    """Call Brave and return ``[{title, url, snippet}]``."""
    from fluid_build.util.safe_http import safe_httpx_client

    with safe_httpx_client(require_https=True, timeout=SEARCH_TIMEOUT_SECONDS) as client:
        resp = client.get(
            _BRAVE_ENDPOINT,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            params={"q": query, "count": max_results},
        )
        resp.raise_for_status()
        payload = resp.json()

    results: List[Dict[str, Any]] = []
    web = payload.get("web") if isinstance(payload, dict) else None
    for item in ((web or {}).get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("description") or "",
            }
        )
    return results


def _web_search(
    arguments: Dict[str, Any],
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Run a web search via the configured provider.

    With no provider key the tool returns a typed ``not configured``
    result (never raises) so the agent loop continues.
    """
    from fluid_build.cli._forge_copilot_tool_args import WebSearchArgs

    env = env if env is not None else os.environ

    try:
        args = WebSearchArgs.model_validate(arguments)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "ToolValidationError",
            "message": f"web_search got invalid args: {exc}",
        }

    query = (args.query or "").strip()
    if not query:
        return {"error": "InvalidArgs", "message": "query is required"}
    max_results = max(1, min(int(args.max_results or 5), _MAX_SEARCH_RESULTS))

    provider = _select_search_provider(env)
    if provider is None:
        return {
            "error": "SearchProviderNotConfigured",
            "message": (
                "No web-search provider configured. Set TAVILY_API_KEY "
                "or BRAVE_API_KEY to enable web_search."
            ),
            "results": [],
        }

    try:
        if provider == "tavily":
            results = _search_tavily(query, max_results, env["TAVILY_API_KEY"])
        else:
            key = env.get("BRAVE_API_KEY") or env.get("BRAVE_SEARCH_API_KEY") or ""
            results = _search_brave(query, max_results, key)
    except Exception as exc:  # noqa: BLE001 — never leak the key-bearing request
        LOG.warning("web_search (%s) failed: %s", provider, type(exc).__name__, exc_info=True)
        return {
            "error": type(exc).__name__,
            "message": f"web_search via {provider} failed — see server logs",
            "results": [],
        }

    return {"provider": provider, "query": query, "results": results}


# ---------------------------------------------------------------------------
# Dispatch (mirrors cli.dbt_mcp.dispatch_dbt_mcp_tool)
# ---------------------------------------------------------------------------
def dispatch_web_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    """Route a ``web_search`` / ``web_fetch`` agent call.

    Mirrors ``dispatch_tool_call``'s error contract: a failure returns a
    typed ``{"error": …, "message": …}`` dict (no raw exception text) so
    the agent loop continues.
    """
    args = arguments or {}
    try:
        if name == "web_fetch":
            return _web_fetch(args)
        if name == "web_search":
            return _web_search(args, env)
    except Exception as exc:  # noqa: BLE001 — final safety net
        LOG.warning("web tool %s failed: %s", name, type(exc).__name__, exc_info=True)
        return {
            "error": type(exc).__name__,
            "message": f"web tool {name} failed — see server logs",
        }
    return {"error": "UnknownTool", "message": f"Unknown web tool: {name}"}
