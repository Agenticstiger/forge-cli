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

"""Cleanup script for BigQuery integration test artifacts.

Runs after each ``bigquery-integration`` job in `integration.yml`
(`if: always()`).

Deletes BigQuery datasets tagged ``forge_ci=true`` AND created in the
current run (matched on the ``forge_ci_run`` label set from
``$FORGE_CI_RUN_TAG``). Falls back to a prefix-based sweep
(``forge_ci_*`` dataset names) for resources whose label propagation
lagged.

Idempotent: safe to re-run.

Required env:
  GCP_PROJECT — the BigQuery project hosting the test datasets
  FORGE_CI_RUN_TAG — value of the per-run label the tests applied

Authentication is via the workload-identity short-lived token from the
``google-github-actions/auth`` step in `integration.yml`.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    project = os.environ.get("GCP_PROJECT", "")
    run_tag = os.environ.get("FORGE_CI_RUN_TAG", "")
    if not project:
        print("::warning::GCP_PROJECT empty; nothing to clean", file=sys.stderr)
        return 0

    try:
        from google.cloud import bigquery
    except ImportError:
        print("::warning::google-cloud-bigquery not installed; nothing to clean", file=sys.stderr)
        return 0

    client = bigquery.Client(project=project)

    swept_by_label = 0
    swept_by_prefix = 0

    # Tagged sweep: list datasets in the project, filter by labels.
    for dataset in client.list_datasets(project=project):
        try:
            full = client.get_dataset(dataset.reference)
        except Exception as exc:  # noqa: BLE001
            print(
                f"::warning::could not fetch dataset {dataset.dataset_id}: {exc}", file=sys.stderr
            )
            continue

        labels = dict(full.labels or {})
        is_forge_ci = labels.get("forge_ci") == "true"
        matches_run = (not run_tag) or labels.get("forge_ci_run") == run_tag.replace(
            ".", "_"
        ).lower()

        # Belt-and-braces: also accept any dataset whose name starts with
        # ``forge_ci_``. Catches the case where label propagation lagged.
        is_prefix_match = full.dataset_id.startswith("forge_ci_")

        if (is_forge_ci and matches_run) or is_prefix_match:
            try:
                client.delete_dataset(
                    full.reference,
                    delete_contents=True,
                    not_found_ok=True,
                )
                if is_prefix_match and not is_forge_ci:
                    swept_by_prefix += 1
                    print(f"deleted dataset {full.dataset_id} (prefix sweep)")
                else:
                    swept_by_label += 1
                    print(f"deleted dataset {full.dataset_id} (label match)")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"::warning::could not delete dataset {full.dataset_id}: {exc}",
                    file=sys.stderr,
                )

    print(
        f"bigquery cleanup complete; "
        f"{swept_by_label} datasets by label + {swept_by_prefix} by prefix"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
