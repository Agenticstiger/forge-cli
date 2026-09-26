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

"""Stage 9 checks an S3+Glue binding through Glue and Athena.

``fluid verify`` answered "unsupported" for ``{platform: aws, format: parquet,
location: {bucket, path, database, table}}``: counted as *not checked*, never
failed under ``--strict``. So a build that landed nothing, or landed it where
Athena could not read it, passed stage 9. Measured on the demo contract below,
where the first real run left every byte in S3 and Athena refused the table.

Every test drives ``verify.run`` end to end (one calls the verifier with the
flag ``verify.run`` passes it), with the contract and overlay the
demo uses, and real botocore clients answered by ``botocore.stub.Stubber`` —
which also checks each request against the service model, so a request shape
Athena or Glue would reject fails here too. ``boto3.client`` is the only seam.

Marked ``emulated`` so the keyless ``emulated-integration`` CI lane, which has
boto3, runs them. Without boto3 the stubbed tests skip and the missing-extra
test still runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli.verify import register, run

try:
    import boto3
    from botocore.stub import Stubber
except ImportError:  # the aws extra is not installed
    boto3 = None
    Stubber = None

pytestmark = [pytest.mark.unit, pytest.mark.emulated]

_LOG = logging.getLogger("test_verify_athena")

PRODUCT = "bronze.customer_subscriptions"
BUILD = "ingest_subscriptions"
REGION = "eu-north-1"
DATA_LOCATION = "s3://northwind-demo-lake/bronze/customer_subscriptions/"
# The object the duckdb runner writes inside that prefix for the demo binding
# (``_resolve_destination_path``: the prefix plus ``<location.table>.parquet``).
DATA_FILE = DATA_LOCATION + "customer_subscriptions.parquet"
DEFAULT_RESULTS = "s3://northwind-demo-lake/.fluid/athena-results/"
COUNT_SQL = 'SELECT COUNT(*) FROM "demo_bronze"."customer_subscriptions"'

# The demo contract (fluid-demo-env contracts/customer_subscriptions), trimmed
# to what verify reads: an acquisition build whose output is the expose, and a
# declared schema in the SOURCE's type names.
CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
builds:
  - id: ingest_subscriptions
    pattern: acquisition
    engine: duckdb
    properties:
      source: {kind: postgres, mode: full_refresh, streams: [public.product_subscription]}
      sink: {format: parquet}
    outputs: [subscriptions]
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location: {path: ./out/customer_subscriptions.parquet}
    contract:
      schema:
        - {name: subscription_id, type: VARCHAR, required: true}
        - {name: customer_id, type: VARCHAR, required: true}
        - {name: product_id, type: VARCHAR, required: true}
        - {name: start_date, type: DATE, required: true}
        - {name: end_date, type: DATE, required: false}
        - {name: status, type: VARCHAR, required: true}
        - {name: msisdn, type: VARCHAR, required: false}
        - {name: created_at, type: TIMESTAMP, required: true}
"""

# The demo's overlays/aws.yaml, verbatim apart from its header comment.
AWS_OVERLAY = """\
exposes:
  - binding:
      platform: aws
      format: parquet
      location:
        database: demo_bronze
        table: customer_subscriptions
        bucket: northwind-demo-lake
        path: bronze/customer_subscriptions/
        region: eu-north-1
"""

# The Glue columns ``fluid apply`` declares for that schema (the emitter folds
# VARCHAR to string).
GLUE_COLUMNS = [
    {"Name": "subscription_id", "Type": "string"},
    {"Name": "customer_id", "Type": "string"},
    {"Name": "product_id", "Type": "string"},
    {"Name": "start_date", "Type": "date"},
    {"Name": "end_date", "Type": "date"},
    {"Name": "status", "Type": "string"},
    {"Name": "msisdn", "Type": "string"},
    {"Name": "created_at", "Type": "timestamp"},
]


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    """Nothing here may reach an account, read a profile, or pick a region."""
    for var in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FLUID_ATHENA_OUTPUT_LOCATION",
        "FLUID_ATHENA_WORKGROUP",
        "FLUID_ATHENA_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # The poll loop sleeps between GetQueryExecution calls; no test waits.
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


class _Aws:
    """Stubbed Glue and Athena clients handed out by a patched ``boto3.client``."""

    def __init__(self) -> None:
        creds = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
        self.glue = boto3.client("glue", region_name=REGION, **creds)
        self.athena = boto3.client("athena", region_name=REGION, **creds)
        self.glue_stub = Stubber(self.glue)
        self.athena_stub = Stubber(self.athena)
        self.requested: List[tuple] = []

    def client(self, service: str, *args: Any, **kwargs: Any) -> Any:
        self.requested.append((service, kwargs.get("region_name")))
        return {"glue": self.glue, "athena": self.athena}[service]

    # Glue ------------------------------------------------------------------
    def table(
        self,
        columns: Optional[List[Dict[str, str]]] = None,
        location: str = DATA_LOCATION,
        partition_keys: Optional[List[Dict[str, str]]] = None,
        name: str = "customer_subscriptions",
    ) -> None:
        self.glue_stub.add_response(
            "get_table",
            {
                "Table": {
                    "Name": name,
                    "DatabaseName": "demo_bronze",
                    "TableType": "EXTERNAL_TABLE",
                    "StorageDescriptor": {
                        "Columns": GLUE_COLUMNS if columns is None else columns,
                        "Location": location,
                    },
                    "PartitionKeys": partition_keys or [],
                }
            },
            {"DatabaseName": "demo_bronze", "Name": name},
        )

    def no_table(self) -> None:
        self.glue_stub.add_client_error(
            "get_table",
            service_error_code="EntityNotFoundException",
            service_message="Table customer_subscriptions not found.",
            expected_params={"DatabaseName": "demo_bronze", "Name": "customer_subscriptions"},
        )

    # Athena ----------------------------------------------------------------
    def workgroup(self, configuration: Optional[Dict[str, Any]] = None, name="primary") -> None:
        self.athena_stub.add_response(
            "get_work_group",
            {"WorkGroup": {"Name": name, "Configuration": configuration or {}}},
            {"WorkGroup": name},
        )

    def start(
        self, output_location: Optional[str] = DEFAULT_RESULTS, workgroup="primary", sql=COUNT_SQL
    ):
        expected: Dict[str, Any] = {"QueryString": sql, "WorkGroup": workgroup}
        if output_location is not None:
            expected["ResultConfiguration"] = {"OutputLocation": output_location}
        self.athena_stub.add_response(
            "start_query_execution", {"QueryExecutionId": "q-1"}, expected
        )

    def state(self, state: str, reason: Optional[str] = None) -> None:
        status: Dict[str, Any] = {"State": state}
        if reason:
            status["StateChangeReason"] = reason
        self.athena_stub.add_response(
            "get_query_execution",
            {
                "QueryExecution": {
                    "QueryExecutionId": "q-1",
                    "Status": status,
                    "Statistics": {"DataScannedInBytes": 0, "EngineExecutionTimeInMillis": 412},
                }
            },
            {"QueryExecutionId": "q-1"},
        )

    def count(self, rows: int) -> None:
        self.athena_stub.add_response(
            "get_query_results",
            {
                "ResultSet": {
                    "Rows": [
                        {"Data": [{"VarCharValue": "_col0"}]},
                        {"Data": [{"VarCharValue": str(rows)}]},
                    ]
                }
            },
            {"QueryExecutionId": "q-1", "MaxResults": 2},
        )

    def counts(self, rows: int, **start: Any) -> None:
        """The whole happy query: workgroup, start, queued, running, done, count."""
        self.workgroup()
        self.start(**start)
        self.state("QUEUED")
        self.state("RUNNING")
        self.state("SUCCEEDED")
        self.count(rows)

    def __enter__(self) -> "_Aws":
        self.glue_stub.activate()
        self.athena_stub.activate()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.glue_stub.deactivate()
        self.athena_stub.deactivate()

    def assert_all_called(self) -> None:
        self.glue_stub.assert_no_pending_responses()
        self.athena_stub.assert_no_pending_responses()


@pytest.fixture
def aws(monkeypatch):
    if boto3 is None:
        pytest.skip("botocore's Stubber comes with the aws extra")
    fake = _Aws()
    monkeypatch.setattr(boto3, "client", fake.client)
    with fake:
        yield fake


def _write_contract(tmp_path: Path, overlay: str = AWS_OVERLAY, contract: str = CONTRACT) -> Path:
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "aws.yaml").write_text(overlay, encoding="utf-8")
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(contract, encoding="utf-8")
    return path


def _with_mode(mode: str) -> str:
    """The demo contract with its build's acquisition mode changed."""
    text = CONTRACT.replace("mode: full_refresh", f"mode: {mode}")
    assert text != CONTRACT
    return text


def _write_run_record(
    tmp_path: Path,
    records_total: int,
    state: str = "succeeded",
    *,
    run_id: str = "0101M37Z8SDZ06AJXT",
    mode: str = "full_refresh",
    destination: str = DATA_FILE,
    rows_from: Optional[str] = "write",
    landed: bool = True,
) -> None:
    """A run record as the duckdb runner writes it (``execute_duckdb_build``).

    ``facets.landed`` is what the runner records about where the rows went, in
    which mode, and whether ``records_total`` is the write's own count. Run ids
    sort by time, so a smaller id is an older run.
    """
    runs = tmp_path / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stream = "public.product_subscription"
    facets: Dict[str, Any] = {"engine": "duckdb", "duration_seconds": 1.2}
    if landed:
        facets["landed"] = {"mode": mode, "destinations": {stream: destination}}
        if rows_from is not None:
            facets["landed"]["rows_from"] = rows_from
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "state": state,
                "records_total": records_total,
                "finished_at": "2026-09-23T20:28:16Z",
                "streams": [{"name": stream, "records": records_total}],
                "facets": facets,
            }
        ),
        encoding="utf-8",
    )


def _verify(tmp_path: Path, contract_path: Path, **flags: Any):
    """Run ``fluid verify --env aws`` and return ``(exit code, report)``."""
    out = tmp_path / "report.json"
    args = argparse.Namespace(
        contract=str(contract_path),
        expose_id=None,
        strict=flags.pop("strict", True),
        fail_on_warning=flags.pop("fail_on_warning", False),
        out=str(out),
        show_diffs=False,
        env="aws",
        **flags,
    )
    code = run(args, _LOG)
    return code, json.loads(out.read_text(encoding="utf-8"))


# ── Row count ───────────────────────────────────────────────────────────


def test_counts_through_athena_and_matches_the_build(tmp_path, aws, capsys):
    """The demo's own check, now stage 9's: the table serves what the build landed."""
    contract = _write_contract(tmp_path)
    _write_run_record(tmp_path, 10172)
    aws.table()
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0
    assert result["status"] == "match"
    assert result["metadata"]["num_rows"] == 10172
    row_count = result["dimensions"]["row_count"]
    assert row_count["status"] == "pass"
    assert row_count["expected"] == 10172
    assert row_count["compared_with"]["run_id"] == "0101M37Z8SDZ06AJXT"
    assert row_count["compared_with"]["rule"] == "equal"  # a full_refresh build
    assert result["athena"]["output_location"] == DEFAULT_RESULTS
    assert result["athena"]["output_location_source"] == "binding-bucket"
    # Both clients in the binding's region, not whatever the environment says.
    assert set(aws.requested) == {("glue", REGION), ("athena", REGION)}
    assert report["summary"]["match"] == 1 and report["summary"]["unsupported"] == 0
    aws.assert_all_called()
    out = " ".join(capsys.readouterr().out.split())  # rich wraps at the terminal width
    assert "equal to what build ingest_subscriptions run 0101M37Z8SDZ06AJXT landed" in out
    assert "demo_bronze.customer_subscriptions" in out


def test_a_count_that_differs_from_the_build_fails_strict(tmp_path, aws):
    contract = _write_contract(tmp_path)
    _write_run_record(tmp_path, 10172)
    aws.table()
    aws.counts(9000)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "CRITICAL"
    assert result["dimensions"]["row_count"]["status"] == "fail"
    assert "9,000" in result["severity"]["reason"] and "10,172" in result["severity"]["reason"]


def test_an_empty_table_with_no_run_record_fails_strict(tmp_path, aws):
    """No run record: report the count, and fail only when there is nothing."""
    contract = _write_contract(tmp_path)
    aws.table()
    aws.counts(0)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["severity"]["level"] == "CRITICAL"
    assert result["dimensions"]["row_count"] == {
        "status": "fail",
        "actual": 0,
        "expected": None,
        "compared_with": {"source": "none", "build_id": BUILD, "note": "no run record"},
        "message": "Athena counted 0 rows in demo_bronze.customer_subscriptions",
    }


def test_an_empty_table_fails_even_when_the_build_landed_nothing(tmp_path, aws):
    """Agreeing on zero is not agreement (the demo's verify_cloud.py)."""
    contract = _write_contract(tmp_path)
    _write_run_record(tmp_path, 0)
    aws.table()
    aws.counts(0)

    code, report = _verify(tmp_path, contract)

    assert code == 1
    assert report["results"]["subscriptions"]["dimensions"]["row_count"]["status"] == "fail"


def test_a_populated_table_with_no_run_record_is_reported_and_passes(tmp_path, aws):
    contract = _write_contract(tmp_path)
    aws.table()
    aws.counts(42)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0
    assert result["status"] == "match"
    assert result["dimensions"]["row_count"]["expected"] is None
    assert "not compared with a build" in result["dimensions"]["row_count"]["message"]


def test_a_failed_last_run_is_not_the_count_to_compare_with(tmp_path, aws):
    contract = _write_contract(tmp_path)
    _write_run_record(tmp_path, 3, state="failed")
    aws.table()
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert row_count["status"] == "pass" and row_count["expected"] is None
    assert "ended failed" in row_count["compared_with"]["note"]


def test_an_appending_build_holds_the_table_to_at_least_its_run(tmp_path, aws):
    """An append keeps earlier runs' rows, so the last run is a floor, not a count."""
    contract = _write_contract(tmp_path, contract=_with_mode("incremental_append"))
    _write_run_record(tmp_path, 120, mode="incremental_append")
    aws.table()
    aws.counts(10292)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["status"] == "pass"
    assert row_count["compared_with"]["rule"] == "at_least_cumulative"

    aws.table()
    aws.counts(100)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 1
    assert row_count["status"] == "fail"
    assert "fewer rows than its runs landed" in row_count["message"]


def test_a_merging_build_is_reported_not_gated(tmp_path, aws):
    """A merge updates rows in place, so its run's count bounds nothing."""
    contract = _write_contract(tmp_path, contract=_with_mode("incremental_merge"))
    _write_run_record(tmp_path, 500, mode="incremental_merge")
    aws.table()
    aws.counts(42)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0
    assert row_count["status"] == "pass"
    assert row_count["compared_with"]["rule"] == "reported"
    assert "not compared" in row_count["message"]


# ── Glue ────────────────────────────────────────────────────────────────


def test_a_missing_glue_table_is_an_error_and_athena_is_not_asked(tmp_path, aws):
    """Not drift: the object the contract names is not there. Fatal without --strict."""
    contract = _write_contract(tmp_path)
    aws.no_table()  # no Athena responses queued: any Athena call fails the test

    code, report = _verify(tmp_path, contract, strict=False)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "error"
    assert result["exists"] is False
    assert "Glue table not found: demo_bronze.customer_subscriptions" in result["error"]
    assert report["summary"]["error"] == 1
    aws.assert_all_called()


def test_a_changed_type_and_a_missing_column_are_critical(tmp_path, aws):
    contract = _write_contract(tmp_path)
    columns = [dict(c) for c in GLUE_COLUMNS if c["Name"] != "msisdn"]
    columns[1]["Type"] = "bigint"  # customer_id
    aws.table(columns=columns)
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["severity"]["level"] == "CRITICAL"
    dims = result["dimensions"]
    assert dims["types"]["mismatches"] == [
        {"field": "customer_id", "expected": "string", "actual": "bigint"}
    ]
    assert [f["field"] for f in dims["structure"]["missing_fields"]] == ["msisdn"]


def test_an_extra_column_is_info_so_only_fail_on_warning_gates_it(tmp_path, aws):
    contract = _write_contract(tmp_path)
    extra = GLUE_COLUMNS + [{"Name": "ingested_at", "Type": "timestamp"}]
    aws.table(columns=extra)
    aws.counts(10172)
    code, report = _verify(tmp_path, contract)
    assert code == 0
    assert report["results"]["subscriptions"]["severity"]["level"] == "INFO"

    aws.table(columns=extra)
    aws.counts(10172)
    code, _ = _verify(tmp_path, contract, fail_on_warning=True)
    assert code == 1


def test_type_spelling_and_column_case_are_not_drift(tmp_path, aws):
    """``INTEGER``/``int`` and ``decimal(10, 2)``/``decimal(10,2)`` are one type."""
    contract_text = CONTRACT.replace(
        "        - {name: created_at, type: TIMESTAMP, required: true}\n",
        "        - {name: created_at, type: TIMESTAMP, required: true}\n"
        "        - {name: seats, type: integer}\n"
        "        - {name: price, type: 'decimal(10,2)'}\n",
    )
    contract = _write_contract(tmp_path, contract=contract_text)
    columns = [dict(c, Name=c["Name"].upper()) for c in GLUE_COLUMNS] + [
        {"Name": "seats", "Type": "INTEGER"},
    ]
    # A partition key is a column of the table as Athena sees it.
    aws.table(columns=columns, partition_keys=[{"Name": "price", "Type": "decimal(10, 2)"}])
    aws.counts(1)

    code, report = _verify(tmp_path, contract)

    assert code == 0, report["results"]["subscriptions"]
    assert report["results"]["subscriptions"]["status"] == "match"


def test_a_table_reading_another_prefix_is_critical(tmp_path, aws):
    contract = _write_contract(tmp_path)
    aws.table(location="s3://northwind-demo-lake/bronze/old_subscriptions/")
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["dimensions"]["location"]["status"] == "fail"
    assert "bronze/old_subscriptions" in result["dimensions"]["location"]["message"]


# ── Athena ──────────────────────────────────────────────────────────────


def test_a_failed_athena_query_is_an_error_with_athenas_reason(tmp_path, aws):
    """The failure the first real run hit: Glue had the table, Athena refused it."""
    contract = _write_contract(tmp_path)
    reason = "HIVE_UNSUPPORTED_FORMAT: Unable to create input format"
    aws.table()
    aws.workgroup()
    aws.start()
    aws.state("RUNNING")
    aws.state("FAILED", reason=reason)

    code, report = _verify(tmp_path, contract, strict=False)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "error"
    assert reason in result["error"] and "q-1 FAILED" in result["error"]
    # What Glue said is still in the report.
    assert result["dimensions"]["structure"]["status"] == "pass"
    aws.assert_all_called()


def test_a_query_past_the_timeout_is_stopped_and_fails(tmp_path, aws):
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup()
    aws.start()
    aws.state("RUNNING")
    aws.athena_stub.add_response("stop_query_execution", {}, {"QueryExecutionId": "q-1"})

    code, report = _verify(tmp_path, contract, strict=False, athena_timeout=1e-9)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "error"
    assert "did not finish within" in result["error"] and "was stopped" in result["error"]
    aws.assert_all_called()


def test_without_boto3_the_error_names_the_extra(tmp_path, monkeypatch):
    contract = _write_contract(tmp_path)
    monkeypatch.setitem(sys.modules, "boto3", None)  # import boto3 -> ImportError

    code, report = _verify(tmp_path, contract, strict=False)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "error"
    assert "pip install 'data-product-forge[aws]'" in result["error"]


def test_no_region_anywhere_is_an_error(tmp_path, aws):
    overlay = AWS_OVERLAY.replace("        region: eu-north-1\n", "")
    contract = _write_contract(tmp_path, overlay=overlay)

    code, report = _verify(tmp_path, contract, strict=False)

    assert code == 1
    assert "No AWS region" in report["results"]["subscriptions"]["error"]
    assert aws.requested == []


def test_the_flags_are_on_the_verify_parser():
    """The interface a CI generator writes: three flags, each optional."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    args = parser.parse_args(
        [
            "verify",
            "contract.fluid.yaml",
            "--athena-output-location",
            "s3://ops-results/verify/",
            "--athena-workgroup",
            "ci",
            "--athena-timeout",
            "90",
        ]
    )
    assert args.athena_output_location == "s3://ops-results/verify/"
    assert args.athena_workgroup == "ci"
    assert args.athena_timeout == 90.0
    defaults = parser.parse_args(["verify", "contract.fluid.yaml"])
    assert defaults.athena_output_location is None
    assert defaults.athena_workgroup is None
    assert defaults.athena_timeout is None


# ── Where the result goes ───────────────────────────────────────────────

_OVERRIDE = "s3://ops-athena-results/verify/"
_WG_OUTPUT = "s3://team-athena-results/primary/"


@pytest.mark.parametrize(
    ("workgroup", "flags", "env", "sent", "source"),
    [
        pytest.param(
            {"ResultConfiguration": {"OutputLocation": _WG_OUTPUT}},
            {},
            {},
            None,
            "workgroup",
            id="workgroup-location",
        ),
        pytest.param(
            {
                "ResultConfiguration": {"OutputLocation": _WG_OUTPUT},
                "EnforceWorkGroupConfiguration": True,
            },
            {"athena_output_location": _OVERRIDE},
            {},
            None,
            "workgroup-enforced",
            id="enforced-workgroup-beats-override",
        ),
        pytest.param(
            {"ManagedQueryResultsConfiguration": {"Enabled": True}},
            {},
            {},
            None,
            "workgroup-managed",
            id="managed-results",
        ),
        pytest.param(
            {"ResultConfiguration": {"OutputLocation": _WG_OUTPUT}},
            {"athena_output_location": _OVERRIDE},
            {},
            _OVERRIDE,
            "override",
            id="flag-override",
        ),
        pytest.param(
            {},
            {},
            {"FLUID_ATHENA_OUTPUT_LOCATION": "s3://ops-athena-results/verify"},
            _OVERRIDE,
            "override",
            id="env-override",
        ),
    ],
)
def test_the_results_location(tmp_path, aws, monkeypatch, workgroup, flags, env, sent, source):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup(workgroup)
    aws.start(output_location=sent)  # Stubber fails on any other ResultConfiguration
    aws.state("SUCCEEDED")
    aws.count(10172)

    code, report = _verify(tmp_path, contract, **flags)

    assert code == 0, report["results"]["subscriptions"]
    assert report["results"]["subscriptions"]["athena"]["output_location_source"] == source
    aws.assert_all_called()


def test_an_unreadable_workgroup_falls_back_to_the_bucket(tmp_path, aws):
    contract = _write_contract(tmp_path)
    aws.table()
    aws.athena_stub.add_client_error(
        "get_work_group", service_error_code="AccessDeniedException", http_status_code=400
    )
    aws.start(output_location=DEFAULT_RESULTS)
    aws.state("SUCCEEDED")
    aws.count(5)

    code, report = _verify(tmp_path, contract)

    assert code == 0
    assert report["results"]["subscriptions"]["athena"]["output_location_source"] == (
        "binding-bucket"
    )


def test_the_workgroup_flag_and_env_are_used(tmp_path, aws, monkeypatch):
    monkeypatch.setenv("FLUID_ATHENA_WORKGROUP", "from-env")
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup(name="from-flag")
    aws.start(workgroup="from-flag")
    aws.state("SUCCEEDED")
    aws.count(5)

    code, _ = _verify(tmp_path, contract, athena_workgroup="from-flag")

    assert code == 0
    aws.assert_all_called()


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({"athena_output_location": "https://example.com/results/"}, id="not-s3"),
        pytest.param({"athena_output_location": "s3://ops-results/x\ny/"}, id="control-char"),
        pytest.param({"athena_output_location": "s3://B/"}, id="bad-bucket"),
        pytest.param({"athena_workgroup": "primary; DROP"}, id="bad-workgroup"),
        pytest.param({"athena_timeout": 0}, id="zero-timeout"),
    ],
)
def test_an_invalid_option_is_an_error_before_any_aws_call(tmp_path, aws, flags):
    contract = _write_contract(tmp_path)  # nothing queued: any AWS call fails

    code, report = _verify(tmp_path, contract, **flags)

    assert code == 1
    assert report["results"]["subscriptions"]["status"] == "error"
    assert aws.requested == []


def test_results_are_never_written_inside_the_table(tmp_path, aws):
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup()

    code, report = _verify(
        tmp_path,
        contract,
        athena_output_location="s3://northwind-demo-lake/bronze/customer_subscriptions/tmp/",
    )

    assert code == 1
    assert "inside the table's own location" in report["results"]["subscriptions"]["error"]


# ── What is and is not claimed ──────────────────────────────────────────


def test_an_unsafe_table_name_never_reaches_athena(tmp_path, aws):
    overlay = AWS_OVERLAY.replace("table: customer_subscriptions", "table: 'x\"; DROP TABLE y; --'")
    contract = _write_contract(tmp_path, overlay=overlay)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 1
    assert result["status"] == "error"
    assert "not a safe identifier" in result["error"]
    assert aws.requested == []


def test_a_region_that_is_not_a_region_never_reaches_boto3(tmp_path, aws):
    """The region becomes part of the endpoint host name."""
    overlay = AWS_OVERLAY.replace("region: eu-north-1", "region: 'evil.example.com/x'")
    contract = _write_contract(tmp_path, overlay=overlay)

    code, report = _verify(tmp_path, contract)

    assert code == 1
    assert "is not an AWS region name" in report["results"]["subscriptions"]["error"]
    assert aws.requested == []


def test_only_formats_athena_can_read_are_claimed(tmp_path, aws):
    """Parquet has Hive storage classes in the emitter; CSV does not, so a CSV
    Glue table is still reported as not checked rather than failed."""
    contract_text = CONTRACT + (
        "  - exposeId: raw_csv\n"
        "    kind: table\n"
        "    binding:\n"
        "      platform: aws\n"
        "      format: csv\n"
        "      location:\n"
        "        database: demo_bronze\n"
        "        table: raw_csv\n"
        "        bucket: northwind-demo-lake\n"
        "        path: bronze/raw_csv/\n"
        "        region: eu-north-1\n"
    )
    contract = _write_contract(tmp_path, contract=contract_text)
    aws.table()
    aws.counts(7)

    code, report = _verify(tmp_path, contract)

    assert code == 0
    assert report["results"]["subscriptions"]["status"] == "match"
    assert report["results"]["raw_csv"]["status"] == "unsupported"
    aws.assert_all_called()


# ── Which runs a table is held to ───────────────────────────────────────

# The demo contract with a filesystem source instead of Postgres, so the real
# duckdb runner can run it here. Same product, build and expose ids.
_FS_CONTRACT = CONTRACT.replace(
    "      source: {kind: postgres, mode: full_refresh, streams: [public.product_subscription]}\n",
    "      source:\n"
    "        kind: filesystem\n"
    "        mode: full_refresh\n"
    "        connection: {uri: SOURCE_CSV}\n"
    "        reader: {format: csv}\n"
    "        streams: [public.product_subscription]\n",
)


def _fs_contract(tmp_path: Path, rows: int) -> Path:
    """The filesystem-source contract, with ``rows`` rows in its source CSV."""
    source = tmp_path / "source.csv"
    source.write_text(
        "subscription_id\n" + "".join(f"s{i}\n" for i in range(rows)), encoding="utf-8"
    )
    assert _FS_CONTRACT != CONTRACT
    return _write_contract(tmp_path, contract=_FS_CONTRACT.replace("SOURCE_CSV", str(source)))


def _run_duckdb_build(contract_path: Path, env: Optional[str]) -> Dict[str, Any]:
    """Run the build with the real duckdb runner; return the run record it wrote."""
    from fluid_build.build_runners.duckdb.runner import execute_duckdb_build
    from fluid_build.cli._common import load_contract_with_overlay

    contract = load_contract_with_overlay(str(contract_path), env, _LOG)
    runs = contract_path.parent / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    before = set(runs.glob("*.json")) if runs.is_dir() else set()
    assert execute_duckdb_build(contract["builds"][0], contract, contract_path.parent) == 0
    (written,) = set(runs.glob("*.json")) - before
    return json.loads(written.read_text(encoding="utf-8"))


def test_a_dbt_build_is_reported_not_held_to_its_node_count(tmp_path, aws):
    """The dbt runner's records_total is the number of dbt nodes, not rows: a model
    and two tests is 3. Held to it as a full refresh, a healthy 10,172-row table
    failed --strict as CRITICAL."""
    from fluid_build.build_runners.dbt.runner import _persist_dbt_run_record

    contract_text = CONTRACT.replace(
        "  - id: ingest_subscriptions\n"
        "    pattern: acquisition\n"
        "    engine: duckdb\n"
        "    properties:\n"
        "      source: {kind: postgres, mode: full_refresh, streams: [public.product_subscription]}\n"
        "      sink: {format: parquet}\n",
        "  - id: model_subscriptions\n"
        "    pattern: transformation\n"
        "    engine: dbt\n"
        "    properties: {model: customer_subscriptions}\n",
    )
    assert contract_text != CONTRACT
    contract = _write_contract(tmp_path, contract=contract_text)
    project = tmp_path / "dbt"
    (project / "target").mkdir(parents=True)
    (project / "target" / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"dbt_version": "1.9.0"},
                "elapsed_time": 3.2,
                "results": [
                    {"unique_id": "model.nw.customer_subscriptions", "status": "success"},
                    {"unique_id": "test.nw.not_null_subscription_id", "status": "pass"},
                    {"unique_id": "test.nw.unique_subscription_id", "status": "pass"},
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = _persist_dbt_run_record(
        {"id": "model_subscriptions", "engine": "dbt"},
        project,
        tmp_path,
        returncode=0,
        started_at="2026-09-25T10:00:00Z",
        finished_at="2026-09-25T10:00:03Z",
        duration_seconds=3.2,
        product_id=PRODUCT,
    )
    assert run_id, "the dbt runner wrote no run record"
    aws.table()
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["status"] == "pass"
    assert row_count["expected"] is None
    assert row_count["compared_with"]["rule"] == "reported"
    assert "transformation build" in row_count["compared_with"]["note"]


def test_a_local_run_from_the_same_directory_is_not_held_against_the_cloud_table(tmp_path, aws):
    """One contract directory serves every overlay, so its run records mix targets.
    A local run of 105 rows was held against the 100 the AWS run landed in S3."""
    pytest.importorskip("duckdb")
    contract = _fs_contract(tmp_path, rows=105)
    local_run = _run_duckdb_build(contract, env=None)  # the base binding: a local file
    assert local_run["records_total"] == 105
    aws.table()
    aws.counts(100)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] is None
    assert "none of the build's 1 recorded runs landed in " + DATA_LOCATION.rstrip("/") in (
        row_count["compared_with"]["note"]
    )

    # With the earlier AWS run on record, the table is held to that run.
    _write_run_record(tmp_path, 100, run_id="0100000000000000AA")
    aws.table()
    aws.counts(100)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] == 100
    assert row_count["compared_with"]["run_id"] == "0100000000000000AA"
    assert row_count["compared_with"]["other_target_runs_skipped"] == 1

    aws.table()
    aws.counts(90)
    code, report = _verify(tmp_path, contract)
    assert code == 1
    assert report["results"]["subscriptions"]["severity"]["level"] == "CRITICAL"


def test_an_append_is_held_to_every_run_since_the_last_full_load(tmp_path, aws):
    """The last run alone is no floor: a table that lost every earlier run's rows
    still holds as many as the last run appended. The duckdb runner does exactly
    that to an S3 prefix, rewriting <prefix>/<table>.parquet on every run."""
    contract = _write_contract(tmp_path, contract=_with_mode("incremental_append"))
    _write_run_record(tmp_path, 7, run_id="0101M30000000000A1", mode="incremental_append")
    _write_run_record(tmp_path, 100, run_id="0101M30000000000A2", mode="full_refresh")
    _write_run_record(
        tmp_path,
        5,
        run_id="0101M30000000000A3",
        mode="incremental_append",
        destination="out/customer_subscriptions.parquet",  # a local run: another target
    )
    _write_run_record(tmp_path, 5, run_id="0101M30000000000A4", mode="incremental_append")
    aws.table()
    aws.counts(5)  # what is left when each run overwrites the last one's file

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    row_count = result["dimensions"]["row_count"]
    assert code == 1, row_count
    assert result["severity"]["level"] == "CRITICAL"
    assert row_count["expected"] == 105
    assert row_count["compared_with"]["rule"] == "at_least_cumulative"
    assert row_count["compared_with"]["runs"] == ["0101M30000000000A4", "0101M30000000000A2"]
    assert row_count["compared_with"]["reached_full_load"] is True
    assert "since the last full load" in row_count["message"]
    assert "runs 0101M30000000000A2 to 0101M30000000000A4 (2 runs)" in row_count["message"]

    # The append before the full refresh is not part of the floor (112 would fail).
    aws.table()
    aws.counts(106)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["status"] == "pass"


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"rows_from": "source_count"}, id="counted-by-a-second-read"),
        pytest.param({"rows_from": None}, id="no-rows-from"),
        pytest.param({"landed": False}, id="written-before-runs-recorded-it"),
    ],
)
def test_a_run_that_did_not_count_at_the_write_is_reported_not_gated(tmp_path, aws, record):
    """records_total from a second read of a live source is larger than what the
    COPY wrote, so a table holding exactly what landed failed as CRITICAL. And a
    record that does not say where it landed may be another target's run."""
    contract = _write_contract(tmp_path)
    _write_run_record(tmp_path, 10172, **record)
    aws.table()
    aws.counts(9000)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] is None
    assert "not compared with a build" in row_count["message"]


def test_a_table_holding_what_the_copy_wrote_passes_though_the_source_grew(
    tmp_path, aws, monkeypatch
):
    """End to end, the demo's shape: the real duckdb runner lands an AWS binding
    while the source (a live Postgres in the demo) grows. The run record must say
    what the COPY wrote, and where, for stage 9 to hold the table to it."""
    duckdb = pytest.importorskip("duckdb")
    from fluid_build.build_runners.duckdb import runner as duckdb_runner

    contract = _fs_contract(tmp_path, rows=100)
    source = tmp_path / "source.csv"
    s3_object = tmp_path / "s3-object.parquet"
    real_connect = duckdb.connect

    class _S3AndALiveSource:
        """A DuckDB connection whose COPY to the binding's S3 object lands in a
        local file, after which the source gains five rows."""

        def __init__(self, con: Any) -> None:
            self._con = con

        def __getattr__(self, name: str) -> Any:
            return getattr(self._con, name)

        def execute(self, sql: str, *args: Any) -> Any:
            if sql.startswith("COPY") and DATA_FILE in sql:
                result = self._con.execute(sql.replace(DATA_FILE, str(s3_object)), *args)
                with source.open("a", encoding="utf-8") as fh:
                    fh.write("".join(f"late{i}\n" for i in range(5)))
                return result
            return self._con.execute(sql, *args)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _S3AndALiveSource(real_connect(*a, **k)))
    # No httpfs or S3 secret: nothing here reaches S3.
    monkeypatch.setattr(duckdb_runner.DuckdbRunner, "_load_extensions", lambda *_: None)

    record = _run_duckdb_build(contract, env="aws")

    landed = real_connect().execute(f"SELECT COUNT(*) FROM '{s3_object}'").fetchone()[0]
    assert landed == 100
    assert record["records_total"] == 100
    assert record["facets"]["landed"] == {
        "mode": "full_refresh",
        "rows_from": "write",
        "destinations": {"public.product_subscription": DATA_FILE},
    }
    aws.table()
    aws.counts(100)  # Athena reads exactly what the COPY wrote

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] == 100
    assert row_count["compared_with"]["rule"] == "equal"
    assert row_count["compared_with"]["run_id"] == record["run_id"]


# ── Region and names ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "region", "target"),
    [
        pytest.param(
            {"DEMO_REGION": "eu-north-1", "AWS_REGION": "us-east-1"},
            "eu-north-1",
            "(Glue + Athena, eu-north-1)",
            id="template-resolved",
        ),
        pytest.param(
            {"AWS_REGION": "eu-north-1"},
            "eu-north-1",
            "(Glue + Athena, eu-north-1 from AWS_REGION)",
            id="template-unset-falls-back-like-apply",
        ),
    ],
)
def test_a_templated_region_is_resolved_not_refused(
    tmp_path, aws, monkeypatch, env, region, target
):
    """``fluid apply`` accepts ``region: '{{ env.X }}'``: the emitter passes over a
    value that is not a region code and deploys in the environment's region."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    overlay = AWS_OVERLAY.replace("region: eu-north-1", "region: '{{ env.DEMO_REGION }}'")
    contract = _write_contract(tmp_path, overlay=overlay)
    aws.table()
    aws.counts(42)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0, result
    assert result["status"] == "match"
    assert set(aws.requested) == {("glue", region), ("athena", region)}
    assert result["target"].endswith(target)


def test_the_region_in_the_aws_config_profile_is_used(tmp_path, aws, monkeypatch):
    """Credentials came from boto3's default chain but the region did not, so a
    region set only in ~/.aws/config was "No AWS region"."""
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = eu-north-1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    overlay = AWS_OVERLAY.replace("        region: eu-north-1\n", "")
    contract = _write_contract(tmp_path, overlay=overlay)
    aws.table()
    aws.counts(42)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0, result
    assert set(aws.requested) == {("glue", REGION), ("athena", REGION)}
    assert result["target"].endswith("(Glue + Athena, eu-north-1 from the AWS config profile)")
    assert result["athena"]["region_source"] == "the AWS config profile"


def test_a_table_name_starting_with_a_digit_is_counted_quoted(tmp_path, aws):
    """Athena takes such a name in a SELECT when it is double-quoted, and the
    count query always quotes; ``fluid apply`` creates the table."""
    overlay = AWS_OVERLAY.replace("table: customer_subscriptions", "table: 2024_subscriptions")
    contract = _write_contract(tmp_path, overlay=overlay)
    aws.table(name="2024_subscriptions")
    aws.workgroup()
    aws.start(sql='SELECT COUNT(*) FROM "demo_bronze"."2024_subscriptions"')
    aws.state("SUCCEEDED")
    aws.count(42)

    code, report = _verify(tmp_path, contract)

    assert code == 0, report["results"]["subscriptions"]
    assert report["results"]["subscriptions"]["status"] == "match"
    aws.assert_all_called()


# ── Timeouts and result locations, as reported ──────────────────────────


def test_a_timed_out_query_that_could_not_be_stopped_says_so(tmp_path, aws):
    """The log said the stop failed while the error said the query was stopped."""
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup()
    aws.start()
    aws.state("RUNNING")
    aws.athena_stub.add_client_error(
        "stop_query_execution",
        service_error_code="AccessDeniedException",
        service_message="not authorized to perform athena:StopQueryExecution",
        expected_params={"QueryExecutionId": "q-1"},
    )

    code, report = _verify(tmp_path, contract, strict=False, athena_timeout=1e-9)

    error = report["results"]["subscriptions"]["error"]
    assert code == 1
    assert "was stopped" not in error
    assert "stopping it failed (AccessDeniedException), so it may still be running" in error
    aws.assert_all_called()


def test_a_workgroup_that_enforces_a_location_inside_the_table_is_refused(tmp_path, aws):
    """Athena ignores any other location for such a workgroup, so the query must
    not start: its result would land among the table's parquet files."""
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup(
        {
            "ResultConfiguration": {"OutputLocation": DATA_LOCATION},
            "EnforceWorkGroupConfiguration": True,
        }
    )  # no StartQueryExecution queued: starting the query fails the test

    code, report = _verify(tmp_path, contract)

    error = report["results"]["subscriptions"]["error"]
    assert code == 1
    assert "enforces its result location" in error and "inside the table" in error
    aws.assert_all_called()


def test_a_workgroup_location_inside_the_table_is_overridden(tmp_path, aws):
    """Not enforced, so the location sent with the query wins: the binding's bucket."""
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup({"ResultConfiguration": {"OutputLocation": DATA_LOCATION + "athena/"}})
    aws.start(output_location=DEFAULT_RESULTS)  # Stubber fails on any other location
    aws.state("SUCCEEDED")
    aws.count(10172)

    code, report = _verify(tmp_path, contract)

    athena = report["results"]["subscriptions"]["athena"]
    assert code == 0, report["results"]["subscriptions"]
    assert athena["output_location"] == DEFAULT_RESULTS
    assert athena["output_location_source"] == "binding-bucket"
    assert "inside the table" in athena["output_location_note"]
    aws.assert_all_called()


# ── Templates, resolved as fluid apply resolves them ────────────────────

_TEMPLATED_OVERLAY = (
    AWS_OVERLAY.replace("database: demo_bronze", "database: '{{ env.GLUE_DB }}'")
    .replace("table: customer_subscriptions", "table: '{{ env.GLUE_TABLE }}'")
    .replace("region: eu-north-1", "region: '{{ env.DEMO_REGION }}'")
)


def test_templated_database_and_table_are_the_names_apply_created(tmp_path, aws, monkeypatch):
    """``fluid apply`` resolves ``{{ env.* }}`` across the contract before it emits,
    so the Glue table it created is named by the values. Verify read the raw
    template and refused it as an unsafe identifier: a table apply provisioned
    could never pass."""
    monkeypatch.setenv("GLUE_DB", "demo_bronze")
    monkeypatch.setenv("GLUE_TABLE", "customer_subscriptions")
    monkeypatch.setenv("DEMO_REGION", REGION)
    contract = _write_contract(tmp_path, overlay=_TEMPLATED_OVERLAY)
    aws.table()  # Stubber checks GetTable asks for demo_bronze.customer_subscriptions
    aws.counts(42)  # ... and that the query counts "demo_bronze"."customer_subscriptions"

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0, result
    assert result["status"] == "match"
    assert result["table_id"] == "demo_bronze.customer_subscriptions"
    assert set(aws.requested) == {("glue", REGION), ("athena", REGION)}
    aws.assert_all_called()


def test_a_template_whose_variable_is_unset_is_refused_naming_it(tmp_path, aws, monkeypatch):
    monkeypatch.setenv("GLUE_DB", "demo_bronze")
    monkeypatch.setenv("DEMO_REGION", REGION)
    contract = _write_contract(tmp_path, overlay=_TEMPLATED_OVERLAY)  # GLUE_TABLE unset

    code, report = _verify(tmp_path, contract)

    error = report["results"]["subscriptions"]["error"]
    assert code == 1
    assert "binding.location.table" in error and "GLUE_TABLE is not set" in error
    assert aws.requested == []


# Stands in for a credential; any value the report must not carry.
_CANARY = "canary-value-that-must-stay-out-of-the-report"


@pytest.mark.parametrize(
    ("field", "value", "expect_code"),
    [
        # apply passes over a region that is not a region code; so does verify.
        pytest.param("region: eu-north-1", "region: '{{ env.NW_REGION_SECRET }}'", 0, id="region"),
        pytest.param(
            "table: customer_subscriptions",
            "table: '{{ env.GLUE_TABLE_PASSWORD }}'",
            1,
            id="table",
        ),
    ],
)
def test_a_credential_named_template_is_never_resolved_into_the_report(
    tmp_path, aws, monkeypatch, capsys, field, value, expect_code
):
    """``fluid apply`` leaves a credential-shaped placeholder literal rather than
    publish it. Verify resolved the region with the plain resolver, so a secret
    reached the report as "'<secret>' is not an AWS region name"."""
    monkeypatch.setenv("NW_REGION_SECRET", _CANARY)
    monkeypatch.setenv("GLUE_TABLE_PASSWORD", _CANARY)
    monkeypatch.setenv("AWS_REGION", REGION)
    contract = _write_contract(tmp_path, overlay=AWS_OVERLAY.replace(field, value))
    if expect_code == 0:
        aws.table()
        aws.counts(42)

    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == expect_code, result
    assert _CANARY not in json.dumps(report)
    assert _CANARY not in capsys.readouterr().out
    if expect_code:
        assert "looks like a credential" in result["error"]
        assert aws.requested == []


# ── Reference-only contracts ────────────────────────────────────────────

_REFERENCE_CONTRACT = CONTRACT.replace(
    "  - id: ingest_subscriptions\n"
    "    pattern: acquisition\n"
    "    engine: duckdb\n"
    "    properties:\n"
    "      source: {kind: postgres, mode: full_refresh, streams: [public.product_subscription]}\n"
    "      sink: {format: parquet}\n",
    # engine: python, so the dbt run-record probes (which also run for a dbt
    # reference build) stay out of what this checks.
    "  - id: external_subscriptions\n    pattern: hybrid-reference\n    engine: python\n",
)


def test_a_reference_only_contracts_empty_table_is_info_not_critical(tmp_path, aws, capsys):
    """Bug 6: a pipeline outside forge owns the rows. ``fluid apply`` still creates
    the Glue table, so on the first run it exists and is empty, and ``--strict``
    blocked stage 10 on that expected state (a missing table is already INFO)."""
    assert _REFERENCE_CONTRACT != CONTRACT
    contract = _write_contract(tmp_path, contract=_REFERENCE_CONTRACT)
    aws.table()
    aws.counts(0)
    code, report = _verify(tmp_path, contract)

    result = report["results"]["subscriptions"]
    assert code == 0, result
    assert result["status"] == "match"
    assert result["severity"]["level"] == "INFO"
    assert result["dimensions"]["row_count"]["status"] == "info"
    assert "reference-only" in result["dimensions"]["row_count"]["message"]
    assert "reference-only" in " ".join(capsys.readouterr().out.split())

    # Not with --fail-on-warning either: the missing table is not gated there.
    aws.table()
    aws.counts(0)
    code, _ = _verify(tmp_path, contract, fail_on_warning=True)
    assert code == 0

    # The schema is still checked: a missing column stays CRITICAL.
    aws.table(columns=[c for c in GLUE_COLUMNS if c["Name"] != "msisdn"])
    aws.counts(0)
    code, report = _verify(tmp_path, contract)
    assert code == 1
    assert report["results"]["subscriptions"]["severity"]["level"] == "CRITICAL"


def test_an_empty_table_a_recorded_run_landed_rows_in_still_fails(tmp_path, aws):
    """Only the absence of a count to compare with is excused: when a run of this
    contract's own build says it landed rows here, an empty table is a failure."""
    from fluid_build.cli._common import load_contract_with_overlay
    from fluid_build.cli._verify_athena import AthenaOptions, verify_athena_expose

    contract_path = _write_contract(tmp_path)
    _write_run_record(tmp_path, 10172)
    aws.table()
    aws.counts(0)
    contract = load_contract_with_overlay(str(contract_path), "aws", _LOG)

    result = verify_athena_expose(
        "subscriptions",
        contract["exposes"][0],
        contract=contract,
        workdir=tmp_path,
        options=AthenaOptions(),
        reference_only=True,
    )

    assert result["dimensions"]["row_count"]["status"] == "fail", result
    assert result["severity"]["level"] == "CRITICAL"


# ── What the console prints ─────────────────────────────────────────────


def test_the_console_prints_the_extra_to_install(tmp_path, monkeypatch, capsys):
    """Rich read ``[aws]`` as a style tag and dropped it, so the console said to
    ``pip install 'data-product-forge'``, which brings no boto3."""
    contract = _write_contract(tmp_path)
    monkeypatch.setitem(sys.modules, "boto3", None)

    code, _ = _verify(tmp_path, contract, strict=False)

    assert code == 1
    assert "pip install 'data-product-forge[aws]'" in " ".join(capsys.readouterr().out.split())


# ── Behaviour the module claims ─────────────────────────────────────────


def test_an_interrupted_wait_stops_the_query(tmp_path, aws, monkeypatch):
    """PyAthena's kill_on_interrupt: Ctrl-C must not leave the count scanning."""
    contract = _write_contract(tmp_path)
    aws.table()
    aws.workgroup()
    aws.start()
    aws.athena_stub.add_response("stop_query_execution", {}, {"QueryExecutionId": "q-1"})

    def _interrupted(**_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(aws.athena, "get_query_execution", _interrupted)

    with pytest.raises(KeyboardInterrupt):
        _verify(tmp_path, contract)
    aws.assert_all_called()


def test_the_bindings_region_wins_over_the_environment(tmp_path, aws, monkeypatch):
    """``location.region``, then ``binding.region``, then the environment."""
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = ap-south-1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    overlay = AWS_OVERLAY.replace(
        "      format: parquet\n", "      format: parquet\n      region: sa-east-1\n"
    )
    contract = _write_contract(tmp_path, overlay=overlay)
    aws.table()
    aws.counts(42)

    code, report = _verify(tmp_path, contract)

    assert code == 0, report["results"]["subscriptions"]
    assert set(aws.requested) == {("glue", REGION), ("athena", REGION)}
    assert report["results"]["subscriptions"]["athena"]["region_source"] == (
        "binding.location.region"
    )

    # Without location.region, binding.region comes next.
    contract.write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "overlays" / "aws.yaml").write_text(
        overlay.replace("        region: eu-north-1\n", ""), encoding="utf-8"
    )
    aws.requested.clear()
    aws.table()
    aws.counts(42)
    code, report = _verify(tmp_path, contract)
    assert set(aws.requested) == {("glue", "sa-east-1"), ("athena", "sa-east-1")}


@pytest.mark.parametrize(
    ("old", "new", "planted"),
    [
        pytest.param(
            "id: bronze.customer_subscriptions",
            "id: ../../escape",
            Path("escape") / BUILD,
            id="contract-id",
        ),
        pytest.param(
            "  - id: ingest_subscriptions",
            "  - id: ../../../escape",
            Path("escape"),
            id="build-id",
        ),
    ],
)
def test_an_id_that_is_a_path_never_reaches_the_filesystem(tmp_path, aws, old, new, planted):
    """The run-record path is built from contract.id and build.id. A record planted
    where a traversing id would lead (and which would fail the table) is never read."""
    text = CONTRACT.replace(old, new)
    assert text != CONTRACT
    contract = _write_contract(tmp_path, contract=text)
    runs = tmp_path / planted / "runs"
    runs.mkdir(parents=True)
    (runs / "0101M37Z8SDZ06AJXT.json").write_text(
        json.dumps(
            {
                "run_id": "0101M37Z8SDZ06AJXT",
                "state": "succeeded",
                "records_total": 1,
                "facets": {
                    "landed": {
                        "mode": "full_refresh",
                        "rows_from": "write",
                        "destinations": {"s": DATA_FILE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    aws.table()
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] is None
    assert "not a valid identifier" in row_count["compared_with"]["note"]


def test_two_builds_writing_one_expose_are_not_compared(tmp_path, aws):
    """Which build's run landed the table's rows is unknowable, so neither is used."""
    text = CONTRACT.replace(
        "    outputs: [subscriptions]\n",
        "    outputs: [subscriptions]\n"
        "  - id: backfill_subscriptions\n"
        "    pattern: acquisition\n"
        "    engine: duckdb\n"
        "    properties:\n"
        "      source: {kind: postgres, mode: full_refresh, streams: [public.product_subscription]}\n"
        "    outputs: [subscriptions]\n",
    )
    assert text != CONTRACT
    contract = _write_contract(tmp_path, contract=text)
    _write_run_record(tmp_path, 1)  # would fail the table if it were compared
    aws.table()
    aws.counts(10172)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["expected"] is None
    assert "2 builds write this expose" in row_count["compared_with"]["note"]


def test_an_output_location_from_the_environment_is_trimmed(tmp_path, aws, monkeypatch):
    """``export FLUID_ATHENA_OUTPUT_LOCATION=$(cat file)`` keeps the newline."""
    monkeypatch.setenv("FLUID_ATHENA_OUTPUT_LOCATION", f"  {_OVERRIDE}\n")
    contract = _write_contract(tmp_path)
    aws.table()
    aws.counts(42, output_location=_OVERRIDE)  # Stubber fails on any other location

    code, report = _verify(tmp_path, contract)

    assert code == 0, report["results"]["subscriptions"]
    assert report["results"]["subscriptions"]["athena"]["output_location"] == _OVERRIDE
    aws.assert_all_called()


def test_an_append_floor_that_reaches_no_full_load_says_it_may_be_partial(tmp_path, aws):
    """A CI stage has only the run records it carried over, so the appends on
    disk may not reach back to the full load: the sum is a floor, not the floor."""
    contract = _write_contract(tmp_path, contract=_with_mode("incremental_append"))
    _write_run_record(tmp_path, 60, run_id="0101M30000000000B1", mode="incremental_append")
    _write_run_record(tmp_path, 40, run_id="0101M30000000000B2", mode="incremental_append")
    aws.table()
    aws.counts(50)

    code, report = _verify(tmp_path, contract)

    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 1, row_count
    assert row_count["expected"] == 100
    assert row_count["compared_with"]["reached_full_load"] is False
    assert "reach back to no full load" in row_count["message"]
    assert "since the last full load" not in row_count["message"]
