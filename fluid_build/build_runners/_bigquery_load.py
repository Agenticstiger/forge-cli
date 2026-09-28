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

"""Load a landed file into the BigQuery table the IaC declared.

A BigQuery binding is not a file destination. The build used to treat its
``location.path`` as one and COPY Parquet to ``gs://``, which needs HMAC keys
DuckDB cannot get from Application Default Credentials, and even when the write
succeeded nothing moved the rows into the table ``tofu apply`` had created. Now
the build lands the file locally and one load job moves it into that table.

The shape follows dlt's BigQuery destination (dlt-hub/dlt,
``destinations/impl/bigquery``): ``load_table_from_file`` for a local file,
``source_format=PARQUET``, no autodetect. Unlike dlt it passes the table's own
schema, because here the table already exists and the contract owns it.
Authentication is the client's own ``google.auth.default()``, so gcloud ADC, a
VM's service account and Workload Identity Federation all work with no key.

``google.cloud.bigquery`` is imported only when a load runs, so installs
without the ``gcp`` extra are unaffected until they bind a BigQuery table.

**Timestamps.** DuckDB writes a ``TIMESTAMP`` column as a Parquet timestamp
with ``isAdjustedToUTC=false``, which BigQuery reads as ``DATETIME``, not
``TIMESTAMP``. Before the load, every column the table declares ``TIMESTAMP``
that the file holds as a naive timestamp is rewritten as ``TIMESTAMPTZ`` under
``TimeZone='UTC'``, so the Parquet column is UTC-adjusted and loads as the
table's ``TIMESTAMP``: the mapping python-bigquery itself uses
(``_pyarrow_helpers``: ``TIMESTAMP`` is ``pyarrow.timestamp("us", tz="UTC")``,
``DATETIME`` the naive one). The wall-clock value is read as UTC, which is how
BigQuery reads a zone-less timestamp literal too.

**Emulators.** With ``BIGQUERY_EMULATOR_HOST`` set, python-bigquery already
sends every request to that host, but it still resolves Application Default
Credentials first: with none it raises, and with some it sends a real OAuth
token to a local emulator. :func:`bigquery_client` passes
``AnonymousCredentials`` instead, the way goccy/bigquery-emulator documents
its clients. The goccy emulator also reports no ``outputRows`` for a load
job, so a load whose job reports no count is checked by counting the table
afterwards; it is never taken as zero rows loaded, nor as success.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

# Write disposition per source mode. The other modes (merge, dedup, CDC) need a
# MERGE this path does not do, and are refused rather than approximated.
_WRITE_DISPOSITION = {"full_refresh": "WRITE_TRUNCATE", "incremental_append": "WRITE_APPEND"}
_SOURCE_FORMAT = {"parquet": "PARQUET"}

# The project order ``terraform-provider-google`` reads, so the load goes to the
# project ``tofu apply`` created the table in when the binding names none.
_PROJECT_ENV = ("GOOGLE_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT")


_MISSING_EXTRA = "loading into BigQuery needs the gcp extra: pip install 'data-product-forge[gcp]'"


#: The variable python-bigquery reads for an emulator endpoint
#: (``google.cloud.bigquery._helpers.BIGQUERY_EMULATOR_HOST``).
EMULATOR_HOST_ENV = "BIGQUERY_EMULATOR_HOST"

#: DuckDB's names for a timestamp without a time zone, at every precision.
_NAIVE_TIMESTAMPS = frozenset({"TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"})


class BigQueryLoadError(RuntimeError):
    """The landed file could not be loaded, or loaded the wrong number of rows."""


def emulator_host() -> Optional[str]:
    """The BigQuery emulator endpoint this process is pointed at, if any."""
    host = os.environ.get(EMULATOR_HOST_ENV, "").strip()
    return host or None


def bigquery_client(bigquery: Any, project: Optional[str]) -> Any:
    """A ``bigquery.Client`` for ``project``: ADC, or anonymous on an emulator.

    Without an emulator this is exactly ``bigquery.Client(project=project)``,
    so gcloud ADC, a VM's service account and Workload Identity Federation
    (an ``external_account`` credential file) resolve as they always did. With
    ``BIGQUERY_EMULATOR_HOST`` set, the client already talks to that host
    (python-bigquery reads the variable itself) and gets no credentials to
    send there: goccy/bigquery-emulator's documented client setup.
    """
    if emulator_host() is None:
        return bigquery.Client(project=project)
    return bigquery.Client(project=project, credentials=_anonymous_credentials())


def _anonymous_credentials() -> Any:
    """``google.auth``'s ``AnonymousCredentials``, imported on first use. Tests replace this,
    as they replace :func:`_bigquery_module`, so the unit lanes need no Google library."""
    from google.auth.credentials import AnonymousCredentials

    return AnonymousCredentials()


def _bigquery_module() -> Any:
    """``google.cloud.bigquery``, imported on first use. Tests replace this."""
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise BigQueryLoadError(_MISSING_EXTRA) from exc
    return bigquery


def bigquery_load_target(
    binding: Mapping[str, Any], expose: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """The table a binding loads into, or None when it is not a BigQuery table.

    Resolved with the IaC's own helpers, so the load names the dataset, table
    and location ``_emit_bigquery`` created.
    """
    from ..iac.providers.gcp import BIGQUERY_TABLE, _bq_table_name, resolve_gcp_target

    if resolve_gcp_target(binding) != BIGQUERY_TABLE:
        return None
    loc = binding.get("location") or {}
    project = loc.get("project") or next(
        (os.environ[k] for k in _PROJECT_ENV if os.environ.get(k)), None
    )
    return {
        "project": project,
        "dataset": loc.get("dataset") or "default",
        "table": _bq_table_name(expose, loc),
        "location": loc.get("region") or loc.get("location") or "US",
    }


def unsupported_reason(mode: str, sink_format: str, stream_count: int) -> Optional[str]:
    """Why this build cannot be loaded into BigQuery, or None if it can.

    Checked before the build runs, so an unsupported shape fails by name
    instead of landing somewhere else and reporting success.
    """
    if stream_count != 1:
        return f"a BigQuery binding loads exactly one stream; this build has {stream_count}"
    if sink_format not in _SOURCE_FORMAT:
        return f"a BigQuery load takes {sorted(_SOURCE_FORMAT)}; the sink format is {sink_format!r}"
    if mode not in _WRITE_DISPOSITION:
        return (
            f"a BigQuery load supports modes {sorted(_WRITE_DISPOSITION)}; this build is {mode!r}"
        )
    try:
        _bigquery_module()
    except BigQueryLoadError as exc:
        return str(exc)
    return None


def _timestamp_columns(schema: Any) -> list:
    """The names of the top-level columns the table declares ``TIMESTAMP``."""
    names = []
    for field in schema or []:
        field_type = str(getattr(field, "field_type", "") or "").upper()
        name = getattr(field, "name", None)
        if name and field_type == "TIMESTAMP":
            names.append(str(name))
    return names


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def utc_adjusted_copy(path: str, schema: Any) -> Optional[str]:
    """A copy of the Parquet file at ``path`` whose ``TIMESTAMP`` columns are UTC-adjusted.

    ``schema`` is the destination table's. ``None`` when no column needs it:
    the table declares no ``TIMESTAMP`` column, or the file already holds each
    one as ``TIMESTAMPTZ``. The copy sits beside ``path`` and the caller
    removes it after the load; ``path`` itself (the build's landed file, which
    the run record names) is left as it is.
    """
    wanted = {name.lower(): name for name in _timestamp_columns(schema)}
    if not wanted:
        return None
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        # Read as UTC: the cast below turns the naive wall clock into an instant.
        con.execute("SET TimeZone = 'UTC'")
        described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
        naive = [
            str(row[0])
            for row in described
            if str(row[0]).lower() in wanted and str(row[1]).upper() in _NAIVE_TIMESTAMPS
        ]
        if not naive:
            return None
        replaced = ", ".join(
            f"CAST({_quote_ident(c)} AS TIMESTAMPTZ) AS {_quote_ident(c)}" for c in naive
        )
        base, _ext = os.path.splitext(str(path))
        out = f"{base}.bq-load.parquet"
        # A path is not a bindable parameter in COPY ... TO; quote it as a literal.
        target = "'" + out.replace("'", "''") + "'"
        con.execute(
            f"COPY (SELECT * REPLACE ({replaced}) FROM read_parquet(?)) "
            f"TO {target} (FORMAT parquet)",
            [str(path)],
        )
        return out
    finally:
        con.close()


def quote_bq_ident(name: str) -> str:
    """``name`` as a GoogleSQL quoted identifier.

    Backtick-quoted identifiers take the string-literal escapes (GoogleSQL
    lexical structure, "Quoted identifiers"), so a backslash and a backtick
    are escaped and nothing in ``name`` can close the quote.
    """
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def quote_table_id(table_id: str) -> str:
    """``project.dataset.table`` as three quoted GoogleSQL identifiers."""
    parts = str(table_id).rsplit(".", 2)
    if len(parts) != 3 or not all(parts):
        raise BigQueryLoadError(f"{table_id!r} is not a project.dataset.table id")
    return ".".join(quote_bq_ident(part) for part in parts)


def _count_table(client: Any, table_id: str, location: Optional[str]) -> int:
    """``COUNT(*)`` of ``table_id``, read back through a query."""
    sql = f"SELECT COUNT(*) FROM {quote_table_id(table_id)}"
    rows = list(client.query(sql, location=location).result())
    try:
        return int(list(rows[0].values())[0])
    except (IndexError, TypeError, ValueError) as exc:
        raise BigQueryLoadError(f"counting {table_id} returned no readable count") from exc


def load_file(
    path: str,
    target: Mapping[str, Any],
    *,
    mode: str,
    sink_format: str,
    expected_rows: int,
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Load ``path`` into ``target`` and check the row count. Returns the job facts.

    The table's own schema is passed to the job. Without it, a Parquet file
    whose columns are all optional cannot load into REQUIRED columns
    (googleapis/python-bigquery#2373), and ``WRITE_TRUNCATE`` would replace
    the declared schema with the file's. A ``TIMESTAMP`` column the file holds
    as a naive timestamp is loaded from a UTC-adjusted copy
    (:func:`utc_adjusted_copy`).

    The count is the job's ``outputRows``. A job that reports none (the goccy
    emulator never does) is checked by counting the table: for
    ``WRITE_TRUNCATE`` the table must then hold exactly the file's rows, and
    for ``WRITE_APPEND`` it must have grown by them, which needs the count
    before the load; that is taken on an emulator only, and a real job with no
    count and no earlier count is refused rather than assumed.
    """
    reason = unsupported_reason(mode, sink_format, 1)
    if reason:
        raise BigQueryLoadError(reason)
    bigquery = _bigquery_module()
    client = bigquery_client(bigquery, target["project"])
    table_id = f"{client.project}.{target['dataset']}.{target['table']}"
    # Never create: the table is the IaC's, and a load that made its own would
    # carry the file's schema, not the contract's.
    table = client.get_table(table_id)
    appending = _WRITE_DISPOSITION[mode] == "WRITE_APPEND"
    before: Optional[int] = None
    if appending and emulator_host() is not None:
        before = _count_table(client, table_id, target["location"])
    job_config = bigquery.LoadJobConfig(
        source_format=_SOURCE_FORMAT[sink_format],
        write_disposition=_WRITE_DISPOSITION[mode],
        create_disposition="CREATE_NEVER",
        schema=table.schema,
    )
    upload = utc_adjusted_copy(path, table.schema) if sink_format == "parquet" else None
    try:
        with open(upload or path, "rb") as fh:
            job = client.load_table_from_file(
                fh, table_id, job_config=job_config, location=target["location"]
            )
        job.result()
    finally:
        if upload is not None:
            try:
                os.remove(upload)
            except OSError as exc:  # the load's outcome stands; the copy is reported
                logger.warning("bigquery.load copy_not_removed path=%s error=%s", upload, exc)
    rows_from = "output_rows"
    if job.output_rows is not None:
        loaded = int(job.output_rows)
    elif not appending:
        loaded = _count_table(client, table_id, target["location"])
        rows_from = "count_after_load"
    elif before is not None:
        loaded = _count_table(client, table_id, target["location"]) - before
        rows_from = "count_after_load"
    else:
        raise BigQueryLoadError(
            f"the load job into {table_id} reported no output row count, and without the "
            "table's count before an append the rows it added cannot be checked"
        )
    if loaded != expected_rows:
        raise BigQueryLoadError(
            f"loaded {loaded} rows into {table_id}, but the landed file holds {expected_rows}"
        )
    logger.info(
        "bigquery.load table=%s rows=%d rows_from=%s job=%s",
        table_id,
        loaded,
        rows_from,
        job.job_id,
    )
    return {"table": table_id, "rows": loaded, "rows_from": rows_from, "job_id": job.job_id}
