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

"""Source-discovery CLI for ``fluid init --discover <connection-uri>``.

Introspects a live source and emits a deterministic Bronze contract for every
discoverable stream. Supported source kinds:

  - ``postgres://...``   — full schema/table introspection via DuckDB postgres_scan
  - ``mysql://...``      — same via mysql_scan
  - ``sqlite:///...``    — via sqlite_scan
  - ``s3://`` / ``file://`` / ``https://`` — filesystem (CSV / Parquet / JSONL)

The output is a list of ``contract.<id>.fluid.yaml`` files plus an optional
``<contract>.suggested-pii.yaml`` that captures classifications discovered
during the introspection.

This is the **deterministic** forge path for Bronze. AI-mode forge is
governed by ``contract.py::apply_suggestion`` which honors the same
suggestion-file contract.
"""

from __future__ import annotations

from .filesystem import FilesystemDiscoverer
from .mysql import MySqlDiscoverer
from .postgres import PostgresDiscoverer
from .registry import DISCOVERERS, DiscoveredStream, get_discoverer, register_discoverer

__all__ = [
    "DISCOVERERS",
    "DiscoveredStream",
    "FilesystemDiscoverer",
    "MySqlDiscoverer",
    "PostgresDiscoverer",
    "get_discoverer",
    "register_discoverer",
]


# Auto-register the built-in discoverers (after all per-source modules are
# imported above, breaking the circular import).
register_discoverer("postgres", PostgresDiscoverer())
register_discoverer("mysql", MySqlDiscoverer())
register_discoverer("file", FilesystemDiscoverer())
