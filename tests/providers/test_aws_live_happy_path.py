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

"""Live AWS Glue happy-path integration test.

Runs only in the gated `integration.yml::aws-integration` job. Requires
the env vars set by the workflow's OIDC role-assumption + secrets:

  AWS_REGION           — region hosting the Glue catalog
  AWS_GLUE_DATABASE    — Glue database where tests provision tables
  FORGE_CI_RUN_TAG     — per-run tag for the cleanup script
  AWS_*                — temporary credentials set by the OIDC step

Provisions a Glue table tagged with this run's identifier, asserts it
exists, then deletes it. The cleanup script in
`scripts/cleanup_aws_test_artifacts.py` catches anything we leak.

Skipped automatically when the env vars aren't present so contributors
can run `pytest` locally without AWS creds.
"""

from __future__ import annotations

import os
import uuid

import pytest

REQUIRED_ENV_VARS = [
    "AWS_REGION",
    "AWS_GLUE_DATABASE",
]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.aws,
    pytest.mark.skipif(
        not all(os.environ.get(v) for v in REQUIRED_ENV_VARS),
        reason=f"AWS integration requires env vars: {REQUIRED_ENV_VARS}",
    ),
]


def _have_boto3() -> bool:
    try:
        import boto3  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark.append(pytest.mark.skipif(not _have_boto3(), reason="boto3 not installed"))


@pytest.fixture
def run_tag() -> str:
    return os.environ.get("FORGE_CI_RUN_TAG", f"forge-ci-{uuid.uuid4().hex[:12]}")


@pytest.fixture
def glue_client():
    import boto3

    return boto3.client("glue", region_name=os.environ["AWS_REGION"])


@pytest.fixture
def temp_table_name(run_tag: str) -> str:
    suffix = run_tag.replace("-", "_").replace(".", "_").lower()
    return f"forge_ci_{suffix}_{uuid.uuid4().hex[:8]}"


class TestAwsGlueProviderHappyPath:
    """Happy path: create Glue table → verify → cleanup."""

    def test_create_glue_table_verify_drop(
        self, glue_client, temp_table_name: str, run_tag: str
    ) -> None:
        database = os.environ["AWS_GLUE_DATABASE"]

        try:
            # 1. Create the Glue table with run-scoped tags.
            glue_client.create_table(
                DatabaseName=database,
                TableInput={
                    "Name": temp_table_name,
                    "TableType": "EXTERNAL_TABLE",
                    "Parameters": {
                        "forge_ci": "true",
                        "forge_ci_run": run_tag,
                        "forge_ci_ttl": "24h",
                    },
                    "StorageDescriptor": {
                        "Columns": [
                            {"Name": "id", "Type": "bigint"},
                            {"Name": "message", "Type": "string"},
                            {"Name": "created_at", "Type": "timestamp"},
                        ],
                        "Location": f"s3://forge-ci-fixtures/empty/{temp_table_name}/",
                        "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                        "OutputFormat": (
                            "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"
                        ),
                    },
                },
            )

            # 2. Verify the table exists and has the expected shape.
            response = glue_client.get_table(DatabaseName=database, Name=temp_table_name)
            table = response["Table"]
            assert table["Name"] == temp_table_name
            column_names = [c["Name"] for c in table["StorageDescriptor"]["Columns"]]
            assert "id" in column_names
            assert "message" in column_names
            assert "created_at" in column_names

            # Verify our tags survived (the cleanup script depends on them).
            params = table.get("Parameters", {})
            assert params.get("forge_ci") == "true"
            assert params.get("forge_ci_run") == run_tag

        finally:
            # 3. Always delete the table. Cleanup script handles any leaks.
            try:
                glue_client.delete_table(DatabaseName=database, Name=temp_table_name)
            except glue_client.exceptions.EntityNotFoundException:
                pass  # already deleted, fine
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"could not delete test table {temp_table_name}: {exc}")
