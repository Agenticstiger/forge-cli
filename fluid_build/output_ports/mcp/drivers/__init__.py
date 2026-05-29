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

"""Driver registry for the consumer MCP output-port server.

Drivers are keyed on ``(binding.platform, binding.format)``. The
registry intentionally lives in this module (rather than the
top-level ``providers/__init__.py``) because consumer-side drivers
are a separate concern from the build-side providers and have a
narrower surface (just ``execute`` + ``health_check`` rather than
``plan`` / ``apply`` / ``render``).

Out-of-tree drivers can register themselves at runtime via
:func:`register_driver`. Internal teams that ship a Databricks /
Postgres / Redshift driver as a private wheel only need to:

1. Subclass :class:`fluid_build.output_ports.mcp.drivers.base.EngineDriver`.
2. Call ``register_driver(("databricks", "delta_table"), DatabricksDriver)``
   from the wheel's ``__init__``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple, Type

from .athena import AthenaDriver
from .base import EngineDriver, UnsupportedBindingError, get_binding
from .bigquery import BigQueryDriver
from .duckdb import DuckDBDriver
from .postgres import PostgresDriver
from .snowflake import SnowflakeDriver

_LOG = logging.getLogger("fluid.output_port.mcp.drivers")

DriverKey = Tuple[str, str]
"""Tuple of ``(binding.platform, binding.format)`` used to look up
the right driver class."""


_DRIVER_REGISTRY: Dict[DriverKey, Type[EngineDriver]] = {
    ("local", "parquet"): DuckDBDriver,
    ("local", "csv"): DuckDBDriver,
    ("local", "json"): DuckDBDriver,
    ("local", "other"): DuckDBDriver,
    ("gcp", "bigquery_table"): BigQueryDriver,
    ("snowflake", "snowflake_table"): SnowflakeDriver,
    # NEW in v0.7.4: enterprise OLTP + AWS analytics surfaces.
    ("postgres", "postgres_table"): PostgresDriver,
    ("postgres", "table"): PostgresDriver,
    ("aws", "athena_table"): AthenaDriver,
    ("aws", "glue_table"): AthenaDriver,
}


def register_driver(key: DriverKey, driver_class: Type[EngineDriver]) -> None:
    """Register or replace a driver class for ``(platform, format)``.

    Intended for out-of-tree drivers shipped as separate Python
    packages. The most-recently-registered driver wins; calling
    ``register_driver`` with an existing key replaces the previous
    binding silently — this is on purpose, so a customer's private
    package can override the upstream default without forking.
    """
    if not isinstance(key, tuple) or len(key) != 2:
        raise TypeError(f"driver key must be a (platform, format) tuple; got {key!r}")
    platform, fmt = key
    if not isinstance(platform, str) or not isinstance(fmt, str):
        raise TypeError("driver key entries must be strings")
    _DRIVER_REGISTRY[(platform, fmt)] = driver_class
    _LOG.debug("registered driver %s -> %s", key, driver_class.__name__)


def supported_keys() -> Tuple[DriverKey, ...]:
    """Snapshot of registered ``(platform, format)`` pairs.

    Used by ``describe`` and the ``health`` tool to advertise the
    server's supported binding shapes.
    """
    return tuple(sorted(_DRIVER_REGISTRY.keys()))


def build_driver(
    *,
    expose: Mapping[str, Any],
    contract: Mapping[str, Any],
    logger: Optional[logging.Logger] = None,
    extra_kwargs: Optional[Mapping[str, Any]] = None,
) -> EngineDriver:
    """Build the right driver for the given expose's binding.

    Raises :class:`UnsupportedBindingError` if no driver covers the
    ``(platform, format)`` pair. The error message lists the keys
    that are registered so an operator can install the right extra
    or register an out-of-tree driver.
    """
    platform, fmt, _ = get_binding(expose)
    key = (platform, fmt)
    driver_class = _DRIVER_REGISTRY.get(key)
    if driver_class is None:
        raise UnsupportedBindingError(
            f"No driver registered for binding ({platform!r}, {fmt!r}). "
            f"Supported: {sorted(_DRIVER_REGISTRY.keys())}"
        )
    kwargs: Dict[str, Any] = {
        "expose": expose,
        "contract": contract,
        "logger": logger,
    }
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return driver_class(**kwargs)


__all__ = [
    "EngineDriver",
    "DuckDBDriver",
    "BigQueryDriver",
    "SnowflakeDriver",
    "PostgresDriver",
    "AthenaDriver",
    "build_driver",
    "register_driver",
    "supported_keys",
    "UnsupportedBindingError",
]
