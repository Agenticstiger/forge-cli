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

"""DuckDB driver for the consumer MCP output-port server.

DuckDB is the Phase-1 reference driver because:

* No cloud credentials needed — the cert script and integration
  tests can run in any sandbox.
* DuckDB is the engine FLUID's ``local`` provider already uses
  (see ``fluid_build.engines.dbt_duckdb``), so a consumer who
  developed a contract locally can serve it via MCP without changing
  a line.
* The dialect is close enough to BigQuery / Snowflake that the same
  query-compiler output runs on all three engines (parameter syntax
  is the only meaningful difference).

Connection model:

* If ``binding.location.path`` is set, opens that file (read-only).
* Else opens an in-memory database — used by tests to load CSV
  fixtures and serve them as exposes.
* The optional ``binding.location.attach`` list can carry additional
  files to attach (e.g. a parquet manifest); each entry must be a
  string filesystem path that resolves under the server's
  ``readable_paths`` allowlist.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident, validate_sql_expression_allowlist

from .base import (
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    UnsupportedBindingError,
    get_binding,
    guard_against_injection_markers,
)


class DuckDBDriver(EngineDriver):
    """DuckDB-backed engine driver.

    Reads from a file when ``binding.location.path`` is set; falls
    back to ``:memory:`` otherwise. Tables are referenced by their
    ``binding.location.table`` field, validated as a SQL identifier.
    """

    name = "duckdb"

    def __init__(
        self,
        *,
        expose: Mapping[str, Any],
        contract: Mapping[str, Any],
        logger: Optional[logging.Logger] = None,
        connection_options: Optional[Mapping[str, Any]] = None,
        readable_paths: Tuple[Path, ...] = (),
    ) -> None:
        super().__init__(
            expose=expose,
            contract=contract,
            logger=logger,
            connection_options=connection_options,
        )
        platform, fmt, location = get_binding(expose)
        if platform != "local":
            raise UnsupportedBindingError(
                f"DuckDBDriver requires binding.platform='local'; got {platform!r}"
            )
        if fmt not in {"parquet", "csv", "json", "other"}:
            raise UnsupportedBindingError(
                f"DuckDBDriver does not yet handle binding.format={fmt!r}"
            )
        table_raw = location.get("table") or location.get("name")
        if not isinstance(table_raw, str) or not table_raw:
            raise UnsupportedBindingError("DuckDBDriver requires binding.location.table (or .name)")
        self._table = validate_ident(table_raw)
        path_raw = location.get("path")
        self._path: Optional[Path] = None
        if isinstance(path_raw, str) and path_raw:
            candidate = Path(path_raw).expanduser().resolve()
            if readable_paths and not _path_is_under(candidate, readable_paths):
                raise UnsupportedBindingError(
                    f"binding.location.path {candidate} is outside --readable-paths"
                )
            self._path = candidate
        attach_paths_raw = location.get("attach") or []
        self._attach: Tuple[Path, ...] = tuple(
            self._resolve_attach_path(item, readable_paths) for item in attach_paths_raw
        )
        self._db_file_raw = location.get("dbFile")
        self._connection = None  # lazy

    # ------------------------------------------------------------------
    # EngineDriver surface
    # ------------------------------------------------------------------

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="local",
            format=str((self.expose.get("binding") or {}).get("format") or "other"),
            table_reference=self._table,
            dialect="duckdb",
            capabilities={
                "describe": True,
                "sample": True,
                "query": True,
                "query_sql": True,
                "lineage": False,
                "quality": False,
            },
        )

    def execute(
        self,
        *,
        sql: str,
        params: Sequence[Any] = (),
        timeout_seconds: Optional[float] = None,
    ) -> QueryResult:
        connection = self._get_connection()
        # DuckDB's Python API uses positional params with `?`. Our
        # compiler emits named ``:p_<index>`` placeholders, which the
        # DuckDB driver also accepts via the ``execute(sql, dict)``
        # path. Passing a dict keeps the placeholder→value mapping
        # explicit and leaves no room for off-by-one mistakes.
        bound: Dict[str, Any] = {f"p_{index}": value for index, value in enumerate(params)}
        rendered = sql
        # ``:p_<index>`` is DuckDB-native; no rewrite needed. The
        # compiler validates every interpolated identifier and
        # expression; ``guard_against_injection_markers`` is the
        # shared defence-in-depth pass that catches a regression
        # between compiler and cursor.
        guard_against_injection_markers(rendered)
        cursor = connection.cursor()
        try:
            if bound:
                cursor.execute(rendered, bound)
            else:
                cursor.execute(rendered)
            description = cursor.description or []
            columns = tuple(str(col[0]) for col in description)
            raw_rows = cursor.fetchall()
        finally:
            cursor.close()
        rows = tuple(dict(zip(columns, row, strict=False)) for row in raw_rows)
        return QueryResult(columns=columns, rows=rows)

    def health_check(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            connection = self._get_connection()
            connection.execute("SELECT 1").fetchall()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "detail": str(exc),
                "engine": "duckdb",
            }
        return {
            "status": "ok",
            "detail": "duckdb-ok",
            "engine": "duckdb",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_connection(self):
        if self._connection is not None:
            return self._connection
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise UnsupportedBindingError(
                "duckdb is not installed; install via the 'local' extra: "
                "pip install 'data-product-forge[local]'"
            ) from exc
        target = self._db_file_raw if isinstance(self._db_file_raw, str) else ":memory:"
        # DuckDB read-only mode protects against accidental writes
        # even though every advertised tool is SELECT-only. Skip
        # read-only when the target is in-memory because a fresh
        # in-memory instance is empty and would refuse the table-load
        # below.
        if target == ":memory:":
            connection = duckdb.connect(database=":memory:")
        else:
            connection = duckdb.connect(database=target, read_only=True)
        self._configure_connection(connection)
        self._connection = connection
        return connection

    def _configure_connection(self, connection) -> None:
        """Wire the configured CSV/parquet/json file or attach list
        into the connection so the bound table reference resolves.

        The simplest world-class approach: if a path is configured,
        define a SQL VIEW with the same name as the table that
        SELECTs from the file. ``read_csv_auto``, ``read_parquet`` and
        ``read_json_auto`` are DuckDB's safe entry points. Quoting is
        strict: every interpolated identifier is allowlist-validated.
        """
        if self._path is None and not self._attach:
            return
        if self._path is not None:
            extension = self._path.suffix.lower()
            if extension == ".csv":
                read_fn = "read_csv_auto"
            elif extension == ".parquet":
                read_fn = "read_parquet"
            elif extension in {".json", ".ndjson"}:
                read_fn = "read_json_auto"
            else:
                raise UnsupportedBindingError(
                    f"DuckDB driver does not auto-load files with extension {extension!r}; "
                    "supported: .csv, .parquet, .json"
                )
            literal = self._path.as_posix().replace("'", "''")
            connection.execute(
                f"CREATE OR REPLACE VIEW {self._table} AS " f"SELECT * FROM {read_fn}('{literal}')"
            )
        # Future: handle ``attach`` paths once the schema for them
        # solidifies; today they are accepted but ignored to keep the
        # driver narrow.

    @staticmethod
    def _resolve_attach_path(item: Any, readable_paths: Tuple[Path, ...]) -> Path:
        if not isinstance(item, str) or not item:
            raise UnsupportedBindingError("attach entries must be non-empty strings")
        candidate = Path(item).expanduser().resolve()
        if readable_paths and not _path_is_under(candidate, readable_paths):
            raise UnsupportedBindingError(f"attach path {candidate} is outside --readable-paths")
        return candidate


def _path_is_under(target: Path, roots: Tuple[Path, ...]) -> bool:
    if not roots:
        return True
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False
