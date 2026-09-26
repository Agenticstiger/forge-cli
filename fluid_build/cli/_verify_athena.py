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

"""``fluid verify`` for an AWS binding that lands files in S3 behind a Glue table.

``fluid apply`` provisions such a binding (``platform: aws``, a Glue-readable
``format``, ``location.{bucket, path, database, table}``) as an S3 prefix plus a
Glue catalog table whose storage descriptor points at it, and the build writes
the rows into that prefix. Stage 9 used to answer "unsupported" for it, which
the summary counts as *not checked* and ``--strict`` never fails on, so a
pipeline could land nothing, or land it where no query engine could read it,
and still go green.

Three checks, each against the live account:

1. **The catalogue.** ``glue:GetTable`` must find the table, and its columns
   (storage-descriptor columns plus partition keys) must match the contract's
   declared schema. Expected types are folded through the IaC emitter's own
   ``_hive_type``, so verify compares the table with what ``fluid apply``
   declared rather than with a second hand-kept type map (the pattern the
   BigQuery verifier follows with ``_bq_type``). The table's S3 location must
   be the binding's ``s3://<bucket>/<path>``.
2. **What the table serves.** ``SELECT COUNT(*)`` through Athena, in the
   binding's region: a table that exists in Glue is not a table anyone can
   query. The first real run of this binding landed every byte in S3 and
   Athena still refused the table with ``HIVE_UNSUPPORTED_FORMAT``. A failed,
   cancelled or timed-out query is an *error* (the check could not run), which
   ``fluid verify`` always fails on.
3. **Against the build.** The count is held to the run records of the
   acquisition build that lands this expose
   (``.fluid/runs/<product>/<build>/runs/*.json`` next to the contract, the
   records the acquisition probes read), but only to a run that says, in
   ``facets.landed``, that it landed its rows in this table's
   ``s3://<bucket>/<path>`` and that ``records_total`` is what its writes
   reported (``rows_from: write``). That contract directory serves every
   target an overlay selects, so a local run sits beside the cloud runs and is
   passed over, and a record that does not say where it landed may be either.
   By the mode the run recorded: equal for ``full_refresh``; for
   ``incremental_append``, at least the sum over the runs back to the last
   full load (the last run alone is no floor: a table that lost every earlier
   run's rows still holds as many as it appended); reported without a gate for
   a merge, dedup, CDC or streaming load. A transformation build is never
   compared: the dbt runner's ``records_total`` counts dbt nodes, not rows.
   Otherwise the count is reported with the reason, and only an empty table
   fails. An empty table fails even when the run landed none: agreeing on zero
   is not agreement. The one exception is a reference-only contract with no
   run of its own to compare with: a pipeline outside forge owns the rows and
   may not have run yet, so the empty table is INFO, as a missing one is.

The binding's ``{{ env.* }}`` templates are resolved with the resolver
``fluid apply`` runs before it emits (``resolve_env_templates_in_contract``),
so verify looks for the table apply created; a database, table or bucket still
templated after that is an error naming the variable.

A missing column, a type change, a moved location, an empty table and a count
that breaks the build's rule are CRITICAL, so ``--strict`` fails on them.
Extra columns are INFO, as in every other verifier. Glue declares no
nullability, so there is no catalogue constraint to drift and the constraints
dimension says so rather than reporting a comparison it did not make.

**Where Athena writes the result.** Resolved in the order AWS SDK for pandas
uses (``awswrangler.athena._utils._get_s3_output``): a workgroup with managed
query results, or one that enforces its own output location, wins, because
Athena ignores the client's choice there; then an explicit override
(``--athena-output-location`` / ``FLUID_ATHENA_OUTPUT_LOCATION``); then the
workgroup's configured output; then ``s3://<binding bucket>/.fluid/athena-results/``.
Whoever chose it, a location inside the table's own prefix is refused (an
enforced one) or replaced by the binding-bucket default (one the query can
override), since Athena's result files there would be read back as data.
awswrangler's own last resort is a bucket named
``aws-athena-query-results-<account>-<region>``, which it warns against
because S3 names are global and a predictable name can be claimed by someone
else. The binding's own bucket cannot be, and the leading ``.`` keeps the
result files out of any Hive table listing.

**IAM this needs** (docs/verify-aws-athena.md has the policy):
``glue:GetTable`` and ``glue:GetDatabase``; ``athena:GetWorkGroup``,
``athena:StartQueryExecution``, ``athena:GetQueryExecution``,
``athena:GetQueryResults`` and ``athena:StopQueryExecution``; ``s3:GetObject``,
``s3:ListBucket`` and ``s3:GetBucketLocation`` on the data prefix;
``s3:PutObject``, ``s3:GetObject``, ``s3:ListBucket``,
``s3:GetBucketLocation``, ``s3:ListBucketMultipartUploads``,
``s3:AbortMultipartUpload`` and ``s3:ListMultipartUploadParts`` on the results
prefix (the set AWS's ``AmazonAthenaFullAccess`` grants on its results bucket).

Borrowed, not built:

* Polling follows PyAthena (``pyathena/common.py``, ``BaseCursor.__poll`` /
  ``_poll``) and awswrangler (``athena._executions.wait_query``): a fixed poll
  interval, terminal states ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``, and
  PyAthena's ``kill_on_interrupt`` (Ctrl-C stops the query rather than leaving
  it scanning). Both libraries poll without a deadline; a CI stage cannot, so
  this adds one and calls ``StopQueryExecution`` when it passes, as the
  output-port driver in ``output_ports/mcp/drivers/athena.py`` already does.
  Neither library is a dependency: ``awswrangler`` pulls pandas and pyarrow
  and PyAthena a DB-API layer, for one fixed ``COUNT(*)``. That driver is not
  reused either: it refuses ``format: parquet`` (it serves ``athena_table`` /
  ``glue_table`` bindings), falls back to ``us-east-1`` when no region is
  named, and has no results-location resolution.
* The checks are Soda's contract checks for a dataset, ``schema`` plus
  ``row_count`` (whose default threshold is "at least one row"), and dbt-utils'
  ``equal_rowcount`` with the run record standing in for the second relation.
  ``equal_rowcount`` passes two empty relations; this does not, following
  Soda's threshold.
* The query and its reading of ``Rows[1]`` come from the demo repository's
  hand-written ``scripts/verify_cloud.py``, which this check replaces; so does
  passing the region explicitly, because the boto3 the demo ran read
  ``AWS_DEFAULT_REGION`` and not ``AWS_REGION``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.verify.athena")

#: The optional-dependency extra that brings boto3.
AWS_EXTRA_INSTALL = "pip install 'data-product-forge[aws]'"

#: Env overrides, each read when the matching CLI flag is not given.
ENV_OUTPUT_LOCATION = "FLUID_ATHENA_OUTPUT_LOCATION"
ENV_WORKGROUP = "FLUID_ATHENA_WORKGROUP"
ENV_TIMEOUT_SECONDS = "FLUID_ATHENA_TIMEOUT_SECONDS"

DEFAULT_WORKGROUP = "primary"
DEFAULT_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 1.0

#: Prefix under the binding's own bucket used when nothing else names a
#: results location. The leading dot is Hadoop's hidden-path convention, so a
#: Hive/Athena table over the bucket never lists the result files as data.
RESULTS_PREFIX = ".fluid/athena-results/"

_FINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

#: How the Athena count is held to the build's run records, by the acquisition
#: mode the run recorded (``_comparison_rule``). Recorded as
#: ``dimensions.row_count.compared_with.rule``.
RULE_EQUAL = "equal"
RULE_AT_LEAST_CUMULATIVE = "at_least_cumulative"
RULE_REPORTED = "reported"

#: Run records read for one comparison, newest first. An append history longer
#: than this is summed over its newest runs only, which still bounds the table
#: from below.
MAX_RUN_RECORDS = 1000

# Every pattern below is applied with ``fullmatch``: ``$`` also matches before
# a trailing newline, so ``re.match(r"...$")`` would admit ``"eu-north-1\n"``.
#
# Athena workgroup names: 1-128 of ``[a-zA-Z0-9._-]`` (Athena API reference,
# ``WorkGroupName``). It is an API parameter, not SQL, but a malformed one is
# better refused here with a clear message than by Athena with a generic one.
_WORKGROUP_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
# ``s3://<bucket>/<optional key prefix>``: a DNS-style bucket name and a key
# with no whitespace or control characters.
_S3_URI_RE = re.compile(r"s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9](/[^\s\x00-\x1f\x7f]*)?")
# AWS region names (``eu-north-1``, ``us-gov-west-1``). The region becomes part
# of the endpoint host name, so a contract must not be able to put anything
# else there; botocore also refuses one, but by raising at client creation.
_REGION_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)+")
# Glue database and table names as the count query quotes them. Athena's rule
# for names is letters, digits and underscore, and a name that starts with a
# digit is legal when it is double-quoted in a SELECT (Athena User Guide,
# "Name databases, tables, and columns"); the query always quotes. Nothing
# here can close a double-quoted identifier.
_ATHENA_IDENT_RE = re.compile(r"[A-Za-z0-9_]{1,255}")

# Hive/Glue spells some types two ways; compare on one. Whitespace inside a
# parameterised type (``decimal(10, 2)``) is not significant either.
_GLUE_TYPE_SYNONYMS = {"integer": "int"}

ClientFactory = Callable[[str, str], Any]


class AthenaVerifyError(Exception):
    """A check that could not run. Becomes a ``status: error`` result."""

    def __init__(self, message: str, *, exists: Optional[bool] = None) -> None:
        super().__init__(message)
        self.exists = exists


# ── Dispatch ────────────────────────────────────────────────────────────


def is_athena_verifiable(binding: Any) -> bool:
    """True when ``binding`` is an S3+Glue table this module can verify.

    The same gates the IaC emitter applies before it writes a Glue table
    (``iac/providers/aws.py::_emit_glue``): an AWS platform, a
    ``location.database`` and ``location.table``, and a format with Hive
    storage classes Athena can read the table through. Resolved through the
    emitter's own ``is_cloud`` and ``_GLUE_HIVE_STORAGE`` so the two cannot
    disagree about what ``fluid apply`` created. A bucket is required because
    the default results location lives in it; a bucket-less binding keeps its
    previous handling.

    Imported lazily: ``fluid --help`` must not pull in the ``iac`` package.
    """
    if not isinstance(binding, Mapping):
        return False
    from fluid_build.iac.provider_match import is_cloud
    from fluid_build.iac.providers.aws import _GLUE_HIVE_STORAGE

    if not is_cloud(binding, "aws"):
        return False
    # The emitter's own default: a binding with no format is emitted as parquet.
    fmt = str(binding.get("format") or "parquet").lower()
    if fmt not in _GLUE_HIVE_STORAGE:
        return False
    location = binding.get("location")
    if not isinstance(location, Mapping):
        return False
    return all(location.get(key) for key in ("database", "table", "bucket"))


# ── Options ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AthenaOptions:
    """How to run the count query. ``problem`` is set when an option is invalid."""

    output_location: Optional[str] = None
    workgroup: str = DEFAULT_WORKGROUP
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    problem: Optional[str] = None


def options_from_args(args: Any, environ: Optional[Mapping[str, str]] = None) -> AthenaOptions:
    """Read the Athena options: CLI flag, then env var, then default.

    Never raises. An invalid value is carried as ``problem`` and reported as a
    verification error for every expose that needs it, so a typo in CI fails
    the stage instead of silently falling back to a default.
    """
    env = os.environ if environ is None else environ
    problems: List[str] = []

    output_location = getattr(args, "athena_output_location", None) or env.get(ENV_OUTPUT_LOCATION)
    if output_location:
        output_location = output_location.strip()
        if not _S3_URI_RE.fullmatch(output_location):
            problems.append(
                f"Athena output location {output_location!r} is not an s3://<bucket>/<prefix> URI "
                f"(--athena-output-location / {ENV_OUTPUT_LOCATION})"
            )
        elif not output_location.endswith("/"):
            output_location += "/"

    workgroup = (
        getattr(args, "athena_workgroup", None) or env.get(ENV_WORKGROUP) or DEFAULT_WORKGROUP
    ).strip()
    if not _WORKGROUP_RE.fullmatch(workgroup):
        problems.append(
            f"Athena workgroup {workgroup!r} is not a valid workgroup name "
            f"(--athena-workgroup / {ENV_WORKGROUP})"
        )

    raw_timeout: Any = getattr(args, "athena_timeout", None)
    if raw_timeout is None:
        raw_timeout = env.get(ENV_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS
    try:
        timeout_seconds = float(raw_timeout)
        if not timeout_seconds > 0 or timeout_seconds == float("inf"):
            raise ValueError
    except (TypeError, ValueError):
        problems.append(
            f"Athena timeout {raw_timeout!r} is not a positive number of seconds "
            f"(--athena-timeout / {ENV_TIMEOUT_SECONDS})"
        )
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    return AthenaOptions(
        output_location=output_location or None,
        workgroup=workgroup,
        timeout_seconds=timeout_seconds,
        problem="; ".join(problems) or None,
    )


# ── Clients ─────────────────────────────────────────────────────────────


def _boto3_client(service: str, region: str) -> Any:
    try:
        import boto3  # type: ignore[import-untyped,unused-ignore]
    except ImportError as exc:
        raise AthenaVerifyError(
            "boto3 is not installed, so the Glue table and the Athena row count cannot "
            f"be checked. Install the aws extra: {AWS_EXTRA_INSTALL}"
        ) from exc
    # Explicit region: the boto3 the demo ran read AWS_DEFAULT_REGION and not
    # AWS_REGION, and failed with "You must specify a region" after the apply.
    return boto3.client(service, region_name=region)


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


# ── Glue ────────────────────────────────────────────────────────────────


def _canonical_glue_type(raw: Any) -> str:
    name = re.sub(r"\s+", "", str(raw or "")).lower()
    return _GLUE_TYPE_SYNONYMS.get(name, name)


def _normalize_s3(uri: Any) -> str:
    text = str(uri or "").strip()
    for scheme in ("s3a://", "s3n://"):
        if text.startswith(scheme):
            text = "s3://" + text[len(scheme) :]
    return text.rstrip("/")


def _inside(uri: Any, prefix: Any) -> bool:
    """True when ``uri`` is ``prefix`` itself or an object or prefix under it."""
    base = _normalize_s3(prefix)
    return bool(base) and (_normalize_s3(uri) + "/").startswith(base + "/")


def _declared_fields(expose: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    # The emitter reads ``exposes[].contract.schema``; the older top-level
    # ``schema`` dialect is accepted the way the other verifiers accept it.
    section = expose.get("contract")
    raw = (section.get("schema") if isinstance(section, Mapping) else None) or expose.get("schema")
    if isinstance(raw, Mapping):
        raw = raw.get("fields")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, Mapping) and f.get("name")]


def _compare_schema(
    glue_table: Mapping[str, Any], declared: List[Mapping[str, Any]]
) -> Dict[str, Any]:
    from fluid_build.iac.providers.aws import _hive_type

    storage = glue_table.get("StorageDescriptor") or {}
    actual: Dict[str, Dict[str, str]] = {}
    for column in list(storage.get("Columns") or []) + list(glue_table.get("PartitionKeys") or []):
        name = str(column.get("Name") or "")
        if name:
            actual[name.lower()] = {"name": name, "type": _canonical_glue_type(column.get("Type"))}

    expected: Dict[str, Dict[str, str]] = {}
    for field in declared:
        name = str(field["name"])
        expected[name.lower()] = {
            "name": name,
            "type": _canonical_glue_type(_hive_type(field.get("type"))),
        }

    matching: List[str] = []
    missing: List[Dict[str, Any]] = []
    type_mismatches: List[Dict[str, Any]] = []
    for key, exp in expected.items():
        act = actual.get(key)
        if act is None:
            missing.append({"field": exp["name"], "expected": {"type": exp["type"]}})
            continue
        matching.append(exp["name"])
        if act["type"] != exp["type"]:
            type_mismatches.append(
                {"field": exp["name"], "expected": exp["type"], "actual": act["type"]}
            )
    extra = [
        {"field": act["name"], "actual": {"type": act["type"]}}
        for key, act in actual.items()
        if key not in expected
    ]
    return {
        "matching": matching,
        "missing": missing,
        "extra": extra,
        "type_mismatches": type_mismatches,
        "total_expected": len(expected),
        "total_actual": len(actual),
    }


# ── Athena ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ResultsLocation:
    """Where Athena writes the count's result, and why there."""

    #: What to send as ``ResultConfiguration.OutputLocation``; ``None`` when the
    #: workgroup decides, so no ``ResultConfiguration`` is passed.
    send: Optional[str]
    #: The location Athena will use (``None`` for managed query results).
    in_effect: Optional[str]
    source: str
    note: Optional[str] = None


_OUTSIDE_HINT = (
    f"Pass a location outside it with --athena-output-location or {ENV_OUTPUT_LOCATION}."
)


def _inside_table_error(location: str, table_locations: List[str], how: str) -> AthenaVerifyError:
    table_location = next(t for t in table_locations if _inside(location, t))
    return AthenaVerifyError(
        f"Athena would write its result to {location} ({how}), inside the table's own "
        f"location {table_location}, where it would be read back as data. {_OUTSIDE_HINT}",
        exists=True,
    )


def _results_location(
    athena: Any, options: AthenaOptions, bucket: str, table_locations: List[str]
) -> _ResultsLocation:
    """Resolve the result location, refusing one inside the table.

    ``table_locations`` are the prefixes the result must stay out of: the Glue
    table's location, which the count reads, and the binding's, which the build
    writes. The check applies to the location *in effect*, whoever chose it: a
    workgroup's own location inside the table would put ``<query id>.csv`` among
    the table's files just as an override would.
    """

    def inside(location: Optional[str]) -> bool:
        return bool(location) and any(_inside(location, t) for t in table_locations)

    configuration: Optional[Mapping[str, Any]] = None
    try:
        workgroup = athena.get_work_group(WorkGroup=options.workgroup)
        configuration = (workgroup.get("WorkGroup") or {}).get("Configuration") or {}
    except Exception as exc:  # noqa: BLE001 — unreadable config is not fatal
        # Without athena:GetWorkGroup the query can still run; it just cannot
        # know what the workgroup would have chosen. A workgroup that enforces
        # its own location still wins on Athena's side.
        LOG.warning(
            "verify_athena_workgroup_unreadable workgroup=%s error=%s",
            options.workgroup,
            _error_code(exc) or type(exc).__name__,
        )

    workgroup_output: Optional[str] = None
    if configuration is not None:
        managed = configuration.get("ManagedQueryResultsConfiguration") or {}
        if managed.get("Enabled"):
            return _ResultsLocation(None, None, "workgroup-managed")
        workgroup_output = (configuration.get("ResultConfiguration") or {}).get("OutputLocation")
        if configuration.get("EnforceWorkGroupConfiguration") and workgroup_output:
            if inside(workgroup_output):
                # Athena ignores any other location for this workgroup, so the
                # only safe answer is not to run the query at all.
                raise AthenaVerifyError(
                    f"Athena workgroup {options.workgroup} enforces its result location "
                    f"{workgroup_output}, which is inside the table's own location, so the "
                    "count's result would be read back as table data. Move the workgroup's "
                    "output location, or pick another workgroup with --athena-workgroup or "
                    f"{ENV_WORKGROUP}.",
                    exists=True,
                )
            return _ResultsLocation(None, workgroup_output, "workgroup-enforced")
    if options.output_location:
        if inside(options.output_location):
            raise _inside_table_error(options.output_location, table_locations, "the override")
        return _ResultsLocation(options.output_location, options.output_location, "override")
    note: Optional[str] = None
    if workgroup_output:
        if not inside(workgroup_output):
            return _ResultsLocation(None, workgroup_output, "workgroup")
        # Not enforced, so a location sent with the query wins over it.
        note = (
            f"the workgroup's output location {workgroup_output} is inside the table, so the "
            "result goes under the binding's bucket instead"
        )
        LOG.warning("verify_athena_workgroup_output_inside_table workgroup=%s", options.workgroup)
    fallback = f"s3://{bucket}/{RESULTS_PREFIX}"
    if not _S3_URI_RE.fullmatch(fallback):
        raise AthenaVerifyError(
            f"binding.location.bucket {bucket!r} is not an S3 bucket name to write the Athena "
            f"result under; pass --athena-output-location or {ENV_OUTPUT_LOCATION}",
            exists=True,
        )
    if inside(fallback):
        raise _inside_table_error(fallback, table_locations, "the default")
    return _ResultsLocation(fallback, fallback, "binding-bucket", note)


def _stop_quietly(athena: Any, query_id: str) -> Optional[str]:
    """Stop the query. ``None`` when it was stopped, else why it was not."""
    try:
        athena.stop_query_execution(QueryExecutionId=query_id)
    except Exception as exc:  # noqa: BLE001 — best effort; the error is already reported
        reason = _error_code(exc) or type(exc).__name__
        LOG.warning("verify_athena_stop_failed query_execution_id=%s error=%s", query_id, reason)
        return reason
    return None


def _wait(
    athena: Any,
    query_id: str,
    timeout_seconds: float,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> Mapping[str, Any]:
    deadline = monotonic() + timeout_seconds
    try:
        while True:
            execution = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            state = str((execution.get("Status") or {}).get("State") or "")
            if state in _FINAL_STATES:
                return dict(execution)
            remaining = deadline - monotonic()
            if remaining <= 0:
                LOG.warning(
                    "verify_athena_query_timeout query_execution_id=%s timeout_seconds=%s",
                    query_id,
                    timeout_seconds,
                )
                stop_failed = _stop_quietly(athena, query_id)
                stopped = (
                    "it was stopped"
                    if stop_failed is None
                    else f"stopping it failed ({stop_failed}), so it may still be running"
                )
                raise AthenaVerifyError(
                    f"Athena query {query_id} did not finish within {timeout_seconds:g}s "
                    f"(last state {state or 'unknown'}); {stopped}. Raise the limit with "
                    f"--athena-timeout or {ENV_TIMEOUT_SECONDS}.",
                    exists=True,
                )
            sleep(min(POLL_INTERVAL_SECONDS, remaining))
    except KeyboardInterrupt:
        # PyAthena's kill_on_interrupt: do not leave the query scanning.
        _stop_quietly(athena, query_id)
        raise


def _count_rows(
    athena: Any,
    database: str,
    table: str,
    options: AthenaOptions,
    bucket: str,
    table_locations: List[str],
    region: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> Tuple[int, Dict[str, Any]]:
    # Identifier positions cannot be bound, so check before interpolating:
    # ``_ATHENA_IDENT_RE`` admits nothing that can close a double-quoted name.
    for name in (database, table):
        if not _ATHENA_IDENT_RE.fullmatch(name):
            raise AthenaVerifyError(f"{name!r} is not a quotable Athena identifier", exists=True)
    sql = f'SELECT COUNT(*) FROM "{database}"."{table}"'

    results = _results_location(athena, options, bucket, table_locations)
    request: Dict[str, Any] = {"QueryString": sql, "WorkGroup": options.workgroup}
    if results.send:
        request["ResultConfiguration"] = {"OutputLocation": results.send}

    query: Dict[str, Any] = {
        "workgroup": options.workgroup,
        "region": region,
        "sql": sql,
        "output_location": results.in_effect,
        "output_location_source": results.source,
    }
    if results.note:
        query["output_location_note"] = results.note
    query_id = str(athena.start_query_execution(**request)["QueryExecutionId"])
    query["query_execution_id"] = query_id
    LOG.info(
        "verify_athena_query_started query_execution_id=%s workgroup=%s region=%s "
        "output_location_source=%s",
        query_id,
        options.workgroup,
        region,
        results.source,
    )

    execution = _wait(athena, query_id, options.timeout_seconds, sleep=sleep, monotonic=monotonic)
    status = execution.get("Status") or {}
    state = str(status.get("State") or "")
    statistics = execution.get("Statistics") or {}
    query["state"] = state
    query["data_scanned_bytes"] = statistics.get("DataScannedInBytes")
    query["engine_execution_ms"] = statistics.get("EngineExecutionTimeInMillis")
    if state != "SUCCEEDED":
        reason = (
            status.get("StateChangeReason")
            or (status.get("AthenaError") or {}).get("ErrorMessage")
            or "no reason given"
        )
        raise AthenaVerifyError(
            f"Athena could not count {database}.{table}: query {query_id} {state}: {reason}",
            exists=True,
        )

    rows = athena.get_query_results(QueryExecutionId=query_id, MaxResults=2)["ResultSet"]["Rows"]
    try:
        # Rows[0] is the header row Athena always returns for a SELECT.
        count = int(rows[1]["Data"][0]["VarCharValue"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise AthenaVerifyError(
            f"Athena query {query_id} succeeded but returned no readable count", exists=True
        ) from exc
    LOG.info(
        "verify_athena_query_finished query_execution_id=%s state=%s rows=%d "
        "data_scanned_bytes=%s",
        query_id,
        state,
        count,
        query["data_scanned_bytes"],
    )
    return count, query


# ── Run records ─────────────────────────────────────────────────────────


def _landing_builds(contract: Mapping[str, Any], expose_id: str) -> List[Mapping[str, Any]]:
    """The builds that write ``expose_id``, as the build runners decide it.

    A build writes the exposes its ``outputs`` names; one that names none of
    the contract's exposes writes ``exposes[0]``
    (``build_runners/duckdb/runner.py::_build_expose``).
    """
    exposes = [e for e in contract.get("exposes") or [] if isinstance(e, Mapping)]
    expose_ids = {e.get("exposeId") or e.get("id") for e in exposes}
    first = (exposes[0].get("exposeId") or exposes[0].get("id")) if exposes else None
    landing: List[Mapping[str, Any]] = []
    for build in contract.get("builds") or []:
        if not isinstance(build, Mapping):
            continue
        outputs = {o for o in build.get("outputs") or [] if isinstance(o, str)}
        if expose_id in outputs or (not outputs & expose_ids and expose_id == first):
            landing.append(build)
    return landing


def _comparison_rule(mode: str) -> str:
    """How the table's count relates to what the build's runs landed.

    Only a full refresh replaces the table with exactly the run's rows. An
    append keeps every earlier run's rows too, so the runs since the last full
    load are a floor. A merge, dedup, CDC or streaming load can update or delete
    rows a run carried, so its count bounds nothing and is reported, not gated.
    """
    mode = mode.lower()
    if mode == "full_refresh":
        return RULE_EQUAL
    if mode == "incremental_append":
        return RULE_AT_LEAST_CUMULATIVE
    return RULE_REPORTED


def _run_records(workdir: Path, product_id: str, build_id: str) -> List[Optional[Dict[str, Any]]]:
    """The build's run records, newest first; ``None`` for one that does not parse.

    The directory and order ``latest_run_record`` reads (run ids sort by time).
    An unreadable record is kept as ``None``, not skipped: it may be the run
    that last replaced the table, so the runs before it bound nothing.
    """
    runs_dir = workdir / ".fluid" / "runs" / product_id / build_id / "runs"
    if not runs_dir.is_dir():
        return []
    records: List[Optional[Dict[str, Any]]] = []
    for path in sorted(runs_dir.glob("*.json"), reverse=True)[:MAX_RUN_RECORDS]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        records.append(record if isinstance(record, dict) else None)
    return records


def _landed_facet(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """``facets.landed``, which the duckdb runner writes (``_landed_facet`` there)."""
    facets = record.get("facets")
    landed = facets.get("landed") if isinstance(facets, Mapping) else None
    return landed if isinstance(landed, Mapping) else None


def _wrote_into(record: Optional[Mapping[str, Any]], table_location: str) -> Optional[bool]:
    """Whether a run landed its rows in ``table_location``: yes, no, or unknown.

    One contract directory serves every target an overlay selects, so its
    ``.fluid/runs`` holds local runs and cloud runs of the same build side by
    side, and only the destinations the run recorded say which table it wrote.
    Unknown (``None``) for a record that does not name them: an engine that does
    not record them, a run from before they were recorded, or a record that does
    not parse.
    """
    if record is None:
        return None
    landed = _landed_facet(record)
    destinations = landed.get("destinations") if landed is not None else None
    if not isinstance(destinations, Mapping) or not destinations:
        return None
    inside = [_inside(str(d), table_location) for d in destinations.values()]
    if all(inside):
        return True
    if not any(inside):
        return False
    return None


def _run_total(record: Mapping[str, Any]) -> Optional[int]:
    """``records_total`` of a run that succeeded and counted it at the write.

    ``rows_from: write`` says the total is what the writes reported writing. A
    total from a second read of the source is not: a live source can change
    between the write and the count, and the table then fails a correct load.
    """
    landed = _landed_facet(record)
    if landed is None or landed.get("rows_from") != "write":
        return None
    if str(record.get("state") or "").lower() != "succeeded":
        return None
    try:
        total = int(record["records_total"])
    except (KeyError, TypeError, ValueError):
        return None
    return total if total >= 0 else None


def _landed_since_full_load(
    records: List[Optional[Dict[str, Any]]], table_location: str
) -> Tuple[int, List[str], bool]:
    """Rows the runs since the last full load put in the table, those runs, and
    whether the walk reached that full load.

    Walks back from the newest run that wrote the table. An
    ``incremental_append`` run adds its rows; a ``full_refresh`` run is the base
    they were added to and ends the walk. So does anything that leaves the
    history unknown: a run that did not succeed, did not count at the write,
    recorded another mode, or does not say where it landed, and the end of the
    records on disk (a CI stage has only the records it carried over). Runs
    that wrote another target are skipped. However the walk ends, the sum is a
    floor, because an append removes no row; it is the whole floor only when the
    walk reached a full load.
    """
    total = 0
    runs: List[str] = []
    for record in records:
        wrote = _wrote_into(record, table_location)
        if wrote is False:
            continue
        if record is None or wrote is None:
            break
        rows = _run_total(record)
        mode = str((_landed_facet(record) or {}).get("mode") or "").lower()
        if rows is None or mode not in ("incremental_append", "full_refresh"):
            break
        total += rows
        runs.append(str(record.get("run_id")))
        if mode == "full_refresh":
            return total, runs, True
    return total, runs, False


def _landed_rows(
    contract: Mapping[str, Any], expose_id: str, workdir: Path, table_location: str
) -> Tuple[Optional[int], Dict[str, Any]]:
    """What the landing build's runs put in ``table_location``, when a record says.

    Compared only for an acquisition build, and only with a run that recorded
    landing its rows in this table and counting them at the write. Anything
    else is reported with the reason and never gates.
    """
    from fluid_build.build_runners._acquisition_common import is_acquisition_build
    from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier

    builds = _landing_builds(contract, expose_id)
    if not builds:
        return None, {"source": "none", "note": "no build in the contract writes this expose"}
    if len(builds) > 1:
        ids = ", ".join(str(b.get("id")) for b in builds)
        return None, {
            "source": "none",
            "note": f"{len(builds)} builds write this expose ({ids}); no single run to compare",
        }
    build = builds[0]
    if not is_acquisition_build(dict(build)):
        # A transformation's run record counts what its engine ran: the dbt
        # runner's records_total is the number of nodes in run_results.json.
        return None, {
            "source": "none",
            "build_id": str(build.get("id")),
            "rule": RULE_REPORTED,
            "note": (
                f"build {build.get('id')} is a {build.get('pattern') or 'non-acquisition'} "
                "build, whose run record does not count the rows it landed"
            ),
        }
    try:
        product_id = validate_identifier(str(contract.get("id") or ""), kind="contract.id")
        build_id = validate_identifier(str(build.get("id") or ""), kind="build.id")
    except IdentifierViolation:
        # The runner refuses these ids too, so it never wrote a record for them;
        # and a path built from them must not be read.
        return None, {"source": "none", "note": "contract or build id is not a valid identifier"}

    records = _run_records(workdir, product_id, build_id)
    if not records:
        return None, {"source": "none", "build_id": build_id, "note": "no run record"}
    # Runs that landed in another target (a local run from the same directory)
    # never touched this table, so the newest run that may have is the one.
    skipped = 0
    while skipped < len(records) and _wrote_into(records[skipped], table_location) is False:
        skipped += 1
    if skipped == len(records):
        return None, {
            "source": "none",
            "build_id": build_id,
            "other_target_runs_skipped": skipped,
            "note": f"none of the build's {skipped} recorded runs landed in {table_location}",
        }
    record = records[skipped]
    if record is None:
        return None, {
            "source": "none",
            "build_id": build_id,
            "note": "the newest run record that may be this table's does not parse",
        }
    landed = _landed_facet(record) or {}
    info: Dict[str, Any] = {
        "source": "run_record",
        "build_id": build_id,
        "run_id": record.get("run_id"),
        "state": record.get("state"),
        "finished_at": record.get("finished_at"),
        "rule": _comparison_rule(str(landed.get("mode") or "")),
    }
    if skipped:
        info["other_target_runs_skipped"] = skipped
    if _wrote_into(record, table_location) is None:
        info["source"] = "none"
        info["note"] = (
            f"run {record.get('run_id')} does not record where it landed its rows, so it "
            "may have been another target's run"
        )
        return None, info
    if str(record.get("state") or "").lower() != "succeeded":
        info["source"] = "none"
        info["note"] = (
            f"the last run {record.get('run_id')} ended {record.get('state')}, so what it "
            "landed is not a count to hold the table to"
        )
        return None, info
    total = _run_total(record)
    if total is None:
        info["source"] = "none"
        info["note"] = (
            f"run {record.get('run_id')} did not count its rows at the write, so its "
            "records_total is not a count to hold the table to"
        )
        return None, info
    if info["rule"] == RULE_AT_LEAST_CUMULATIVE:
        total, info["runs"], info["reached_full_load"] = _landed_since_full_load(
            records[skipped:], table_location
        )
    return total, info


def _runs_phrase(info: Mapping[str, Any]) -> str:
    runs = [str(r) for r in info.get("runs") or []]
    if len(runs) > 1:
        return f"build {info.get('build_id')} runs {runs[-1]} to {runs[0]} ({len(runs)} runs)"
    return f"build {info.get('build_id')} run {info.get('run_id')}"


def _since_phrase(info: Mapping[str, Any]) -> str:
    """Which appends the floor sums, and whether it is the whole floor."""
    if info.get("reached_full_load"):
        return "since the last full load"
    return (
        "in the runs on record, which reach back to no full load, so earlier runs "
        "may be missing from this floor"
    )


def _row_count_dimension(
    count: int,
    landed: Optional[int],
    info: Mapping[str, Any],
    table_id: str,
    *,
    reference_only: bool = False,
) -> Dict[str, Any]:
    """``status`` is ``pass``, ``fail`` (CRITICAL) or ``info`` (reported, never gated)."""
    run = _runs_phrase(info)
    rule = info.get("rule")
    if count == 0 and landed is None and reference_only:
        # Bug 6's case with the table present: apply creates the Glue table,
        # and the pipeline that owns the rows may not have run yet.
        message = (
            f"Athena counted 0 rows in {table_id}; not gated, because the contract is "
            "reference-only and the pipeline that owns the table may not have written it yet"
        )
        status = "info"
    elif count == 0:
        message = f"Athena counted 0 rows in {table_id}"
        if landed is not None:
            message += f"; {run} landed {landed:,}"
        status = "fail"
    elif landed is not None and rule == RULE_EQUAL and count != landed:
        message = (
            f"Athena counted {count:,} rows in {table_id}; {run} landed {landed:,} "
            "and is a full refresh"
        )
        status = "fail"
    elif landed is not None and rule == RULE_AT_LEAST_CUMULATIVE and count < landed:
        message = (
            f"Athena counted {count:,} rows in {table_id}; {run} landed {landed:,} "
            f"{_since_phrase(info)}, and an append removes no row, so the table holds "
            "fewer rows than its runs landed"
        )
        status = "fail"
    elif landed is not None and rule == RULE_EQUAL:
        message = f"{count:,} rows, equal to what {run} landed"
        status = "pass"
    elif landed is not None and rule == RULE_AT_LEAST_CUMULATIVE:
        message = f"{count:,} rows, at least the {landed:,} that {run} landed {_since_phrase(info)}"
        status = "pass"
    elif landed is not None:
        message = (
            f"{count:,} rows; {run} landed {landed:,}, not compared because the build "
            "merges, dedups or streams, so the count bounds nothing"
        )
        status = "pass"
    else:
        note = info.get("note") or "no run record"
        message = f"{count:,} rows; not compared with a build ({note})"
        status = "pass"
    return {
        "status": status,
        "actual": count,
        "expected": landed,
        "compared_with": dict(info),
        "message": message,
    }


# ── Result ──────────────────────────────────────────────────────────────


def _default_chain_region() -> Optional[str]:
    """The region boto3's own chain resolves (``AWS_DEFAULT_REGION``, then the
    profile in the AWS config file): what ``fluid apply``'s provider reads too."""
    try:
        import boto3  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        return None
    try:
        return str(boto3.Session().region_name or "") or None
    except Exception:  # noqa: BLE001 — e.g. AWS_PROFILE names no profile
        return None


def _resolved_binding(binding: Any) -> Dict[str, Any]:
    """The binding with ``{{ env.* }}`` resolved exactly as ``fluid apply`` resolves it.

    Apply runs the whole contract through ``resolve_env_templates_in_contract``
    before the emitter reads it (``_apply_opentofu_engine``), so the Glue table
    it created is named by the resolved values. The same resolver, not the plain
    one: it leaves a credential-shaped placeholder (``{{ env.X_PASSWORD }}``)
    literal, and verify writes these values into the report, the console, the
    count query and Athena's query history.
    """
    if not isinstance(binding, Mapping):
        return {}
    from fluid_build.cli._common import resolve_env_templates_in_contract

    resolved = resolve_env_templates_in_contract(dict(binding))
    return resolved if isinstance(resolved, dict) else {}


def _unresolved_template(field: str, value: Any) -> Optional[str]:
    """Why ``value`` is still a template after resolution; ``None`` when it is not."""
    from fluid_build.observability.secret_redactor import is_sensitive_key_name
    from fluid_build.providers.snowflake.util.config import ENV_TEMPLATE_RE

    text = str(value or "")
    if "{{" not in text:
        return None
    names = sorted({name.strip() for name in ENV_TEMPLATE_RE.findall(text)})
    sensitive = [name for name in names if is_sensitive_key_name(name)]
    if sensitive:
        return (
            f"{field} names {', '.join(sensitive)}, which looks like a credential, so it is "
            "never resolved into a contract (fluid apply leaves it literal too); name it "
            "through a variable that is not a secret"
        )
    if names:
        verb = "is" if len(names) == 1 else "are"
        return f"{field} is {text!r}, and {', '.join(names)} {verb} not set"
    return f"{field} {text!r} is not a {{{{ env.NAME }}}} template that can be resolved"


def _resolve_region(binding: Mapping[str, Any], loc: Mapping[str, Any]) -> Tuple[str, str]:
    """``(region, where it came from)``; ``("", "")`` when nothing names one.

    The binding's own region first (``binding`` has been through
    ``_resolved_binding``). One still templated, because its variable is unset
    or names a credential, is passed over, as the IaC emitter passes over a
    region value that is not a region code (``provider_block_for``): apply
    then deployed in the environment's region, so that is where to look.
    """
    for source, raw in (
        ("binding.location.region", loc.get("region")),
        ("binding.region", binding.get("region")),
    ):
        if not raw:
            continue
        value = str(raw).strip()
        if "{{" in value:
            LOG.warning("verify_athena_region_template_unresolved field=%s", source)
            continue
        return value, source
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(var, "").strip()
        if value:
            return value, var
    profile_region = _default_chain_region()
    if profile_region:
        return profile_region.strip(), "the AWS config profile"
    return "", ""


def _preflight_problem(
    options: AthenaOptions, region: str, database: str, table: str, loc: Mapping[str, Any]
) -> Optional[str]:
    """Why the check cannot start, before any AWS call; ``None`` when it can."""
    from fluid_build.providers.aws.util.warehouse import bucket_uses_fallback

    if options.problem:
        return options.problem
    if not region:
        return (
            "No AWS region for this binding: set binding.location.region, AWS_REGION, or a "
            "region in the AWS config profile"
        )
    if not _REGION_RE.fullmatch(region):
        return f"{region!r} is not an AWS region name"
    for key in ("database", "table", "bucket"):
        # Checked before the names are: a template is not a name to refuse.
        template = _unresolved_template(f"binding.location.{key}", loc.get(key))
        if template:
            return template
    for name in (database, table):
        if not _ATHENA_IDENT_RE.fullmatch(name):
            return (
                f"Glue database/table {database + '.' + table!r} is not a safe identifier for "
                f"the count query: {name!r} must be letters, digits and underscores"
            )
    if bucket_uses_fallback(loc):
        return f"binding.location.bucket {loc.get('bucket')!r} did not resolve to a bucket name"
    return None


def _catalogue_dimensions(
    schema: Mapping[str, Any], actual_location: str, expected_location: str, region: str
) -> Dict[str, Any]:
    """The four dimensions every verifier reports, from what Glue says."""
    location_ok = _normalize_s3(actual_location) == _normalize_s3(expected_location)
    return {
        "structure": {
            "status": "pass" if not (schema["missing"] or schema["extra"]) else "fail",
            "matching_fields": schema["matching"],
            "missing_fields": schema["missing"],
            "extra_fields": schema["extra"],
            "total_expected": schema["total_expected"],
            "total_actual": schema["total_actual"],
        },
        "types": {
            "status": "pass" if not schema["type_mismatches"] else "fail",
            "mismatches": schema["type_mismatches"],
        },
        "constraints": {
            "status": "pass",
            "mismatches": [],
            "message": "Glue columns declare no nullability, so there is no constraint to drift",
        },
        "location": {
            "status": "pass" if location_ok else "fail",
            "expected": f"{expected_location} ({region})",
            "actual": f"{actual_location} ({region})",
            "message": (
                None
                if location_ok
                else f"Glue table reads {actual_location or 'no location'}, "
                f"the binding writes {expected_location}"
            ),
        },
    }


def _severity(
    schema: Mapping[str, Any], dimensions: Mapping[str, Any], expected_location: str
) -> Dict[str, Any]:
    """The shared drift grading, raised to CRITICAL by a moved table or a bad count."""
    from fluid_build.cli.verify import assess_drift_severity

    severity = assess_drift_severity(
        missing_fields=schema["missing"],
        extra_fields=schema["extra"],
        type_mismatches=schema["type_mismatches"],
        mode_mismatches=[],
        region_match=True,
    )
    problems: List[Tuple[str, str]] = []
    if dimensions["location"]["status"] == "fail":
        problems.append(
            (
                "Glue table location differs from the binding",
                f"Re-apply so the table reads {expected_location}, or fix binding.location",
            )
        )
    if dimensions["row_count"]["status"] == "fail":
        problems.append(
            (
                dimensions["row_count"]["message"],
                "Re-run the build and check its run record, then verify again",
            )
        )
    if not problems:
        if dimensions["row_count"]["status"] == "info" and severity["level"] == "SUCCESS":
            return {
                "level": "INFO",
                "impact": "LOW",
                "symbol": "🔵",
                "remediation": "NONE",
                "reason": dimensions["row_count"]["message"],
                "actions": ["Run the pipeline that owns the table, then verify again"],
            }
        return severity
    already = severity["level"] == "CRITICAL"
    return {
        "level": "CRITICAL",
        "impact": "HIGH",
        "symbol": "🔴",
        "remediation": "MANUAL_INTERVENTION_REQUIRED",
        "reason": "; ".join(([severity["reason"]] if already else []) + [p[0] for p in problems]),
        "actions": (list(severity["actions"]) if already else []) + [p[1] for p in problems],
    }


# ── Entry point ─────────────────────────────────────────────────────────


def _error(message: str, *, target: str, exists: Optional[bool], **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "error", "error": message, "target": target}
    if exists is not None:
        result["exists"] = exists
    result.update(extra)
    return result


def verify_athena_expose(
    expose_id: str,
    expose: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    workdir: Path,
    options: AthenaOptions,
    reference_only: bool = False,
    client_factory: Optional[ClientFactory] = None,
    sleep: Optional[Callable[[float], None]] = None,
    monotonic: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Verify one S3+Glue expose. Returns the shape the other verifiers return.

    ``status`` is ``match``, ``mismatch`` (with a ``severity``) or ``error``.
    Never raises for an AWS or configuration failure: the reason is the result.
    ``reference_only`` is ``verify.run``'s reading of the contract (a
    ``builds[].pattern`` that is a reference variant): a pipeline outside forge
    owns the rows, so an empty table with no run of this contract's own to
    compare with is INFO rather than CRITICAL, as a missing one already is.
    ``client_factory(service, region)``, ``sleep`` and ``monotonic`` default to
    boto3 and the ``time`` module, resolved at call time.
    """
    from fluid_build.providers.aws.util.warehouse import normalize_location

    binding = _resolved_binding(expose.get("binding"))
    loc = binding.get("location") or {}
    if not isinstance(loc, Mapping):
        loc = {}
    database = str(loc.get("database") or "")
    table = str(loc.get("table") or "")
    table_id = f"{database}.{table}"
    region, region_source = _resolve_region(binding, loc)
    # Say where the region came from when the binding did not name it.
    where = region or "no region"
    if region and not region_source.startswith("binding."):
        where += f" from {region_source}"
    target = f"{table_id} (Glue + Athena, {where})"

    problem = _preflight_problem(options, region, database, table, loc)
    if problem:
        return _error(problem, target=target, exists=None)
    bucket, path = normalize_location(loc, account_ref="")
    expected_location = f"s3://{bucket}/{path}"

    factory = client_factory or _boto3_client
    try:
        glue = factory("glue", region)
        athena = factory("athena", region)
    except AthenaVerifyError as exc:
        return _error(str(exc), target=target, exists=exc.exists)
    except Exception as exc:  # noqa: BLE001 — e.g. botocore refusing the region
        return _error(f"Could not create the AWS clients: {exc}", target=target, exists=None)

    try:
        glue_table = glue.get_table(DatabaseName=database, Name=table)["Table"]
    except Exception as exc:  # noqa: BLE001 — every AWS failure is reported, not raised
        if _error_code(exc) == "EntityNotFoundException":
            return _error(
                f"Glue table not found: {table_id} in {region}", target=target, exists=False
            )
        return _error(f"Glue GetTable {table_id} failed: {exc}", target=target, exists=None)

    schema = _compare_schema(glue_table, _declared_fields(expose))
    actual_location = str((glue_table.get("StorageDescriptor") or {}).get("Location") or "")
    dimensions = _catalogue_dimensions(schema, actual_location, expected_location, region)

    # The result must stay out of what Athena reads and of what the build writes.
    table_locations = [where for where in (actual_location, expected_location) if where]
    try:
        count, query = _count_rows(
            athena,
            database,
            table,
            options,
            bucket,
            table_locations,
            region,
            sleep=sleep or time.sleep,
            monotonic=monotonic or time.monotonic,
        )
    except AthenaVerifyError as exc:
        return _error(str(exc), target=target, exists=True, dimensions=dimensions)
    except Exception as exc:  # noqa: BLE001 — every AWS failure is reported, not raised
        return _error(
            f"Athena could not count {table_id}: {exc}",
            target=target,
            exists=True,
            dimensions=dimensions,
        )

    query["region_source"] = region_source
    try:
        landed, info = _landed_rows(contract, expose_id, workdir, expected_location)
    except Exception as exc:  # noqa: BLE001 — an unreadable record is not a count
        LOG.warning("verify_athena_run_record_unreadable error=%s", type(exc).__name__)
        landed, info = None, {"source": "none", "note": "the run record could not be read"}
    row_count = _row_count_dimension(count, landed, info, table_id, reference_only=reference_only)
    dimensions["row_count"] = row_count

    severity = _severity(schema, dimensions, expected_location)
    has_issues = any(
        dimensions[name]["status"] == "fail"
        for name in ("structure", "types", "location", "row_count")
    )
    created = glue_table.get("CreateTime")
    modified = glue_table.get("UpdateTime")
    return {
        "status": "mismatch" if has_issues else "match",
        "exists": True,
        "table_id": table_id,
        "target": target,
        "severity": severity,
        "dimensions": dimensions,
        "metadata": {
            "num_rows": count,
            "row_count_detail": row_count["message"],
            "created": created.isoformat() if hasattr(created, "isoformat") else None,
            "modified": modified.isoformat() if hasattr(modified, "isoformat") else None,
        },
        "athena": query,
    }
