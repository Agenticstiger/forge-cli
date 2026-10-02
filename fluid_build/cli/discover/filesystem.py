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

"""Filesystem source discoverer.

Walks a directory or matches a glob and emits one stream per file. Schema
is inferred via DuckDB's auto-readers (``read_csv_auto``,
``read_parquet``, ``read_json_auto``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from .registry import DiscoveredColumn, DiscoveredStream, Discoverer


@dataclass
class FilesystemDiscoverer(Discoverer):
    scheme: str = "file"

    def discover(self, uri: str) -> List[DiscoveredStream]:
        path = _normalize(uri)
        files = _expand_glob(path)
        streams: List[DiscoveredStream] = []
        for f in files:
            fmt = _format_for(f)
            cols = _columns_for(f, fmt)
            streams.append(
                DiscoveredStream(
                    name=Path(f).stem,
                    columns=cols,
                    metadata={"path": f, "format": fmt},
                )
            )
        return streams


def _normalize(uri: str) -> str:
    if "://" not in uri:
        return uri
    p = urlparse(uri)
    if p.scheme == "file":
        return p.path
    # s3:// gs:// https:// pass through to DuckDB which loads httpfs / aws
    return uri


def _expand_glob(uri: str) -> List[str]:
    if any(c in uri for c in "*?["):
        # DuckDB readers support globs natively; we just pass through.
        return [uri]
    p = Path(uri)
    if p.is_dir():
        return [str(child) for child in p.iterdir() if child.is_file()]
    if p.exists():
        return [str(p)]
    # Also pass through cloud URIs without local existence check.
    if "://" in uri:
        return [uri]
    raise FileNotFoundError(uri)


def _format_for(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in ("csv",):
        return "csv"
    if suffix in ("parquet", "pq"):
        return "parquet"
    if suffix in ("json", "ndjson", "jsonl"):
        return "json"
    return "csv"  # default


def _columns_for(path: str, fmt: str) -> List[DiscoveredColumn]:
    from fluid_build.providers._duckdb_sandbox import (
        DuckDBAllowlist,
        is_remote_location,
        secure_duckdb_connect,
    )
    from fluid_build.providers._sql_safety import quote_ansi_string_literal

    reader = {"csv": "read_csv_auto", "parquet": "read_parquet", "json": "read_json_auto"}.get(fmt)
    if reader is None:
        return []
    # The one location being discovered. A URL needs its filesystem extension
    # loaded before the sandbox locks (autoloading is off from then on).
    remote = is_remote_location(path)
    extensions: List[str] = []
    if remote:
        extensions = ["azure"] if path.lower().startswith(("azure://", "az://")) else ["httpfs"]
    con = secure_duckdb_connect(
        ":memory:",
        allow=DuckDBAllowlist.none().with_locations(path),
        extensions=extensions,
    )
    try:
        sql = f"SELECT * FROM {reader}({quote_ansi_string_literal(path)}) LIMIT 0"
        con.execute(sql)
        descr = con.description or []
        return [DiscoveredColumn(name=c[0], type=str(c[1]), nullable=True) for c in descr]
    finally:
        con.close()
