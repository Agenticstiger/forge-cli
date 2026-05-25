# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 3 — GCP idempotency: ``fluid apply`` twice = 0 changes.

See ``test_iac_aws_real_idempotency_e2e.py`` for the rationale. This
file is the GCP analogue: BigQuery + GCS + Pub/Sub in one multi-
exposure contract, applied twice; the second plan must be a no-op.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import runner

from .conftest import (
    GCP_LIVE_ENABLED,
    GCP_LIVE_REGION,
    GCP_LIVE_SKIP_REASON,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.gcp,
    pytest.mark.slow,
    pytest.mark.skipif(not GCP_LIVE_ENABLED, reason=GCP_LIVE_SKIP_REASON),
]


def test_real_gcp_idempotency_apply_twice_no_changes(gcp_real_project, gcp_account):
    """Apply a multi-format GCP contract, re-plan, assert 0 changes."""
    dataset_id = gcp_real_project.name("idem").replace("-", "_")
    bucket = gcp_real_project.name("idem-b").lower()
    topic = gcp_real_project.name("idem-t").lower()
    contract = {
        "id": "iac.gcp.real.idem",
        "exposes": [
            {
                "exposeId": "tbl",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": dataset_id,
                        "table": "events",
                        "region": GCP_LIVE_REGION,
                    },
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

    second_plan = gcp_real_project.plan()
    assert second_plan.ok, second_plan.stderr or second_plan.stdout
    summary = runner.change_summary(second_plan)
    assert summary["add"] == 0 and summary["change"] == 0 and summary["remove"] == 0, (
        f"non-idempotent emit — second plan: {summary}\n"
        f"stdout (last 2000):\n{second_plan.stdout[-2000:]}"
    )

    second_apply = gcp_real_project.apply()
    assert second_apply.ok, second_apply.stderr or second_apply.stdout
    apply_summary = runner.change_summary(second_apply)
    assert (
        apply_summary["add"] == 0 and apply_summary["change"] == 0 and apply_summary["remove"] == 0
    ), f"second apply caused churn: {apply_summary}"
