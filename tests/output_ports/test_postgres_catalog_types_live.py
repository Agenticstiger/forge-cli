# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Postgres catalog read that types a filtered caller's comparisons, on a real server.

``catalog_columns_sql`` reads ``pg_catalog`` for the relation the exposed table's name
resolves to: a table, a view or a materialized view (``information_schema.columns``
lists no materialized view), with an enum and ``citext`` read as text and a domain as
its base type. The unit tests check the statement; here it runs on Postgres. It skips
unless ``FLUID_TEST_POSTGRES_DSN`` names a server whose user may create a schema, types
and tables. The CI job that starts one sets it, and sets ``FLUID_TEST_POSTGRES_REQUIRED=1``
so that a missing server fails the job instead (``_live_postgres.py``). Docker is CI-only
for forge.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

from ._live_postgres import live_postgres

pytestmark = pytest.mark.integration

_DSN, psycopg = live_postgres()

from psycopg import sql as pgsql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from fluid_build.output_ports.mcp.drivers.postgres import PostgresDriver  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import (  # noqa: E402
    ColumnType,
    QueryValidationError,
    compile_free_form_sql,
)

_SCHEMA = f"fluid_ct_{secrets.token_hex(4)}"
EU = "group:eu@x.example"

# Column name: (its type, with {schema} for this test's own types; what it reads as).
_COLUMNS: Dict[str, Tuple[str, Optional[ColumnType]]] = {
    "id": ("integer", ColumnType("number", 10, 0)),
    "region": ("text", ColumnType("string")),
    "amount": ("numeric(10,2)", ColumnType("number", 10, 2)),
    "total": ("numeric", ColumnType("number")),
    "ratio": ("double precision", ColumnType("float")),
    "label": ("varchar(20)", ColumnType("string")),
    "flag": ("boolean", ColumnType("boolean")),
    "created": ("date", ColumnType("date")),
    "seen": ("timestamp", ColumnType("timestamp")),
    "stamped": ("timestamptz", ColumnType("timestamptz")),
    "mood": ("{schema}.mood", ColumnType("string")),
    "price": ("{schema}.cents", ColumnType("number", 12, 2)),
    "code": ("{schema}.code", ColumnType("string")),
    "tags": ("text[]", ColumnType("complex")),
    "doc": ("jsonb", ColumnType("complex")),
    "addr": ("inet", None),
}


def _type_sql(written: str) -> Any:
    if written.startswith("{schema}."):
        name = written.split(".", 1)[1]
        return pgsql.SQL("{}.{}").format(pgsql.Identifier(_SCHEMA), pgsql.Identifier(name))
    return pgsql.SQL(written)


@pytest.fixture(scope="module")
def server() -> Iterator[Dict[str, Optional[ColumnType]]]:
    """A schema holding a table, a view and a materialized view of every type in
    ``_COLUMNS`` (and ``citext`` when the server has the extension); yields what each
    column reads as. Everything it creates is dropped at the end."""
    admin = psycopg.connect(_DSN, autocommit=True)
    schema = pgsql.Identifier(_SCHEMA)
    expected = {name: read_as for name, (_, read_as) in _COLUMNS.items()}
    created_citext = False
    try:
        admin.execute(pgsql.SQL("CREATE SCHEMA {}").format(schema))
        admin.execute(pgsql.SQL("CREATE TYPE {}.mood AS ENUM ('ok', 'sad')").format(schema))
        admin.execute(pgsql.SQL("CREATE DOMAIN {}.cents AS numeric(12,2)").format(schema))
        admin.execute(pgsql.SQL("CREATE DOMAIN {}.code AS varchar(8)").format(schema))
        columns = [
            pgsql.SQL("{} {}").format(pgsql.Identifier(name), _type_sql(written))
            for name, (written, _) in _COLUMNS.items()
        ]
        installed = admin.execute("SELECT 1 FROM pg_extension WHERE extname = 'citext'")
        available = admin.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'citext'"
        ).fetchall()
        if available and not installed.fetchall():
            admin.execute(pgsql.SQL("CREATE EXTENSION citext SCHEMA {}").format(schema))
            created_citext = True
        if admin.execute("SELECT 1 FROM pg_type WHERE typname = 'citext'").fetchall():
            citext = admin.execute(
                "SELECT pg_catalog.format_type(oid, NULL) FROM pg_type "
                "WHERE typname = 'citext' LIMIT 1"
            ).fetchall()[0][0]
            columns.append(pgsql.SQL("email {}").format(pgsql.SQL(citext)))
            expected["email"] = ColumnType("string")
        admin.execute(
            pgsql.SQL("CREATE TABLE {}.typed ({}, dropped integer)").format(
                schema, pgsql.SQL(", ").join(columns)
            )
        )
        admin.execute(pgsql.SQL("ALTER TABLE {}.typed DROP COLUMN dropped").format(schema))
        admin.execute(
            pgsql.SQL("CREATE VIEW {0}.typed_view AS SELECT * FROM {0}.typed").format(schema)
        )
        admin.execute(
            pgsql.SQL("CREATE MATERIALIZED VIEW {0}.typed_mv AS SELECT * FROM {0}.typed").format(
                schema
            )
        )
        yield expected
    finally:
        if created_citext:
            admin.execute("DROP EXTENSION IF EXISTS citext CASCADE")
        admin.execute(pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(schema))
        admin.close()


def _driver(relation: str) -> PostgresDriver:
    """A driver over ``relation`` as the DSN's own user. That user may be a superuser,
    which the role check refuses for an expose with row filters, so the catalog is read
    through an expose without them."""
    server = conninfo_to_dict(_DSN)
    keys = ("host", "port", "dbname", "user", "password")
    options = {key: server[key] for key in keys if key in server}
    expose: Dict[str, Any] = {
        "exposeId": "typed",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {
                "database": options.get("dbname", "postgres"),
                "schema": _SCHEMA,
                "table": relation,
            },
        },
    }
    return PostgresDriver(expose=expose, contract={}, connection_options=options)


@pytest.mark.parametrize("relation", ["typed", "typed_view", "typed_mv"])
def test_the_catalog_read_types_every_column(server, relation):
    driver = _driver(relation)
    try:
        assert driver.physical_column_types() == server
    finally:
        driver.close()


@pytest.mark.parametrize(
    ("where", "allowed"),
    [
        ("amount > 5", True),
        ("price > 5.5", True),
        ("mood = 'ok'", True),
        ("code = 'x'", True),
        ("created = CURRENT_DATE", True),
        ("created > CAST(CURRENT_DATE - INTERVAL '7 day' AS DATE)", True),
        ("stamped > now() - INTERVAL '1 day'", True),
        ("ratio > 1.5", True),
        ("total > 5", False),
        ("seen < now()", False),
        ("addr = '10.0.0.1'", False),
        ("doc = 'x'", False),
    ],
)
def test_a_filtered_callers_comparisons_are_typed_by_the_live_catalog(server, where, allowed):
    driver = _driver("typed_mv")
    expose = {
        "exposeId": "typed",
        "contract": {"schema": [{"name": name} for name in server]},
        "policy": {"authz": {"rowFilters": [{"principal": EU, "where": "region = 'EU'"}]}},
    }

    def compile_it() -> Any:
        return compile_free_form_sql(
            sql=f"SELECT id FROM typed_mv WHERE {where}",
            table_reference="typed_mv",
            limit=10,
            expose=expose,
            caller_attributes={"principal": EU},
            dialect="postgres",
            physical_types=driver.physical_column_types,
        )

    try:
        if allowed:
            compile_it()
        else:
            with pytest.raises(QueryValidationError, match="on an expose with row filters"):
                compile_it()
    finally:
        driver.close()
