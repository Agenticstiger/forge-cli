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

"""AWS Athena driver for the consumer MCP output-port server.

Athena is the canonical AWS serverless query engine over S3 / Glue
catalog data. The driver mirrors the Snowflake / BigQuery shape
(``descriptor`` + ``execute`` + ``health_check``) so the gateway
treats it as a peer engine.

Unlike row-by-row OLTP engines, Athena is a query-execution model:
``StartQueryExecution`` returns a queryExecutionId, then we poll
``GetQueryExecution`` until the state is ``SUCCEEDED`` (or
``FAILED`` / ``CANCELLED``), then page through ``GetQueryResults``.

Phase-1 identity model:

* boto3 default credential chain (env vars, ~/.aws/credentials, IAM
  role, OIDC). Per the AWS recommendation, the gateway does NOT
  bake in long-lived AWS access keys — use OIDC or instance roles.

* Two binding-time fields are required: ``database`` (Glue catalog
  database) and ``table`` (Glue table inside that database). The
  optional ``workgroup`` and ``output_location`` knobs come from
  the binding or env vars.

Borrowed-not-built per /borrow-before-build:

* `boto3 <https://boto3.amazonaws.com>`_ is the AWS SDK; its
  Athena client handles the request signing, retry, and pagination
  we'd otherwise hand-maintain. Pinned via the optional ``aws``
  extra. We do NOT borrow PyAthena (community wrapper) — it adds a
  PEP 249 cursor abstraction that doesn't pay back when our query
  shape is fixed (``StartQueryExecution`` → poll → page).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident

from .base import (
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    UnsupportedBindingError,
    get_binding,
    guard_against_injection_markers,
)

# Athena uses ``?`` positional placeholders in prepared statements,
# but our compiler emits named ``:p_<index>``. Athena supports
# server-side query parameters via ``ExecuteParameters`` on
# ``StartQueryExecution`` — we pass the bound values as a list
# matching the order of ``?`` placeholders we substitute in for
# ``:p_<index>`` (preserving the index).
_PARAM_REWRITE = re.compile(r":p_(\d+)\b")

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_POLL_TIMEOUT_SECONDS = 60.0


class AthenaDriver(EngineDriver):
    """AWS Athena driver.

    Requires the ``aws`` extra:
    ``pip install 'data-product-forge[aws]'``.
    """

    name = "athena"

    def __init__(
        self,
        *,
        expose: Mapping[str, Any],
        contract: Mapping[str, Any],
        logger: Optional[logging.Logger] = None,
        connection_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            expose=expose,
            contract=contract,
            logger=logger,
            connection_options=connection_options,
        )
        platform, fmt, location = get_binding(expose)
        if platform != "aws":
            raise UnsupportedBindingError(
                f"AthenaDriver requires binding.platform='aws'; got {platform!r}"
            )
        if fmt not in {"athena_table", "glue_table"}:
            raise UnsupportedBindingError(
                "AthenaDriver requires binding.format in {'athena_table','glue_table'}; "
                f"got {fmt!r}"
            )
        database = location.get("database") or location.get("dataset")
        table = location.get("table") or location.get("name")
        for component, label in [
            (database, "binding.location.database"),
            (table, "binding.location.table"),
        ]:
            if not isinstance(component, str) or not component:
                raise UnsupportedBindingError(f"AthenaDriver requires {label}")
        self._database = validate_ident(str(database))
        self._table = validate_ident(str(table))
        self._workgroup = (
            location.get("workgroup") or os.environ.get("ATHENA_WORKGROUP") or "primary"
        )
        self._output_location = (
            location.get("outputLocation")
            or location.get("output_location")
            or os.environ.get("ATHENA_OUTPUT_LOCATION")
        )
        self._region = (
            location.get("region")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        self._client = None  # lazy

    # ------------------------------------------------------------------
    # EngineDriver surface
    # ------------------------------------------------------------------

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="aws",
            format="athena_table",
            table_reference=f'"{self._database}"."{self._table}"',
            dialect="athena",
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
        client = self._get_client()
        # Substitute ``:p_<index>`` → ``?`` while preserving order.
        ordered_indices: List[int] = []
        rendered = _PARAM_REWRITE.sub(
            lambda m: (ordered_indices.append(int(m.group(1))) or "?"),
            sql,
        )
        guard_against_injection_markers(rendered)

        # Athena requires string-form parameter values. Numeric / date
        # values get passed through ``str()`` because Athena's
        # parameterised query path infers types from the SQL context.
        execution_params = [str(params[i]) for i in ordered_indices] if ordered_indices else None

        start_kwargs: Dict[str, Any] = {
            "QueryString": rendered,
            "QueryExecutionContext": {"Database": self._database},
            "WorkGroup": self._workgroup,
        }
        if self._output_location:
            start_kwargs["ResultConfiguration"] = {"OutputLocation": self._output_location}
        if execution_params:
            start_kwargs["ExecutionParameters"] = execution_params

        response = client.start_query_execution(**start_kwargs)
        query_id = response["QueryExecutionId"]
        deadline = time.monotonic() + (timeout_seconds or DEFAULT_MAX_POLL_TIMEOUT_SECONDS)
        state = "QUEUED"
        while time.monotonic() < deadline:
            status = client.get_query_execution(QueryExecutionId=query_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)
        else:
            client.stop_query_execution(QueryExecutionId=query_id)
            raise TimeoutError(
                f"Athena query {query_id} exceeded "
                f"{timeout_seconds or DEFAULT_MAX_POLL_TIMEOUT_SECONDS}s"
            )

        if state != "SUCCEEDED":
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {query_id} {state}: {reason}")

        # Page through results. Athena returns the schema header in
        # the first row of the first page; we drop it.
        columns: Tuple[str, ...] = ()
        rows: List[Dict[str, Any]] = []
        next_token = None
        first_page = True
        while True:
            kwargs = {"QueryExecutionId": query_id}
            if next_token:
                kwargs["NextToken"] = next_token
            page = client.get_query_results(**kwargs)
            data_rows = page["ResultSet"]["Rows"]
            if first_page and data_rows:
                # Header row carries column metadata.
                metadata = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
                columns = tuple(col["Name"] for col in metadata)
                data_rows = data_rows[1:]
                first_page = False
            for row in data_rows:
                values = [cell.get("VarCharValue") for cell in row.get("Data", [])]
                rows.append(dict(zip(columns, values, strict=False)))
            next_token = page.get("NextToken")
            if not next_token:
                break

        return QueryResult(columns=columns, rows=tuple(rows))

    def health_check(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            client = self._get_client()
            # Cheapest possible probe — list workgroups (1 call,
            # tiny payload, exercises auth + region).
            client.list_work_groups(MaxResults=1)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "detail": str(exc),
                "engine": "athena",
            }
        return {
            "status": "ok",
            "detail": "athena-ok",
            "engine": "athena",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "workgroup": self._workgroup,
            "region": self._region,
        }

    def close(self) -> None:
        """boto3 clients hold no persistent connection; provided for
        symmetry with the OLTP drivers."""
        self._client = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dep
            raise UnsupportedBindingError(
                "boto3 is required for AthenaDriver. Install with: "
                "pip install 'data-product-forge[aws]'"
            ) from exc
        client_kwargs: Dict[str, Any] = {"region_name": self._region}
        client_kwargs.update(dict(self.connection_options or {}))
        self._client = boto3.client("athena", **client_kwargs)
        return self._client
