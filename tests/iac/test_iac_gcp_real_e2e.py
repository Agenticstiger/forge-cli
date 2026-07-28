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

"""Stage 3 — real GCP round-trips for every plugin format + dbt-bigquery.

The GCP analogue of the Stage-3 AWS suite. Each test:

  1. Builds a FLUID contract.
  2. Emits ``.tf.json`` via the GCP plugin.
  3. ``tofu init/plan/apply`` against the real project, impersonating
     the bootstrap-created ``fluid-iactest-runner`` service account.
  4. Verifies via google-cloud-* SDK as the same impersonated identity.
  5. Per-test fixture destroys; session sweeper catches leaks.

Quad-gated: ``tofu`` + ADC + ``FLUID_IAC_LIVE_GCP=1`` + the bootstrap
output env vars (``FLUID_GCP_PROJECT``, ``FLUID_GCP_TEST_SA``,
``FLUID_GCP_REGION``). See ``tests/iac/_gcp_stage3_bootstrap/README.md``.

Coverage by file section:

  * `test_real_gcp_bigquery_dataset_and_table` — BQ dataset+table
  * `test_real_gcp_bigquery_view` — BQ view (SELECT-driven)
  * `test_real_gcp_gcs_bucket` — GCS bucket
  * `test_real_gcp_pubsub_topic_and_subscription` — Pub/Sub
  * `test_real_gcp_multi_exposure` — single contract → all four
  * `test_real_cli_dbt_bigquery_amend_and_build` — `fluid apply
    --mode amend-and-build` driving dbt-bigquery against real BQ
  * `test_real_gcp_mesh_dual_port_end_to_end` — SDP raw BQ → ADP dbt-bq
    aggregate → CDP BQ view consuming the aggregate.

Cost: low. BQ on-demand is per-TB scanned; tests use tiny tables and
either DDL-only or SELECT-1 queries (~$0 per run). GCS + Pub/Sub free
tier covers the per-test resources. Service-account itself doesn't
incur charges. Per-test resources tagged ``managed_by=fluid`` + named
``fluid-iactest-*`` so the sweeper picks up any leaks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest

from .conftest import (
    GCP_LIVE_ENABLED,
    GCP_LIVE_PROJECT,
    GCP_LIVE_REGION,
    GCP_LIVE_SKIP_REASON,
    GCP_LIVE_TEST_SA,
    gcp_real_client,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.gcp,
    pytest.mark.slow,
    pytest.mark.skipif(not GCP_LIVE_ENABLED, reason=GCP_LIVE_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Per-emit-format round-trips
# ---------------------------------------------------------------------------


def test_real_gcp_bigquery_dataset_and_table(gcp_real_project, gcp_account):
    """contract → real BigQuery dataset + table; verified via
    ``bigquery.Client.get_table`` returning the schema we emitted."""
    # BQ dataset names: [A-Za-z0-9_] only. Hyphens forbidden.
    dataset_id = gcp_real_project.name("bq_basic").replace("-", "_")
    table_id = "events"
    contract = {
        "id": "iac.gcp.real.bq",
        "name": "Real BQ",
        "exposes": [
            {
                "exposeId": "e",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": dataset_id, "table": table_id},
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "occurred_at", "type": "timestamp"},
                        {"name": "amount", "type": "decimal"},
                    ]
                },
            }
        ],
    }
    gcp_real_project.apply_ok(contract)

    bq = gcp_real_client("bigquery")
    ds = bq.get_dataset(f"{GCP_LIVE_PROJECT}.{dataset_id}")
    assert ds.dataset_id == dataset_id
    table = bq.get_table(f"{GCP_LIVE_PROJECT}.{dataset_id}.{table_id}")
    cols = {f.name: f.field_type for f in table.schema}
    assert cols["event_id"] == "STRING"
    assert cols["occurred_at"] == "TIMESTAMP"


def test_real_gcp_bigquery_view(gcp_real_project, gcp_account):
    """A ``bigquery_view`` binding → real view; verified via
    ``bigquery.Client.get_table`` with ``table_type == 'VIEW'``."""
    dataset_id = gcp_real_project.name("bq_view").replace("-", "_")
    view_id = "events_v"
    contract = {
        "id": "iac.gcp.real.bqview",
        "exposes": [
            {
                "exposeId": "v",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_view",
                    "location": {
                        "dataset": dataset_id,
                        "view": view_id,
                        "query": "SELECT 1 AS one, 'hello' AS greet",
                    },
                },
            }
        ],
    }
    gcp_real_project.apply_ok(contract)

    bq = gcp_real_client("bigquery")
    v = bq.get_table(f"{GCP_LIVE_PROJECT}.{dataset_id}.{view_id}")
    assert v.table_type == "VIEW"
    assert "SELECT 1" in (v.view_query or "")


def test_real_gcp_gcs_bucket(gcp_real_project, gcp_account):
    """A ``gcs_bucket`` binding → real bucket; verified via
    ``storage.Client.get_bucket``. Bucket names are globally unique +
    must match ``[a-z0-9-]`` — our UID-suffixed names satisfy both."""
    bucket = gcp_real_project.name("gcs").lower()
    contract = {
        "id": "iac.gcp.real.gcs",
        "exposes": [
            {
                "exposeId": "raw",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": bucket, "region": GCP_LIVE_REGION},
                },
            }
        ],
    }
    gcp_real_project.apply_ok(contract)

    gcs = gcp_real_client("storage")
    b = gcs.get_bucket(bucket)
    assert b.name == bucket
    assert b.location.lower() == GCP_LIVE_REGION.lower()


def test_real_gcp_pubsub_topic_and_subscription(gcp_real_project, gcp_account):
    """A ``pubsub_topic`` binding with subscription → real topic + sub.
    Verified via the publisher + subscriber clients' get_topic/get_subscription."""
    topic = gcp_real_project.name("ps").lower()
    sub_name = gcp_real_project.name("ps-sub").lower()
    contract = {
        "id": "iac.gcp.real.ps",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "gcp",
                    "format": "pubsub_topic",
                    "location": {"topic": topic, "subscription": sub_name},
                },
            }
        ],
    }
    gcp_real_project.apply_ok(contract)

    pub = gcp_real_client("pubsub_publisher")
    topic_path = pub.topic_path(GCP_LIVE_PROJECT, topic)
    assert pub.get_topic(request={"topic": topic_path}).name == topic_path

    sub_client = gcp_real_client("pubsub_subscriber")
    sub_path = sub_client.subscription_path(GCP_LIVE_PROJECT, sub_name)
    info = sub_client.get_subscription(request={"subscription": sub_path})
    assert info.topic == topic_path


# ---------------------------------------------------------------------------
# Multi-exposure — single contract → all four resources in one apply
# ---------------------------------------------------------------------------


def test_real_gcp_multi_exposure(gcp_real_project, gcp_account):
    """A single contract with BigQuery + GCS + Pub/Sub. Catches missing
    cross-resource ordering issues that single-format tests can't."""
    dataset_id = gcp_real_project.name("mix").replace("-", "_")
    bucket = gcp_real_project.name("mix-b").lower()
    topic = gcp_real_project.name("mix-t").lower()
    contract = {
        "id": "iac.gcp.real.mix",
        "exposes": [
            {
                "exposeId": "tbl",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": dataset_id, "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
            {
                "exposeId": "bkt",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": bucket, "region": GCP_LIVE_REGION},
                },
            },
            {
                "exposeId": "topic",
                "binding": {
                    "platform": "gcp",
                    "format": "pubsub_topic",
                    "location": {"topic": topic},
                },
            },
        ],
    }
    gcp_real_project.apply_ok(contract)

    bq = gcp_real_client("bigquery")
    assert bq.get_table(f"{GCP_LIVE_PROJECT}.{dataset_id}.events").table_id == "events"
    gcs = gcp_real_client("storage")
    assert gcs.get_bucket(bucket).name == bucket
    pub = gcp_real_client("pubsub_publisher")
    topic_path = pub.topic_path(GCP_LIVE_PROJECT, topic)
    assert pub.get_topic(request={"topic": topic_path}).name == topic_path


# ---------------------------------------------------------------------------
# dbt-bigquery via `fluid apply --mode amend-and-build`
# ---------------------------------------------------------------------------


def _have_dbt_bigquery() -> bool:
    try:
        import dbt.adapters.bigquery  # noqa: F401

        return True
    except ImportError:
        return False


_HAVE_DBT_BIGQUERY = _have_dbt_bigquery()


def _fluid(
    *args: str, cwd: Path, env_overrides: Optional[Mapping[str, str]] = None, timeout: int = 600
) -> subprocess.CompletedProcess:
    """Invoke the fluid CLI as subprocess. Same shape as the AWS dbt-mesh suite."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _write_yaml(path: Path, body: Dict[str, Any]) -> None:
    import yaml

    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def _write_dbt_bigquery_project(
    project_dir: Path,
    *,
    profile: str,
    model_name: str,
    model_sql: str,
) -> Path:
    """Materialise a minimal dbt-bigquery project under ``project_dir``."""
    import yaml

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": profile,
                "version": "1.0.0",
                "config-version": 2,
                "profile": profile,
                "model-paths": ["models"],
                "target-path": "target",
            },
            sort_keys=False,
        )
    )
    models = project_dir / "models"
    models.mkdir(exist_ok=True)
    (models / f"{model_name}.sql").write_text(model_sql, encoding="utf-8")
    return project_dir


@pytest.mark.skipif(not _HAVE_DBT_BIGQUERY, reason="needs dbt-bigquery installed")
def test_real_cli_dbt_bigquery_amend_and_build(gcp_real_project, gcp_account, tmp_path):
    """``fluid apply --mode amend-and-build`` → emit the BigQuery dataset
    via OpenTofu, then dispatch to dbt-bigquery which materialises one
    model into the dataset. Verified via ``bigquery.get_table``."""
    dataset_id = gcp_real_project.name("dbt_bq").replace("-", "_")
    model_name = f"hello_{gcp_real_project.uid}"

    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.gcp.real.dbt.bq",
        "name": "Real GCP dbt-bigquery",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng"}},
        "exposes": [
            {
                "exposeId": "warehouse",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": dataset_id,
                        "table": "events_seed",
                        # ``region`` lands as ``location`` on the
                        # ``google_bigquery_dataset`` resource — defaults
                        # to ``US`` if omitted. dbt-bigquery's profile
                        # below pins the same region; both must agree.
                        "region": GCP_LIVE_REGION,
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
        "build": {
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "./dbt_project",
            "properties": {"model": model_name},
            "outputs": [model_name],
            "execution": {
                "runtime": {
                    "platform": "bigquery",
                    "resources": {
                        "project": GCP_LIVE_PROJECT,
                        "dataset": dataset_id,
                        "location": GCP_LIVE_REGION,
                    },
                }
            },
        },
    }
    _write_yaml(gcp_real_project.workdir / "contract.fluid.yaml", contract)
    _write_dbt_bigquery_project(
        gcp_real_project.workdir / "dbt_project",
        profile="iac_gcp_dbt_bq",
        model_name=model_name,
        model_sql=(
            "{{ config(materialized='table') }}\n" "SELECT 42 AS answer, 'hello' AS greeting\n"
        ),
    )

    env_overrides = {
        "FLUID_IAC_LIVE_GCP": "1",
        "FLUID_GCP_PROJECT": GCP_LIVE_PROJECT,
        "FLUID_GCP_TEST_SA": GCP_LIVE_TEST_SA,
        "FLUID_GCP_REGION": GCP_LIVE_REGION,
        # The hashicorp/google provider self-configures from the
        # environment when no static ``project`` is in the provider
        # block — forge-cli's GcpIacPlugin.provider_block() returns
        # ``{}`` to stay credential-free. So we provide the project
        # via the provider's documented env var. Same for the
        # impersonation target.
        "GOOGLE_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_CLOUD_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_REGION": GCP_LIVE_REGION,
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
        # dbt-bigquery's oauth method picks impersonation up via the
        # forge-cli profile generator (which writes
        # impersonate_service_account when GCP_IMPERSONATE_SERVICE_ACCOUNT
        # is set).
        "GCP_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
    }

    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=env_overrides,
    )
    gcp_real_project.applied = True
    if rc.returncode != 0:
        pytest.fail(
            f"`fluid apply --mode amend-and-build` exited {rc.returncode}\n"
            f"--- STDOUT ---\n{rc.stdout[-4000:]}\n"
            f"--- STDERR ---\n{rc.stderr[-2000:]}"
        )

    bq = gcp_real_client("bigquery")
    row = next(
        iter(
            bq.query(
                f"SELECT answer, greeting FROM `{GCP_LIVE_PROJECT}.{dataset_id}.{model_name}`"
            ).result()
        )
    )
    assert row.answer == 42
    assert row.greeting == "hello"


# ---------------------------------------------------------------------------
# Mesh dual-port: SDP raw BQ → ADP dbt-bq aggregate → CDP BQ view
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_DBT_BIGQUERY, reason="needs dbt-bigquery installed")
def test_real_gcp_mesh_dual_port_end_to_end(gcp_real_project, gcp_account, tmp_path):
    """End-to-end data-mesh path on GCP:

    1. SDP — raw BQ table provisioned by tofu via the contract's exposes.
    2. ADP — dbt-bigquery aggregates the SDP into a new table, via
       ``fluid apply --mode amend-and-build``.
    3. CDP — a BQ view (separate contract apply) selects from the
       aggregate, simulating the consumer-aligned read port.

    Verification: SELECT through the CDP view returns the aggregate row.
    """
    # Distinct datasets for SDP+ADP vs CDP — the mesh's natural boundary.
    # Each ``fluid apply`` invocation has its own tofu state; sharing a
    # single dataset across two invocations would double-emit the
    # dataset resource and collide. Cross-dataset references via fully-
    # qualified ``project.dataset.table`` are exactly the mesh pattern.
    dataset_sdp = gcp_real_project.name("mesh_a").replace("-", "_")
    dataset_cdp = gcp_real_project.name("mesh_c").replace("-", "_")
    sdp_table = "events_seed"
    adp_model = f"agg_{gcp_real_project.uid}"
    cdp_view = f"cdp_v_{gcp_real_project.uid}"

    # Phase 1 — SDP + ADP contract: provision a seed table, then dbt
    # materialises an aggregate over it.
    sdp_adp_dir = gcp_real_project.workdir / "sdp_adp"
    sdp_adp_dir.mkdir(exist_ok=True)
    contract_a = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.gcp.mesh.sdp_adp",
        "name": "Mesh SDP+ADP",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng"}},
        "exposes": [
            {
                "exposeId": "sdp",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": dataset_sdp,
                        "table": sdp_table,
                        "region": GCP_LIVE_REGION,
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "region", "type": "string"},
                    ]
                },
            }
        ],
        "build": {
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "./dbt_project",
            "properties": {"model": adp_model},
            "outputs": [adp_model],
            "execution": {
                "runtime": {
                    "platform": "bigquery",
                    "resources": {
                        "project": GCP_LIVE_PROJECT,
                        "dataset": dataset_sdp,
                        "location": GCP_LIVE_REGION,
                    },
                }
            },
        },
    }
    _write_yaml(sdp_adp_dir / "contract.fluid.yaml", contract_a)
    _write_dbt_bigquery_project(
        sdp_adp_dir / "dbt_project",
        profile="iac_gcp_mesh_adp",
        model_name=adp_model,
        # SDP is empty (just provisioned, no rows) — VALUES literal
        # produces the aggregate without depending on SDP rows. Mirrors
        # the AWS mesh test's approach.
        model_sql=(
            "{{ config(materialized='table') }}\n"
            "SELECT COUNT(*) AS row_count "
            "FROM UNNEST([STRUCT('x' AS id), STRUCT('y' AS id)])\n"
        ),
    )
    env_overrides = {
        "FLUID_IAC_LIVE_GCP": "1",
        "FLUID_GCP_PROJECT": GCP_LIVE_PROJECT,
        "FLUID_GCP_TEST_SA": GCP_LIVE_TEST_SA,
        "FLUID_GCP_REGION": GCP_LIVE_REGION,
        "GOOGLE_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_CLOUD_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_REGION": GCP_LIVE_REGION,
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
        "GCP_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
    }
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=sdp_adp_dir,
        env_overrides=env_overrides,
    )
    gcp_real_project.applied = True
    if rc.returncode != 0:
        pytest.fail(
            f"SDP+ADP apply exited {rc.returncode}\n--- STDOUT ---\n"
            f"{rc.stdout[-3000:]}\n--- STDERR ---\n{rc.stderr[-2000:]}"
        )

    # Phase 2 — CDP contract: a view selecting from the aggregate.
    cdp_dir = gcp_real_project.workdir / "cdp"
    cdp_dir.mkdir(exist_ok=True)
    contract_c = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.gcp.mesh.cdp",
        "name": "Mesh CDP",
        "metadata": {"layer": "Gold", "owner": {"team": "data-eng"}},
        "exposes": [
            {
                "exposeId": "v",
                "kind": "view",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_view",
                    "location": {
                        "dataset": dataset_cdp,
                        "view": cdp_view,
                        "region": GCP_LIVE_REGION,
                        # Cross-dataset reference: the CDP view reads the
                        # ADP table that lives in the SDP+ADP dataset.
                        # This is exactly the mesh pattern — consumer
                        # product references an aggregate product via
                        # fully-qualified table path.
                        "query": (
                            f"SELECT row_count FROM "
                            f"`{GCP_LIVE_PROJECT}.{dataset_sdp}.{adp_model}`"
                        ),
                    },
                },
            }
        ],
    }
    _write_yaml(cdp_dir / "contract.fluid.yaml", contract_c)
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=cdp_dir,
        env_overrides=env_overrides,
    )
    if rc.returncode != 0:
        pytest.fail(
            f"CDP apply exited {rc.returncode}\n--- STDOUT ---\n"
            f"{rc.stdout[-3000:]}\n--- STDERR ---\n{rc.stderr[-2000:]}"
        )

    # Mesh assertion — SELECT through the CDP view returns the
    # ADP aggregate.
    bq = gcp_real_client("bigquery")
    row = next(
        iter(
            bq.query(
                f"SELECT row_count FROM `{GCP_LIVE_PROJECT}.{dataset_cdp}.{cdp_view}`"
            ).result()
        )
    )
    assert row.row_count == 2
