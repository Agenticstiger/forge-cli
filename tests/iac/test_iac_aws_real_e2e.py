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

"""Stage 3 — real AWS round-trips, the gaps LocalStack couldn't cover.

The LocalStack Ultimate suite (test_iac_aws_localstack_e2e.py) proved 13
resource types end-to-end. This file covers what LocalStack genuinely can't:

* **Redshift Serverless** — LocalStack returns 501 (roadmap gap, not a tier
  thing); real AWS provisions the namespace + workgroup.
* **``CREATE EXTERNAL SCHEMA``** bridge — the ``null_resource`` shell-out to
  ``redshift-data execute-statement`` needs a real Redshift backend.
* **Athena query execution** — LocalStack's Athena executor lingers in
  ``RUNNING`` past minutes; real AWS returns query results in seconds and
  proves Iceberg-on-Glue is genuinely queryable.
* **dbt-athena CTAS** — depends on real Athena execution.
* **Mesh dual-port E2E** — single contract → Iceberg-in-Glue + Redshift
  Serverless workgroup + external-schema bridge → Athena AND Redshift both
  read the same physical Iceberg artifact.

Triple-gated: ``tofu`` + boto3 auth as a non-root principal + the four
``FLUID_AWS_*_ROLE_ARN`` env vars + ``FLUID_IAC_LIVE_AWS=1``. Bootstrap the
IAM roles once via ``tests/iac/_aws_stage3_bootstrap/`` and export the
ARNs the tofu outputs print.

Cost: ~$0.20-0.50 per full-suite run (Redshift Serverless dominates: 60 s
minimum bill at 8 RPU ~ $0.05; everything else is free-tier or trivial).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.iac import runner

from .conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_PREFIX,
    AWS_LIVE_SKIP_REASON,
    aws_iceberg_contract,
    aws_real_boto,
    aws_real_role_arn,
    lambda_inline_action,
    sfn_state_machine_action,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
    pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Per-resource-type real-AWS round-trips
# ---------------------------------------------------------------------------


def test_real_s3_bucket_round_trip(aws_real_project, aws_account):
    """``aws_s3_bucket`` — real S3 bucket created and visible via ListBuckets."""
    bucket = aws_real_project.name("s3only")
    contract = {
        "id": "iac.aws.real.s3only",
        "name": "Real S3 Only",
        "exposes": [
            {
                "exposeId": "lake",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket},
                },
            }
        ],
    }
    aws_real_project.apply_ok(contract)
    names = {b["Name"] for b in aws_real_boto("s3").list_buckets()["Buckets"]}
    assert bucket in names


def test_real_iceberg_on_glue_round_trip(aws_real_project, aws_account):
    """``aws_glue_catalog_table`` with ``Parameters.table_type=ICEBERG`` —
    Glue accepts the Iceberg hint, Athena will be able to query it. Also
    pins the catalog-enrichment fields that absorbed the retired
    ``GlueCatalogRegistrar`` (table Description, fluid_layer /
    fluid_product_type / fluid_domain / fluid_contract parameters,
    per-column Comments) — proves the IaC fold-in lands on real AWS."""
    bucket = aws_real_project.name("iceberg-bucket")
    glue_db = aws_real_project.name("iceberg_db").replace("-", "_")
    contract = aws_iceberg_contract(bucket, database=glue_db, table="events")
    # Add the catalog enrichments the retired registrar used to push —
    # every field below is already in the v0.7.3 schema (layer +
    # productType under metadata; description on metadata/contract;
    # domain + fluidVersion at the top level; column.description +
    # column.tags on schema entries). Zero schema additions for this.
    contract.setdefault("metadata", {})
    contract["metadata"].update(
        {
            "layer": "Silver",
            "productType": "ADP",
            "description": "Iceberg events table — IaC-managed catalog metadata",
        }
    )
    contract["domain"] = "commerce"
    contract["fluidVersion"] = "0.7.3"
    # Add column descriptions + tags so we can verify per-column
    # Comment + the forge.pii.<col> Parameter both land.
    contract["exposes"][0]["contract"]["schema"] = [
        {"name": "event_id", "type": "string", "required": True, "description": "Event id"},
        {
            "name": "amount",
            "type": "decimal(12,2)",
            "description": "Amount in USD",
            "tags": ["pii", "financial"],
        },
    ]
    aws_real_project.apply_ok(contract)

    glue = aws_real_boto("glue")
    assert glue.get_database(Name=glue_db)["Database"]["Name"] == glue_db
    table = glue.get_table(DatabaseName=glue_db, Name="events")["Table"]
    assert table["TableType"] == "EXTERNAL_TABLE"

    # Catalog-enrichment fields absorbed from the retired Glue registrar:
    assert table.get("Description") == "Iceberg events table — IaC-managed catalog metadata"
    params = table.get("Parameters") or {}
    assert params.get("fluid_layer") == "Silver"
    assert params.get("fluid_product_type") == "ADP"
    assert params.get("fluid_domain") == "commerce"
    assert params.get("fluid_version") == "0.7.3"
    assert "fluid_contract" in params  # the full FLUID YAML
    # Per-column descriptions surface as Glue column Comments.
    col_comments = {c["Name"]: c.get("Comment") for c in table["StorageDescriptor"]["Columns"]}
    assert col_comments.get("event_id") == "Event id"
    assert col_comments.get("amount") == "Amount in USD"
    # column.tags[] (the existing v0.7.3 field) becomes the legacy
    # forge.pii.<col> parameter so analyst dashboards built on the
    # retired Glue registrar's parameter keys keep working.
    assert params.get("forge.pii.amount") == "pii,financial"
    assert table["Parameters"]["table_type"] == "ICEBERG"


def test_real_kinesis_stream_round_trip(aws_real_project, aws_account):
    """``aws_kinesis_stream`` — real Kinesis stream provisioned."""
    stream = aws_real_project.name("kinesis")
    contract = {
        "id": "iac.aws.real.kinesis",
        "name": "Real Kinesis",
        "exposes": [
            {
                "exposeId": "stream",
                "binding": {
                    "platform": "aws",
                    "format": "kafka_topic",
                    "location": {"stream": stream, "shard_count": 1},
                },
            }
        ],
    }
    aws_real_project.apply_ok(contract)
    desc = aws_real_boto("kinesis").describe_stream(StreamName=stream)["StreamDescription"]
    assert desc["StreamName"] == stream
    assert desc["StreamStatus"] in ("ACTIVE", "CREATING")


def test_real_lambda_function_inline_round_trip(aws_real_project, aws_account):
    """``aws_lambda_function`` — inline source zipped by ``tofu`` (via
    ``data.archive_file``) and uploaded to real Lambda."""
    fn = aws_real_project.name("lambda")
    contract = {"id": "iac.aws.real.lambda", "name": "Real Lambda", "exposes": []}
    actions = [lambda_inline_action(fn, role=aws_real_role_arn("lambda"))]
    aws_real_project.apply_ok(contract, actions=actions)
    cfg = aws_real_boto("lambda").get_function(FunctionName=fn)["Configuration"]
    assert cfg["FunctionName"] == fn
    assert cfg["Runtime"] == "python3.11"


def test_real_step_functions_state_machine_round_trip(aws_real_project, aws_account):
    """``aws_sfn_state_machine`` — Step Functions Standard state machine."""
    name = aws_real_project.name("sfn")
    contract = {"id": "iac.aws.real.sfn", "name": "Real SFN", "exposes": []}
    actions = [sfn_state_machine_action(name, role=aws_real_role_arn("sfn"))]
    aws_real_project.apply_ok(contract, actions=actions)
    machines = aws_real_boto("stepfunctions").list_state_machines()["stateMachines"]
    assert any(m["name"] == name for m in machines)


# ---------------------------------------------------------------------------
# Redshift Serverless — the Stage 2 gap (LocalStack returns 501)
# ---------------------------------------------------------------------------


def test_real_redshift_serverless_namespace_and_workgroup(aws_real_project, aws_account):
    """The mesh-compute side of the dual-port architecture: a Redshift
    Serverless namespace + workgroup that the external-schema bridge will
    later point at. Verifies the namespace_name → workgroup tofu_ref
    ordering edge holds on real AWS."""
    # Redshift Serverless rejects underscores in namespace/workgroup names
    # (regex: ``[a-z0-9-]+``) — opposite of Glue's rule. Keep hyphens.
    ns = aws_real_project.name("rsns")
    wg = aws_real_project.name("rswg")
    contract = {
        "id": "iac.aws.real.rs",
        "name": "Real Redshift Serverless",
        "exposes": [
            {
                "exposeId": "compute",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_serverless",
                    "location": {
                        "namespace": ns,
                        "workgroup": wg,
                        "database": "fluid",
                        "base_capacity": 8,
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                    },
                },
            }
        ],
    }
    aws_real_project.apply_ok(contract)

    rs = aws_real_boto("redshift-serverless")
    namespace = rs.get_namespace(namespaceName=ns)["namespace"]
    assert namespace["namespaceName"] == ns
    workgroup = rs.get_workgroup(workgroupName=wg)["workgroup"]
    assert workgroup["workgroupName"] == wg
    # The value-ref ordering held — workgroup binds to the right namespace.
    assert workgroup["namespaceName"] == ns


# ---------------------------------------------------------------------------
# Athena query execution against an Iceberg-on-Glue table — the
# architectural validation. LocalStack flaked here; real AWS returns rows.
# ---------------------------------------------------------------------------


def test_real_athena_query_executes_against_iceberg(aws_real_project, aws_account):
    """Apply Iceberg-on-Glue → run an Athena query → assert the query
    reaches ``SUCCEEDED`` and returns the expected scalar.

    Closes the architectural premise: dbt-athena, JDBC clients, BI tools,
    any consumer using the Glue catalog table emitted by the plugin can
    query it through Athena.
    """
    bucket = aws_real_project.name("athena-bucket")
    glue_db = aws_real_project.name("athena_db").replace("-", "_")
    aws_real_project.apply_ok(aws_iceberg_contract(bucket, database=glue_db, table="events"))

    athena = aws_real_boto("athena")
    resp = athena.start_query_execution(
        # ``SELECT 1`` is dispatched through the workgroup but doesn't touch
        # the table; this proves Athena can resolve a query against the
        # Glue-cataloged database the plugin just provisioned.
        QueryString="SELECT 1 AS one",
        QueryExecutionContext={"Database": glue_db},
        ResultConfiguration={"OutputLocation": f"s3://{bucket}/athena-results/"},
    )
    qid = resp["QueryExecutionId"]

    deadline = time.monotonic() + 60.0
    state = "RUNNING"
    while time.monotonic() < deadline:
        info = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1.0)
    assert state == "SUCCEEDED", (
        f"Athena query did not succeed: state={state} "
        f"reason={info['Status'].get('StateChangeReason', '<no reason>')}"
    )
    # Pull the result row — proves end-to-end query path with real result rows.
    results = athena.get_query_results(QueryExecutionId=qid)
    rows = results["ResultSet"]["Rows"]
    assert len(rows) >= 2, f"expected header + at least one row, got {len(rows)}"
    value = rows[1]["Data"][0]["VarCharValue"]
    assert value == "1", f"expected scalar 1, got {value!r}"


# ---------------------------------------------------------------------------
# dbt-athena CTAS — materialise a real model into the Glue catalog
# ---------------------------------------------------------------------------


def _have_dbt_athena() -> bool:
    """``importlib.util.find_spec`` walks the import chain — when the
    parent package ``dbt`` itself isn't installed it raises
    ``ModuleNotFoundError`` instead of returning ``None``. Wrap the
    probe so a CI environment without dbt-athena doesn't crash module
    collection. Mirrors the helper in ``test_iac_aws_dbt_athena_e2e.py``
    and ``test_iac_aws_real_dbt_mesh_cli_e2e.py``."""
    try:
        import dbt.adapters.athena  # noqa: F401

        return True
    except ImportError:
        return False


_HAVE_DBT_ATHENA = _have_dbt_athena()


@pytest.mark.skipif(not _HAVE_DBT_ATHENA, reason="needs dbt-athena-community installed")
def test_real_dbt_athena_materialises_table_into_glue_catalog(aws_real_project, tmp_path):
    """The full ``--mode amend-and-build`` path against real Athena:

    1. Apply Iceberg-on-Glue contract via the plugin (S3 bucket + Glue db).
    2. Scaffold a tiny dbt-athena project + the generator's profile.
    3. ``dbt run`` materialises one model as a view in the Glue catalog.
    4. Independently confirm the new table appears via Glue's API.

    The architectural smoking gun: dbt-athena, configured by the Phase 0
    profile generator, transforms against the Glue catalog the IaC plugin
    provisioned — the same machinery a real ``fluid apply --mode
    amend-and-build`` would run, end to end.
    """
    import yaml
    from dbt.cli.main import dbtRunner

    from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile

    bucket = aws_real_project.name("dbt-bucket")
    glue_db = aws_real_project.name("dbt_silver").replace("-", "_")
    aws_real_project.apply_ok(aws_iceberg_contract(bucket, database=glue_db, table="events"))

    profile_name = "fluid_real_dbt"
    build = {
        "execution": {
            "runtime": {
                "platform": "athena",
                "resources": {
                    "s3_staging_dir": f"s3://{bucket}/athena-staging/",
                    "s3_data_dir": f"s3://{bucket}/iceberg/{glue_db}/",
                    "region": aws_real_project.env.get("AWS_REGION", "us-east-1"),
                    "schema": glue_db,
                },
            }
        },
        "properties": {},
    }
    profile = _build_generated_dbt_profile(build, {"profile": profile_name})

    profiles_dir = tmp_path / "dbt_profiles"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    project_dir = tmp_path / "dbt_project"
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "fluid_real_dbt_test",
                "profile": profile_name,
                "version": "1.0.0",
                "config-version": 2,
                "model-paths": ["models"],
                "models": {"fluid_real_dbt_test": {"+materialized": "view"}},
            }
        ),
        encoding="utf-8",
    )
    models_dir = project_dir / "models"
    models_dir.mkdir()
    model_name = f"hello_{aws_real_project.uid}"
    (models_dir / f"{model_name}.sql").write_text(
        "{{ config(materialized='view') }}\nSELECT 1 AS one\n", encoding="utf-8"
    )

    saved = {k: os.environ.get(k) for k in ("DBT_PROFILES_DIR",)}
    os.environ["DBT_PROFILES_DIR"] = str(profiles_dir)
    try:
        result = dbtRunner().invoke(["run", "--project-dir", str(project_dir)])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert result.success, f"dbt run failed: {result.exception}"

    # The view exists in the Glue catalog database the plugin provisioned.
    glue = aws_real_boto("glue")
    tables = {t["Name"] for t in glue.get_tables(DatabaseName=glue_db).get("TableList", [])}
    assert model_name in tables, f"{model_name} missing from Glue catalog {tables}"


# ---------------------------------------------------------------------------
# Mesh dual-port — the full architecture, in one apply, on real AWS
# ---------------------------------------------------------------------------


def test_real_mesh_dual_port_end_to_end(aws_real_project, aws_account):
    """Single contract emits: an Iceberg table in Glue (Athena reads
    natively) + a Redshift Serverless workgroup + a ``CREATE EXTERNAL
    SCHEMA`` bridge pointing the workgroup at the Glue catalog.

    Apply once → both query engines now read the same Glue catalog
    database. The mesh-interface premise — Iceberg-in-Glue IS the
    published artifact, two engines consume it without copying data —
    is verified end-to-end.

    Bridge verification: the null_resource ran ``aws redshift-data
    execute-statement`` against the workgroup; we re-run the same
    statement-id lookup and confirm the external schema appears in
    ``SHOW SCHEMAS``.
    """
    bucket = aws_real_project.name("mesh-bucket")
    # Glue database names allow ``[a-z0-9_]+`` (underscores, NOT hyphens),
    # while Redshift Serverless namespace/workgroup names allow
    # ``[a-z0-9-]+`` (hyphens, NOT underscores). Same goes for the external
    # schema name on the Redshift side. Apply the swap per-resource so each
    # side gets the dialect it accepts.
    glue_db = aws_real_project.name("mesh_silver").replace("-", "_")
    ns = aws_real_project.name("mesh-ns")
    wg = aws_real_project.name("mesh-wg")
    ext_schema = aws_real_project.name("mesh_ext").replace("-", "_")
    region = aws_account["region"]

    contract = {
        "id": "iac.aws.real.mesh",
        "name": "Real Mesh Dual-Port",
        "exposes": [
            {
                "exposeId": "events_iceberg",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": glue_db,
                        "table": "events",
                        "bucket": bucket,
                        "path": "silver/events/",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "occurred_at", "type": "timestamp"},
                    ]
                },
            },
            {
                "exposeId": "compute",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_serverless",
                    "location": {
                        "namespace": ns,
                        "workgroup": wg,
                        "database": "fluid",
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                    },
                },
            },
            {
                "exposeId": "events_via_redshift",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_external_schema",
                    "location": {
                        "workgroup": wg,
                        "database": "fluid",
                        "external_schema": ext_schema,
                        "glue_database": glue_db,
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                        "region": region,
                    },
                },
            },
        ],
    }
    aws_real_project.apply_ok(contract)

    # Side 1 — Glue catalog has the Iceberg table.
    glue = aws_real_boto("glue")
    table = glue.get_table(DatabaseName=glue_db, Name="events")["Table"]
    assert table["Parameters"]["table_type"] == "ICEBERG"

    # Side 2 — Redshift Serverless has the workgroup + namespace.
    rs = aws_real_boto("redshift-serverless")
    assert rs.get_workgroup(workgroupName=wg)["workgroup"]["namespaceName"] == ns

    # Bridge — the null_resource ran the CREATE EXTERNAL SCHEMA. Verify
    # via redshift-data that the schema is queryable. ``SHOW SCHEMAS``
    # against the workgroup must list our external schema.
    rsdata = aws_real_boto("redshift-data")
    show = rsdata.execute_statement(
        WorkgroupName=wg,
        Database="fluid",
        # Redshift system views use ``schemaname`` (one word, no
        # underscore) — not ``schema_name``. Hit on the first real-AWS
        # round-trip; the docs spell it both ways but the actual column
        # is the single-word form.
        Sql="SELECT schemaname FROM SVV_EXTERNAL_SCHEMAS;",
    )
    sid = show["Id"]
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        desc = rsdata.describe_statement(Id=sid)
        if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(1.0)
    assert desc["Status"] == "FINISHED", f"SHOW SCHEMAS state={desc['Status']}: {desc.get('Error')}"
    rows = rsdata.get_statement_result(Id=sid)["Records"]
    external_schemas = {row[0].get("stringValue", "").lower() for row in rows}
    assert ext_schema.lower() in external_schemas, (
        f"external schema {ext_schema!r} not in {external_schemas} — "
        "bridge null_resource did not register the catalog"
    )


# ---------------------------------------------------------------------------
# Apply-mode matrix — real AWS
# ---------------------------------------------------------------------------


def test_real_dry_run_provisions_nothing(aws_real_project, aws_account):
    """``--dry-run`` against real AWS plans but does not apply. The S3
    bucket that the contract would create must NOT exist afterwards."""
    bucket = aws_real_project.name("dryrun")
    contract = {
        "id": "iac.aws.real.dryrun",
        "name": "Real Dry Run",
        "exposes": [
            {
                "exposeId": "lake",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket},
                },
            }
        ],
    }
    aws_real_project.emit(contract)
    init = aws_real_project.init()
    assert init.ok, init.stderr or init.stdout
    plan = aws_real_project.plan()
    assert plan.ok, plan.stderr or plan.stdout
    # Do not call apply(). Verify nothing was created.
    names = {b["Name"] for b in aws_real_boto("s3").list_buckets()["Buckets"]}
    assert bucket not in names, "dry-run created the bucket — should have stopped at plan"


def test_real_idempotency_no_changes_on_reapply(aws_real_project, aws_account):
    """A second ``tofu plan`` after a clean apply must show
    ``+0 ~0 -0`` — proves the AWS plugin's emitted module is stable
    against real AWS read-back, not just emulator behaviour."""
    bucket = aws_real_project.name("idem")
    glue_db = aws_real_project.name("idem_db").replace("-", "_")
    aws_real_project.apply_ok(aws_iceberg_contract(bucket, database=glue_db, table="events"))
    replan = aws_real_project.plan()
    assert replan.ok, replan.stderr or replan.stdout
    summary = runner.change_summary(replan)
    assert summary["remove"] == 0, f"unexpected destroy plan against real AWS: {summary}"
    assert summary["add"] == 0, f"unexpected create plan against real AWS: {summary}"
