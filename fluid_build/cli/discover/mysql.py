# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""MySQL / MariaDB source discoverer (DuckDB mysql extension)."""

from __future__ import annotations

from dataclasses import dataclass

from ._jdbc_base import MYSQL_CONFIG, JdbcDiscoverer, JdbcSourceConfig


@dataclass
class MySqlDiscoverer(JdbcDiscoverer):
    """Discoverer for ``mysql://`` and ``mariadb://`` URIs.

    All introspection logic — DSN building, ATTACH, ``information_schema``
    walking — lives in :class:`JdbcDiscoverer`. This subclass only
    supplies the per-source config.
    """

    config: JdbcSourceConfig = MYSQL_CONFIG
