# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the AWS IaC plugin — contract -> .tf.json translation.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _aws():
    return get_iac_plugin("aws")


def _contract(exposes):
    return {"id": "analytics.lake", "name": "Lake", "exposes": exposes}


def _glue_exposure(**location):
    return {
        "exposeId": "t",
        "binding": {"platform": "aws", "format": "parquet", "location": location},
        "contract": {
            "schema": [
                {"name": "order_id", "type": "string", "required": True},
                {"name": "qty", "type": "integer"},
                {"name": "price", "type": "decimal(10,2)"},
            ]
        },
    }


class TestAwsGlue:
    def test_database_and_table_emitted(self):
        res = _aws().emit(
            _contract([_glue_exposure(database="sales", table="orders", bucket="lake")])
        )
        assert "aws_glue_catalog_database" in res
        assert "aws_glue_catalog_table" in res
        db = next(iter(res["aws_glue_catalog_database"].values()))
        assert db["name"] == "sales"
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["name"] == "orders"
        assert tbl["table_type"] == "EXTERNAL_TABLE"

    def test_table_references_its_database(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        db_name = next(iter(res["aws_glue_catalog_database"]))
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["database_name"] == f"${{aws_glue_catalog_database.{db_name}.name}}"

    def test_columns_use_hive_types(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        cols = {c["name"]: c["type"] for c in tbl["storage_descriptor"]["columns"]}
        assert cols["order_id"] == "string"
        assert cols["qty"] == "int"
        assert cols["price"] == "decimal(10,2)"

    def test_storage_descriptor_points_at_s3(self):
        res = _aws().emit(
            _contract(
                [_glue_exposure(database="d", table="t", bucket="my-lake", path="sales/curated/")]
            )
        )
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["storage_descriptor"]["location"] == "s3://my-lake/sales/curated/"

    def test_iceberg_format_marks_glue_table_type(self):
        res = _aws().emit(
            _contract(
                [
                    {
                        "exposeId": "t",
                        "binding": {
                            "platform": "aws",
                            "format": "iceberg",
                            "location": {"database": "d", "table": "t", "bucket": "b"},
                        },
                    }
                ]
            )
        )
        params = next(iter(res["aws_glue_catalog_table"].values()))["parameters"]
        assert params["table_type"] == "ICEBERG"
        assert params["classification"] == "iceberg"

    def test_non_iceberg_table_has_no_table_type_parameter(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        params = next(iter(res["aws_glue_catalog_table"].values()))["parameters"]
        assert "table_type" not in params


class TestAwsS3:
    def test_bucket_emitted(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="my-lake")]))
        bkt = next(iter(res["aws_s3_bucket"].values()))
        assert bkt["bucket"] == "my-lake"
        assert bkt["force_destroy"] is True

    def test_shared_bucket_is_deduplicated(self):
        # Two exposures sharing one bucket -> a single aws_s3_bucket resource.
        res = _aws().emit(
            _contract(
                [
                    _glue_exposure(database="d", table="t1", bucket="shared"),
                    _glue_exposure(database="d", table="t2", bucket="shared"),
                ]
            )
        )
        assert len(res["aws_s3_bucket"]) == 1
        assert len(res["aws_glue_catalog_table"]) == 2


class TestAwsModuleOutput:
    def test_non_aws_exposures_are_skipped(self):
        c = _contract(
            [
                {
                    "exposeId": "x",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ]
        )
        assert _aws().emit(c) == {}

    def test_output_is_canonical_and_declares_aws(self):
        c = _contract([_glue_exposure(database="d", table="t", bucket="b")])
        text = build_module(_aws(), c)
        doc = json.loads(text)
        assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
        assert doc["terraform"]["required_providers"]["aws"]["source"] == "hashicorp/aws"


class TestAwsKinesis:
    def test_stream_emitted_on_demand(self):
        res = _aws().emit(_contract([_glue_exposure(stream="events")]))
        assert "aws_kinesis_stream" in res
        stream = next(iter(res["aws_kinesis_stream"].values()))
        assert stream["name"] == "events"
        assert stream["stream_mode_details"] == [{"stream_mode": "ON_DEMAND"}]

    def test_no_stream_means_no_kinesis_resource(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        assert "aws_kinesis_stream" not in res


class TestAwsPlannedActions:
    """``emit(contract, actions)`` — Glue jobs / Step Functions from planner ops."""

    def test_glue_job(self):
        res = _aws().emit(
            _contract([]),
            [
                {
                    "op": "glue.ensure_job",
                    "name": "demo-etl",
                    "role": "arn:aws:iam::123456789012:role/GlueETLRole",
                    "script_location": "s3://demo-staging/scripts/etl.py",
                    "command_name": "glueetl",
                    "glue_version": "4.0",
                    "worker_type": "G.1X",
                    "number_of_workers": 10,
                }
            ],
        )
        job = next(iter(res["aws_glue_job"].values()))
        assert job["role_arn"] == "arn:aws:iam::123456789012:role/GlueETLRole"
        assert job["command"] == {
            "name": "glueetl",
            "script_location": "s3://demo-staging/scripts/etl.py",
        }
        assert job["number_of_workers"] == 10

    def test_glue_job_skipped_without_role_or_script(self):
        res = _aws().emit(
            _contract([]),
            [{"op": "glue.ensure_job", "name": "demo-etl", "command_name": "glueetl"}],
        )
        assert "aws_glue_job" not in res

    def test_state_machine(self):
        res = _aws().emit(
            _contract([]),
            [
                {
                    "op": "stepfunctions.ensure_state_machine",
                    "state_machine_name": "fluid-workflow",
                    "definition": '{"StartAt":"X","States":{"X":{"Type":"Pass","End":true}}}',
                    "role_arn": "arn:aws:iam::123456789012:role/SfnRole",
                    "type": "STANDARD",
                }
            ],
        )
        sm = next(iter(res["aws_sfn_state_machine"].values()))
        assert sm["name"] == "fluid-workflow"
        assert sm["role_arn"] == "arn:aws:iam::123456789012:role/SfnRole"
        assert sm["type"] == "STANDARD"

    def test_lambda_ops_are_skipped(self):
        # The Lambda-based schedule path is imperative (inline code, no
        # deployable artifact) — it must not produce a resource.
        res = _aws().emit(
            _contract([]),
            [
                {"op": "lambda.ensure_function", "function_name": "f", "code": {}},
                {"op": "eventbridge.ensure_schedule", "schedule_name": "s"},
                {"op": "mwaa.ensure_environment", "environment_name": "e"},
            ],
        )
        assert res == {}

    def test_no_actions_emits_no_planned_resources(self):
        assert _aws().emit(_contract([]), []) == {}


class TestAwsLambdaPath:
    """``emit`` + ``emit_data`` — the Lambda schedule / event path."""

    _SRC = "def handler(event, context):\n    return {'statusCode': 200}\n"

    def _fn(self, name):
        return {
            "op": "lambda.ensure_function",
            "function_name": name,
            "runtime": "python3.11",
            "handler": "index.handler",
            "role": "arn:aws:iam::123456789012:role/exec",
            "code": {"ZipFile": self._SRC},
        }

    def test_lambda_function_and_archive(self):
        actions = [self._fn("fn-x")]
        res = _aws().emit(_contract([]), actions)
        data = _aws().emit_data(_contract([]), actions)
        fn = next(iter(res["aws_lambda_function"].values()))
        assert fn["function_name"] == "fn-x"
        assert fn["filename"].startswith("${data.archive_file.")
        arch = next(iter(data["archive_file"].values()))
        assert arch["type"] == "zip"
        assert arch["source"][0]["content"] == self._SRC
        assert arch["source"][0]["filename"] == "index.py"

    def test_lambda_permission_references_function(self):
        actions = [
            self._fn("fn-x"),
            {
                "op": "lambda.add_permission",
                "function_name": "fn-x",
                "statement_id": "AllowX",
                "action": "lambda:InvokeFunction",
                "principal": "events.amazonaws.com",
            },
        ]
        perm = next(iter(_aws().emit(_contract([]), actions)["aws_lambda_permission"].values()))
        assert perm["function_name"].startswith("${aws_lambda_function.")
        assert perm["principal"] == "events.amazonaws.com"

    def test_scheduler_schedule(self):
        actions = [
            self._fn("fn-s"),
            {
                "op": "eventbridge.ensure_schedule",
                "schedule_name": "s",
                "schedule_expression": "rate(1 hour)",
                "flexible_time_window": {"mode": "OFF"},
                "target": {
                    "arn": "arn:aws:lambda:us-east-1:1:function:fn-s",
                    "role_arn": "arn:aws:iam::1:role/r",
                },
            },
        ]
        sched = next(iter(_aws().emit(_contract([]), actions)["aws_scheduler_schedule"].values()))
        assert sched["flexible_time_window"] == {"mode": "OFF"}
        assert sched["target"]["arn"].startswith("${aws_lambda_function.")

    def test_event_rule_and_target(self):
        actions = [
            self._fn("fn-e"),
            {
                "op": "eventbridge.ensure_rule",
                "rule_name": "r",
                "event_pattern": '{"source":["aws.s3"]}',
                "targets": [{"id": "1", "arn": "arn:aws:lambda:us-east-1:1:function:fn-e"}],
            },
        ]
        res = _aws().emit(_contract([]), actions)
        assert "aws_cloudwatch_event_rule" in res
        tgt = next(iter(res["aws_cloudwatch_event_target"].values()))
        assert tgt["rule"].startswith("${aws_cloudwatch_event_rule.")
        assert tgt["arn"].startswith("${aws_lambda_function.")

    def test_s3_notification(self):
        actions = [
            self._fn("fn-n"),
            {
                "op": "s3.ensure_notification",
                "bucket": "b",
                "lambda_function_arn": "arn:aws:lambda:us-east-1:1:function:fn-n",
                "events": ["s3:ObjectCreated:*"],
                "filter": {"prefix": "in/"},
            },
        ]
        notif = next(
            iter(_aws().emit(_contract([]), actions)["aws_s3_bucket_notification"].values())
        )
        assert notif["bucket"] == "b"
        assert notif["lambda_function"][0]["filter_prefix"] == "in/"

    def test_event_source_mapping(self):
        actions = [
            self._fn("fn-m"),
            {
                "op": "lambda.create_event_source_mapping",
                "function_name": "fn-m",
                "event_source_arn": "arn:aws:sqs:us-east-1:1:q",
                "batch_size": 10,
            },
        ]
        esm = next(
            iter(_aws().emit(_contract([]), actions)["aws_lambda_event_source_mapping"].values())
        )
        assert esm["event_source_arn"] == "arn:aws:sqs:us-east-1:1:q"
        assert esm["batch_size"] == 10

    def test_function_without_code_skipped(self):
        # No deployable source → no resource (and no dangling archive ref).
        actions = [
            {
                "op": "lambda.ensure_function",
                "function_name": "f",
                "role": "arn:aws:iam::1:role/r",
            }
        ]
        assert _aws().emit(_contract([]), actions) == {}
        assert _aws().emit_data(_contract([]), actions) == {}

    def test_no_data_when_no_lambda(self):
        assert _aws().emit_data(_contract([]), []) == {}


def _redshift_serverless_exposure(**location):
    """Exposure that provisions a Redshift Serverless namespace + workgroup."""
    return {
        "exposeId": "redshift_compute",
        "binding": {
            "platform": "aws",
            "format": "redshift_serverless",
            "location": location,
        },
    }


def _redshift_external_schema_exposure(**location):
    """Exposure that publishes a Redshift external schema over Glue Catalog."""
    return {
        "exposeId": "redshift_via_glue",
        "binding": {
            "platform": "aws",
            "format": "redshift_external_schema",
            "location": location,
        },
    }


class TestAwsRedshiftServerless:
    """``redshift_serverless`` exposure → paired namespace + workgroup."""

    def test_emits_namespace_and_workgroup(self):
        res = _aws().emit(
            _contract(
                [
                    _redshift_serverless_exposure(
                        namespace="fluid_mesh",
                        workgroup="fluid_mesh_wg",
                        database="fluid",
                        base_capacity=8,
                    )
                ]
            )
        )
        assert "aws_redshiftserverless_namespace" in res
        assert "aws_redshiftserverless_workgroup" in res
        ns = next(iter(res["aws_redshiftserverless_namespace"].values()))
        wg = next(iter(res["aws_redshiftserverless_workgroup"].values()))
        assert ns["namespace_name"] == "fluid_mesh"
        assert ns["db_name"] == "fluid"
        assert wg["workgroup_name"] == "fluid_mesh_wg"
        assert wg["base_capacity"] == 8

    def test_workgroup_references_namespace_by_ref(self):
        # Value reference creates the namespace → workgroup ordering edge.
        res = _aws().emit(_contract([_redshift_serverless_exposure(namespace="n", workgroup="w")]))
        ns_key = next(iter(res["aws_redshiftserverless_namespace"]))
        wg = next(iter(res["aws_redshiftserverless_workgroup"].values()))
        assert wg["namespace_name"] == (
            f"${{aws_redshiftserverless_namespace.{ns_key}.namespace_name}}"
        )

    def test_iam_role_wires_namespace_default_role(self):
        res = _aws().emit(
            _contract(
                [
                    _redshift_serverless_exposure(
                        namespace="n",
                        workgroup="w",
                        iam_role_arn="arn:aws:iam::1:role/Spectrum",
                    )
                ]
            )
        )
        ns = next(iter(res["aws_redshiftserverless_namespace"].values()))
        assert ns["iam_roles"] == ["arn:aws:iam::1:role/Spectrum"]
        assert ns["default_iam_role_arn"] == "arn:aws:iam::1:role/Spectrum"

    def test_missing_namespace_or_workgroup_emits_nothing(self):
        # Both are required — without either the compute pair is left external.
        assert _aws().emit(_contract([_redshift_serverless_exposure(workgroup="w")])) == {}
        assert _aws().emit(_contract([_redshift_serverless_exposure(namespace="n")])) == {}


class TestAwsRedshiftExternalSchema:
    """``redshift_external_schema`` → ``null_resource`` running
    ``CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`` via the
    ``redshift-data`` API (the documented bridge for an upstream
    Terraform provider gap)."""

    def _ext(self, **overrides):
        loc = {
            "workgroup": "fluid_mesh_wg",
            "database": "fluid",
            "external_schema": "ext_silver",
            "glue_database": "silver_events",
            "iam_role_arn": "arn:aws:iam::1:role/RedshiftSpectrum",
        }
        loc.update(overrides)
        return _redshift_external_schema_exposure(**loc)

    def test_emits_null_resource_with_create_external_schema_sql(self):
        res = _aws().emit(_contract([self._ext()]))
        assert "null_resource" in res
        body = next(iter(res["null_resource"].values()))
        cmd = body["provisioner"][0]["local-exec"]["command"]
        assert "aws redshift-data execute-statement" in cmd
        assert "--workgroup-name fluid_mesh_wg" in cmd
        assert "--database fluid" in cmd
        assert "CREATE EXTERNAL SCHEMA IF NOT EXISTS ext_silver" in cmd
        assert "FROM DATA CATALOG" in cmd
        assert "DATABASE 'silver_events'" in cmd
        assert "IAM_ROLE 'arn:aws:iam::1:role/RedshiftSpectrum'" in cmd

    def test_triggers_carry_inputs_for_re_exec_on_change(self):
        res = _aws().emit(_contract([self._ext()]))
        body = next(iter(res["null_resource"].values()))
        triggers = body["triggers"]
        assert triggers["schema"] == "ext_silver"
        assert triggers["workgroup"] == "fluid_mesh_wg"
        assert triggers["glue_database"] == "silver_events"
        assert triggers["iam_role"] == "arn:aws:iam::1:role/RedshiftSpectrum"

    def test_region_clause_included_when_supplied(self):
        res = _aws().emit(_contract([self._ext(region="us-west-2")]))
        cmd = next(iter(res["null_resource"].values()))["provisioner"][0]["local-exec"]["command"]
        assert "REGION 'us-west-2'" in cmd

    def test_region_clause_omitted_when_absent(self):
        # No region → no REGION clause (workgroup-local Glue catalog).
        res = _aws().emit(_contract([self._ext()]))
        cmd = next(iter(res["null_resource"].values()))["provisioner"][0]["local-exec"]["command"]
        assert "REGION " not in cmd

    def test_missing_required_inputs_emits_nothing(self):
        for missing in ("workgroup", "external_schema", "glue_database", "iam_role_arn"):
            loc = {
                "workgroup": "w",
                "external_schema": "e",
                "glue_database": "g",
                "iam_role_arn": "arn:aws:iam::1:role/r",
            }
            loc.pop(missing)
            assert _aws().emit(_contract([_redshift_external_schema_exposure(**loc)])) == {}


class TestAwsRedshiftDependencyWiring:
    """The bridge null_resource must run AFTER its workgroup and the
    upstream Glue Catalog database — both literal-named in the SQL so
    OpenTofu carries no value-derived edge. The post-emit pass attaches
    ``depends_on`` for matches in the same module."""

    def test_depends_on_workgroup_when_same_module_provisions_it(self):
        c = _contract(
            [
                _redshift_serverless_exposure(namespace="ns", workgroup="wg", database="fluid"),
                _redshift_external_schema_exposure(
                    workgroup="wg",
                    database="fluid",
                    external_schema="ext",
                    glue_database="g",
                    iam_role_arn="arn:aws:iam::1:role/r",
                ),
            ]
        )
        res = _aws().emit(c)
        body = next(iter(res["null_resource"].values()))
        assert any(
            d.startswith("aws_redshiftserverless_workgroup.") for d in body.get("depends_on", [])
        )

    def test_depends_on_glue_database_when_same_module_provisions_it(self):
        # The mesh interface: the bridge references a Glue catalog database
        # that this module also emits → tofu must create the Glue db first.
        c = _contract(
            [
                _glue_exposure(database="silver_events", table="events", bucket="lake"),
                _redshift_external_schema_exposure(
                    workgroup="wg",
                    database="fluid",
                    external_schema="ext_silver",
                    glue_database="silver_events",
                    iam_role_arn="arn:aws:iam::1:role/r",
                ),
            ]
        )
        res = _aws().emit(c)
        body = next(iter(res["null_resource"].values()))
        assert any(d.startswith("aws_glue_catalog_database.") for d in body.get("depends_on", []))

    def test_depends_on_handles_order_independence(self):
        # The dep wiring must work regardless of which exposure appears
        # first in the list — the post-emit pass scans the *final* resources.
        c = _contract(
            [
                _redshift_external_schema_exposure(
                    workgroup="wg",
                    database="fluid",
                    external_schema="ext",
                    glue_database="silver_events",
                    iam_role_arn="arn:aws:iam::1:role/r",
                ),
                _redshift_serverless_exposure(namespace="ns", workgroup="wg"),
                _glue_exposure(database="silver_events", table="events", bucket="lake"),
            ]
        )
        res = _aws().emit(c)
        body = next(iter(res["null_resource"].values()))
        deps = body.get("depends_on", [])
        assert any(d.startswith("aws_redshiftserverless_workgroup.") for d in deps)
        assert any(d.startswith("aws_glue_catalog_database.") for d in deps)

    def test_external_container_no_depends_on(self):
        # Workgroup and Glue database are pre-existing (not in this module) —
        # the bridge applies against external infrastructure, no deps emitted.
        c = _contract(
            [
                _redshift_external_schema_exposure(
                    workgroup="external_wg",
                    database="fluid",
                    external_schema="ext",
                    glue_database="external_glue",
                    iam_role_arn="arn:aws:iam::1:role/r",
                )
            ]
        )
        res = _aws().emit(c)
        body = next(iter(res["null_resource"].values()))
        assert "depends_on" not in body


class TestAwsRedshiftModuleOutput:
    """The full module must declare the ``null`` provider and stay
    secret-free even when Redshift resources are emitted."""

    def test_required_providers_include_null(self):
        # The Redshift external-schema bridge uses ``null_resource``, so
        # the emitted module must declare the `hashicorp/null` provider.
        text = build_module(
            _aws(),
            _contract(
                [
                    _redshift_external_schema_exposure(
                        workgroup="wg",
                        database="fluid",
                        external_schema="ext",
                        glue_database="g",
                        iam_role_arn="arn:aws:iam::1:role/r",
                    )
                ]
            ),
        )
        doc = json.loads(text)
        assert "null" in doc["terraform"]["required_providers"]
        assert doc["terraform"]["required_providers"]["null"]["source"] == "hashicorp/null"
