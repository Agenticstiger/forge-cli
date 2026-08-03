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

"""DuckDB acquisition runner.

Engine name: ``duckdb``. Lane: zero-infra file/JDBC ingestion.

Capabilities: ``full_refresh``, ``incremental_append``, ``schema_discovery``.
"""

from __future__ import annotations

from .runner import DuckdbRunner, execute_duckdb_build

__all__ = ["DuckdbRunner", "execute_duckdb_build"]
