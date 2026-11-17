# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""LocalStack's EMR Serverless quirk, kept in the tests: forge's product code reads AWS's shape.

LocalStack Pro 2026.8.2 lists EMR Serverless applications and job runs with an ``arn`` and no
``id``; AWS always returns ``id`` there, and ``jobRunId`` on ``GetJobRun``. The lane's clients
get a botocore ``after-call`` hook that fills ``id`` from the ARN's last segment on those two
list calls (paginators included) and ``jobRunId`` on ``GetJobRun`` when it is missing, so
``fluid_build.build_runners.spark._adapters`` reads LocalStack's records as it reads AWS's.
"""

from __future__ import annotations

from typing import Any, Callable

#: The list calls whose summaries LocalStack sends without ``id``, and the list they carry.
LISTS = {"ListApplications": "applications", "ListJobRuns": "jobRuns"}
#: The calls whose record may come without ``jobRunId`` (botocore keeps only the model's
#: members, so a non-model ``id`` never reaches the adapter: only the ARN can stand in).
JOB_RUNS = {"GetJobRun": "jobRun"}


def fill_summary_ids(parsed: Any = None, model: Any = None, **_kwargs: Any) -> None:
    """botocore ``after-call`` handler: give each list summary without ``id``, and a job run
    without ``jobRunId``, its ARN's last part."""
    name = getattr(model, "name", "")
    if not isinstance(parsed, dict):
        return
    for summary in parsed.get(LISTS.get(name, ""), None) or []:
        if not summary.get("id") and summary.get("arn"):
            summary["id"] = str(summary["arn"]).rsplit("/", 1)[-1]
    job = parsed.get(JOB_RUNS.get(name, ""), None)
    if isinstance(job, dict) and not job.get("jobRunId") and job.get("arn"):
        job["jobRunId"] = str(job["arn"]).rsplit("/", 1)[-1]


def with_summary_ids(clients: Callable[[str, str], Any]) -> Callable[[str, str], Any]:
    """``clients`` (a ``(service, region) -> boto3 client`` factory), whose emr-serverless
    clients fill ``id`` on LocalStack's list summaries."""

    def factory(service: str, region: str) -> Any:
        client = clients(service, region)
        if service == "emr-serverless":
            for operation in (*LISTS, *JOB_RUNS):
                client.meta.events.register(
                    f"after-call.emr-serverless.{operation}", fill_summary_ids
                )
        return client

    return factory
