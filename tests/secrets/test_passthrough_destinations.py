# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A setting a connector takes its host from is judged wherever the block holds it.

The Singer and Airbyte runners hand a connection block to the connector whole,
so a host can come from a setting this policy does not name: a mapping a tap
merges into its connection URL's query string or into its driver's arguments,
or a URI under a setting of the connector's own.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import pytest

from fluid_build.secrets.contract import lint_secret_refs
from fluid_build.secrets.policy import (
    ALLOWLIST_ENV,
    AcquisitionSecretPolicyError,
    refuse_unless_allowed,
)

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


def _lint(connection: Dict[str, Any], environ: Optional[Dict[str, str]] = None) -> Any:
    sent = {**connection, "secretRef": "env://PG"}  # pragma: allowlist secret
    contract = {"builds": [{"properties": {"source": {"connection": sent}}}]}
    (finding,) = lint_secret_refs(contract, environ=environ or {})
    return finding


# ── a mapping merged into a connection URL's query or a driver's arguments ───

QUERY_MAPPINGS = [
    {"driver_type": "pymssql", "sqlalchemy_url_query": {"host": ELSEWHERE}},
    {"sqlalchemy_url_query": {"driver": "ODBC Driver 18 for SQL Server", "Server": ELSEWHERE}},
    {"sqlalchemy_url_query": {"odbc_connect": f"DRIVER=x;SERVER={ELSEWHERE}"}},
    {"sqlalchemy_options": {"host": ELSEWHERE}},
    {"sqlalchemy_options": {"read_default_file": "/tmp/my.cnf"}},
    {"url_query": {"HOST": ELSEWHERE}},
    {"jdbc_url_params": {"failoverPartner": ELSEWHERE}},
    {"jdbc_url_params": f"socketFactory=x.Factory&socketFactoryArg={ELSEWHERE}"},
    {"sqlalchemy_eng_params": {"connect_args": {"host": ELSEWHERE}}},
]


@pytest.mark.unit
@pytest.mark.parametrize("extra", QUERY_MAPPINGS)
def test_a_host_named_in_a_query_mapping_is_refused(extra):
    connection = {"host": "localhost", **extra}
    refusal = _refusal(connection)
    assert refusal is not None
    assert "names in" in refusal.what
    assert _refusal(connection, ANY_HOST) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        {"sqlalchemy_url_query": {"driver": "ODBC Driver 18", "TrustServerCertificate": "yes"}},
        {"sqlalchemy_options": {"charset": "utf8mb4"}},
        {"jdbc_url_params": {"sslmode": "require"}},
        {"sqlalchemy_eng_params": {"fast_executemany": "True", "connect_args": {"timeout": 30}}},
        {"custom_query": "SELECT host, port FROM servers WHERE id = 1"},
    ],
)
def test_a_query_mapping_that_names_no_host_is_fine(extra):
    assert _refusal({"host": "localhost", **extra}) is None


@pytest.mark.unit
def test_the_refusal_names_the_key_and_the_mapping_but_not_the_value():
    refusal = _refusal({"host": "localhost", "sqlalchemy_url_query": {"host": ELSEWHERE}})
    assert refusal is not None
    assert "'host'" in refusal.what
    assert "connection.sqlalchemy_url_query" in refusal.what
    assert ELSEWHERE not in str(refusal)


@pytest.mark.unit
def test_validate_refuses_a_host_in_a_query_mapping():
    finding = _lint({"host": "localhost", "sqlalchemy_options": {"host": ELSEWHERE}})
    assert not finding.ok
    assert "connection.sqlalchemy_options" in finding.error.what


# ── a URI under a setting of the connector's own ─────────────────────────────

NESTED_URIS = [
    {"api_url": f"https://{ELSEWHERE}/v1"},
    {"mongodb_connection_string": f"mongodb://{ELSEWHERE}:27017/db"},
    {
        "database_config": {
            "cluster_type": "SELF_MANAGED",
            "connection_string": f"mongodb://{ELSEWHERE}/",
        }
    },
    {"sqlalchemy_eng_params": {"url": f"mssql+pymssql://u@{ELSEWHERE}/db"}},
    {"replicas": [f"postgresql://{ELSEWHERE}/db"]},
    {"jdbc_url": f"jdbc:postgresql://{ELSEWHERE}/db"},
]


@pytest.mark.unit
@pytest.mark.parametrize("extra", NESTED_URIS)
def test_a_uri_anywhere_in_the_block_is_a_destination(extra):
    connection = {"host": "localhost", **extra}
    assert _refusal(connection) is not None
    assert _refusal(connection, ANY_HOST) is None


@pytest.mark.unit
def test_a_nested_uri_is_named_by_its_path_and_can_be_allowed():
    connection = {
        "host": "localhost",
        "database_config": {"connection_string": f"mongodb://{ELSEWHERE}/"},
    }
    refusal = _refusal(connection)
    assert refusal is not None
    assert f"host {ELSEWHERE!r} (connection.database_config.connection_string)" in refusal.what
    assert _refusal(connection, {ALLOWLIST_ENV: f"PG@{ELSEWHERE}"}) is None


@pytest.mark.unit
def test_a_list_entry_is_named_by_its_index():
    refusal = _refusal(
        {"host": "localhost", "replicas": ["http://127.0.0.1/", f"https://{ELSEWHERE}/"]}
    )
    assert refusal is not None
    assert "connection.replicas[1]" in refusal.what


@pytest.mark.unit
def test_a_query_host_in_a_nested_uri_is_refused():
    connection = {"host": "localhost", "api_url": f"postgresql://localhost/db?host={ELSEWHERE}"}
    refusal = _refusal(connection)
    assert refusal is not None
    assert "'host'" in refusal.what
    assert "connection.api_url" in refusal.what


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        {"api_url": "http://127.0.0.1:8080/v1"},
        {"batch_config": {"storage": {"root": "s3://bucket/prefix"}}},
        {"batch_config": {"storage": {"root": "file:///tmp/batch"}}},
        {"stream_maps": {"users": {"email": "__NULL__", "host": "__NULL__"}}},
        {"filter_schemas": ["public"], "description": "see https: notes"},
    ],
)
def test_settings_that_name_no_remote_host_are_fine(extra):
    assert _refusal({"host": "localhost", **extra}) is None


@pytest.mark.unit
def test_validate_judges_a_nested_uri():
    finding = _lint({"host": "localhost", "api_url": f"https://{ELSEWHERE}/v1"})
    assert not finding.ok
    assert "connection.api_url" in finding.error.what


@pytest.mark.unit
def test_validate_leaves_a_nested_uri_whose_host_is_a_placeholder_to_apply():
    connection = {
        "host": "localhost",
        "database_config": {"connection_string": "mongodb://{{ env.MONGO_HOST }}/db"},
    }
    finding = _lint(connection)
    assert finding.ok
    assert finding.deferred is not None
    assert "connection.database_config.connection_string" in finding.deferred.what
    assert "mongodb://{{ env.MONGO_HOST }}" in finding.deferred.what


# ── a Unix-domain socket named in a query ────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "connection",
    [
        {"uri": "postgresql:///db?host=/run/postgresql"},
        {"uri": "postgresql://u@/db?host=%2Frun%2Fpostgresql"},
        {"uri": "postgresql+psycopg2:///db?host=/cloudsql/proj:region:inst"},
        {"uri": "mysql+pymysql://u@/db?unix_socket=/tmp/mysql.sock"},
        {"host": "localhost", "sqlalchemy_options": {"unix_socket": "/var/run/mysqld/mysqld.sock"}},
    ],
)
def test_a_socket_path_in_a_query_is_this_machine(connection):
    assert _refusal(connection) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "connection",
    [
        {"uri": f"postgresql:///db?host=/run/postgresql,{ELSEWHERE}"},
        {"uri": f"postgresql:///db?host=/run/postgresql%2C{ELSEWHERE}"},
        {"uri": f"postgresql:///db?host=//{ELSEWHERE}/x"},
        {"uri": f"postgresql:///db?host=/run/postgresql&host={ELSEWHERE}"},
        {"uri": "postgresql:///db?hostaddr=/run/postgresql"},
        {"uri": "postgresql:///db?host=@abstract"},
        {"uri": "postgresql://@@/db?host=/run/postgresql"},
    ],
)
def test_anything_but_socket_paths_in_a_query_is_still_refused(connection):
    assert _refusal(connection) is not None


@pytest.mark.unit
def test_a_socket_is_this_machine_behind_a_tunnel_too():
    """The client opens the socket here; the tunnel forwards TCP connections only."""
    connection = {
        "uri": "postgresql:///db?host=/run/postgresql",
        "ssh_tunnel": {"enable": True, "host": "bastion.example"},
    }
    assert _refusal(connection, {ALLOWLIST_ENV: "PG@bastion.example"}) is None


# ── a tunnel its connector leaves off ────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "tunnel",
    [
        {"ssh_tunnel": {"host": ELSEWHERE, "port": 22}},
        {"ssh_tunnel": {"enable": None, "host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_method": "no_tunnel", "tunnel_host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_method": " No_Tunnel ", "tunnel_host": ELSEWHERE}},
    ],
)
def test_a_tunnel_its_connector_leaves_off_is_not_judged(tunnel):
    assert _refusal({"host": "localhost", **tunnel}) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "tunnel",
    [
        {"ssh_tunnel": {"enable": "false", "host": ELSEWHERE}},
        {"ssh_tunnel": {"enable": 1, "host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_host": ELSEWHERE}},
        {"tunnel_method": {"tunnel_method": "SSH_KEY_AUTH", "tunnel_host": ELSEWHERE}},
    ],
)
def test_a_tunnel_its_connector_may_use_is_judged(tunnel):
    assert _refusal({"host": "localhost", **tunnel}) is not None


@pytest.mark.unit
def test_validate_leaves_a_tunnel_without_enable_alone():
    finding = _lint({"host": "localhost", "ssh_tunnel": {"host": "{{ env.BASTION }}"}})
    assert finding.ok
    assert finding.deferred is None


# ── every container is read; storage and local files name no host ───────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        {"cfg": {"u": f"jdbc:mysql:loadbalance://{ELSEWHERE}/db"}},
        {"cfg": {"u": f"JDBC:oracle:thin:@{ELSEWHERE}:1521:orcl"}},
        {"database": {"url": f"postgres://{ELSEWHERE}/x"}},
        {"credentials": {"auth_url": f"https://{ELSEWHERE}/x"}},
    ],
)
def test_a_uri_in_any_container_is_a_destination(extra):
    assert _refusal({"host": "localhost", **extra}) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        {"catalog": {"url": "sqlite:///c.db"}},
        {"catalog": {"url": "sqlite+pysqlite:///c.db"}},
        {"docker": "unix:///var/run/docker.sock"},
        {"staging": "abfs://c@acct.dfs.core.windows.net/p"},
        {"warehouse": "hdfs://namenode:8020/wh"},
    ],
)
def test_storage_and_local_files_name_no_host(extra):
    assert _refusal({"host": "localhost", **extra}) is None


@pytest.mark.unit
def test_a_mapping_key_is_named_as_written():
    refusal = _refusal({"host": "localhost", "sqlalchemy_options": {"unix_socket": "x/y"}})
    assert refusal is not None
    assert "'unix_socket' names in connection.sqlalchemy_options" in refusal.what
    assert "remove 'unix_socket' from connection.sqlalchemy_options" in refusal.fix


@pytest.mark.unit
def test_a_cyclic_block_is_read_once():
    connection: Dict[str, Any] = {"host": "localhost", "api_url": f"https://{ELSEWHERE}/"}
    connection["self"] = connection
    assert _refusal(connection) is not None


@pytest.mark.unit
def test_a_cyclic_contract_as_written_is_read_once(monkeypatch):
    from fluid_build.secrets import resolve_secret, secret_run
    from fluid_build.secrets.policy import destinations_as_written

    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    contract: Dict[str, Any] = {"builds": []}
    contract["self"] = contract
    with destinations_as_written(contract), secret_run("apply"):
        with pytest.raises(AcquisitionSecretPolicyError):
            resolve_secret("env://PG", connection={"host": ELSEWHERE})


@pytest.mark.unit
def test_validate_quotes_a_uri_under_a_key_named_host_as_a_uri():
    connection = {
        "host": "localhost",
        "replica": {"host": "postgres://{{ env.H }}/db?x=QUERYTEXT"},
    }
    finding = _lint(connection)
    assert finding.ok
    assert finding.deferred is not None
    assert "('postgres://{{ env.H }}')" in finding.deferred.what
    assert "QUERYTEXT" not in finding.deferred.what


@pytest.mark.unit
def test_validate_leaves_a_uri_under_a_key_that_is_not_text_to_apply():
    finding = _lint({"host": "localhost", 5: "postgres://{{ env.H }}/db"})
    assert finding.ok
    assert finding.deferred is not None


# ── a field named for a host, every URI spelling, and what is never quoted ───


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra, field",
    [
        (
            {"instance_type": {"instance": "standalone", "host": ELSEWHERE, "port": 27017}},
            "connection.instance_type.host",
        ),
        (
            {"instance_type": {"server_addresses": f"{ELSEWHERE}:27017"}},
            "connection.instance_type.server_addresses",
        ),
        ({"hosts": [f"{ELSEWHERE}:27017"]}, "connection.hosts[0]"),
        ({"proxy": {"hostname": ELSEWHERE}}, "connection.proxy.hostname"),
    ],
)
def test_a_field_named_for_a_host_is_a_destination_anywhere(extra, field):
    connection = {"host": "localhost", **extra}
    refusal = _refusal(connection)
    assert refusal is not None
    assert field in refusal.what
    assert _refusal(connection, {ALLOWLIST_ENV: f"PG@{ELSEWHERE}"}) is None


@pytest.mark.unit
def test_a_loopback_or_empty_named_host_needs_no_entry():
    connection = {"host": "localhost", "proxy": {"host": "127.0.0.1"}, "replica": {"host": ""}}
    assert _refusal(connection) is None


@pytest.mark.unit
def test_a_switched_off_tunnel_is_not_judged_by_its_field_names():
    tunnel = {"ssh_tunnel": {"enable": False, "host": ELSEWHERE, "hostname": ELSEWHERE}}
    assert _refusal({"host": "localhost", **tunnel}) is None


@pytest.mark.unit
def test_validate_leaves_a_named_host_placeholder_to_apply():
    finding = _lint({"host": "localhost", "instance_type": {"host": "{{ env.MONGO_HOST }}"}})
    assert finding.ok
    assert finding.deferred is not None
    assert "connection.instance_type.host ('{{ env.MONGO_HOST }}')" in finding.deferred.what


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        f"ibm_db_sa://u@{ELSEWHERE}/db",
        f"\x00postgres://{ELSEWHERE}/db",
        f"jdbc:mysql:replication://{ELSEWHERE}/db",
    ],
)
def test_every_uri_spelling_is_read_as_one(uri):
    assert _refusal({"host": "localhost", "cfg": {"u": uri}}) is not None


@pytest.mark.unit
def test_a_uri_under_a_credential_field_is_judged_and_never_quoted():
    secret_uri = f"postgres://u:QUOTED@{ELSEWHERE}/x"  # pragma: allowlist secret
    refusal = _refusal({"host": "localhost", "credentials": {"password": secret_uri}})
    assert refusal is not None
    assert "QUOTED" not in str(refusal)
    assert ELSEWHERE not in str(refusal)


@pytest.mark.unit
def test_a_driver_argument_that_extends_the_connection_string_is_refused():
    arguments = {"Encrypt": f"yes;Address=tcp:{ELSEWHERE},1433"}
    connection = {"host": "localhost", "sqlalchemy_eng_params": {"connect_args": arguments}}
    refusal = _refusal(connection)
    assert refusal is not None
    assert "connection.sqlalchemy_eng_params.connect_args" in refusal.what
    assert ELSEWHERE not in str(refusal)


@pytest.mark.unit
@pytest.mark.parametrize(
    "host",
    [
        f"u:QUOTED@{ELSEWHERE}",  # pragma: allowlist secret
        f"postgres://u:QUOTED@{ELSEWHERE}/db",  # pragma: allowlist secret
    ],
)
def test_a_host_field_written_with_a_password_is_not_quoted(host):
    refusal = _refusal({"host": host})
    assert refusal is not None
    assert "QUOTED" not in str(refusal)
