# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Who reads every row of an expose that declares ``policy.authz.rowFilters``.

The principals that read every row are ``policy.authz.readers`` and, when the caller
passes ``contract=``, the principal of each unconditional ``accessPolicy`` read grant
that covers the expose, less every principal ``binding.principals`` maps to ``[]`` and
every principal a row filter names (compared by logical key with the case folded).
A caller's verified principals match that set as written; nothing is translated through
``binding.principals``. The schema has no explicit deny for rows, so there is none to
read here.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build._mcp_compat import (
    open_inmemory_session,
    self_attesting_client_kwargs,
)  # noqa: E402
from fluid_build.output_ports.mcp import OutputPortMcpServer, OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp import query_compiler as qc  # noqa: E402
from fluid_build.output_ports.mcp._handlers import (  # noqa: E402
    tool_query,
    tool_query_sql,
    tool_sample,
)
from fluid_build.output_ports.mcp.drivers import build_driver  # noqa: E402

BRANCH = "group:branch-north@library.test"
READER = "group:readers@library.test"
GRANTEE = "group:analysts@library.test"
LOADER = "serviceAccount:loader@library.test"

_COLUMNS = ("id", "branch", "amount")
_ROWS = [(1, "north", 10), (2, "south", 20), (3, "north", 30), (4, "east", 40)]
_NORTH_IDS = [1, 3]
_ALL_IDS = [1, 2, 3, 4]


def _grant(principal: str = GRANTEE, **extra: Any) -> Dict[str, Any]:
    grant: Dict[str, Any] = {"principal": principal, "permissions": ["read"]}
    grant.update(extra)
    return grant


def _contract(
    grants: Optional[List[Any]] = None,
    *,
    readers: Optional[List[str]] = None,
    principals: Optional[Dict[str, Any]] = None,
    filter_principal: str = BRANCH,
    access_policy: Any = "default",
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "orders",
        "contract": {
            "schema": [
                {"name": "id", "type": "integer"},
                {"name": "branch", "type": "string"},
                {"name": "amount", "type": "integer"},
            ]
        },
        "semantics": {"measures": [{"name": "total", "agg": "sum", "expr": "amount"}]},
        "policy": {
            "authz": {
                "readers": [] if readers is None else readers,
                "rowFilters": [{"principal": filter_principal, "where": "branch = 'north'"}],
            }
        },
    }
    if principals is not None:
        expose["binding"] = {"principals": principals}
    contract: Dict[str, Any] = {"exposes": [expose]}
    if access_policy == "default":
        contract["accessPolicy"] = {"grants": grants or []}
    elif access_policy is not None:
        contract["accessPolicy"] = access_policy
    return contract


def _predicate(contract: Dict[str, Any], principal: str, *, with_contract: bool = True):
    return qc.row_filter_predicate(
        contract["exposes"][0],
        {"principal": principal},
        dialect="duckdb",
        contract=contract if with_contract else None,
    )


def _refused(call) -> Optional[str]:
    try:
        call()
    except (qc.RowFilterIdentityMissing, qc.RowFilterUnavailable) as exc:
        return type(exc).__name__
    return None


# (id, accessPolicy.grants, reads every row with contract=)
_GRANT_CASES = [
    ("read", [_grant()], True),
    ("select", [_grant(permissions=["Select"])], True),
    ("query", [_grant(permissions=["query"])], True),
    ("admin", [_grant(permissions=[" admin "])], True),
    # ``owner`` reads on BigQuery but holds no table privilege on Databricks.
    ("owner", [_grant(permissions=["owner"])], False),
    ("owner-and-read", [_grant(permissions=["owner", "read"])], True),
    ("write-only", [_grant(permissions=["write"])], False),
    ("no-permissions", [_grant(permissions=[])], False),
    ("conditions-absent", [_grant()], True),
    ("conditions-none", [_grant(conditions=None)], True),
    ("conditions-empty", [_grant(conditions={})], True),
    ("conditions-set", [_grant(conditions={"expression": "request.time < x"})], False),
    ("conditions-text", [_grant(conditions="weekdays")], False),
    ("conditions-list", [_grant(conditions=[])], False),
    ("resources-empty", [_grant(resources=[])], True),
    ("resources-every", [_grant(resources=["$.exposes[*]"])], True),
    ("resources-exposes", [_grant(resources=["$.exposes"])], True),
    ("resources-this", [_grant(resources=["$.exposes[?(@.exposeId=='orders')]"])], True),
    ("resources-this-double", [_grant(resources=['$.exposes[?(@.exposeId=="orders")]'])], True),
    ("resources-elsewhere", [_grant(resources=["$.exposes[?(@.exposeId=='returns')]"])], False),
    ("resources-unevaluable", [_grant(resources=["$.exposes[0]"])], False),
    (
        "resources-one-unevaluable",
        [_grant(resources=["$.exposes[*]", "$.exposes[0]"])],
        False,
    ),
    ("principal-not-a-string", [_grant(principal=7)], False),
    ("entry-not-a-mapping", ["group:analysts@library.test"], False),
]


@pytest.mark.parametrize(
    ("grants", "reads"), [c[1:] for c in _GRANT_CASES], ids=[c[0] for c in _GRANT_CASES]
)
def test_a_grant_reads_every_row_only_when_it_is_an_unconditional_read_covering_the_expose(
    grants, reads
):
    contract = _contract(grants)
    if reads:
        assert _predicate(contract, GRANTEE) is None
    else:
        assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterIdentityMissing"
    # Without contract= grants are never read.
    assert _refused(lambda: _predicate(contract, GRANTEE, with_contract=False)) == (
        "RowFilterIdentityMissing"
    )


def test_a_reader_reads_every_row_with_and_without_the_contract():
    contract = _contract([], readers=[READER])
    assert _predicate(contract, READER) is None
    assert _predicate(contract, READER, with_contract=False) is None


def test_the_filtered_principal_keeps_its_predicate():
    contract = _contract([_grant()])
    assert "branch" in _predicate(contract, BRANCH)


def test_a_caller_holding_a_grantee_and_a_filtered_principal_reads_every_row():
    contract = _contract([_grant()])
    attrs = {"principal": [BRANCH, GRANTEE]}
    assert qc.row_filter_predicate(contract["exposes"][0], attrs, contract=contract) is None
    assert qc.row_filter_predicate(contract["exposes"][0], attrs) is not None


def test_a_grantee_mapped_to_nothing_is_refused():
    contract = _contract([_grant()], principals={GRANTEE: []})
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterIdentityMissing"


def test_a_reader_mapped_to_nothing_is_refused_even_without_the_contract():
    contract = _contract([], readers=[READER], principals={READER: []})
    assert _refused(lambda: _predicate(contract, READER)) == "RowFilterIdentityMissing"
    assert _refused(lambda: _predicate(contract, READER, with_contract=False)) == (
        "RowFilterIdentityMissing"
    )


def test_the_empty_mapping_is_matched_by_logical_key_and_case():
    contract = _contract([_grant()], principals={"Group:Analysts@library.test": []})
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterIdentityMissing"


def test_a_principal_mapped_to_an_identity_is_not_translated():
    contract = _contract(
        [_grant()],
        principals={GRANTEE: "group:analysts@corp.example.com", BRANCH: "group:b@corp.example.com"},
    )
    assert _predicate(contract, GRANTEE) is None
    # The identity it maps to is not a claim that matches.
    assert _refused(lambda: _predicate(contract, "group:analysts@corp.example.com"))


@pytest.mark.parametrize(
    "principals",
    [
        "group:analysts@library.test",
        {"": "x"},
        {GRANTEE: ""},
        {GRANTEE: 7},
        {GRANTEE: ["a", 7]},
        {GRANTEE: ["a", ""]},
    ],
)
def test_a_binding_principals_shape_the_emitters_refuse_is_refused(principals):
    contract = _contract([_grant()], principals=principals)
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterUnavailable"


def test_a_grant_or_reader_spelled_in_another_case_than_a_filter_never_reads_every_row():
    contract = _contract(
        [_grant("Group:Branch-North@library.test")],
        readers=["group:BRANCH-NORTH@library.test"],
    )
    for spelling in ("Group:Branch-North@library.test", "group:BRANCH-NORTH@library.test"):
        assert _refused(lambda: _predicate(contract, spelling)) == "RowFilterIdentityMissing"
    # The filtered principal as written still reads its rows.
    assert "branch" in _predicate(contract, BRANCH)


def test_the_prefix_spelling_is_one_key_for_the_filter_subtraction():
    contract = _contract([_grant("serviceaccount:loader@library.test")], filter_principal=LOADER)
    assert _refused(lambda: _predicate(contract, "serviceaccount:loader@library.test"))
    assert "branch" in _predicate(contract, LOADER)


def test_the_caller_matches_as_written_not_by_another_spelling():
    contract = _contract([_grant()])
    assert _refused(lambda: _predicate(contract, "Group:analysts@library.test")) == (
        "RowFilterIdentityMissing"
    )


@pytest.mark.parametrize(
    "access_policy",
    [
        "grants",
        ["x"],
        {"grants": {"principal": GRANTEE}},
        {"grants": "x"},
        {"$ref": "other.yaml#/policy"},
        {"grants": {"$ref": "other.yaml#/grants"}},
    ],
)
def test_a_malformed_access_policy_is_refused(access_policy):
    contract = _contract(access_policy=access_policy)
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterUnavailable"
    # Without contract= it is not read.
    assert _refused(lambda: _predicate(contract, GRANTEE, with_contract=False)) == (
        "RowFilterIdentityMissing"
    )


def test_a_missing_access_policy_grants_nothing():
    contract = _contract(access_policy=None)
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterIdentityMissing"


def test_readers_that_are_not_a_list_are_still_refused():
    contract = _contract([_grant()])
    contract["exposes"][0]["policy"]["authz"]["readers"] = GRANTEE
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterUnavailable"
    assert _refused(lambda: _predicate(contract, GRANTEE, with_contract=False)) == (
        "RowFilterUnavailable"
    )


def test_the_legacy_policies_surface_is_never_read():
    contract = _contract(access_policy=None)
    contract["metadata"] = {
        "policies": {"readers": {"principals": [GRANTEE], "permissions": ["read"]}}
    }
    assert _refused(lambda: _predicate(contract, GRANTEE)) == "RowFilterIdentityMissing"


# ---------------------------------------------------------------------
# Every read path
# ---------------------------------------------------------------------


@pytest.fixture()
def table(tmp_path: Path):
    path = tmp_path / "orders.csv"
    path.write_text(
        ",".join(_COLUMNS) + "\n" + "".join(",".join(map(str, r)) + "\n" for r in _ROWS),
        encoding="utf-8",
    )
    return path


def _driver(table: Path, contract: Dict[str, Any]):
    expose = contract["exposes"][0]
    expose["binding"] = {
        **expose.get("binding", {}),
        "platform": "local",
        "format": "csv",
        "location": {"path": str(table), "table": "orders"},
    }
    return build_driver(expose=expose, contract=contract)


def _con():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INTEGER, branch VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", _ROWS)
    return con


def _semantic_total(contract: Dict[str, Any], principal: str, *, with_contract: bool) -> float:
    compiled = qc.compile_semantic_query(
        expose=contract["exposes"][0],
        measure="total",
        limit=10,
        caller_attributes={"principal": principal},
        table_reference="orders",
        dialect="duckdb",
        contract=contract if with_contract else None,
    )
    assert not compiled.params
    return float(_con().execute(compiled.sql).fetchall()[0][0])


def _free_form_ids(contract: Dict[str, Any], principal: str, *, with_contract: bool) -> List[int]:
    compiled = qc.compile_free_form_sql(
        sql="SELECT id FROM orders",
        table_reference="orders",
        limit=100,
        expose=contract["exposes"][0],
        caller_attributes={"principal": principal},
        dialect="duckdb",
        contract=contract if with_contract else None,
    )
    return sorted(r[0] for r in _con().execute(compiled.sql).fetchall())


def _sample_ids(table: Path, contract: Dict[str, Any], principal: str, *, with_contract: bool):
    driver = _driver(table, contract if with_contract else _without_grants(contract))
    result = driver.sample(limit=100, caller_attributes={"principal": principal})
    column = list(result.columns).index("id")
    return sorted(
        int(row[column]) if not isinstance(row, dict) else int(row["id"]) for row in result.rows
    )


def _without_grants(contract: Dict[str, Any]) -> Dict[str, Any]:
    # ``sample`` takes its contract from the driver; a driver built without the
    # grants stands for a library caller that does not pass one.
    stripped = copy.deepcopy(contract)
    stripped.pop("accessPolicy", None)
    return stripped


def test_a_grantee_reads_every_row_on_every_path_and_is_refused_without_the_contract(table):
    contract = _contract([_grant()])
    assert _semantic_total(contract, GRANTEE, with_contract=True) == 100
    assert _free_form_ids(contract, GRANTEE, with_contract=True) == _ALL_IDS
    assert _sample_ids(table, contract, GRANTEE, with_contract=True) == _ALL_IDS
    assert _refused(lambda: _semantic_total(contract, GRANTEE, with_contract=False))
    assert _refused(lambda: _free_form_ids(contract, GRANTEE, with_contract=False))
    with pytest.raises(qc.RowFilterIdentityMissing):
        _sample_ids(table, contract, GRANTEE, with_contract=False)


@pytest.mark.parametrize(
    "grants",
    [
        [_grant(permissions=["write"])],
        [_grant(conditions={"expression": "x"})],
        [_grant(resources=["$.exposes[?(@.exposeId=='returns')]"])],
        [_grant(conditions="x")],
    ],
    ids=["write-only", "conditional", "elsewhere", "conditions-not-a-mapping"],
)
def test_a_grant_that_does_not_count_is_refused_on_every_path(table, grants):
    contract = _contract(grants)
    assert _refused(lambda: _semantic_total(contract, GRANTEE, with_contract=True))
    assert _refused(lambda: _free_form_ids(contract, GRANTEE, with_contract=True))
    with pytest.raises(qc.RowFilterIdentityMissing):
        _sample_ids(table, contract, GRANTEE, with_contract=True)


def test_a_mapped_to_nothing_grantee_is_refused_on_every_path(table):
    contract = _contract([_grant()], principals={GRANTEE: []})
    assert _refused(lambda: _semantic_total(contract, GRANTEE, with_contract=True))
    assert _refused(lambda: _free_form_ids(contract, GRANTEE, with_contract=True))
    with pytest.raises(qc.RowFilterIdentityMissing):
        _sample_ids(table, contract, GRANTEE, with_contract=True)


def test_the_filtered_principal_reads_its_rows_on_every_path(table):
    contract = _contract([_grant()])
    assert _semantic_total(contract, BRANCH, with_contract=True) == 40
    assert _free_form_ids(contract, BRANCH, with_contract=True) == _NORTH_IDS
    assert _sample_ids(table, contract, BRANCH, with_contract=True) == _NORTH_IDS


def test_a_malformed_access_policy_is_refused_on_every_path(table):
    contract = _contract(access_policy="grants")
    for call in (
        lambda: _semantic_total(contract, GRANTEE, with_contract=True),
        lambda: _free_form_ids(contract, GRANTEE, with_contract=True),
        lambda: _sample_ids(table, contract, GRANTEE, with_contract=True),
    ):
        with pytest.raises(qc.RowFilterUnavailable):
            call()


class _State:
    """What the tool handlers read of a session, over a real DuckDB driver."""

    def __init__(self, table: Path, contract: Dict[str, Any]) -> None:
        from types import SimpleNamespace

        self.contract = contract
        self.expose = contract["exposes"][0]
        self.caller_attributes: Dict[str, Any] = {}
        self.policy = SimpleNamespace(max_sample_rows=100)
        self.query_timeout_seconds = None
        driver = _driver(table, contract)
        self._driver = driver
        self.get_driver = lambda: driver


def test_the_tool_handlers_pass_the_sessions_contract(table):
    state = _State(table, _contract([_grant()]))
    attrs = {"principal": GRANTEE}
    sample = tool_sample(state, {"limit": 100}, caller_attributes=attrs)
    assert sorted(int(r["id"]) for r in sample["rows"]) == _ALL_IDS
    sql = tool_query_sql(state, {"sql": "SELECT id FROM orders"}, caller_attributes=attrs)
    assert sorted(int(r["id"]) for r in sql["rows"]) == _ALL_IDS
    query = tool_query(state, {"measure": "total"}, caller_attributes=attrs)
    assert float(query["rows"][0]["total"]) == 100


# ---------------------------------------------------------------------
# A principal a client states about itself is never the verified one
# ---------------------------------------------------------------------


def _server(contract: Dict[str, Any], table: Path, tmp_path: Path) -> OutputPortMcpServer:
    expose = contract["exposes"][0]
    expose["binding"] = {
        **expose.get("binding", {}),
        "platform": "local",
        "format": "csv",
        "location": {"path": str(table), "table": "orders"},
    }
    contract = {"fluidVersion": "0.7.6", "kind": "DataProduct", "id": "lib.demo", **contract}
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose, readable_paths=(tmp_path.resolve(),)
    )
    return OutputPortMcpServer(
        contract=contract, expose=expose, policy=policy, logger=logging.getLogger("test.readers")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [True, False], ids=["with-grant", "without-grant"])
async def test_a_client_naming_a_grantee_in_client_info_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: Path, granted: bool
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLUID_AUDIT_ROOT", str(tmp_path / "audit"))
    for name in ("FLUID_MCP_AUTH_MODE", "FLUID_MCP_AUTH_TOKEN", "FLUID_MCP_JWT_ISSUER"):
        monkeypatch.delenv(name, raising=False)
    contract = _contract([_grant()] if granted else [], readers=[READER])
    server = _server(contract, table, tmp_path)
    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("lib-test", "1.0.0", principal=GRANTEE),
    ) as client:
        sampled = await client.call_tool("sample", {"limit": 10})
        queried = await client.call_tool("query_sql", {"sql": "SELECT id FROM orders"})
    for result in (sampled, queried):
        payload = json.loads(result.content[0].text)
        assert payload.get("rows") is None, payload
        assert payload.get("error"), payload
