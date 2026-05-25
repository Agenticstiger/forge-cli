# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""GCP Stage 2 — Docker emulator round-trips for the GCP IaC plugin.

The Stage-2 ladder for GCP, mirroring AWS LocalStack:

  Stage 1 (unit) → `test_iac_gcp.py` + `test_iac_tofu_validate.py[gcp]`
  Stage 2 (docker emulator) → THIS FILE
  Stage 3 (real GCP) → `test_iac_gcp_real_e2e.py` (separate, gated)

Three emulators back this layer:

  * goccy/bigquery-emulator (ghcr.io/goccy/bigquery-emulator)
  * fsouza/fake-gcs-server  (fsouza/fake-gcs-server)
  * gcloud beta emulators pubsub (google/cloud-sdk)

Tests SKIP when any gate is closed — safe to run in the light suite,
safe to run on machines without docker.

Stage-2 honest scope — hybrid emit-and-verify
=============================================

Unlike AWS LocalStack (which implements enough of the AWS API surface
for ``tofu apply`` to round-trip cleanly), the OSS GCP emulators are
designed for **client-library** testing, not for hashicorp/google
provider compatibility. ``tofu apply`` against the emulators reliably
creates the first resource and then crashes the Google provider plugin
on read-back/refresh ("Plugin did not respond"). The
``test_iac_gcp_emulator_e2e.py`` predecessor of this file confirmed
this for Pub/Sub; this rewrite confirmed it for BigQuery, GCS, and
multi-exposure contracts.

So Stage 2 for GCP verifies the two halves separately:

  * **Emitter correctness** — ``tofu init`` + ``tofu plan`` against the
    emulator-overlaid module. Plan does NOT crash the provider; it
    validates the schema, resolves references, and prints the changeset.
    A successful plan + the right add/change/destroy counts proves the
    emitted ``.tf.json`` is well-formed for the real provider.
  * **Emulator + resource shape correctness** — create the matching
    logical resource via the official google-cloud-* Python SDK pointed
    at the emulator, then read it back. Proves the resource SHAPE the
    emitter produces (column types, location, ID format) is what the
    emulator expects.

Full ``tofu apply`` round-trip lives in Stage 3 (real GCP). The cost is
deliberate: the savings from avoiding the provider/emulator bug far
outweigh the loss of a single apply-step check in Stage 2.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.iac import runner

from .conftest import (
    GCP_EMULATOR_ENABLED,
    GCP_EMULATOR_PROJECT,
    GCP_EMULATOR_SKIP_REASON,
    gcp_emulator_bigquery_client,
    gcp_emulator_storage_client,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.gcp,
    pytest.mark.provider,
    pytest.mark.emulated_heavy,
    pytest.mark.skipif(not GCP_EMULATOR_ENABLED, reason=GCP_EMULATOR_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Half A — emitter correctness via ``tofu plan`` (creds-free, emulator-overlaid)
# ---------------------------------------------------------------------------


def test_emu_bigquery_dataset_and_table_plan_clean(gcp_emulator_project):
    """Emitter half: a BQ dataset+table contract → emit → tofu init/plan.
    Plan must succeed and propose exactly 2 adds (dataset + table)."""
    contract = {
        "id": "emu.bq.basic",
        "name": "BQ basic",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "emu_silver", "table": "events"},
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "occurred_at", "type": "timestamp"},
                    ]
                },
            }
        ],
    }
    gcp_emulator_project.emit(contract)
    init = gcp_emulator_project.init()
    assert init.ok, init.stderr or init.stdout
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr or plan.stdout
    summary = runner.change_summary(plan)
    assert summary["add"] == 2, summary


def test_emu_bigquery_view_plan_clean(gcp_emulator_project):
    """Emitter half: a BQ view contract → plan = 2 adds (dataset + view)."""
    contract = {
        "id": "emu.bq.view",
        "name": "BQ view",
        "exposes": [
            {
                "exposeId": "v",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_view",
                    "location": {
                        "dataset": "emu_views",
                        "view": "events_v",
                        "query": "SELECT 1 AS one",
                    },
                },
            }
        ],
    }
    gcp_emulator_project.emit(contract)
    assert gcp_emulator_project.init().ok
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr
    assert runner.change_summary(plan)["add"] == 2


def test_emu_gcs_bucket_plan_clean(gcp_emulator_project):
    """Emitter half: a GCS bucket contract → plan = 1 add (the bucket)."""
    contract = {
        "id": "emu.gcs",
        "name": "GCS",
        "exposes": [
            {
                "exposeId": "raw",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": "fluid-emu-bucket-events", "region": "EU"},
                },
            }
        ],
    }
    gcp_emulator_project.emit(contract)
    assert gcp_emulator_project.init().ok
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr
    assert runner.change_summary(plan)["add"] == 1


def test_emu_pubsub_topic_and_subscription_plan_clean(gcp_emulator_project):
    """Emitter half: a Pub/Sub binding → plan = 2 adds (topic + sub)."""
    contract = {
        "id": "emu.ps",
        "name": "PubSub",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "gcp",
                    "format": "pubsub_topic",
                    "location": {
                        "topic": "fluid-emu-events",
                        "subscription": "fluid-emu-events-sub",
                    },
                },
            }
        ],
    }
    gcp_emulator_project.emit(contract)
    assert gcp_emulator_project.init().ok
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr
    assert runner.change_summary(plan)["add"] == 2


def test_emu_multi_exposure_plan_clean(gcp_emulator_project):
    """Emitter half: a contract mixing BQ + GCS + Pub/Sub → plan = 4 adds.
    Catches missing ``depends_on`` / cross-resource ordering bugs that
    single-format tests can't see."""
    contract = {
        "id": "emu.mix",
        "name": "Mix",
        "exposes": [
            {
                "exposeId": "tbl",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "emu_mix_pl", "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
            {
                "exposeId": "bkt",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": "fluid-emu-mix-pl-bucket", "region": "EU"},
                },
            },
            {
                "exposeId": "topic",
                "binding": {
                    "platform": "gcp",
                    "format": "pubsub_topic",
                    "location": {"topic": "fluid-emu-mix-pl-events"},
                },
            },
        ],
    }
    gcp_emulator_project.emit(contract)
    assert gcp_emulator_project.init().ok
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr
    # dataset + table + bucket + topic = 4
    assert runner.change_summary(plan)["add"] == 4


# ---------------------------------------------------------------------------
# Half B — emulators+resource-shape via Python SDK
# ---------------------------------------------------------------------------


def test_emu_bigquery_sdk_create_read():
    """Emulator half: create a dataset + table using google-cloud-bigquery
    against the emulator, read them back. Proves the emulator handles the
    same resource shape forge-cli's plugin emits."""
    from google.cloud import bigquery

    bq = gcp_emulator_bigquery_client()
    dataset_id = f"{GCP_EMULATOR_PROJECT}.sdk_probe"
    table_id = f"{dataset_id}.events"

    ds = bigquery.Dataset(dataset_id)
    ds.location = "US"
    bq.create_dataset(ds, exists_ok=True)

    schema = [
        bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("occurred_at", "TIMESTAMP"),
    ]
    bq.create_table(bigquery.Table(table_id, schema=schema), exists_ok=True)

    got = bq.get_table(table_id)
    cols = {f.name: f.field_type for f in got.schema}
    assert cols == {"event_id": "STRING", "occurred_at": "TIMESTAMP"}


def test_emu_gcs_sdk_create_read():
    """Emulator half: create a bucket via google-cloud-storage against
    the fake-gcs-server, read it back."""
    gcs = gcp_emulator_storage_client()
    bucket_name = "fluid-emu-sdk-probe"
    # ``exists_ok`` idiom: get-or-create.
    try:
        bucket = gcs.get_bucket(bucket_name)
    except Exception:  # noqa: BLE001
        bucket = gcs.create_bucket(bucket_name, location="EU")
    assert bucket.name == bucket_name


def test_emu_pubsub_sdk_create_read():
    """Emulator half: create a topic + subscription via google-cloud-pubsub
    against the gcloud emulator, read them back."""
    import os

    # google-cloud-pubsub reads PUBSUB_EMULATOR_HOST from env (already set
    # by the outer test harness). Explicit ANYWAY for documentation.
    os.environ.setdefault("PUBSUB_EMULATOR_HOST", "localhost:8085")
    from google.cloud import pubsub_v1

    pub = pubsub_v1.PublisherClient()
    topic = pub.topic_path(GCP_EMULATOR_PROJECT, "sdk-probe-topic")
    try:
        pub.create_topic(request={"name": topic})
    except Exception:  # already-exists; idempotent for the emulator
        pass
    assert pub.get_topic(request={"topic": topic}).name == topic

    sub_client = pubsub_v1.SubscriberClient()
    sub = sub_client.subscription_path(GCP_EMULATOR_PROJECT, "sdk-probe-sub")
    try:
        sub_client.create_subscription(request={"name": sub, "topic": topic})
    except Exception:
        pass
    got = sub_client.get_subscription(request={"subscription": sub})
    assert got.topic == topic


# ---------------------------------------------------------------------------
# Backwards-compat — keep the existing Pub/Sub apply-round-trip but xfail
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "OSS GCP emulators do not implement enough of the hashicorp/google "
        "provider's read-back paths for `tofu apply` to round-trip cleanly. "
        "The provider creates the resource then crashes on the post-create "
        "refresh ('Plugin did not respond'). Stage 2 verifies the emitter "
        "via `tofu plan` and the emulators via the Python SDK; full "
        "`tofu apply` round-trips live in Stage 3 (real GCP). Tracked as an "
        "upstream limitation — see goccy/bigquery-emulator and "
        "hashicorp/terraform-provider-google issues."
    ),
    strict=False,
)
def test_emu_tofu_apply_round_trip_xfail(gcp_emulator_project):
    """Known-failing apply round-trip — kept so regressions surface if
    the upstream emulator/provider gap ever closes."""
    contract = {
        "id": "emu.xfail",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "xfail_ds", "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    gcp_emulator_project.apply_ok(contract)
