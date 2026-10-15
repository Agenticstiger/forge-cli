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

"""Only a connection an MCP client sent is pinned away from this host's TLS identity.

A postgres ATTACH whose URI carries a password is pinned to that password:
``passfile``, ``sslcert`` and ``sslkey`` name paths that cannot exist, and
``PGSSLCERT`` / ``PGSSLKEY`` / ``PGSERVICE`` / ... are hidden while it connects.
The pin is there so that an MCP client cannot make this host present its client
certificate, its libpq service or its password file to a server the client
chose. Applied to every caller, it also broke the operator's own
``fluid init --discover`` and ``fluid forge data-model from-source`` against a
server that requires a client certificate (``clientcert=verify-ca``).

``operator_supplied`` is the explicit, keyword-only, fail-closed switch: it
defaults to False (pinned), and only the operator's command-line entry points
pass True. Nothing an MCP client sends can set it.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
from typing import Any, Dict, List, Optional

import pytest

FAKE_PW = "operator-fake-pw-123"  # pragma: allowlist secret
PG_URI = f"postgresql://fluid:{FAKE_PW}@db.test:5432/app"

# This host's own libpq TLS / service settings (fake values).
_HOST_LIBPQ_ENV = {
    "PGSSLCERT": "/fake/host/postgresql.crt",
    "PGSSLKEY": "/fake/host/postgresql.key",
    "PGSERVICE": "fake-host-service",
}
# And the mysql client's (fake value).
_HOST_MYSQL_PWD = "fake-host-mysql-pw"  # pragma: allowlist secret

_PIN_KEYS = ("passfile=", "sslcert=", "sslkey=")


class _Recorder:
    def __init__(self) -> None:
        self.attaches: List[str] = []
        self.env_at_attach: List[Dict[str, Optional[str]]] = []
        self.mysql_pwd_at_attach: List[Optional[str]] = []

    @property
    def only(self) -> str:
        assert len(self.attaches) == 1, self.attaches
        return self.attaches[0]


@pytest.fixture
def recorded(monkeypatch) -> _Recorder:
    """Stand in for the duckdb sandbox: record each ATTACH and the libpq variables
    visible while it runs, then report an empty source (no tables, no rows)."""
    pytest.importorskip("duckdb")
    import fluid_build.providers._duckdb_sandbox as sandbox

    rec = _Recorder()

    class _Result:
        description: List[Any] = []

        def fetchall(self) -> List[Any]:
            return []

    class _Con:
        def execute(self, sql: Any, *_a: Any, **_k: Any) -> _Result:
            if isinstance(sql, str) and sql.lstrip().upper().startswith("ATTACH"):
                rec.attaches.append(sql)
                rec.env_at_attach.append({k: os.environ.get(k) for k in _HOST_LIBPQ_ENV})
                rec.mysql_pwd_at_attach.append(os.environ.get("MYSQL_PWD"))
            return _Result()

        def close(self) -> None:
            pass

    def fake_connect(*_a: Any, before_lock: Any = None, **_k: Any) -> _Con:
        con = _Con()
        if before_lock is not None:
            before_lock(con)
        return con

    monkeypatch.setattr(sandbox, "secure_duckdb_connect", fake_connect)
    for key, value in _HOST_LIBPQ_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MYSQL_PWD", _HOST_MYSQL_PWD)
    return rec


def _is_pinned(attach_sql: str) -> bool:
    found = [key in attach_sql for key in _PIN_KEYS]
    assert all(found) or not any(found), attach_sql
    return all(found)


class TestTheSwitch:
    """Each layer takes the switch as a keyword-only argument that defaults to the pin."""

    @pytest.mark.parametrize(
        "dotted",
        [
            "fluid_build.cli.discover._jdbc_introspect:build_attach_dsn",
            "fluid_build.cli.discover._jdbc_introspect:introspect_jdbc",
            "fluid_build.cli.discover._jdbc_introspect:_attach_string_postgres",
            "fluid_build.cli._forge_data_model_jdbc:_run_from_jdbc_source",
            "fluid_build.cli.discover._jdbc_base:JdbcDiscoverer.discover",
        ],
    )
    def test_keyword_only_and_pinned_by_default(self, dotted):
        import importlib

        module_name, _, attr_path = dotted.partition(":")
        obj: Any = importlib.import_module(module_name)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
        param = inspect.signature(obj).parameters.get("operator_supplied")
        assert param is not None, f"{dotted} takes no operator_supplied"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is False

    def test_default_pins_a_password_connection(self):
        from fluid_build.cli.discover._jdbc_introspect import build_attach_dsn

        conn = {"host": "db.test", "port": 5432, "user": "fluid", "password": FAKE_PW}
        conn["database"] = "app"
        dsn = build_attach_dsn(conn, database_key="dbname", postgres=True)
        assert all(key in dsn for key in _PIN_KEYS)

    def test_operator_supplied_is_the_plain_libpq_dsn(self):
        from fluid_build.cli.discover._jdbc_introspect import build_attach_dsn
        from fluid_build.providers._sql_safety import build_libpq_dsn

        conn = {"host": "db.test", "port": 5432, "user": "fluid", "password": FAKE_PW}
        conn["database"] = "app"
        dsn = build_attach_dsn(conn, database_key="dbname", postgres=True, operator_supplied=True)
        # Quoted exactly as before, with nothing that overrides libpq's own
        # client-certificate, password-file or service lookup.
        assert dsn == build_libpq_dsn(conn, database_key="dbname")
        assert not any(key in dsn for key in _PIN_KEYS)


class TestOperatorCommandLine:
    """What the operator types may use this host's client certificate and libpq settings."""

    def test_init_discover(self, recorded, tmp_path):
        from fluid_build.cli import main

        main(["init", "--discover", PG_URI, "--dir", str(tmp_path / "out"), "--yes"])
        assert not _is_pinned(recorded.only)

    def test_init_discover_keeps_the_one_argument_discoverer_interface(self, monkeypatch, tmp_path):
        # Only the JDBC discoverer has a pin to turn off. Any other discoverer
        # (the filesystem one, a plugin, a test stub) is still called as
        # discover(uri), as before.
        import fluid_build.cli._init_discover_handler as handler
        from fluid_build.cli.discover.registry import DiscoveredColumn, DiscoveredStream

        seen: List[str] = []

        class _OneArgument:
            def discover(self, uri: str) -> List[DiscoveredStream]:
                seen.append(uri)
                columns = [DiscoveredColumn(name="id", type="bigint", nullable=False)]
                return [DiscoveredStream(name="orders", columns=columns)]

        uri = "https://files.example.test/orders.csv"
        monkeypatch.setattr(handler, "get_discoverer", lambda _uri: _OneArgument())
        args = argparse.Namespace(target_dir=str(tmp_path), name=None)
        assert handler.run_discover(args, logging.getLogger("test"), uri=uri) == 0
        assert seen == [uri]
        assert (tmp_path / "contract.orders.fluid.yaml").is_file()

    def test_forge_data_model_from_source(self, recorded, tmp_path):
        from fluid_build.cli import main

        main(
            [
                "forge",
                "data-model",
                "from-source",
                "--source",
                "postgres",
                "--uri",
                PG_URI,
                "--output",
                str(tmp_path / "model.fluid.yaml"),
            ]
        )
        assert not _is_pinned(recorded.only)
        # libpq reads this host's PGSSLCERT / PGSSLKEY / PGSERVICE as it did before.
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV]

    def test_fetch_sample_rows_reads_an_operator_configured_uri(self, recorded):
        # The URI comes from the operator's environment (FLUID_FORGE_DB_URI*); the
        # model names only a table and a connection alias.
        from fluid_build.cli.forge_db_tools import dispatch_db_tool

        result = dispatch_db_tool(
            "fetch_sample_rows",
            {"table": "customers", "schema": "public", "operator_supplied": False},
            {"FLUID_FORGE_DB_URI": PG_URI, "FLUID_FORGE_DB_TOOLS": "1"},
        )
        assert "error" not in result, result
        assert not _is_pinned(recorded.only)


def _forge_from_source(tmp_path, **extra: Any) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "source": "postgres",
        "uri": PG_URI,
        "output_path": str(tmp_path / "contract.fluid.yaml"),
    }
    args.update(extra)
    return args


class TestMcpClientStaysPinned:
    """A URI an MCP client sent never presents this host's TLS identity."""

    def test_forge_from_source(self, recorded, tmp_path):
        from fluid_build.cli.mcp.dispatch import _call_tool

        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                _forge_from_source(tmp_path),
                read_only=False,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [{k: None for k in _HOST_LIBPQ_ENV}]

    @pytest.mark.parametrize(
        "extra",
        [
            {"operator_supplied": True},
            {"operatorSupplied": True},
            {"scope": {"operator_supplied": True}},
            {"credentials": {"operator_supplied": True}},
        ],
        ids=["argument", "camel-case-argument", "in-scope", "in-credentials"],
    )
    def test_a_client_supplied_operator_supplied_has_no_effect(self, recorded, tmp_path, extra):
        from fluid_build.cli.mcp.dispatch import _call_tool

        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                _forge_from_source(tmp_path, **extra),
                read_only=False,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [{k: None for k in _HOST_LIBPQ_ENV}]

    def test_a_namespace_attribute_has_no_effect(self, recorded, tmp_path):
        # The switch is read from the keyword argument only, never from the
        # argparse-shaped namespace the MCP shim builds.
        from fluid_build.cli._forge_data_model_jdbc import _run_from_jdbc_source

        namespace = argparse.Namespace(
            source="postgres",
            uri=PG_URI,
            output=str(tmp_path / "contract.fluid.yaml"),
            name=None,
            schema_name=None,
            tables=None,
            operator_supplied=True,
        )
        _run_from_jdbc_source(namespace, logging.getLogger("test"))
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [{k: None for k in _HOST_LIBPQ_ENV}]

    def test_a_password_free_uri_is_unchanged(self, recorded, tmp_path):
        # No password: nothing to pin, over MCP as on the CLI (the
        # --allow-metadata-service gate is what authorises that connection).
        from fluid_build.cli.mcp.dispatch import _call_tool

        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                _forge_from_source(tmp_path, uri="postgresql://fluid@db.test:5432/app"),
                read_only=False,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert not _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV]


MYSQL_URI = f"mysql://fluid:{FAKE_PW}@db.test:3306/app"
_HIDDEN = {k: None for k in _HOST_LIBPQ_ENV}


class TestDiscovererCalledDirectly:
    """A caller that does not say where the URI came from gets the whole pin, as
    ``introspect_jdbc`` gives it: the DSN and the host credential window, which
    for mysql is the whole pin."""

    def test_postgres_is_pinned_and_hides_the_host_environment(self, recorded):
        from fluid_build.cli.discover.postgres import PostgresDiscoverer

        PostgresDiscoverer().discover(PG_URI)
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HIDDEN]
        assert recorded.mysql_pwd_at_attach == [None]
        # Put back once the attach is done.
        assert {k: os.environ.get(k) for k in _HOST_LIBPQ_ENV} == _HOST_LIBPQ_ENV

    def test_mysql_hides_the_host_environment(self, recorded):
        from fluid_build.cli.discover.mysql import MySqlDiscoverer

        MySqlDiscoverer().discover(MYSQL_URI)
        assert recorded.mysql_pwd_at_attach == [None]
        assert recorded.env_at_attach == [_HIDDEN]
        assert os.environ.get("MYSQL_PWD") == _HOST_MYSQL_PWD

    def test_a_password_free_uri_is_left_to_the_host(self, recorded):
        from fluid_build.cli.discover.postgres import PostgresDiscoverer

        PostgresDiscoverer().discover("postgresql://fluid@db.test:5432/app")
        assert not _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV]

    def test_the_operator_keyword_turns_the_whole_pin_off(self, recorded):
        from fluid_build.cli.discover.mysql import MySqlDiscoverer
        from fluid_build.cli.discover.postgres import PostgresDiscoverer

        PostgresDiscoverer().discover(PG_URI, operator_supplied=True)
        MySqlDiscoverer().discover(MYSQL_URI, operator_supplied=True)
        assert not _is_pinned(recorded.attaches[0])
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV, _HOST_LIBPQ_ENV]
        assert recorded.mysql_pwd_at_attach == [_HOST_MYSQL_PWD, _HOST_MYSQL_PWD]


def _run_cli(argv: List[str]) -> int:
    from fluid_build.cli import main

    try:
        rc = main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return int(rc or 0)


class TestExtensionDiscoverersKeepMainsInterface:
    """A JdbcDiscoverer subclass registered through ``register_discoverer`` and
    written against main's one-argument ``discover(uri)`` / ``_introspect(conn)``
    still works under ``fluid init --discover``, as the operator's own connection,
    and is pinned anywhere else."""

    @pytest.fixture
    def registry(self, monkeypatch):
        from fluid_build.cli.discover import registry as reg

        monkeypatch.setattr(reg, "DISCOVERERS", dict(reg.DISCOVERERS))
        return reg

    @staticmethod
    def _config(scheme: str) -> Any:
        import dataclasses

        from fluid_build.cli.discover._jdbc_base import POSTGRES_CONFIG

        return dataclasses.replace(POSTGRES_CONFIG, schemes=(scheme,))

    @staticmethod
    def _one_argument_discover() -> Any:
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer

        class TenantDiscoverer(JdbcDiscoverer):
            def discover(self, uri: str) -> List[Any]:  # main's signature
                streams = super().discover(uri)
                return [s for s in streams if not s.name.endswith("_audit")]

        return TenantDiscoverer

    @staticmethod
    def _one_argument_introspect() -> Any:
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer

        class TaggingDiscoverer(JdbcDiscoverer):
            def _introspect(self, conn: dict) -> List[Any]:  # main's signature
                streams = super()._introspect(conn)
                for stream in streams:
                    stream.metadata["tagged"] = True
                return streams

        return TaggingDiscoverer

    @staticmethod
    def _thread_pool_discover() -> Any:
        import concurrent.futures

        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer

        class PooledDiscoverer(JdbcDiscoverer):
            # Main's signature, introspecting in a worker thread it starts. A
            # worker does not run in the calling thread's context (PEP 567).
            def discover(self, uri: str) -> List[Any]:
                conn = self._parse_uri(uri)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(self._introspect, conn).result()

        return PooledDiscoverer

    _PLUGINS = ["_one_argument_discover", "_one_argument_introspect", "_thread_pool_discover"]

    @pytest.mark.parametrize("which", _PLUGINS)
    def test_init_discover_runs_it_as_the_operator(self, recorded, registry, tmp_path, which):
        cls = getattr(self, which)()
        registry.register_discoverer("tenantpg", cls(config=self._config("tenantpg")))
        uri = f"tenantpg://fluid:{FAKE_PW}@db.test:5432/app"
        rc = _run_cli(["init", "--discover", uri, "--dir", str(tmp_path / "out"), "--yes"])
        assert rc == 0
        assert not _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV]

    @pytest.mark.parametrize("which", _PLUGINS)
    def test_called_directly_it_is_pinned(self, recorded, which):
        cls = getattr(self, which)()
        cls(config=self._config("tenantpg")).discover(f"tenantpg://fluid:{FAKE_PW}@db.test/app")
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HIDDEN]

    def test_a_plugin_that_takes_the_keyword_gets_it(self, monkeypatch, tmp_path):
        import fluid_build.cli._init_discover_handler as handler
        from fluid_build.cli.discover._jdbc_base import POSTGRES_CONFIG, JdbcDiscoverer

        got: List[bool] = []

        class Plugin(JdbcDiscoverer):
            def discover(self, uri: str, *, operator_supplied: bool = False) -> List[Any]:
                got.append(operator_supplied)
                return []

        monkeypatch.setattr(handler, "get_discoverer", lambda _uri: Plugin(config=POSTGRES_CONFIG))
        args = argparse.Namespace(target_dir=str(tmp_path), name=None)
        assert handler.run_discover(args, logging.getLogger("test"), uri=PG_URI) == 0
        assert got == [True]

    def test_the_operator_context_ends_with_the_command(self, recorded):
        # A discoverer that fails inside the operator's command leaves nothing
        # behind: the next connection made without it is pinned again.
        from fluid_build.cli.discover._jdbc_base import discover_operator_uri
        from fluid_build.cli.discover.postgres import PostgresDiscoverer

        class Failing:
            def discover(self, uri: str) -> List[Any]:
                raise RuntimeError("driver said no")

        with pytest.raises(RuntimeError):
            discover_operator_uri(Failing(), PG_URI)
        PostgresDiscoverer().discover(PG_URI)
        assert _is_pinned(recorded.only)
        assert recorded.env_at_attach == [_HIDDEN]


class TestOperatorSwitchOnTheInstance:
    """``discover_operator_uri`` also counts the discoverer instance it runs as
    the operator's, for the length of the call, so a plugin's own worker threads
    see the switch. Any other instance stays pinned, and the instance is pinned
    again once the call returns or raises."""

    _URI = f"tenantpg://fluid:{FAKE_PW}@db.test:5432/app"

    @staticmethod
    def _config() -> Any:
        import dataclasses

        from fluid_build.cli.discover._jdbc_base import POSTGRES_CONFIG

        return dataclasses.replace(POSTGRES_CONFIG, schemes=("tenantpg",))

    @staticmethod
    def _in_a_thread(fn: Any, *args: Any) -> None:
        import threading

        worker = threading.Thread(target=fn, args=args)
        worker.start()
        worker.join()

    def test_shards_in_a_thread_pool_all_run_as_the_operator(self, recorded):
        import concurrent.futures

        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, discover_operator_uri

        class ShardedDiscoverer(JdbcDiscoverer):
            def discover(self, uri: str) -> List[Any]:
                base = self._parse_uri(uri)
                shards = [dict(base, database=f"app_{i}") for i in range(3)]
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    return [s for r in pool.map(self._introspect, shards) for s in r]

        discover_operator_uri(ShardedDiscoverer(config=self._config()), self._URI)
        assert len(recorded.attaches) == 3
        assert not any(_is_pinned(sql) for sql in recorded.attaches)
        assert recorded.env_at_attach == [_HOST_LIBPQ_ENV] * 3

    def test_another_instance_stays_pinned_during_the_call(self, recorded):
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, discover_operator_uri

        other = JdbcDiscoverer(config=self._config())
        in_thread = self._in_a_thread

        class Plugin(JdbcDiscoverer):
            def discover(self, uri: str) -> List[Any]:
                conn = self._parse_uri(uri)
                in_thread(other._introspect, conn)  # not the operator's instance
                in_thread(self._introspect, conn)  # the operator's, in its own thread
                return []

        discover_operator_uri(Plugin(config=self._config()), self._URI)
        assert [_is_pinned(sql) for sql in recorded.attaches] == [True, False]
        assert recorded.env_at_attach == [_HIDDEN, _HOST_LIBPQ_ENV]

    @pytest.mark.parametrize("fails", [False, True], ids=["returns", "raises"])
    def test_the_instance_is_pinned_again_after_the_call(self, recorded, fails):
        from fluid_build.cli.discover import _jdbc_base
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, discover_operator_uri

        class Plugin(JdbcDiscoverer):
            def discover(self, uri: str) -> List[Any]:
                if fails:
                    raise RuntimeError("driver said no")
                return []

        plugin = Plugin(config=self._config())
        if fails:
            with pytest.raises(RuntimeError):
                discover_operator_uri(plugin, self._URI)
        else:
            discover_operator_uri(plugin, self._URI)
        assert _jdbc_base._OPERATOR_INSTANCES == {}
        self._in_a_thread(plugin._introspect, plugin._parse_uri(self._URI))
        plugin._introspect(plugin._parse_uri(self._URI))
        assert [_is_pinned(sql) for sql in recorded.attaches] == [True, True]

    def test_a_nested_call_does_not_end_the_outer_one(self, recorded):
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, discover_operator_uri

        in_thread = self._in_a_thread
        depth: List[int] = []

        class Plugin(JdbcDiscoverer):
            def discover(self, uri: str) -> List[Any]:
                depth.append(1)
                if len(depth) == 1:
                    discover_operator_uri(self, uri)  # returns before the outer call
                in_thread(self._introspect, self._parse_uri(uri))
                return []

        discover_operator_uri(Plugin(config=self._config()), self._URI)
        assert [_is_pinned(sql) for sql in recorded.attaches] == [False, False]
