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

"""The duckdb runner's ``ATTACH ... (TYPE mysql)`` for a contract's mysql source.

DuckDB's mysql_scanner DSN is not libpq syntax: an unquoted value runs to the
next space, single quotes mean nothing, inside double quotes only ``\\\\`` and
``\\"`` are escapes, and everything after a ``?`` is read as URI options
whatever the quoting. A connection value must reach the server as exactly
that value and never add a keyword such as ``socket=`` or ``ssl_key=``.
"""

from __future__ import annotations

import types
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.build_runners.duckdb.runner import DuckdbRunner

FAKE_PW = "runner-fake-pw-5Q"  # pragma: allowlist secret
_BASE = {
    "host": "db.test",
    "port": "3306",
    "user": "fluid",
    "password": FAKE_PW,
    "database": "app",
}


def _attach_on(con: Any, raw: Dict[str, Any], kind: str = "mysql") -> None:
    """Run the runner's attach of a ``kind`` source with ``raw`` on ``con``."""
    ctx = types.SimpleNamespace(
        build_id="b1",
        source=types.SimpleNamespace(kind=kind, connection=types.SimpleNamespace(raw=dict(raw))),
    )
    runner = DuckdbRunner.__new__(DuckdbRunner)
    runner._attach_external_databases(con, ctx)  # type: ignore[arg-type]


def _attach(raw: Dict[str, Any], kind: str = "mysql") -> List[str]:
    """Run the runner's mysql attach on a recording connection; return the SQL run."""
    seen: List[str] = []

    class _Con:
        def execute(self, sql: str, *_a: Any, **_k: Any) -> None:
            seen.append(sql)

    _attach_on(_Con(), raw, kind)
    return seen


def _dsn_of(attach_sql: str) -> str:
    """The DSN inside ``ATTACH '<dsn>' AS ...`` with the SQL literal unquoted."""
    assert attach_sql.startswith("ATTACH '"), attach_sql
    literal = attach_sql[len("ATTACH '") : attach_sql.rindex("' AS ")]
    return literal.replace("''", "'")


def _parse_mysql_dsn(dsn: str) -> List[Tuple[str, str]]:
    """Read ``dsn`` the way mysql_scanner's key=value reader does."""
    pairs: List[Tuple[str, str]] = []
    i = 0
    while i < len(dsn):
        while i < len(dsn) and dsn[i] == " ":
            i += 1
        if i >= len(dsn):
            break
        eq = dsn.index("=", i)
        key, i = dsn[i:eq], eq + 1
        if i < len(dsn) and dsn[i] == '"':
            i += 1
            value: List[str] = []
            while dsn[i] != '"':
                if dsn[i] == "\\" and dsn[i + 1] in '\\"':
                    value.append(dsn[i + 1])
                    i += 2
                    continue
                value.append(dsn[i])
                i += 1
            i += 1
            pairs.append((key, "".join(value)))
        else:
            end = dsn.find(" ", i)
            end = len(dsn) if end < 0 else end
            pairs.append((key, dsn[i:end]))
            i = end
    return pairs


_TRICKY = [
    "x socket=/tmp/fake.sock",
    'x" socket="/tmp/fake.sock',
    "x' socket='/tmp/fake.sock",
    'x\\" ssl_key=\\"/tmp/fake.key',
    "x\\",
    "with space",
    "eq=sign",
]


class TestEveryValueReachesTheServerExactly:
    @pytest.mark.parametrize("field", ["user", "password", "database"])
    @pytest.mark.parametrize("value", _TRICKY)
    def test_one_keyword_per_field(self, field, value):
        (sql,) = _attach(dict(_BASE, **{field: value}))
        pairs = _parse_mysql_dsn(_dsn_of(sql))
        keys = [key for key, _ in pairs]
        assert keys == ["host", "port", "user", "password", "database"], sql
        assert dict(pairs)[field] == value, sql

    def test_a_plain_connection(self):
        (sql,) = _attach(_BASE)
        assert dict(_parse_mysql_dsn(_dsn_of(sql))) == _BASE
        assert sql.endswith(" AS mysql_b1 (TYPE mysql)")


class TestRefusedBeforeTheAttach:
    @pytest.mark.parametrize("field", ["host", "user", "password", "database"])
    @pytest.mark.parametrize("value", ["?", "pw?socket=/tmp/fake.sock", "a ? b"])
    def test_question_mark(self, field, value):
        # mysql_scanner reads everything after a '?' as URI options.
        seen: List[str] = []
        with pytest.raises(ValueError, match=field):
            seen = _attach(dict(_BASE, **{field: value}))
        assert seen == []

    @pytest.mark.parametrize("host", ["x://y", "/tmp/fake.sock"])
    def test_a_host_with_a_slash(self, host):
        # "name://..." makes the client load a plugin of that name.
        with pytest.raises(ValueError, match="host"):
            _attach(dict(_BASE, host=host))

    @pytest.mark.parametrize("port", ["\uff13\uff13\uff10\uff16", "3306 socket=x", "-1"])
    def test_a_port_that_is_not_ascii_digits(self, port):
        with pytest.raises(ValueError, match="port"):
            _attach(dict(_BASE, port=port))


# Every character of Unicode category Cc: C0 (NUL, TAB, LF, CR, ESC), DEL and C1
# (U+0085 NEXT LINE is a line break to str.splitlines(), U+009B a one-byte CSI).
_CONTROLS = ["\x00", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", "\x9b"]


class TestControlCharactersRefusedBeforeTheAttach:
    """A control character in a value is refused before the ATTACH, as on every
    JDBC path: inside the DSN's double quotes it is carried, not interpreted, but
    the scanner's connect error quotes the DSN, so an ESC or a newline would reach
    the terminal of whoever runs ``fluid apply`` and forge its log lines."""

    @pytest.mark.parametrize("kind", ["mysql", "mariadb"])
    @pytest.mark.parametrize("field", ["host", "user", "password", "database"])
    @pytest.mark.parametrize("ch", _CONTROLS, ids=lambda c: f"U+{ord(c):04X}")
    def test_refused(self, kind, field, ch):
        seen: List[str] = []

        class _Con:
            def execute(self, sql: str, *_a: Any, **_k: Any) -> None:
                seen.append(sql)

        with pytest.raises(ValueError, match=f"{field}.*control characters"):
            _attach_on(_Con(), dict(_BASE, **{field: f"ab{ch}cd"}), kind)
        assert seen == []

    def test_a_secret_ref_password_is_checked_after_it_is_resolved(self, monkeypatch):
        monkeypatch.setenv("RUNNER_FAKE_MYSQL_SECRET", "ab\x1b[31mcd")
        raw = {k: v for k, v in _BASE.items() if k != "password"}
        raw["secretRef"] = "env://RUNNER_FAKE_MYSQL_SECRET"  # pragma: allowlist secret
        with pytest.raises(ValueError, match="password.*control characters"):
            _attach(raw)

    @pytest.mark.parametrize("value", ["p\xa0w", "pässwörd", "密码"], ids=repr)
    def test_other_non_ascii_text_is_accepted(self, value):
        (sql,) = _attach(dict(_BASE, password=value))
        assert dict(_parse_mysql_dsn(_dsn_of(sql)))["password"] == value


class TestPostgresAttachDsnRefusesControlCharacters:
    """The runner's postgres source is attached with ``ATTACH '<dsn>' ... (TYPE
    postgres)``, whose DSN the same shared builder makes. libpq ends a bare
    value at any C ``isspace()`` character, and libpq_escape quotes only a value
    with a space or a quote, so a tab or newline there started a second keyword."""

    @pytest.mark.parametrize("field", ["host", "user", "password", "database"])
    @pytest.mark.parametrize("ch", _CONTROLS, ids=lambda c: f"U+{ord(c):04X}")
    def test_refused(self, field, ch):
        conn = {"host": "db.test", "port": "5432", "user": "u", "password": FAKE_PW}
        conn["database"] = "app"
        conn[field] = f"ab{ch}sslmode=disable"
        with pytest.raises(ValueError, match=f"{field}.*control characters"):
            _attach(conn, kind="postgres")


class TestPostgresAttach:
    """DuckDB 2.0's postgres extension has no ``postgres_scan``, so the runner
    attaches a postgres source before the sandbox's lock, read-only, as it
    attaches mysql and sqlite."""

    def test_the_source_is_attached_read_only_under_a_per_build_alias(self):
        secret, sql = _attach(dict(_BASE, port="5432"), kind="postgres")
        # An empty temporary secret: the sandbox's disabled persistent storage
        # otherwise fails the ATTACH's default-secret lookup.
        assert secret == "CREATE TEMPORARY SECRET postgres_b1 (TYPE postgres)"
        assert sql.startswith("ATTACH '")
        assert sql.endswith(" AS postgres_b1 (TYPE postgres, SECRET postgres_b1, READ_ONLY)")
        assert "dbname=app" in _dsn_of(sql)

    def test_the_select_reads_the_attached_alias(self):
        from fluid_build.build_runners.duckdb.runner import _build_select_for_postgres

        assert (
            _build_select_for_postgres("sales.orders", alias="postgres_b1")
            == "SELECT * FROM postgres_b1.sales.orders"
        )
        assert (
            _build_select_for_postgres("orders", alias="postgres_b1")
            == "SELECT * FROM postgres_b1.public.orders"
        )

    def test_a_secret_ref_password_is_resolved(self, monkeypatch):
        monkeypatch.setenv("RUNNER_FAKE_PG_SECRET", "runner-pg-ref-pw-3J")
        raw = {k: v for k, v in _BASE.items() if k != "password"}
        raw["secretRef"] = "env://RUNNER_FAKE_PG_SECRET"  # pragma: allowlist secret
        _secret, sql = _attach(raw, kind="postgres")
        assert "password=runner-pg-ref-pw-3J" in _dsn_of(sql)

    def test_a_connect_error_has_the_password_masked(self):
        class _Con:
            def execute(self, sql: str, *_a: Any, **_k: Any) -> None:
                if sql.startswith("ATTACH"):
                    # What postgres_scanner says: it quotes the DSN it was given.
                    raise RuntimeError(
                        f'IO Error: Unable to connect to Postgres at "{_dsn_of(sql)}"'
                    )

        with pytest.raises(RuntimeError) as caught:
            _attach_on(_Con(), dict(_BASE, port="5432"), kind="postgres")
        assert FAKE_PW not in str(caught.value)
        assert "password=***" in str(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None


class TestConnectErrorHasThePasswordMasked:
    """mysql_scanner quotes the whole DSN it was given in a connect error, and
    ``fluid apply`` prints that error; the password in it is masked, in every
    form the DSN and the SQL literal spell it, as the JDBC introspection's own
    connect error is."""

    @staticmethod
    def _failing_con() -> Any:
        class _Con:
            def execute(self, sql: str, *_a: Any, **_k: Any) -> None:
                # What mysql_scanner 1.5.6 says, quoting the DSN and the SQL.
                raise RuntimeError(
                    "IO Error: Failed to connect to MySQL database with parameters "
                    f'"{_dsn_of(sql)}": Can\'t connect to server on 127.0.0.1 (111)\n'
                    f"LINE 1: {sql}"
                )

        return _Con()

    @staticmethod
    def _chain(exc: BaseException) -> List[BaseException]:
        seen: List[BaseException] = []
        stack: List[Any] = [exc]
        while stack:
            current = stack.pop()
            if current is None or any(current is s for s in seen):
                continue
            seen.append(current)
            stack.extend([current.__cause__, current.__context__])
        return seen

    @pytest.mark.parametrize(
        "password",
        [
            FAKE_PW,
            "runner fake pw 5Q",  # pragma: allowlist secret
            "runner\"fake\\pw'5Q",  # pragma: allowlist secret
        ],
        ids=["plain", "spaced", "quotes-and-backslash"],
    )
    def test_masked(self, password):
        with pytest.raises(Exception) as caught:
            _attach_on(self._failing_con(), dict(_BASE, password=password))
        for exc in self._chain(caught.value):
            text = f"{exc!s} {exc.args!r}"
            for piece in ("runner", "5Q"):
                assert piece not in text, text
        message = str(caught.value)
        assert "Failed to connect to MySQL database" in message
        assert 'user="fluid"' in message and "password=***" in message
        # Raised after the handler: the unmasked error is not kept on the chain.
        assert caught.value.__context__ is None and caught.value.__cause__ is None

    def test_a_secret_ref_password_is_masked(self, monkeypatch):
        monkeypatch.setenv("RUNNER_FAKE_MYSQL_SECRET", "runner-ref-pw-8K")
        raw = {k: v for k, v in _BASE.items() if k != "password"}
        raw["secretRef"] = "env://RUNNER_FAKE_MYSQL_SECRET"  # pragma: allowlist secret
        with pytest.raises(Exception) as caught:
            _attach_on(self._failing_con(), raw)
        assert "runner-ref-pw-8K" not in str(caught.value)
        assert "password=***" in str(caught.value)

    def test_without_a_password_the_error_is_unchanged(self):
        raw = {k: v for k, v in _BASE.items() if k != "password"}
        with pytest.raises(RuntimeError) as caught:
            _attach_on(self._failing_con(), raw)
        assert 'user="fluid"' in str(caught.value)
