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

"""Read a BigQuery table into a local Parquet file an embedded-SQL build's DuckDB reads.

An embedded-SQL build on DuckDB whose ``consumes[]`` entry resolves to a
BigQuery table (the upstream's gcp ``bigquery_table`` binding) cannot read it
the way it reads an S3 prefix: DuckDB has no BigQuery reader of its own, and
``gs://`` is not where the table lives. So the table is read through the
BigQuery API, page by page, into a Parquet file under the build's
``.fluid/staging``, and the build's view over it is the ordinary local-file
view every other input gets. The SQL does not change.

Borrowed, not built:

* The read is python-bigquery's own: ``Client.list_rows(table)`` and
  ``RowIterator.to_arrow_iterable()``, one Arrow record batch per page, written
  with ``pyarrow.parquet.ParquetWriter`` as they arrive, so the whole table is
  never held in memory. This is the pattern Google documents for BigQuery to
  DuckDB (``to_arrow`` into a DuckDB relation; the MotherDuck and
  ``davidgasquez.com/duckdb-bq-storage`` write-ups use it), streamed.
* Types follow the DuckDB BigQuery extension's documented read mapping
  (hafenkran/duckdb-bigquery, ``docs/concepts/data-types.md``): a BigQuery
  ``TIMESTAMP`` reads as a DuckDB ``TIMESTAMP`` holding the UTC wall clock,
  not a ``TIMESTAMPTZ`` whose rendering would follow the runner's time zone.
  That is also what the same SQL reads on the local and aws targets, where
  the upstream lands naive timestamps, so one query means one thing on every
  target.

Why not the extension itself (``ATTACH 'project=...' AS bq (TYPE bigquery)``):
it is a native binary fetched from DuckDB's community repository at run
time (``INSTALL bigquery FROM community``), outside the pip install and its
lockfile, built for one DuckDB release line at a time while forge-cli accepts
``duckdb>=1.0``; its endpoint overrides are per-function parameters rather
than ``ATTACH`` options, so the attached catalog cannot be pointed at an
emulator in CI; and it would authenticate through google-cloud-cpp while the
load that follows (``_bigquery_load``) authenticates through google-auth, two
credential chains to configure and debug for one build. Reading with the
client the load already uses keeps one chain: gcloud ADC, a VM service account
and Workload Identity Federation (an ``external_account`` file named by
``GOOGLE_APPLICATION_CREDENTIALS``) all work with no key.

The read uses the REST ``tabledata.list`` pages. The BigQuery Storage Read API
is faster for large tables, but it needs ``bigquery.readsessions.create`` and
the ``google-cloud-bigquery-storage`` package, neither of which the ``gcp``
extra or the documented pipeline role grants; it is a follow-up, not a silent
fallback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import _bigquery_load
from ._bigquery_load import BigQueryLoadError, bigquery_client

_MISSING_PYARROW = (
    "reading a BigQuery table into DuckDB needs pyarrow: pip install 'data-product-forge[gcp]'"
)


class BigQueryReadError(RuntimeError):
    """An upstream BigQuery table could not be read into the build."""


def missing_dependency() -> Optional[str]:
    """Why a BigQuery read cannot run here, or ``None`` when it can."""
    try:
        # Through the module, so a test that replaces ``_bigquery_module`` there
        # replaces it for the read as well as the load.
        _bigquery_load._bigquery_module()
    except BigQueryLoadError as exc:
        return str(exc)
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return _MISSING_PYARROW
    return None


def _naive_schema(schema: Any) -> Any:
    """``schema`` with each zoned timestamp as a naive one of the same unit.

    Arrow stores a timestamp as an offset from the epoch in UTC whatever its
    ``tz``, so a cast that drops the zone keeps the value, which then reads as
    the UTC wall clock.
    """
    import pyarrow as pa

    return pa.schema(
        [
            (
                f.with_type(pa.timestamp(f.type.unit))
                if pa.types.is_timestamp(f.type) and f.type.tz is not None
                else f
            )
            for f in schema
        ],
        metadata=schema.metadata,
    )


def stage_table(table_id: str, dest: Path, *, logger: logging.Logger) -> Dict[str, Any]:
    """Write every row of BigQuery ``table_id`` to the Parquet file ``dest``.

    ``table_id`` is ``project.dataset.table``; its project may be empty, and
    the client's own (``GOOGLE_PROJECT`` and friends, else ADC's) is used.
    Returns ``{"table", "rows", "path"}``, the table as the client resolved
    it. A table that cannot be read (missing, forbidden) is a
    :class:`BigQueryReadError` naming it; an empty table stages an empty file
    with the table's columns, which the SQL then reads as zero rows.
    """
    problem = missing_dependency()
    if problem:
        raise BigQueryReadError(problem)
    import pyarrow as pa
    import pyarrow.parquet as pq

    bigquery = _bigquery_load._bigquery_module()
    project, dataset, table = table_id.rsplit(".", 2)
    client = bigquery_client(bigquery, project or None)
    resolved = f"{client.project}.{dataset}.{table}"
    try:
        bq_table = client.get_table(resolved)
    except Exception as exc:  # noqa: BLE001 - NotFound, Forbidden: all name the table
        raise BigQueryReadError(
            f"could not read BigQuery table {resolved}: {type(exc).__name__}: {exc}"
        ) from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    rows = 0
    writer = None
    try:
        for batch in client.list_rows(bq_table).to_arrow_iterable():
            schema = _naive_schema(batch.schema)
            if writer is None:
                writer = pq.ParquetWriter(str(partial), schema)
            # Table.cast, not RecordBatch.cast (pyarrow 16+): the pin is pyarrow>=14.
            writer.write_table(pa.Table.from_batches([batch]).cast(schema))
            rows += batch.num_rows
        if writer is None:
            # No page at all: an empty table. Its columns still come from BigQuery.
            empty = client.list_rows(bq_table, max_results=0).to_arrow()
            schema = _naive_schema(empty.schema)
            writer = pq.ParquetWriter(str(partial), schema)
            writer.write_table(empty.cast(schema))
        writer.close()
        writer = None
        os.replace(partial, dest)
    finally:
        if writer is not None:
            writer.close()
        if partial.exists():
            partial.unlink()
    logger.info("bigquery.read table=%s rows=%d staged=%s", resolved, rows, dest)
    return {"table": resolved, "rows": rows, "path": str(dest)}


__all__ = ["BigQueryReadError", "missing_dependency", "stage_table"]
