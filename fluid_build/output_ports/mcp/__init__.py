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

"""Consumer-side MCP output-port server.

Public API:

* :class:`OutputPortMcpServer` — Anthropic SDK-bound dispatcher
  bound to one expose. Requires the optional ``mcp`` package.
* :class:`OutputPortPolicy` — access-control policy applied to every
  ``tools/call`` request, including ``agentPolicy.allowedModels`` and
  ``agentPolicy.allowedUseCases`` enforcement.
* :func:`build_driver` — factory that resolves the right
  :class:`EngineDriver` for an expose's ``binding``.
* :func:`derive_advertised_tools` — render the ``tools/list``
  advertisement for a given expose + policy.
* :func:`compile_semantic_query` / :func:`compile_free_form_sql` —
  the two query-compilation paths (predeclared semantic / explicit
  SQL).
* :func:`find_expose`, :func:`list_exposes`, :func:`resolve_expose_paths`
  — non-dispatcher utilities used by the CLI; SDK-independent.

The CLI wrapper at :mod:`fluid_build.cli.mcp_output_port` builds an
:class:`OutputPortPolicy` from argparse, finds the bound expose, and
calls :func:`run_stdio`.

The Anthropic ``mcp`` SDK is the only new runtime dependency; if
it is not installed the SDK-bound exports below raise on first
use rather than at import, so utility imports keep working.
"""

from __future__ import annotations

from ._expose_utils import (
    _annotate_engine_error,
    _format_table_reference,
    _jsonable,
    _summarise_arguments,
    find_expose,
    list_exposes,
    resolve_expose_paths,
)
from .drivers import (
    AthenaDriver,
    BigQueryDriver,
    DuckDBDriver,
    EngineDriver,
    PostgresDriver,
    SnowflakeDriver,
    UnsupportedBindingError,
    build_driver,
    register_driver,
    supported_keys,
)
from .policy import OutputPortPolicy
from .query_compiler import (
    CompiledQuery,
    compile_free_form_sql,
    compile_semantic_query,
)
from .tools import (
    OUTPUT_PORT_TOOL_CAPABILITIES,
    ToolCapability,
    check_tool_permission,
    derive_advertised_tools,
)

# SDK-bound dispatcher: imported lazily so module load works without
# the ``mcp`` SDK installed (utility callers don't need it).
try:
    from .server import (
        SERVER_NAME,
        SERVER_VERSION,
        OutputPortMcpServer,
        run_stdio,
    )
except ImportError:  # pragma: no cover - exercised only when mcp SDK absent
    SERVER_NAME = "forge-cli-output-port-mcp"
    SERVER_VERSION = "0.1.0"

    def _missing_sdk(*_args, **_kwargs):  # type: ignore[unused-ignore]
        raise ImportError(
            "The 'mcp' package is required for the Fluid MCP output-port "
            "server. Install with: pip install 'mcp>=1.0,<2.0'"
        )

    OutputPortMcpServer = _missing_sdk  # type: ignore[assignment]
    run_stdio = _missing_sdk  # type: ignore[assignment]


__all__ = [
    # SDK-bound server
    "OutputPortMcpServer",
    "SERVER_NAME",
    "SERVER_VERSION",
    "run_stdio",
    # Expose utilities (SDK-independent)
    "find_expose",
    "list_exposes",
    "resolve_expose_paths",
    # Policy + tools
    "OUTPUT_PORT_TOOL_CAPABILITIES",
    "OutputPortPolicy",
    "ToolCapability",
    "check_tool_permission",
    "derive_advertised_tools",
    # Query compilation
    "CompiledQuery",
    "compile_free_form_sql",
    "compile_semantic_query",
    # Drivers
    "AthenaDriver",
    "BigQueryDriver",
    "DuckDBDriver",
    "EngineDriver",
    "PostgresDriver",
    "SnowflakeDriver",
    "UnsupportedBindingError",
    "build_driver",
    "register_driver",
    "supported_keys",
]
