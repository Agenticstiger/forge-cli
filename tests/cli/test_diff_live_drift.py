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
* a Glue table read through a real boto3 client under ``botocore`` 's Stubber
  (skipped without boto3);
* a BigQuery table read through a stubbed ``bigquery.Client``, with the real
  ``google-cloud-bigquery`` types when the ``gcp`` extra is installed and a
  stand-in for them when it is not (CI's test jobs install ``.[dev,local]``).

A local target is written by the build, so its differences are judged by the
expose's ``schemaPolicy`` (``evolve_safe`` when unset); the tests that mean
"changed by hand" declare ``strict``, as the demo contract does. A Glue or
BigQuery table's columns are declared by the IaC, so any difference is drift.

Nothing here reaches a cloud endpoint.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


def _contract(
    binding: Dict[str, Any],
    *,
    schema=None,
    policy: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
    **top: Any,
) -> Dict[str, Any]:
    block: Dict[str, Any] = {"schema": SCHEMA if schema is None else schema}
    if policy:
        block["schemaPolicy"] = policy
    if overrides:
        block["evolutionOverrides"] = overrides
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
                "contract": block,
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
    duckdb = pytest.importorskip("duckdb")

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
    """A real boto3 Glue client under Stubber, handed to the Glue inspector.

    Skips without boto3, as the Athena verify tests do: botocore's Stubber is
    the point, and CI's Python 3.10 leg has no boto3 (it arrives there only
    through another package's dependencies).
    """
    boto3 = pytest.importorskip("boto3", reason="botocore's Stubber comes with the aws extra")
    Stubber = pytest.importorskip("botocore.stub").Stubber

    from fluid_build.providers import aws_validation

    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    client = boto3.session.Session(
        region_name="eu-north-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret
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


_BQ_VALIDATION = "fluid_build.providers.bigquery_validation"


def _google_sdk_stand_in() -> Dict[str, ModuleType]:
    """The parts of ``google-cloud-bigquery`` that ``bigquery_validation`` reaches.

    ``Client`` is replaced by the ``bigquery`` fixture before anything calls
    it. ``SchemaField`` carries the four attributes the provider reads, with
    the SDK's upper-casing of the type. The exceptions keep the SDK's
    hierarchy, which is what lets the provider read ``NotFound`` as a table
    not created yet and re-raise ``Forbidden``.
    """

    class GoogleAPICallError(Exception):
        pass

    class ClientError(GoogleAPICallError):
        pass

    class NotFound(ClientError):
        pass

    class Forbidden(ClientError):
        pass

    class SchemaField:
        def __init__(
            self,
            name: str,
            field_type: str,
            mode: str = "NULLABLE",
            description: Optional[str] = None,
        ) -> None:
            self.name = name
            self.field_type = field_type.upper()
            self.mode = mode
            self.description = description

    class Client:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the stand-in SDK builds no client; the fixture replaces it")

    exceptions = ModuleType("google.api_core.exceptions")
    for cls in (GoogleAPICallError, ClientError, NotFound, Forbidden):
        setattr(exceptions, cls.__name__, cls)
    api_core = ModuleType("google.api_core")
    api_core.exceptions = exceptions
    bigquery = ModuleType("google.cloud.bigquery")
    bigquery.SchemaField = SchemaField
    bigquery.Client = Client
    cloud = ModuleType("google.cloud")
    cloud.bigquery = bigquery
    modules = {
        "google.api_core": api_core,
        "google.api_core.exceptions": exceptions,
        "google.cloud": cloud,
        "google.cloud.bigquery": bigquery,
    }
    if importlib.util.find_spec("google") is None:
        root = ModuleType("google")
        root.api_core = api_core
        root.cloud = cloud
        modules["google"] = root
    return modules


@pytest.fixture
def fresh_bigquery_validation():
    """``bigquery_validation`` is imported afresh in this test, and that copy
    is forgotten afterwards.

    The module binds the ``google`` SDK when it is imported. A copy bound to
    the stand-in, or cached from an earlier test, would otherwise answer for
    the SDK in a later test on the same worker that expects something else.
    """
    from fluid_build import providers

    saved_module = sys.modules.pop(_BQ_VALIDATION, None)
    saved_attr = vars(providers).pop("bigquery_validation", None)
    yield
    sys.modules.pop(_BQ_VALIDATION, None)
    vars(providers).pop("bigquery_validation", None)
    if saved_module is not None:
        sys.modules[_BQ_VALIDATION] = saved_module
    if saved_attr is not None:
        providers.bigquery_validation = saved_attr


@pytest.fixture
def google_sdk(monkeypatch, fresh_bigquery_validation):
    """``SchemaField`` and the exceptions the BigQuery cases raise.

    The real SDK when the ``gcp`` extra is installed; otherwise the stand-in,
    placed in ``sys.modules`` for this test only. Without it every BigQuery
    case here errored at setup in CI and none of them ran.
    """
    try:
        from google.api_core import exceptions
        from google.cloud.bigquery import SchemaField

        return SimpleNamespace(SchemaField=SchemaField, exceptions=exceptions)
    except ImportError:
        pass
    modules = _google_sdk_stand_in()
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(
        SchemaField=modules["google.cloud.bigquery"].SchemaField,
        exceptions=modules["google.api_core.exceptions"],
    )


@pytest.fixture
def bigquery(monkeypatch, google_sdk):
    """A stub ``bigquery.Client``: answers ``get_table`` from ``state``."""
    from fluid_build.providers import bigquery_validation

    SchemaField = google_sdk.SchemaField
    state = SimpleNamespace(
        columns=list(BQ_COLUMNS),
        error=None,
        clients=[],
        requested=[],
        exceptions=google_sdk.exceptions,
    )

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


def _local_case(root: Path, select_sql: Optional[str], *, policy: Optional[str] = None) -> Path:
    contract = _write_contract(root, _contract(LOCAL_BINDING, policy=policy))
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
    contract = _local_case(workspace, select_sql, policy="strict")

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
    contract = _local_case(workspace, select_sql, policy="strict")

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
    contract = _local_case(
        workspace, f'SELECT *, 1 AS "{forged}" FROM ({MATCHING_SELECT})', policy="strict"
    )

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


def test_glue_without_boto3_fails_the_gate_and_names_the_extra(
    workspace, built_providers, monkeypatch
):
    """Runs everywhere: a Glue table that cannot be read is not one to create."""
    from fluid_build.providers import aws_validation

    # What the module sets when ``import boto3`` fails, so nothing can reach AWS.
    monkeypatch.setattr(aws_validation, "BOTO3_AVAILABLE", False)
    monkeypatch.setattr(aws_validation, "boto3", None)
    contract = _write_contract(workspace, _contract(AWS_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    expose = _report(workspace / "diff.json")["live"]["exposes"][0]
    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert expose["status"] == "error"
    assert "install the 'aws' extra" in expose["detail"]


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
    bigquery.error = bigquery.exceptions.NotFound("Not found: Table northwind-demo:demo_bronze.x")
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    report = _report(workspace / "diff.json")
    assert (rc, event) == (0, None)
    assert report["summary"]["has_drift"] is False
    assert report["live"]["exposes"][0]["status"] == "absent"


def test_bigquery_forbidden_is_not_read_as_absent(workspace, built_providers, bigquery):
    bigquery.error = bigquery.exceptions.Forbidden("Access Denied: Table northwind-demo")
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "error"


def test_bigquery_without_the_gcp_extra_fails_the_gate_and_names_it(
    workspace, built_providers, fresh_bigquery_validation, monkeypatch
):
    """A table that cannot be read is not a table that does not exist."""
    for name in (
        "google.api_core",
        "google.api_core.exceptions",
        "google.cloud",
        "google.cloud.bigquery",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    contract = _write_contract(workspace, _contract(GCP_BINDING))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    expose = _report(workspace / "diff.json")["live"]["exposes"][0]
    assert (rc, event) == (2, "diff_live_inspection_failed")
    assert expose["status"] == "error"
    assert "install the 'gcp' extra" in expose["detail"]


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
    bigquery.error = bigquery.exceptions.Forbidden("denied")
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


# ---------------------------------------------------------------------------
# schemaPolicy: a build-written target is judged by the policy the build ran
# ---------------------------------------------------------------------------


def _source_contract(root: Path, policy: str) -> Path:
    """A local product whose build copies ``src/subs.parquet`` to the target."""
    doc = _contract(LOCAL_BINDING, schema=SCHEMA[:2] + [SCHEMA[3]], policy=policy)
    doc["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "parquet",
        "location": {"path": "./out/subs.parquet"},
    }
    doc["builds"] = [
        {
            "id": "ingest",
            "pattern": "acquisition",
            "engine": "duckdb",
            "properties": {
                "source": {
                    "kind": "filesystem",
                    "connection": {"uri": "./src/subs.parquet"},
                    "reader": {"format": "parquet"},
                    "mode": "full_refresh",
                    "streams": ["subs"],
                },
                "sink": {"format": "parquet"},
            },
            "outputs": ["subscriptions"],
        }
    ]
    return _write_contract(root, doc)


def test_apply_under_evolve_safe_then_diff_is_not_drift(workspace):
    """The measured failure: ``fluid apply`` landed the column ``evolve_safe``
    lets a build include, and the gate then failed on apply's own output."""
    source = workspace / "src" / "subs.parquet"
    _write_parquet(
        source,
        "SELECT 's1'::VARCHAR AS subscription_id, 'c1'::VARCHAR AS customer_id, "
        "'active'::VARCHAR AS status",
    )
    contract = _source_contract(workspace, "evolve_safe")
    apply = ["apply", str(contract), "--mode", "amend-and-build", "--yes"]
    assert _invoke(apply) == (0, None)

    # The source gains a column; apply lands it, as evolve_safe allows.
    grown = workspace / "src" / "grown.parquet"
    _write_parquet(grown, f"SELECT *, 'gold' AS plan_tier FROM '{source.as_posix()}'")
    grown.replace(source)
    assert _invoke(apply) == (0, None)

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert (rc, event) == (0, None), "apply's own output is not drift"
    assert expose["status"] == "evolved"
    assert expose["schema_policy"] == "evolve_safe"
    assert [
        (c["column"], c["event"], c["action"], c["classification"]) for c in expose["columns"]
    ] == [("plan_tier", "added", "include", "allowed")]


@pytest.mark.parametrize(
    ("policy", "overrides", "select_sql", "status", "action"),
    [
        (None, None, f"SELECT *, 'x' AS added FROM ({MATCHING_SELECT})", "evolved", "include"),
        (
            "evolve_safe",
            None,
            f"SELECT * EXCLUDE (status) FROM ({MATCHING_SELECT})",
            "evolved",
            "warn",
        ),
        (
            "evolve_safe",
            None,
            f"SELECT * REPLACE (CAST(1 AS BIGINT) AS status) FROM ({MATCHING_SELECT})",
            "drift",
            "fail",
        ),
        (
            "evolve_all",
            None,
            f"SELECT * REPLACE (CAST(1 AS BIGINT) AS status) FROM ({MATCHING_SELECT})",
            "evolved",
            "cast",
        ),
        (
            "discover_and_freeze",
            None,
            f"SELECT *, 'x' AS added FROM ({MATCHING_SELECT})",
            "drift",
            "fail",
        ),
        (
            "evolve_safe",
            {"onAddedColumn": "fail"},
            f"SELECT *, 'x' AS added FROM ({MATCHING_SELECT})",
            "drift",
            "fail",
        ),
    ],
    ids=[
        "default-added",
        "evolve_safe-removed",
        "evolve_safe-retyped",
        "evolve_all-retyped",
        "freeze-added",
        "override-added",
    ],
)
def test_local_differences_are_classified_by_the_expose_policy(
    workspace, policy, overrides, select_sql, status, action
):
    contract = _write_contract(
        workspace, _contract(LOCAL_BINDING, policy=policy, overrides=overrides)
    )
    _write_parquet(workspace / "out" / "customer_subscriptions.parquet", select_sql)

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert expose["status"] == status
    assert [c["action"] for c in expose["columns"]] == [action]
    assert rc == (1 if status == "drift" else 0)


def test_glue_columns_are_the_iac_s_so_the_policy_does_not_excuse_them(
    workspace, built_providers, glue
):
    """The build never changes a Glue table's columns; a column added there
    was added by hand, whatever evolve_safe lets a build write."""
    columns = GLUE_COLUMNS + [{"Name": "added_by_hand", "Type": "string"}]
    glue.stubber.add_response("get_table", _glue_table(columns), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(AWS_BINDING, policy="evolve_safe"))

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 1
    assert expose["status"] == "drift"
    assert expose["schema_policy"] is None


def test_glue_column_names_match_without_case(workspace, built_providers, glue):
    """Glue lower-cases every column name it stores."""
    schema = SCHEMA + [{"name": "customerId", "type": "VARCHAR"}]
    glue.stubber.add_response(
        "get_table",
        _glue_table(GLUE_COLUMNS + [{"Name": "customerid", "Type": "string"}]),
        GLUE_PARAMS,
    )
    contract = _write_contract(workspace, _contract(AWS_BINDING, schema=schema, policy="strict"))

    rc, event = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert (rc, event) == (0, None)
    assert expose["status"] == "match"
    assert expose["columns"] == []


# ---------------------------------------------------------------------------
# --last-applied: a change the contract made is pending, not drift
# ---------------------------------------------------------------------------


def _plan_file(root: Path, contract: Dict[str, Any]) -> Path:
    """The shape ``fluid plan --out`` writes: the planned contract under ``contract``."""
    path = root / "last-plan.json"
    path.write_text(json.dumps({"format_version": 1, "contract": contract}), encoding="utf-8")
    return path


PLAN_TIER = {"name": "plan_tier", "type": "VARCHAR"}


@pytest.mark.parametrize(
    ("new_schema", "column", "reason"),
    [
        (SCHEMA + [PLAN_TIER], "plan_tier", "missing_in_target"),
        ([c for c in SCHEMA if c["name"] != "status"], "status", "missing_in_contract"),
        (
            [dict(c, type="INTEGER") if c["name"] == "status" else c for c in SCHEMA],
            "status",
            "type_mismatch",
        ),
    ],
    ids=["contract-added", "contract-removed", "contract-retyped"],
)
def test_a_contract_change_since_the_last_apply_is_pending_not_drift(
    workspace, caplog, new_schema, column, reason
):
    _write_parquet(workspace / "out" / "customer_subscriptions.parquet", MATCHING_SELECT)
    applied = _plan_file(workspace, _contract(LOCAL_BINDING, policy="strict"))
    contract = _write_contract(
        workspace, _contract(LOCAL_BINDING, schema=new_schema, policy="strict")
    )

    # Two-way, the contract is all there is to compare with: drift.
    rc_two_way, _ = _invoke(["diff", str(contract), "--out", "two.json", "--exit-on-drift"])
    with caplog.at_level(logging.DEBUG):
        rc, event = _invoke(
            [
                "diff",
                str(contract),
                "--last-applied",
                str(applied),
                "--out",
                "diff.json",
                "--exit-on-drift",
            ]
        )

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc_two_way == 1
    assert (rc, event) == (0, None), "the target is as the last apply left it"
    assert expose["status"] == "pending"
    assert expose["baseline"] == "last_applied"
    assert [(c["column"], c["reason"], c["classification"]) for c in expose["columns"]] == [
        (column, reason, "pending")
    ]
    assert "diff_live_changes_pending" in caplog.text


def test_a_target_changed_since_the_last_apply_is_still_drift(workspace):
    _write_parquet(
        workspace / "out" / "customer_subscriptions.parquet",
        f"SELECT *, 'x' AS added_by_hand FROM ({MATCHING_SELECT})",
    )
    doc = _contract(LOCAL_BINDING, schema=SCHEMA + [PLAN_TIER], policy="strict")
    applied = _plan_file(workspace, _contract(LOCAL_BINDING, policy="strict"))
    contract = _write_contract(workspace, doc)

    rc, _ = _invoke(
        [
            "diff",
            str(contract),
            "--last-applied",
            str(applied),
            "--out",
            "diff.json",
            "--exit-on-drift",
        ]
    )

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 1
    assert expose["status"] == "drift"
    assert {c["column"]: c["classification"] for c in expose["columns"]} == {
        "added_by_hand": "drift",
        "plan_tier": "pending",
    }


def test_the_last_applied_policy_judges_what_builds_did_since(workspace):
    """Builds since the last apply ran under its policy (evolve_safe), so a
    column they included is not drift because the contract now says strict."""
    _write_parquet(
        workspace / "out" / "customer_subscriptions.parquet",
        f"SELECT *, 'x' AS added FROM ({MATCHING_SELECT})",
    )
    applied = _plan_file(workspace, _contract(LOCAL_BINDING, policy="evolve_safe"))
    contract = _write_contract(workspace, _contract(LOCAL_BINDING, policy="strict"))

    rc, _ = _invoke(
        [
            "diff",
            str(contract),
            "--last-applied",
            str(applied),
            "--out",
            "diff.json",
            "--exit-on-drift",
        ]
    )

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 0
    assert (expose["status"], expose["schema_policy"]) == ("evolved", "evolve_safe")


def test_glue_contract_change_since_the_last_apply_is_pending(workspace, built_providers, glue):
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    applied = _plan_file(workspace, _contract(AWS_BINDING))
    contract = _write_contract(workspace, _contract(AWS_BINDING, schema=SCHEMA + [PLAN_TIER]))

    rc, event = _invoke(
        [
            "diff",
            str(contract),
            "--last-applied",
            str(applied),
            "--out",
            "diff.json",
            "--exit-on-drift",
        ]
    )

    assert (rc, event) == (0, None)
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "pending"


def test_a_missing_last_applied_is_the_first_run(workspace, caplog):
    contract = _local_case(
        workspace, f"SELECT *, 'x' AS extra FROM ({MATCHING_SELECT})", policy="strict"
    )

    with caplog.at_level(logging.DEBUG):
        rc, event = _invoke(
            [
                "diff",
                str(contract),
                "--last-applied",
                str(workspace / "never-applied.json"),
                "--out",
                "diff.json",
                "--exit-on-drift",
            ]
        )

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert (rc, event) == (1, None)
    assert expose["baseline"] == "contract"
    assert "diff_last_applied_not_found" in caplog.text


def test_a_last_applied_file_with_no_contract_is_refused(workspace):
    contract = _local_case(workspace, MATCHING_SELECT)
    bogus = workspace / "bogus.json"
    bogus.write_text(json.dumps({"results": []}), encoding="utf-8")

    rc, event = _invoke(
        [
            "diff",
            str(contract),
            "--last-applied",
            str(bogus),
            "--out",
            "diff.json",
            "--exit-on-drift",
        ]
    )

    assert (rc, event) == (2, "diff_last_applied_invalid")


# ---------------------------------------------------------------------------
# Where the cloud targets are read
# ---------------------------------------------------------------------------


def _no_region_aws() -> Dict[str, Any]:
    binding = json.loads(json.dumps(AWS_BINDING))
    del binding["location"]["region"]
    return binding


def test_glue_read_takes_aws_region_before_aws_default_region(
    workspace, built_providers, glue, monkeypatch
):
    """The tofu AWS provider reads AWS_REGION first; boto3 reads only
    AWS_DEFAULT_REGION, so the read went to another region than the apply."""
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(_no_region_aws()))

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert rc == 0
    assert glue.sessions == [{"region_name": "eu-north-1"}]
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["target"].endswith("(eu-north-1)")


@pytest.fixture
def no_gcp_project_env(monkeypatch):
    for var in (
        "GOOGLE_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CORE_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)


def _no_project_gcp() -> Dict[str, Any]:
    binding = json.loads(json.dumps(GCP_BINDING))
    del binding["location"]["project"]
    return binding


@pytest.mark.parametrize(
    ("env", "argv_prefix", "project"),
    [
        ({"GOOGLE_PROJECT": "team-data-proj"}, [], "team-data-proj"),
        ({}, ["--project", "team-data-proj"], "team-data-proj"),
        ({"FLUID_PROJECT": "team-data-proj"}, [], "team-data-proj"),
        ({"GOOGLE_CLOUD_PROJECT": "tofu-proj"}, ["--project", "plan-proj"], "tofu-proj"),
    ],
    ids=["GOOGLE_PROJECT", "--project", "FLUID_PROJECT", "env-before-flag"],
)
def test_bigquery_read_uses_the_project_apply_and_plan_use(
    workspace, built_providers, bigquery, no_gcp_project_env, monkeypatch, env, argv_prefix, project
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    contract = _write_contract(workspace, _contract(_no_project_gcp()))

    _invoke([*argv_prefix, "diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    assert bigquery.requested == [f"{project}.demo_bronze.customer_subscriptions"]
    assert bigquery.clients == [project]


# ---------------------------------------------------------------------------
# Types and formats the local reader must judge correctly
# ---------------------------------------------------------------------------


def test_a_local_column_retyped_into_a_type_the_family_table_lacks_is_drift(workspace):
    """A list and an unsigned integer used to read as ``match``."""
    contract = _local_case(
        workspace,
        f"SELECT * REPLACE ([status] AS status, CAST(1 AS UBIGINT) AS created_at) "
        f"FROM ({MATCHING_SELECT})",
    )

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 1
    assert [(c["column"], c["reason"], c["target_type"]) for c in expose["columns"]] == [
        ("created_at", "type_mismatch", "UBIGINT"),
        ("status", "type_mismatch", "VARCHAR[]"),
    ]


def test_duckdb_spellings_of_a_declared_family_are_not_drift(workspace):
    schema = SCHEMA + [
        {"name": "n", "type": "integer"},
        {"name": "tags", "type": "array<string>"},
        {"name": "seen_at", "type": "TIMESTAMP"},
    ]
    contract = _write_contract(workspace, _contract(LOCAL_BINDING, schema=schema, policy="strict"))
    _write_parquet(
        workspace / "out" / "customer_subscriptions.parquet",
        f"SELECT *, CAST(1 AS HUGEINT) AS n, ['a'] AS tags, "
        f"CAST(TIMESTAMP '2026-01-01' AS TIMESTAMP_NS) AS seen_at FROM ({MATCHING_SELECT})",
    )

    rc, _ = _invoke(["diff", str(contract), "--out", "diff.json", "--exit-on-drift"])

    (expose,) = _report(workspace / "diff.json")["live"]["exposes"]
    assert rc == 0
    assert expose["status"] == "match"


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_schema_less_files_compare_names_not_sniffed_types(workspace, fmt):
    """DuckDB sniffs a VARCHAR column of digits back as BIGINT from CSV, and
    date-like text back as DATE from CSV and JSON."""
    path = workspace / "out" / f"customer_subscriptions.{fmt}"
    binding = {"platform": "local", "format": fmt, "location": {"path": f"./out/{path.name}"}}
    contract = _write_contract(workspace, _contract(binding, policy="strict"))
    select = MATCHING_SELECT.replace("'c1'::VARCHAR", "'1001'::VARCHAR").replace(
        "'active'::VARCHAR", "'2026-01-01'::VARCHAR"
    )

    duckdb = pytest.importorskip("duckdb")

    path.parent.mkdir(parents=True, exist_ok=True)
    options = "FORMAT 'csv', HEADER" if fmt == "csv" else "FORMAT 'json'"
    con = duckdb.connect(":memory:")
    con.execute(f"COPY ({select}) TO '{path.as_posix()}' ({options})")
    rc_same, _ = _invoke(["diff", str(contract), "--out", "same.json", "--exit-on-drift"])
    con.execute(f"COPY (SELECT *, 1 AS added FROM ({select})) TO '{path.as_posix()}' ({options})")
    con.close()
    rc_added, _ = _invoke(["diff", str(contract), "--out", "added.json", "--exit-on-drift"])

    assert rc_same == 0
    assert _report(workspace / "same.json")["live"]["exposes"][0]["status"] == "match"
    assert rc_added == 1, "a column added to the file is still seen"
    assert [
        c["column"] for c in _report(workspace / "added.json")["live"]["exposes"][0]["columns"]
    ] == ["added"]


def test_a_duckdb_file_without_the_table_yet_is_to_be_created(workspace):
    duckdb = pytest.importorskip("duckdb")

    db = workspace / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE other_product AS SELECT 1 AS x")
    con.close()
    binding = {
        "platform": "local",
        "format": "parquet",
        "location": {
            "path": "./warehouse.duckdb",
            "schema": "main",
            "table": "customer_subscriptions",
        },
    }
    contract = _write_contract(workspace, _contract(binding))

    rc_absent, event = _invoke(["diff", str(contract), "--out", "absent.json", "--exit-on-drift"])
    con = duckdb.connect(str(db))
    con.execute(f"CREATE TABLE customer_subscriptions AS {MATCHING_SELECT}")
    con.close()
    rc_built, _ = _invoke(["diff", str(contract), "--out", "built.json", "--exit-on-drift"])

    assert (rc_absent, event) == (0, None)
    assert _report(workspace / "absent.json")["live"]["exposes"][0]["status"] == "absent"
    assert rc_built == 0
    assert _report(workspace / "built.json")["live"]["exposes"][0]["status"] == "match"


# ---------------------------------------------------------------------------
# Provider region and --state fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "argv_prefix"),
    [
        ({}, ["--provider", "aws", "--region", "eu-north-1"]),
        ({"FLUID_PROVIDER": "aws", "FLUID_REGION": "eu-north-1"}, []),
    ],
    ids=["global-flags", "FLUID_env"],
)
def test_an_explicit_provider_and_global_region_plan_and_compare(
    workspace, glue, monkeypatch, env, argv_prefix
):
    """``main`` planned these; the diff sub-flag's default then dropped the
    global region and the explicit provider skipped the binding's."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)
    contract = _write_contract(workspace, _contract(AWS_BINDING, sovereignty=SOVEREIGNTY))

    rc, event = _invoke(
        [*argv_prefix, "diff", str(contract), "--out", "diff.json", "--exit-on-drift"]
    )

    assert (rc, event) == (0, None)
    assert _report(workspace / "diff.json")["live"]["exposes"][0]["status"] == "match"


def test_provider_region_precedence(workspace, built_providers, glue):
    """diff --region, then the binding's (even with --provider), then the
    global --region / FLUID_REGION; the global built-in default (a GCP region)
    never reaches another cloud's provider."""
    from fluid_build.cli import build_parser

    aws_dir, bare_dir = workspace / "aws", workspace / "bare"
    aws_dir.mkdir()
    bare_dir.mkdir()
    aws = _write_contract(aws_dir, _contract(AWS_BINDING))
    bare = _write_contract(bare_dir, _contract(_no_region_aws()))
    for _ in range(4):
        glue.stubber.add_response("get_table", _glue_table(GLUE_COLUMNS), GLUE_PARAMS)

    def _region(argv: List[str]) -> Optional[str]:
        before = len(built_providers)
        _invoke(argv)
        assert len(built_providers) == before + 1
        return built_providers[-1]["region"]

    assert _region(["--provider", "aws", "diff", str(aws), "--out", "a.json"]) == "eu-north-1"
    assert (
        _region(
            [
                "--region",
                "eu-west-1",
                "diff",
                str(aws),
                "--region",
                "eu-central-1",
                "--out",
                "b.json",
            ]
        )
        == "eu-central-1"
    )
    assert build_parser().parse_args(["--region", "eu-west-1", "diff", "x.yaml"]).region == (
        "eu-west-1"
    )
    assert _region(["--region", "eu-west-1", "diff", str(bare), "--out", "c.json"]) == "eu-west-1"
    assert _region(["diff", str(bare), "--out", "d.json"]) is None


def test_a_missing_state_file_falls_back_to_the_live_comparison(workspace, caplog):
    contract = _local_case(
        workspace, f"SELECT *, 'x' AS extra FROM ({MATCHING_SELECT})", policy="strict"
    )

    with caplog.at_level(logging.DEBUG):
        rc, event = _invoke(
            [
                "diff",
                str(contract),
                "--state",
                str(workspace / "runtime" / "apply-report.json"),
                "--out",
                "diff.json",
                "--exit-on-drift",
            ]
        )

    report = _report(workspace / "diff.json")
    assert (rc, event) == (1, None), "live drift, not a missing-file error"
    assert report["drift_source"] == "live"
    assert "diff_state_not_found" in caplog.text
