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

Every test drives ``verify.run`` end to end, with the contract and overlay the
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
    ) -> None:
        self.glue_stub.add_response(
            "get_table",
            {
                "Table": {
                    "Name": "customer_subscriptions",
                    "DatabaseName": "demo_bronze",
                    "TableType": "EXTERNAL_TABLE",
                    "StorageDescriptor": {
                        "Columns": GLUE_COLUMNS if columns is None else columns,
                        "Location": location,
                    },
                    "PartitionKeys": partition_keys or [],
                }
            },
            {"DatabaseName": "demo_bronze", "Name": "customer_subscriptions"},
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

    def start(self, output_location: Optional[str] = DEFAULT_RESULTS, workgroup="primary"):
        expected: Dict[str, Any] = {"QueryString": COUNT_SQL, "WorkGroup": workgroup}
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


def _write_run_record(tmp_path: Path, records_total: int, state: str = "succeeded") -> None:
    runs = tmp_path / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    runs.mkdir(parents=True)
    (runs / "0101M37Z8SDZ06AJXT.json").write_text(
        json.dumps(
            {
                "run_id": "0101M37Z8SDZ06AJXT",
                "state": state,
                "records_total": records_total,
                "finished_at": "2026-09-23T20:28:16Z",
                "streams": [{"name": "public.product_subscription", "records": records_total}],
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
    _write_run_record(tmp_path, 120)
    aws.table()
    aws.counts(10292)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 0, row_count
    assert row_count["status"] == "pass"
    assert row_count["compared_with"]["rule"] == "at_least"

    aws.table()
    aws.counts(100)
    code, report = _verify(tmp_path, contract)
    row_count = report["results"]["subscriptions"]["dimensions"]["row_count"]
    assert code == 1
    assert row_count["status"] == "fail"
    assert "fewer rows than one run landed" in row_count["message"]


def test_a_merging_build_is_reported_not_gated(tmp_path, aws):
    """A merge updates rows in place, so its run's count bounds nothing."""
    contract = _write_contract(tmp_path, contract=_with_mode("incremental_merge"))
    _write_run_record(tmp_path, 500)
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
