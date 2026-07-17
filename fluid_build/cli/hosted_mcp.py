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

"""Hosted-MCP registry — delegate the forge agent loop to hosted MCP servers.

The forge copilot agent loop can call tools (``forge_copilot_tools``). The
``cli/dbt_mcp`` delegate already lets it *delegate* a whole tool surface to ONE
hosted MCP server (dbt-labs/dbt-mcp). This module generalises that into a
**registry** so the agent can delegate to any number of hosted MCP servers —
shipping **GitHub MCP** (github/github-mcp-server) and **Snowflake MCP**
(Snowflake-Labs/mcp) as built-in, opt-in servers.

**Borrowed, not built.** This is a direct generalisation of the in-repo
``cli/dbt_mcp`` delegate: the stdio client mechanics (``ClientSessionGroup`` +
``StdioServerParameters``, one self-contained session per operation, env-sourced
secrets, and an ``_open_session`` seam overridable for in-memory tests) are the
same, and the ``_run_async`` / ``_result_to_payload`` helpers are imported from
``dbt_mcp`` rather than re-implemented. The registry shape (one spec per server,
prefixed tool names, ``[]`` on discovery failure) follows OpenAI-Agents'
``HostedMCPTool`` / ``langchain-mcp-adapters`` multi-server delegate pattern.

Each server:

* is **off by default** and enabled with its own flag (``FLUID_GITHUB_MCP=1`` /
  ``FLUID_SNOWFLAKE_MCP=1``);
* has its tools **namespaced with a per-server prefix** (``github.`` /
  ``snowflake.``) so a delegated tool can never collide with (or shadow) a
  native ``@forge_tool``;
* **reads its credentials from the inherited shell environment** (a GitHub PAT
  in ``GITHUB_PERSONAL_ACCESS_TOKEN``; a Snowflake PAT / connection in the
  ``SNOWFLAKE_*`` vars) — exactly like dbt-mcp and Claude Desktop, so **no
  secret ever lands in a config file or a tool argument**;
* is launched as a **local stdio subprocess** whose command + args are
  overridable per server (``FLUID_<SERVER>_MCP_COMMAND`` /
  ``FLUID_<SERVER>_MCP_ARGS``) so an operator can point at their exact install
  (a pinned Docker tag, a ``uvx`` package version, a local binary).

Transport note (scoped follow-up): the built-in specs use **stdio** (the
transport dbt-mcp, Claude Desktop, and Cursor use, and the one this repo's MCP
client already speaks). The hosted *remote* variants — GitHub's
``https://api.githubcopilot.com/mcp/`` and Snowflake's managed Cortex MCP — use
Streamable-HTTP + OAuth, a different transport + auth surface. That is a
deliberate follow-up: it slots in behind the same ``_open_session`` seam (swap
``StdioServerParameters`` for ``streamablehttp_client`` + an OAuth provider)
without changing the registry, the bridge, or the dispatch contract.

Security posture (identical to the merged dbt-mcp delegate): discovery never
breaks the native tool listing (``[]`` on failure); every error is **type-only
logged** (a hosted server's stderr can carry connection-string-shaped text);
dispatch returns the repo's typed-``{"error", "message"}`` contract with no raw
exception text; and the delegate is resolved AFTER the native registries so a
native tool always wins.
"""

from __future__ import annotations

import logging
import os
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

# Reuse dbt-mcp's async bridge + result coercion verbatim (borrow, no drift).
from fluid_build.cli.dbt_mcp import _result_to_payload, _run_async

LOG = logging.getLogger("fluid.cli.hosted_mcp")

_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Server spec + registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HostedMcpServerSpec:
    """One hosted MCP server the agent can delegate to.

    Fields:

    * ``name``            — registry key (``"github"``).
    * ``prefix``          — forge-side tool namespace (``"github."``); MUST end
      with ``.`` so delegated names can't collide with native tools.
    * ``label``           — human label used in tool descriptions
      (``"[GitHub MCP] …"``).
    * ``enable_env``      — the opt-in flag (``"FLUID_GITHUB_MCP"``).
    * ``default_command`` / ``default_args`` — the stdio launcher when no
      override env is set.
    * ``command_env`` / ``args_env`` / ``extra_env_var`` — per-server overrides
      (default to ``FLUID_<NAME>_MCP_COMMAND`` / ``_ARGS`` / ``_ENV``).
    * ``secret_env_hint`` — documented shell env vars the server reads for its
      credentials (surfaced in ``fluid doctor``; NEVER written anywhere).
    """

    name: str
    prefix: str
    label: str
    enable_env: str
    default_command: str
    default_args: Tuple[str, ...]
    command_env: str = ""
    args_env: str = ""
    extra_env_var: str = ""
    secret_env_hint: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Fill the override-env-var names from the server name when not given,
        # so a new spec only needs to name its ``enable_env`` explicitly.
        upper = self.name.upper()
        object.__setattr__(self, "command_env", self.command_env or f"FLUID_{upper}_MCP_COMMAND")
        object.__setattr__(self, "args_env", self.args_env or f"FLUID_{upper}_MCP_ARGS")
        object.__setattr__(self, "extra_env_var", self.extra_env_var or f"FLUID_{upper}_MCP_ENV")


HOSTED_MCP_REGISTRY: Dict[str, HostedMcpServerSpec] = {}
"""Every registered hosted MCP server, keyed by ``spec.name``."""


def register_hosted_mcp_server(spec: HostedMcpServerSpec) -> None:
    """Register (or replace) a hosted MCP server spec."""
    HOSTED_MCP_REGISTRY[spec.name] = spec


# -- built-in servers --------------------------------------------------------
# GitHub MCP (github/github-mcp-server): local stdio via the official Docker
# image; the server reads GITHUB_PERSONAL_ACCESS_TOKEN from the inherited env.
register_hosted_mcp_server(
    HostedMcpServerSpec(
        name="github",
        prefix="github.",
        label="GitHub MCP",
        enable_env="FLUID_GITHUB_MCP",
        default_command="docker",
        default_args=(
            "run",
            "-i",
            "--rm",
            "-e",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ),
        secret_env_hint=("GITHUB_PERSONAL_ACCESS_TOKEN",),
    )
)

# Snowflake MCP (Snowflake-Labs/mcp): local stdio via uvx; the server reads its
# connection (SNOWFLAKE_PAT / account / user / …) from the inherited env.
register_hosted_mcp_server(
    HostedMcpServerSpec(
        name="snowflake",
        prefix="snowflake.",
        label="Snowflake MCP",
        enable_env="FLUID_SNOWFLAKE_MCP",
        default_command="uvx",
        default_args=("snowflake-labs-mcp",),
        secret_env_hint=("SNOWFLAKE_PAT", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"),
    )
)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
def _is_spec_enabled(spec: HostedMcpServerSpec, env: Mapping[str, str]) -> bool:
    return str(env.get(spec.enable_env, "")).strip().lower() in _TRUTHY


def enabled_specs(env: Optional[Mapping[str, str]] = None) -> List[HostedMcpServerSpec]:
    """Return the specs whose opt-in flag is set (stable registry order)."""
    env = env if env is not None else os.environ
    return [spec for spec in HOSTED_MCP_REGISTRY.values() if _is_spec_enabled(spec, env)]


def _spec_for_tool(
    name: str, env: Optional[Mapping[str, str]] = None
) -> Optional[HostedMcpServerSpec]:
    """Return the enabled spec whose prefix matches *name* (else None)."""
    if not name:
        return None
    for spec in enabled_specs(env):
        if name.startswith(spec.prefix):
            return spec
    return None


def is_hosted_mcp_tool(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when *name* is a delegated hosted-MCP tool whose server is enabled."""
    return _spec_for_tool(name, env) is not None


_DEFAULT_OPERATION_TIMEOUT_SECONDS = 120.0


def _operation_timeout_seconds() -> Optional[float]:
    """Wall-clock bound for one hosted-MCP operation (connect → op → close).

    A hosted server that never responds must fail the forge tool call in
    seconds, not hang the synchronous agent loop. Overridable via
    ``FLUID_HOSTED_MCP_TIMEOUT_SECONDS``; ``0`` (or negative) disables the
    bound entirely.
    """
    raw = (os.environ.get("FLUID_HOSTED_MCP_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_OPERATION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("invalid FLUID_HOSTED_MCP_TIMEOUT_SECONDS=%r — using default", raw)
        return _DEFAULT_OPERATION_TIMEOUT_SECONDS
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Client (generalises DbtMcpClient; one session per operation)
# ---------------------------------------------------------------------------
class HostedMcpClient:
    """Thin stdio client that lists/calls tools on a hosted MCP server.

    One self-contained MCP session per operation (connect → list/call → close),
    exactly like :class:`fluid_build.cli.dbt_mcp.DbtMcpClient` — a forge tool
    call is already a coarse-grained operation, so a long-lived session buys
    nothing and complicates the sync dispatch path.
    """

    def __init__(
        self,
        spec: HostedMcpServerSpec,
        *,
        env: Optional[Mapping[str, str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        source = env if env is not None else os.environ
        self.spec = spec
        self.command = source.get(spec.command_env) or spec.default_command
        args_override = source.get(spec.args_env)
        self.args = shlex.split(args_override) if args_override else list(spec.default_args)
        self._extra_env = self._parse_extra_env(source.get(spec.extra_env_var))
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
        """Environment for the stdio server.

        Inherits the caller's environment so credentials exported in the shell
        (``GITHUB_PERSONAL_ACCESS_TOKEN``, ``SNOWFLAKE_PAT`` …) reach the server
        WITHOUT being named in any config; the per-server ``*_MCP_ENV`` layers
        NON-secret overrides on top.
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
        The remote-HTTP transport (scoped follow-up) also swaps in here.
        """
        return self._open_real_session()

    @asynccontextmanager
    async def _open_real_session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSessionGroup, StdioServerParameters
        except ImportError as exc:  # pragma: no cover - mcp is a base dep
            raise RuntimeError(
                "Hosted-MCP delegation requires the 'mcp' SDK (ships with fluid-build)."
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
        """Return ``[(name, description, input_schema), …]`` for this server."""
        return _run_async(self._list_tools_async(), timeout_seconds=_operation_timeout_seconds())

    def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a hosted MCP tool (bare server-side name) and return its payload."""
        return _run_async(
            self._call_tool_async(tool_name, arguments or {}),
            timeout_seconds=_operation_timeout_seconds(),
        )


# ---------------------------------------------------------------------------
# Bridge: surface hosted tools to the agent loop + route their calls.
# ---------------------------------------------------------------------------
def hosted_mcp_tool_definitions(
    env: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Forge-shaped tool defs (prefixed) for every enabled hosted MCP server.

    Returns ``[]`` for a server whose discovery fails — a missing / unstartable
    server degrades to "no tools from that server", never crashing the agent
    loop's tool listing.
    """
    # A hosted server's tool descriptions are UNTRUSTED — a malicious/compromised
    # server could embed injection in a description that lands in the model's
    # context. Neutralise turn-boundary spoofing before advertising (mirrors
    # Command Center's mcp/sanitize.py; the security core of the registry).
    from fluid_build.cli._untrusted_content import demote_markers

    defs: List[Dict[str, Any]] = []
    for spec in enabled_specs(env):
        try:
            tools = HostedMcpClient(spec, env=env).list_tools()
        except Exception as exc:  # noqa: BLE001 - never break tool listing
            # Type-only log (no message interpolation) — the server's stderr may
            # carry connection-string-shaped text.
            LOG.warning("%s tool discovery unavailable: %s", spec.name, type(exc).__name__)
            continue
        for name, description, schema in tools:
            safe_desc = demote_markers(f"[{spec.label}] {description}".strip())
            defs.append(
                {
                    "name": f"{spec.prefix}{name}",
                    "description": safe_desc,
                    "input_schema": schema or {"type": "object", "properties": {}},
                }
            )
    return defs


def dispatch_hosted_mcp_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    """Route a ``<server>.<tool>`` agent call to the right hosted MCP server.

    Mirrors ``dispatch_tool_call``'s error contract: a failure returns a typed
    ``{"error": …, "message": …}`` dict (no raw exception text) so the agent
    loop continues.
    """
    spec = _spec_for_tool(name, env)
    if spec is None:
        return {"error": "UnknownTool", "message": f"Unknown hosted-MCP tool: {name}"}
    bare = name[len(spec.prefix) :]
    try:
        payload = HostedMcpClient(spec, env=env).call_tool(bare, arguments or {})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("hosted MCP tool %s failed: %s", name, type(exc).__name__)
        return {
            "error": type(exc).__name__,
            "message": f"hosted MCP tool {name} failed — see server logs",
        }

    # A hosted server's tool OUTPUT is UNTRUSTED content flowing straight into
    # the model's context — neutralise turn-boundary spoofing in every string
    # leaf before returning it (structure preserved so JSON payloads stay
    # parseable). Mirrors Command Center's mcp/sanitize.py posture.
    from fluid_build.cli._untrusted_content import neutralize_data

    return neutralize_data(payload)
