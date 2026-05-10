# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Per-source adapters for the Meltano (Singer-tap) runner.

Each Singer tap has its own JSON-schema config (declared in the tap's
``settings`` block); FLUID's generic ``connection`` block doesn't always
map 1:1. The functions below translate between the two so contract
authors can write a single canonical ``connection: {host, port,
database, user, secretRef}`` block and have it work for any Singer tap
kind we've adapted.

Adapters register themselves at package import time via
``register_source_adapter("meltano", "<kind>")``. Adding support for a new
Singer tap = one new function in this file.

Cross-cutting concerns:

- **type coercion**: ``port`` is forced to int. FLUID's ``{{ env.X }}``
  template substitution always yields strings; tap-postgres' Singer-SDK
  validator hard-rejects ``port: "5433"``.
- **container-runtime loopback override**: Singer taps inherit the
  FLUID process's network namespace. When FLUID itself runs inside a
  container (lab Jenkins, CI runner, Codespaces, …), ``localhost``
  points at the container, not the operator's host. The operator sets
  ``FLUID_RUNNER_HOST_OVERRIDE`` / ``TESTCONTAINERS_HOST_OVERRIDE`` to
  reach the host (``host.docker.internal`` on Docker Desktop, the
  bridge IP on Linux, ``host.containers.internal`` on Podman), and we
  rewrite contract-author ``host: localhost`` accordingly. No-op on
  operator hosts where the env var isn't set.
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
    the tap will surface."""
    port = connection.get("port")
    if port is None:
        return
    try:
        connection["port"] = int(port)
    except (TypeError, ValueError):
        pass


@register_source_adapter("meltano", "postgres")
def _meltano_postgres(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Singer ``tap-postgres`` config.

    Field names are identical (host, port, database, user, password) so
    only port-coercion + SSL default + loopback override are needed."""
    _coerce_port(connection)
    apply_loopback_host_override(connection)
    connection.setdefault("ssl", "false")
    return connection


@register_source_adapter("meltano", "mysql")
def _meltano_mysql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Singer ``tap-mysql`` config."""
    _coerce_port(connection)
    apply_loopback_host_override(connection)
    return connection


@register_source_adapter("meltano", "mssql")
def _meltano_mssql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Singer ``tap-mssql`` config."""
    _coerce_port(connection)
    apply_loopback_host_override(connection)
    return connection
