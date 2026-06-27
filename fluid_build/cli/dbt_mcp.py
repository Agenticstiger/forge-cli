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

"""Delegate forge agent tool-calls to the **dbt MCP server** (dbt-labs/dbt-mcp).

The forge copilot agent loop can call tools (``forge_copilot_tools``). This
module lets it *delegate* a whole extra tool surface to dbt's official MCP
server — its SQL / Semantic-Layer / Discovery / dbt-CLI tool groups — without
forge re-implementing any of them. Off by default; opt in with
``FLUID_DBT_MCP=1``.

**Borrowed, not built.** This reuses the in-repo MCP *client* pattern from
``cli/market_catalogs/mcp_catalog.py`` (the ``mcp`` SDK's ``ClientSessionGroup``
+ ``StdioServerParameters``, env-sourced secrets, and an ``_open_session`` seam
overridable for in-memory tests) — the same SDK already shipped as a base dep.
The bridge shape (expose a remote MCP server's tools as agent tools) follows the
``langchain-mcp-adapters`` / OpenAI-Agents ``HostedMCPTool`` delegate pattern.
The server itself is dbt-labs/dbt-mcp, launched as a local stdio subprocess
(default ``uvx dbt-mcp``); it reads its dbt credentials (``DBT_HOST`` /
``DBT_TOKEN`` / ``DBT_PROD_ENV_ID`` / ``DBT_PROJECT_DIR`` / …) from the inherited
shell environment, so **no secret ever lands in a config file** — the same model
Claude Desktop and the dbt-mcp docs use.

Tools are namespaced with a ``dbt.`` prefix on the forge side so a dbt tool can
never collide with (or shadow) a native ``@forge_tool``.

Env vars:

* ``FLUID_DBT_MCP``          — ``1``/``true`` to enable the delegate (default off).
* ``FLUID_DBT_MCP_COMMAND``  — launcher for the stdio server (default ``uvx``).
* ``FLUID_DBT_MCP_ARGS``     — args for the launcher (default ``dbt-mcp``);
  shell-split.
* ``FLUID_DBT_MCP_ENV``      — ``A=1,B=2`` extra NON-secret env for the server
  process (URLs/IDs; secrets stay in the inherited shell env).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.dbt_mcp")

# Forge-side namespace so a dbt MCP tool can never collide with a native tool.
TOOL_PREFIX = "dbt."

_TRUTHY = {"1", "true", "yes", "on"}


def is_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the dbt MCP delegate is opted in via ``FLUID_DBT_MCP``."""
    env = env if env is not None else os.environ
    return str(env.get("FLUID_DBT_MCP", "")).strip().lower() in _TRUTHY


def is_dbt_mcp_tool(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when *name* is a delegated dbt MCP tool and the delegate is enabled."""
    return bool(name) and name.startswith(TOOL_PREFIX) and is_enabled(env)


def _run_async(coro) -> Any:
    """Run an async coroutine from forge's synchronous dispatch path.

    The agent loop is synchronous, so the common path is ``asyncio.run``. If we
    are somehow already inside an event loop, run on a dedicated thread with its
    own loop instead of raising ``asyncio.run() cannot be called from a running
    event loop``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _result_to_payload(result: Any) -> Any:
    """Coerce an MCP ``CallToolResult`` to a JSON-serialisable value for the LLM.

    Prefers ``structuredContent`` (modern SDK); else joins the text blocks. The
    return value is sent back to the agent as a tool result, mirroring
    ``forge_copilot_tools.dispatch_tool_call``'s contract (plain dict / str).
    """
    structured = getattr(result, "structuredContent", None)
    if structured not in (None, {}):
        return structured
    texts: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    if texts:
        return "\n".join(texts)
    return {"ok": not getattr(result, "isError", False)}


class DbtMcpClient:
    """Thin client that lists/calls tools on the dbt MCP server over stdio.

    One self-contained MCP session per operation (connect → list/call → close),
    exactly like :class:`McpCatalogConnector` — simpler and less error-prone
    than threading a long-lived session through the sync dispatch path, and a
    forge tool call is already a coarse-grained operation.
    """

    def __init__(
        self,
        *,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Mapping[str, str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        source = env if env is not None else os.environ
        self.command = command or source.get("FLUID_DBT_MCP_COMMAND") or "uvx"
        if args is not None:
            self.args = list(args)
        else:
            self.args = shlex.split(source.get("FLUID_DBT_MCP_ARGS") or "dbt-mcp")
        self._extra_env = self._parse_extra_env(source.get("FLUID_DBT_MCP_ENV"))
        self.logger = logger or LOG

    @staticmethod
    def _parse_extra_env(spec: Optional[str]) -> Dict[str, str]:
        """Parse ``A=1,B=2`` into a dict (NON-secret overrides only)."""
        out: Dict[str, str] = {}
        for pair in (spec or "").split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key = key.strip()
            if key:
                out[key] = value.strip()
        return out

    def _subprocess_env(self) -> Dict[str, str]:
        """Environment for the stdio dbt MCP server.

        Inherits the caller's environment so dbt credentials exported in the
        shell (``DBT_TOKEN`` etc.) reach the server WITHOUT being named in any
        config; ``FLUID_DBT_MCP_ENV`` layers NON-secret overrides on top.
        """
        env: Dict[str, str] = {k: str(v) for k, v in os.environ.items()}
        env.update(self._extra_env)
        return env

    # -- session seam (overridden in tests with an in-memory server) ---------
    def _open_session(self):
        """Async context manager yielding an initialised MCP ``ClientSession``.

        Tests override this to inject an in-memory server session
        (``mcp.shared.memory.create_connected_server_and_client_session``),
        exercising the full client → tool path with zero network / subprocess.
        """
        return self._open_real_session()

    @asynccontextmanager
    async def _open_real_session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSessionGroup, StdioServerParameters
        except ImportError as exc:  # pragma: no cover - mcp is a base dep
            raise RuntimeError(
                "The dbt MCP delegate requires the 'mcp' SDK (ships with fluid-build)."
            ) from exc

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self._subprocess_env(),
        )
        async with ClientSessionGroup() as group:
            session = await group.connect_to_server(params)
            yield session

    # -- async core ----------------------------------------------------------
    async def _list_tools_async(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        async with self._open_session() as session:
            tools = (await session.list_tools()).tools
            return [
                (t.name, getattr(t, "description", "") or "", getattr(t, "inputSchema", {}) or {})
                for t in tools
            ]

    async def _call_tool_async(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        async with self._open_session() as session:
            result = await session.call_tool(tool_name, arguments)
            return _result_to_payload(result)

    # -- sync wrappers (used by the synchronous agent-loop dispatch) ---------
    def list_tools(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Return ``[(name, description, input_schema), …]`` for the dbt server."""
        return _run_async(self._list_tools_async())

    def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a dbt MCP tool (bare server name) and return its payload."""
        return _run_async(self._call_tool_async(tool_name, arguments or {}))


# ---------------------------------------------------------------------------
# Bridge: surface dbt tools to the agent loop + route their calls.
# ---------------------------------------------------------------------------
def dbt_mcp_tool_definitions(env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Forge-shaped tool defs (``dbt.``-prefixed) for the LLM tool list.

    Returns ``[]`` when the delegate is disabled or discovery fails — discovery
    must never break ``get_tool_definitions`` (a missing/unstartable dbt-mcp
    server should degrade to "no dbt tools", not crash the agent loop).
    """
    if not is_enabled(env):
        return []
    try:
        client = DbtMcpClient(env=env)
        defs: List[Dict[str, Any]] = []
        for name, description, schema in client.list_tools():
            defs.append(
                {
                    "name": f"{TOOL_PREFIX}{name}",
                    "description": f"[dbt MCP] {description}".strip(),
                    "input_schema": schema or {"type": "object", "properties": {}},
                }
            )
        return defs
    except Exception as exc:  # noqa: BLE001 - never break tool listing
        # Type-only log (no message interpolation) — the dbt-mcp server's
        # stderr may carry connection-string-shaped text.
        LOG.warning("dbt MCP tool discovery unavailable: %s", type(exc).__name__)
        return []


def dispatch_dbt_mcp_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    """Route a ``dbt.<tool>`` agent call to the dbt MCP server.

    Mirrors ``dispatch_tool_call``'s error contract: a failure returns a typed
    ``{"error": …, "message": …}`` dict (no raw exception text) so the agent
    loop continues.
    """
    bare = name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name
    try:
        return DbtMcpClient(env=env).call_tool(bare, arguments or {})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("dbt MCP tool %s failed: %s", name, type(exc).__name__)
        return {
            "error": type(exc).__name__,
            "message": f"dbt MCP tool {name} failed — see server logs",
        }
