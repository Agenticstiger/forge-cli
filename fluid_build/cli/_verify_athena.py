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
3. **Against the build.** When the build that lands this expose left a
   successful run record (``.fluid/runs/<product>/<build>/runs/*.json`` next
   to the contract, the same record the acquisition probes read), the count is
   held to that run's ``records_total``: equal for a ``full_refresh`` build, at
   least that many for ``incremental_append``, and reported without a gate for
   a merge, dedup, CDC or streaming build, whose count the last run does not
   determine. With no run record the count is reported and only an empty table
   fails. An empty table always fails: agreeing on zero is not agreement.

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

#: How the Athena count is held to the build's run record, by acquisition mode
#: (``_comparison_rule``). Recorded as ``dimensions.row_count.compared_with.rule``.
RULE_EQUAL = "equal"
RULE_AT_LEAST = "at_least"
RULE_REPORTED = "reported"

# Athena workgroup names: 1-128 of ``[a-zA-Z0-9._-]`` (Athena API reference,
# ``WorkGroupName``). It is an API parameter, not SQL, but a malformed one is
# better refused here with a clear message than by Athena with a generic one.
_WORKGROUP_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# ``s3://<bucket>/<optional key prefix>``: a DNS-style bucket name and a key
# with no whitespace or control characters.
_S3_URI_RE = re.compile(r"^s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9](/[^\s\x00-\x1f\x7f]*)?$")
# AWS region names (``eu-north-1``, ``us-gov-west-1``). The region becomes part
# of the endpoint host name, so a contract must not be able to put anything
# else there; botocore also refuses one, but by raising at client creation.
_REGION_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")

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
        if not _S3_URI_RE.match(output_location):
            problems.append(
                f"Athena output location {output_location!r} is not an s3://<bucket>/<prefix> URI "
                f"(--athena-output-location / {ENV_OUTPUT_LOCATION})"
            )
        elif not output_location.endswith("/"):
            output_location += "/"

    workgroup = (
        getattr(args, "athena_workgroup", None) or env.get(ENV_WORKGROUP) or DEFAULT_WORKGROUP
    ).strip()
    if not _WORKGROUP_RE.match(workgroup):
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


def _results_location(
    athena: Any, options: AthenaOptions, bucket: str
) -> Tuple[Optional[str], Optional[str], str]:
    """``(location to send, location in effect, source)`` for the query.

    ``location to send`` is ``None`` when the workgroup decides, so no
    ``ResultConfiguration`` is passed and Athena applies its own.
    """
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
            return None, None, "workgroup-managed"
        workgroup_output = (configuration.get("ResultConfiguration") or {}).get("OutputLocation")
        if configuration.get("EnforceWorkGroupConfiguration") and workgroup_output:
            return None, workgroup_output, "workgroup-enforced"
    if options.output_location:
        return options.output_location, options.output_location, "override"
    if workgroup_output:
        return None, workgroup_output, "workgroup"
    fallback = f"s3://{bucket}/{RESULTS_PREFIX}"
    if not _S3_URI_RE.match(fallback):
        raise AthenaVerifyError(
            f"binding.location.bucket {bucket!r} is not an S3 bucket name to write the Athena "
            f"result under; pass --athena-output-location or {ENV_OUTPUT_LOCATION}",
            exists=True,
        )
    return fallback, fallback, "binding-bucket"


def _stop_quietly(athena: Any, query_id: str) -> None:
    try:
        athena.stop_query_execution(QueryExecutionId=query_id)
    except Exception as exc:  # noqa: BLE001 — best effort; the error is already reported
        LOG.warning(
            "verify_athena_stop_failed query_execution_id=%s error=%s",
            query_id,
            _error_code(exc) or type(exc).__name__,
        )


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
                _stop_quietly(athena, query_id)
                raise AthenaVerifyError(
                    f"Athena query {query_id} did not finish within {timeout_seconds:g}s "
                    f"(last state {state or 'unknown'}); it was stopped. Raise the limit with "
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
    table_location: str,
    region: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> Tuple[int, Dict[str, Any]]:
    from fluid_build.providers._sql_safety import validate_ident

    # Identifier positions cannot be bound, so validate before interpolating;
    # ``validate_ident`` admits nothing that can close a double-quoted name.
    sql = f'SELECT COUNT(*) FROM "{validate_ident(database)}"."{validate_ident(table)}"'

    send, in_effect, source = _results_location(athena, options, bucket)
    if (
        send
        and table_location
        and (_normalize_s3(send) + "/").startswith(_normalize_s3(table_location) + "/")
    ):
        raise AthenaVerifyError(
            f"Athena would write its result to {send}, inside the table's own location "
            f"{table_location}, where it would be read back as data. Pass a location "
            f"outside it with --athena-output-location or {ENV_OUTPUT_LOCATION}.",
            exists=True,
        )
    request: Dict[str, Any] = {"QueryString": sql, "WorkGroup": options.workgroup}
    if send:
        request["ResultConfiguration"] = {"OutputLocation": send}

    query: Dict[str, Any] = {
        "workgroup": options.workgroup,
        "region": region,
        "sql": sql,
        "output_location": in_effect,
        "output_location_source": source,
    }
    query_id = str(athena.start_query_execution(**request)["QueryExecutionId"])
    query["query_execution_id"] = query_id
    LOG.info(
        "verify_athena_query_started query_execution_id=%s workgroup=%s region=%s "
        "output_location_source=%s",
        query_id,
        options.workgroup,
        region,
        source,
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


def _comparison_rule(build: Mapping[str, Any]) -> str:
    """How the table's count relates to what the build's last run landed.

    Only a full refresh replaces the table with exactly the run's rows. An
    append keeps every earlier run's rows too, so the run is a floor. A merge,
    dedup, CDC or streaming load can update or delete rows the run carried, so
    its count bounds nothing and is reported, not gated.
    """
    source = (build.get("properties") or {}).get("source") or {}
    mode = str(source.get("mode") or "full_refresh").lower() if isinstance(source, Mapping) else ""
    if mode == "full_refresh":
        return RULE_EQUAL
    if mode == "incremental_append":
        return RULE_AT_LEAST
    return RULE_REPORTED


def _landed_rows(
    contract: Mapping[str, Any], expose_id: str, workdir: Path
) -> Tuple[Optional[int], Dict[str, Any]]:
    """Rows the last successful run of the landing build wrote, when knowable."""
    from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier
    from fluid_build.cli._acquisition_stage_ext import latest_run_record

    builds = _landing_builds(contract, expose_id)
    if not builds:
        return None, {"source": "none", "note": "no build in the contract writes this expose"}
    if len(builds) > 1:
        ids = ", ".join(str(b.get("id")) for b in builds)
        return None, {
            "source": "none",
            "note": f"{len(builds)} builds write this expose ({ids}); no single run to compare",
        }
    try:
        product_id = validate_identifier(str(contract.get("id") or ""), kind="contract.id")
        build_id = validate_identifier(str(builds[0].get("id") or ""), kind="build.id")
    except IdentifierViolation:
        # The runner refuses these ids too, so it never wrote a record for them;
        # and a path built from them must not be read.
        return None, {"source": "none", "note": "contract or build id is not a valid identifier"}

    record = latest_run_record(workdir, product_id, build_id)
    if record is None:
        return None, {"source": "none", "build_id": build_id, "note": "no run record"}
    info: Dict[str, Any] = {
        "source": "run_record",
        "build_id": build_id,
        "run_id": record.get("run_id"),
        "state": record.get("state"),
        "finished_at": record.get("finished_at"),
        "rule": _comparison_rule(builds[0]),
    }
    if str(record.get("state") or "").lower() != "succeeded":
        info["source"] = "none"
        info["note"] = (
            f"the last run {record.get('run_id')} ended {record.get('state')}, so what it "
            "landed is not a count to hold the table to"
        )
        return None, info
    try:
        return int(record["records_total"]), info
    except (KeyError, TypeError, ValueError):
        info["source"] = "none"
        info["note"] = "the run record carries no records_total"
        return None, info


def _row_count_dimension(
    count: int, landed: Optional[int], info: Mapping[str, Any], table_id: str
) -> Dict[str, Any]:
    run = f"build {info.get('build_id')} run {info.get('run_id')}"
    rule = info.get("rule")
    if count == 0:
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
    elif landed is not None and rule == RULE_AT_LEAST and count < landed:
        message = (
            f"Athena counted {count:,} rows in {table_id}; {run} appended {landed:,}, "
            "so the table holds fewer rows than one run landed"
        )
        status = "fail"
    elif landed is not None and rule == RULE_EQUAL:
        message = f"{count:,} rows, equal to what {run} landed"
        status = "pass"
    elif landed is not None and rule == RULE_AT_LEAST:
        message = f"{count:,} rows, at least the {landed:,} that {run} appended"
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


def _preflight_problem(
    options: AthenaOptions, region: str, database: str, table: str, loc: Mapping[str, Any]
) -> Optional[str]:
    """Why the check cannot start, before any AWS call; ``None`` when it can."""
    from fluid_build.providers._sql_safety import validate_ident
    from fluid_build.providers.aws.util.warehouse import bucket_uses_fallback

    if options.problem:
        return options.problem
    if not region:
        return "No AWS region for this binding: set binding.location.region (or AWS_REGION)"
    if not _REGION_RE.match(region):
        return f"{region!r} is not an AWS region name"
    try:
        validate_ident(database)
        validate_ident(table)
    except ValueError as exc:
        return (
            f"Glue database/table {database + '.' + table!r} is not a safe identifier for "
            f"the count query: {exc}"
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
    client_factory: Optional[ClientFactory] = None,
    sleep: Optional[Callable[[float], None]] = None,
    monotonic: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Verify one S3+Glue expose. Returns the shape the other verifiers return.

    ``status`` is ``match``, ``mismatch`` (with a ``severity``) or ``error``.
    Never raises for an AWS or configuration failure: the reason is the result.
    ``client_factory(service, region)``, ``sleep`` and ``monotonic`` default to
    boto3 and the ``time`` module, resolved at call time.
    """
    from fluid_build.providers.aws.util.warehouse import normalize_location

    binding = expose.get("binding") or {}
    loc = binding.get("location") or {}
    database = str(loc.get("database") or "")
    table = str(loc.get("table") or "")
    table_id = f"{database}.{table}"
    region = str(
        loc.get("region")
        or binding.get("region")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ""
    )
    target = f"{table_id} (Glue + Athena, {region or 'no region'})"

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

    try:
        count, query = _count_rows(
            athena,
            database,
            table,
            options,
            bucket,
            actual_location,
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

    try:
        landed, info = _landed_rows(contract, expose_id, workdir)
    except Exception as exc:  # noqa: BLE001 — an unreadable record is not a count
        LOG.warning("verify_athena_run_record_unreadable error=%s", type(exc).__name__)
        landed, info = None, {"source": "none", "note": "the run record could not be read"}
    row_count = _row_count_dimension(count, landed, info, table_id)
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
