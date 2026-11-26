# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The gateway never lets a principal read every row that a platform policy filters.

One contract, eight kinds of principal, three bindings. For each platform the emitter
writes the policy that decides who reads every row, and the gateway
(``query_compiler.row_filter_predicate``) decides the same from the contract:

* ``reader``: in ``policy.authz.readers``;
* ``grantee``: an unconditional read grant covering the expose;
* ``writer``: a write-only grant;
* ``owner``: a grant whose only read verb is ``owner``, which reads on BigQuery but holds
  no table privilege on Databricks, so the gateway does not count it;
* ``conditional``: a read grant with a condition;
* ``elsewhere``: a read grant scoped to another expose;
* ``unmapped``: a read grant whose principal ``binding.principals`` maps to ``[]``;
* ``filtered``: a row filter's principal, which also holds a read grant spelled with
  another prefix case.

The gateway matches a caller's verified principals as written and never translates one
through ``binding.principals``, so on Lake Formation, whose grants name IAM ARNs, parity
stops at what the emitter writes: the filtered principal's grant carries the filter, the
others' carry none, and the gateway never lets the filtered principal read every row.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers.aws import lf_row_filters
from fluid_build.iac.providers.databricks_access import compile_enforcement, string_literal
from fluid_build.iac.providers.gcp_governance import ALL_ROWS_POLICY_ID, row_policies
from fluid_build.output_ports.mcp import query_compiler as qc

READER = "group:readers@library.test"
GRANTEE = "group:analysts@library.test"
WRITER = "group:loaders@library.test"
OWNER = "group:stewards@library.test"
CONDITIONAL = "group:auditors@library.test"
ELSEWHERE = "group:returns-team@library.test"
UNMAPPED = "group:contractors@library.test"
FILTERED = "group:branch-north@library.test"
FILTERED_GRANT = "Group:branch-north@library.test"

ACCOUNT = "123456789012"
_NAMES = {
    READER: "readers",
    GRANTEE: "analysts",
    WRITER: "loaders",
    OWNER: "stewards",
    CONDITIONAL: "auditors",
    ELSEWHERE: "returns-team",
    UNMAPPED: "contractors",
    FILTERED: "branch-north",
}
_SCHEMA = [{"name": "id", "type": "string"}, {"name": "branch", "type": "string"}]


def _base(*, with_conditional: bool = False) -> Dict[str, Any]:
    grants: List[Dict[str, Any]] = [
        {"principal": GRANTEE, "permissions": ["read"]},
        {"principal": WRITER, "permissions": ["write"]},
        {"principal": OWNER, "permissions": ["owner"]},
        {
            "principal": ELSEWHERE,
            "permissions": ["read"],
            "resources": ["$.exposes[?(@.exposeId=='returns')]"],
        },
        {"principal": UNMAPPED, "permissions": ["read"]},
        {"principal": FILTERED_GRANT, "permissions": ["read"]},
    ]
    if with_conditional:
        grants.append(
            {"principal": CONDITIONAL, "permissions": ["read"], "conditions": {"expression": "x"}}
        )
    orders = {
        "exposeId": "orders",
        "contract": {"schema": copy.deepcopy(_SCHEMA)},
        "policy": {
            "authz": {
                "readers": [READER],
                "rowFilters": [{"principal": FILTERED, "where": "branch = 'north'"}],
            }
        },
    }
    returns = {"exposeId": "returns", "contract": {"schema": copy.deepcopy(_SCHEMA)}}
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "library.orders",
        "accessPolicy": {"grants": grants},
        "exposes": [orders, returns],
    }


def _bind(contract: Dict[str, Any], platform: str) -> Dict[str, Any]:
    for expose in contract["exposes"]:
        table = expose["exposeId"]
        if platform == "gcp":
            expose["binding"] = {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {"project": "demo", "dataset": "sales", "table": table},
                "principals": {k: f"group:{n}@fluid-fake.test-corp.com" for k, n in _NAMES.items()},
            }
        elif platform == "databricks":
            expose["binding"] = {
                "platform": "databricks",
                "format": "delta_table",
                "location": {"database": "main", "schema": "sales", "table": table},
                "principals": {k: f"{n}-group" for k, n in _NAMES.items()},
            }
        else:
            expose["binding"] = {
                "platform": "aws",
                "format": "parquet",
                "location": {
                    "database": "silver",
                    "table": table,
                    "bucket": "library-silver",
                    "path": f"silver/{table}/",
                },
                "principals": {k: f"arn:aws:iam::{ACCOUNT}:role/{n}" for k, n in _NAMES.items()},
                "governance": {
                    "lakeFormation": {
                        "grants": [
                            {
                                "principal": f"arn:aws:iam::{ACCOUNT}:role/{_NAMES[who]}",
                                "permissions": ["SELECT", "DESCRIBE"],
                            }
                            for who in (READER, GRANTEE, FILTERED)
                        ]
                    }
                },
            }
    # Mapped to nothing on every cloud.
    contract["exposes"][0]["binding"]["principals"][UNMAPPED] = []
    return contract


def _gateway(contract: Dict[str, Any], who: str, *, with_contract: bool = True):
    return qc.row_filter_predicate(
        contract["exposes"][0],
        {"principal": who},
        dialect="duckdb",
        contract=contract if with_contract else None,
    )


def _reads_every_row(contract, who, *, with_contract=True) -> bool:
    try:
        return _gateway(contract, who, with_contract=with_contract) is None
    except qc.RowFilterIdentityMissing:
        return False


def _gateway_readers(contract) -> set:
    return {
        who
        for who in (READER, GRANTEE, WRITER, OWNER, CONDITIONAL, ELSEWHERE, UNMAPPED, FILTERED)
        if _reads_every_row(contract, who)
    }


def test_the_gateway_lets_exactly_the_readers_and_unconditional_read_grants_read_every_row():
    contract = _bind(_base(with_conditional=True), "gcp")
    assert _gateway_readers(contract) == {READER, GRANTEE}
    assert "branch" in _gateway(contract, FILTERED)
    for who in (WRITER, OWNER, CONDITIONAL, ELSEWHERE, UNMAPPED):
        with pytest.raises(qc.RowFilterIdentityMissing):
            _gateway(contract, who)
    # Without the contract, only policy.authz.readers read every row.
    assert [w for w in (READER, GRANTEE) if _reads_every_row(contract, w, with_contract=False)] == [
        READER
    ]
    assert not _reads_every_row(contract, FILTERED_GRANT)


def test_bigquery_every_row_policy_holds_the_readers_and_grantees_and_never_a_filtered_identity():
    contract = _bind(_base(with_conditional=True), "gcp")
    policies = row_policies(contract, contract["exposes"][0], "c", "orders")
    every = {g for p in policies if p.policy_id == ALL_ROWS_POLICY_ID for g in p.grantees}
    mine = {g for p in policies if p.policy_id != ALL_ROWS_POLICY_ID for g in p.grantees}

    def identity(who: str) -> str:
        return f"group:{_NAMES[who]}@fluid-fake.test-corp.com"

    gateway = _gateway_readers(contract)
    assert {identity(READER), identity(GRANTEE)} <= every
    assert {identity(w) for w in gateway} <= every, "the gateway is never wider than BigQuery"
    # BigQuery reads an owner-only grant; the gateway, held to every emitter, does not.
    assert identity(OWNER) in every and OWNER not in gateway
    assert identity(UNMAPPED) not in every | mine
    assert {g.casefold() for g in every}.isdisjoint({g.casefold() for g in mine})
    assert identity(FILTERED) in mine and identity(FILTERED) not in every
    assert not any(g.casefold() == identity(FILTERED).casefold() for g in every)


@pytest.fixture()
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-0123456789abcdef")


def _databricks_privileges(contract: Dict[str, Any], table: str) -> Dict[str, set]:
    module = json.loads(build_module(IAC_PLUGINS["databricks"], contract))
    tables = module["resource"]["databricks_sql_table"]
    by_ref = {f"${{databricks_sql_table.{key}.id}}": t["name"] for key, t in tables.items()}
    out: Dict[str, set] = {}
    for grant in module["resource"]["databricks_grant"].values():
        if "table" in grant and by_ref.get(grant["table"]) == table:
            out.setdefault(grant["principal"], set()).update(grant["privileges"])
    return out


def test_databricks_grants_select_to_readers_and_grantees_and_the_filter_names_only_the_filtered(
    _warehouse,
):
    contract = _bind(_base(), "databricks")
    held = _databricks_privileges(contract, "orders")
    select = {p for p, privileges in held.items() if "SELECT" in privileges}
    assert {"readers-group", "analysts-group"} <= select
    assert "loaders-group" not in select
    assert "stewards-group" not in held, "Unity Catalog grants an owner-only grant nothing"
    assert "returns-team-group" not in held
    assert "contractors-group" not in held

    exposure = contract["exposes"][0]
    enforcement = compile_enforcement(
        where="exposes[orders]",
        index=0,
        exposure=exposure,
        binding=exposure["binding"],
        catalog="main",
        schema="sales",
        table="orders",
        columns=_SCHEMA,
    )
    body = enforcement.row_filter.function.body
    members = body.split(" OR (", 1)[0]  # ``NOT (<every filtered identity>)``
    assert string_literal("branch-north-group") in members
    for name in ("readers-group", "analysts-group"):
        assert string_literal(name) not in body

    assert _gateway_readers(contract) == {READER, GRANTEE}
    assert {n for n in ("readers-group", "analysts-group")} <= select


def test_databricks_refuses_a_conditional_grant(_warehouse):
    contract = _bind(_base(with_conditional=True), "databricks")
    with pytest.raises(UnsupportedBindingError):
        IAC_PLUGINS["databricks"].emit(contract)


def test_lake_formation_grants_the_filtered_role_through_the_filter_and_no_other():
    contract = _bind(_base(), "aws")
    exposure = contract["exposes"][0]
    filters = lf_row_filters(exposure, exposure["binding"])
    grants = exposure["binding"]["governance"]["lakeFormation"]["grants"]
    arn = {who: f"arn:aws:iam::{ACCOUNT}:role/{_NAMES[who]}" for who in _NAMES}
    carrying = {grants[i]["principal"] for i in filters}
    assert carrying == {arn[FILTERED]}
    assert all(f.principal == FILTERED for f in filters.values())
    # Its grant is the one that carries the filter; the reader's and the grantee's carry none.
    assert arn[READER] not in carrying and arn[GRANTEE] not in carrying
    # The gateway never lets the filtered principal read every row, and it matches the
    # reader and the grantee as written: the ARNs a binding maps them to are not claims.
    assert not _reads_every_row(contract, FILTERED)
    assert not _reads_every_row(contract, FILTERED_GRANT)
    assert not _reads_every_row(contract, arn[READER])
    assert _gateway_readers(contract) == {READER, GRANTEE}


def test_the_gateway_counts_only_the_verbs_that_read_on_every_emitter():
    """Each verb the gateway counts reads on BigQuery and holds SELECT on Databricks."""
    from fluid_build.providers._access_grants import EVERY_ROW_VERBS, READ_VERBS

    databricks_reads = {
        verb
        for verb, privilege in dbx._TABLE_PRIVILEGES.items()
        if privilege in {"SELECT", "ALL_PRIVILEGES"}
    }
    assert EVERY_ROW_VERBS <= READ_VERBS
    assert EVERY_ROW_VERBS <= databricks_reads


@pytest.mark.parametrize(
    ("filter_principal", "grant_principal", "identity"),
    [
        (
            "alice@fluid-fake.test-corp.com",
            "user:alice@fluid-fake.test-corp.com",
            "user:alice@fluid-fake.test-corp.com",
        ),
        (
            "user:alice@fluid-fake.test-corp.com",
            "alice@fluid-fake.test-corp.com",
            "user:alice@fluid-fake.test-corp.com",
        ),
        (
            "etl@proj.iam.gserviceaccount.com",
            "serviceAccount:etl@proj.iam.gserviceaccount.com",
            "serviceAccount:etl@proj.iam.gserviceaccount.com",
        ),
    ],
)
def test_an_unmapped_filter_and_grant_that_differ_in_the_prefix_are_one_member_on_bigquery(
    filter_principal, grant_principal, identity
):
    # With no binding.principals, BigQuery reads an unprefixed principal as the member of
    # the type it infers, so the two spellings are one member and it is filtered, never in
    # the every-row policy; the gateway must not let the grant's spelling read every row.
    contract = _base()
    contract["accessPolicy"] = {"grants": [{"principal": grant_principal, "permissions": ["read"]}]}
    orders = contract["exposes"][0]
    orders["policy"]["authz"] = {
        "rowFilters": [{"principal": filter_principal, "where": "branch = 'north'"}]
    }
    orders["binding"] = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {"project": "demo", "dataset": "sales", "table": "orders"},
    }
    policies = row_policies(contract, orders, "c", "orders")
    every = {g for p in policies if p.policy_id == ALL_ROWS_POLICY_ID for g in p.grantees}
    mine = {g for p in policies if p.policy_id != ALL_ROWS_POLICY_ID for g in p.grantees}
    assert identity in mine and identity not in every
    assert not _reads_every_row(contract, grant_principal)
    assert not _reads_every_row(contract, filter_principal)
