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

"""A JDBC URI that cannot be used never has its password in an error.

``_parse_uri`` quotes the URI it refuses, and ``_run_from_jdbc_source`` prints
that text. Over MCP ``forge_from_source`` that is the server's console and log
(and a mysql URI's error is raised from the gate itself), and
``fetch_sample_rows`` returns the message to the model. Each message, and each
exception chained to it, has the password masked as a connect error has.
"""

from __future__ import annotations

import argparse
import logging
from urllib.parse import urlparse

import pytest

import fluid_build.cli._forge_data_model_jdbc as jdbc_cli
from fluid_build.cli._forge_data_model_jdbc import _run_from_jdbc_source
from fluid_build.cli.discover._jdbc_introspect import _parse_uri
from fluid_build.cli.forge_db_tools import dispatch_db_tool
from fluid_build.cli.mcp.dispatch import _call_tool

PW = "fakePw7Qx"  # pragma: allowlist secret
A = "fakePwA7Qx"  # pragma: allowlist secret
B = "fakePwB7Qx"  # pragma: allowlist secret
FULLWIDTH_AT = chr(0xFF20)  # NFKC-normalises to '@', which urllib refuses in a netloc

# (kind, uri, pieces of the password none of which may appear)
MALFORMED = [
    ("postgres", f"postgres://u:{PW}@/db", [PW]),  # pragma: allowlist secret
    ("postgres", f"postgresql://u:{PW}@:5432/db", [PW]),  # pragma: allowlist secret
    ("mysql", f"mysql://u:{PW}@/db", [PW]),  # pragma: allowlist secret
    ("postgres", f"postgres://u:{PW}@db:notaport/db", [PW]),  # pragma: allowlist secret
    ("postgres", f"postgres://u:{PW}@db:99999/db", [PW]),  # pragma: allowlist secret
    # Two '@': libpq reads the password up to the first one, urllib to the last.
    ("postgres", f"postgres://u:{A}@{B}@/db", [A, B]),  # pragma: allowlist secret
    # No '//': urllib finds no host at all.
    ("postgres", f"postgres:u:{PW}@db/x", [PW]),  # pragma: allowlist secret
    # A reserved character typed unencoded in the password. urllib then reads
    # the piece before it as the port, the bracketed piece as an IPv6 host, or
    # refuses the whole netloc, and quotes that text in its own error.
    ("postgres", f"postgres://u:{A}/{B}@db/x", [A, B]),  # pragma: allowlist secret
    ("postgres", f"postgres://u:{A}?{B}@db/x", [A, B]),  # pragma: allowlist secret
    ("postgres", f"postgres://u:{A}#{B}@db/x", [A, B]),  # pragma: allowlist secret
    ("mysql", f"mysql://u:{A}[{B}]@db/x", [A, B]),  # pragma: allowlist secret
    ("postgres", f"postgres://u:{A}{FULLWIDTH_AT}{B}@db/x", [A, B]),  # pragma: allowlist secret
]
IDS = [uri.replace(FULLWIDTH_AT, "<U+FF20>") for _kind, uri, _pieces in MALFORMED]


def _urllib_reads(uri: str) -> bool:
    """Whether this interpreter's urllib gives ``uri`` a host and a valid port.

    Only then does ``_parse_uri`` accept the URI. It is asked of urllib itself
    rather than inferred from the Python version: whether a bracket in the
    netloc is refused (the bracket row) depends on the urllib patch level, and
    current 3.10 and 3.11 patch releases refuse it as 3.12 does.
    """
    try:
        parsed = urlparse(uri)
        return bool(parsed.hostname) and (parsed.port or 0) >= 0
    except ValueError:
        return False


def _chain(exc: BaseException) -> list:
    """``exc`` and every exception chained to it, suppressed context included."""
    seen: list = []
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        stack.extend([current.__cause__, current.__context__])
    return seen


def _texts_of(exc: BaseException) -> list:
    return [f"{e!s} {e.args!r}" for e in _chain(exc)]


def _assert_masked(texts, pieces):
    for text in texts:
        for piece in pieces:
            assert piece not in text, f"password piece {piece!r} in {text!r}"


@pytest.fixture
def printed(monkeypatch):
    """Every string ``_run_from_jdbc_source`` hands to ``cprint``, still printed."""
    lines: list = []
    real = jdbc_cli.cprint

    def record(*args, **kwargs):
        lines.append(" ".join(str(a) for a in args))
        return real(*args, **kwargs)

    monkeypatch.setattr(jdbc_cli, "cprint", record)
    return lines


@pytest.fixture
def no_connect(monkeypatch):
    """Fail any connection attempt, so a URI that does parse never leaves the test."""
    duckdb = pytest.importorskip("duckdb")
    import fluid_build.providers._duckdb_sandbox as sandbox

    def refuse(*_a, **_k):
        raise RuntimeError("no connection in this test")

    monkeypatch.setattr(sandbox, "secure_duckdb_connect", refuse)
    return duckdb


def _all_output(capfd, caplog):
    out, err = capfd.readouterr()
    logs = [r.getMessage() for r in caplog.records]
    logs += [caplog.text]
    # Rich wraps long lines; a password must not survive in the joined text either.
    return [out, err, "".join(out.split()), "".join(err.split())] + logs


class TestParseUriMasksThePassword:
    @pytest.mark.parametrize("kind,uri,pieces", MALFORMED, ids=IDS)
    def test_no_message_or_chained_error_carries_the_password(self, kind, uri, pieces):
        if _urllib_reads(uri):
            # A urllib without the bracketed-host check reads the bracket row,
            # and _parse_uri then has no error whose text could be checked.
            pytest.skip("this interpreter's urllib reads the URI, so nothing is refused")
        with pytest.raises(ValueError) as caught:
            _parse_uri(uri, kind)
        _assert_masked(_texts_of(caught.value), pieces)
        assert f"Invalid {kind} URI" in str(caught.value)

    def test_the_message_still_shows_the_uri_with_the_password_masked(self):
        with pytest.raises(ValueError) as caught:
            _parse_uri(f"postgres://u:{PW}@/db", "postgres")  # pragma: allowlist secret
        assert str(caught.value) == (
            "Invalid postgres URI: missing hostname. Got: 'postgres://u:***@/db'"
        )

    def test_a_bad_port_is_still_named(self):
        with pytest.raises(ValueError, match="port") as caught:
            _parse_uri(f"postgres://u:{PW}@db:notaport/db", "postgres")  # pragma: allowlist secret
        assert "postgres://u:***@db:notaport/db" in str(caught.value)

    @pytest.mark.parametrize("kind,uri,pieces", MALFORMED, ids=IDS)
    def test_no_message_advises_percent_encoding_the_password(self, kind, uri, pieces):
        # The attach does not percent-decode the user name or password, so an
        # encoded password reaches the server still encoded and is refused
        # there. A message must not send the user down that path.
        if _urllib_reads(uri):
            pytest.skip("this interpreter's urllib reads the URI, so nothing is refused")
        with pytest.raises(ValueError) as caught:
            _parse_uri(uri, kind)
        assert "encod" not in str(caught.value).lower()

    def test_many_at_signs_cost_a_bounded_number_of_readings(self):
        # An MCP client chooses the URI, and a mysql URI's error is built before
        # any operator flag is checked: one reading per '@' would make a long
        # URI cost quadratic time and memory.
        from fluid_build.cli.discover._jdbc_introspect import _uri_password_readings

        uri = "mysql://u:" + "x@" * 20_000 + "/db"
        assert len(_uri_password_readings(uri)) <= 3
        with pytest.raises(ValueError, match="missing hostname"):
            _parse_uri(uri, "mysql")

    def test_a_well_formed_uri_is_read_unchanged(self):
        uri = f"postgres://u:{PW}@db:5433/sales"  # pragma: allowlist secret
        assert _parse_uri(uri, "postgres") == {
            "host": "db",
            "port": "5433",
            "user": "u",
            "password": PW,
            "dbname": "sales",
        }


class TestRunFromJdbcSourcePrintsNoPassword:
    @pytest.mark.parametrize("kind,uri,pieces", MALFORMED, ids=IDS)
    def test_cli_path(self, kind, uri, pieces, printed, no_connect, capfd, caplog, tmp_path):
        caplog.set_level(logging.DEBUG)
        args = argparse.Namespace(
            source=kind,
            uri=uri,
            output=str(tmp_path / "contract.fluid.yaml"),
            name=None,
            schema_name=None,
            tables=None,
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("fluid.test.jdbc"))
        assert rc != 0
        assert printed, "the failure is reported"
        _assert_masked(printed + _all_output(capfd, caplog), pieces)
        assert not (tmp_path / "contract.fluid.yaml").exists()

    @pytest.mark.parametrize("error", [ValueError, RuntimeError])
    def test_any_error_that_quotes_the_uri_is_masked_when_printed(
        self, error, printed, monkeypatch, capfd, caplog, tmp_path
    ):
        # The print boundary masks the URI's password whatever raised the text,
        # so a message added later that quotes the URI cannot reach the console.
        import fluid_build.cli.discover._jdbc_introspect as introspect

        uri = f"postgres://u:{A}/{B}@db/x"  # pragma: allowlist secret

        def quote_the_uri(**_kwargs):
            raise error(f"could not use {uri}")

        monkeypatch.setattr(introspect, "introspect_jdbc", quote_the_uri)
        args = argparse.Namespace(source="postgres", uri=uri, output=str(tmp_path / "c.yaml"))
        assert _run_from_jdbc_source(args, logging.getLogger("fluid.test.jdbc")) != 0
        assert any("could not use postgres://u:***@db/x" in line for line in printed)
        _assert_masked(printed + _all_output(capfd, caplog), [A, B])


class TestMcpForgeFromSourceEchoesNoPassword:
    @pytest.mark.parametrize("kind,uri,pieces", MALFORMED, ids=IDS)
    def test_mcp_path(self, kind, uri, pieces, printed, no_connect, capfd, caplog, tmp_path):
        caplog.set_level(logging.DEBUG)
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(Exception) as caught:
            _call_tool(
                "forge_from_source",
                {"source": kind, "uri": uri, "output_path": str(out)},
                read_only=False,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        _assert_masked(_texts_of(caught.value) + printed + _all_output(capfd, caplog), pieces)
        assert not out.exists()


class TestFetchSampleRowsReturnsNoPassword:
    @pytest.mark.parametrize("kind,uri,pieces", MALFORMED, ids=IDS)
    def test_tool_result(self, kind, uri, pieces, no_connect, capfd, caplog):
        caplog.set_level(logging.DEBUG)
        result = dispatch_db_tool(
            "fetch_sample_rows", {"table": "orders"}, env={"FLUID_FORGE_DB_URI": uri}
        )
        assert result.get("error"), result
        _assert_masked([repr(result)] + _all_output(capfd, caplog), pieces)


# A URI without '://', a typo an operator can make in FLUID_FORGE_DB_URI or in
# --uri. The password may hold characters repr() escapes (both quote kinds, a
# tab), and '://' may still come later, in the query or in the password itself.
NO_SCHEME_SEPARATOR = [
    f"postgres:/u:{A}-{B}@db/app",  # pragma: allowlist secret
    f"postgres:u:{A}-{B}@db/app",  # pragma: allowlist secret
    f"u:{A}-{B}@db/app",  # pragma: allowlist secret
    f"postgresql//u:{A}-{B}@db/app",  # pragma: allowlist secret
    f"mysql:/u:{A}-{B}@db/app",  # pragma: allowlist secret
    f"postgres:/u:{A}'\"{B}@db/app",  # pragma: allowlist secret
    f"postgres:/u:{A}\t{B}@db/app",  # pragma: allowlist secret
    f"postgres:/u:{A}-{B}@db/app?application_name=x://y",  # pragma: allowlist secret
    f"postgres:/u:{A}://{B}@db/app",  # pragma: allowlist secret
]


class TestUriWithoutSchemeSeparator:
    @pytest.mark.parametrize("uri", NO_SCHEME_SEPARATOR, ids=repr)
    def test_fetch_sample_rows(self, uri, no_connect, capfd, caplog):
        caplog.set_level(logging.DEBUG)
        result = dispatch_db_tool(
            "fetch_sample_rows", {"table": "orders"}, env={"FLUID_FORGE_DB_URI": uri}
        )
        assert result.get("error") == "InvalidConnectionUri", result
        _assert_masked([repr(result), str(result)] + _all_output(capfd, caplog), [A, B])

    @pytest.mark.parametrize("uri", NO_SCHEME_SEPARATOR, ids=repr)
    def test_cli_path(self, uri, printed, no_connect, capfd, caplog, tmp_path):
        caplog.set_level(logging.DEBUG)
        args = argparse.Namespace(
            source="mysql" if uri.startswith("mysql") else "postgres",
            uri=uri,
            output=str(tmp_path / "contract.fluid.yaml"),
            name=None,
            schema_name=None,
            tables=None,
        )
        rc = _run_from_jdbc_source(
            args, logging.getLogger("fluid.test.jdbc"), operator_supplied=True
        )
        assert rc != 0
        assert printed, "the failure is reported"
        _assert_masked(printed + _all_output(capfd, caplog), [A, B])

    def test_a_later_scheme_separator_is_not_where_the_authority_starts(self):
        from fluid_build.cli.discover._jdbc_introspect import _uri_password_readings

        uri = f"postgres:/u:{A}://{B}@db/app"  # pragma: allowlist secret
        assert f"{A}://{B}" in _uri_password_readings(uri)

    def test_fetch_sample_rows_says_what_is_wrong(self, no_connect):
        uri = f"postgres:/u:{PW}@db/app?application_name=x://y"  # pragma: allowlist secret
        result = dispatch_db_tool(
            "fetch_sample_rows", {"table": "orders"}, env={"FLUID_FORGE_DB_URI": uri}
        )
        assert result == {
            "error": "InvalidConnectionUri",
            "message": (
                "Unsupported JDBC source: the URI does not start with postgres://, "
                "postgresql://, mysql:// or sqlite://. "
                "Got: 'postgres:***@db/app?application_name=x://y'"
            ),
        }


# The stock logins of the official database images: the password is the user
# name, the database name, or the source kind's own word. The password is
# masked where it sits in the URI, so the rest of the message stays readable.
STOCK_LOGINS = [
    (
        "postgres",
        "postgresql://postgres:postgres@/postgres",  # pragma: allowlist secret
        "Invalid postgres URI: missing hostname. Got: 'postgresql://postgres:***@/postgres'",
    ),
    (
        "mysql",
        "mysql://root:root@/shop",  # pragma: allowlist secret
        "Invalid mysql URI: missing hostname. Got: 'mysql://root:***@/shop'",
    ),
    (
        "postgres",
        "postgres://admin:admin@:5432/admin",  # pragma: allowlist secret
        "Invalid postgres URI: missing hostname. Got: 'postgres://admin:***@:5432/admin'",
    ),
    (
        "postgres",
        "postgresql://u:ql@/app",  # pragma: allowlist secret
        "Invalid postgres URI: missing hostname. Got: 'postgresql://u:***@/app'",
    ),
]
STOCK_IDS = ["postgres-postgres", "root-root", "admin-admin", "scheme-piece"]

# libpq also reads a password (and the other secrets) from the query string.
# Masking by position must still cover it, including the same password given
# twice, which a substring mask covered.
QUERY_SECRETS = [
    (
        f"postgres://u:{PW}@/app?password={PW}",  # pragma: allowlist secret
        "Got: 'postgres://u:***@/app?password=***'",
    ),
    (
        f"postgres://u@/app?sslmode=require&password={PW}",  # pragma: allowlist secret
        "Got: 'postgres://u@/app?sslmode=require&password=***'",
    ),
    (
        f"postgres://u@/app?sslpassword={A}&oauth_client_secret={B}",  # pragma: allowlist secret
        "Got: 'postgres://u@/app?sslpassword=***&oauth_client_secret=***'",
    ),
    (
        f"postgres://u@/app?PassWord={PW}#frag",  # pragma: allowlist secret
        "Got: 'postgres://u@/app?PassWord=***#frag'",
    ),
]


class TestPasswordEqualToAnotherPartOfTheUri:
    @pytest.mark.parametrize("kind,uri,expected", STOCK_LOGINS, ids=STOCK_IDS)
    def test_parse_uri(self, kind, uri, expected):
        with pytest.raises(ValueError) as caught:
            _parse_uri(uri, kind)
        assert str(caught.value) == expected

    @pytest.mark.parametrize("kind,uri,expected", STOCK_LOGINS, ids=STOCK_IDS)
    def test_cli_path(self, kind, uri, expected, printed, no_connect, tmp_path):
        args = argparse.Namespace(
            source=kind,
            uri=uri,
            output=str(tmp_path / "contract.fluid.yaml"),
            name=None,
            schema_name=None,
            tables=None,
        )
        rc = _run_from_jdbc_source(
            args, logging.getLogger("fluid.test.jdbc"), operator_supplied=True
        )
        assert rc == 2
        assert printed == [f"[red]JDBC introspection failed:[/red] {expected}"]

    @pytest.mark.parametrize("kind,uri,expected", STOCK_LOGINS, ids=STOCK_IDS)
    def test_fetch_sample_rows(self, kind, uri, expected, no_connect):
        result = dispatch_db_tool(
            "fetch_sample_rows", {"table": "orders"}, env={"FLUID_FORGE_DB_URI": uri}
        )
        assert result == {"error": "InvalidConnectionUri", "message": expected}

    def test_a_driver_error_is_still_masked_wherever_the_password_appears(
        self, printed, monkeypatch, tmp_path
    ):
        # A message that is not the URI refusal is not read by position: the
        # password's text is masked wherever it occurs.
        import fluid_build.cli.discover._jdbc_introspect as introspect

        uri = f"postgres://u:{PW}@db/x"  # pragma: allowlist secret

        def quote_it(**_kwargs):
            raise RuntimeError(f"server said: {PW} rejected")

        monkeypatch.setattr(introspect, "introspect_jdbc", quote_it)
        args = argparse.Namespace(source="postgres", uri=uri, output=str(tmp_path / "c.yaml"))
        assert _run_from_jdbc_source(args, logging.getLogger("fluid.test.jdbc")) == 1
        assert printed == ["[red]JDBC introspection failed:[/red] server said: *** rejected"]


class TestQueryStringSecrets:
    @pytest.mark.parametrize(
        "uri,echo", QUERY_SECRETS, ids=["twice", "query-only", "ssl-oauth", "case"]
    )
    def test_parse_uri(self, uri, echo):
        with pytest.raises(ValueError) as caught:
            _parse_uri(uri, "postgres")
        assert str(caught.value) == f"Invalid postgres URI: missing hostname. {echo}"
        _assert_masked(_texts_of(caught.value), [PW, A, B])
