# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the GCP IaC plugin — contract -> .tf.json translation.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _gcp():
    return get_iac_plugin("gcp")


def _contract(exposes):
    return {"id": "analytics.demo", "name": "Demo", "exposes": exposes}


class TestGcpBigQuery:
    def test_table_emits_dataset_and_table(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "orders",
                        "binding": {
                            "platform": "gcp",
                            "format": "bigquery_table",
                            "location": {"dataset": "sales", "table": "orders"},
                        },
                        "contract": {
                            "schema": [
                                {"name": "id", "type": "integer", "required": True},
                                {"name": "note", "type": "string"},
                            ]
                        },
                    }
                ]
            )
        )
        assert "google_bigquery_dataset" in res
        assert "google_bigquery_table" in res
        ds = next(iter(res["google_bigquery_dataset"].values()))
        assert ds["dataset_id"] == "sales"
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["table_id"] == "orders"
        assert tbl["deletion_protection"] is False
        by_name = {f["name"]: f for f in json.loads(tbl["schema"])}
        assert by_name["id"]["type"] == "INT64"
        assert by_name["id"]["mode"] == "REQUIRED"
        assert by_name["note"]["type"] == "STRING"
        assert by_name["note"]["mode"] == "NULLABLE"

    def test_table_cross_references_its_dataset(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "t",
                        "binding": {
                            "format": "bigquery_table",
                            "location": {"dataset": "d", "table": "t"},
                        },
                    }
                ]
            )
        )
        ds_name = next(iter(res["google_bigquery_dataset"]))
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["dataset_id"] == f"${{google_bigquery_dataset.{ds_name}.dataset_id}}"

    def test_view_emits_view_block_not_schema(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "v",
                        "binding": {
                            "format": "bigquery_view",
                            "location": {"dataset": "d", "view": "v", "query": "SELECT 1"},
                        },
                    }
                ]
            )
        )
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["view"]["query"] == "SELECT 1"
        assert tbl["view"]["use_legacy_sql"] is False
        assert "schema" not in tbl


class TestGcpStorageAndPubsub:
    def test_gcs_bucket(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "b",
                        "binding": {
                            "format": "gcs_bucket",
                            "location": {"bucket": "my-bucket", "region": "EU"},
                        },
                    }
                ]
            )
        )
        bkt = next(iter(res["google_storage_bucket"].values()))
        assert bkt["name"] == "my-bucket"
        assert bkt["location"] == "EU"
        assert bkt["uniform_bucket_level_access"] is True

    def test_pubsub_topic(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "e",
                        "binding": {"format": "pubsub_topic", "location": {"topic": "events"}},
                    }
                ]
            )
        )
        topic = next(iter(res["google_pubsub_topic"].values()))
        assert topic["name"] == "events"

    def test_pubsub_topic_with_subscription(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "e",
                        "binding": {
                            "format": "pubsub_topic",
                            "location": {"topic": "events", "subscription": "events-sub"},
                        },
                    }
                ]
            )
        )
        topic_res = next(iter(res["google_pubsub_topic"]))
        sub = next(iter(res["google_pubsub_subscription"].values()))
        assert sub["name"] == "events-sub"
        assert sub["topic"] == f"${{google_pubsub_topic.{topic_res}.name}}"

    def test_pubsub_topic_without_subscription_emits_none(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "e",
                        "binding": {"format": "pubsub_topic", "location": {"topic": "t"}},
                    }
                ]
            )
        )
        assert "google_pubsub_subscription" not in res


class TestGcpModuleOutput:
    def test_empty_contract_yields_empty_resources(self):
        # An empty `resource` object is invalid OpenTofu, so the key is
        # omitted entirely — the module is just the terraform{} block.
        doc = json.loads(build_module(_gcp(), _contract([])))
        assert doc.get("resource", {}) == {}
        assert "resource" not in doc

    def test_non_gcp_formats_are_skipped(self):
        c = _contract(
            [{"exposeId": "x", "binding": {"format": "parquet", "location": {"table": "t"}}}]
        )
        assert _gcp().emit(c) == {}

    def test_output_is_canonical(self):
        c = _contract(
            [
                {
                    "exposeId": "t",
                    "binding": {
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ]
        )
        text = build_module(_gcp(), c)
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"

    def test_required_providers_declares_google(self):
        doc = json.loads(build_module(_gcp(), _contract([])))
        assert doc["terraform"]["required_providers"]["google"]["source"] == "hashicorp/google"


class TestGcpIam:
    """``metadata.policies`` → BigQuery dataset access entries + GCS IAM members."""

    _POLICIES = {
        "policies": {
            "analysts": {
                "principals": ["alice@example.com", "data-team"],
                "permissions": ["read"],
            },
            "writers": {
                "principals": ["svc@proj.iam.gserviceaccount.com"],
                "permissions": ["write"],
            },
        }
    }

    def _with_policies(self, exposes):
        return {
            "id": "analytics.demo",
            "name": "Demo",
            "metadata": self._POLICIES,
            "exposes": exposes,
        }

    def test_bigquery_dataset_gets_access_entries(self):
        res = _gcp().emit(
            self._with_policies(
                [
                    {
                        "exposeId": "t",
                        "binding": {
                            "format": "bigquery_table",
                            "location": {"dataset": "d", "table": "t"},
                        },
                    }
                ]
            )
        )
        access = next(iter(res["google_bigquery_dataset"].values()))["access"]
        assert {e["role"] for e in access} == {"READER", "WRITER"}
        # '@' -> user_by_email; bare name -> group_by_email
        assert {"role": "READER", "user_by_email": "alice@example.com"} in access
        assert {"role": "READER", "group_by_email": "data-team"} in access

    def test_gcs_bucket_gets_iam_members(self):
        res = _gcp().emit(
            self._with_policies(
                [
                    {
                        "exposeId": "b",
                        "binding": {"format": "gcs_bucket", "location": {"bucket": "my-bucket"}},
                    }
                ]
            )
        )
        members = res["google_storage_bucket_iam_member"].values()
        assert {m["role"] for m in members} == {
            "roles/storage.objectViewer",
            "roles/storage.objectCreator",
        }
        member_strs = {m["member"] for m in members}
        assert "user:alice@example.com" in member_strs
        assert "group:data-team" in member_strs
        assert "serviceAccount:svc@proj.iam.gserviceaccount.com" in member_strs

    def test_no_policies_means_no_iam(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "t",
                        "binding": {
                            "format": "bigquery_table",
                            "location": {"dataset": "d", "table": "t"},
                        },
                    }
                ]
            )
        )
        assert "access" not in next(iter(res["google_bigquery_dataset"].values()))
        assert "google_storage_bucket_iam_member" not in res


class TestGcpPlannedActions:
    """``emit(contract, actions)`` — Cloud Run / Scheduler / Pub-Sub from planner ops."""

    def test_cloud_run_service(self):
        res = _gcp().emit(
            _contract([]),
            [
                {
                    "op": "run.ensure_service",
                    "project": "p",
                    "region": "us-central1",
                    "service_name": "fluid-demo",
                    "image": "gcr.io/fluid-forge/runner:latest",
                    "cpu": "1",
                    "memory": "512Mi",
                    "concurrency": 4,
                    "env_vars": {"FLUID_PROJECT": "p"},
                    "min_instances": 0,
                    "max_instances": 10,
                }
            ],
        )
        svc = next(iter(res["google_cloud_run_v2_service"].values()))
        assert svc["name"] == "fluid-demo"
        assert svc["location"] == "us-central1"
        assert svc["deletion_protection"] is False
        container = svc["template"]["containers"][0]
        assert container["image"] == "gcr.io/fluid-forge/runner:latest"
        assert container["env"] == [{"name": "FLUID_PROJECT", "value": "p"}]
        assert svc["template"]["scaling"] == {"min_instance_count": 0, "max_instance_count": 10}

    def test_cloud_scheduler_job(self):
        res = _gcp().emit(
            _contract([]),
            [
                {
                    "op": "scheduler.ensure_job",
                    "job_name": "fluid-job",
                    "location": "us-central1",
                    "schedule": "0 2 * * *",
                    "timezone": "UTC",
                    "target": {
                        "http_target": {
                            "uri": "https://x.a.run.app/execute",
                            "http_method": "POST",
                        }
                    },
                }
            ],
        )
        job = next(iter(res["google_cloud_scheduler_job"].values()))
        assert job["schedule"] == "0 2 * * *"
        assert job["region"] == "us-central1"
        assert job["time_zone"] == "UTC"
        assert job["http_target"]["uri"] == "https://x.a.run.app/execute"

    def test_scheduler_skipped_without_schedule(self):
        res = _gcp().emit(
            _contract([]),
            [
                {
                    "op": "scheduler.ensure_job",
                    "job_name": "j",
                    "target": {"http_target": {"uri": "https://x"}},
                }
            ],
        )
        assert "google_cloud_scheduler_job" not in res

    def test_pubsub_push_subscription(self):
        res = _gcp().emit(
            _contract([]),
            [
                {"op": "ps.ensure_topic", "topic": "evt", "message_retention_duration": "604800s"},
                {
                    "op": "ps.ensure_subscription",
                    "topic": "evt",
                    "subscription": "evt-sub",
                    "push_config": {"push_endpoint": "https://x.a.run.app/pubsub"},
                },
            ],
        )
        topic = next(iter(res["google_pubsub_topic"].values()))
        assert topic["message_retention_duration"] == "604800s"
        sub = next(iter(res["google_pubsub_subscription"].values()))
        assert sub["push_config"]["push_endpoint"] == "https://x.a.run.app/pubsub"
        assert sub["topic"].startswith("${google_pubsub_topic.")

    def test_bigquery_table_iam(self):
        res = _gcp().emit(
            _contract([]),
            [
                {
                    "op": "iam.bind_bq_table",
                    "dataset": "analytics",
                    "table": "events",
                    "policies": {
                        "readers": {
                            "principals": ["analyst@example.com"],
                            "permissions": ["read"],
                        }
                    },
                }
            ],
        )
        member = next(iter(res["google_bigquery_table_iam_member"].values()))
        assert member["dataset_id"] == "analytics"
        assert member["table_id"] == "events"
        assert member["role"] == "roles/bigquery.dataViewer"
        assert member["member"] == "user:analyst@example.com"

    def test_composer_dag_deploy(self):
        res = _gcp().emit(
            _contract([]),
            [
                {
                    "op": "composer.deploy_dag",
                    "dag_id": "fluid_demo",
                    "environment": "fluid-composer",
                    "dag_bucket": "us-central1-env-abc-bucket",
                    "dag_content": "from airflow import DAG\n",
                }
            ],
        )
        obj = next(iter(res["google_storage_bucket_object"].values()))
        assert obj["name"] == "dags/fluid_demo.py"
        assert obj["bucket"] == "us-central1-env-abc-bucket"
        assert obj["content"] == "from airflow import DAG\n"

    def test_composer_dag_skipped_without_bucket(self):
        # No DAG bucket → cannot upload declaratively → skipped.
        res = _gcp().emit(
            _contract([]),
            [{"op": "composer.deploy_dag", "dag_id": "d", "environment": "e", "dag_content": "x"}],
        )
        assert res == {}

    def test_composer_trigger_dag_is_skipped(self):
        # composer.trigger_dag (a one-off run) has no declarative form.
        res = _gcp().emit(_contract([]), [{"op": "composer.trigger_dag", "dag_id": "d"}])
        assert res == {}

    def test_no_actions_emits_no_planned_resources(self):
        assert _gcp().emit(_contract([]), []) == {}
