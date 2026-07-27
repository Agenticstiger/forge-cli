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

"""BigQuery driver for the consumer MCP output-port server.

Phase-1 identity model:

* Service-account credentials, resolved through the standard
  Google Cloud client library (``GOOGLE_APPLICATION_CREDENTIALS`` env
  var or the ADC chain). Phase-3 will add OAuth-token passthrough so
  the caller's identity is honoured by BigQuery's row-level
  security and column masks.

Quoting:

* BigQuery uses backticks for identifiers (``` `project.dataset.table` ``).
* Each component is allowlist-validated separately and only
  reassembled into the table reference after validation passes.
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


class BigQueryDriver(EngineDriver):
    """BigQuery driver.

    Requires the ``gcp`` extra: ``pip install 'data-product-forge[gcp]'``.
    """

    name = "bigquery"

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
            readable_paths=readable_paths,
        )
        platform, fmt, location = get_binding(expose)
        if platform != "gcp":
            raise UnsupportedBindingError(
                f"BigQueryDriver requires binding.platform='gcp'; got {platform!r}"
            )
        if fmt != "bigquery_table":
            raise UnsupportedBindingError(
                f"BigQueryDriver only handles binding.format='bigquery_table'; got {fmt!r}"
            )
        project_raw = location.get("project") or location.get("account")
        dataset_raw = location.get("dataset") or location.get("schema")
        table_raw = location.get("table") or location.get("name")
        if not isinstance(project_raw, str) or not project_raw:
            raise UnsupportedBindingError("BigQueryDriver requires binding.location.project")
        if not isinstance(dataset_raw, str) or not dataset_raw:
            raise UnsupportedBindingError("BigQueryDriver requires binding.location.dataset")
        if not isinstance(table_raw, str) or not table_raw:
            raise UnsupportedBindingError("BigQueryDriver requires binding.location.table")
        # Project IDs may include hyphens — BigQuery accepts them but
        # _SAFE_IDENT does not. Validate via expression allowlist
        # instead, which permits hyphens via the safe-char regex.
        validate_sql_expression_allowlist(project_raw)
        validate_ident(dataset_raw)
        validate_ident(table_raw)
        self._project = project_raw
        self._dataset = dataset_raw
        self._table = table_raw
        self._table_reference = f"`{project_raw}.{dataset_raw}.{table_raw}`"
        self._client = None  # lazy

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="gcp",
            format="bigquery_table",
            table_reference=self._table_reference,
            dialect="bigquery",
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
        try:
            from google.cloud import bigquery  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise UnsupportedBindingError(
                "google-cloud-bigquery is not installed; install via the "
                "'gcp' extra: pip install 'data-product-forge[gcp]'"
            ) from exc
        client = self._get_client(bigquery)
        rendered = sql.replace(":p_", "@p_")
        guard_against_injection_markers(rendered)
        query_parameters = [
            bigquery.ScalarQueryParameter(f"p_{index}", _bq_param_type(value), value)
            for index, value in enumerate(params)
        ]
        job_config = bigquery.QueryJobConfig(
            query_parameters=query_parameters,
            use_legacy_sql=False,
        )
        if timeout_seconds is not None:
            job_config.job_timeout_ms = int(max(1.0, timeout_seconds) * 1000)
        job = client.query(rendered, job_config=job_config)
        result = job.result()
        columns = tuple(field.name for field in result.schema)
        rows: Tuple[Dict[str, Any], ...] = tuple(
            {column: row[column] for column in columns} for row in result
        )
        return QueryResult(columns=columns, rows=rows)

    def health_check(self) -> Dict[str, Any]:
        try:
            from google.cloud import bigquery  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            return {
                "status": "unavailable",
                "detail": "google-cloud-bigquery not installed",
                "engine": "bigquery",
                "exception": str(exc),
            }
        started = time.monotonic()
        try:
            client = self._get_client(bigquery)
            client.query("SELECT 1").result()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "detail": str(exc),
                "engine": "bigquery",
            }
        return {
            "status": "ok",
            "detail": "bigquery-ok",
            "engine": "bigquery",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }

    def _get_client(self, bigquery_module):
        if self._client is not None:
            return self._client
        client = bigquery_module.Client(project=self._project)
        self._client = client
        return client


def _bq_param_type(value: Any) -> str:
    """Map a Python scalar to a BigQuery parameter-type string.

    Phase-1 supports the four types the query compiler permits as
    filter values: STRING, INT64, FLOAT64, BOOL. Other types raise so
    a typo at the contract level surfaces immediately rather than
    silently coercing.
    """
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, str):
        return "STRING"
    raise ValueError(
        f"BigQueryDriver only supports scalar filter values "
        f"(str/int/float/bool); got {type(value).__name__}"
    )
