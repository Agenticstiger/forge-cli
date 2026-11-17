# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""LocalStack's EMR Serverless quirk, kept in the tests: forge's product code reads AWS's shape.

LocalStack Pro 2026.8.2 lists EMR Serverless applications and job runs with an ``arn`` and no
``id``; AWS always returns ``id``. The lane's clients get a botocore ``after-call`` hook that
fills ``id`` from the ARN's last segment on those two list calls, paginators included, so
``fluid_build.build_runners.spark._adapters`` reads the summaries as it reads AWS's.
"""

from __future__ import annotations

from typing import Any, Callable

#: The list calls whose summaries LocalStack sends without ``id``, and the list they carry.
LISTS = {"ListApplications": "applications", "ListJobRuns": "jobRuns"}


def fill_summary_ids(parsed: Any = None, model: Any = None, **_kwargs: Any) -> None:
    """botocore ``after-call`` handler: give each summary without ``id`` its ARN's last part."""
    key = LISTS.get(getattr(model, "name", ""))
    if not key or not isinstance(parsed, dict):
        return
    for summary in parsed.get(key) or []:
        if not summary.get("id") and summary.get("arn"):
            summary["id"] = str(summary["arn"]).rsplit("/", 1)[-1]


def with_summary_ids(clients: Callable[[str, str], Any]) -> Callable[[str, str], Any]:
    """``clients`` (a ``(service, region) -> boto3 client`` factory), whose emr-serverless
    clients fill ``id`` on LocalStack's list summaries."""

    def factory(service: str, region: str) -> Any:
        client = clients(service, region)
        if service == "emr-serverless":
            for operation in LISTS:
                client.meta.events.register(
                    f"after-call.emr-serverless.{operation}", fill_summary_ids
                )
        return client

    return factory
