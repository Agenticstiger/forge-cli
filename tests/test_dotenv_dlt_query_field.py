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

"""A dlt destination's ``query`` field, like every other field of its
credentials a project sets, is one of its endpoints.

dlt's PostgreSQL and Redshift credentials carry a ``query`` map that libpq
reads its own connection parameters from, ``hostaddr`` and ``host`` among them
(https://www.postgresql.org/docs/current/libpq-connect.html). So a project
that sets ``DESTINATION__<NAME>__CREDENTIALS__QUERY`` — the whole map, or a
single key under it — moves the operator's connection to the project's address
while dlt still fills in the operator's password. The field is as much an
endpoint of the destination as its host: while a project's dotenv files set it,
the dlt runner builds the destination's credentials from the project's own
values only, or refuses the destination when the project has none.

The rule is default-deny: every field of a destination's dlt credentials, at
any depth, is such an endpoint, except the in-account fields of
``runner._DLT_IN_ACCOUNT_FIELDS``. The sweep below checks it against every
credentials class dlt 1.31 ships; ``test_dotenv_dlt_default_deny.py`` has the
nested fields, the allow-list and the fail-closed fallback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

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


def _operator_pg_secrets(port: int, host: str, destination: str = "postgres") -> Dict[str, Any]:
    section = f"destination.{destination}.credentials."
    return {
        section + "host": host,
        section + "port": port,
        section + "username": "operator_user",
        section + "password": OPERATOR_KEY,
        section + "database": "operator_db",
    }


# ── the query field is an endpoint of the destination ──────────────────────

# every lookup spelling dlt reads the query under, for the destination and the
# pipeline, as the whole map and as a single key under it, upper and lower case
_QUERY_NAMES = [
    "DESTINATION__POSTGRES__CREDENTIALS__QUERY",
    "destination__postgres__credentials__query",
    "DESTINATION__CREDENTIALS__QUERY",
    "CREDENTIALS__QUERY",
    "ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS__QUERY",
    "DESTINATION__POSTGRES__CREDENTIALS__QUERY__HOSTADDR",
    "DESTINATION__POSTGRES__CREDENTIALS__QUERY__host",
    "CREDENTIALS__QUERY__HOSTADDR",
]


@pytest.mark.parametrize("destination", ["postgres", "redshift"])
@pytest.mark.parametrize("name", _QUERY_NAMES)
def test_a_project_query_field_pins_the_destination(
    project: Path, name: str, destination: str
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    spelled = name.replace("POSTGRES", destination.upper()).replace("postgres", destination)
    (project / ".env").write_text(f'{spelled}={{"hostaddr": "127.0.0.1"}}\n')
    hydrate_dotenv(project)
    assert os.environ.get(spelled) is not None

    found = _project_endpoints_for_destination(destination, _PIPELINE)

    assert spelled in found, found


@pytest.mark.parametrize("destination", ["postgres", "redshift", "snowflake", "mssql"])
def test_a_project_query_field_pins_every_sql_destination(project: Path, destination: str) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    name = f"DESTINATION__{destination.upper()}__CREDENTIALS__QUERY"
    (project / ".env").write_text(f'{name}={{"host": "127.0.0.1"}}\n')
    hydrate_dotenv(project)

    assert name in _project_endpoints_for_destination(destination, _PIPELINE)


def test_a_project_query_field_pins_the_sqlalchemy_destination(project: Path) -> None:
    pytest.importorskip("dlt")
    pytest.importorskip("sqlalchemy")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    name = "DESTINATION__SQLALCHEMY__CREDENTIALS__QUERY"
    (project / ".env").write_text(f'{name}={{"host": "127.0.0.1"}}\n')
    hydrate_dotenv(project)

    assert name in _project_endpoints_for_destination("sqlalchemy", _PIPELINE)


@pytest.mark.parametrize("field", ["TOKEN_URI", "AUTH_URI"])
def test_a_project_oauth_endpoint_pins_the_bigquery_destination(project: Path, field: str) -> None:
    # a GCP service-account OAuth endpoint chooses where the key is exchanged;
    # a project that set it would send the operator's service-account key there
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    # without dlt's bigquery extra its spec cannot be introspected, and the
    # name pins all the same (fail closed)
    name = f"DESTINATION__BIGQUERY__CREDENTIALS__{field}"
    (project / ".env").write_text(f"{name}=https://token.project.test/\n")
    hydrate_dotenv(project)

    assert name in _project_endpoints_for_destination("bigquery", _PIPELINE)


def test_a_query_field_behind_another_pipeline_pins_nothing(project: Path) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    name = "OTHER_INGEST__DESTINATION__POSTGRES__CREDENTIALS__QUERY"
    (project / ".env").write_text(f'{name}={{"hostaddr": "127.0.0.1"}}\n')
    hydrate_dotenv(project)

    assert name not in _project_endpoints_for_destination("postgres", _PIPELINE)


def test_an_operator_query_field_pins_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    # the operator set it themselves: it is theirs, so it pins nothing even
    # when the project file has the same name
    monkeypatch.setenv("DESTINATION__POSTGRES__CREDENTIALS__QUERY", '{"sslmode": "require"}')
    (project / ".env").write_text(
        'DESTINATION__POSTGRES__CREDENTIALS__QUERY={"sslmode": "require"}\n'
    )
    hydrate_dotenv(project)

    assert _project_endpoints_for_destination("postgres", _PIPELINE) == []


def _credentials_leaves(
    cls: Any, path: Tuple[str, ...] = (), seen: Tuple[Any, ...] = ()
) -> Iterator[Tuple[str, ...]]:
    """Every resolvable field under the dlt configuration class ``cls``, as its
    path from ``cls``: nested configurations, and each member of a union of
    them, are walked into (a field typed by a bare configuration with no fields
    of its own is a leaf)."""
    import typing

    from dlt.common.configuration.specs.base_configuration import BaseConfiguration

    for field, hint in cls.get_resolvable_fields().items():
        members, stack = [], [hint]
        while stack:
            current = stack.pop()
            if isinstance(current, type) and issubclass(current, BaseConfiguration):
                members.append(current)
            else:
                stack.extend(typing.get_args(current))
        here = (*path, field)
        nested = [m for m in members if m not in seen and m.get_resolvable_fields()]
        if not nested:
            yield here
        for member in nested:
            yield from _credentials_leaves(member, here, (*seen, member))


def test_every_credentials_field_dlt_ships_pins_or_is_allow_listed() -> None:
    # Default-deny, swept over dlt itself: for every destination dlt 1.31
    # ships, every credentials class it can resolve (each member of a union),
    # and every field at any depth (the ducklake catalog and storage, the lance
    # credentials ...), a project that sets it under the full section path,
    # behind the pipeline name, or under the shortest path dlt reads it under
    # pins the destination — unless it is a top-level field on the allow-list
    # (runner._DLT_IN_ACCOUNT_FIELDS) of that very class, which never pins.
    import typing

    dlt = pytest.importorskip("dlt")
    from dlt.common.configuration.specs.base_configuration import BaseConfiguration

    from fluid_build.build_runners.dlt.runner import (
        _DLT_IN_ACCOUNT_FIELDS,
        _project_endpoint_field_names,
    )

    swept: Dict[str, set] = {}
    allowed_seen = set()
    for kind in sorted(name for name in dir(dlt.destinations) if not name.startswith("_")):
        try:
            spec = getattr(dlt.destinations, kind)().spec()
        except Exception:  # noqa: BLE001 — a module of dlt.destinations, not a factory
            continue
        classes, stack = [spec.credentials_type()], [spec.get_resolvable_fields()["credentials"]]
        while stack:
            current = stack.pop()
            if isinstance(current, type) and issubclass(current, BaseConfiguration):
                classes.append(current)
            else:
                stack.extend(typing.get_args(current))
        for cls in dict.fromkeys(classes):
            allowed = _DLT_IN_ACCOUNT_FIELDS.get(f"{cls.__module__}.{cls.__qualname__}", ())
            for path in _credentials_leaves(cls):
                swept.setdefault(kind, set()).add(path)
                upper = [p.upper() for p in path]
                full = "__".join(["DESTINATION", kind.upper(), "CREDENTIALS", *upper])
                shortest = "__".join(upper[-2:] if len(upper) > 1 else ["CREDENTIALS", *upper])
                names = [full, f"{_PIPELINE.upper()}__{full}", shortest]
                found = _project_endpoint_field_names(kind, _PIPELINE, kind, names)
                if len(path) == 1 and path[0] in allowed:
                    allowed_seen.add((cls.__qualname__, path[0]))
                    assert found == [], (kind, cls.__qualname__, path, found)
                else:
                    assert found == names, (kind, cls.__qualname__, path, found)
    # not vacuous: the destinations FLUID drives and those nesting a configuration
    assert {
        "athena",
        "bigquery",
        "clickhouse",
        "databricks",
        "ducklake",
        "filesystem",
        "lance",
        "mssql",
        "postgres",
        "redshift",
        "snowflake",
        "sqlalchemy",
    } <= set(swept), sorted(swept)
    assert {("catalog", "host"), ("catalog", "query"), ("storage", "bucket_url")} <= swept[
        "ducklake"
    ]
    assert ("storage", "credentials", "endpoint_url") in swept["ducklake"]
    assert ("credentials", "endpoint_url") in swept["lance"]
    assert sum(len(paths) for paths in swept.values()) > 300
    # and every allow-listed field exists on its class
    assert len(allowed_seen) == sum(len(fields) for fields in _DLT_IN_ACCOUNT_FIELDS.values())


# ── real loads: the operator's password never reaches the project's address ─


@pytest.mark.parametrize("parameter", ["hostaddr", "host"])
@pytest.mark.parametrize("where", ["secrets", "env"])
@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_a_project_query_field_never_moves_the_operators_destination(
    tmp_path: Path, destination: str, where: str, parameter: str
) -> None:
    # a real load: dlt, psycopg2 and libpq. The operator's destination is on
    # 127.0.0.2 (nothing listens); only the project's query parameter could
    # bring the connection to the stand-in on 127.0.0.1.
    pytest.importorskip("psycopg2")
    section = destination.upper()
    with FakePostgres() as server:
        secrets = _operator_pg_secrets(server.port, "127.0.0.2", destination)
        env = {key.upper().replace(".", "__"): str(value) for key, value in secrets.items()}
        dotenv = (
            f"DESTINATION__{section}__CREDENTIALS__QUERY="
            + json.dumps({parameter: "127.0.0.1"})
            + "\n"
        )
        builds = dlt_builds_in_one_process(
            tmp_path,
            destination,
            dotenv,
            (),
            operator_env=env if where == "env" else None,
            operator_secrets=secrets if where == "secrets" else None,
            real=True,
        )

    assert server.passwords == [], (builds, server.passwords)
    assert server.connections == 0, builds
    assert builds == [{"rc": 1}], builds


@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_a_projects_own_destination_with_a_query_param_still_reaches_it(
    tmp_path: Path, destination: str
) -> None:
    # positive control, not vacuous: the project brings its whole destination
    # and a query parameter of its own; it still reaches its own address and
    # its own password arrives.
    pytest.importorskip("psycopg2")
    section = destination.upper()
    with FakePostgres() as server:
        dotenv = (
            f'DESTINATION__{section}__CREDENTIALS__QUERY={{"connect_timeout": "5"}}\n'
            f"DESTINATION__{section}__CREDENTIALS__HOST=127.0.0.1\n"
            f"DESTINATION__{section}__CREDENTIALS__PORT={server.port}\n"
            f"DESTINATION__{section}__CREDENTIALS__USERNAME=project_user\n"
            f"DESTINATION__{section}__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
            f"DESTINATION__{section}__CREDENTIALS__DATABASE=project_db\n"
        )
        secrets = _operator_pg_secrets(5432, "pg.operator.test", destination)
        builds = dlt_builds_in_one_process(
            tmp_path, destination, dotenv, (), operator_secrets=secrets, real=True
        )

    assert OPERATOR_KEY not in server.passwords, (builds, server.passwords)
    assert server.passwords and set(server.passwords) == {PROJECT_KEY}, (builds, server.passwords)


@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_the_operators_own_query_still_reaches_the_operators_address(
    tmp_path: Path, destination: str
) -> None:
    # positive control: with no project endpoint, the operator's own
    # destination (query included) reaches the operator's own address.
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        secrets = {
            **_operator_pg_secrets(server.port, "127.0.0.1", destination),
            f"destination.{destination}.credentials.query": {"connect_timeout": "5"},
        }
        builds = dlt_builds_in_one_process(
            tmp_path, destination, None, (), operator_secrets=secrets, real=True
        )

    assert server.passwords and set(server.passwords) == {OPERATOR_KEY}, (builds, server.passwords)
