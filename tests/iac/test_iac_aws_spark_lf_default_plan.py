# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A forge Spark job's own Lake Formation grant, decided by a real ``tofu plan``.

The output declares no ``governance.lakeFormation``. Whether its job role needs a grant to see
its table depends on the account's Lake Formation settings, which only a plan reads
(``data.aws_lakeformation_data_lake_settings``): an account that keeps the
``IAM_ALLOWED_PRINCIPALS`` default for new tables plans no grant, and one that removed it
plans the grant. Same harness as ``test_iac_lakeformation_bucket_policy_plan.py``: moto
answers the reads, and no AWS account or credentials are involved.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import copy

import pytest

from fluid_build.iac.naming import safe_ident
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.iac.test_iac_lakeformation_bucket_policy_plan import (  # noqa: F401 - fixtures
    _SKIP,
    _SKIP_REASON,
    _planned_changes,
    moto_endpoint,
    tofu_env,
)

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

CID = "shop.orders"
GRANT = f"aws_lakeformation_permissions.{safe_ident(CID)}_lf_spark_writer_lines_0"
DEAD_LETTERS = f"aws_lakeformation_permissions.{safe_ident(CID)}_lf_spark_dead_letters_ingest"
SCHEMA = [{"name": "order_id", "type": "string", "required": True}]
CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": CID,
    "name": "Orders",
    "metadata": {"owner": {"team": "shop"}},
    "packaging": {"containers": {"emrApplication": "isolated"}},
    "builds": [
        {
            "id": "ingest",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["lines"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "shop.orders",
                    "format": "json",
                    "schema": SCHEMA,
                    "trigger": {"processingTime": "PT1M"},
                    "deadLetter": {},  # the records it cannot land, to a table beside it
                },
                "mapping": {"columns": [{"name": "order_id", "from": "order_id"}]},
                "parameters": {
                    "spark": {
                        "runtime": "emr-serverless",
                        "source": {"bootstrapServers": "b-1.example.internal:9092", "auth": "none"},
                    }
                },
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "lines",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "shop",
                    "table": "lines",
                    "bucket": "shop-lake",
                    "path": "iceberg/shop/lines/",
                },
            },
            "contract": {"schema": SCHEMA},
        }
    ],
}
IAM_ALLOWED = [{"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
                "Permissions": ["ALL"]}]  # fmt: skip


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
@pytest.mark.parametrize(
    ("table_defaults", "planned"),
    [(IAM_ALLOWED, []), ([], [f"{DEAD_LETTERS}[0]", f"{GRANT}[0]"])],
    ids=["iam-allowed-principals", "lake-formation-governs"],
)
def test_the_plan_grants_the_job_role_only_where_lake_formation_governs(
    moto_endpoint, tofu_env, tmp_path, monkeypatch, table_defaults, planned
):
    import boto3

    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    boto3.client(
        "lakeformation",
        endpoint_url=moto_endpoint,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret - moto dummy
    ).put_data_lake_settings(
        DataLakeSettings={
            "DataLakeAdmins": [],
            "CreateDatabaseDefaultPermissions": table_defaults,
            "CreateTableDefaultPermissions": table_defaults,
        }
    )
    changes = _planned_changes(copy.deepcopy(CONTRACT), tmp_path, moto_endpoint, tofu_env)
    grants = sorted(c["address"] for c in changes if c["type"] == "aws_lakeformation_permissions")
    assert grants == planned
