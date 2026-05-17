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

"""BigQuery happy-path integration test (bigquery-emulator).

Dataset and table DDL exercised against ``goccy/bigquery-emulator`` (a local
BigQuery API emulator) instead of a real GCP project — anonymous auth, no
credentials. The emulator runs as a Docker container ("heavy"), so this is a
Stage-2 test: admin-gated via ``integration-emulated-heavy.yml``, not run on
every community PR.

``test_bigquery_live_happy_path.py`` remains the authority on real BigQuery.
"""

from __future__ import annotations

import uuid

import pytest

from tests._infrastructure.emulator_fixtures import EMULATED_BQ_PROJECT

pytestmark = [pytest.mark.integration, pytest.mark.emulated_heavy]


def test_bigquery_dataset_table_emulated_happy_path(bigquery_emulator_client) -> None:
    """bigquery-emulator: create dataset -> create table -> read it back."""
    from google.cloud import bigquery

    client = bigquery_emulator_client
    dataset_id = f"forge_emu_{uuid.uuid4().hex[:8]}"
    table_ref = f"{EMULATED_BQ_PROJECT}.{dataset_id}.smoke_table"

    client.create_dataset(bigquery.Dataset(f"{EMULATED_BQ_PROJECT}.{dataset_id}"))
    client.create_table(
        bigquery.Table(
            table_ref,
            schema=[
                bigquery.SchemaField("id", "INTEGER"),
                bigquery.SchemaField("message", "STRING"),
                bigquery.SchemaField("created_at", "TIMESTAMP"),
            ],
        )
    )

    fetched = client.get_table(table_ref)
    assert fetched.table_id == "smoke_table"
    assert [f.name for f in fetched.schema] == ["id", "message", "created_at"]
