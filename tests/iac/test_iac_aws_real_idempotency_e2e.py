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

"""Stage 3 — idempotency: ``fluid apply`` twice = 0 changes.

A declarative system MUST be idempotent. Applying the same contract
twice should result in a no-op `tofu plan` (0 to add, 0 to change, 0
to destroy) on the second invocation. Any spurious drift would surface
as resource churn — expensive, noisy, and indicates the emit is
non-deterministic somewhere (UUIDs in resource names, timestamps in
tags, unsorted lists, etc.).

This test exists to catch that class of bug. One test per emit kind
would be overkill; one test exercising a multi-format contract is
enough — if the apply-twice path is clean for S3 + Iceberg + Glue
together, it's clean for each individually.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import runner

from .conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
    aws_iceberg_contract,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
    pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON),
]


def test_real_aws_idempotency_apply_twice_no_changes(aws_real_project, aws_account):
    """Apply an Iceberg-on-Glue contract, then immediately re-plan
    against the same workdir. Plan must propose 0 changes — proves the
    emit is deterministic and the provider's drift detection sees no
    diff between desired (.tf.json) and observed (real AWS) state.
    """
    bucket = aws_real_project.name("idem")
    glue_db = aws_real_project.name("idem_db").replace("-", "_")
    contract = aws_iceberg_contract(
        bucket, database=glue_db, table="events", cid="iac.aws.real.idem"
    )

    # First apply — creates S3 bucket + Glue database + Glue table.
    aws_real_project.apply_ok(contract)

    # Second plan against the same workdir + same state. Drift would
    # show up here as proposed adds / changes / destroys.
    second_plan = aws_real_project.plan()
    assert second_plan.ok, second_plan.stderr or second_plan.stdout
    summary = runner.change_summary(second_plan)
    assert summary["add"] == 0 and summary["change"] == 0 and summary["remove"] == 0, (
        f"non-idempotent emit — second plan proposes: {summary}\n"
        f"plan stdout (last 2000):\n{second_plan.stdout[-2000:]}"
    )

    # Belt-and-suspenders: also do a second `apply` and confirm zero
    # resources moved. `tofu apply` against an in-sync state is a
    # native no-op; this asserts that contract.
    second_apply = aws_real_project.apply()
    assert second_apply.ok, second_apply.stderr or second_apply.stdout
    apply_summary = runner.change_summary(second_apply)
    assert (
        apply_summary["add"] == 0 and apply_summary["change"] == 0 and apply_summary["remove"] == 0
    ), f"second apply caused churn: {apply_summary}"
