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

"""Stage 3 — brownfield ``tofu import`` on real AWS.

Closes the brownfield gap surfaced by the gap-analysis: the AWS
plugin's :meth:`discover_imports` used to return ``[]``, so a first
apply against pre-existing infrastructure failed with
``AlreadyExistsException``. The plugin now mirrors the Snowflake
plugin's discover-by-contract pattern, and the apply engine's
``_adopt_existing`` tolerates missing-resource imports.

Test path: pre-create a Glue catalog database + an S3 bucket via
boto3 → apply a contract that names the same resources → verify
``tofu apply`` succeeds (the resources were adopted into state and
reconciled, not re-created and failed).

Gated on ``FLUID_IAC_LIVE_AWS=1`` + ``AWS_PROFILE`` like every other
Stage 3 AWS test. Resources are tagged ``managed_by=fluid`` +
``fluid-iactest-*`` so the session sweeper picks them up if a crash
prevents the per-test teardown.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner

from .conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
    aws_real_boto,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
    pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON),
]


def _brownfield_contract(*, db: str, table: str, bucket: str, cid: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": "Brownfield import test",
        "domain": "ledger",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng", "email": "x@x.co"}},
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": db,
                        "table": table,
                        "bucket": bucket,
                        "path": "events/",
                        "region": "eu-west-1",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {"name": "amount", "type": "integer"},
                    ]
                },
            }
        ],
    }


def test_real_brownfield_glue_database_adopted(aws_real_project, aws_account):
    """A Glue database that already exists is adopted via ``tofu import``;
    ``tofu apply`` reconciles it rather than failing ``AlreadyExistsException``.
    """
    db = aws_real_project.name("brownfield-db").replace("-", "_")
    bucket = aws_real_project.name("brownfield-bk")
    table = "events"

    # Pre-create the Glue database OUT OF BAND (the brownfield scenario).
    glue = aws_real_boto("glue")
    glue.create_database(DatabaseInput={"Name": db, "Description": "pre-existing for brownfield"})

    # Pre-create the S3 bucket OUT OF BAND.
    s3 = aws_real_boto("s3")
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": aws_account["region"]},
    )

    try:
        contract = _brownfield_contract(db=db, table=table, bucket=bucket, cid="iac.aws.brownfield")
        # discover_imports returns blocks for the pre-existing resources.
        plugin = get_iac_plugin("aws")
        blocks = plugin.discover_imports(contract)
        addrs = {b.to for b in blocks}
        assert any(a.startswith("aws_glue_catalog_database.") for a in addrs)
        assert any(a.startswith("aws_s3_bucket.") for a in addrs)

        # Emit + init + import + apply via the test fixture's runner.
        aws_real_project.emit(contract)
        init = aws_real_project.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"

        # Run the import for each block — best-effort, the apply engine
        # tolerates failures. This is the same call path
        # ``_apply_opentofu_engine._adopt_existing`` uses.
        adopted = 0
        for block in blocks:
            result = runner.tofu_import(
                str(aws_real_project.workdir), block.to, block.id, env=aws_real_project.env
            )
            if result.ok:
                adopted += 1
        assert adopted >= 1, "expected at least one pre-existing resource to import"

        # Apply against the imported state — if discover_imports + import
        # worked, this is a no-op or update. If it didn't, AWS would
        # fail with ``AlreadyExistsException`` for the Glue DB.
        plan = aws_real_project.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = aws_real_project.apply()
        assert applied.ok, (
            f"tofu apply failed (AlreadyExistsException is the symptom of a "
            f"brownfield-discovery regression):\n{applied.stderr or applied.stdout}"
        )
    finally:
        # Best-effort teardown — the fixture's destroy is the primary path,
        # this handles the case where the test failed before applied=True.
        import contextlib

        with contextlib.suppress(Exception):
            glue.delete_database(Name=db)
        with contextlib.suppress(Exception):
            # Empty the bucket first.
            s3.delete_bucket(Bucket=bucket)
