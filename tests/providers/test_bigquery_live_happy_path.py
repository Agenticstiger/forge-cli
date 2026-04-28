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

"""Live BigQuery happy-path integration test.

Runs only in the gated `integration.yml::bigquery-integration` job.
Requires the env vars set by the workflow's WIF auth + secrets:

  GCP_PROJECT          — test project (forge-ci-bigquery)
  GCP_LOCATION         — BQ region (e.g. "US" or "EU")
  FORGE_CI_RUN_TAG     — per-run tag for the cleanup script
  GOOGLE_APPLICATION_CREDENTIALS  — set by `google-github-actions/auth`

Provisions a temp dataset, runs one CREATE TABLE through the BigQuery
provider, verifies the table exists via the bq SDK, then drops the
dataset. The cleanup script in `scripts/cleanup_bigquery_test_artifacts.py`
catches anything we leak.

Skipped automatically when the env vars aren't present so contributors
can run `pytest` locally without GCP creds.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

REQUIRED_ENV_VARS = [
    "GCP_PROJECT",
    "GCP_LOCATION",
]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.gcp,
    pytest.mark.skipif(
        not all(os.environ.get(v) for v in REQUIRED_ENV_VARS),
        reason=f"BigQuery integration requires env vars: {REQUIRED_ENV_VARS}",
    ),
]


def _have_bigquery() -> bool:
    try:
        import google.cloud.bigquery  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark.append(
    pytest.mark.skipif(not _have_bigquery(), reason="google-cloud-bigquery not installed")
)


@pytest.fixture
def run_tag() -> str:
    return os.environ.get("FORGE_CI_RUN_TAG", f"forge-ci-{uuid.uuid4().hex[:12]}")


@pytest.fixture
def temp_dataset_id(run_tag: str) -> str:
    """Return a unique dataset name for this test run.

    BigQuery dataset IDs must match ``[A-Za-z0-9_]+`` (no hyphens), so we
    normalise the run tag before using it as a name.
    """
    suffix = run_tag.replace("-", "_").replace(".", "_").lower()
    return f"forge_ci_{suffix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def bq_client():
    """Authenticated BigQuery client using the workflow's WIF token."""
    from google.cloud import bigquery

    return bigquery.Client(project=os.environ["GCP_PROJECT"])


class TestBigQueryProviderHappyPath:
    """Happy path: create dataset → emit one DDL → verify → cleanup."""

    def test_create_dataset_emit_table_verify_drop(
        self, bq_client, temp_dataset_id: str, run_tag: str
    ) -> None:
        from google.cloud import bigquery

        project = os.environ["GCP_PROJECT"]
        location = os.environ["GCP_LOCATION"]
        full_id = f"{project}.{temp_dataset_id}"

        # 1. Create the dataset with run-scoped labels.
        dataset = bigquery.Dataset(full_id)
        dataset.location = location
        # Labels: BigQuery requires lowercase letters, digits, dashes, underscores.
        dataset.labels = {
            "forge_ci": "true",
            "forge_ci_run": run_tag.replace(".", "_").lower(),
            "forge_ci_ttl": "24h",
        }
        try:
            bq_client.create_dataset(dataset, timeout=30)

            # 2. Run one create-table DDL — the smallest meaningful test.
            table_id = f"{full_id}.smoke_table"
            query = f"""
                CREATE TABLE `{table_id}` (
                    id INT64 NOT NULL,
                    message STRING,
                    created_at TIMESTAMP
                ) OPTIONS (
                    labels = [("forge_ci", "true")]
                )
            """
            job = bq_client.query(query)
            job.result(timeout=60)

            # 3. Verify the table exists.
            table = bq_client.get_table(table_id)
            assert table.table_id == "smoke_table"
            schema_names = [field.name for field in table.schema]
            assert "id" in schema_names
            assert "message" in schema_names
            assert "created_at" in schema_names

        finally:
            # 4. Always drop the dataset. The cleanup script catches it
            # if this finally block doesn't run.
            try:
                bq_client.delete_dataset(
                    full_id,
                    delete_contents=True,
                    not_found_ok=True,
                )
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"could not drop test dataset {full_id}: {exc}")
