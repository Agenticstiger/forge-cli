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

from .._acquisition_common import register_source_adapter

# NOTE: we deliberately do NOT call ``apply_loopback_host_override``
# here. Meltano taps (tap-postgres / tap-mysql / tap-mssql) run as
# host subprocesses out of the lab's ``.venv.fluid-dev/bin/`` — they
# are not containerised, so the operator's shell ``host: localhost``
# resolves correctly without translation. Calling the override would
# mistranslate ``localhost`` into ``host.docker.internal`` and break
# host-side runs on macOS (where that name is unresolvable).
#
# The Airbyte runner DOES apply the override because PyAirbyte runs
# each source connector as a Docker container with its own loopback.
# Engine-specific: each runner decides based on whether its execution
# model is in-process / subprocess / containerised. See the docstring
# on ``apply_loopback_host_override`` in ``_acquisition_common.py``.


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
    only port-coercion + SSL default is needed."""
    _coerce_port(connection)
    connection.setdefault("ssl", "false")
    return connection


@register_source_adapter("meltano", "mysql")
def _meltano_mysql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Singer ``tap-mysql`` config."""
    _coerce_port(connection)
    return connection


@register_source_adapter("meltano", "mssql")
def _meltano_mssql(connection: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID generic connection → Singer ``tap-mssql`` config."""
    _coerce_port(connection)
    return connection
