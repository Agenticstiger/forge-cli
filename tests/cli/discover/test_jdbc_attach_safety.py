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

"""Safety of the JDBC ``ATTACH`` string built from a client-supplied URI.

The duckdb ``ATTACH '<dsn>' AS ...`` string is built from URI fields a caller
controls. Two injection layers must stay closed:

* the **libpq layer** — a DSN is ``key=value`` space-separated, so a value with
  a space must not be able to add a second keyword (``host=`` / ``passfile=`` /
  ``sslcert=`` …);
* the **DuckDB SQL layer** — the DSN sits inside a ``'...'`` SQL literal, so a
  value with a quote must not be able to break out into DuckDB SQL (DuckDB can
  read and write files: ``read_csv`` / ``COPY ... TO``).

And an inline connection (a URI that carries its own password) must be pinned
to that password so it never falls back to this host's ``~/.pgpass``.
"""

from __future__ import annotations

import os
import threading

import pytest

from fluid_build.cli.discover._jdbc_introspect import (
    _attach_string_mysql,
    _attach_string_postgres,
    _attach_string_sqlite,
    _host_credential_env_suppressed,
    _parse_uri,
    build_attach_dsn,
)

_PG_DB_KEY = "dbname"  # pragma: allowlist secret (the libpq keyword name)


def _pg(uri: str) -> str:
    return _attach_string_postgres(_parse_uri(uri, "postgres"), "s")


def _my(uri: str) -> str:
    return _attach_string_mysql(_parse_uri(uri, "mysql"), "s")


class TestKeywordInjectionClosed:
    def test_space_value_stays_one_libpq_field(self):
        # A password carrying a space would, unquoted, add host=/port= keywords.
        sql = _pg("postgresql://fluid:x host=evil port=1@h:5432/db")
        # The whole attacker value is quoted as one libpq field (single quotes
        # doubled for the surrounding DuckDB literal) — not a second keyword.
        assert "password=''x host=evil port=1''" in sql
        # The only live host keyword is the real one.
        assert "host=h " in sql
        assert "passfile=/dev/null sslcert=" in sql
        assert sql.endswith(".key' AS s (TYPE postgres)")

    def test_mysql_space_value_stays_one_field(self):
        sql = _my("mysql://u:p q@h:3306/db")
        assert 'password="p q"' in sql  # pragma: allowlist secret
        assert sql.startswith('ATTACH \'host="h" ')


class TestLiteralBreakoutClosed:
    def test_quote_in_value_cannot_break_the_sql_literal(self):
        sql = _pg("postgresql://fluid:p' AS a (TYPE postgres); ATTACH ':memory:'@h:5432/db")
        # No bare `' AS a` escapes the literal: every embedded quote is doubled
        # for DuckDB and backslash-escaped for libpq, so it stays in the value.
        assert "' AS a (TYPE postgres); ATTACH ':memory:' AS s" not in sql
        assert sql.endswith("' AS s (TYPE postgres)")

    def test_sqlite_path_is_quoted(self):
        assert _attach_string_sqlite({"path": "/data/app.db"}, "s") == (
            "ATTACH '/data/app.db' AS s (TYPE sqlite)"
        )

    def test_sqlite_quote_in_path_is_doubled_not_broken_out(self):
        sql = _attach_string_sqlite({"path": "a.db'; COPY (SELECT 1) TO 'x.csv"}, "s")
        assert "''" in sql  # the embedded quote is doubled
        assert sql.endswith("' AS s (TYPE sqlite)")


class TestControlCharsRejected:
    @pytest.mark.parametrize("bad", ["x\ny", "x\x00y", "x\ty", "x\ry"])
    def test_postgres_rejects_control_chars(self, bad):
        with pytest.raises(ValueError, match="control characters"):
            _attach_string_postgres(
                {"host": "h", "port": "5432", "user": "u", "password": bad, "dbname": "db"}, "s"
            )

    def test_sqlite_rejects_control_chars(self):
        with pytest.raises(ValueError, match="control characters"):
            _attach_string_sqlite({"path": "a\nb.db"}, "s")

    def test_build_attach_dsn_rejects_control_chars(self):
        with pytest.raises(ValueError, match="control characters"):
            build_attach_dsn(
                {"host": "h\n", "database": "db"},
                database_key="dbname",  # pragma: allowlist secret
                postgres=True,
            )

    # The C1 controls, U+0080..U+009F: Unicode category Cc, like C0 and DEL.
    # U+0085 is NEXT LINE, a line break to Python's str.splitlines().
    @pytest.mark.parametrize("ch", ["\x80", "\x85", "\x9b", "\x9f"], ids=repr)
    @pytest.mark.parametrize("field", ["host", "user", "password", "database"])
    @pytest.mark.parametrize("postgres", [True, False], ids=["postgres", "mysql"])
    def test_c1_controls_are_rejected(self, ch, field, postgres):
        conn = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}
        conn[field] += ch
        with pytest.raises(ValueError, match="control characters"):
            build_attach_dsn(conn, database_key=_PG_DB_KEY, postgres=postgres)

    def test_sqlite_path_c1_control_is_rejected(self):
        with pytest.raises(ValueError, match="control characters"):
            _attach_string_sqlite({"path": "a\x85b.db"}, "s")

    # Not controls: NO-BREAK SPACE (Zs), the first character past C1, and letters.
    @pytest.mark.parametrize("value", ["p\xa0w", "\xa1", "pässwörd", "密码"], ids=repr)
    @pytest.mark.parametrize("postgres", [True, False], ids=["postgres", "mysql"])
    def test_other_non_ascii_text_is_accepted(self, value, postgres):
        conn = {"host": "h", "port": "5432", "user": "u", "password": value, "database": "d"}
        assert value in build_attach_dsn(conn, database_key=_PG_DB_KEY, postgres=postgres)


class TestSharedBuildersRejectControlChars:
    """The check lives in the shared DSN builders, so every caller gets it: the
    JDBC attach, and the duckdb build runner's mysql and postgres ATTACH DSNs,
    which call the builders directly."""

    @pytest.mark.parametrize("ch", ["\x00", "\t", "\n", "\x1b", "\x7f", "\x85"], ids=repr)
    @pytest.mark.parametrize("field", ["host", "user", "password", "database"])
    @pytest.mark.parametrize("builder", ["build_libpq_dsn", "build_mysql_dsn"])
    def test_refused(self, builder, field, ch):
        from fluid_build.providers import _sql_safety

        conn = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}
        conn[field] = f"a{ch}b"
        with pytest.raises(ValueError, match=f"{field}.*control characters"):
            getattr(_sql_safety, builder)(conn)

    def test_the_jdbc_attach_and_the_runner_share_one_masker(self):
        from fluid_build.cli.discover import _jdbc_introspect
        from fluid_build.providers._sql_safety import mask_dsn_password

        assert _jdbc_introspect._mask_password is mask_dsn_password


class TestPostgresPortIsAsciiDigits:
    """The postgres half of the builder takes the same ASCII-only port as the
    mysql half: str.isdigit() alone accepts every Unicode decimal digit."""

    _PORTS = {
        "fullwidth": "３３０６",
        "arabic-indic": "٣٣٠٦",
        "superscript": "³³⁰⁶",
        "mixed": "33٠٦",
    }

    @pytest.mark.parametrize("name", sorted(_PORTS))
    def test_build_attach_dsn_refuses_a_non_ascii_digit_port(self, name):
        port = self._PORTS[name]
        assert port.isdigit() and not port.isascii()
        with pytest.raises(ValueError, match="port"):
            build_attach_dsn(
                {"host": "h", "port": port, "user": "u", "password": "p", "database": "d"},
                database_key="dbname",  # pragma: allowlist secret (the libpq keyword name)
                postgres=True,
            )

    @pytest.mark.parametrize("name", sorted(_PORTS))
    def test_build_libpq_dsn_refuses_a_non_ascii_digit_port(self, name):
        # The shared builder: the duckdb runner's postgres ATTACH DSN uses it too.
        from fluid_build.providers._sql_safety import build_libpq_dsn

        with pytest.raises(ValueError, match="port"):
            build_libpq_dsn({"host": "h", "port": self._PORTS[name]})

    @pytest.mark.parametrize("port", ["5432", 5432, "0", "65535"])
    def test_ascii_digits_are_accepted(self, port):
        dsn = build_attach_dsn(
            {"host": "h", "port": port, "user": "u", "password": "p", "database": "d"},
            database_key="dbname",  # pragma: allowlist secret (the libpq keyword name)
            postgres=True,
        )
        assert f"port={port} " in dsn


class TestPinToOwnMaterial:
    def test_password_present_pins_passfile_to_devnull(self):
        sql = _pg("postgresql://fluid:secret@h:5432/db")
        assert "passfile=/dev/null" in sql
        assert "password=secret" in sql

    def test_no_password_leaves_host_fallback_for_the_operator(self):
        # The operator CLI legitimately relies on ~/.pgpass / PGPASSWORD here;
        # over MCP the --allow-metadata-service gate is what authorises it.
        sql = _pg("postgresql://fluid@h:5432/db")
        assert "password=" not in sql
        assert "passfile=" not in sql

    def test_mysql_has_no_passfile_key(self):
        # passfile is a libpq-only concept; the mysql DSN pins via its explicit
        # password alone.
        sql = _my("mysql://u:secret@h:3306/db")
        assert "passfile" not in sql
        assert 'password="secret"' in sql  # pragma: allowlist secret

    def test_password_present_pins_away_the_default_client_certificate(self):
        # libpq would otherwise present this host's ~/.postgresql/postgresql.crt
        # (and key) to any server that asks for a client certificate.
        sql = _pg("postgresql://fluid:secret@h:5432/db")
        assert "sslcert=" in sql and "sslkey=" in sql
        dsn = sql.split("'", 1)[1].rsplit("'", 1)[0]
        for key in ("sslcert", "sslkey"):
            value = dsn.split(f"{key}=", 1)[1].split(" ", 1)[0]
            # The path can never exist, so libpq proceeds without a certificate.
            assert value.startswith(os.devnull + "/")

    def test_no_password_leaves_the_client_certificate_to_the_operator(self):
        sql = _pg("postgresql://fluid@h:5432/db")
        assert "sslcert=" not in sql
        assert "sslkey=" not in sql


class TestInjectionDoesNotExecuteSql:
    """Red->green: before the fix the injected ``COPY ... TO`` ran during the
    ATTACH (pre-sandbox-lock) and wrote the file; after the fix the path is one
    quoted literal and nothing is written."""

    def test_sqlite_injected_copy_does_not_write_a_file(self, tmp_path):
        duckdb = pytest.importorskip("duckdb")
        assert duckdb  # used by introspect_jdbc under the hood
        import sqlite3

        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        real = tmp_path / "real.sqlite"
        sqlite3.connect(str(real)).execute("CREATE TABLE t (id integer)")
        sentinel = tmp_path / "pwned.csv"
        evil = (
            f"{real}' AS a (TYPE sqlite); "
            f"COPY (SELECT 1 AS owned) TO '{sentinel}' (HEADER); ATTACH '{real}"
        )
        with pytest.raises(Exception):
            # The whole string is now one literal path, so opening it fails...
            introspect_jdbc(source="sqlite", uri=f"sqlite:///{evil}")
        # ...and crucially the injected COPY never ran.
        assert not sentinel.exists()


def _parse_duckdb_mysql_dsn(dsn: str) -> dict:
    """Reference parser for the DuckDB mysql_scanner DSN grammar.

    Space-separated ``key=value`` pairs. A value is either unquoted (taken
    literally up to the next space) or double-quoted, where only ``\\\\`` and
    ``\\"`` are escapes. Confirmed against DuckDB 1.5.2's mysql_scanner and a
    capturing server (scratchpad evidence, fa3 round 5).
    """
    out: dict = {}
    i = 0
    while i < len(dsn):
        if dsn[i] == " ":
            i += 1
            continue
        eq = dsn.index("=", i)
        key = dsn[i:eq]
        i = eq + 1
        if i < len(dsn) and dsn[i] == '"':
            i += 1
            buf = []
            while dsn[i] != '"':
                if dsn[i] == "\\":
                    assert dsn[i + 1] in ('"', "\\"), 'only \\\\ and \\" are escapes'
                    buf.append(dsn[i + 1])
                    i += 2
                else:
                    buf.append(dsn[i])
                    i += 1
            i += 1
            value = "".join(buf)
        else:
            end = dsn.find(" ", i)
            end = len(dsn) if end == -1 else end
            value = dsn[i:end]
            i = end
        assert key not in out, f"duplicate key {key!r}"
        out[key] = value
    return out


def _mysql_dsn(conn: dict) -> str:
    sql = _attach_string_mysql(conn, "s")
    literal = sql[len("ATTACH '") : -len("' AS s (TYPE mysql)")]
    return literal.replace("''", "'")


class TestMysqlDsnQuoting:
    """DuckDB's mysql DSN is not libpq syntax: unquoted values are literal and
    split on spaces, single quotes mean nothing, and only double quotes quote.
    Every value must reach the server exactly, and no value may add a key."""

    @pytest.mark.parametrize(
        "value",
        [
            "pa\\ss",
            "trailing\\",
            "p q",
            "p'q",
            'p"q',
            "a=b",
            "\u00fcn\u00ef\u00e7\u00f8d\u00e9",
            "x ssl_mode=required ssl_cipher=z",
            'x" ssl_key="/etc/ssl/private/k.pem',
            "x socket=/var/run/mysqld/mysqld.sock",
            "",
        ],
    )
    @pytest.mark.parametrize("field", ["user", "password", "dbname"])
    def test_value_round_trips_and_adds_no_key(self, field, value):
        conn = {
            "host": "db.test",
            "port": "3306",
            "user": "u",
            "password": "pw",  # pragma: allowlist secret
            "dbname": "d",
        }
        conn[field] = value
        parsed = _parse_duckdb_mysql_dsn(_mysql_dsn(conn))
        expected_keys = {"host", "port", "user", "password", "database"}
        if value == "":
            expected_keys.discard("database" if field == "dbname" else field)
        assert set(parsed) == expected_keys
        key = "database" if field == "dbname" else field
        if value:
            assert parsed[key] == value

    def test_backslash_is_not_doubled_on_the_wire(self):
        # Regression vs origin/main: libpq quoting doubled every backslash.
        parsed = _parse_duckdb_mysql_dsn(
            _mysql_dsn(
                {
                    "host": "h",
                    "port": "3306",
                    "user": "u\\x",
                    "password": "pa\\ss",  # pragma: allowlist secret
                    "dbname": "app\\db",
                }
            )
        )
        assert parsed["user"] == "u\\x"
        assert parsed["password"] == "pa\\ss"  # pragma: allowlist secret
        assert parsed["database"] == "app\\db"

    @pytest.mark.parametrize("field", ["host", "user", "password", "dbname"])
    @pytest.mark.parametrize("value", ["pa?ss", "trailing?", "x?socket=/tmp/m.sock", "?"])
    def test_a_question_mark_is_refused(self, field, value):
        # mysql_scanner runs every attach string through a URI parser first,
        # which splits on '?' regardless of quoting and reads what follows as
        # URI options (socket, ssl-key, ...): no value can carry one.
        conn = {"host": "h", "port": "3306", "user": "u", "password": "p", "dbname": "d"}
        conn[field] = value
        with pytest.raises(ValueError, match=r"'\?'"):
            _mysql_dsn(conn)

    def test_a_question_mark_is_fine_for_postgres(self):
        conn = {"host": "h", "port": "5432", "user": "u", "dbname": "d"}
        conn["password"] = "pa?ss"  # pragma: allowlist secret
        sql = _attach_string_postgres(conn, "s")
        assert "password=pa?ss" in sql

    def test_host_with_a_slash_is_refused(self):
        # A host of the form name://... makes the client dlopen a plugin.
        with pytest.raises(ValueError, match="host"):
            _mysql_dsn(
                {"host": "x://y", "port": "3306", "user": "u", "password": "p", "dbname": "d"}
            )

    def test_non_numeric_port_is_refused(self):
        with pytest.raises(ValueError, match="port"):
            _mysql_dsn(
                {"host": "h", "port": "3306 socket=x", "user": "u", "password": "p", "dbname": "d"}
            )

    @pytest.mark.parametrize(
        "zero",
        [0xFF10, 0x0660, 0x06F0, 0x0966],
        ids=["fullwidth", "arabic-indic", "extended-arabic-indic", "devanagari"],
    )
    def test_a_port_of_non_ascii_digits_is_refused(self, zero):
        # str.isdigit() accepts every Unicode decimal digit; only ASCII 0-9 may
        # reach the DSN as a port.
        port = "".join(chr(zero + int(d)) for d in "3306")
        assert port.isdigit() and not port.isascii()
        with pytest.raises(ValueError, match="port"):
            _mysql_dsn({"host": "h", "port": port, "user": "u", "password": "p", "dbname": "d"})

    def test_a_port_of_superscript_digits_is_refused(self):
        port = "".join(chr(c) for c in (0x00B3, 0x00B3, 0x2070, 0x2076))  # superscript 3306
        assert port.isdigit() and not port.isascii()
        with pytest.raises(ValueError, match="port"):
            _mysql_dsn({"host": "h", "port": port, "user": "u", "password": "p", "dbname": "d"})

    def test_control_chars_are_refused(self):
        with pytest.raises(ValueError, match="control characters"):
            _mysql_dsn(
                {
                    "host": "h",
                    "port": "3306",
                    "user": "u",
                    "password": "p\nq",  # pragma: allowlist secret
                    "dbname": "d",
                }
            )

    def test_parses_the_uri_form(self):
        parsed = _parse_duckdb_mysql_dsn(
            _mysql_dsn(_parse_uri("mysql://u:p q@db.test:3307/d", "mysql"))
        )
        assert parsed == {
            "host": "db.test",
            "port": "3307",
            "user": "u",
            "password": "p q",  # pragma: allowlist secret
            "database": "d",
        }


class TestHostCredentialEnvSuppression:
    _VARS = {"PGPASSWORD": "host-pg-fake", "MYSQL_PWD": "host-my-fake"}  # pragma: allowlist secret

    # Every variable through which the bundled scanners would hand this host's
    # credential, or a file or switch that yields one, to a pinned connection.
    _CREDENTIAL_VARS = {
        "PGPASSWORD": "host-pg-fake",  # pragma: allowlist secret
        "PGPASSFILE": "/fake/host/.pgpass",
        "PGSERVICE": "fake-host-service",
        "PGSERVICEFILE": "/fake/host/pg_service.conf",
        # The directory of the system-wide pg_service.conf.
        "PGSYSCONFDIR": "/fake/host/etc/postgresql",
        "PGSSLCERT": "/fake/host/postgresql.crt",
        "PGSSLKEY": "/fake/host/postgresql.key",
        # DuckDB's postgres_scanner sends it as an OAUTHBEARER token.
        "PGOAUTHTOKEN": "host-oauth-fake",  # pragma: allowlist secret
        # Forwards this host's Kerberos credentials to the server.
        "PGGSSDELEGATION": "1",
        "MYSQL_PWD": "host-my-fake",  # pragma: allowlist secret
        "MYSQL_HOST": "fake-host-mysql",
        "MYSQL_TCP_PORT": "3307",
        "MYSQL_UNIX_PORT": "/fake/host/mysqld.sock",
        "MYSQL_GROUP_SUFFIX": "_fakehost",
    }

    @pytest.mark.parametrize("name", sorted(_CREDENTIAL_VARS))
    def test_each_credential_variable_is_hidden_in_the_window(self, monkeypatch, name):
        value = self._CREDENTIAL_VARS[name]
        monkeypatch.setenv(name, value)
        with _host_credential_env_suppressed():
            assert name not in os.environ
        assert os.environ.get(name) == value

    def test_the_docs_name_only_variables_the_window_hides(self):
        # docs/MCP.md lists this host's ambient DB credential environment that a
        # pinned connection never sees; each name there must be one the window
        # hides, and the bearer token must be among them.
        from pathlib import Path

        from fluid_build.cli.discover._jdbc_introspect import _HOST_CREDENTIAL_ENV

        docs = (Path(__file__).resolve().parents[3] / "docs" / "MCP.md").read_text("utf-8")
        listed = docs.split("ambient DB credential environment (", 1)[1].split(")", 1)[0]
        names = [part.strip(" `\n…,") for part in listed.split(",")]
        names = [name for name in names if name and name != "…"]
        assert "PGOAUTHTOKEN" in names, names
        assert set(names) <= set(_HOST_CREDENTIAL_ENV), names

    def test_restored_after_the_window(self, monkeypatch):
        for k, v in self._VARS.items():
            monkeypatch.setenv(k, v)
        with _host_credential_env_suppressed():
            assert all(k not in os.environ for k in self._VARS)
        assert all(os.environ.get(k) == v for k, v in self._VARS.items())

    def test_restored_when_the_attach_raises(self, monkeypatch):
        for k, v in self._VARS.items():
            monkeypatch.setenv(k, v)
        with pytest.raises(RuntimeError):
            with _host_credential_env_suppressed():
                raise RuntimeError("attach failed")
        assert all(os.environ.get(k) == v for k, v in self._VARS.items())

    def test_overlapping_windows_never_expose_host_material(self, monkeypatch):
        """Thread A is inside its window when thread B starts one, then A leaves.
        Inside B's window the host credentials must still be hidden, and after
        both windows they must be back exactly."""
        for k, v in self._VARS.items():
            monkeypatch.setenv(k, v)
        a_inside, a_may_leave, a_left = threading.Event(), threading.Event(), threading.Event()
        seen_in_b: dict = {}
        errors: list = []

        def run_a():
            try:
                with _host_credential_env_suppressed():
                    a_inside.set()
                    assert a_may_leave.wait(5)
                a_left.set()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def run_b():
            try:
                with _host_credential_env_suppressed():
                    # Read only after A has left its own window.
                    assert a_left.wait(5)
                    seen_in_b.update({k: os.environ.get(k) for k in self._VARS})
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        ta, tb = threading.Thread(target=run_a), threading.Thread(target=run_b)
        ta.start()
        assert a_inside.wait(5)
        tb.start()
        tb.join(0.3)  # let B try to open its window while A is inside
        a_may_leave.set()
        ta.join(5)
        tb.join(5)
        assert not errors
        assert seen_in_b == {k: None for k in self._VARS}
        assert all(os.environ.get(k) == v for k, v in self._VARS.items())

    def test_reentrant_in_one_thread(self, monkeypatch):
        for k, v in self._VARS.items():
            monkeypatch.setenv(k, v)
        with _host_credential_env_suppressed():
            with _host_credential_env_suppressed():
                assert all(k not in os.environ for k in self._VARS)
            assert all(k not in os.environ for k in self._VARS)
        assert all(os.environ.get(k) == v for k, v in self._VARS.items())


def _exception_chain(exc: BaseException) -> list:
    """Every exception reachable from ``exc`` through ``__cause__`` / ``__context__``."""
    seen: list = []
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        stack.extend([current.__cause__, current.__context__])
    return seen


class TestAttachErrorsDoNotEchoThePassword:
    _CASES = pytest.mark.parametrize(
        "uri,secret",
        [
            ("postgresql://u:Fake Pw'x@db.test:5432/d", "Fake Pw'x"),  # pragma: allowlist secret
            ('mysql://u:Fake\\Pw"x@db.test:3306/d', 'Fake\\Pw"x'),  # pragma: allowlist secret
        ],
    )

    @staticmethod
    def _attach_error(monkeypatch, uri: str) -> BaseException:
        pytest.importorskip("duckdb")
        import fluid_build.providers._duckdb_sandbox as sandbox
        from fluid_build.cli.discover import _jdbc_introspect as ji

        class _Con:
            def execute(self, sql, *a, **k):
                # Mimic a driver error that quotes the DSN (as mysql_scanner does).
                raise RuntimeError(f"Failed to connect with parameters {sql}")

        def fake_connect(*_a, before_lock=None, **_k):
            before_lock(_Con())
            raise AssertionError("unreachable")

        monkeypatch.setattr(sandbox, "secure_duckdb_connect", fake_connect)
        kind = "postgres" if uri.startswith("postgresql") else "mysql"
        with pytest.raises(Exception) as info:
            ji.introspect_jdbc(source=kind, uri=uri)
        return info.value

    @_CASES
    def test_no_exception_in_the_chain_carries_the_password(self, monkeypatch, uri, secret):
        """``raise ... from None`` only sets ``__suppress_context__``: the unmasked
        driver error would stay on ``__context__`` for any code that walks it."""
        import traceback

        exc = self._attach_error(monkeypatch, uri)
        texts = ["".join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
        for link in _exception_chain(exc):
            texts += [str(link), repr(link), *(repr(arg) for arg in link.args)]
            texts += [str(note) for note in getattr(link, "__notes__", ())]
        # "Fake" is the password's leading run of plain characters; no quoted,
        # escaped or doubled form of the password can avoid carrying it.
        leaks = [text for text in texts if "Fake" in text or secret in text]
        assert not leaks, leaks

    @_CASES
    def test_attach_failure_message_is_masked(self, monkeypatch, uri, secret):
        exc = self._attach_error(monkeypatch, uri)
        text = str(exc) + repr(exc)
        chain = exc.__cause__ or exc.__context__
        assert chain is None or exc.__suppress_context__
        for form in {
            secret,
            secret.replace("'", "''"),
            secret.replace("\\", "\\\\").replace('"', '\\"'),
        }:
            assert form not in text
        assert "***" in text
