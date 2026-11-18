# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

import uuid
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
    Plan must succeed and propose exactly 3 adds (dataset, table and the contract's
    decimal-parameters marker)."""
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
    assert summary["add"] == 3, summary


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
    # dataset + table + decimal-parameters marker + bucket + topic = 5
    assert runner.change_summary(plan)["add"] == 5


# ---------------------------------------------------------------------------
# Half B — emulators+resource-shape via Python SDK
# ---------------------------------------------------------------------------


def test_emu_bigquery_sdk_create_read():
    """Emulator half: create a dataset + table using google-cloud-bigquery
    against the emulator, read them back. Proves the emulator handles the
    same resource shape forge-cli's plugin emits."""
    from google.cloud import bigquery

    bq = gcp_emulator_bigquery_client()
    # Unique per run, matching the pattern in
    # tests/providers/test_bigquery_emulated_happy_path.py. A fixed name
    # made this test pass exactly ONCE per emulator container: goccy/
    # bigquery-emulator answers a repeat create with
    # ``internalError: dataset <id> is already created`` where real
    # BigQuery answers 409 alreadyExists. ``exists_ok=True`` is
    # implemented by catching the 409, so the client never saw an
    # "already exists" — it saw a retryable server error, backed off,
    # and burned the deadline. CI always passed it on a fresh container;
    # running it twice locally hung for minutes and then failed.
    dataset_id = f"{GCP_EMULATOR_PROJECT}.sdk_probe_{uuid.uuid4().hex[:8]}"
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


def _nested_contract(dataset: str) -> Dict[str, Any]:
    """A 0.7.7 table with an array of strings, a struct, an array of structs and a map."""
    return {
        "fluidVersion": "0.7.7",
        "id": "emu.bq.nested",
        "name": "BQ nested",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": dataset, "table": "orders"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {
                            "name": "tags",
                            "type": "array",
                            "required": True,
                            "element": {"type": "string", "required": True},
                        },
                        {
                            "name": "address",
                            "type": "struct",
                            "fields": [
                                {"name": "street", "type": "string"},
                                {"name": "zip", "type": "string"},
                            ],
                        },
                        {
                            "name": "items",
                            "type": "array",
                            "required": True,
                            "element": {
                                "type": "struct",
                                "required": True,
                                "fields": [
                                    {"name": "sku", "type": "string", "required": True},
                                    {"name": "qty", "type": "integer"},
                                ],
                            },
                        },
                        {
                            "name": "attrs",
                            "type": "map",
                            "required": True,
                            "key": {"type": "string", "required": True},
                            "value": {"type": "string"},
                        },
                    ]
                },
            }
        ],
    }


def _planned_adds(gcp_emulator_project, contract: Dict[str, Any]) -> int:
    """Emit, init and plan ``contract`` against the emulator: the resources it adds."""
    gcp_emulator_project.emit(contract)
    init = gcp_emulator_project.init()
    assert init.ok, init.stderr or init.stdout
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr or plan.stdout
    return runner.change_summary(plan)["add"]


def test_emu_bigquery_nested_table_plan_clean(gcp_emulator_project):
    """Emitter half: REPEATED and RECORD fields plan against the emulator: dataset,
    table and the contract's decimal-parameters marker."""
    contract = _nested_contract(f"emu_nested_{uuid.uuid4().hex[:8]}")
    assert _planned_adds(gcp_emulator_project, contract) == 3


def _emulator_table(contract: Dict[str, Any], prefix: str):
    """The emitted table, made on the emulator from the BigQuery API's own field JSON:
    the client, the table's reference, its dataset id, and its fields read back by name."""
    import json

    from google.cloud import bigquery

    from fluid_build.iac import build_module, get_iac_plugin

    dataset_id = f"{GCP_EMULATOR_PROJECT}.{prefix}_{uuid.uuid4().hex[:8]}"
    module = json.loads(build_module(get_iac_plugin("gcp"), contract))
    [table_body] = module["resource"]["google_bigquery_table"].values()
    schema = [bigquery.SchemaField.from_api_repr(f) for f in json.loads(table_body["schema"])]

    bq = gcp_emulator_bigquery_client()
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    bq.create_dataset(dataset, exists_ok=True)
    table = bigquery.Table(f"{dataset_id}.orders", schema=schema)
    bq.create_table(table)
    got = {f.name: f for f in bq.get_table(table.reference).schema}
    return bq, table.reference, dataset_id, got


def test_emu_bigquery_nested_columns_round_trip_rows():
    """Emulator half: the emitted schema makes a table the emulator stores and queries:
    arrays, records and a map as key and value records."""
    bq, table, dataset_id, got = _emulator_table(_nested_contract("unused"), "emu_nested_rows")
    assert (got["tags"].field_type, got["tags"].mode) == ("STRING", "REPEATED")
    assert (got["address"].field_type, [f.name for f in got["address"].fields]) == (
        "RECORD",
        ["street", "zip"],
    )
    assert (got["items"].field_type, got["items"].mode) == ("RECORD", "REPEATED")
    assert [f.name for f in got["attrs"].fields] == ["key", "value"]

    errors = bq.insert_rows_json(
        table,
        [
            {
                "id": "o1",
                "tags": ["a", "b"],
                "address": {"street": "Main 1", "zip": "8000"},
                "items": [{"sku": "s1", "qty": 2}, {"sku": "s2", "qty": 1}],
                "attrs": [{"key": "channel", "value": "web"}],
            }
        ],
    )
    assert errors == []
    rows = list(
        bq.query(
            "SELECT id, ARRAY_LENGTH(tags) AS n_tags, address.zip AS zip, "
            "(SELECT SUM(i.qty) FROM UNNEST(items) AS i) AS qty, "
            "(SELECT a.value FROM UNNEST(attrs) AS a WHERE a.key = 'channel') AS channel "
            f"FROM `{dataset_id}.orders`"
        ).result()
    )
    assert [tuple(r.values()) for r in rows] == [("o1", 2, "8000", 3, "web")]


def test_emu_bigquery_partitioned_clustered_table_plan_clean(gcp_emulator_project):
    """Emitter half: a partition column without retention and clustering plan against
    the emulator: dataset, table, the partitioning's replacement trigger and the
    contract's decimal-parameters marker."""
    contract = _nested_contract(f"emu_layout_{uuid.uuid4().hex[:8]}")
    expose = contract["exposes"][0]
    expose["contract"]["schema"] += [
        {"name": "event_date", "type": "date", "required": True},
        {"name": "region", "type": "string"},
    ]
    expose["binding"]["location"]["partitionBy"] = ["event_date"]
    expose["binding"]["properties"] = {"cluster_by": ["region", "id"]}
    assert _planned_adds(gcp_emulator_project, contract) == 4


def _decimal_contract(dataset: str) -> Dict[str, Any]:
    """The nested table's binding with a NUMERIC(P,S), a BIGNUMERIC(P,S) and a decimal in
    a record."""
    contract = _nested_contract(dataset)
    contract["id"], contract["name"] = "emu.bq.decimals", "BQ decimals"
    contract["exposes"][0]["contract"]["schema"] = [
        {"name": "id", "type": "string", "required": True},
        {"name": "amount", "type": "decimal(18,4)"},
        {"name": "wide", "type": "decimal(40,20)"},
        {"name": "fee", "type": "struct", "fields": [{"name": "value", "type": "decimal(12,2)"}]},
    ]
    return contract


def test_emu_bigquery_decimal_parameters_plan_clean(gcp_emulator_project):
    """Emitter half: parameterised decimals plan against the emulator: dataset, table
    and the contract's decimal-parameters marker."""
    contract = _decimal_contract(f"emu_decimals_{uuid.uuid4().hex[:8]}")
    assert _planned_adds(gcp_emulator_project, contract) == 3


def test_emu_bigquery_decimal_parameters_round_trip_rows():
    """Emulator half: the emitted schema makes a table the emulator stores with each
    precision and scale, and queries."""
    from decimal import Decimal

    bq, table, dataset_id, got = _emulator_table(_decimal_contract("unused"), "emu_decimal_rows")
    assert (got["amount"].field_type, got["amount"].precision, got["amount"].scale) == (
        "NUMERIC",
        18,
        4,
    )
    assert (got["wide"].field_type, got["wide"].precision, got["wide"].scale) == (
        "BIGNUMERIC",
        40,
        20,
    )
    assert (got["fee"].fields[0].precision, got["fee"].fields[0].scale) == (12, 2)

    errors = bq.insert_rows_json(
        table, [{"id": "o1", "amount": "12.3456", "wide": "1.5", "fee": {"value": "9.99"}}]
    )
    assert errors == []
    rows = list(
        bq.query(f"SELECT id, amount, wide, fee.value AS fee FROM `{dataset_id}.orders`").result()
    )
    assert [tuple(r.values()) for r in rows] == [
        ("o1", Decimal("12.3456"), Decimal("1.5"), Decimal("9.99"))
    ]


def test_emu_gcs_sdk_create_read():
    """Emulator half: create a bucket via google-cloud-storage against
    the fake-gcs-server, read it back."""
    gcs = gcp_emulator_storage_client()
    # Unique per run for the same reason as the BigQuery probe above.
    bucket_name = f"fluid-emu-sdk-probe-{uuid.uuid4().hex[:8]}"
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


def test_emu_iceberg_storage_plan_clean(gcp_emulator_project):
    """An Iceberg expose provisions the GCS bucket dbt's catalogs.yml names.

    dbt materializes BigQuery Iceberg via ``catalog_type: biglake_metastore``
    and refuses to create the storage behind it. Proves the emitted bucket
    plans cleanly against the real provider, and that the bucket name is the
    one the dbt emitter writes into ``external_volume``.
    """
    from fluid_build.engines.dbt.catalogs_yml import generate_catalogs_yml
    from fluid_build.providers._iceberg_catalog import iceberg_storage_uri

    contract = {
        "id": "emu.iceberg",
        "name": "Iceberg",
        "metadata": {"layer": "Gold", "name": "Iceberg"},
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "gcp",
                    "format": "iceberg",
                    "location": {
                        "project": "fluid-emulator",
                        "dataset": "emu",
                        "table": "EVENTS",
                        "bucket": "fluid-emu-iceberg-events",
                        "path": "products/events",
                        "region": "EU",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    gcp_emulator_project.emit(contract)
    assert gcp_emulator_project.init().ok
    plan = gcp_emulator_project.plan()
    assert plan.ok, plan.stderr
    assert runner.change_summary(plan)["add"] == 1

    # The cross-emitter contract, asserted against the same contract object.
    rendered = generate_catalogs_yml(
        contract, {"engine": "dbt", "execution": {"runtime": {"platform": "gcp"}}}
    )
    uri = iceberg_storage_uri(contract["exposes"][0]["binding"], scheme="gs")
    assert uri == "gs://fluid-emu-iceberg-events/products/events"
    assert f"external_volume: {uri}" in rendered
