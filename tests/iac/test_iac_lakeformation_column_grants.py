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

"""The three column shapes of a Lake Formation grant, as emitted.

``governance.lakeFormation.grants[]`` picks the ``aws_lakeformation_permissions``
block from its columns:

* neither ``columns`` nor ``excludedColumns`` — a ``table`` block, every column;
* ``columns`` (an allow-list) — ``table_with_columns.column_names``, no wildcard;
* ``excludedColumns`` — ``table_with_columns`` with ``wildcard = true`` and
  ``excluded_column_names``. The hashicorp/aws docs: "If
  ``excluded_column_names`` is included, ``wildcard`` must be set to ``true``".

The excluded shape used to go out without the wildcard, and ``tofu plan``
refused it ("Missing required argument"). ``tofu validate`` passed it, because
the block's table name is a reference that is unknown until plan, so only a
real plan proves the module: ``test_iac_lakeformation_column_grants_plan.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit, pytest.mark.provider]

STEWARD = "arn:aws:iam::111111111111:role/steward"
ANALYST = "arn:aws:iam::111111111111:role/analyst"
AUDITOR = "arn:aws:iam::111111111111:role/auditor"
TABLE_KEY = "lf_columns_crm_customers"
TABLE_REFS = {
    "database_name": "${aws_glue_catalog_table." + TABLE_KEY + ".database_name}",
    "name": "${aws_glue_catalog_table." + TABLE_KEY + ".name}",
}


def _grant(
    principal: str, columns: Optional[List[str]] = None, excluded: Optional[List[str]] = None
) -> Dict[str, Any]:
    grant: Dict[str, Any] = {"principal": principal, "permissions": ["SELECT", "DESCRIBE"]}
    if columns is not None:
        grant["columns"] = columns
    if excluded is not None:
        grant["excludedColumns"] = excluded
    return grant


def _contract(grants: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "lf.columns",
        "name": "LF column grants",
        "metadata": {"layer": "Bronze", "owner": {"team": "data", "email": "d@example.com"}},
        "exposes": [
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": "acme-lake",
                        "path": "crm/customers/",
                        "database": "crm",
                        "table": "customers",
                    },
                    "governance": {"lakeFormation": {"registerLocation": True, "grants": grants}},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "msisdn", "type": "string"},
                        {"name": "status", "type": "string"},
                    ]
                },
            }
        ],
    }


def _grants(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The emitted ``aws_lakeformation_permissions`` bodies, by principal."""
    emitted = AwsIacPlugin().emit(contract)["aws_lakeformation_permissions"]
    return {body["principal"]: body for body in emitted.values()}


THREE_SHAPES = _contract(
    [
        _grant(STEWARD),
        _grant(ANALYST, excluded=["msisdn"]),
        _grant(AUDITOR, columns=["id", "status"]),
    ]
)


class TestTheThreeShapes:
    def test_a_grant_with_no_columns_is_a_table_block(self):
        body = _grants(THREE_SHAPES)[STEWARD]
        assert body["table"] == [TABLE_REFS]
        assert "table_with_columns" not in body

    def test_excluded_columns_are_a_column_wildcard(self):
        body = _grants(THREE_SHAPES)[ANALYST]
        assert "table" not in body
        assert body["table_with_columns"] == [
            {**TABLE_REFS, "wildcard": True, "excluded_column_names": ["msisdn"]}
        ]

    def test_an_allow_list_never_carries_a_wildcard(self):
        body = _grants(THREE_SHAPES)[AUDITOR]
        assert "table" not in body
        assert body["table_with_columns"] == [{**TABLE_REFS, "column_names": ["id", "status"]}]

    def test_every_table_with_columns_names_exactly_one_of_the_two(self):
        # The provider's AtLeastOneOf(column_names, wildcard), and Lake
        # Formation's either-or: a column list or a wildcard, never both.
        for body in _grants(THREE_SHAPES).values():
            for block in body.get("table_with_columns", []):
                assert ("column_names" in block) != (block.get("wildcard") is True), block
                if "excluded_column_names" in block:
                    assert block["wildcard"] is True, block

    def test_the_rendered_module_carries_the_wildcard(self):
        module = json.loads(build_module(get_iac_plugin("aws"), THREE_SHAPES))
        grants = module["resource"]["aws_lakeformation_permissions"]
        twc = [b["table_with_columns"][0] for b in grants.values() if "table_with_columns" in b]
        assert {"wildcard": True, "excluded_column_names": ["msisdn"]}.items() <= twc[0].items()

    def test_the_contract_is_schema_valid(self):
        result = FluidSchemaManager().validate_contract(THREE_SHAPES)
        assert result.is_valid, "\n".join(result.errors)


class TestEdges:
    def test_an_empty_exclusion_list_is_a_table_block(self):
        body = _grants(_contract([_grant(ANALYST, excluded=[])]))[ANALYST]
        assert body["table"] == [TABLE_REFS]
        assert "table_with_columns" not in body

    def test_columns_and_excluded_columns_together_are_refused(self):
        # The schema calls them mutually exclusive, and Lake Formation takes a
        # column list or a wildcard, not both: emitting both would pass the
        # plan and fail the apply.
        contract = _contract([_grant(ANALYST, columns=["id"], excluded=["msisdn"])])
        with pytest.raises(UnsupportedBindingError) as refused:
            AwsIacPlugin().emit(contract)
        assert refused.value.kind == "lakeformation-grant-columns"
        assert "grants[0]" in str(refused.value)
