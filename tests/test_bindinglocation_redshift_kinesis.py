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

"""Regression: v0.7.5 ``bindingLocation`` accepts Redshift Serverless + Kinesis fields.

The AWS IaC emitter reads ``binding.location.{stream, namespace, workgroup,
iam_role_arn, external_schema, glue_database}`` (``iac/providers/aws.py``) to emit
``aws_kinesis_stream`` / ``aws_redshiftserverless_namespace`` /
``aws_redshiftserverless_workgroup`` + the redshift-data ``CREATE EXTERNAL
SCHEMA`` bridge. But the schema's ``additionalProperties: false`` rejected those
keys at ``fluid validate`` time — so a Redshift/Kinesis contract passed IaC emit
yet failed validation. This pins the six fields as declared, that the three
binding shapes validate, and that unknown location keys stay rejected (the fix
is additive only).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_075 = REPO / "fluid_build" / "schemas" / "fluid-schema-0.7.5.json"

_NEW_FIELDS = [
    "stream",
    "namespace",
    "workgroup",
    "iam_role_arn",
    "external_schema",
    "glue_database",
]


def test_bindinglocation_0_7_5_declares_redshift_kinesis_fields() -> None:
    props = json.loads(SCHEMA_075.read_text())["$defs"]["bindingLocation"]["properties"]
    missing = [f for f in _NEW_FIELDS if f not in props]
    assert not missing, f"bindingLocation (0.7.5) is missing {missing}"


def _contract(location: dict) -> dict:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "test.redshift.kinesis",
        "name": "Redshift Kinesis binding test",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [
            {
                "exposeId": "e",
                "kind": "table",
                "binding": {"platform": "aws", "format": "parquet", "location": location},
                "contract": {"schema": [{"name": "id", "type": "string", "required": True}]},
            }
        ],
    }


@pytest.mark.parametrize(
    "location",
    [
        {"stream": "s", "region": "us-east-1"},
        {
            "namespace": "ns",
            "workgroup": "wg",
            "iam_role_arn": "arn:aws:iam::000000000000:role/r",
            "region": "us-east-1",
        },
        {
            "external_schema": "es",
            "glue_database": "gd",
            "workgroup": "wg",
            "iam_role_arn": "arn:aws:iam::000000000000:role/r",
        },
    ],
    ids=["kinesis-stream", "redshift-serverless", "redshift-external-schema"],
)
def test_redshift_kinesis_bindings_validate(location: dict) -> None:
    result = FluidSchemaManager().validate_contract(_contract(location), "0.7.5", offline_only=True)
    assert result.is_valid, result.errors


def test_unknown_location_field_still_rejected() -> None:
    """``additionalProperties: false`` is still enforced — the fix is additive only."""
    result = FluidSchemaManager().validate_contract(
        _contract({"table": "t", "totally_bogus_field_xyz": "x"}), "0.7.5", offline_only=True
    )
    assert not result.is_valid
