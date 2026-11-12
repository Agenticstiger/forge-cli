# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Rows from a Databricks SQL warehouse, for ``fluid test``.

Statements run through ``iac/providers/databricks_enforce.SqlRunner``, the Statement
Execution API runner ``fluid apply`` already uses for masks and row filters: it polls a
long statement, cancels one that times out or is interrupted, and scrubs every error.
This module adds what reading results needs: ``:name`` parameters, and the rows of an
``INLINE`` ``JSON_ARRAY`` result (the API's defaults), converted by each column's
``type_name``, since every value arrives as a string, across every chunk of the result.

The client is ``databricks_enforce.default_client()``, ``WorkspaceClient`` built from
``databricks_auth.sdk_config`` (the policy ``fluid apply`` and ``fluid verify`` share);
the SDK is the ``[databricks]`` extra, imported only when a client is made.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from fluid_build.iac.providers.databricks_enforce import (
    DATABRICKS_EXTRA_INSTALL,
    WAREHOUSE_ENV,
    SqlRunner,
    default_client,
    scrub,
)
from fluid_build.iac.providers.databricks_enforce import StatementFailed as StatementError

STATEMENTS_PATH = "/api/2.0/sql/statements"

#: Converters for the ``type_name`` of a ``JSON_ARRAY`` result column.
_INTEGER_TYPES = frozenset({"BYTE", "SHORT", "INT", "LONG"})
_FLOAT_TYPES = frozenset({"FLOAT", "DOUBLE", "DECIMAL"})

#: A guard against a result that never stops naming a next chunk.
_MAX_CHUNKS = 10_000


def warehouse_id_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """The SQL warehouse id from ``DATABRICKS_WAREHOUSE_ID``, or a fix-it error."""
    value = str((env if env is not None else os.environ).get(WAREHOUSE_ENV) or "").strip()
    if not value:
        raise StatementError(
            f"{WAREHOUSE_ENV} is not set, so there is no SQL warehouse to run the checks on. "
            "Set it to the id of a SQL warehouse you can use (SQL Warehouses > the "
            "warehouse > Connection details)."
        )
    return value


def param(name: str, value: Any, type_name: str = "STRING") -> Dict[str, Any]:
    """A ``parameters`` entry binding ``value`` to ``:name`` as ``type_name``.

    ``None`` binds NULL (the API reads an omitted value as NULL).
    """
    entry: Dict[str, Any] = {"name": name, "type": type_name}
    if value is not None:
        entry["value"] = str(value)
    return entry


def _convert(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    try:
        if type_name in _INTEGER_TYPES:
            return int(value)
        if type_name in _FLOAT_TYPES:
            return float(value)
        if type_name == "BOOLEAN":
            return str(value).lower() == "true"
    except (TypeError, ValueError):
        return value
    return value


class StatementRunner:
    """Run statements on one warehouse and return their rows as tuples."""

    def __init__(
        self,
        client: Any,
        warehouse_id: str,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._sql = SqlRunner(
            client, warehouse_id, timeout=timeout_seconds, poll=poll_seconds, sleep=sleep
        )
        self._unreachable: Optional[str] = None

    def run(
        self, sql: str, parameters: Optional[Sequence[Mapping[str, Any]]] = None
    ) -> List[tuple]:
        """Run ``sql`` (``:name`` markers bound from ``parameters``) and return its rows.

        A statement that failed because the warehouse did not answer (a timeout, a
        transport error, a cancelled statement), rather than because of its SQL, stops
        the ones after it: each would wait out the same timeout.
        """
        if self._unreachable:
            raise StatementError(f"not run: {self._unreachable}")
        try:
            doc = self._sql.execute(sql, parameters)
        except StatementError as exc:
            if not str(exc).startswith("FAILED"):
                self._unreachable = f"an earlier statement did not complete ({exc})"
            raise
        columns = ((doc.get("manifest") or {}).get("schema") or {}).get("columns") or []
        types = [str(c.get("type_name") or "").upper() for c in columns]
        statement_id = str(doc.get("statement_id") or "")
        result = doc.get("result") or {}
        rows: List[tuple] = []
        for _ in range(_MAX_CHUNKS):
            for raw in result.get("data_array") or []:
                rows.append(
                    tuple(
                        _convert(v, types[i] if i < len(types) else "") for i, v in enumerate(raw)
                    )
                )
            index = result.get("next_chunk_index")
            if index is None or not statement_id:
                break
            result = self._chunk(statement_id, int(index))
        return rows

    def _chunk(self, statement_id: str, index: int) -> Mapping[str, Any]:
        path = f"{STATEMENTS_PATH}/{quote(statement_id, safe='')}/result/chunks/{index}"
        try:
            out = self._client.api_client.do("GET", path)
        except Exception as exc:  # noqa: BLE001 - every transport failure is the statement's
            raise StatementError(f"{type(exc).__name__}: {scrub(exc)}") from None
        return out if isinstance(out, dict) else {}


__all__ = [
    "DATABRICKS_EXTRA_INSTALL",
    "STATEMENTS_PATH",
    "StatementError",
    "StatementRunner",
    "WAREHOUSE_ENV",
    "default_client",
    "param",
    "scrub",
    "warehouse_id_from_env",
]
