# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Postgres source discoverer (DuckDB postgres extension)."""

from __future__ import annotations

from dataclasses import dataclass

from ._jdbc_base import POSTGRES_CONFIG, JdbcDiscoverer, JdbcSourceConfig


@dataclass
class PostgresDiscoverer(JdbcDiscoverer):
    """Discoverer for ``postgres://`` and ``postgresql://`` URIs.

    All introspection logic — DSN building, ATTACH, ``information_schema``
    walking — lives in :class:`JdbcDiscoverer`. This subclass only
    supplies the per-source config.
    """

    config: JdbcSourceConfig = POSTGRES_CONFIG
