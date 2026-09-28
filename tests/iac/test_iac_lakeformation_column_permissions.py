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
  table", so those are refused at emit rather than split out, and so is a
  second grant for the same principal on the same table.

The grant option: the permissions reference says "When granting SELECT, you
can't include the grant option if column filtering is applied", the console
page offers it under simple column-based access, and *Data filtering
limitations* settles it: "To grant SELECT with the grant option and column
filtering, you must use an include list, not an exclude list." So SELECT's
grant option is kept beside ``columns`` and refused beside ``excludedColumns``.
DESCRIBE's grant option is refused beside either: no resource can carry it.

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

import logging
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

    def test_selects_grant_option_is_kept_beside_an_include_list(self):
        # Data filtering limitations: the grant option with column filtering
        # needs "an include list, not an exclude list".
        contract = _contract(
            [_grant(AUDITOR, ["SELECT", "DESCRIBE"], columns=["id"], grant_option=["SELECT"])]
        )
        (body,) = _emitted(contract).values()
        assert body["permissions"] == ["SELECT"]
        assert body["permissions_with_grant_option"] == ["SELECT"]
        assert body["table_with_columns"][0]["column_names"] == ["id"]

    def test_a_describe_dropped_beside_a_column_limit_is_logged(self, caplog):
        # Lake Formation implies DESCRIBE with the SELECT ("If a user has other
        # Lake Formation permissions on a ... table ..., DESCRIBE is implicitly
        # granted"), so nothing is lost, but the plan differs from the contract.
        with caplog.at_level(logging.INFO, logger="fluid_build.iac.providers.aws"):
            _emitted(DEMO)
        logged = [r.getMessage() for r in caplog.records if "lf_column_grant_describe" in r.msg]
        assert len(logged) == 2, logged
        assert any("grants[1]" in m and ANALYST in m for m in logged), logged
        assert any("grants[2]" in m and AUDITOR in m for m in logged), logged

    def test_nothing_is_logged_for_a_select_only_column_grant(self, caplog):
        with caplog.at_level(logging.INFO, logger="fluid_build.iac.providers.aws"):
            _emitted(_contract([_grant(ANALYST, ["SELECT"], excluded=["msisdn"])]))
        assert not [r for r in caplog.records if "lf_column_grant_describe" in r.msg]

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

    @pytest.mark.parametrize("permissions", [["SELECT"], ["SELECT", "DESCRIBE"]])
    def test_selects_grant_option_beside_an_exclude_list(self, permissions):
        contract = _contract(
            [_grant(ANALYST, permissions, excluded=["msisdn"], grant_option=["SELECT"])]
        )
        assert FluidSchemaManager().validate_contract(contract).is_valid
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert "grants[0]" in str(error) and "exclude" in str(error)

    @pytest.mark.parametrize("limit", ["excluded", "columns"])
    @pytest.mark.parametrize("grant_option", [["DESCRIBE"], ["SELECT", "DESCRIBE"]])
    def test_describes_grant_option_beside_a_column_limit(self, limit, grant_option):
        # Nothing can carry it: table_with_columns takes SELECT only, and a
        # DESCRIBE on the table is refused to a principal holding a partial SELECT.
        cols = {"excluded": ["msisdn"]} if limit == "excluded" else {"columns": ["id"]}
        contract = _contract(
            [_grant(ANALYST, ["SELECT", "DESCRIBE"], grant_option=grant_option, **cols)]
        )
        assert FluidSchemaManager().validate_contract(contract).is_valid
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert "grants[0]" in str(error) and "DESCRIBE" in str(error)


class TestOneGrantPerPrincipalBesideAColumnLimit:
    """A second grant for the column-limited principal on the same table.

    Lake Formation refuses DESCRIBE, ALTER, DROP, DELETE and INSERT on the table to
    a principal holding a partial SELECT; a table-level SELECT or ALL would read the
    columns the limit withholds; and hashicorp/aws reads a ``table_with_columns``
    grant back through the principal's whole listing on the table, so a pair never
    plans clean. The contract cannot express the pair, whichever grant comes first.
    """

    @pytest.mark.parametrize(
        "other",
        [
            _grant(ANALYST, ["DESCRIBE"]),
            _grant(ANALYST, ["INSERT"]),
            _grant(ANALYST, ["SELECT"]),
            _grant(ANALYST, ["SELECT"], columns=["id"]),
        ],
        ids=["describe", "insert", "select", "second-column-limit"],
    )
    @pytest.mark.parametrize("column_grant_first", [False, True])
    def test_is_refused(self, other, column_grant_first):
        column_grant = _grant(ANALYST, ["SELECT"], excluded=["msisdn"])
        grants = [column_grant, other] if column_grant_first else [other, column_grant]
        contract = _contract([_grant(STEWARD, ["SELECT", "DESCRIBE"]), *grants])
        assert FluidSchemaManager().validate_contract(contract).is_valid
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert "grants[1]" in str(error) and "grants[2]" in str(error), str(error)
        assert ANALYST in str(error)

    def test_a_grant_with_no_permissions_is_not_a_second_grant(self):
        # The emitter writes nothing for it, so there is no pair.
        contract = _contract(
            [_grant(ANALYST, []), _grant(ANALYST, ["SELECT"], excluded=["msisdn"])]
        )
        assert sorted(_emitted(contract)) == ["lf_columns_lf_grant_customers_1"]

    def test_other_principals_beside_a_column_limit_are_untouched(self):
        assert len(_emitted(DEMO)) == 3
