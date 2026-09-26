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

"""``fluid diff --exit-on-drift`` against the live target, through the real parser.

Stage 5 of the generated pipeline runs ``fluid diff --exit-on-drift`` with no
``--state``. It used to compare against nothing: ``has_drift`` was true on
every run and the gate was always downgraded to a warning, so it could never
fire. These tests pin the live comparison that replaced it:

* local parquet read through DuckDB, a real file on disk;
* a Glue table read through a real boto3 client under ``botocore`` 's Stubber;
* a BigQuery table read through a stubbed ``bigquery.Client``.

Nothing here reaches a cloud endpoint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

LOGGER = logging.getLogger("test.diff_live_drift")

# The demo product's declared schema, in the source's own type names.
SCHEMA: List[Dict[str, Any]] = [
    {"name": "subscription_id", "type": "VARCHAR", "required": True},
    {"name": "customer_id", "type": "VARCHAR", "required": True},
    {"name": "start_date", "type": "DATE", "required": True},
    {"name": "status", "type": "VARCHAR", "required": True},
    {"name": "created_at", "type": "TIMESTAMP", "required": True},
]

# What DuckDB writes for that schema.
MATCHING_SELECT = (
    "SELECT 's1'::VARCHAR AS subscription_id, 'c1'::VARCHAR AS customer_id, "
    "DATE '2026-01-01' AS start_date, 'active'::VARCHAR AS status, "
    "TIMESTAMP '2026-01-01 00:00:00' AS created_at"
)

LOCAL_BINDING = {
    "platform": "local",
    "format": "parquet",
    "location": {"path": "./out/customer_subscriptions.parquet"},
}

AWS_BINDING = {
    "platform": "aws",
    "format": "parquet",
    "location": {
        "database": "demo_bronze",
        "table": "customer_subscriptions",
        "bucket": "northwind-demo-lake",
        "path": "bronze/customer_subscriptions/",
        "region": "eu-north-1",
    },
}

GCP_BINDING = {
    "platform": "gcp",
    "format": "bigquery_table",
    "location": {
        "project": "northwind-demo",
        "dataset": "demo_bronze",
        "table": "customer_subscriptions",
        "region": "europe-west1",
    },
}

# EU-only, like the demo contract: the global ``--region`` default
# (europe-west3) is not allowed, so a provider built in it fails.
SOVEREIGNTY = {
    "jurisdiction": "EU",
    "allowedRegions": ["eu-north-1", "eu-west-1", "europe-west1"],
    "deniedRegions": ["us-east-1", "us-west-2"],
    "dataResidency": True,
    "enforcementMode": "strict",
}

# The Glue columns ``fluid apply`` creates for SCHEMA (``_hive_type``).
GLUE_COLUMNS = [
    {"Name": "subscription_id", "Type": "string"},
    {"Name": "customer_id", "Type": "string"},
    {"Name": "start_date", "Type": "date"},
    {"Name": "status", "Type": "string"},
    {"Name": "created_at", "Type": "timestamp"},
]

# The BigQuery columns it creates (``_bq_type``), as ``get_table`` reports them.
BQ_COLUMNS = [
    ("subscription_id", "STRING"),
    ("customer_id", "STRING"),
    ("start_date", "DATE"),
    ("status", "STRING"),
    ("created_at", "TIMESTAMP"),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _contract(binding: Dict[str, Any], *, schema=None, **top: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.customer_subscriptions",
        "name": "Customer Subscriptions",
        "domain": "Customer",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "subscriptions",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": SCHEMA if schema is None else schema},
            }
        ],
    }
    doc.update(top)
    return doc


def _write_contract(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _write_parquet(path: Path, select_sql: str) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"COPY ({select_sql}) TO '{path.as_posix()}' (FORMAT parquet)")
    finally:
        con.close()


def _invoke(argv: List[str]):
    """Run ``fluid <argv>`` through the real parser.

    Returns ``(exit_code, event)``: ``event`` is the ``CLIError`` event when
    the command raised one (the CLI's main turns it into ``exit_code``).
    """
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(argv)
    try:
        return args.func(args, LOGGER), None
    except CLIError as exc:
        return exc.exit_code, exc.event


def _report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class _PlanOnlyProvider:
    """Stands in for a cloud provider whose ``plan`` needs credentials."""

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"op": "ensure_table", "resource_type": "table", "resource_id": "subscriptions"}]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for var in ("FLUID_PROVIDER", "FLUID_PROJECT", "FLUID_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def built_providers(monkeypatch):
    """Replace ``build_provider`` in ``fluid diff``; record what it was asked for."""
    from fluid_build.cli import diff as diff_mod

    calls: List[Dict[str, Optional[str]]] = []

    def _build(provider, project, region, logger):
        calls.append({"provider": provider, "project": project, "region": region})
        return _PlanOnlyProvider()

    monkeypatch.setattr(diff_mod, "build_provider", _build)
    return calls


@pytest.fixture
def glue(monkeypatch):
    """A real boto3 Glue client under Stubber, handed to the Glue inspector."""
    import boto3
    from botocore.stub import Stubber

    from fluid_build.providers import aws_validation

    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    client = boto3.session.Session(
        region_name="eu-north-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ).client("glue")
    stubber = Stubber(client)
    sessions: List[Dict[str, Any]] = []

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            sessions.append(kwargs)

        def client(self, service: str):
            assert service == "glue"
            return client

    monkeypatch.setattr(aws_validation.boto3, "Session", _Session)
    with stubber:
        yield SimpleNamespace(stubber=stubber, sessions=sessions)


def _glue_table(columns: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "Table": {
            "Name": "customer_subscriptions",
            "DatabaseName": "demo_bronze",
            "TableType": "EXTERNAL_TABLE",
            "StorageDescriptor": {"Columns": columns},
        }
    }


GLUE_PARAMS = {"DatabaseName": "demo_bronze", "Name": "customer_subscriptions"}


@pytest.fixture
def bigquery(monkeypatch):
    """A stub ``bigquery.Client``: answers ``get_table`` from ``state``."""
    from google.cloud.bigquery import SchemaField

    from fluid_build.providers import bigquery_validation

    state = SimpleNamespace(columns=list(BQ_COLUMNS), error=None, clients=[], requested=[])

    class _Client:
        def __init__(self, project=None, **_kwargs: Any) -> None:
            state.clients.append(project)

        def get_table(self, table_id: str):
            state.requested.append(table_id)
            if state.error is not None:
                raise state.error
            return SimpleNamespace(
                schema=[SchemaField(name, typ) for name, typ in state.columns],
                num_rows=1,
                num_bytes=10,
                modified=None,
                created=None,
                table_type="TABLE",
                location="europe-west1",
                description=None,
            )

    monkeypatch.setattr(bigquery_validation.bigquery, "Client", _Client)
    return state


# ---------------------------------------------------------------------------
# local parquet (DuckDB)
# ---------------------------------------------------------------------------


def _local_case(root: Path, select_sql: Optional[str]) -> Path:
    contract = _write_contract(root, _contract(LOCAL_BINDING))
    if select_sql is not None:
        _write_parquet(root / "out" / "customer_subscriptions.parquet", select_sql)
    return contract


def test_local_parquet_matching_the_contract_is_no_drift(workspace, capsys):
    contract = _local_case(workspace, MATCHING_SELECT)

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert report["summary"]["has_drift"] is False, "a matching target is not drift"
    assert (rc, event) == (0, None)
    assert report["drift_source"] == "live"
    assert report["live"]["counts"]["match"] == 1
    assert "subscriptions [local] match" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("select_sql", "column", "reason"),
    [
        (
            f"SELECT *, 'x' AS rogue_col FROM ({MATCHING_SELECT})",
            "rogue_col",
            "missing_in_contract",
        ),
        (f"SELECT * EXCLUDE (status) FROM ({MATCHING_SELECT})", "status", "missing_in_target"),
        (
            f"SELECT * REPLACE (CAST(1 AS BIGINT) AS status) FROM ({MATCHING_SELECT})",
            "status",
            "type_mismatch",
        ),
    ],
    ids=["column-added", "column-removed", "column-retyped"],
)
def test_local_parquet_changed_outside_the_contract_fails_the_gate(
    workspace, caplog, select_sql, column, reason
):
    contract = _local_case(workspace, select_sql)

    with caplog.at_level(logging.WARNING):
        rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert rc == 1, "--exit-on-drift must fail the gate on live drift"
    assert event is None
    assert report["summary"]["has_drift"] is True
    (expose,) = report["live"]["exposes"]
    assert expose["status"] == "drift"
    assert [(c["column"], c["reason"]) for c in expose["columns"]] == [(column, reason)]
    assert "diff_live_drift_detected" in caplog.text


@pytest.mark.parametrize(
    ("select_sql", "has_drift", "columns"),
    [
        (MATCHING_SELECT, False, []),
        (f"SELECT *, 1 AS extra FROM ({MATCHING_SELECT})", True, ["extra"]),
    ],
    ids=["match", "drift"],
)
def test_without_exit_on_drift_the_comparison_is_reported_and_exits_zero(
    workspace, select_sql, has_drift, columns
):
    contract = _local_case(workspace, select_sql)

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json"])

    report = _report(workspace / "diff.json")
    assert rc == 0
    assert report["summary"]["has_drift"] is has_drift
    assert [c["column"] for c in report["live"]["exposes"][0]["columns"]] == columns


def test_local_target_not_built_yet_is_to_be_created_not_drift(workspace, capsys):
    contract = _local_case(workspace, None)

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["summary"]["has_drift"] is False
    assert report["live"]["exposes"][0]["status"] == "absent"
    assert "absent (to be created)" in capsys.readouterr().out


def test_a_column_name_from_the_target_cannot_forge_gate_output(workspace, capsys):
    """Column names come from the target, which the contract does not control."""
    forged = "x\n  subscriptions [local] match\x1b[2K"
    contract = _local_case(workspace, f'SELECT *, 1 AS "{forged}" FROM ({MATCHING_SELECT})')

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "\x1b" not in out
    assert not [
        line for line in out.splitlines() if line.startswith("  subscriptions [local] match")
    ]


# ---------------------------------------------------------------------------
# --state and --no-live keep their meaning
# ---------------------------------------------------------------------------


def test_state_baseline_is_still_what_state_mode_compares(workspace, built_providers):
    """``--state`` compares plan resources with that apply report, as before.

    The live target is drifted on purpose: if ``--state`` mode read it, the
    clean baseline would fail and the report would carry a ``live`` block.
    """
    contract = _local_case(workspace, f"SELECT *, 1 AS extra FROM ({MATCHING_SELECT})")
    same = workspace / "same.json"
    same.write_text(json.dumps({"results": _PlanOnlyProvider().plan({})}), encoding="utf-8")
    empty = workspace / "empty.json"
    empty.write_text(json.dumps({"results": []}), encoding="utf-8")

    rc_same, _ = _invoke(
        ["diff", str(contract), "--state", str(same), "--out", "same.out.json", "--exit-on-drift"]
    )
    rc_empty, _ = _invoke(
        ["diff", str(contract), "--state", str(empty), "--out", "empty.out.json", "--exit-on-drift"]
    )

    same_report = _report(workspace / "same.out.json")
    empty_report = _report(workspace / "empty.out.json")
    assert (rc_same, rc_empty) == (0, 1)
    assert same_report["summary"]["has_drift"] is False
    assert empty_report["changes"]["added"] == ["table:subscriptions"]
    assert same_report["drift_source"] == empty_report["drift_source"] == "state"
    assert "live" not in same_report and "live" not in empty_report


def test_no_live_without_state_keeps_the_old_downgrade(workspace, built_providers):
    contract = _local_case(workspace, f"SELECT *, 1 AS extra FROM ({MATCHING_SELECT})")

    rc, event = _invoke(
        ["diff", str(contract), "--no-live", "--out", "diff.json", "--exit-on-drift"]
    )

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["drift_source"] == "none"
    assert "live" not in report


# ---------------------------------------------------------------------------
# AWS Glue
# ---------------------------------------------------------------------------


def test_glue_table_matching_the_contract_is_no_drift(workspace, built_providers, glue):
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(AWS_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["live"]["exposes"][0]["status"] == "match"
    # Read where apply created it: the binding's region, not the global default.
    assert glue.sessions == [{"region_name": "eu-north-1"}]
    glue.stubber.assert_no_pending_responses()


def test_glue_column_added_outside_the_contract_fails_the_gate(workspace, built_providers, glue):
    columns = GLUE_COLUMNS + [{"Name": "added_by_hand", "Type": "string"}]
    glue.stubber.add_response("get_table", _glue_table(columns), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(AWS_BINDING))

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 1
    assert [(c["column"], c["reason"]) for c in expose["columns"]] == [
        ("added_by_hand", "missing_in_contract")
    ]


def test_glue_table_not_created_yet_is_to_be_created(workspace, built_providers, glue):
    glue.stubber.add_client_error(
        "get_table",
        service_error_code="EntityNotFoundException",
        service_message="Table customer_subscriptions not found.",
        http_status_code=400,
        expected_params=GLUE_PARAMS,
    )
    contract = _write_contract(workspace, _contract(AWS_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["summary"]["has_drift"] is False
    assert report["live"]["exposes"][0]["status"] == "absent"


def test_glue_access_denied_is_not_read_as_absent(workspace, built_providers, glue):
    """A gate that cannot look must not say "nothing there, go ahead"."""
    glue.stubber.add_client_error(
        "get_table",
        service_error_code="AccessDeniedException",
        service_message="User is not authorized to perform glue:GetTable",
        http_status_code=400,
        expected_params=GLUE_PARAMS,
    )
    contract = _write_contract(workspace, _contract(AWS_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert expose["status"] == "error"
    assert "AccessDeniedException" in expose["detail"]


def test_aws_overlay_plans_in_the_binding_region_and_reaches_the_comparison(
    workspace, glue, monkeypatch
):
    """The measured stage-5 failure: an EU-only contract died on the global
    default region (europe-west3) with "data residency violation" before
    anything was compared. The real AWS provider is used here; the account id
    comes from the environment, so no STS call is made."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(AWS_BINDING, sovereignty=SOVEREIGNTY))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert (rc, event) == (0, None)
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "match"


def test_diff_builds_the_provider_with_the_binding_region_and_project(
    workspace, built_providers, glue, bigquery
):
    """Same precedence as ``fluid plan``: the binding's project and region."""
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    aws = workspace / "aws"
    gcp = workspace / "gcp"
    aws.mkdir()
    gcp.mkdir()
    aws_contract = _write_contract(aws, _contract(AWS_BINDING))
    gcp_contract = _write_contract(gcp, _contract(GCP_BINDING))

    _invoke(["diff", str(aws_contract), "--out", "aws.json"])
    _invoke(["diff", str(gcp_contract), "--out", "gcp.json"])

    assert built_providers == [
        {"provider": "aws", "project": None, "region": "eu-north-1"},
        {"provider": "gcp", "project": "northwind-demo", "region": "europe-west1"},
    ]


def test_an_explicit_region_flag_wins_over_the_binding(workspace, built_providers, bigquery):
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    _invoke(["diff", str(contract), "--region", "europe-west4", "--out", "diff.json"])

    assert built_providers == [
        {"provider": "gcp", "project": "northwind-demo", "region": "europe-west4"}
    ]


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


def test_bigquery_table_matching_the_contract_is_no_drift(workspace, built_providers, bigquery):
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert (rc, event) == (0, None)
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "match"
    assert bigquery.clients == ["northwind-demo"]
    assert bigquery.requested == ["northwind-demo.demo_bronze.customer_subscriptions"]


def test_bigquery_column_retyped_outside_the_contract_fails_the_gate(
    workspace, built_providers, bigquery
):
    bigquery.columns = [(n, "INTEGER" if n == "status" else t) for n, t in BQ_COLUMNS]
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 1
    assert [(c["column"], c["reason"], c["target_type"]) for c in expose["columns"]] == [
        ("status", "type_mismatch", "INTEGER")
    ]


def test_bigquery_legacy_type_spelling_is_not_drift(workspace, built_providers, bigquery):
    """``get_table`` answers INTEGER/FLOAT/BOOLEAN for INT64/FLOAT64/BOOL."""
    schema = SCHEMA + [{"name": "n", "type": "bigint"}, {"name": "ok", "type": "boolean"}]
    bigquery.columns = BQ_COLUMNS + [("n", "INTEGER"), ("ok", "BOOLEAN")]
    contract = _write_contract(workspace, _contract(GCP_BINDING, schema=schema))

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert rc == 0
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "match"


def test_bigquery_table_not_created_yet_is_to_be_created(workspace, built_providers, bigquery):
    from google.api_core import exceptions as google_exceptions

    bigquery.error = google_exceptions.NotFound("Not found: Table northwind-demo:demo_bronze.x")
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["summary"]["has_drift"] is False
    assert report["live"]["exposes"][0]["status"] == "absent"


def test_bigquery_forbidden_is_not_read_as_absent(workspace, built_providers, bigquery):
    from google.api_core import exceptions as google_exceptions

    bigquery.error = google_exceptions.Forbidden("Access Denied: Table northwind-demo")
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "error"


def test_bigquery_table_id_that_would_change_the_request_path_is_refused(
    workspace, built_providers, bigquery
):
    """The table id goes into the REST path of an authenticated GET."""
    binding = json.loads(json.dumps(GCP_BINDING))
    binding["location"]["table"] = "x/../../other_dataset/tables/secret"
    contract = _write_contract(workspace, _contract(binding))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert bigquery.requested == []


# ---------------------------------------------------------------------------
# aggregate rules
# ---------------------------------------------------------------------------


def test_an_uninspectable_target_wins_over_drift_elsewhere(
    workspace, built_providers, bigquery, glue
):
    """OpenTofu's rule: an operation that failed returns its failure status
    before ``-detailed-exitcode`` looks at the diff."""
    from google.api_core import exceptions as google_exceptions

    bigquery.error = google_exceptions.Forbidden("denied")
    glue.stubber.add_response(
        "get_table",
        _glue_table(GLUE_COLUMNS + [{"Name": "rogue", "Type": "int"}]),
        GLUE_PARAMS,
    )
    doc = _contract(AWS_BINDING)
    doc["exposes"].append(
        {"exposeId": "bq", "binding": GCP_BINDING, "contract": {"schema": SCHEMA}}
    )
    contract = _write_contract(workspace, doc)

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert report["live"]["counts"]["drift"] == 1
    assert report["live"]["counts"]["error"] == 1


def test_nothing_inspectable_downgrades_the_gate_and_says_so(workspace, built_providers, caplog):
    binding = {"platform": "snowflake", "format": "snowflake_table", "location": {"table": "t"}}
    contract = _write_contract(workspace, _contract(binding))

    with caplog.at_level(logging.WARNING):
        rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["drift_source"] == "live"
    assert report["live"]["counts"]["not_checked"] == 1
    assert "diff_exit_on_drift_skipped" in caplog.text
