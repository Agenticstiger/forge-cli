# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A filtered identity never lands in BigQuery's every-row policy, whatever its case.

BigQuery shows a principal the union of the row access policies naming it, so an
identity that a row filter names and that also sits in ``fluid_all_rows`` reads every
row. The identities a filter names are left out of that policy; the comparison folds
case, so a grant or a reader spelled in another case does not slip past it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fluid_build.iac.providers.gcp_governance import ALL_ROWS_POLICY_ID, row_policies

FILTERED = "group:branch-north@library.test"
OTHER_CASE = "group:Branch-North@library.test"
READER = "group:readers@library.test"


def _contract(*, grants: List[Dict[str, Any]], readers: List[str]) -> Dict[str, Any]:
    binding = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {"project": "demo", "dataset": "sales", "table": "orders"},
        "principals": {
            FILTERED: "group:branch-north@fluid-fake.test-corp.com",
            OTHER_CASE: "group:Branch-North@fluid-fake.test-corp.com",
            READER: "group:readers@fluid-fake.test-corp.com",
        },
    }
    exposure = {
        "exposeId": "orders",
        "binding": binding,
        "contract": {
            "schema": [{"name": "id", "type": "string"}, {"name": "branch", "type": "string"}]
        },
        "policy": {
            "authz": {
                "readers": readers,
                "rowFilters": [{"principal": FILTERED, "where": "branch = 'north'"}],
            }
        },
    }
    return {"accessPolicy": {"grants": grants}, "exposes": [exposure]}


def _all_rows_grantees(contract: Dict[str, Any]) -> set:
    policies = row_policies(contract, contract["exposes"][0], "c", "orders")
    return {g for p in policies if p.policy_id == ALL_ROWS_POLICY_ID for g in p.grantees}


def _filter_grantees(contract: Dict[str, Any]) -> set:
    policies = row_policies(contract, contract["exposes"][0], "c", "orders")
    return {g for p in policies if p.policy_id != ALL_ROWS_POLICY_ID for g in p.grantees}


def test_a_grant_spelled_in_another_case_is_not_in_the_every_row_policy():
    contract = _contract(
        grants=[
            {"principal": OTHER_CASE, "permissions": ["read"]},
            {"principal": READER, "permissions": ["read"]},
        ],
        readers=[],
    )
    every = _all_rows_grantees(contract)
    assert "group:readers@fluid-fake.test-corp.com" in every
    assert not {g for g in every if g.casefold().startswith("group:branch-north@")}
    assert _filter_grantees(contract) == {"group:branch-north@fluid-fake.test-corp.com"}


def test_a_reader_spelled_in_another_case_is_not_in_the_every_row_policy():
    contract = _contract(
        grants=[{"principal": READER, "permissions": ["read"]}], readers=[OTHER_CASE]
    )
    every = _all_rows_grantees(contract)
    assert every == {"group:readers@fluid-fake.test-corp.com"}


def test_the_filtered_identity_in_its_own_case_stays_out_as_before():
    contract = _contract(
        grants=[
            {"principal": FILTERED, "permissions": ["read"]},
            {"principal": READER, "permissions": ["read"]},
        ],
        readers=[],
    )
    assert _all_rows_grantees(contract) == {"group:readers@fluid-fake.test-corp.com"}
