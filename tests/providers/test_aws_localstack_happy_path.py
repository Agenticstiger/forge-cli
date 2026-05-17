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

"""LocalStack-backed AWS happy-path integration test (S3 + Glue).

Exercises AWS operations against a LocalStack endpoint: an S3 bucket
round-trip and a Glue Data Catalog database/table round-trip. LocalStack
is a higher-fidelity AWS emulator than moto, but requires a
LOCALSTACK_AUTH_TOKEN (Glue is a LocalStack Pro feature), so this is a
Stage-2 test in the admin-gated ``integration-emulated-heavy.yml`` lane.

Skipped automatically when no LocalStack endpoint is reachable, so it is
harmless in the keyless suite and in local runs without LocalStack. The
endpoint defaults to ``http://localhost:4566`` and is overridable via
``FLUID_LOCALSTACK_ENDPOINT``.
"""

from __future__ import annotations

import os
import socket
import urllib.parse
import uuid

import pytest

_ENDPOINT = os.environ.get("FLUID_LOCALSTACK_ENDPOINT", "http://localhost:4566")


def _have_boto3() -> bool:
    try:
        import boto3  # noqa: F401

        return True
    except ImportError:
        return False


def _localstack_reachable() -> bool:
    try:
        parsed = urllib.parse.urlparse(_ENDPOINT)
        with socket.socket() as sock:
            sock.settimeout(2)
            return sock.connect_ex((parsed.hostname or "localhost", parsed.port or 4566)) == 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.aws,
    pytest.mark.emulated_heavy,
    pytest.mark.skipif(not _have_boto3(), reason="boto3 not installed"),
    pytest.mark.skipif(
        not _localstack_reachable(),
        reason=f"no LocalStack endpoint reachable at {_ENDPOINT}",
    ),
]


@pytest.fixture
def aws_session():
    import boto3

    # LocalStack ignores the credential values; dummies keep boto3 from
    # picking up ambient real credentials.
    return boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def test_localstack_s3_bucket_round_trip(aws_session) -> None:
    """LocalStack S3: create bucket -> put -> get -> verify -> clean up."""
    s3 = aws_session.client("s3", endpoint_url=_ENDPOINT)
    bucket = f"forge-localstack-{uuid.uuid4().hex[:10]}"

    s3.create_bucket(Bucket=bucket)
    try:
        s3.put_object(Bucket=bucket, Key="probe.txt", Body=b"forge-ci")
        body = s3.get_object(Bucket=bucket, Key="probe.txt")["Body"].read()
        assert body == b"forge-ci"
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key="probe.txt")
            s3.delete_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            pass


def test_localstack_glue_table_round_trip(aws_session) -> None:
    """LocalStack Glue: create database + table -> verify -> clean up."""
    glue = aws_session.client("glue", endpoint_url=_ENDPOINT)
    database = f"forge_localstack_{uuid.uuid4().hex[:8]}"
    table = "smoke_table"

    glue.create_database(DatabaseInput={"Name": database})
    try:
        glue.create_table(
            DatabaseName=database,
            TableInput={
                "Name": table,
                "TableType": "EXTERNAL_TABLE",
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "id", "Type": "bigint"},
                        {"Name": "message", "Type": "string"},
                    ],
                    "Location": f"s3://forge-localstack/{table}/",
                },
            },
        )
        got = glue.get_table(DatabaseName=database, Name=table)["Table"]
        assert got["Name"] == table
        assert [c["Name"] for c in got["StorageDescriptor"]["Columns"]] == ["id", "message"]
    finally:
        try:
            glue.delete_table(DatabaseName=database, Name=table)
            glue.delete_database(Name=database)
        except Exception:  # noqa: BLE001
            pass
