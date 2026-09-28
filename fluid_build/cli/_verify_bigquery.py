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

"""``fluid verify``'s data dimensions for a BigQuery table: rows, and masking.

The BigQuery verifier used to read only the table's metadata: its schema, and
the dataset's location. A table that held no rows, or that held a cleartext
``msisdn`` where the contract says it lands hashed, passed ``--strict``. These
are the two dimensions the Glue + Athena verifier already reports
(``_verify_athena``), by the same rules, from one GoogleSQL query:

* **row_count.** ``SELECT COUNT(*)`` of the table, held to the run records of
  the build that lands the expose (an acquisition build, or an embedded-SQL
  build whose load records its run the same way), by the mode the run recorded
  (equal for ``full_refresh``, at least the sum since the last full load for
  ``incremental_append``, reported for anything else). A run counts as this
  table's when its ``facets.bigquery_load`` names the table: the load the
  duckdb runner performs records it, with the rows the load job (or, on an
  emulator, the count after it) says arrived. A run that succeeded without a
  BigQuery load landed somewhere else (a local or aws run from the same
  contract directory) and is passed over; a failed one may have been this
  table's, and ends the comparison. An empty table fails, except for a
  reference-only contract with no run of its own. ``metadata.num_rows`` is
  not the count: it leaves out the streaming buffer, and the goccy emulator
  leaves it unset.
* **masking.** For each ``policy.privacy.masking`` rule, the non-null values
  of the column that do not have the strategy's shape, counted in the same
  query (``_verify_masking.bigquery_plan``); one is CRITICAL. Only counts come
  back.

A query that cannot run is an error, which ``fluid verify`` fails on: the
dimensions are unproven, not passed. The query needs ``bigquery.jobs.create``
(``roles/bigquery.jobUser``) on the project as well as read access to the table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.verify.bigquery")

#: How long the count query may run before verify reports it as an error.
QUERY_TIMEOUT_SECONDS = 300.0


def _loaded_into(record: Optional[Mapping[str, Any]], table_id: str) -> Optional[bool]:
    """Whether a run loaded ``table_id``: yes, no, or unknown (``None``)."""
    if record is None:
        return None
    facets = record.get("facets")
    load = facets.get("bigquery_load") if isinstance(facets, Mapping) else None
    if isinstance(load, Mapping) and load.get("table"):
        return str(load["table"]).lower() == table_id.lower()
    if str(record.get("state") or "").lower() == "succeeded":
        # A BigQuery-bound run only succeeds after its load, which it records.
        return False
    return None


def _count_query(
    client: Any,
    bigquery: Any,
    table_id: str,
    selects: List[str],
    params: List[Tuple[str, str]],
    location: Optional[str],
) -> Tuple[int, List[int], Dict[str, Any]]:
    """``(rows, the value of each extra select, query facts)``."""
    from fluid_build.build_runners._bigquery_load import quote_table_id

    selected = ", ".join(["COUNT(*)", *selects])
    sql = f"SELECT {selected} FROM {quote_table_id(table_id)}"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(n, "STRING", v) for n, v in params]
    )
    job = client.query(sql, job_config=job_config, location=location)
    rows = list(job.result(timeout=QUERY_TIMEOUT_SECONDS))
    try:
        cells = list(rows[0].values())
        # COUNT(*) always has a value; a count over no rows may come back NULL
        # from an engine that sums (DuckDB's count_if does), which is zero.
        values = [int(cells[0])] + [int(v or 0) for v in cells[1:]]
    except (IndexError, TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"the count query on {table_id} returned no readable count") from exc
    if len(values) != 1 + len(selects):
        raise RuntimeError(f"the count query on {table_id} returned {len(values)} value(s)")
    return values[0], values[1:], {"sql": sql, "job_id": getattr(job, "job_id", None)}


def _escalate(
    severity: Dict[str, Any], problems: List[Tuple[str, str]], row_count: Mapping[str, Any]
) -> Dict[str, Any]:
    """The schema severity, raised to CRITICAL by a bad count or untreated values.

    As ``_verify_athena._severity``: an empty table of a reference-only
    contract is INFO, reported and never gated.
    """
    if not problems:
        if row_count.get("status") == "info" and severity.get("level") == "SUCCESS":
            return {
                "level": "INFO",
                "impact": "LOW",
                "symbol": "🔵",
                "remediation": "NONE",
                "reason": row_count.get("message"),
                "actions": ["Run the pipeline that owns the table, then verify again"],
            }
        return severity
    already = severity.get("level") == "CRITICAL"
    return {
        "level": "CRITICAL",
        "impact": "HIGH",
        "symbol": "🔴",
        "remediation": "MANUAL_INTERVENTION_REQUIRED",
        "reason": "; ".join(
            ([severity.get("reason", "")] if already else []) + [p[0] for p in problems]
        ),
        "actions": (list(severity.get("actions") or []) if already else [])
        + [p[1] for p in problems],
    }


def add_data_dimensions(
    result: Dict[str, Any],
    *,
    client: Any,
    bigquery: Any,
    bq_table: Any,
    expose: Mapping[str, Any],
    contract: Mapping[str, Any],
    workdir: Path,
    reference_only: bool = False,
    location: Optional[str] = None,
) -> Dict[str, Any]:
    """``result`` (``verify_bigquery_table``'s) with ``row_count`` and ``masking`` added.

    Mutates and returns ``result``: its dimensions, severity, status and
    ``metadata.num_rows`` (the counted rows; the table's own figure is kept as
    ``metadata.table_num_rows``). A count that cannot be taken turns the result
    into an error.
    """
    from fluid_build.cli._verify_athena import _landed_rows, _row_count_dimension
    from fluid_build.cli._verify_masking import bigquery_plan, severity_problem

    table_id = str(result["table_id"])
    expose_id = str(expose.get("exposeId") or expose.get("id") or "")
    columns = [
        str(f.name) for f in getattr(bq_table, "schema", None) or [] if getattr(f, "name", None)
    ]
    selects, params, finish_masking = bigquery_plan(expose, columns)
    try:
        count, masking_values, query = _count_query(
            client, bigquery, table_id, selects, params, location
        )
    except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
        LOG.warning("verify_bigquery_count_failed table=%s error=%s", table_id, type(exc).__name__)
        result["status"] = "error"
        result["error"] = f"BigQuery could not count {table_id}: {exc}"
        return result
    try:
        landed, info = _landed_rows(
            contract,
            expose_id,
            workdir,
            table_id,
            wrote_into=_loaded_into,
            embedded_sql_records=True,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable record is not a count
        LOG.warning("verify_bigquery_run_record_unreadable error=%s", type(exc).__name__)
        landed, info = None, {"source": "none", "note": "the run record could not be read"}
    row_count = _row_count_dimension(
        count, landed, info, table_id, reference_only=reference_only, engine="BigQuery"
    )
    dimensions = result.setdefault("dimensions", {})
    dimensions["row_count"] = row_count
    masking = finish_masking(masking_values)
    if masking is not None:
        dimensions["masking"] = masking

    problems: List[Tuple[str, str]] = []
    if row_count["status"] == "fail":
        problems.append(
            (row_count["message"], "Re-run the build and check its run record, then verify again")
        )
    masking_problem = severity_problem(masking)
    if masking_problem is not None:
        problems.append(masking_problem)
    result["severity"] = _escalate(dict(result.get("severity") or {}), problems, row_count)
    if problems:
        result["status"] = "mismatch"
    metadata = result.setdefault("metadata", {})
    metadata["table_num_rows"] = metadata.get("num_rows")
    metadata["num_rows"] = count
    metadata["row_count_detail"] = row_count["message"]
    result["bigquery"] = query
    return result


__all__ = ["QUERY_TIMEOUT_SECONDS", "add_data_dimensions"]
