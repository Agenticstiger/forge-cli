# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every place a connection block can name where its secret goes is judged.

The policy reads ``connection.host`` and the host part of every URI field. A
driver can also be pointed somewhere by a host in the URI's query string, by a
database name that a connection URL reads as more than a name, or by the
SSH tunnel it connects through.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import pytest

from fluid_build.secrets.contract import lint_secret_refs
from fluid_build.secrets.policy import (
    ALLOWLIST_ENV,
    AcquisitionSecretPolicyError,
    refuse_destination_overrides,
    refuse_unless_allowed,
)

REMOTE = "db.example.com"
ELSEWHERE = "elsewhere.example"
ANY_HOST = {ALLOWLIST_ENV: "PG"}


def _refusal(
    connection: Mapping[str, Any], environ: Optional[Dict[str, str]] = None
) -> Optional[AcquisitionSecretPolicyError]:
    try:
        refuse_unless_allowed("env://PG", connection=connection, environ=environ or {})
    except AcquisitionSecretPolicyError as exc:
        return exc
    return None


def _lint(connection: Dict[str, Any]) -> Any:
    sent = {**connection, "secretRef": "env://PG"}  # pragma: allowlist secret
    contract = {"builds": [{"properties": {"source": {"connection": sent}}}]}
    (finding,) = lint_secret_refs(contract, environ={})
    return finding


# ── a host in a URI's query string ───────────────────────────────────────────

QUERY_HOSTS = [
    {"uri": f"postgresql://localhost/db?host={ELSEWHERE}"},
    {"uri": "postgresql://localhost/db?hostaddr=192.0.2.7"},
    {"uri": f"postgresql://localhost/db?sslmode=require&host={ELSEWHERE}"},
    {"uri": f"postgresql://localhost/db?HOST={ELSEWHERE}"},
    {"uri": f"postgresql://localhost/db?%68ost={ELSEWHERE}"},
    {"uri": f"postgresql://localhost/db?sslmode=require#&host={ELSEWHERE}"},
    {"uri": "postgresql://localhost/db?service=prod"},
    {"connection_string": f"mongodb://localhost:27017/db?proxyHost={ELSEWHERE}"},
    {"uri": f"mssql+pyodbc://localhost/db?odbc_connect=DRIVER%3Dx%3BSERVER%3D{ELSEWHERE}"},
    {"uri": f"mysql+pymysql://u@localhost/db?unix_socket=/tmp/{ELSEWHERE}.sock"},
    {"uri": f"snowflake://u@{REMOTE}/db?account={ELSEWHERE}"},
    {"sqlalchemy_url": f"postgresql+psycopg2://u@localhost/db?host={ELSEWHERE}"},
    {"host": "localhost", "jdbc_url_params": f"ssl=true&host={ELSEWHERE}"},
    {"host": "localhost", "jdbc_url_params": f"encrypt=true;serverName={ELSEWHERE}"},
]


@pytest.mark.unit
@pytest.mark.parametrize("connection", QUERY_HOSTS)
def test_a_host_named_in_a_query_string_is_refused(connection):
    """An entry for the host part does not clear a host the query string names."""
    refusal = _refusal(connection, {ALLOWLIST_ENV: f"PG@{REMOTE}"})
    assert refusal is not None
    assert "query string" in refusal.what
    assert ELSEWHERE not in str(refusal)
    assert "192.0.2.7" not in str(refusal)
    # An entry for any host clears it: the operator chose not to judge the host.
    assert _refusal(connection, ANY_HOST) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "connection",
    [
        {"uri": "postgresql://localhost/db?sslmode=require&connect_timeout=10"},
        {"uri": "postgresql://localhost/db?application_name=fluid&options=-c%20x%3D1"},
        {"connection_string": "mongodb://localhost:27017/db?replicaSet=rs0&authSource=admin"},
        {"endpoint": f"https://localhost/api?host={ELSEWHERE}"},
        {"host": "localhost", "jdbc_url_params": "ssl=true&sslmode=require"},
    ],
)
def test_a_query_string_that_names_no_host_is_fine(connection):
    """An HTTP client never takes its host from the query, so an https query is not read."""
    assert _refusal(connection) is None


@pytest.mark.unit
def test_validate_refuses_a_host_in_a_query_string():
    finding = _lint({"uri": f"postgresql://localhost/db?host={ELSEWHERE}"})
    assert not finding.ok
    assert "query string" in finding.error.what


@pytest.mark.unit
def test_validate_refuses_a_query_host_beside_a_placeholder_host():
    """The query decides the host whatever the placeholder renders to, so it does not wait."""
    finding = _lint({"uri": f"postgresql://{{{{ env.PGHOST }}}}/db?host={ELSEWHERE}"})
    assert not finding.ok
    assert finding.deferred is None
    assert "query string" in finding.error.what


@pytest.mark.unit
def test_an_sqlalchemy_url_is_a_destination():
    """A tap's sqlalchemy_url wins over its host and port, so its host is judged."""
    refusal = _refusal({"host": "localhost", "sqlalchemy_url": f"postgresql://u@{REMOTE}/db"})
    assert refusal is not None
    assert REMOTE in refusal.what
    assert _refusal({"sqlalchemy_url": "postgresql://u@localhost/db"}) is None


# ── a database name a connection URL reads as more ───────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "database",
    [
        f"db?host={ELSEWHERE}",
        "db#x",
        f"db;serverName={ELSEWHERE}",
        "db&x",
        "db@x",
        "db\\x",
        "db\nx",
        "db\x00",
    ],
)
@pytest.mark.parametrize("field", ["database", "dbname"])
def test_a_database_name_with_url_syntax_is_refused(field, database):
    refusal = _refusal({"host": "localhost", field: database})
    assert refusal is not None
    assert f"connection.{field}" in refusal.what
    assert ELSEWHERE not in str(refusal)
    assert _refusal({"host": "localhost", field: database}, ANY_HOST) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "database", ["orders", "orders_db", "my-db", "a/b", "café", "with space", 42]
)
def test_a_plain_database_name_is_fine(database):
    assert _refusal({"host": "localhost", "database": database}) is None


@pytest.mark.unit
def test_validate_refuses_a_database_name_with_url_syntax():
    finding = _lint({"host": "localhost", "database": f"db?host={ELSEWHERE}"})
    assert not finding.ok
    assert "connection.database" in finding.error.what


# ── an SSH tunnel the client connects through ────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "tunnel",
    [
        {"ssh_tunnel": {"enable": True, "host": ELSEWHERE, "port": 22}},
        {"tunnel_method": {"tunnel_method": "SSH_KEY_AUTH", "tunnel_host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_method": "SSH_PASSWORD_AUTH", "tunnel_host": ELSEWHERE}},
    ],
    ids=["ssh_tunnel", "tunnel_method-key", "tunnel_method-password"],
)
def test_the_host_of_a_tunnel_in_use_is_a_destination(tunnel):
    connection = {"host": REMOTE, **tunnel}
    refusal = _refusal(connection, {ALLOWLIST_ENV: f"PG@{REMOTE}"})
    assert refusal is not None
    assert ELSEWHERE in refusal.what
    assert "tunnel" in refusal.what
    both = {ALLOWLIST_ENV: f"PG@{REMOTE},PG@{ELSEWHERE}"}
    assert _refusal(connection, both) is None


@pytest.mark.unit
def test_behind_a_tunnel_a_loopback_host_is_not_this_machine():
    """The client reads the host from the tunnel host's side, so localhost is that machine."""
    connection = {"host": "localhost", "ssh_tunnel": {"enable": True, "host": "bastion.example"}}
    refusal = _refusal(connection, {ALLOWLIST_ENV: "PG@bastion.example"})
    assert refusal is not None
    assert "localhost" in refusal.what
    assert "tunnel" in refusal.why
    allowed = {ALLOWLIST_ENV: "PG@bastion.example,PG@localhost"}
    assert _refusal(connection, allowed) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "tunnel",
    [
        {"ssh_tunnel": {"enable": False, "host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_method": "NO_TUNNEL", "tunnel_host": ELSEWHERE}},
    ],
)
def test_a_tunnel_switched_off_is_not_judged(tunnel):
    assert _refusal({"host": "localhost", **tunnel}) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "tunnel",
    [
        {"ssh_tunnel": {"enable": True}},
        {"ssh_tunnel": {"enable": True, "host": "  "}},
        {"ssh_tunnel": "bastion.example"},
        {"tunnel_method": {"tunnel_method": "SSH_KEY_AUTH"}},
        {"tunnel_method": {"tunnel_method": "SSH_KEY_AUTH", "tunnel_host": ["a", "b"]}},
    ],
)
def test_a_tunnel_without_a_readable_host_cannot_be_cleared(tunnel):
    assert _refusal({"host": "localhost", **tunnel}) is not None
    assert _refusal({"host": "localhost", **tunnel}, ANY_HOST) is None


@pytest.mark.unit
def test_validate_judges_a_tunnel_host():
    finding = _lint({"host": "localhost", "ssh_tunnel": {"enable": True, "host": ELSEWHERE}})
    assert not finding.ok
    assert ELSEWHERE in finding.error.what


# ── an override that replaces a URL-bound connector setting ──────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["database.port", "database.dbname", "database.names", "connector.class", "Database.DBName"],
)
def test_an_override_may_not_replace_a_setting_the_url_is_built_from(key):
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_destination_overrides({key: "x"}, where="properties.debezium.extra_config")
    assert "properties.debezium.extra_config" in info.value.what


# ── a user part that hides a host from urlsplit ──────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "connection",
    [
        {"connection_string": f"mongodb://localhost#@{ELSEWHERE}/db"},
        {"connection_string": f"mongodb://127.0.0.1?@{ELSEWHERE}/db"},
        {"uri": f"postgresql://{REMOTE}#@{ELSEWHERE}/db"},
        {"sqlalchemy_url": f"postgresql://u@127.0.0.1?@{ELSEWHERE}/db"},
        {"uri": f"localhost#@{ELSEWHERE}/db"},
        {"sqlalchemy_url": f"postgresql://localhost:1/@{ELSEWHERE}/db"},
        {"uri": f"postgresql://{REMOTE}:1/x@{ELSEWHERE}/db"},
        {"uri": f"postgresql://localhost:5432/db?application_name=a@{ELSEWHERE}"},
    ],
)
def test_a_user_part_a_client_reads_past_a_query_or_fragment_cannot_be_cleared(connection):
    """urlsplit ends the authority at ``/``, ``?`` or ``#``; a client may read on to an ``@``.

    The MongoDB drivers take the last ``@`` before the first ``/``, and SQLAlchemy's
    ``make_url`` reads a password through ``/``, ``?`` and ``#`` to the first ``@``.
    """
    refusal = _refusal(connection, {ALLOWLIST_ENV: f"PG@{REMOTE}"})
    assert refusal is not None
    assert "could not be determined" in refusal.what
    assert ELSEWHERE not in str(refusal)
    assert _refusal(connection, ANY_HOST) is None


@pytest.mark.unit
def test_validate_refuses_a_user_part_that_hides_a_host_beside_a_placeholder():
    finding = _lint({"uri": f"postgresql://{{{{ env.PGHOST }}}}#@{ELSEWHERE}/db"})
    assert not finding.ok
    assert finding.deferred is None


@pytest.mark.unit
def test_a_schemeless_uri_with_a_query_host_is_refused():
    refusal = _refusal({"uri": f"localhost:27017/db?host={ELSEWHERE}"})
    assert refusal is not None
    assert "query string" in refusal.what


@pytest.mark.unit
def test_a_windows_domain_user_part_is_read_as_a_user():
    """``DOMAIN\\user`` is a SQL Server login: every parser reads the host after its ``@``."""
    connection = {"uri": f"mssql+pyodbc://DOMAIN\\user:pw@{REMOTE}/db"}  # pragma: allowlist secret
    assert _refusal(connection, {ALLOWLIST_ENV: f"PG@{REMOTE}"}) is None
