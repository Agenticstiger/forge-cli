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

"""libpq credential sources a project dotenv file must not reach.

* An operator ``~/.pgpass`` entry whose host field is ``*`` gives its password
  to any host, so while one exists a project PostgreSQL endpoint pairs only
  with the password its readers send ahead of that file.
* ``PGSERVICE`` / ``PGSERVICEFILE`` / ``PGSYSCONFDIR`` pick or supply libpq
  service definitions, which can carry a host, a password and a passfile.
* ``PGHOSTADDR`` makes libpq connect to that address whatever host it was
  given, with the password meant for that host.
* botocore's container, instance-metadata and web-identity providers fetch or
  exchange the operator's credentials at the locations these variables name.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from fluid_build.credentials.protected_env import is_protected_env_key, unpaired_endpoints
from tests._dotenv_pairing_support import (
    OPERATOR_KEY,
    PROJECT_KEY,
    FakePostgres,
    drop_new_names,
    hermetic_env,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="libpq's permission check is POSIX-only")


def _pgpass(home: Path, *lines: str, mode: int = 0o600) -> Path:
    path = home / ".pgpass"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


ANY_HOST = "*:*:*:analyst:OPERATOR-FAKE-PGPASS"  # pragma: allowlist secret
ONE_HOST = "db.internal.test:5432:*:analyst:OPERATOR-FAKE-PGPASS"  # pragma: allowlist secret


class TestPgpassAnyHostEntry:
    # PGHOSTADDR is refused outright (TestPghostaddrIsOperatorOnly)
    @pytest.mark.parametrize("endpoint", ["PGHOST", "PGPORT", "POSTGRES_HOST", "PG_HOST"])
    def test_an_endpoint_alone_is_refused(self, tmp_path, endpoint):
        path = _pgpass(tmp_path, ANY_HOST)
        refused = unpaired_endpoints({endpoint: "pg.project.test"}, {"HOME": str(tmp_path)})
        assert endpoint in refused
        assert any(str(path) in c for c in refused[endpoint].operator_credentials)

    @pytest.mark.parametrize(
        "endpoint,password",
        [
            ("PGHOST", "PGPASSWORD"),
            ("POSTGRES_HOST", "POSTGRES_PASSWORD"),
            ("PG_HOST", "PG_PASSWORD"),
            ("PG_HOST", "POSTGRES_PASSWORD"),
        ],
    )
    def test_the_password_its_readers_send_pairs_it(self, tmp_path, endpoint, password):
        _pgpass(tmp_path, ANY_HOST)
        project = {endpoint: "pg.project.test", password: "project-own"}  # pragma: allowlist secret
        assert endpoint not in unpaired_endpoints(project, {"HOME": str(tmp_path)})

    def test_a_password_its_readers_do_not_send_does_not_pair_it(self, tmp_path):
        # The output-port driver and libpq read POSTGRES_PASSWORD and
        # PGPASSWORD respectively; the other one leaves libpq on ~/.pgpass.
        _pgpass(tmp_path, ANY_HOST)
        project = {
            "PGHOST": "pg.project.test",
            "POSTGRES_PASSWORD": "x",
        }  # pragma: allowlist secret
        assert "PGHOST" in unpaired_endpoints(project, {"HOME": str(tmp_path)})

    def test_entries_for_named_hosts_only_are_not_sent_elsewhere(self, tmp_path):
        _pgpass(tmp_path, ONE_HOST, "# *:*:*:analyst:commented-out")
        assert unpaired_endpoints({"PGHOST": "pg.project.test"}, {"HOME": str(tmp_path)}) == {}

    def test_pgpassfile_names_the_file(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        path = _pgpass(other, ANY_HOST)
        operator = {"HOME": str(tmp_path), "PGPASSFILE": str(path)}
        assert "PGHOST" in unpaired_endpoints({"PGHOST": "pg.project.test"}, operator)

    @posix_only
    def test_a_file_libpq_ignores_for_its_permissions_is_ignored(self, tmp_path):
        _pgpass(tmp_path, ANY_HOST, mode=0o644)
        assert unpaired_endpoints({"PGHOST": "pg.project.test"}, {"HOME": str(tmp_path)}) == {}

    def test_no_file_no_refusal(self, tmp_path):
        assert unpaired_endpoints({"PGHOST": "pg.project.test"}, {"HOME": str(tmp_path)}) == {}

    def test_the_dotenv_store_drops_the_endpoint(self, tmp_path, monkeypatch):
        from fluid_build.credentials.dotenv_store import DotEnvCredentialStore

        home = tmp_path / "home"
        home.mkdir()
        _pgpass(home, ANY_HOST)
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text("PGHOST=pg.project.test\n", encoding="utf-8")
        for name in ("PGHOST", "PGPASSWORD", "PGPASSFILE", "POSTGRES_PASSWORD", "PG_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(home))
        DotEnvCredentialStore(project_root=project).load()
        assert "PGHOST" not in os.environ


@pytest.mark.parametrize(
    "name",
    [
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGHOSTADDR",
        "pghostaddr",
        "PgHostAddr",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "pgservice",
        "aws_role_arn",
    ],
)
def test_operator_only(name):
    assert is_protected_env_key(name)


@pytest.mark.parametrize(
    "name", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_EC2_METADATA_DISABLED"]
)
def test_still_loadable(name):
    # AWS_PROFILE picks among the operator's own profiles for AWS's own
    # endpoints (which a project cannot move), and the metadata switch can
    # only turn a credential source off.
    assert not is_protected_env_key(name)


def test_botocore_container_provider_reads_the_protected_names():
    botocore = pytest.importorskip("botocore.credentials")
    names = {
        botocore.ContainerProvider.ENV_VAR,
        botocore.ContainerProvider.ENV_VAR_FULL,
        botocore.ContainerProvider.ENV_VAR_AUTH_TOKEN,
    }
    assert all(is_protected_env_key(n) for n in names), names


# ── PGHOSTADDR ────────────────────────────────────────────────────────────
#
# libpq connects to ``hostaddr`` whatever ``host`` says, and still uses
# ``host`` for everything else: the passfile entry it looks up and the
# password the caller passed for that host
# (https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT-HOSTADDR,
# https://www.postgresql.org/docs/current/libpq-pgpass.html). It takes
# ``hostaddr`` from PGHOSTADDR wherever the connection string leaves it out,
# which nearly every caller does: dlt's postgres, redshift, sqlalchemy and
# ducklake destinations, dbt-postgres with ~/.dbt/profiles.yml, psycopg itself.
# So a project's PGHOSTADDR would send the operator's password to the project's
# address from any of them, and no project file may set it.

_PROJECT_ADDRESS = "127.0.0.1"  # where the stand-in listens
_OPERATOR_ADDRESS = "127.0.0.2"  # the operator's own server: nothing listens there
_OPERATOR_PGPASS = "OPERATOR-FAKE-PGPASS"  # pragma: allowlist secret
_PASSWORD_SOURCES = [
    "in the connection",
    # ~/.pgpass is where libpq looks on POSIX (%APPDATA% on Windows)
    pytest.param("pgpass entry", marks=posix_only),
]


class TestPghostaddrIsOperatorOnly:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
        from fluid_build.credentials.dotenv_store import reset_dotenv_load_log

        before = hermetic_env(monkeypatch, tmp_path / "home")
        reset_dotenv_load_log()
        yield
        drop_new_names(before)
        reset_dotenv_load_log()

    @staticmethod
    def _project(tmp_path: Path, filename: str, text: str) -> Path:
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        (project / filename).write_text(text, encoding="utf-8")
        return project

    @pytest.mark.parametrize("spelling", ["PGHOSTADDR", "pghostaddr", "PgHostAddr"])
    @pytest.mark.parametrize("filename", [".env", ".env.local"])
    def test_a_project_file_cannot_set_it(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, spelling: str, filename: str
    ) -> None:
        from fluid_build.cli._common import hydrate_dotenv

        project = self._project(
            tmp_path, filename, f"{spelling}={_PROJECT_ADDRESS}\nPGPASSWORD={PROJECT_KEY}\n"
        )

        with caplog.at_level(logging.WARNING):
            hydrate_dotenv(project)

        assert not [k for k in os.environ if k.upper() == "PGHOSTADDR"], dict(os.environ)
        # the project's own credential still loads: that is what the file is for
        assert os.environ.get("PGPASSWORD") == PROJECT_KEY
        (warning,) = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and r.getMessage().startswith("Ignored ")
        ]
        assert f"{spelling} ({filename})" in warning
        assert _PROJECT_ADDRESS not in warning
        assert "export" in warning

    @pytest.mark.parametrize(
        "text",
        [
            "export PGHOSTADDR=10.0.0.1\n",
            'PGHOSTADDR="10.0.0.1"\n',
            "PGHOSTADDR='10.0.0.1'\n",
            "PGHOSTADDR=10.0.0.1\r\n",
            "PGHOSTADDR=::1\n",
            "# header\n\nPGHOSTADDR=10.0.0.1  # a comment\n",
            "PGHOSTADDR=10.0.0.1\nPGHOSTADDR=10.0.0.2\n",
            "PGHOSTADDR = 10.0.0.1\n",
            "PGHOST=pg.project.test\nPGPORT=5433\nPGHOSTADDR=10.0.0.1\nPGUSER=u\n",
        ],
        ids=[
            "export",
            "double-quoted",
            "single-quoted",
            "crlf",
            "ipv6",
            "comments",
            "duplicate",
            "spaces",
            "with-other-pg-names",
        ],
    )
    def test_no_way_of_writing_it_gets_it_loaded(self, tmp_path: Path, text: str) -> None:
        from fluid_build.cli._common import hydrate_dotenv

        hydrate_dotenv(self._project(tmp_path, ".env", text))

        assert "PGHOSTADDR" not in os.environ

    def test_the_operator_s_own_value_stays_and_is_not_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fluid_build.cli._common import hydrate_dotenv

        monkeypatch.setenv("PGHOSTADDR", "10.1.2.3")
        project = self._project(tmp_path, ".env", f"PGHOSTADDR={_PROJECT_ADDRESS}\n")

        with caplog.at_level(logging.WARNING):
            hydrate_dotenv(project)

        assert os.environ["PGHOSTADDR"] == "10.1.2.3"
        assert not [r for r in caplog.records if r.getMessage().startswith("Ignored ")]

    @staticmethod
    def _operator_connection(port: int, host: str, source: str) -> Dict[str, Any]:
        """How the operator's own tools connect: libpq given the operator's host
        and the password meant for it, passed in the connection (a dbt profile)
        or found in the entry for that host in the operator's ``~/.pgpass``
        (which libpq finds through ``HOME``, the test's own)."""
        kwargs: Dict[str, Any] = {
            "host": host,
            "port": port,
            "user": "analyst",
            "dbname": "operator_db",
            "connect_timeout": 3,
        }
        if source == "in the connection":
            kwargs["password"] = OPERATOR_KEY
        else:
            _pgpass(Path(os.environ["HOME"]), f"{host}:{port}:*:analyst:{_OPERATOR_PGPASS}")
        return kwargs

    @staticmethod
    def _connect(kwargs: Dict[str, Any]) -> None:
        psycopg2 = pytest.importorskip("psycopg2")
        with pytest.raises(psycopg2.OperationalError):  # the stand-in refuses every login
            psycopg2.connect(**kwargs)

    @pytest.mark.parametrize("source", _PASSWORD_SOURCES)
    def test_libpq_never_takes_the_operator_s_password_to_a_project_address(
        self, tmp_path: Path, source: str
    ) -> None:
        pytest.importorskip("psycopg2")
        from fluid_build.cli._common import hydrate_dotenv

        project = self._project(tmp_path, ".env", f"PGHOSTADDR={_PROJECT_ADDRESS}\n")
        with FakePostgres() as server:
            connection = self._operator_connection(server.port, _OPERATOR_ADDRESS, source)
            hydrate_dotenv(project)
            self._connect(connection)

        assert server.passwords == [], server.passwords
        assert server.connections == 0

    @pytest.mark.parametrize("source", _PASSWORD_SOURCES)
    def test_control_the_operator_s_own_server_gets_that_password(self, source: str) -> None:
        # not vacuous: at the stand-in's own address libpq does send it
        pytest.importorskip("psycopg2")
        with FakePostgres() as server:
            self._connect(self._operator_connection(server.port, _PROJECT_ADDRESS, source))

        expected = OPERATOR_KEY if source == "in the connection" else _OPERATOR_PGPASS
        assert server.passwords == [expected], server.passwords
