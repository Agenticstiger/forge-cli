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

"""Per-source adapters for the airbyte runner.

Each Airbyte source connector has its own JSON-schema config; FLUID's generic
``connection`` block doesn't always map 1:1. The functions below translate
between the two shapes so contract authors can write a single canonical
``connection: {host, port, database, user, secretRef, schema}`` block and
have it work for any Airbyte source kind we've adapted.

The adapters register themselves at package import time via
``register_source_adapter("airbyte", "<kind>")``. Adding support for a new
Airbyte source = one new function in this file (or a sibling). New engines
follow the same pattern in their own ``sources.py``.

Common cross-cutting concerns we handle once here:

- **field renames**: FLUID ``user`` → Airbyte ``username`` (Airbyte's CDK
  validator hard-rejects ``user``).
- **type coercion**: ``port`` is forced to int; FLUID's ``{{ env.X }}``
  template substitution always yields strings, but Airbyte's JSON-schema
  validator is strict.
- **Container-runtime host translation**: PyAirbyte runs source connectors
  in containers. ``localhost`` inside the connector container is the
  container itself, not the host. The runner consults the operator's
  ``FLUID_RUNNER_HOST_OVERRIDE`` env var (set once for the operator's
  runtime: ``host.docker.internal`` for Docker Desktop, the bridge IP for
  Linux Docker, ``host.containers.internal`` for Podman, a Service name
  for K8s) — see :func:`apply_loopback_host_override`. Contract authors
  keep writing ``host: localhost``; the substitution is purely a
  runtime-topology concern.
- **connector-required defaults**: ``ssl_mode`` / ``replication_method`` /
  etc. that source connectors require but FLUID's generic block doesn't
  declare. Defaults are the most permissive non-TLS, non-CDC variant —
  override by setting the field literally in ``connection``.
"""

from __future__ import annotations

from typing import Any, Dict

from .._acquisition_common import (
    apply_loopback_host_override,
    register_source_adapter,
)


def _coerce_port(connection: Dict[str, Any]) -> None:
    """In-place: force ``port`` to int. Idempotent. Silently leaves
    non-numeric values alone so we don't mask the real validation error
    Airbyte will surface."""
    port = connection.get("port")
    if port is None:
        return
    try:
        connection["port"] = int(port)
    except (TypeError, ValueError):
        pass


def _rename_user_to_username(connection: Dict[str, Any]) -> None:
    """In-place: ``user`` → ``username`` (Airbyte's relational connectors
    require ``username``). No-op when ``username`` is already set."""
    if "user" in connection and "username" not in connection:
        connection["username"] = connection.pop("user")


def _runtime_aware_host(connection: Dict[str, Any]) -> None:
    """Delegate loopback-host translation to the shared helper.

    PyAirbyte runs each source connector as a Docker container; the address
    the connector container needs to reach the host is operator-runtime-
    specific (``host.docker.internal`` on Docker Desktop, the bridge IP on
    Linux, ``host.containers.internal`` on Podman, a Service name on K8s,
    …). The runner doesn't make that choice — it consults the operator's
    ``FLUID_RUNNER_HOST_OVERRIDE`` env var via
    :func:`apply_loopback_host_override` in ``_acquisition_common``.

    No-op when the operator hasn't set an override OR the host is already
    non-loopback. Real prod hosts are never touched.
    """
    apply_loopback_host_override(connection)


@register_source_adapter("airbyte", "postgres")
def _airbyte_postgres(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Airbyte source-postgres spec."""
    _rename_user_to_username(connection)
    _coerce_port(connection)
    _runtime_aware_host(connection)
    connection.setdefault("ssl_mode", {"mode": "disable"})
    connection.setdefault("replication_method", {"method": "Standard"})
    return connection


@register_source_adapter("airbyte", "mysql")
def _airbyte_mysql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Airbyte source-mysql spec."""
    _rename_user_to_username(connection)
    _coerce_port(connection)
    _runtime_aware_host(connection)
    connection.setdefault("ssl_mode", {"mode": "preferred"})
    connection.setdefault("replication_method", {"method": "STANDARD"})
    return connection


@register_source_adapter("airbyte", "mssql")
def _airbyte_mssql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Airbyte source-mssql (SQL Server) spec."""
    _rename_user_to_username(connection)
    _coerce_port(connection)
    _runtime_aware_host(connection)
    connection.setdefault("ssl_method", {"ssl_method": "unencrypted"})
    connection.setdefault("replication_method", {"method": "STANDARD"})
    return connection
