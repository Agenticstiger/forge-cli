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

"""The permissions a column-limited Lake Formation grant is emitted with.

A grant with ``excludedColumns`` and ``permissions: [SELECT, DESCRIBE]`` went out
as one ``table_with_columns`` resource carrying both, and the apply failed:
``GrantPermissions ... InvalidInputException: Permissions modification is
invalid``. The AWS LF developer guide (*Lake Formation permissions reference*):

* "Table with column filter -- Permission: SELECT"; the API's
  ``TableWithColumnsResource`` "is only used when granting a SELECT permission";
* "You can't grant DESCRIBE to a user who has partial select on a table", so
  DESCRIBE cannot move to a separate ``table`` grant for the same principal,
  and it need not: with SELECT the principal "can view a table in the Data
  Catalog", restricted to the granted columns;
* "A principal with the SELECT permission on a subset of columns in a table
  cannot be granted the ALTER, DROP, DELETE, or INSERT permission on that
  table", so those are refused at emit rather than split out.

A separate ``table`` grant would also never settle in OpenTofu: hashicorp/aws
reads a ``table_with_columns`` grant back through a ``Table`` listing and keeps
the principal's table-level permissions as its own (``filterTableWithColumns
Permissions``), and ``permissions`` forces replacement.

``tofu plan`` and moto accept every one of these shapes (moto's
``grant_permissions`` stores any permission on any resource), which is why the
emulator tests passed. So the emitted JSON is the proof here, and
``test_iac_lakeformation_column_grants_plan.py`` checks the planned values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit, pytest.mark.provider]

STEWARD = "arn:aws:iam::111111111111:role/steward"
ANALYST = "arn:aws:iam::111111111111:role/analyst"
AUDITOR = "arn:aws:iam::111111111111:role/auditor"
TABLE_LEVEL = ("ALTER", "DROP", "DELETE", "INSERT", "ALL")


def _grant(
    principal: str,
    permissions: List[str],
    *,
    columns: Optional[List[str]] = None,
    excluded: Optional[List[str]] = None,
    grant_option: Optional[List[str]] = None,
) -> Dict[str, Any]:
    grant: Dict[str, Any] = {"principal": principal, "permissions": permissions}
    if columns is not None:
        grant["columns"] = columns
    if excluded is not None:
        grant["excludedColumns"] = excluded
    if grant_option is not None:
        grant["permissionsWithGrantOption"] = grant_option
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


def _emitted(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The emitted ``aws_lakeformation_permissions`` bodies, by resource key."""
    emitted = AwsIacPlugin().emit(contract).get("aws_lakeformation_permissions") or {}
    return dict(emitted)


def _table_of(body: Dict[str, Any]) -> Optional[str]:
    """The Glue table reference a grant body targets, whichever block it uses."""
    for block in ("table", "table_with_columns"):
        if body.get(block):
            return str(body[block][0]["name"])
    return None


#: The demo's shape: the steward reads every column, the analyst every column
#: but msisdn, the auditor an allow-list; all ask for SELECT and DESCRIBE.
DEMO = _contract(
    [
        _grant(STEWARD, ["SELECT", "DESCRIBE"]),
        _grant(ANALYST, ["SELECT", "DESCRIBE"], excluded=["msisdn"]),
        _grant(AUDITOR, ["SELECT", "DESCRIBE"], columns=["id", "status"]),
    ]
)


class TestAColumnLimitedGrantCarriesSelectOnly:
    def test_no_table_with_columns_carries_a_non_select_permission(self):
        column_grants = [b for b in _emitted(DEMO).values() if "table_with_columns" in b]
        assert len(column_grants) == 2
        for body in column_grants:
            assert body["permissions"] == ["SELECT"], body
            assert set(body.get("permissions_with_grant_option") or ()) <= {"SELECT"}, body

    def test_describe_is_not_split_onto_a_table_grant_for_the_same_principal(self):
        # Lake Formation refuses DESCRIBE on the table to a principal holding a
        # partial SELECT, and the provider would read it into the column grant.
        bodies = list(_emitted(DEMO).values())
        for column_grant in (b for b in bodies if "table_with_columns" in b):
            same_principal_same_table = [
                b
                for b in bodies
                if b is not column_grant
                and b["principal"] == column_grant["principal"]
                and _table_of(b) == _table_of(column_grant)
            ]
            assert same_principal_same_table == [], same_principal_same_table

    def test_one_resource_per_grant_at_the_address_it_had(self):
        # The demo's state already holds these addresses; nothing is renumbered
        # and nothing is added, so no moved block is needed.
        assert sorted(_emitted(DEMO)) == [
            "lf_columns_lf_grant_customers_0",
            "lf_columns_lf_grant_customers_1",
            "lf_columns_lf_grant_customers_2",
        ]

    def test_a_grant_without_a_column_limit_keeps_every_permission(self):
        (steward,) = [b for b in _emitted(DEMO).values() if b["principal"] == STEWARD]
        assert "table_with_columns" not in steward
        assert steward["permissions"] == ["SELECT", "DESCRIBE"]

    @pytest.mark.parametrize("permissions", [["SELECT"], ["DESCRIBE", "SELECT"], ["select"]])
    def test_any_order_or_case_of_select_and_describe_is_select(self, permissions):
        grants = _emitted(_contract([_grant(ANALYST, permissions, excluded=["msisdn"])]))
        (body,) = grants.values()
        assert body["permissions"] == ["SELECT"]

    def test_selects_grant_option_is_kept_and_describes_is_dropped(self):
        contract = _contract(
            [
                _grant(
                    ANALYST,
                    ["SELECT", "DESCRIBE"],
                    excluded=["msisdn"],
                    grant_option=["SELECT", "DESCRIBE"],
                )
            ]
        )
        (body,) = _emitted(contract).values()
        assert body["permissions"] == ["SELECT"]
        assert body["permissions_with_grant_option"] == ["SELECT"]

    def test_a_describe_only_grant_option_leaves_no_grant_option(self):
        contract = _contract(
            [
                _grant(
                    ANALYST, ["SELECT", "DESCRIBE"], excluded=["msisdn"], grant_option=["DESCRIBE"]
                )
            ]
        )
        (body,) = _emitted(contract).values()
        assert "permissions_with_grant_option" not in body

    def test_the_demo_contract_is_schema_valid(self):
        result = FluidSchemaManager().validate_contract(DEMO)
        assert result.is_valid, "\n".join(result.errors)


def _refusal(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as refused:
        AwsIacPlugin().emit(contract)
    return refused.value


class TestWhatLakeFormationWouldRefuseIsRefusedAtEmit:
    @pytest.mark.parametrize("permission", TABLE_LEVEL)
    @pytest.mark.parametrize("limit", ["excluded", "columns"])
    def test_a_table_level_permission_beside_a_column_limit(self, permission, limit):
        cols = {"excluded": ["msisdn"]} if limit == "excluded" else {"columns": ["id"]}
        contract = _contract(
            [_grant(STEWARD, ["SELECT"]), _grant(ANALYST, ["SELECT", permission], **cols)]
        )
        # The schema allows it; Lake Formation does not.
        assert FluidSchemaManager().validate_contract(contract).is_valid
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert "grants[1]" in str(error) and f"'{permission}'" in str(error)

    def test_a_table_level_grant_option_beside_a_column_limit(self):
        contract = _contract(
            [_grant(ANALYST, ["SELECT"], excluded=["msisdn"], grant_option=["ALTER"])]
        )
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert "'ALTER'" in str(error)

    def test_describe_alone_cannot_carry_a_column_limit(self):
        error = _refusal(_contract([_grant(ANALYST, ["DESCRIBE"], excluded=["msisdn"])]))
        assert error.kind == "lakeformation-grant-columns"
        assert "does not grant SELECT" in str(error)

    def test_table_level_permissions_without_a_column_limit_are_untouched(self):
        contract = _contract([_grant(STEWARD, ["SELECT", "ALTER", "INSERT", "DESCRIBE"])])
        (body,) = _emitted(contract).values()
        assert body["permissions"] == ["SELECT", "ALTER", "INSERT", "DESCRIBE"]
        assert "table" in body and "table_with_columns" not in body
