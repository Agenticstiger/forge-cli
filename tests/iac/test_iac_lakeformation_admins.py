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

"""``governance.lakeFormation.admins`` is authoritative, and every schema says so.

The emit is one ``aws_lakeformation_data_lake_settings``. Lake Formation's
``PutDataLakeSettings`` "replaces the current list of data lake admins with the
new list being passed" (API reference), the terraform-provider-aws resource
clears every setting its configuration omits, and its destroy empties the
admins. The bundled schemas used to say the opposite: that principals not
listed are not removed. These tests pin the corrected description in every
schema that carries the field, and that the emit itself did not change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fluid_build
from fluid_build.iac.providers.aws import AwsIacPlugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SCHEMAS = Path(fluid_build.__file__).resolve().parent / "schemas"
ADMIN = "arn:aws:iam::111111111111:role/lake-admin"


def _admins_descriptions():
    for path in sorted(SCHEMAS.glob("fluid-schema-*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        governance = (schema.get("$defs") or {}).get("governance") or {}
        lake_formation = (governance.get("properties") or {}).get("lakeFormation")
        if lake_formation:
            yield path.name, lake_formation["properties"]["admins"]["description"]


def test_every_schema_that_carries_admins_is_covered():
    assert [name for name, _ in _admins_descriptions()] == [
        "fluid-schema-0.7.3.json",
        "fluid-schema-0.7.4.json",
        "fluid-schema-0.7.5.json",
        "fluid-schema-0.7.6.json",
    ]


@pytest.mark.parametrize("name,description", list(_admins_descriptions()))
def test_no_schema_claims_unlisted_admins_survive(name, description):
    assert "NOT removed" not in description
    assert "preserve other admins" not in description
    assert "REMOVED on apply" in description
    # The consequence an operator must act on: list your own role.
    assert "runs the apply" in description
    assert "Destroying the resource empties the admin list" in description


def test_the_admins_emit_is_unchanged():
    contract = {
        "id": "lf.admins",
        "governance": {"lakeFormation": {"admins": [ADMIN]}},
        "exposes": [],
    }
    resources = AwsIacPlugin().emit(contract)
    assert resources["aws_lakeformation_data_lake_settings"] == {
        "lf_admins_lf_settings": {"admins": [ADMIN]}
    }
