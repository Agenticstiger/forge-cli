# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Postgres driver's role check, against a real server.

The unit tests stub the rows the check reads. Here ``_PRIVILEGE_QUERY`` runs on Postgres
for one role per privileged membership, held directly and through another role, and for
a plain role, and the driver connects as such roles to an expose with row filters. It
skips unless ``FLUID_TEST_POSTGRES_DSN`` names a server whose user may create roles and
grant the predefined ones. The CI job that starts one sets it, and sets
``FLUID_TEST_POSTGRES_REQUIRED=1`` so that a missing server fails the job instead
(``_live_postgres.py``). Docker is CI-only for forge.
"""

from __future__ import annotations

import secrets
from typing import Any, Callable, Iterator, List, Tuple

import pytest

from ._live_postgres import live_postgres

pytestmark = pytest.mark.integration

_DSN, psycopg = live_postgres()

from psycopg import sql as pgsql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from fluid_build.output_ports.mcp.drivers.postgres import (  # noqa: E402
    _PRIVILEGE_QUERY,
    PRIVILEGED_ROLES,
    PostgresDriver,
)
from fluid_build.output_ports.mcp.query_compiler import RowFilterUnavailable  # noqa: E402

_TAG = secrets.token_hex(4)
_PASSWORD = secrets.token_hex(12)


@pytest.fixture(scope="module")
def server() -> Iterator[Tuple[Any, Callable[..., str]]]:
    """An autocommit admin connection, and a factory for roles it drops at the end."""
    admin = psycopg.connect(_DSN, autocommit=True)
    created: List[str] = []

    def create(name: str, *, grants: Tuple[str, ...] = (), login: bool = False) -> str:
        role = f"fluid_rc_{name}_{_TAG}"
        access = (
            pgsql.SQL("LOGIN PASSWORD {}").format(pgsql.Literal(_PASSWORD))
            if login
            else pgsql.SQL("NOLOGIN")
        )
        admin.execute(pgsql.SQL("CREATE ROLE {} {}").format(pgsql.Identifier(role), access))
        created.append(role)
        for granted in grants:
            admin.execute(
                pgsql.SQL("GRANT {} TO {}").format(
                    pgsql.Identifier(granted), pgsql.Identifier(role)
                )
            )
        return role

    try:
        yield admin, create
    finally:
        for role in reversed(created):
            admin.execute(pgsql.SQL("DROP ROLE IF EXISTS {}").format(pgsql.Identifier(role)))
        admin.close()


def _check_as(admin: Any, role: str) -> Tuple[bool, List[str]]:
    """What the role check reads for ``role``: whether it is a superuser, and the
    privileged roles it is a member of."""
    with admin.cursor() as cursor:
        cursor.execute(pgsql.SQL("SET ROLE {}").format(pgsql.Identifier(role)))
        try:
            cursor.execute(_PRIVILEGE_QUERY)
            superuser, held = cursor.fetchall()[0]
        finally:
            cursor.execute("RESET ROLE")
    return bool(superuser), list(held)


@pytest.mark.parametrize("privileged", PRIVILEGED_ROLES)
@pytest.mark.parametrize("held", ["directly", "through-another-role"])
def test_the_role_query_finds_each_privileged_membership(server, privileged, held):
    admin, create = server
    if held == "directly":
        role = create(f"d{PRIVILEGED_ROLES.index(privileged)}", grants=(privileged,))
    else:
        middle = create(f"m{PRIVILEGED_ROLES.index(privileged)}", grants=(privileged,))
        role = create(f"i{PRIVILEGED_ROLES.index(privileged)}", grants=(middle,))
    superuser, roles = _check_as(admin, role)
    assert not superuser
    assert privileged in roles


def test_the_role_query_finds_nothing_for_a_plain_role(server):
    admin, create = server
    assert _check_as(admin, create("plain")) == (False, [])


def test_the_role_query_finds_a_superuser(server):
    """The DSN's own user created the server's roles above, as a superuser."""
    admin, _ = server
    with admin.cursor() as cursor:
        cursor.execute(_PRIVILEGE_QUERY)
        superuser, _held = cursor.fetchall()[0]
    assert superuser


def _driver(role: str) -> PostgresDriver:
    server = conninfo_to_dict(_DSN)
    options = {key: server[key] for key in ("host", "port", "dbname") if key in server}
    options.update(user=role, password=_PASSWORD)
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {
                "database": options.get("dbname", "postgres"),
                "schema": "public",
                "table": "rows",
            },
        },
        "policy": {
            "authz": {"rowFilters": [{"principal": "group:eu@x.example", "where": "id > 0"}]}
        },
    }
    return PostgresDriver(expose=expose, contract={}, connection_options=options)


def test_the_driver_refuses_row_filters_as_a_privileged_login(server):
    _, create = server
    middle = create("login_mid", grants=("pg_read_all_data",))
    driver = _driver(create("login_reader", grants=(middle,), login=True))
    with pytest.raises(RowFilterUnavailable, match="pg_read_all_data"):
        driver._get_connection()
    assert driver._connection is None


def test_the_driver_serves_row_filters_as_a_plain_login(server):
    _, create = server
    driver = _driver(create("login_plain", login=True))
    try:
        assert driver._get_connection() is not None
    finally:
        driver.close()
