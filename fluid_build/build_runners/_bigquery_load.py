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


class BigQueryLoadError(RuntimeError):
    """The landed file could not be loaded, or loaded the wrong number of rows."""


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
    the declared schema with the file's.
    """
    reason = unsupported_reason(mode, sink_format, 1)
    if reason:
        raise BigQueryLoadError(reason)
    bigquery = _bigquery_module()
    client = bigquery.Client(project=target["project"])
    table_id = f"{client.project}.{target['dataset']}.{target['table']}"
    # Never create: the table is the IaC's, and a load that made its own would
    # carry the file's schema, not the contract's.
    table = client.get_table(table_id)
    job_config = bigquery.LoadJobConfig(
        source_format=_SOURCE_FORMAT[sink_format],
        write_disposition=_WRITE_DISPOSITION[mode],
        create_disposition="CREATE_NEVER",
        schema=table.schema,
    )
    with open(path, "rb") as fh:
        job = client.load_table_from_file(
            fh, table_id, job_config=job_config, location=target["location"]
        )
    job.result()
    loaded = int(job.output_rows or 0)
    if loaded != expected_rows:
        raise BigQueryLoadError(
            f"loaded {loaded} rows into {table_id}, but the landed file holds {expected_rows}"
        )
    logger.info("bigquery.load table=%s rows=%d job=%s", table_id, loaded, job.job_id)
    return {"table": table_id, "rows": loaded, "job_id": job.job_id}
