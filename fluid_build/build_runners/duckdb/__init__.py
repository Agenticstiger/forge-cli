# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""DuckDB acquisition runner.

Engine name: ``duckdb``. Lane: zero-infra file/JDBC ingestion.

Capabilities: ``full_refresh``, ``incremental_append``, ``schema_discovery``.
"""

from __future__ import annotations

from .runner import DuckdbRunner, execute_duckdb_build

__all__ = ["DuckdbRunner", "execute_duckdb_build"]
