# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``governance.lakeFormation.admins`` is authoritative, and every schema says so.

The emit is one ``aws_lakeformation_data_lake_settings``. Lake Formation's
``PutDataLakeSettings`` "replaces the current list of data lake admins with the
new list being passed" (API reference), the terraform-provider-aws resource
clears every setting its configuration omits, and its destroy empties the
admins. The bundled schemas used to say the opposite: that principals not
listed are not removed. These tests pin the corrected description in every
schema that carries the field, and that the emitter refuses the field outright
(founder decision, 2026-11-16): the platform's bootstrap owns the settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fluid_build
from fluid_build.iac.base import UnsupportedBindingError
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
        "fluid-schema-0.7.7.json",
    ]


@pytest.mark.parametrize("name,description", list(_admins_descriptions()))
def test_no_schema_claims_unlisted_admins_survive(name, description):
    assert "NOT removed" not in description
    assert "preserve other admins" not in description
    assert "REMOVED on apply" in description
    # The consequence an operator must act on: list your own role.
    assert "runs the apply" in description
    assert "Destroying the resource empties the admin list" in description


def _with_admins(exposes=None):
    return {
        "id": "lf.admins",
        "governance": {"lakeFormation": {"admins": [ADMIN]}},
        "exposes": exposes or [],
    }


def test_admins_are_refused_in_any_contract():
    """Founder decision, 2026-11-16: the account's authoritative Lake Formation
    settings belong to the platform's bootstrap. A product that owned them would
    empty the account's admin list on destroy."""
    with pytest.raises(UnsupportedBindingError) as excinfo:
        AwsIacPlugin().emit(_with_admins())
    err = excinfo.value
    assert err.kind == "lakeformation-admins-not-in-a-product"
    assert "empties it" in str(err)
    assert any("put-data-lake-settings" in step for step in err.remediation)


def test_validate_refuses_admins_as_emit_does():
    from fluid_build.iac.governance_validation import validate_governance

    expose = {
        "exposeId": "orders",
        "binding": {
            "platform": "aws",
            "format": "parquet",
            "location": {"database": "sales", "table": "orders", "bucket": "b", "path": "o/"},
        },
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }
    errors, _warnings = validate_governance(_with_admins([expose]))
    assert any("governance.lakeFormation.admins" in e for e in errors), errors


def test_an_empty_admin_list_is_not_refused():
    contract = _with_admins()
    contract["governance"]["lakeFormation"]["admins"] = []
    assert "aws_lakeformation_data_lake_settings" not in AwsIacPlugin().emit(contract)
