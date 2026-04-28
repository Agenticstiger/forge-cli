#!/usr/bin/env python3
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

"""Cleanup script for AWS Glue integration test artifacts.

Runs after each ``aws-integration`` job in `integration.yml`
(`if: always()`).

Deletes AWS Glue tables tagged ``forge_ci=true`` AND created in the
current run (matched on the ``forge_ci_run`` tag from
``$FORGE_CI_RUN_TAG``). Falls back to a prefix-based sweep
(``forge_ci_*`` table names) for tables whose tag propagation lagged.

Idempotent: safe to re-run.

Required env:
  AWS_REGION — the AWS region hosting the Glue catalog
  AWS_GLUE_DATABASE — the Glue database where tests provision tables
  FORGE_CI_RUN_TAG — value of the per-run tag the tests applied

Authentication is via the OIDC short-lived credentials from
``aws-actions/configure-aws-credentials`` in `integration.yml`.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    region = os.environ.get("AWS_REGION", "")
    database = os.environ.get("AWS_GLUE_DATABASE", "")
    run_tag = os.environ.get("FORGE_CI_RUN_TAG", "")
    if not region or not database:
        print(
            f"::warning::AWS_REGION ({region!r}) or AWS_GLUE_DATABASE ({database!r}) empty; "
            "nothing to clean",
            file=sys.stderr,
        )
        return 0

    try:
        import boto3
    except ImportError:
        print("::warning::boto3 not installed; nothing to clean", file=sys.stderr)
        return 0

    glue = boto3.client("glue", region_name=region)

    swept_by_tag = 0
    swept_by_prefix = 0

    # List tables in the test database; for each, fetch tags and
    # decide whether to delete.
    paginator = glue.get_paginator("get_tables")
    for page in paginator.paginate(DatabaseName=database):
        for table in page.get("TableList", []):
            table_name = table["Name"]
            arn = (
                f"arn:aws:glue:{region}:{boto3.client('sts').get_caller_identity()['Account']}"
                f":table/{database}/{table_name}"
            )

            # Fetch tags for this table.
            try:
                tags_resp = glue.get_tags(ResourceArn=arn)
                tags = tags_resp.get("Tags", {}) or {}
            except Exception as exc:  # noqa: BLE001
                print(f"::warning::could not get tags for {table_name}: {exc}", file=sys.stderr)
                tags = {}

            is_forge_ci = tags.get("forge_ci") == "true"
            matches_run = (not run_tag) or tags.get("forge_ci_run") == run_tag
            is_prefix_match = table_name.startswith("forge_ci_")

            if (is_forge_ci and matches_run) or is_prefix_match:
                try:
                    glue.delete_table(DatabaseName=database, Name=table_name)
                    if is_prefix_match and not is_forge_ci:
                        swept_by_prefix += 1
                        print(f"deleted glue table {table_name} (prefix sweep)")
                    else:
                        swept_by_tag += 1
                        print(f"deleted glue table {table_name} (tag match)")
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"::warning::could not delete table {table_name}: {exc}",
                        file=sys.stderr,
                    )

    print(f"aws cleanup complete; {swept_by_tag} tables by tag + {swept_by_prefix} by prefix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
