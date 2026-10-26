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

"""A contract's source host never gets this machine's ambient DB credentials.

A contract names the host its postgres or mysql source lives on. libpq and
mysql_scanner then read credentials from the PROCESS environment for whatever
host they are handed, so a contract naming a host of its own choosing could be
authenticated with this machine's ``PGPASSWORD`` -- a credential it was never
given, belonging to a different server.

This is a SECOND route, independent of the contract's own ``connection.secretRef``:
no allowlist over ``secretRef`` closes it, because the credential never passes
through ``secretRef`` at all. ``fluid init --discover`` has suppressed these
since its JDBC introspection landed; the acquisition runners had not.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

from fluid_build.credentials.host_db_env import (
    HOST_CREDENTIAL_ENV,
    host_credential_env_suppressed,
)

#: A value no real credential store would hand out, so a leak is unmistakable.
SENTINEL = "SENTINEL-HOST-PASSWORD-DO-NOT-SEND"  # pragma: allowlist secret

#: What the CONTRACT declared, as opposed to what the host environment holds.
#: Named once so neither the fixture nor the assertion spells a literal that
#: reads as a credential to the secret scanner.
CONTRACT_PASSWORD = "from-the-contract"  # pragma: allowlist secret


@pytest.mark.parametrize("name", ["PGPASSWORD", "PGPASSFILE", "PGSERVICE", "MYSQL_PWD"])
def test_the_variables_libpq_and_mysql_read_are_covered(name: str) -> None:
    """The set is the contract: a variable missing from it is a live hole."""
    assert name in HOST_CREDENTIAL_ENV, f"{name} is not suppressed"


def test_the_window_hides_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", SENTINEL)
    monkeypatch.setenv("MYSQL_PWD", SENTINEL)
    with host_credential_env_suppressed():
        assert "PGPASSWORD" not in os.environ
        assert "MYSQL_PWD" not in os.environ
    assert os.environ["PGPASSWORD"] == SENTINEL
    assert os.environ["MYSQL_PWD"] == SENTINEL


def test_the_window_restores_when_the_attach_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed attach must not leave the operator's environment stripped."""
    monkeypatch.setenv("PGPASSWORD", SENTINEL)
    # A flag rather than `else:` or a nested `pytest.raises`. Both of those read
    # to CodeQL as unreachable code, because its analysis does not model a
    # context manager's __exit__ returning true -- which is precisely the
    # failure this checks for: a window that SWALLOWED the exception would
    # otherwise leave the test passing with nothing asserted.
    propagated = False
    try:
        with host_credential_env_suppressed():
            raise RuntimeError("attach failed")
    except RuntimeError as exc:
        propagated = True
        assert "attach failed" in str(exc)
    assert propagated, "the window swallowed the exception instead of re-raising"
    assert os.environ["PGPASSWORD"] == SENTINEL


def test_an_unset_variable_is_not_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    with host_credential_env_suppressed():
        pass
    assert "PGPASSWORD" not in os.environ


# ── the runner's attach, which is what this change is for ────────────────


def test_the_duckdb_runner_attaches_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test. Reverting the `with` in the runner fails this.

    Observes the environment from inside the attach itself, which is the only
    place that proves the window actually wraps the call that reaches libpq.
    """
    from fluid_build.build_runners.duckdb import runner as duckdb_runner

    monkeypatch.setenv("PGPASSWORD", SENTINEL)
    seen: Dict[str, Any] = {}

    def spy(_con: Any, statement: str, password: Any) -> None:
        seen["pgpassword_visible_during_attach"] = os.environ.get("PGPASSWORD")
        seen["statement"] = statement
        seen["password"] = password

    monkeypatch.setattr(duckdb_runner, "_attach_with_password_masked", spy)

    class _Conn:
        raw = {
            "host": "db.example.com",
            "port": 5432,
            "user": "alice",
            "database": "sales",
            "password": CONTRACT_PASSWORD,
        }

    class _Source:
        kind = "postgres"
        connection = _Conn()

    class _Ctx:
        build_id = "ingest"
        source = _Source()

    class _Con:
        def execute(self, _sql: str) -> None:
            return None

    runner = duckdb_runner.DuckdbRunner()
    runner._attach_external_databases(_Con(), _Ctx())

    assert (
        seen["pgpassword_visible_during_attach"] is None
    ), "the runner attached with this host's PGPASSWORD still in the environment"
    # Restored afterwards, and the contract's own credential was untouched.
    assert os.environ["PGPASSWORD"] == SENTINEL
    assert seen["password"] == CONTRACT_PASSWORD


def test_a_sqlite_source_needs_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the libpq/mysql_scanner paths read these; sqlite takes a path.

    Pins that the change did not quietly widen to the sqlite branch, whose
    confinement is a different control.
    """
    from fluid_build.build_runners.duckdb import runner as duckdb_runner

    calls: List[str] = []

    class _Con:
        def execute(self, sql: str) -> None:
            calls.append(sql)

    class _Conn:
        raw: Dict[str, Any] = {"uri": "/nonexistent/does-not-exist.sqlite"}

    class _Source:
        kind = "sqlite"
        connection = _Conn()

    class _Ctx:
        build_id = "ingest"
        source = _Source()
        contract: Dict[str, Any] = {}
        workdir = "."

    runner = duckdb_runner.DuckdbRunner()
    # The path is confined before any attach, so an outside path is refused
    # here; what matters is that it does not reach the credential window.
    with pytest.raises(Exception):
        runner._attach_external_databases(_Con(), _Ctx())
    assert not any("ATTACH" in c for c in calls), calls
