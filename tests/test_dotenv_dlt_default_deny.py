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

"""A project may set none of a dlt destination's credentials, except a short
allow-list of in-account fields (default-deny).

dlt reads a destination's credentials from the environment under every section
path it tries: ``DESTINATION__<NAME>__CREDENTIALS__<FIELD>``, the shorter
``DESTINATION__CREDENTIALS__`` and ``CREDENTIALS__`` sections, each also behind
the pipeline name. A field of a configuration nested in the credentials (the
ducklake ``catalog`` connection and ``storage`` filesystem, the lance
``credentials``) it reads under each of those paths extended by the nested
configuration's name, and under every shorter one that still ends in that
name (``DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG__HOST``,
``DESTINATION__DUCKLAKE__CATALOG__HOST``, ``CATALOG__HOST``)
(dlt/common/configuration/resolve.py, ``_build_section_lookup_paths``, dlt
1.31.0). dlt layers each value on top of the operator's credentials, so a
project that set one could move or reshape the operator's connection with the
operator's password still on it. So every one of those names a project dotenv
file sets is an endpoint of the destination: the dlt runner builds the
destination's credentials from the project's own values, or fails closed. The
exception is ``runner._DLT_IN_ACCOUNT_FIELDS``, checked here against dlt's own
classes and against libpq. Real dlt is the oracle for which names it reads;
real loads (ducklake's PostgreSQL catalog, through libpq) show the outcome.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Sequence, Tuple

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    FakePostgres,
    dlt_builds_in_one_process,
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
)

_PIPELINE = "acme_ingest"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    refuse_outbound_dns(monkeypatch)
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _pins(kind: str, names: Sequence[str]) -> List[str]:
    """The names in ``names`` that pin the destination ``kind`` in the run's pipeline."""
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoint_field_names

    return _project_endpoint_field_names(kind, _PIPELINE, kind, list(names))


# ── real dlt as the oracle: every name dlt reads a nested field under ──────


def _spellings(chain: Sequence[str], leaf: str) -> List[str]:
    """Every ordered pick of ``chain``'s sections ahead of ``leaf``, plain and
    behind the pipeline name: a superset of the names dlt could look up."""
    out = []
    for size in range(len(chain) + 1):
        for picked in itertools.combinations(chain, size):
            body = "__".join((*picked, leaf))
            out += [body, f"{_PIPELINE.upper()}__{body}"]
    return out


def _dlt_reads(
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    assignments: Mapping[str, str],
    path: Sequence[str],
    expected: Any,
) -> bool:
    """Whether real dlt, resolving ``reference``'s configuration in the run's
    pipeline with ``assignments`` in the environment, ends up with ``expected``
    at ``path`` of it."""
    from dlt.common.configuration.inject import inject_section
    from dlt.common.configuration.specs import ConfigSectionContext
    from dlt.common.destination import Destination

    for name, value in assignments.items():
        monkeypatch.setenv(name, value)
    try:
        destination = Destination.from_reference(reference)
        with inject_section(ConfigSectionContext(pipeline_name=_PIPELINE)):
            config: Any = destination.configuration(
                destination.spec()._bind_dataset_name(dataset_name="x"), accept_partial=True
            )
    finally:
        for name in assignments:
            monkeypatch.delenv(name, raising=False)
    for attribute in path:
        config = getattr(config, attribute, None)
    return bool(config == expected)


_PROJECT_ADDRESS = "203.0.113.9"

# (destination, its sections down to the nested configuration, the field, the
# value the project sets, where dlt puts it, what dlt makes of it, what the
# operator has set that selects the nested configuration's type)
_NESTED: List[Tuple[str, Tuple[str, ...], str, str, Tuple[str, ...], Any, Dict[str, str]]] = [
    (
        "ducklake",
        ("DESTINATION", "DUCKLAKE", "CREDENTIALS", "CATALOG"),
        "HOST",
        _PROJECT_ADDRESS,
        ("credentials", "catalog", "host"),
        _PROJECT_ADDRESS,
        {},
    ),
    (
        "ducklake",
        ("DESTINATION", "DUCKLAKE", "CREDENTIALS", "CATALOG"),
        "PORT",
        "6543",
        ("credentials", "catalog", "port"),
        6543,
        {},
    ),
    (
        "ducklake",
        ("DESTINATION", "DUCKLAKE", "CREDENTIALS", "CATALOG"),
        "QUERY",
        '{"hostaddr": "203.0.113.9"}',
        ("credentials", "catalog", "query"),
        {"hostaddr": _PROJECT_ADDRESS},
        {},
    ),
    (
        "ducklake",
        ("DESTINATION", "DUCKLAKE", "CREDENTIALS", "STORAGE"),
        "BUCKET_URL",
        "s3://project-bucket/lake",
        ("credentials", "storage", "bucket_url"),
        "s3://project-bucket/lake",
        {},
    ),
    (
        "ducklake",
        ("DESTINATION", "DUCKLAKE", "CREDENTIALS", "STORAGE", "CREDENTIALS"),
        "ENDPOINT_URL",
        f"https://{_PROJECT_ADDRESS}",
        ("credentials", "storage", "credentials", "endpoint_url"),
        f"https://{_PROJECT_ADDRESS}",
        {"DESTINATION__DUCKLAKE__CREDENTIALS__STORAGE__BUCKET_URL": "s3://operator-bucket/lake"},
    ),
    (
        "lance",
        ("DESTINATION", "LANCE", "CREDENTIALS", "CREDENTIALS"),
        "ENDPOINT_URL",
        f"https://{_PROJECT_ADDRESS}",
        ("credentials", "credentials", "endpoint_url"),
        f"https://{_PROJECT_ADDRESS}",
        {"DESTINATION__LANCE__CREDENTIALS__BUCKET_URL": "s3://operator-bucket/lance"},
    ),
]


@pytest.mark.parametrize(
    "reference,chain,leaf,value,path,expected,operator",
    _NESTED,
    ids=[f"{n[0]}-{'.'.join(n[4][1:])}" for n in _NESTED],
)
def test_every_name_dlt_reads_a_nested_credentials_field_under_pins_the_destination(
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    chain: Tuple[str, ...],
    leaf: str,
    value: str,
    path: Tuple[str, ...],
    expected: Any,
    operator: Dict[str, str],
) -> None:
    pytest.importorskip("dlt")
    pytest.importorskip("duckdb" if reference == "ducklake" else "lance")
    read = [
        name
        for name in _spellings(chain, leaf)
        if _dlt_reads(monkeypatch, reference, {**operator, name: value}, path, expected)
    ]
    # not vacuous: dlt reads the full path, a shorter one that ends in the
    # nested configuration's name, and both behind the pipeline name
    full = "__".join((*chain, leaf))
    shortest = f"{chain[-1]}__{leaf}"
    assert {full, shortest, f"{_PIPELINE.upper()}__{full}"} <= set(read), read
    for name in read:
        for spelled in (name, name.lower(), name.title(), f"  {name}  "):
            assert _pins(reference, [spelled]) == [spelled], (spelled, read)


@pytest.mark.parametrize(
    "kind,name",
    [
        ("ducklake", "DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG__HOST"),
        ("ducklake", "DESTINATION__DUCKLAKE__CATALOG__HOST"),
        ("ducklake", "CATALOG__HOST"),
        ("ducklake", "ACME_INGEST__DESTINATION__CATALOG__PORT"),
        ("ducklake", "DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG__QUERY"),
        ("ducklake", "DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG"),
        ("ducklake", "STORAGE__BUCKET_URL"),
        ("ducklake", "DESTINATION__DUCKLAKE__CREDENTIALS__STORAGE__CREDENTIALS__ENDPOINT_URL"),
        ("ducklake", "CREDENTIALS__ENDPOINT_URL"),
        ("lance", "DESTINATION__LANCE__CREDENTIALS__CREDENTIALS__ENDPOINT_URL"),
        ("lance", "DESTINATION__LANCE__CREDENTIALS__ENDPOINT_URL"),
    ],
)
def test_a_project_dotenv_nested_credentials_name_pins_the_destination(
    project: Path, kind: str, name: str
) -> None:
    pytest.importorskip("dlt")
    pytest.importorskip("duckdb" if kind == "ducklake" else "lance")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(f"{name}=127.0.0.1\n")
    hydrate_dotenv(project)

    assert name in _project_endpoints_for_destination(kind, _PIPELINE), name


def test_a_query_with_its_paired_host_and_password_lists_all_three(project: Path) -> None:
    # every credentials field the project set is named where the run says why
    # the destination is the project's: its password too, which chooses the
    # credential (the round-16 list named only the address fields)
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(
        'DESTINATION__POSTGRES__CREDENTIALS__QUERY={"hostaddr": "127.0.0.1"}\n'
        "DESTINATION__POSTGRES__CREDENTIALS__HOST=127.0.0.1\n"
        f"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
        "DESTINATION__POSTGRES__CREDENTIALS__CONNECT_TIMEOUT=5\n"
    )
    hydrate_dotenv(project)

    found = _project_endpoints_for_destination("postgres", _PIPELINE)

    assert sorted(found) == [
        "DESTINATION__POSTGRES__CREDENTIALS__HOST",
        "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD",
        "DESTINATION__POSTGRES__CREDENTIALS__QUERY",
    ], found


# ── real loads: ducklake's PostgreSQL catalog connects through libpq ───────


def _ducklake_secrets(host: str, port: int, lake: Path) -> Dict[str, Any]:
    base = "destination.ducklake.credentials."
    return {
        base + "catalog.drivername": "postgres",
        base + "catalog.host": host,
        base + "catalog.port": port,
        base + "catalog.username": "operator_user",
        base + "catalog.password": OPERATOR_KEY,
        base + "catalog.database": "operator_db",
        base + "storage.bucket_url": str(lake),
    }


def _require_a_reachable_ducklake_catalog(tmp_path: Path) -> None:
    """Positive control, so the loads below are not vacuous: the operator's
    own PostgreSQL catalog on the stand-in gets the operator's password."""
    pytest.importorskip("duckdb")
    pytest.importorskip("psycopg2")
    run = tmp_path / "control"
    run.mkdir()
    with FakePostgres() as server:
        builds = dlt_builds_in_one_process(
            run,
            "ducklake",
            None,
            (),
            operator_secrets=_ducklake_secrets("127.0.0.1", server.port, tmp_path / "lake0"),
            real=True,
        )
    if not server.passwords:
        pytest.skip(f"ducklake cannot reach a PostgreSQL catalog in this sandbox: {builds}")
    assert set(server.passwords) == {OPERATOR_KEY}, server.passwords


@pytest.mark.parametrize(
    "section",
    [
        "DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG",
        "DESTINATION__DUCKLAKE__CATALOG",
        "CATALOG",
        "ACME_INGEST__DESTINATION__CATALOG",
    ],
)
def test_realload_a_project_ducklake_catalog_address_never_gets_the_operators_password(
    tmp_path: Path, section: str
) -> None:
    _require_a_reachable_ducklake_catalog(tmp_path)
    run = tmp_path / "project-run"
    run.mkdir()
    with FakePostgres() as server:
        # the operator's catalog is elsewhere (nothing listens on 127.0.0.2);
        # only the project's two lines could bring the connection to the stand-in
        builds = dlt_builds_in_one_process(
            run,
            "ducklake",
            f"{section}__HOST=127.0.0.1\n{section}__PORT={server.port}\n",
            (),
            operator_secrets=_ducklake_secrets("127.0.0.2", 5432, tmp_path / "lake1"),
            pipeline_name=_PIPELINE,
            real=True,
        )
    assert server.passwords == [], (section, builds, server.passwords)
    assert server.connections == 0, (section, builds)
    assert builds == [{"rc": 1}], builds


def test_realload_a_project_ducklake_catalog_query_never_moves_the_operators_catalog(
    tmp_path: Path,
) -> None:
    # libpq takes hostaddr from the catalog URL's query and connects there
    # whatever the host says (https://www.postgresql.org/docs/current/libpq-connect.html)
    _require_a_reachable_ducklake_catalog(tmp_path)
    run = tmp_path / "project-run"
    run.mkdir()
    with FakePostgres() as server:
        builds = dlt_builds_in_one_process(
            run,
            "ducklake",
            'DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG__QUERY={"hostaddr": "127.0.0.1"}\n',
            (),
            operator_secrets=_ducklake_secrets("127.0.0.2", server.port, tmp_path / "lake1"),
            real=True,
        )
    assert server.passwords == [], (builds, server.passwords)
    assert server.connections == 0, builds


# ── fail closed: a spec dlt cannot introspect pins ─────────────────────────

_POSTGRES_NAMES = [
    "DESTINATION__POSTGRES__CREDENTIALS__QUERY",
    "DESTINATION__CREDENTIALS__QUERY",
    "CREDENTIALS__QUERY",
    "ACME_INGEST__CREDENTIALS__QUERY__HOSTADDR",
    "destination__postgres__credentials__password",
    "DESTINATION__POSTGRES__CREDENTIALS__ANY_FIELD_AT_ALL",
    "DESTINATION__POSTGRES__CATALOG__HOST",
]


@pytest.mark.parametrize("failure", ["raises", "no-class", "unlistable"])
def test_a_destination_dlt_cannot_introspect_pins_every_credentials_name(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt import destinations

    class Unlistable:
        @classmethod
        def get_resolvable_fields(cls) -> Dict[str, Any]:
            raise RuntimeError("a later dlt changed its spec")

    def broken(kind: str) -> Any:
        if failure == "raises":
            raise RuntimeError("spec cannot be built")
        return None if failure == "no-class" else Unlistable

    monkeypatch.setattr(destinations, "_credentials_class", broken)

    assert _pins("postgres", _POSTGRES_NAMES) == _POSTGRES_NAMES
    # the destination's sections are still known: another one's stay its own
    assert _pins("postgres", ["DESTINATION__MSSQL__CREDENTIALS__QUERY", "SOURCES__QUERY"]) == []


# ── default-deny: every credentials field off the allow-list pins ──────────

_OFF_THE_LIST = [
    ("postgres", "PASSWORD"),
    ("postgres", "USERNAME"),
    ("postgres", "DRIVERNAME"),
    ("postgres", "QUERY"),
    ("redshift", "PASSWORD"),
    ("redshift", "USERNAME"),
    ("snowflake", "AUTHENTICATOR"),
    ("snowflake", "APPLICATION"),
    ("snowflake", "PRIVATE_KEY_PATH"),
    ("snowflake", "WORKLOAD_IDENTITY_PROVIDER"),
    ("snowflake", "SESSION_TIMEZONE"),
    ("snowflake", "USERNAME"),
    ("bigquery", "PROJECT_ID"),
    ("bigquery", "CLIENT_EMAIL"),
    # a value of an ODBC connection string: Fabric joins it in unescaped
    # (MSSQL / Synapse escape it); not checked, so not allowed
    ("mssql", "DATABASE"),
    ("synapse", "DATABASE"),
    ("fabric", "DATABASE"),
    ("clickhouse", "DATABASE"),
    ("clickhouse", "SECURE"),
    # a file path, or MotherDuck's md: URL
    ("duckdb", "DATABASE"),
    ("motherduck", "DATABASE"),
    ("sqlalchemy", "DATABASE"),
    ("databricks", "CATALOG"),
    ("weaviate", "GRPC_PORT"),
    ("athena", "PROFILE_NAME"),
]


@pytest.mark.parametrize("kind,field", _OFF_THE_LIST)
def test_a_project_credentials_field_off_the_allow_list_pins_the_destination(
    kind: str, field: str
) -> None:
    names = [
        f"DESTINATION__{kind.upper()}__CREDENTIALS__{field}",
        f"DESTINATION__CREDENTIALS__{field}",
        f"CREDENTIALS__{field}",
        f"{_PIPELINE.upper()}__CREDENTIALS__{field}",
    ]
    assert _pins(kind, names) == names


def test_a_project_password_alone_now_pins_and_fails_closed(tmp_path: Path) -> None:
    # it chooses the credential: the destination's credentials then come from
    # the project only, which names no host, so the build fails and says so
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        secrets = {
            "destination.postgres.credentials.host": "127.0.0.1",
            "destination.postgres.credentials.port": server.port,
            "destination.postgres.credentials.username": "operator_user",
            "destination.postgres.credentials.password": OPERATOR_KEY,
            "destination.postgres.credentials.database": "operator_db",
        }
        (build,) = dlt_builds_in_one_process(
            tmp_path,
            "postgres",
            f"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD={PROJECT_KEY}\n",
            ("host", "password"),
            operator_secrets=secrets,
        )
    err = build.get("failed", "")
    assert "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD" in err and "host" in err, build
    assert OPERATOR_KEY not in str(build)


# ── the allow-list: exactly these, each checked against dlt and libpq ──────

_IN_ACCOUNT = {
    "dlt.destinations.impl.postgres.configuration.PostgresCredentials": {
        "database",
        "connect_timeout",
        "client_encoding",
        "session_timezone",
    },
    "dlt.destinations.impl.redshift.configuration.RedshiftCredentials": {
        "database",
        "connect_timeout",
        "client_encoding",
        "session_timezone",
    },
    "dlt.destinations.impl.snowflake.configuration.SnowflakeCredentials": {
        "database",
        "warehouse",
        "role",
    },
}


def test_the_allow_list_is_exactly_the_documented_one() -> None:
    from fluid_build.build_runners.dlt.runner import _DLT_IN_ACCOUNT_FIELDS

    assert {cls: set(fields) for cls, fields in _DLT_IN_ACCOUNT_FIELDS.items()} == _IN_ACCOUNT


def test_every_allowed_field_is_a_plain_field_of_its_dlt_class() -> None:
    pytest.importorskip("dlt")
    import importlib
    import typing

    from dlt.common.configuration.specs.base_configuration import BaseConfiguration

    for qualified, fields in _IN_ACCOUNT.items():
        module, _, name = qualified.rpartition(".")
        hints = getattr(importlib.import_module(module), name).get_resolvable_fields()
        for field in fields:
            assert field in hints, (qualified, field)
            # a value, never a nested configuration or a map of driver parameters
            stack, seen = [hints[field]], []
            while stack:
                hint = stack.pop()
                seen.append(hint)
                stack.extend(typing.get_args(hint))
            assert not any(isinstance(h, type) and issubclass(h, BaseConfiguration) for h in seen)
            assert not any(typing.get_origin(h) in (dict, typing.Dict) for h in seen), field


@pytest.mark.parametrize(
    "kind,field",
    [
        (kind, field.upper())
        for kind, qualified in (
            ("postgres", "dlt.destinations.impl.postgres.configuration.PostgresCredentials"),
            ("redshift", "dlt.destinations.impl.redshift.configuration.RedshiftCredentials"),
            ("snowflake", "dlt.destinations.impl.snowflake.configuration.SnowflakeCredentials"),
        )
        for field in sorted(_IN_ACCOUNT[qualified])
    ],
)
def test_an_in_account_field_alone_leaves_the_operators_destination(kind: str, field: str) -> None:
    names = [
        f"DESTINATION__{kind.upper()}__CREDENTIALS__{field}",
        f"DESTINATION__CREDENTIALS__{field}",
        f"credentials__{field.lower()}",
        f"{_PIPELINE.upper()}__CREDENTIALS__{field}",
    ]
    assert _pins(kind, names) == []
    # a name below it is not one dlt reads, and an allowed name never shields
    # another field's
    assert _pins(kind, [f"DESTINATION__{kind.upper()}__CREDENTIALS__QUERY"]) == [
        f"DESTINATION__{kind.upper()}__CREDENTIALS__QUERY"
    ]


# values a project could try to splice into the connection
_PAYLOADS = [
    "x?hostaddr=127.0.0.9&application_name=",
    "x?hostaddr=127.0.0.9&",
    "x&hostaddr=127.0.0.9",
    "x hostaddr=127.0.0.9",
    "x' hostaddr='127.0.0.9",
    "x#frag",
    "x@127.0.0.9:1/y",
    "x%3Fhostaddr%3D127.0.0.9",
    "UTC hostaddr=127.0.0.9 -chost=127.0.0.9",
    "x/../y?service=project",
]
_LIBPQ_ROUTE = (
    "host",
    "hostaddr",
    "port",
    "user",
    "password",
    "passfile",
    "service",
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "gssencmode",
    "requirepeer",
    "krbsrvname",
    "target_session_attrs",
)


@pytest.mark.parametrize("kind", ["postgres", "redshift"])
@pytest.mark.parametrize("field", sorted(_IN_ACCOUNT[next(iter(_IN_ACCOUNT))]))
def test_an_in_account_libpq_field_cannot_move_the_connection(kind: str, field: str) -> None:
    # What dlt 1.31's Psycopg2SqlClient.open_connection hands libpq: the
    # credentials' URL (dlt renders it through SQLAlchemy's URL when that is
    # installed, else its own shim, which does not quote the database) and the
    # options it adds; parsed by libpq itself (psycopg2's parse_dsn calls
    # PQconninfoParse). A value either stays in its own parameter or libpq
    # refuses the string outright: it never sets where or as whom libpq connects.
    pytest.importorskip("dlt")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import make_dsn, parse_dsn

    if kind == "postgres":
        from dlt.destinations.impl.postgres.configuration import (
            PostgresCredentials as Credentials,
        )
    else:
        from dlt.destinations.impl.redshift.configuration import (  # type: ignore[assignment]
            RedshiftCredentials as Credentials,
        )

    def libpq(value: Any) -> Dict[str, str]:
        credentials = Credentials()
        credentials.host = "pg.operator.test"
        credentials.port = 5432
        credentials.username = "operator_user"
        credentials.password = OPERATOR_KEY
        credentials.database = "operator_db"
        if value is not None:
            setattr(credentials, field, value)
        options = credentials.get_query().get("options", "")
        dsn = make_dsn(
            credentials.to_native_representation(),
            options=f"{options} -csearch_path=dataset,public",
        )
        return dict(parse_dsn(dsn))

    operator = libpq(None)
    assert operator["password"] == OPERATOR_KEY and operator["host"] == "pg.operator.test"
    for payload in _PAYLOADS:
        try:
            theirs = libpq(payload)
        except psycopg2.ProgrammingError:
            continue  # libpq refuses the string: no connection at all
        moved = {k: theirs.get(k) for k in _LIBPQ_ROUTE if theirs.get(k) != operator.get(k)}
        assert moved == {}, (field, payload, moved)


@pytest.mark.parametrize("field", ["database", "warehouse", "role"])
def test_an_in_account_snowflake_field_stays_its_own_connector_parameter(field: str) -> None:
    # dlt hands the Snowflake connector keyword arguments
    # (SnowflakeCredentials.to_connector_params): the account, host, user and
    # authentication stay the operator's whatever the field says
    pytest.importorskip("dlt")
    from dlt.destinations.impl.snowflake.configuration import SnowflakeCredentials

    def params(value: Any) -> Dict[str, Any]:
        credentials = SnowflakeCredentials()
        credentials.host = "operator-account"
        credentials.username = "operator_user"
        credentials.password = OPERATOR_KEY
        credentials.database = "OPERATOR_DB"
        credentials.warehouse = "OPERATOR_WH"
        credentials.role = "OPERATOR_ROLE"
        if value is not None:
            setattr(credentials, field, value)
        return dict(credentials.to_connector_params())

    operator = params(None)
    for payload in [*_PAYLOADS, "project.snowflakecomputing.com", "https://203.0.113.9"]:
        theirs = params(payload)
        assert set(theirs) == set(operator), (field, payload)
        assert {k for k in theirs if theirs[k] != operator[k]} == {field}, (field, payload)


def test_realload_a_projects_in_account_fields_keep_the_operators_destination(
    tmp_path: Path,
) -> None:
    # the legitimate setup the allow-list keeps: a project chooses the database
    # and session settings on the operator's own server
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        secrets = {
            "destination.postgres.credentials.host": "127.0.0.1",
            "destination.postgres.credentials.port": server.port,
            "destination.postgres.credentials.username": "operator_user",
            "destination.postgres.credentials.password": OPERATOR_KEY,
            "destination.postgres.credentials.database": "operator_db",
        }
        builds = dlt_builds_in_one_process(
            tmp_path,
            "postgres",
            "DESTINATION__POSTGRES__CREDENTIALS__DATABASE=project_db\n"
            "DESTINATION__POSTGRES__CREDENTIALS__CONNECT_TIMEOUT=7\n"
            "DESTINATION__POSTGRES__CREDENTIALS__CLIENT_ENCODING=UTF8\n"
            "DESTINATION__POSTGRES__CREDENTIALS__SESSION_TIMEZONE=UTC\n",
            (),
            operator_secrets=secrets,
            real=True,
        )
    assert server.passwords and set(server.passwords) == {OPERATOR_KEY}, (builds, server.passwords)
