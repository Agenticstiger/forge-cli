# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Row filters declared the schema's way are enforced on every MCP read path.

``exposes[].policy.authz.rowFilters[]`` (``{principal, where}``, fluid-schema 0.7.6)
lets its principal read only the rows its predicate selects; a reader no filter
names reads every row. The gateway used to read row filters from
``policy.rowFilters``, a key no schema allows, and ignored this one, so a contract
that declared row-level security the schema's way was served unfiltered by
``sample``, ``query`` and ``query_sql``.

What these tests hold the gateway to:

* each read of the exposed table goes through
  ``(SELECT <allowed> FROM <table> AS _fluid_rows WHERE (<predicate>))``, so no
  spelling of the caller's SQL (a subquery, a CTE, a set operation, a second
  reference) sees a row the filter removed;
* the predicate a caller gets is the union of the filters naming the caller's
  verified principals, as BigQuery's row access policies combine;
* it fails closed: no verified principal, a principal no filter or reader names, a
  malformed filter, the old ``policy.rowFilters`` key, or a predicate the engine
  would read differently all refuse the call instead of returning rows.

The compiled statements also run on a bare DuckDB connection, without the driver's
statement guard, so the filter has to hold in the SQL itself.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp import query_compiler  # noqa: E402
from fluid_build.output_ports.mcp._handlers import (  # noqa: E402
    tool_query,
    tool_query_sql,
    tool_sample,
)

EU = "group:eu-analysts@northwind.example"
T1 = "group:tenant-t1@northwind.example"
EVERYONE = "group:all-staff@northwind.example"
STRANGER = "group:contractors@northwind.example"

_ROWS = [
    # id, region, segment, amount, tenant_id, secret
    (1, "EU", "retail", 10, "t1", "S1"),
    (2, "US", "retail", 20, "t1", "S2"),
    (3, "EU", "wholesale", 30, "t2", "S3"),
    (4, "APAC", "retail", 40, "t3", "S4"),
]
_COLUMNS = ("id", "region", "segment", "amount", "tenant_id", "secret")

_FILTERS = [
    {"principal": EU, "where": "region = 'EU'"},
    {"principal": T1, "where": "tenant_id = 't1'"},
]


def _expose(
    *,
    row_filters: Optional[List[Dict[str, Any]]] = None,
    readers: Optional[List[str]] = None,
    restricted: Optional[List[str]] = None,
    metric_filter: Optional[str] = None,
) -> Dict[str, Any]:
    authz: Dict[str, Any] = {"rowFilters": _FILTERS if row_filters is None else row_filters}
    authz["readers"] = [EVERYONE] if readers is None else readers
    if restricted:
        authz["columnRestrictions"] = [
            {"principal": STRANGER, "columns": restricted, "access": "deny"}
        ]
    metric: Dict[str, Any] = {"name": "revenue", "type": "simple", "measure": "total"}
    if metric_filter:
        metric["filter"] = metric_filter
    return {
        "exposeId": "customers",
        "contract": {"schema": [{"name": name} for name in _COLUMNS]},
        "semantics": {
            "measures": [{"name": "total", "agg": "sum", "expr": "amount"}],
            "dimensions": [{"name": "segment", "type": "categorical", "expr": "segment"}],
            "metrics": [metric],
        },
        "policy": {"authz": authz},
    }


def _as(*principals: str) -> Dict[str, Any]:
    """Caller attributes of a request whose verified identity holds ``principals``."""
    if len(principals) == 1:
        return {"principal": principals[0]}
    return {"principal": list(principals)}


@pytest.fixture()
def con():
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE customers (id INTEGER, region VARCHAR, segment VARCHAR, amount INTEGER, "
        "tenant_id VARCHAR, secret VARCHAR)"
    )
    connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", _ROWS)
    yield connection
    connection.close()


@pytest.fixture()
def state_for(tmp_path):
    """A ``SessionState`` stand-in over a DuckDB driver reading a CSV of ``_ROWS``."""
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(
        ",".join(_COLUMNS) + "\n" + "".join(",".join(map(str, row)) + "\n" for row in _ROWS)
    )

    def _make(expose: Dict[str, Any]) -> SimpleNamespace:
        expose = dict(expose)
        expose["binding"] = {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "customers"},
        }
        driver = build_driver(expose=expose, contract={"exposes": [expose]})
        return SimpleNamespace(
            expose=expose,
            caller_attributes={},
            policy=SimpleNamespace(max_sample_rows=100),
            query_timeout_seconds=None,
            get_driver=lambda: driver,
        )

    return _make


def _refusal(call: Callable[[], Any]) -> str:
    """The class name of what ``call`` raised; fails when it returned rows instead."""
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - the type is the assertion
        return type(exc).__name__
    pytest.fail(f"returned instead of refusing: {result!r}")


def _sample_ids(state, attrs) -> List[int]:
    payload = tool_sample(state, {"limit": 100}, caller_attributes=attrs)
    return sorted(int(row["id"]) for row in payload["rows"])


def _query_totals(state, attrs) -> Dict[str, float]:
    payload = tool_query(
        state, {"measure": "total", "dimensions": ["segment"]}, caller_attributes=attrs
    )
    return {row["segment"]: float(row["total"]) for row in payload["rows"]}


def _query_sql_ids(state, attrs, sql: str = "SELECT id FROM customers") -> List[int]:
    payload = tool_query_sql(state, {"sql": sql}, caller_attributes=attrs)
    return sorted(int(row["id"]) for row in payload["rows"])


def _free_form(con, sql: str, attrs: Dict[str, Any], **expose_kwargs: Any) -> List[Any]:
    """Compile caller SQL for a filtered expose and run it on a bare connection,
    without the driver's statement guard. Returns the rows, sorted."""
    restricted = expose_kwargs.pop("restricted_columns", ())
    expose = expose_kwargs.pop("expose", None) or _expose(**expose_kwargs)
    compiled = query_compiler.compile_free_form_sql(
        sql=sql,
        table_reference="customers",
        limit=100,
        restricted_columns=restricted,
        expose=expose,
        caller_attributes=attrs,
        dialect="duckdb",
    )
    assert not compiled.params
    return sorted(con.execute(compiled.sql).fetchall())


# ---------------------------------------------------------------------
# Every read path serves only the caller's rows
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("principals", "ids"),
    [
        pytest.param((EU,), [1, 3], id="one-filter"),
        pytest.param((T1,), [1, 2], id="another-filter"),
        pytest.param((EU, T1), [1, 2, 3], id="two-filters-union"),
        pytest.param((EVERYONE,), [1, 2, 3, 4], id="unfiltered-reader"),
        pytest.param((EU, EVERYONE), [1, 2, 3, 4], id="filtered-and-unfiltered"),
    ],
)
def test_every_tool_serves_the_rows_the_callers_principals_may_read(state_for, principals, ids):
    state = state_for(_expose())
    attrs = _as(*principals)
    assert _sample_ids(state, attrs) == ids
    assert _query_sql_ids(state, attrs) == ids
    expected: Dict[str, float] = {}
    for row in _ROWS:
        if row[0] in ids:
            expected[row[2]] = expected.get(row[2], 0.0) + row[3]
    assert _query_totals(state, attrs) == expected


def test_a_metric_filter_with_an_or_cannot_widen_the_row_filter(state_for):
    state = state_for(_expose(metric_filter="segment = 'retail' OR 1 = 1"))
    payload = tool_query(state, {"metric": "revenue"}, caller_attributes=_as(EU))
    assert [float(row["revenue"]) for row in payload["rows"]] == [40.0]


def test_the_query_reads_a_filtered_projection():
    compiled = query_compiler.compile_semantic_query(
        expose=_expose(),
        measure="total",
        dimensions=["segment"],
        limit=10,
        table_reference="customers",
        dialect="duckdb",
        caller_attributes=_as(EU),
    )
    assert (
        "FROM (SELECT id, region, segment, amount, tenant_id, secret FROM customers AS "
        "_fluid_rows WHERE ((_fluid_rows.region = 'EU'))) AS _fluid_allowed"
    ) in compiled.sql
    assert compiled.params == []


def test_an_unfiltered_reader_keeps_the_plain_statement():
    compiled = query_compiler.compile_semantic_query(
        expose=_expose(),
        measure="total",
        limit=10,
        table_reference="customers",
        dialect="duckdb",
        caller_attributes=_as(EVERYONE),
    )
    assert "_fluid_rows" not in compiled.sql and "FROM customers" in compiled.sql


# ---------------------------------------------------------------------
# Identity: a filter needs a verified principal
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param({}, id="no-principal"),
        pytest.param(_as(STRANGER), id="principal-no-filter-or-reader-names"),
        pytest.param({"principal": ""}, id="blank-principal"),
        pytest.param({"principal": {"name": EU}}, id="principal-not-a-string"),
        pytest.param({"principal": EU.upper()}, id="principal-differs-in-case"),
        pytest.param({"tenant_id": "t1", "sub": EU}, id="other-attributes-only"),
    ],
)
def test_a_request_without_a_principal_the_policy_names_is_refused(state_for, attrs):
    state = state_for(_expose())
    for call in (
        lambda: tool_sample(state, {"limit": 10}, caller_attributes=attrs),
        lambda: tool_query(state, {"measure": "total"}, caller_attributes=attrs),
        lambda: tool_query_sql(state, {"sql": "SELECT id FROM customers"}, caller_attributes=attrs),
    ):
        assert _refusal(call) == "RowFilterIdentityMissing"


def test_without_declared_readers_only_filtered_principals_read(state_for):
    state = state_for(_expose(readers=[]))
    assert _sample_ids(state, _as(EU)) == [1, 3]
    assert (
        _refusal(lambda: tool_sample(state, {"limit": 10}, caller_attributes=_as(EVERYONE)))
        == "RowFilterIdentityMissing"
    )


def _server(expose: Dict[str, Any]):
    from fluid_build.output_ports.mcp.policy import OutputPortPolicy
    from fluid_build.output_ports.mcp.server import OutputPortMcpServer

    return OutputPortMcpServer(
        contract={"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "c", "exposes": [expose]},
        expose=expose,
        policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
        logger=logging.getLogger("test.authz_row_filters"),
    )


def _request(*, client_info: Optional[Dict[str, Any]] = None, scope=None) -> SimpleNamespace:
    info = SimpleNamespace(model_extra=dict(client_info)) if client_info is not None else None
    session = SimpleNamespace(client_params=SimpleNamespace(clientInfo=info))
    request = SimpleNamespace(scope=scope) if scope is not None else None
    return SimpleNamespace(session=session, request=request)


def test_a_self_attested_principal_never_binds():
    """The principal decides which rows a caller reads, so a client naming its own
    must not pick its filter (or a reader's every row): it binds only from a verified
    credential, whatever the auth mode."""
    server = _server(_expose())
    _model, _use_case, attrs = server._resolve_request_identity(
        _request(client_info={"principal": EVERYONE, "tenant_id": "t1"})
    )
    assert "principal" not in attrs
    _model, _use_case, attrs = server._resolve_request_identity(
        _request(
            client_info={"principal": EVERYONE},
            scope={"fluid_auth_kind": "jwt", "fluid_auth_attrs": {"principal": EU}},
        )
    )
    assert attrs["principal"] == EU


def test_a_verified_principal_reaches_the_compiled_statement_per_request(state_for):
    server = _server(_expose())
    state = state_for(_expose())
    for principal, ids in ((EU, [1, 3]), (T1, [1, 2]), (EU, [1, 3])):
        _model, _use_case, attrs = server._resolve_request_identity(
            _request(scope={"fluid_auth_kind": "jwt", "fluid_auth_attrs": {"principal": principal}})
        )
        assert _query_sql_ids(state, attrs) == ids


# ---------------------------------------------------------------------
# A filter on a restricted column
# ---------------------------------------------------------------------


def test_a_filter_on_a_restricted_column_filters_and_the_column_stays_hidden(state_for, con):
    expose = _expose(
        row_filters=[{"principal": T1, "where": "tenant_id = 't1' AND secret <> 'S9'"}],
        restricted=["tenant_id", "secret"],
    )
    state = state_for(expose)
    sample = tool_sample(state, {"limit": 10}, caller_attributes=_as(T1))
    assert sorted(int(row["id"]) for row in sample["rows"]) == [1, 2]
    assert "tenant_id" not in sample["columns"] and "secret" not in sample["columns"]
    assert _query_totals(state, _as(T1)) == {"retail": 30.0}
    star = tool_query_sql(state, {"sql": "SELECT * FROM customers"}, caller_attributes=_as(T1))
    assert sorted(int(row["id"]) for row in star["rows"]) == [1, 2]
    assert "S1" not in repr(star) and "t1" not in repr(star["rows"])
    assert (
        _refusal(
            lambda: tool_query_sql(
                state, {"sql": "SELECT tenant_id FROM customers"}, caller_attributes=_as(T1)
            )
        )
        == "QueryValidationError"
    )
    rows = _free_form(
        con,
        "SELECT id FROM customers",
        _as(T1),
        row_filters=[{"principal": T1, "where": "tenant_id = 't1'"}],
        restricted_columns=("tenant_id", "secret"),
        restricted=["tenant_id", "secret"],
    )
    assert rows == [(1,), (2,)]


# ---------------------------------------------------------------------
# query_sql cannot read around the filter
# ---------------------------------------------------------------------


# A caller statement must start with SELECT and may not contain the word SELECT
# anywhere else, so every way round the filter below is written in DuckDB's
# FROM-first form, which needs no SELECT: ``(FROM customers)`` is a subquery,
# ``UNION ALL FROM customers`` a second branch, ``(WITH c AS (...) FROM c)`` a CTE.
@pytest.mark.parametrize(
    ("sql", "ids"),
    [
        pytest.param("SELECT id FROM customers", [1, 3], id="plain"),
        pytest.param("SELECT id FROM (FROM customers) AS s", [1, 3], id="subquery"),
        pytest.param(
            "SELECT id FROM customers WHERE NOT EXISTS (FROM customers c2 WHERE c2.id = 2)",
            [1, 3],
            id="exists-subquery",
        ),
        pytest.param("SELECT id FROM (WITH c AS (FROM customers) FROM c) AS s", [1, 3], id="cte"),
        pytest.param(
            "SELECT id FROM (WITH c AS (FROM customers WHERE region <> 'EU') FROM c) AS s",
            [],
            id="cte-with-its-own-where",
        ),
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN customers AS b ON a.id = b.id",
            [1, 3],
            id="second-reference-join",
        ),
        pytest.param(
            "SELECT a.id FROM customers AS a, customers AS b WHERE b.id = 4",
            [],
            id="second-reference-cross",
        ),
        pytest.param("SELECT * FROM customers UNION ALL FROM customers", [1, 1, 3, 3], id="union"),
        pytest.param(
            "SELECT * FROM customers EXCEPT FROM customers WHERE region = 'US'",
            [1, 3],
            id="except",
        ),
        pytest.param("SELECT id FROM customers AS _fluid_rows", [1, 3], id="alias-shadows-inner"),
        pytest.param(
            "SELECT o.id FROM (VALUES ('EU')) AS x(region), LATERAL (FROM customers) AS o",
            [1, 3],
            id="lateral-after-a-region-column",
        ),
        pytest.param("SELECT id FROM main.customers", [1, 3], id="qualified-name"),
        pytest.param('SELECT id FROM "customers"', [1, 3], id="quoted-name"),
    ],
)
def test_query_sql_cannot_read_around_the_filter(con, sql, ids):
    rows = _free_form(con, sql, _as(EU))
    assert sorted(row[0] for row in rows) == ids


def test_a_predicate_column_the_table_lacks_never_reads_an_outer_one(con):
    """The predicate's columns are qualified with the projection's own alias. A
    declared column the table lacks is then an engine error, never a column of the
    caller's statement that the filter would compare instead."""
    expose = _expose(row_filters=[{"principal": EU, "where": "plan = 'gold'"}])
    expose["contract"]["schema"].append({"name": "plan"})
    predicate = query_compiler.row_filter_predicate(expose, _as(EU), dialect="duckdb")
    assert predicate == "((_fluid_rows.plan = 'gold'))"
    # Called below the caller-text checks on purpose: compile_free_form_sql would refuse
    # a statement naming a restricted ``plan``, and this holds even without that.
    rewritten = query_compiler._project_exposed_table(
        "SELECT o.id FROM (VALUES ('gold')) AS x(plan), LATERAL (FROM customers) AS o",
        table_reference="customers",
        allowed=list(_COLUMNS),
        dialect="duckdb",
        predicate=predicate,
    )
    with pytest.raises(duckdb.Error):
        con.execute(rewritten).fetchall()


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM read_csv('customers.csv')", id="file-read"),
        pytest.param("SELECT * FROM 'customers.csv'", id="file-as-table"),
        pytest.param("SELECT * FROM (TABLE customers) AS t", id="table-keyword"),
    ],
)
def test_query_sql_refuses_a_source_the_filter_cannot_cover(sql):
    with pytest.raises(query_compiler.QueryValidationError):
        query_compiler.compile_free_form_sql(
            sql=sql,
            table_reference="customers",
            limit=10,
            expose=_expose(),
            caller_attributes=_as(EU),
            dialect="duckdb",
        )


def test_every_read_of_the_exposed_table_carries_the_filter():
    import sqlglot
    from sqlglot import exp

    compiled = query_compiler.compile_free_form_sql(
        sql=(
            "SELECT a.id FROM customers AS a JOIN (WITH c AS (FROM customers) FROM c) AS c "
            "ON a.id = c.id WHERE EXISTS (FROM customers AS e WHERE e.id = a.id) "
            "UNION ALL FROM customers"
        ),
        table_reference="customers",
        limit=10,
        expose=_expose(),
        caller_attributes=_as(EU),
        dialect="duckdb",
    )
    tree = sqlglot.parse_one(compiled.sql, read="duckdb")
    reads = [table for table in tree.find_all(exp.Table) if table.name == "customers"]
    assert len(reads) == 4
    for table in reads:
        select = table.parent.parent
        assert table.alias == "_fluid_rows"
        assert select.args["where"].this.sql(dialect="duckdb") == "((_fluid_rows.region = 'EU'))"


def test_an_unfiltered_reader_keeps_the_callers_statement():
    sql = "SELECT id FROM customers"
    compiled = query_compiler.compile_free_form_sql(
        sql=sql,
        table_reference="customers",
        limit=10,
        expose=_expose(),
        caller_attributes=_as(EVERYONE),
        dialect="duckdb",
    )
    assert compiled.sql.startswith(sql)


def test_the_engine_parser_refuses_a_projection_whose_filter_was_changed():
    compiled = query_compiler.compile_free_form_sql(
        sql="SELECT id FROM customers",
        table_reference="customers",
        limit=10,
        expose=_expose(),
        caller_attributes=_as(EU),
        dialect="duckdb",
    )
    widened = compiled.sql.replace("_fluid_rows.region = 'EU'", "_fluid_rows.region = 'EU' OR TRUE")
    assert widened != compiled.sql
    with pytest.raises(query_compiler.QueryValidationError):
        query_compiler._crosscheck_duckdb_projection(
            widened, "customers", list(_COLUMNS), "((_fluid_rows.region = 'EU'))"
        )
    query_compiler._crosscheck_duckdb_projection(
        compiled.sql.split("\nLIMIT")[0],
        "customers",
        list(_COLUMNS),
        "((_fluid_rows.region = 'EU'))",
    )


# ---------------------------------------------------------------------
# A filter that cannot be applied is refused, never skipped
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_filters",
    [
        pytest.param([{"principal": EU, "where": "nope = 'EU'"}], id="unknown-column"),
        pytest.param([{"principal": EU, "where": "region = 'EU'; DROP TABLE x"}], id="separator"),
        pytest.param([{"principal": EU, "where": "region = 'EU' -- x"}], id="comment"),
        pytest.param([{"principal": EU, "where": "region = 'EU"}], id="unterminated-quote"),
        pytest.param([{"principal": EU, "where": "EXISTS (SELECT 1 FROM t)"}], id="exists-select"),
        pytest.param([{"principal": EU, "where": "EXISTS (FROM customers)"}], id="exists-from"),
        pytest.param(
            [{"principal": EU, "where": "region IN (VALUES ('EU'))"}], id="values-subquery"
        ),
        pytest.param([{"principal": EU, "where": "region = 'EU' HAVING 1 = 1"}], id="having"),
        pytest.param([{"principal": EU, "where": "region = :p_0"}], id="placeholder"),
        pytest.param([{"principal": EU, "where": "region = ?"}], id="positional-placeholder"),
        pytest.param(
            [{"principal": EU, "where": "customers.region = 'EU'"}], id="qualified-column"
        ),
        pytest.param([{"principal": EU, "where": "  "}], id="blank-predicate"),
        pytest.param([{"where": "region = 'EU'"}], id="no-principal"),
        pytest.param(
            [{"principal": EU, "where": "region = 'EU'"}, {"principal": EU, "where": "id = 1"}],
            id="principal-twice",
        ),
        pytest.param({"principal": EU, "where": "region = 'EU'"}, id="not-a-list"),
        pytest.param(["region = 'EU'"], id="entry-not-a-mapping"),
    ],
)
def test_a_filter_that_cannot_be_applied_refuses_every_tool(state_for, row_filters):
    state = state_for(_expose(row_filters=row_filters))
    for call in (
        lambda: tool_sample(state, {"limit": 10}, caller_attributes=_as(EU)),
        lambda: tool_query(state, {"measure": "total"}, caller_attributes=_as(EU)),
        lambda: tool_query_sql(
            state, {"sql": "SELECT id FROM customers"}, caller_attributes=_as(EU)
        ),
    ):
        assert _refusal(call) == "RowFilterUnavailable"


def test_malformed_readers_are_refused(state_for):
    state = state_for(_expose(readers=EVERYONE))  # type: ignore[arg-type]
    assert (
        _refusal(lambda: tool_sample(state, {"limit": 10}, caller_attributes=_as(EVERYONE)))
        == "RowFilterUnavailable"
    )


@pytest.mark.parametrize(
    ("dialect", "where"),
    [
        # BigQuery and Spark read "..." as a string, so this predicate compares two
        # literals and selects every row there.
        pytest.param("bigquery", "\"region\" <> 'x'", id="bigquery-double-quotes"),
        pytest.param("databricks", "\"region\" <> 'x'", id="spark-double-quotes"),
        pytest.param("snowflake", "`region` = 'EU'", id="snowflake-backticks"),
    ],
)
def test_a_predicate_the_engine_would_read_differently_is_refused(dialect, where):
    expose = _expose(row_filters=[{"principal": EU, "where": where}])
    for call in (
        lambda: query_compiler.compile_semantic_query(
            expose=expose,
            measure="total",
            limit=10,
            table_reference="customers",
            dialect=dialect,
            caller_attributes=_as(EU),
        ),
        lambda: query_compiler.compile_free_form_sql(
            sql="SELECT id FROM customers",
            table_reference="customers",
            limit=10,
            expose=expose,
            caller_attributes=_as(EU),
            dialect=dialect,
        ),
    ):
        assert _refusal(call) == "RowFilterUnavailable"


# ---------------------------------------------------------------------
# The old ``policy.rowFilters`` key
# ---------------------------------------------------------------------


def test_an_empty_old_key_filtered_nothing_and_is_not_refused(state_for):
    expose = _expose()
    expose["policy"] = {"rowFilters": [], "authz": expose["policy"]["authz"]}
    assert _sample_ids(state_for(expose), _as(EU)) == [1, 3]


def test_the_old_policy_row_filters_key_is_refused_not_ignored(state_for):
    """No schema allows ``policy.rowFilters``; an expose that still carries it is
    refused rather than served as though it had no row filter."""
    expose = _expose(row_filters=[])
    expose["policy"] = {"rowFilters": [{"column": "tenant_id", "equals": "t1"}]}
    state = state_for(expose)
    for attrs in ({}, {"tenant_id": "t1"}, _as(EU)):
        for call in (
            lambda: tool_sample(state, {"limit": 10}, caller_attributes=attrs),
            lambda: tool_query(state, {"measure": "total"}, caller_attributes=attrs),
            lambda: tool_query_sql(
                state, {"sql": "SELECT id FROM customers"}, caller_attributes=attrs
            ),
        ):
            assert _refusal(call) == "RowFilterUnavailable"


# ---------------------------------------------------------------------
# Startup: refuse to serve what no call could read
# ---------------------------------------------------------------------


def _jwt_auth(mapping: Optional[Dict[str, str]] = None):
    from fluid_build.output_ports.mcp.auth import AuthValidator

    return AuthValidator(
        mode="jwt",
        jwt_issuer="https://issuer.example",
        jwt_audience="fluid",
        jwt_jwks_url="https://issuer.example/jwks",
        jwt_claim_mappings=mapping,
    )


def test_startup_refuses_row_filters_no_request_can_satisfy():
    from fluid_build.cli.mcp_output_port import _row_filter_gate_unsatisfiable
    from fluid_build.output_ports.mcp.auth import AuthValidator

    expose = _expose()
    stdio = _row_filter_gate_unsatisfiable(expose, transport="stdio", http_auth=None)
    assert stdio and "policy.authz.rowFilters" in stdio
    shared = AuthValidator(mode="shared-token", shared_token="s3cret-token-value")
    assert _row_filter_gate_unsatisfiable(expose, transport="http", http_auth=shared)
    off = AuthValidator(mode="none")
    assert _row_filter_gate_unsatisfiable(expose, transport="http", http_auth=off)
    unmapped = _jwt_auth()
    assert _row_filter_gate_unsatisfiable(expose, transport="http", http_auth=unmapped)
    mapped = _jwt_auth({"https://fluid/principal": "principal"})
    assert _row_filter_gate_unsatisfiable(expose, transport="http", http_auth=mapped) is None
    assert (
        _row_filter_gate_unsatisfiable(
            _expose(row_filters=[], readers=[]), transport="stdio", http_auth=None
        )
        is None
    )


def test_startup_refuses_the_old_key_and_a_malformed_filter():
    from fluid_build.cli.mcp_output_port import _row_filter_gate_unsatisfiable

    mapped = _jwt_auth({"https://fluid/principal": "principal"})
    legacy = _expose(row_filters=[])
    legacy["policy"] = {"rowFilters": [{"column": "tenant_id", "equals": "t1"}]}
    message = _row_filter_gate_unsatisfiable(legacy, transport="http", http_auth=mapped)
    assert message and "policy.authz.rowFilters" in message
    broken = _expose(row_filters=[{"principal": EU, "where": "nope = 1"}])
    assert _row_filter_gate_unsatisfiable(broken, transport="http", http_auth=mapped)
