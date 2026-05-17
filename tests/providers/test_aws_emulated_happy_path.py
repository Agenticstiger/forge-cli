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

"""Keyless AWS Glue happy-path integration test (moto emulator).

The keyless sibling of ``test_aws_live_happy_path.py``: it exercises the same
Glue catalog operations forge-cli's AWS provider depends on — create table,
read it back, delete it — against the in-process ``moto`` emulator instead of a
real AWS account. Runs on every PR, including from forks, with zero credentials.

``test_aws_live_happy_path.py`` remains the authority on real AWS behaviour;
this test catches Glue API-shape regressions early and for free.
"""

from __future__ import annotations

import uuid

import pytest

from tests._infrastructure.emulator_fixtures import EMULATED_GLUE_DATABASE, requires_moto

pytestmark = [pytest.mark.integration, pytest.mark.emulated, requires_moto()]


class TestAwsGlueEmulatedHappyPath:
    """Happy path against moto: create Glue table -> verify -> drop."""

    def test_create_glue_table_verify_drop(self, moto_glue_client) -> None:
        table = f"forge_emulated_{uuid.uuid4().hex[:8]}"

        moto_glue_client.create_table(
            DatabaseName=EMULATED_GLUE_DATABASE,
            TableInput={
                "Name": table,
                "TableType": "EXTERNAL_TABLE",
                "Parameters": {"forge_ci": "true"},
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "id", "Type": "bigint"},
                        {"Name": "message", "Type": "string"},
                        {"Name": "created_at", "Type": "timestamp"},
                    ],
                    "Location": f"s3://forge-emulated/{table}/",
                    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                    "OutputFormat": ("org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"),
                },
            },
        )

        fetched = moto_glue_client.get_table(DatabaseName=EMULATED_GLUE_DATABASE, Name=table)
        glue_table = fetched["Table"]
        assert glue_table["Name"] == table
        columns = [c["Name"] for c in glue_table["StorageDescriptor"]["Columns"]]
        assert columns == ["id", "message", "created_at"]
        assert glue_table["Parameters"]["forge_ci"] == "true"

        moto_glue_client.delete_table(DatabaseName=EMULATED_GLUE_DATABASE, Name=table)
        with pytest.raises(moto_glue_client.exceptions.EntityNotFoundException):
            moto_glue_client.get_table(DatabaseName=EMULATED_GLUE_DATABASE, Name=table)
