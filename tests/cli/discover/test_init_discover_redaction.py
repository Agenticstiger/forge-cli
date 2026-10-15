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

"""``fluid init --discover`` keeps the connection password out of every output.

The password in a ``--discover`` URI must not reach the console, the log, a
typed error (human or JSON form) or the emitted contracts, which are meant to
be committed. The contracts reference the password through ``secretRef``
instead.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterator, List, Optional
from unittest import mock
from urllib.parse import quote, urlparse, urlsplit

import pytest
import yaml
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

import fluid_build.cli._init_discover_handler as handler
from fluid_build.cli._errors import ConnectivityProbeError, SchemaValidationError
from fluid_build.cli.discover.registry import DiscoveredColumn, DiscoveredStream
from fluid_build.observability.secret_redactor import forget_known_secrets, redact_secret_text
from fluid_build.providers._sql_safety import libpq_escape, quote_ansi_string_literal
from fluid_build.schema_manager import FluidSchemaManager

SENTINEL = "S3CRET-sentinel"


class _StubDiscoverer:
    """Returns one stream, or raises ``error`` the way a failed connect does."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.seen: List[str] = []

    def discover(self, uri: str) -> List[DiscoveredStream]:
        self.seen.append(uri)
        if self.error is not None:
            raise self.error
        return [
            DiscoveredStream(
                name="public.orders",
                columns=[DiscoveredColumn(name="id", type="bigint", nullable=False)],
            )
        ]


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    # Every test starts with an empty exact-match registry, so a pass proves
    # the handler protects the password itself rather than relying on a
    # registration some earlier test made.
    forget_known_secrets()
    yield
    forget_known_secrets()


def _args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(target_dir=str(tmp_path), name=None)


def _run(monkeypatch, tmp_path: Path, uri: str, stub: _StubDiscoverer) -> int:
    monkeypatch.setattr(handler, "get_discoverer", lambda _uri: stub)
    return handler.run_discover(_args(tmp_path), logging.getLogger("fluid.test.discover"), uri=uri)


def _contracts(tmp_path: Path) -> List[Path]:
    return sorted(tmp_path.glob("contract.*.fluid.yaml"))


def _connection(path: Path) -> dict:
    # The emitter puts every stream in one build; unpacking asserts that.
    (build,) = yaml.safe_load(path.read_text(encoding="utf-8"))["builds"]
    source = build["properties"]["source"]
    return source["connection"]


def _unwrapped(text: str) -> str:
    """``text`` without whitespace, so a URI the console wrapped reads whole again."""
    return "".join(text.split())


def _init_help(monkeypatch, tmp_path: Path, capsys) -> str:
    """The rendered ``fluid init --help``, with the console's line wrapping undone."""
    from fluid_build.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert main(["init", "--help"]) == 0
    return " ".join(capsys.readouterr().out.split())


def _hint(out: str) -> str:
    """The credential hint line, with the console's line wrapping undone."""
    return " ".join(out[out.index("🔑") :].split())


def _stub_remote_columns(monkeypatch) -> None:
    """Stub only the remote column fetch; the real filesystem discoverer names the stream."""
    import fluid_build.cli.discover.filesystem as filesystem

    monkeypatch.setattr(
        filesystem,
        "_columns_for",
        lambda _path, _fmt: [DiscoveredColumn(name="id", type="bigint", nullable=False)],
    )


def _everything_written(root: Path) -> str:
    """The name and content of every file under ``root``."""
    return "\n".join(
        f"{path.relative_to(root)}\n{path.read_text(encoding='utf-8')}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


class TestDiscoverSuccessKeepsPasswordOut:
    @pytest.mark.parametrize(
        ("scheme", "env_var"),
        [("postgres", "PGPASSWORD"), ("postgresql", "PGPASSWORD"), ("mysql", "MYSQL_PWD")],
    )
    def test_password_absent_and_contract_uses_secret_ref(
        self, monkeypatch, tmp_path, capsys, caplog, scheme, env_var
    ):
        uri = f"{scheme}://alice:{SENTINEL}@127.0.0.1:1/shop"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, stub) == 0
        out, err = capsys.readouterr()

        # The discoverer still receives the real URI; only what is shown changes.
        assert stub.seen == [uri]
        assert SENTINEL not in out
        assert SENTINEL not in err
        assert SENTINEL not in caplog.text
        # The echo keeps everything but the password, so the target is recognisable.
        assert f"{scheme}://alice:***REDACTED***@127.0.0.1:1/shop" in out
        # One line tells the user how to supply the password at run time.
        assert env_var in out

        written = _contracts(tmp_path)
        assert written, "no contract emitted"
        for path in written:
            assert SENTINEL not in path.read_text(encoding="utf-8")
            conn = _connection(path)
            assert "password" not in conn
            assert conn["secretRef"] == f"env://{env_var}"
            assert conn["user"] == "alice"

    def test_emitted_contract_validates(self, monkeypatch, tmp_path):
        _run(monkeypatch, tmp_path, f"postgres://alice:{SENTINEL}@db/shop", _StubDiscoverer())
        doc = yaml.safe_load(_contracts(tmp_path)[0].read_text(encoding="utf-8"))
        result = FluidSchemaManager().validate_contract(doc, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors

    def test_no_password_in_uri_means_no_secret_ref_and_no_hint(
        self, monkeypatch, tmp_path, capsys
    ):
        _run(monkeypatch, tmp_path, "postgres://alice@db/shop", _StubDiscoverer())
        out, _ = capsys.readouterr()
        conn = _connection(_contracts(tmp_path)[0])
        assert "secretRef" not in conn
        assert "password" not in conn
        assert "PGPASSWORD" not in out

    def test_short_password_with_raw_at_sign_is_fully_masked(self, monkeypatch, tmp_path, capsys):
        # Too short for the exact-match registry, and the unencoded ``@`` would
        # end a pattern match early; the echo must still show none of it.
        uri = "postgres://alice:ab@cd@127.0.0.1:1/shop"
        _run(monkeypatch, tmp_path, uri, _StubDiscoverer())
        out, _ = capsys.readouterr()
        assert "ab@cd" not in out
        assert "@cd@" not in out
        assert "cd@127.0.0.1" not in out
        assert "alice:***REDACTED***@127.0.0.1:1/shop" in out

    def test_url_form_connection_drops_userinfo(self, monkeypatch, tmp_path, capsys):
        uri = f"https://alice:{SENTINEL}@files.example.com/data/orders.csv?v=2"
        _run(monkeypatch, tmp_path, uri, _StubDiscoverer())
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        conn = _connection(_contracts(tmp_path)[0])
        assert conn["uri"] == "https://files.example.com/data/orders.csv"
        for path in _contracts(tmp_path):
            text = path.read_text(encoding="utf-8")
            assert SENTINEL not in text
            assert "alice" not in text


class TestDiscoverErrorsKeepPasswordOut:
    def test_unreachable_host_error_is_redacted(self, monkeypatch, tmp_path, capsys):
        uri = f"postgres://alice:{SENTINEL}@127.0.0.1:1/shop"
        # Driver errors commonly echo the URI or the libpq DSN they were given.
        stub = _StubDiscoverer(
            error=RuntimeError(
                f"could not connect to {uri} (dsn: host=127.0.0.1 port=1 user=alice "
                f"password={SENTINEL} dbname=shop)"
            )
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        exc = info.value
        assert SENTINEL not in out + err
        assert SENTINEL not in exc.what
        assert SENTINEL not in (exc.why or "")
        assert SENTINEL not in exc.render(color=False)
        assert SENTINEL not in exc.as_json()
        # The target is still named, so the user knows what failed.
        assert "127.0.0.1:1/shop" in exc.what

    def test_unsupported_scheme_error_is_redacted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(handler, "get_discoverer", lambda _uri: None)
        uri = f"foo://alice:{SENTINEL}@h/x"
        with pytest.raises(SchemaValidationError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri)
        exc = info.value
        assert SENTINEL not in exc.what
        assert SENTINEL not in json.dumps(exc.extras, default=str)
        assert SENTINEL not in exc.render(color=False)
        assert SENTINEL not in exc.as_json()
        # Outside the database schemes the user name can be the token, so the
        # whole user:password part is masked.
        assert "foo://***REDACTED***@h/x" in exc.what

    def test_error_quoting_the_uri_masks_a_short_password_with_raw_at_sign(
        self, monkeypatch, tmp_path
    ):
        uri = "postgres://alice:ab@cd@127.0.0.1:1/shop"
        stub = _StubDiscoverer(error=RuntimeError(f"could not connect to {uri}"))
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        assert "ab@cd" not in info.value.as_json()
        assert "cd@127.0.0.1" not in info.value.as_json()


class TestPasswordWithUnencodedDelimiter:
    """A password holding a raw ``/``, ``?`` or ``#`` ends the RFC 3986 authority early.

    The URI is then refused before any driver sees it, and no part of the
    password is shown.
    """

    @pytest.mark.parametrize("delimiter", ["/", "?", "#"])
    @pytest.mark.parametrize("scheme", ["postgres", "mysql", "https"])
    def test_refused_and_never_shown(
        self, monkeypatch, tmp_path, capsys, caplog, delimiter, scheme
    ):
        uri = f"{scheme}://alice:{SENTINEL}{delimiter}tail-part9@127.0.0.1:1/shop"  # pragma: allowlist secret
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        exc = info.value
        # No driver is handed a URI whose host and port it would misread.
        assert stub.seen == []
        assert not _contracts(tmp_path)
        shown = "\n".join(
            [out, err, caplog.text, exc.what, exc.why or "", exc.render(color=False), exc.as_json()]
        )
        for fragment in (SENTINEL, "tail-part9"):
            assert fragment not in shown
        # A database URI keeps its user name; any other URI masks the whole
        # user:password part. After a raw "?" or "#" the rest may be a query or
        # fragment, so it is masked or left out too, host included.
        userinfo = "alice:***REDACTED***" if scheme != "https" else "***REDACTED***"
        rest = "@127.0.0.1:1/shop" if delimiter == "/" else ""
        assert exc.what.endswith(f"URI: {scheme}://{userinfo}{rest}"), exc.what

    def test_user_name_with_raw_at_sign_still_works(self, monkeypatch, tmp_path, capsys):
        uri = f"postgres://alice@corp.example:{SENTINEL}@127.0.0.1:1/shop"
        assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, _ = capsys.readouterr()
        assert SENTINEL not in out
        assert "postgres://alice@corp.example:***REDACTED***@127.0.0.1:1/shop" in out
        conn = _connection(_contracts(tmp_path)[0])
        assert conn["user"] == "alice@corp.example"
        assert conn["host"] == "127.0.0.1"


_SAS_URI = (
    "https://acct.blob.core.windows.net/c/orders.csv"
    f"?sv=2022-11-02&se=2030-01-01&sp=rl&sig={SENTINEL}"
)
_PRESIGNED_URI = (
    "https://bucket.s3.amazonaws.com/orders.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIDEXAMPLE%2F20300101%2Fus-east-1%2Fs3%2Faws4_request"
    f"&X-Amz-Signature={SENTINEL}&X-Amz-Security-Token=TOKEN-{SENTINEL}"
)


class TestQueryCredentialsKeptOut:
    @pytest.mark.parametrize(
        ("uri", "base", "dropped"),
        [
            (
                _SAS_URI,
                "https://acct.blob.core.windows.net/c/orders.csv",
                ["sv", "se", "sp", "sig"],
            ),
            (
                _PRESIGNED_URI,
                "https://bucket.s3.amazonaws.com/orders.csv",
                [
                    "X-Amz-Algorithm",
                    "X-Amz-Credential",
                    "X-Amz-Signature",
                    "X-Amz-Security-Token",
                ],
            ),
        ],
    )
    def test_signed_url_credentials_absent_everywhere(
        self, monkeypatch, tmp_path, capsys, caplog, uri, base, dropped
    ):
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        assert SENTINEL not in caplog.text
        assert "AKIDEXAMPLE" not in out + err + caplog.text
        for path in _contracts(tmp_path):
            assert SENTINEL not in path.read_text(encoding="utf-8")
        assert _connection(_contracts(tmp_path)[0])["uri"] == base
        # The hint names what was left out (names only, never values).
        for name in dropped:
            assert name in out

    def test_driver_error_quoting_a_signed_url_is_masked(self, monkeypatch, tmp_path):
        stub = _StubDiscoverer(
            error=RuntimeError(f"IO Error: LINE 1: SELECT * FROM read_csv_auto('{_SAS_URI}')")
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, _SAS_URI, stub)
        exc = info.value
        assert SENTINEL not in exc.what
        assert SENTINEL not in (exc.why or "")
        assert SENTINEL not in exc.as_json()
        assert "sig=***REDACTED***" in exc.what

    def test_every_query_parameter_is_left_out_and_named(self, monkeypatch, tmp_path, capsys):
        # No parameter name is trusted to be harmless: the contract keeps none,
        # the echo masks every value, and the hint names each one left out.
        uri = "https://files.example.com/orders.csv?v=2&format=csv"
        _run(monkeypatch, tmp_path, uri, _StubDiscoverer())
        out, _ = capsys.readouterr()
        assert "files.example.com/orders.csv?v=***REDACTED***&format=***REDACTED***" in _unwrapped(
            out
        )
        assert _connection(_contracts(tmp_path)[0])["uri"] == "https://files.example.com/orders.csv"
        hint = _hint(out)
        assert "query parameter(s) v, format" in hint
        assert "secretRef" in hint


class TestPasswordThatIsAlsoAnOrdinaryWord:
    def test_stock_dev_credentials_mask_only_the_password(self, monkeypatch, tmp_path, capsys):
        uri = "postgres://postgres:postgres@db.example/postgres"  # pragma: allowlist secret
        _run(monkeypatch, tmp_path, uri, _StubDiscoverer())
        out, _ = capsys.readouterr()
        assert "postgres://postgres:***REDACTED***@db.example/postgres" in out
        # The word is not turned into a process-wide mask for every later line.
        assert redact_secret_text("kind=postgres db failed") == "kind=postgres db failed"


class TestUserOnlyUserinfoIsACredential:
    """``https://TOKEN@host/...`` carries a token as the user name (git and HTTP style).

    Outside the database schemes, a user name with no password is masked
    whole, the way pip's ``redact_auth_from_url`` does.
    """

    @pytest.mark.parametrize("scheme", ["https", "http", "s3", "HTTPS"])
    def test_token_absent_from_console_log_and_contract(
        self, monkeypatch, tmp_path, capsys, caplog, scheme
    ):
        uri = f"{scheme}://{SENTINEL}@files.example.com/data/orders.csv"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, stub) == 0
        out, err = capsys.readouterr()
        assert stub.seen == [uri]
        assert SENTINEL not in out + err
        assert SENTINEL not in caplog.text
        assert f"{scheme}://***REDACTED***@files.example.com/data/orders.csv" in out
        for path in _contracts(tmp_path):
            assert SENTINEL not in path.read_text(encoding="utf-8")
        # The hint says what was left out, without calling the token a password.
        assert "the URI's user name" in out

    def test_token_with_empty_password_is_masked(self, monkeypatch, tmp_path, capsys):
        uri = f"https://{SENTINEL}:@files.example.com/orders.csv"
        _run(monkeypatch, tmp_path, uri, _StubDiscoverer())
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        assert "https://***REDACTED***@files.example.com/orders.csv" in out

    def test_connect_error_masks_the_token(self, monkeypatch, tmp_path, capsys):
        uri = f"https://{SENTINEL}@127.0.0.1:1/orders.csv"
        stub = _StubDiscoverer(
            error=RuntimeError(f"IO Error: Could not establish connection to {uri} ({SENTINEL})")
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        exc = info.value
        shown = "\n".join(
            [out, err, exc.what, exc.why or "", exc.render(color=False), exc.as_json()]
        )
        assert SENTINEL not in shown
        assert "https://***REDACTED***@127.0.0.1:1/orders.csv" in exc.what

    def test_database_user_name_is_still_shown(self, monkeypatch, tmp_path, capsys):
        # A database user name is not a credential: libpq and MySQL read the
        # password separately, and the contract keeps the user.
        _run(monkeypatch, tmp_path, "postgres://alice@db.example/shop", _StubDiscoverer())
        out, _ = capsys.readouterr()
        assert "postgres://alice@db.example/shop" in out
        assert _connection(_contracts(tmp_path)[0])["user"] == "alice"


class TestStreamNameNeverCarriesTheQuery:
    """The filesystem discoverer names a remote stream after the URL's last segment.

    The query string is not part of that name: a credential in it must not
    reach the contract's name, its stream list, the file name or the console.
    """

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            (f"https://api.example.test/export?api_key={SENTINEL}&file=orders.csv", "export"),
            (
                f"https://acct.blob.core.windows.net/c/data?sp=r&sig={SENTINEL}&sip=10.0.0.1",
                "data",
            ),
            (f"https://files.example.test/x.csv?sig=ab/{SENTINEL}", "x"),
            (f"https://files.example.test/orders?token={SENTINEL}#part.csv", "orders"),
        ],
    )
    def test_credential_query_absent_from_every_emitted_file(
        self, monkeypatch, tmp_path, capsys, caplog, uri, expected
    ):
        import fluid_build.cli.discover.filesystem as filesystem

        # Only the remote column fetch is stubbed; the real discoverer names the stream.
        monkeypatch.setattr(
            filesystem,
            "_columns_for",
            lambda _path, _fmt: [DiscoveredColumn(name="id", type="bigint", nullable=False)],
        )
        args = SimpleNamespace(target_dir=str(tmp_path), name="feed")
        with caplog.at_level(logging.DEBUG):
            assert handler.run_discover(args, logging.getLogger("t"), uri=uri) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err + caplog.text
        written = _contracts(tmp_path)
        assert [p.name for p in written] == [f"contract.{expected}.fluid.yaml"]
        for path in written:
            assert SENTINEL not in path.name
            text = path.read_text(encoding="utf-8")
            assert SENTINEL not in text
            doc = yaml.safe_load(text)
            (build,) = doc["builds"]
            assert build["properties"]["source"]["streams"] == [expected]
            assert doc["name"] == f"feed / {expected}"

    def test_local_file_stream_name_is_unchanged(self, monkeypatch, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "orders.v2.csv").write_text("id\n1\n", encoding="utf-8")
        out_dir = tmp_path / "out"
        args = SimpleNamespace(target_dir=str(out_dir), name="feed")
        assert handler.run_discover(args, logging.getLogger("t"), uri=str(src)) == 0
        (path,) = sorted(out_dir.glob("contract.*.fluid.yaml"))
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        (build,) = doc["builds"]
        assert build["properties"]["source"]["streams"] == ["orders.v2"]

    def test_stream_name_quoting_a_masked_value_is_masked(self, monkeypatch, tmp_path, capsys):
        # Whatever a discoverer names its stream, a credential the URI carried
        # does not reach the contract through that name.
        class _Echoing(_StubDiscoverer):
            def discover(self, uri: str) -> List[DiscoveredStream]:
                return [DiscoveredStream(name=f"orders_{SENTINEL}", columns=[])]

        uri = f"https://files.example.test/orders.csv?sig={SENTINEL}"
        assert _run(monkeypatch, tmp_path, uri, _Echoing()) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        (path,) = _contracts(tmp_path)
        assert path.name == "contract.orders_redacted.fluid.yaml"
        assert SENTINEL not in path.read_text(encoding="utf-8")


# No parameter name, and no shape of scheme, is trusted to say where a
# credential is. The contracts keep no query string and no fragment at all; the
# echo, the log and every error mask every query value, leave out the fragment,
# and mask whatever a parser could read as user:password.

_BASE = "https://files.example.test/orders.csv"


class TestNoQueryOrFragmentInContracts:
    @pytest.mark.parametrize(
        ("uri", "names", "fragment"),
        [
            # Credential names outside any fixed list.
            (f"{_BASE}?key={SENTINEL}", ["key"], False),
            (f"{_BASE}?api-key={SENTINEL}", ["api-key"], False),
            (f"{_BASE}?x-api-key={SENTINEL}", ["x-api-key"], False),
            (f"{_BASE}?pwd={SENTINEL}", ["pwd"], False),
            (f"{_BASE}?code={SENTINEL}", ["code"], False),
            # A ";" separator: the credential sits inside the value of "a".
            (f"{_BASE}?a=1;token={SENTINEL}", ["a"], False),
            (f"{_BASE}?v=2#k={SENTINEL}", ["v"], True),
            (f"{_BASE}#{SENTINEL}", [], True),
            # A bare value has no name to show.
            (f"{_BASE}?{SENTINEL}", ["(unnamed)"], False),
            # An "@" in the query is not the end of a user:password part. Read as
            # one, the display masks "mail", and so does the closing line.
            (f"{_BASE}?mail=ops@example.test&code={SENTINEL}", [handler._MASK, "code"], False),
            # Names earlier rounds already recognised.
            (f"{_BASE}?api_key={SENTINEL}", ["api_key"], False),
            (f"{_BASE}?sv=2022-11-02&sig={SENTINEL}", ["sv", "sig"], False),
            (f"{_BASE}?token={SENTINEL}#part.csv", ["token"], True),
        ],
    )
    def test_query_and_fragment_never_reach_any_output(
        self, monkeypatch, tmp_path, capsys, caplog, uri, names, fragment
    ):
        _stub_remote_columns(monkeypatch)
        args = SimpleNamespace(target_dir=str(tmp_path), name="feed")
        with caplog.at_level(logging.DEBUG):
            assert handler.run_discover(args, logging.getLogger("t"), uri=uri) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err + caplog.text
        assert SENTINEL not in _everything_written(tmp_path)
        (path,) = _contracts(tmp_path)
        assert path.name == "contract.orders.fluid.yaml"
        assert _connection(path) == {"uri": _BASE}
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["builds"][0]["properties"]["source"]["streams"] == ["orders"]
        # The hint names every parameter left out, and only its name.
        hint = _hint(out)
        if names:
            assert f"query parameter(s) {', '.join(names)}" in hint
        assert ("#fragment" in hint) is fragment
        assert "secretRef" in hint

    def test_driver_error_quoting_the_uri_masks_every_query_value(self, monkeypatch, tmp_path):
        uri = f"{_BASE}?key={SENTINEL}"
        stub = _StubDiscoverer(
            error=RuntimeError(f"HTTP 403 for {uri} (the server rejected key {SENTINEL})")
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        exc = info.value
        for shown in (exc.what, exc.why or "", exc.render(color=False), exc.as_json()):
            assert SENTINEL not in shown
        assert exc.what.endswith(f"{_BASE}?key=***REDACTED***"), exc.what


class TestRemoteStreamIsNamedAfterTheUrlPath:
    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            (f"https://h.example.test/export?a=1;token={SENTINEL}", "export"),
            (f"https://h.example.test/x?p=a/b/{SENTINEL}.csv", "x"),
            (f"https://h.example.test/data#frag/{SENTINEL}.csv", "data"),
            (f"https://h.example.test/orders.csv#{SENTINEL}", "orders"),
            (f"s3://bucket/dir/orders.parquet?versionId={SENTINEL}", "orders"),
        ],
    )
    def test_stream_name_has_no_part_of_the_query_or_fragment(self, monkeypatch, uri, expected):
        from fluid_build.cli.discover.filesystem import FilesystemDiscoverer

        _stub_remote_columns(monkeypatch)
        (stream,) = FilesystemDiscoverer().discover(uri)
        assert stream.name == expected

    def test_format_is_read_from_the_url_path(self, monkeypatch):
        from fluid_build.cli.discover import filesystem

        seen: List[str] = []
        monkeypatch.setattr(filesystem, "_columns_for", lambda _path, fmt: seen.append(fmt) or [])
        filesystem.FilesystemDiscoverer().discover("s3://bucket/dir/orders.parquet?versionId=7")
        assert seen == ["parquet"]


class TestDisplayDoesNotTrustTheUriGrammar:
    """The user:password part is masked whatever the scheme looks like."""

    @pytest.mark.parametrize(
        ("uri", "shown"),
        [
            # Not an RFC 3986 scheme (a ":" in it), and a "/" in the password: read
            # as the location "jdbc:" followed by a URI, masked to the last "@".
            (
                f"jdbc:postgresql://alice:hd7/{SENTINEL}@db.example.test/shop",
                "jdbc:***REDACTED***@db.example.test/shop",
            ),
            # A "_" in the scheme, with a "/" or a space in the password.
            (
                f"my_db://alice:hd7/{SENTINEL}@db.example.test/shop",
                "my_db:***REDACTED***@db.example.test/shop",
            ),
            (
                f"my_db://alice:hd7 {SENTINEL}@db.example.test/shop",
                "my_db:***REDACTED***@db.example.test/shop",
            ),
            # A "/" in the user name and a "#" in the password: what follows the
            # "#" may be a fragment, so it is left out, host included.
            (f"foo://corp/alice:hd7#{SENTINEL}@db.example.test/shop", "foo://***REDACTED***"),
            # Every query value, whatever its name.
            (
                f"foo://alice:{SENTINEL}@db.example.test/shop?key={SENTINEL}-2",
                "foo://***REDACTED***@db.example.test/shop?key=***REDACTED***",
            ),
            # A token as the user name.
            (f"foo://{SENTINEL}@db.example.test/shop", "foo://***REDACTED***@db.example.test/shop"),
        ],
    )
    def test_unsupported_scheme_error_shows_no_credential(self, tmp_path, uri, shown):
        # The real registry: none of these schemes has a discoverer.
        with pytest.raises(SchemaValidationError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri)
        exc = info.value
        for text in (
            exc.what,
            exc.why or "",
            exc.render(color=False),
            exc.as_json(),
            json.dumps(exc.extras, default=str),
        ):
            assert SENTINEL not in text
            assert "hd7" not in text
        assert exc.what.endswith(f"URI: {shown}"), exc.what
        assert exc.extras["uri"] == shown

    @pytest.mark.parametrize("scheme", ["postgres", "mysql"])
    @pytest.mark.parametrize(
        "password",
        [SENTINEL, f"ab/{SENTINEL}", f"ab#{SENTINEL}", f"ab?{SENTINEL}", f"ab {SENTINEL}"],
    )
    def test_database_user_name_with_a_slash_is_refused_and_never_shown(
        self, monkeypatch, tmp_path, capsys, caplog, scheme, password
    ):
        # URI parsers end the user:password part at the "/" in the user name and
        # read the password as part of the database name. A database URI is
        # refused before any driver sees it. A URL with such a user name is used
        # as given (TestColonBeforeAnAtSignAfterTheHost): it cannot be told apart
        # from an object key holding a ":" and an "@".
        uri = f"{scheme}://corp/alice:{password}@127.0.0.1:1/shop"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        exc = info.value
        assert stub.seen == []
        assert not _contracts(tmp_path)
        shown = "\n".join(
            [out, err, caplog.text, exc.what, exc.why or "", exc.render(color=False), exc.as_json()]
        )
        assert SENTINEL not in shown
        assert "cannot tell the password from the host" in exc.what

    def test_query_values_after_an_at_sign_behind_a_port_are_refused_and_masked(
        self, monkeypatch, tmp_path, capsys, caplog
    ):
        # The "@" is in the query, and the ":" before it may be a port's or a
        # password's: refused. The refusal masks the code value all the same.
        uri = f"https://files.example.test:8443/orders.csv?mail=ops@example.test&code={SENTINEL}"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        assert stub.seen == []
        assert SENTINEL not in _everything_shown(info.value, out, err, caplog.text)
        assert "&code=***REDACTED***" in info.value.what, info.value.what
        assert not list(tmp_path.iterdir())

    @pytest.mark.parametrize("scheme", ["postgres", "mysql"])
    def test_database_password_with_a_space_is_masked_and_kept_out(
        self, monkeypatch, tmp_path, capsys, scheme
    ):
        uri = f"{scheme}://alice:ab {SENTINEL}@127.0.0.1:1/shop"
        assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        assert f"{scheme}://alice:***REDACTED***@127.0.0.1:1/shop" in _unwrapped(out)
        assert SENTINEL not in _everything_written(tmp_path)
        assert "secretRef" in _connection(_contracts(tmp_path)[0])

    def test_unsupported_scheme_through_the_real_cli(self, monkeypatch, tmp_path, capsys):
        from fluid_build.cli import main

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        uri = f"jdbc:postgresql://alice:hd7/{SENTINEL}@db.example.test/shop"
        out_dir = tmp_path / "out"
        rc = main(["init", "d", "--discover", uri, "--yes", "--dir", str(out_dir)])
        out, err = capsys.readouterr()
        assert rc == 1
        assert SENTINEL not in out + err
        assert "hd7" not in out + err
        assert "***REDACTED***" in err
        assert not out_dir.exists() or SENTINEL not in _everything_written(out_dir)


class TestNoUriShapeRevealsACredential:
    """Whatever characters the user name, password, path and query hold.

    The markers bracket the credential: the display masks contiguous spans, so
    neither marker shown means none of what lies between them is shown either.
    """

    @settings(max_examples=400, deadline=None)
    @given(
        scheme=st.sampled_from(
            ["postgres", "mysql", "https", "s3", "jdbc:postgresql", "my_db", "pg+psycopg"]
        ),
        user=st.text(alphabet="ab/:@?# ", max_size=6),
        secret=st.text(alphabet="xy/:@?#&=; %'\"\\", max_size=10),
        tail=st.sampled_from(
            ["", "/shop", "/f.csv?v=2", "/f.csv?mail=a@b.example&x=1", "/p#frag", ":1/db?a=b@c"]
        ),
    )
    def test_password_is_never_shown(self, scheme, user, secret, tail):
        forget_known_secrets()
        view = handler._inspect_uri(f"{scheme}://{user}:PW{secret}END@db.example.test{tail}")
        assert "PW" not in view.safe and "END" not in view.safe, view.safe

    @settings(max_examples=400, deadline=None)
    @given(
        base=st.sampled_from(
            [
                "https://files.example.test/orders.csv",
                "s3://bucket/k.parquet",
                "https://u:p@h.example.test/f.csv",
                "https://h.example.test/f.csv?a=1;",
                "https://h.example.test/f.csv?mail=a@b.example&",
            ]
        ),
        name=st.text(alphabet="abk-_", min_size=1, max_size=5),
        value=st.text(alphabet="xy/:@?#=; %&'\"\\", max_size=10),
    )
    def test_query_value_is_never_shown_or_written(self, base, name, value):
        forget_known_secrets()
        uri = f"{base}{'&' if '?' in base else '?'}{name}=QV{value}QE"
        view = handler._inspect_uri(uri)
        assert "QV" not in view.safe and "QE" not in view.safe, view.safe
        if not view.ambiguous:
            connection, names, _fragment = handler._connection_from_uri(urlparse(uri))
            assert "QV" not in connection["uri"] and "QE" not in connection["uri"]
            assert not any("QV" in n or "QE" in n for n in names)


# How a driver may print back the URI it was given. DuckDB quotes it as given
# ("HTTP HEAD to '...'") and as the SQL string literal it parsed, quotes doubled
# ("LINE 1:"); the DuckDB sandbox's errors use repr(); a JSON document escapes
# it; an HTTP client re-encodes what a URL may not hold.
_URI_QUOTINGS: Dict[str, Callable[[str], str]] = {
    "as given": lambda s: s,
    "SQL literal": lambda s: quote_ansi_string_literal(s)[1:-1],
    "repr": lambda s: repr(s)[1:-1],
    "JSON": lambda s: json.dumps(s)[1:-1],
    "percent-encoded": lambda s: quote(s, safe="!#$%&'()*+,/:;=?@[]~"),
}

# How a driver may print back the password alone: the Postgres and MySQL
# extensions quote their libpq DSN (a quote or backslash escaped with a
# backslash), which the discoverer embeds in a SQL literal.
_VALUE_QUOTINGS: Dict[str, Callable[[str], str]] = {
    "libpq DSN": libpq_escape,
    "libpq DSN in a SQL literal": lambda s: quote_ansi_string_literal(libpq_escape(s)),
    "SQL literal": quote_ansi_string_literal,
    "repr": repr,
    "JSON": json.dumps,
    "percent-encoded": lambda s: quote(s, safe=""),
}


# 49 characters, none repeated in any 8-character window of the URIs below.
_LONG_VALUE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvw"  # pragma: allowlist secret


def _pieces(value: str, size: int = 8) -> List[str]:
    return [value[i : i + size] for i in range(len(value) - size + 1)]


def _shown_pieces(text: str, value: str) -> List[str]:
    return [piece for piece in _pieces(value) if piece in text]


class TestDriverErrorQuotingATruncatedUri:
    """A driver may quote the URI cut short (DuckDB: ``'<prefix>...``).

    A cut-short copy does not contain the whole masked value, so matching the
    value does not find it; the copy must be masked by where it sits in the URI.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            f"http://h.invalid/a.csv?sig={_LONG_VALUE}",
            f"https://h.invalid/a.csv?X-Amz-Signature={_LONG_VALUE}&X-Amz-Expires=60",
            f"https://{_LONG_VALUE}@h.invalid/a.csv",
            f"https://tok:{_LONG_VALUE}@h.invalid/a.csv",
            f"postgres://alice:{_LONG_VALUE}@127.0.0.1:1/shop",
            f"https://h.invalid/a.csv#{_LONG_VALUE}",
        ],
    )
    def test_no_part_of_the_credential_is_shown_at_any_cut(self, monkeypatch, tmp_path, uri):
        for cut in range(len("http://"), len(uri)):
            forget_known_secrets()
            stub = _StubDiscoverer(
                error=RuntimeError(
                    "Catalog Error: requires the extension httpfs to be loaded\n\n"
                    f"LINE 1: SELECT * FROM read_csv_auto('{uri[:cut]}...\n"
                    "                       ^"
                )
            )
            with pytest.raises(ConnectivityProbeError) as info:
                _run(monkeypatch, tmp_path, uri, stub)
            exc = info.value
            shown = exc.render(color=False) + exc.as_json()
            assert _shown_pieces(shown, _LONG_VALUE) == [], (cut, exc.why)

    def test_the_target_is_still_named_up_to_the_credential(self, monkeypatch, tmp_path):
        uri = f"http://h.invalid/a.csv?sig={_LONG_VALUE}"
        stub = _StubDiscoverer(
            error=RuntimeError(f"LINE 1: SELECT * FROM read_csv_auto('{uri[:45]}...")
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        assert "read_csv_auto('http://h.invalid/a.csv?sig=***REDACTED***..." in (
            info.value.why or ""
        )

    def test_a_whole_copy_followed_by_text_keeps_every_parameter_name(self, monkeypatch, tmp_path):
        uri = f"https://h.invalid/a.csv?sig={_LONG_VALUE}&sv=2024"
        stub = _StubDiscoverer(error=RuntimeError(f"HTTP GET error on '{uri}' (HTTP 403)"))
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        why = info.value.why or ""
        assert "'https://h.invalid/a.csv?sig=***REDACTED***&sv=***REDACTED***' (HTTP 403)" in why

    def test_a_reencoded_copy_is_masked_from_where_it_differs(self, monkeypatch, tmp_path):
        uri = f"https://h.invalid/a.csv?sig=ab%2F{_LONG_VALUE}"
        copy = f"https://h.invalid/a.csv?sig=ab/{_LONG_VALUE}"
        stub = _StubDiscoverer(error=RuntimeError(f"could not open '{copy}'"))
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        assert _shown_pieces(info.value.as_json(), _LONG_VALUE) == []

    def test_real_cli_and_duckdb_failed_read(self, monkeypatch, tmp_path, capsys):
        pytest.importorskip("duckdb")
        from fluid_build.cli import main

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        out_dir = tmp_path / "out"
        uri = f"http://h.invalid/a.csv?sig={_LONG_VALUE}"
        rc = main(["init", "p", "--discover", uri, "--yes", "--dir", str(out_dir)])
        out, err = capsys.readouterr()
        assert rc != 0
        assert _shown_pieces(_unwrapped(out + err), _LONG_VALUE) == []

    @settings(max_examples=400, deadline=None)
    @given(
        scheme=st.sampled_from(["postgres", "https", "s3", "jdbc:postgresql"]),
        user=st.text(alphabet="ab/:@?# ", max_size=4),
        # J and K appear nowhere else in the text, so either one shown is a leak.
        secret=st.text(alphabet="JK/:@?#&=; %.'\"\\", max_size=8),
        tail=st.sampled_from(
            ["", "/shop", "/f.csv?v=2", "/f.csv?k=QV1xQE", ":1/db?a=b@c", "/f.csv?k=QV'J\"K\\QE"]
        ),
        marker=st.sampled_from(["...", "…", "", "')", " (HTTP 403)"]),
        quoting=st.sampled_from(sorted(_URI_QUOTINGS)),
    )
    def test_no_cut_shows_the_password_or_a_query_value(
        self, scheme, user, secret, tail, marker, quoting
    ):
        forget_known_secrets()
        uri = f"{scheme}://{user}:PW{secret}END@db.example.test{tail}"  # pragma: allowlist secret
        view = handler._inspect_uri(uri)
        if view.ambiguous:
            return  # refused before any driver sees it
        # The copy as the driver prints it, then cut short at every length.
        printed = _URI_QUOTINGS[quoting](uri)
        for cut in range(len(printed) + 1):
            masked = handler._mask_uri_copies(f"x '{printed[:cut]}{marker}", uri, view)
            for piece in ("PW", "END", "QV", "QE", "J", "K"):
                assert piece not in masked, (quoting, cut, masked)


class TestDriverErrorQuotingAReQuotedCopy:
    """A driver may print the URI, or the password alone, escaped its own way.

    DuckDB's "LINE 1:" context shows the URI as the SQL literal it read, each
    quote doubled; the Postgres and MySQL extensions show their libpq DSN, a
    quote or backslash in the password escaped with a backslash. Such a copy
    must be masked as the URI itself is, whether whole or cut short.
    """

    # What DuckDB 1.5 prints for these URIs, verbatim.
    @pytest.mark.parametrize(
        ("uri", "error", "leak", "kept"),
        [
            (
                "https://alice:pa'ssWORDTAIL@h.invalid/a.csv",  # pragma: allowlist secret
                "IO Error: Could not resolve hostname error for HTTP HEAD to "
                "'https://alice:pa'ssWORDTAIL@h.invalid/a.csv'\n\n"  # pragma: allowlist secret
                "LINE 1: SELECT * FROM read_csv_auto("
                "'https://alice:pa''ssWORDTAIL@h.invalid/a...\n"  # pragma: allowlist secret
                "                      ^",
                "ssWORDTAIL",
                [
                    "HTTP HEAD to 'https://***REDACTED***@h.invalid/a.csv'",
                    "read_csv_auto('https://***REDACTED***@h.invalid/a...",
                ],
            ),
            (
                "https://h.invalid/a.csv?sig=ab'cdSECRETTAIL",
                "File https://h.invalid/a.csv?sig=ab'cdSECRETTAIL requires the extension httpfs "
                "to be loaded\n\n"
                "LINE 1: SELECT * FROM read_csv_auto("
                "'https://h.invalid/a.csv?sig=ab''cdSECRETTAIL...\n"
                "                      ^",
                "cdSECRETTAIL",
                [
                    "File https://h.invalid/a.csv?sig=***REDACTED*** requires",
                    "read_csv_auto('https://h.invalid/a.csv?sig=***REDACTED***...",
                ],
            ),
            (
                "s3://bucket/a.csv?sig=ab'cdSECRETTAIL",
                "IO Error: Invalid query parameters found.\n\n"
                "LINE 1: SELECT * FROM read_csv_auto('s3://bucket/a.csv?sig=ab''cdSECRETTAIL')...",
                "cdSECRETTAIL",
                ["read_csv_auto('s3://bucket/a.csv?sig=***REDACTED***')..."],
            ),
            (
                "postgres://alice:pa'ssWORDTAIL@127.0.0.1:1/shop",  # pragma: allowlist secret
                'IO Error: Unable to connect to Postgres at "host=127.0.0.1 port=1 user=alice '
                "password='pa\\'ssWORDTAIL' dbname=shop\": connection to server at "  # pragma: allowlist secret
                '"127.0.0.1", port 1 failed: Connection refused',
                "ssWORDTAIL",
                ["user=alice password='***REDACTED***' dbname=shop"],
            ),
            (
                "postgres://alice:pa\\ssWORDTAIL@127.0.0.1:1/shop",  # pragma: allowlist secret
                'IO Error: Unable to connect to Postgres at "host=127.0.0.1 port=1 user=alice '
                'password=pa\\\\ssWORDTAIL dbname=shop": connection to server at '  # pragma: allowlist secret
                '"127.0.0.1", port 1 failed: Connection refused',
                "ssWORDTAIL",
                ["user=alice password=***REDACTED*** dbname=shop"],
            ),
            (
                "mysql://alice:pa'ssWORDTAIL@127.0.0.1:1/shop",  # pragma: allowlist secret
                "IO Error: Failed to connect to MySQL database with parameters "
                "\"host=127.0.0.1 port=1 user=alice password='pa\\'ssWORDTAIL' database=shop\": "  # pragma: allowlist secret
                "Can't connect to server on '127.0.0.1' (115). Attempted host: 127.0.0.1",
                "ssWORDTAIL",
                ["user=alice password='***REDACTED***' database=shop"],
            ),
        ],
    )
    def test_what_duckdb_prints_shows_no_part_of_the_credential(
        self, monkeypatch, tmp_path, uri, error, leak, kept
    ):
        stub = _StubDiscoverer(error=RuntimeError(error))
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        exc = info.value
        why = exc.why or ""
        assert leak not in exc.render(color=False) + exc.as_json(), why
        # What is not secret is still shown, so the user can tell what failed.
        for text in kept:
            assert text in why, why

    def test_a_short_password_is_masked_in_the_sql_literal_copy(self, monkeypatch, tmp_path):
        # Too short to mask by value: only its place in the copy masks it.
        uri = "https://alice:J'K@h.invalid/a.csv"  # pragma: allowlist secret
        stub = _StubDiscoverer(
            error=RuntimeError(
                "IO Error: Could not resolve hostname error for HTTP HEAD to "
                f"'{uri}'\n\nLINE 1: SELECT * FROM read_csv_auto({quote_ansi_string_literal(uri)})"
                " LIMIT 0"
            )
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        why = info.value.why or ""
        assert "J" not in why and "K" not in why, why
        assert "read_csv_auto('https://***REDACTED***@h.invalid/a.csv') LIMIT 0" in why

    @pytest.mark.parametrize(
        ("uri", "leak", "kept"),
        [
            (
                "https://h.invalid/a.csv?sig=ab'cdSECRETTAIL",
                "cdSECRETTAIL",
                "read_csv_auto('https://h.invalid/a.csv?sig=***REDACTED***",
            ),
            (
                "https://alice:pa'ssWORDTAIL@h.invalid/a.csv?v=1",  # pragma: allowlist secret
                "ssWORDTAIL",
                "read_csv_auto('https://***REDACTED***@h.invalid/a",
            ),
        ],
    )
    def test_real_cli_and_duckdb_sql_literal_copy(
        self, monkeypatch, tmp_path, capsys, uri, leak, kept
    ):
        pytest.importorskip("duckdb")
        from fluid_build.cli import main

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        out_dir = tmp_path / "out"
        rc = main(["init", "p", "--discover", uri, "--yes", "--dir", str(out_dir)])
        out, err = capsys.readouterr()
        assert rc != 0
        shown = _unwrapped(out + err)
        assert leak not in shown, shown
        assert kept in shown, shown

    def test_sandbox_refusal_quoting_the_url_with_repr(self, monkeypatch, tmp_path):
        pytest.importorskip("duckdb")
        # Holding both quote characters, repr() escapes the single one.
        uri = "https://alice:pa'ss\"WORDTAIL@h.invalid/../a.csv"  # pragma: allowlist secret
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        with pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri)
        exc = info.value
        why = exc.why or ""
        assert "WORDTAIL" not in exc.render(color=False) + exc.as_json(), why
        assert "remote prefix 'https://***REDACTED***@h.invalid/../' contains '..'" in why

    @pytest.mark.parametrize("odd", ["\udc80", "\ud800"])
    def test_a_value_that_is_not_utf8_is_still_masked(self, odd):
        # A command-line byte that is not UTF-8 arrives as a lone surrogate.
        uri = f"https://h.invalid/a.csv?sig=ab{odd}cdSECRETTAIL"
        view = handler._inspect_uri(uri)
        assert view.safe == "https://h.invalid/a.csv?sig=***REDACTED***"
        printed = f"LINE 1: SELECT * FROM read_csv_auto('{uri[:40]}..."
        assert "cdSECRET" not in handler._mask_uri_copies(printed, uri, view)

    def test_every_form_is_registered_before_the_driver_runs(self, monkeypatch, tmp_path):
        password = "pa'ss\\WORD\"TAIL"  # pragma: allowlist secret
        uri = f"postgres://alice:{password}@127.0.0.1:1/shop"
        printed: List[str] = []

        class _Echo(_StubDiscoverer):
            def discover(self, uri: str) -> List[DiscoveredStream]:
                # What a log line written during the connect would carry.
                printed.extend(redact_secret_text(q(password)) for q in _VALUE_QUOTINGS.values())
                return super().discover(uri)

        assert _run(monkeypatch, tmp_path, uri, _Echo()) == 0
        assert len(printed) == len(_VALUE_QUOTINGS)
        for text in printed:
            assert "WORD" not in text and "TAIL" not in text, text

    @settings(max_examples=400, deadline=None)
    @given(
        scheme=st.sampled_from(["postgres", "mysql", "https", "s3"]),
        user=st.text(alphabet="ab", max_size=4),
        secret=st.text(alphabet="JK:@&=; %.'\"\\", min_size=1, max_size=8),
        quoting=st.sampled_from(sorted(_VALUE_QUOTINGS)),
    )
    def test_a_requoted_password_is_masked_wherever_it_is_printed(
        self, scheme, user, secret, quoting
    ):
        forget_known_secrets()
        uri = f"{scheme}://{user}:PW{secret}END@db.example.test/shop"  # pragma: allowlist secret
        view = handler._inspect_uri(uri)
        if view.ambiguous:
            return  # refused before any driver sees it
        password = urlsplit(uri).password or ""  # what the driver is given
        printed = f"connect failed ({_VALUE_QUOTINGS[quoting](password)}): refused"
        # The registry alone, as the log filter applies it to any later line.
        masked = redact_secret_text(printed)
        for piece in ("PW", "END", "J", "K"):
            assert piece not in masked, (quoting, masked)


def _exception_chain(exc: BaseException) -> List[BaseException]:
    """``exc`` and every exception it links to through ``__cause__`` or ``__context__``."""
    seen: List[BaseException] = []
    todo: List[Optional[BaseException]] = [exc]
    while todo:
        link = todo.pop()
        if link is None or any(link is s for s in seen):
            continue
        seen.append(link)
        todo += [link.__cause__, link.__context__]
    return seen


def _chain_text(exc: BaseException) -> str:
    """What a formatted traceback, a crash reporter or a log record with
    ``exc_info`` may print of ``exc`` and of every exception it links to."""
    texts: List[str] = []
    for link in _exception_chain(exc):
        texts += [str(link), repr(link), *(repr(arg) for arg in link.args)]
        texts += [str(note) for note in getattr(link, "__notes__", ())]
    texts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return "\n".join(texts)


class TestDriverErrorChainKeepsPasswordOut:
    """The driver's own error quotes the URI or DSN as given.

    The typed error shows that text masked, and must not keep the driver's error
    as its cause or context either: a formatted traceback (a crash reporter,
    ``logging`` with ``exc_info``) prints every link of the chain, and ``raise
    ... from None`` would only hide ``__context__`` (PEP 415), not drop it.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            f"postgres://alice:{SENTINEL}@127.0.0.1:1/shop",
            f"mysql://alice:{SENTINEL}@127.0.0.1:1/shop",
            f"https://alice:{SENTINEL}@h.invalid/a.csv",
            f"https://{SENTINEL}@h.invalid/a.csv",
            f"https://h.invalid/a.csv?sig={SENTINEL}",
        ],
    )
    def test_no_link_of_the_chain_carries_the_credential(self, monkeypatch, tmp_path, uri):
        # A driver error quoting the URI and the credential, with a note, a cause
        # and a context that quote them too.
        error = RuntimeError(f"could not connect to {uri} (password={SENTINEL})")
        error.__notes__ = [f"while connecting to {uri}"]
        error.__cause__ = OSError(f"dsn: password={SENTINEL}")
        error.__context__ = ValueError(uri)
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=error))
        exc = info.value
        assert SENTINEL not in _chain_text(exc)
        # Raised once no handler was active, so nothing is chained implicitly.
        assert exc.__context__ is None
        # Its cause stands in for the driver's error: the type and the masked
        # text, with no traceback and no chain of its own.
        cause = exc.__cause__
        assert cause is not None
        assert str(cause).startswith("RuntimeError: could not connect to "), str(cause)
        assert "***REDACTED***" in str(cause)
        assert cause.__cause__ is None and cause.__context__ is None
        assert cause.__traceback__ is None

    def test_real_duckdb_error_is_not_kept_on_the_chain(self, monkeypatch, tmp_path):
        pytest.importorskip("duckdb")
        # The sandbox refuses this before any network access, quoting the URL.
        uri = "https://alice:pa'ss\"WORDTAIL@h.invalid/../a.csv"  # pragma: allowlist secret
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        with pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri)
        text = _chain_text(info.value)
        assert "WORDTAIL" not in text, text
        assert "contains '..'" in str(info.value.__cause__), text


def _type_name(error: BaseException) -> str:
    """How the stand-in names the driver's exception type."""
    kind = type(error)
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"


class _UnprintableError(Exception):
    """A driver error whose own ``str()`` fails; its arguments hold the URI."""

    def __str__(self) -> str:
        raise RuntimeError("driver error cannot be rendered")


class _UnprintableArgument:
    """A driver's connection object: ``str()`` fails, ``repr()`` names its DSN."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def __str__(self) -> str:
        raise ValueError("unprintable argument")

    def __repr__(self) -> str:
        return f"<Connection {self.dsn}>"


class TestDriverErrorThatCannotBeRendered:
    """A driver error whose ``str()`` fails still becomes the typed error.

    Rendering it inside the handler would raise the rendering failure there, with
    the driver's error (URI and password in its arguments) as its ``__context__``.
    """

    @pytest.mark.parametrize(
        "make",
        [
            lambda uri: _UnprintableError(uri, f"password={SENTINEL}"),
            lambda uri: Exception(_UnprintableArgument(f"host=127.0.0.1 password={SENTINEL}")),
        ],
        ids=["str-raises", "argument-str-raises"],
    )
    def test_typed_error_without_the_driver_error_on_its_chain(self, monkeypatch, tmp_path, make):
        uri = f"postgres://alice:{SENTINEL}@127.0.0.1:1/shop"
        error = make(uri)
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=error))
        exc = info.value
        assert exc.__context__ is None
        # What CPython's traceback prints for an exception it cannot render.
        assert exc.why == "<exception str() failed>"
        assert str(exc.__cause__) == f"{_type_name(error)}: <exception str() failed>"
        assert SENTINEL not in _chain_text(exc)


class _TopicListingFailed(RuntimeError):
    """A plugin's wrapper error, raised ``from`` the low-level one."""


def _raised(make: Callable[[], BaseException]) -> BaseException:
    """The error ``make`` raises, with the context a real raise gives it."""
    try:
        raise make()
    except BaseException as error:  # noqa: BLE001 - returned for the test to use
        return error


class TestDriverErrorKeepsItsReasons:
    """The stand-in keeps no chain, so what the driver's error was raised from
    (``raise X from err``) and an exception group's sub-errors are kept as masked
    text, in the typed error's why and in the stand-in alike."""

    def test_root_cause_of_a_wrapped_error(self, monkeypatch, tmp_path):
        uri = f"https://alice:{SENTINEL}@broker.example.test/orders.csv"
        low = ConnectionRefusedError(111, f"Connection refused by {uri}")

        def wrapped() -> BaseException:
            try:
                raise low
            except ConnectionRefusedError as err:
                raise _TopicListingFailed("could not list topics") from err

        error = _raised(wrapped)
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=error))  # type: ignore[arg-type]
        exc = info.value
        assert exc.why == (
            "could not list topics\n"
            "caused by: ConnectionRefusedError: [Errno 111] Connection refused by "
            "https://***REDACTED***@broker.example.test/orders.csv"
        )
        cause = exc.__cause__
        assert str(cause) == f"{_type_name(error)}: {exc.why}"
        assert cause.__cause__ is None and cause.__context__ is None
        logged = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert "could not list topics" in logged
        assert "Connection refused by https://***REDACTED***@broker" in logged
        assert SENTINEL not in _chain_text(exc)

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="exception groups are Python 3.11+")
    def test_sub_errors_of_an_exception_group(self, monkeypatch, tmp_path):
        uri = f"postgres://alice:{SENTINEL}@127.0.0.1:1/shop"
        group = ExceptionGroup(  # noqa: F821 - a builtin from Python 3.11
            "unhandled errors in a TaskGroup",
            [
                ConnectionRefusedError(111, "Connection refused by replica-2.example.test"),
                ValueError(f"bad uri {uri}"),
            ],
        )
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=group))
        exc = info.value
        assert exc.why == (
            "unhandled errors in a TaskGroup (2 sub-exceptions)\n"
            "sub-exception 1: ConnectionRefusedError: [Errno 111] Connection refused by "
            "replica-2.example.test\n"
            "sub-exception 2: ValueError: bad uri postgres://alice:***REDACTED***@127.0.0.1:1/shop"
        )
        assert str(exc.__cause__) == f"ExceptionGroup: {exc.why}"
        assert SENTINEL not in _chain_text(exc)

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="exception groups are Python 3.11+")
    def test_a_wide_group_names_as_many_sub_errors_as_a_traceback_does(self, monkeypatch, tmp_path):
        group = ExceptionGroup(  # noqa: F821 - a builtin from Python 3.11
            "unhandled errors in a TaskGroup", [OSError(f"replica {i} refused") for i in range(20)]
        )
        uri = "postgres://alice@127.0.0.1:1/shop"
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=group))
        lines = info.value.why.splitlines()
        assert lines[1] == "sub-exception 1: OSError: replica 0 refused"
        assert lines[15] == "sub-exception 15: OSError: replica 14 refused"
        assert lines[16:] == ["and 5 more sub-exceptions"]

    def test_an_error_raised_while_handling_another_keeps_only_its_own_reason(
        self, monkeypatch, tmp_path
    ):
        # The DuckDB sandbox installs an extension when loading it fails: the
        # install error is raised while the load error is being handled, and the
        # load error is not its reason. Only ``raise ... from`` names one.
        def install_after_load() -> BaseException:
            try:
                raise OSError("Extension httpfs not found locally")
            except OSError:
                raise RuntimeError("HTTP Error: Failed to download extension httpfs")

        error = _raised(install_after_load)
        assert error.__context__ is not None
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, "https://h.invalid/a.csv", _StubDiscoverer(error=error))  # type: ignore[arg-type]
        assert info.value.why == "HTTP Error: Failed to download extension httpfs"

    def test_a_cause_cycle_is_named_once(self, monkeypatch, tmp_path):
        outer = RuntimeError("connect failed")
        inner = OSError("refused")
        outer.__cause__ = inner
        inner.__cause__ = outer
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, "https://h.invalid/a.csv", _StubDiscoverer(error=outer))
        assert info.value.why == "connect failed\ncaused by: OSError: refused"


class TestAtSignAfterTheHost:
    """An "@" after the host is the path's or the query's own.

    An object key may hold an e-mail address, a bucket or a compose service is a
    dotless host, and npm CDNs serve sample datasets at versioned paths. Such a
    URL is used as given. The display reads up to the last "@" as a possible
    token, so it only over-masks.

    The cost: a token in the user-name position that holds a raw "/", "?" or "#"
    (``https://TOK/EN@host/f.csv``) cannot be told from such a URL. It is used as
    given too, and a driver contacts the text before that character as the host.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            # An object key holding an e-mail address, under a dotless bucket or
            # compose-service host.
            "s3://bucket/reports/user@example.com/a.csv",
            "gs://bucket/exports/ops@example.test/a.csv",
            "http://minio/bucket/a@b.csv",
            # npm CDNs serve widely used sample datasets under versioned and
            # scoped paths.
            "https://cdn.jsdelivr.net/npm/vega-datasets@2/data/cars.json",
            "https://cdn.example.test/@scope/pkg/data.csv",
            # An address in a query value.
            "https://files.example.test/export.csv?mail=ops@example.test",
            "http://localhost/exports/orders@v2.csv",
            # A local file named through a file:// URL: no host is contacted.
            "file:///srv/exports/report@2x.csv",
        ],
    )
    def test_at_sign_after_the_host_is_not_refused(self, monkeypatch, tmp_path, uri):
        stub = _StubDiscoverer()
        assert _run(monkeypatch, tmp_path, uri, stub) == 0
        assert stub.seen == [uri]
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": uri.partition("?")[0]}

    @pytest.mark.parametrize("delimiter", ["/", "?", "#"])
    def test_token_with_a_raw_delimiter_is_masked_in_the_display(
        self, monkeypatch, tmp_path, capsys, caplog, delimiter
    ):
        uri = f"https://{SENTINEL}{delimiter}tail-part9@files.example.test/data/a.csv"
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, err = capsys.readouterr()
        for text in (out, err, caplog.text):
            assert SENTINEL not in text
            assert "tail-part9" not in text
        # After a raw "?" or "#" the rest may be a query or fragment: masked or left out.
        rest = "@files.example.test/data/a.csv" if delimiter == "/" else ""
        assert f"Discoveringstreamsathttps://***REDACTED***{rest}…" in _unwrapped(out)

    def test_help_says_such_a_token_is_used_as_given(self, monkeypatch, tmp_path, capsys):
        shown = _init_help(monkeypatch, tmp_path, capsys)
        assert "must not hold a raw '/', '?' or '#'" in shown, shown
        assert "read as the host, contacted and written to the contracts" in shown, shown
        # What the contracts keep differs by character (pinned by the next test).
        assert (
            "After a '/', the rest of the token is kept too: the contract's connection "
            "uri is then the whole URL, token, '@', host and path" in shown
        ), shown
        assert (
            "After a '?' or '#', the rest is read as the query string or fragment, "
            "which is not written" in shown
        ), shown
        assert "Avoid those characters in such a token" in shown, shown

    @pytest.mark.parametrize(
        ("delimiter", "written"),
        [
            ("/", f"https://{SENTINEL}/tail-part9@files.example.test/data/a.csv"),
            ("?", f"https://{SENTINEL}"),
            ("#", f"https://{SENTINEL}"),
        ],
    )
    def test_token_with_a_raw_delimiter_is_written_as_the_help_says(
        self, monkeypatch, tmp_path, delimiter, written
    ):
        uri = f"https://{SENTINEL}{delimiter}tail-part9@files.example.test/data/a.csv"
        assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": written}


class TestLocationTypedWithoutItsDoubleSlash:
    """``postgres:alice:PW@host/db``: a credential URI typed without its "//".

    With no "://" it is read as a local path, which does not exist, so the error
    quotes it. When the text before its first ":" is the scheme of a credential
    URI, from that ":" to its last "@" is masked wherever it is echoed, as a URI's
    user:password part is, and so is every query value. A local file name may
    hold a ":" before an "@" too, so any other location is shown and named as is.
    """

    @pytest.mark.parametrize(
        ("location", "shown"),
        [
            (
                f"postgres:alice:{SENTINEL}@127.0.0.1:5432/shop",
                "postgres:***REDACTED***@127.0.0.1:5432/shop",
            ),
            (
                f"postgresql:/alice:{SENTINEL}@127.0.0.1:5432/shop",
                "postgresql:***REDACTED***@127.0.0.1:5432/shop",
            ),
            (
                f"mysql:alice:{SENTINEL}@127.0.0.1:3306/shop",
                "mysql:***REDACTED***@127.0.0.1:3306/shop",
            ),
            (f"https:/alice:{SENTINEL}@h.invalid/a.csv", "https:***REDACTED***@h.invalid/a.csv"),
            (
                f"postgres:\\\\alice:{SENTINEL}@127.0.0.1/shop",
                "postgres:***REDACTED***@127.0.0.1/shop",
            ),
            # The scheme in any case, with a driver suffix, or with spaces around it.
            (
                f"POSTGRES:alice:{SENTINEL}@127.0.0.1:5432/shop",
                "POSTGRES:***REDACTED***@127.0.0.1:5432/shop",
            ),
            (
                f"postgresql+psycopg:alice:{SENTINEL}@127.0.0.1/shop",
                "postgresql+psycopg:***REDACTED***@127.0.0.1/shop",
            ),
            (
                f" postgres :alice:{SENTINEL}@127.0.0.1/shop",
                " postgres :***REDACTED***@127.0.0.1/shop",
            ),
            # Cloud storage: a key pair, or a token as the user name.
            (f"s3:KEY:{SENTINEL}@bucket/key.csv", "s3:***REDACTED***@bucket/key.csv"),
            (
                f"abfss:{SENTINEL}@container/dir/f.parquet",
                "abfss:***REDACTED***@container/dir/f.parquet",
            ),
            # The ":" after the scheme percent-encoded.
            (
                f"postgres%3A//alice:{SENTINEL}@127.0.0.1/shop",
                "postgres%3A//alice:***REDACTED***@127.0.0.1/shop",
            ),
        ],
    )
    def test_masked_in_every_echo(self, tmp_path, capsys, caplog, location, shown):
        # The real registry: a location with no "://" goes to the filesystem discoverer.
        with caplog.at_level(logging.DEBUG), pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=location)
        out, err = capsys.readouterr()
        exc = info.value
        for text in (
            out,
            err,
            caplog.text,
            exc.what,
            exc.why or "",
            exc.render(color=False),
            exc.as_json(),
            _chain_text(exc),
        ):
            assert SENTINEL not in text
        assert _unwrapped(shown) in _unwrapped(out)
        assert exc.what == f"connectivity probe failed: {shown}"
        # A later line that quotes the password is masked too.
        assert SENTINEL not in redact_secret_text(f"retrying with {SENTINEL}")

    def test_path_with_no_colon_before_its_at_sign_is_shown_as_is(self, tmp_path):
        missing = tmp_path / "exports" / "orders@2024.csv"
        with pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=str(missing))
        assert info.value.what == f"connectivity probe failed: {missing}"
        assert info.value.why == str(missing)

    @pytest.mark.parametrize(
        ("location", "shown"),
        [
            (
                f"https:/h.invalid/a.csv?sig={SENTINEL}",
                "https:/h.invalid/a.csv?sig=***REDACTED***",
            ),
            (
                f"https:h.invalid/a.csv?X-Amz-Signature={SENTINEL}&X-Amz-Date=20300101",
                "https:h.invalid/a.csv?X-Amz-Signature=***REDACTED***&X-Amz-Date=***REDACTED***",
            ),
            (f"s3:bucket/a.csv?code={SENTINEL}#frag-Kq2", "s3:bucket/a.csv?code=***REDACTED***"),
            # Too short to mask by value: masked where the error quotes the location.
            ("https:/h.invalid/a.csv?sig=Qz9", "https:/h.invalid/a.csv?sig=***REDACTED***"),
        ],
    )
    def test_query_values_masked_in_every_echo(self, tmp_path, capsys, caplog, location, shown):
        with caplog.at_level(logging.DEBUG), pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=location)
        out, err = capsys.readouterr()
        exc = info.value
        value = location.partition("=")[2].partition("&")[0].partition("#")[0]
        for text in (
            out,
            err,
            caplog.text,
            exc.what,
            exc.why or "",
            exc.render(color=False),
            exc.as_json(),
            _chain_text(exc),
        ):
            assert value not in text
            assert "frag-Kq2" not in text
        assert shown in _unwrapped(out)
        assert exc.what == f"connectivity probe failed: {shown}"

    @pytest.mark.parametrize(
        "name",
        [
            "backup:quarterly-2024@snapshot.csv",
            "2024-01-01T10:00:00@export.csv",
            "run:2024@host.csv",
        ],
    )
    def test_local_file_with_a_colon_before_its_at_sign_is_named_as_is(
        self, tmp_path, capsys, name
    ):
        source = tmp_path / "src" / name
        source.parent.mkdir()
        source.write_text("id,name\n1,a\n", encoding="utf-8")
        out_dir = tmp_path / "out"
        assert handler.run_discover(_args(out_dir), logging.getLogger("t"), uri=str(source)) == 0
        out, _ = capsys.readouterr()
        assert f"Discoveringstreamsat{source}…" in _unwrapped(out)
        assert [path.name for path in _contracts(out_dir)] == [
            f"contract.{handler._slug(source.stem)}.fluid.yaml"
        ]
        assert "redacted" not in _everything_written(out_dir)

    @pytest.mark.parametrize(
        ("location", "shown"),
        [
            # The first "://" is in the query or the fragment, not after a scheme.
            (
                f"postgres:alice:{SENTINEL}@127.0.0.1:5432/shop?sslrootcert=file:///etc/ca.pem",
                "postgres:***REDACTED***@127.0.0.1:5432/shop?sslrootcert=***REDACTED***",
            ),
            (
                f"postgres:alice:{SENTINEL}@127.0.0.1:5432/shop#https://x.example.test",
                "postgres:***REDACTED***@127.0.0.1:5432/shop",
            ),
            (
                f"https:/alice:{SENTINEL}@h.invalid/a.csv?next=https://x.example.test",
                "https:***REDACTED***@h.invalid/a.csv?next=***REDACTED***",
            ),
            (
                f"mysql:alice:{SENTINEL}@127.0.0.1/shop?ssl-ca=file://ca.pem&code=Qz9",
                "mysql:***REDACTED***@127.0.0.1/shop?ssl-ca=***REDACTED***&code=***REDACTED***",
            ),
        ],
    )
    def test_a_later_double_slash_does_not_make_a_scheme(
        self, tmp_path, capsys, caplog, location, shown
    ):
        # The real registry reads no scheme it knows before that "://".
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=location)
        out, err = capsys.readouterr()
        exc = info.value
        for text in (
            out,
            err,
            caplog.text,
            exc.what,
            exc.why or "",
            exc.render(color=False),
            exc.as_json(),
            json.dumps(exc.extras, default=str),
            _chain_text(exc),
        ):
            assert SENTINEL not in text
            assert "Qz9" not in text
        assert exc.what.endswith(f"URI: {shown}"), exc.what
        assert exc.extras["uri"] == shown
        assert SENTINEL not in redact_secret_text(f"retrying with {SENTINEL}")

    def test_relative_path_under_a_directory_named_after_a_scheme(
        self, monkeypatch, tmp_path, capsys
    ):
        source = tmp_path / "postgres" / "dump:2024@host.csv"
        source.parent.mkdir()
        source.write_text("id,name\n1,a\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "out"
        location = "postgres/dump:2024@host.csv"
        assert handler.run_discover(_args(out_dir), logging.getLogger("t"), uri=location) == 0
        out, _ = capsys.readouterr()
        assert f"Discoveringstreamsat{location}…" in _unwrapped(out)
        assert [path.name for path in _contracts(out_dir)] == ["contract.dump_2024_host.fluid.yaml"]

    @pytest.mark.parametrize(
        "location",
        [
            "backup:quarterly-2024@snapshot.csv",
            "2024-01-01T10:00:00@export.csv",
            "C:\\dir\\a@b.csv",
            "git@github.example.test:org/repo.git",
            "postgres//dump:2024@host.csv",
        ],
    )
    def test_missing_local_file_is_shown_as_is(self, tmp_path, location):
        with pytest.raises(ConnectivityProbeError) as info:
            handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=location)
        assert info.value.what == f"connectivity probe failed: {location}"
        assert info.value.why == location

    def test_help_names_exactly_the_schemes_masked_without_double_slash(
        self, monkeypatch, tmp_path, capsys
    ):
        # The list is fixed in the handler; the help must name it, not a category
        # ("a database or cloud-storage scheme") that ftp:, mongodb: or redis: fit.
        shown = _init_help(monkeypatch, tmp_path, capsys)
        lead = "a location with no '://' in it is masked only when it starts with "
        tail = ", or with a scheme a registered discoverer reads"
        assert lead in shown, shown
        listed = shown[shown.index(lead) + len(lead) :]
        listed = listed[: listed.index(tail)]
        assert sorted(listed.split(", ")) == sorted(handler._CREDENTIAL_SCHEMES), listed
        assert (
            "Any other location with no '://' (ftp:alice:PW@host, user:PW@host/db, "
            "postgres//alice:PW@host/db) is read as a local path and shown, logged and "
            "quoted in errors as given, password included" in shown
        ), shown


# What a location may hold around its credential: every character that ends or
# separates a part of a URI, a "%" and a space.
_LOCATION_PIECE = st.text(alphabet="abXY019._-%~ /?#\\@:", max_size=8)


def _everything_a_failed_discovery_shows(location: str) -> str:
    """The console, log and error of a discovery of ``location`` whose driver
    fails quoting it as given, as repr, as a SQL literal and as JSON."""
    stub = _StubDiscoverer(
        error=RuntimeError(
            f"cannot open {location} ({location!r}) LINE 1: "
            f"{quote_ansi_string_literal(location)} {json.dumps(location)}"
        )
    )
    out = io.StringIO()
    log = logging.getLogger("fluid.test.discover.location")
    with mock.patch.object(handler, "get_discoverer", lambda _uri: stub):
        try:
            with contextlib.redirect_stdout(out):
                handler.run_discover(_args(Path("/nonexistent")), log, uri=location)
        except (ConnectivityProbeError, SchemaValidationError) as exc:
            return "\n".join(
                [
                    out.getvalue(),
                    exc.what,
                    exc.why or "",
                    exc.render(color=False),
                    exc.as_json(),
                    _chain_text(exc),
                ]
            )
    raise AssertionError(f"discovery of {location!r} did not fail")


class TestNoLocationShapeRevealsACredential:
    """A credential URI typed without its "//", whatever surrounds the credential.

    The masked span runs from the ":" after a credential scheme to the last "@",
    so a credential placed between them is never shown, by the display or in a
    driver error that quotes the location. The same holds for any scheme when a
    "://" follows later (in a query or fragment): such a location is no local
    file name. A location whose text before its first ":" is not a credential
    scheme is a local path and is shown as given, so it is not generated here.
    """

    @settings(max_examples=200, deadline=None)
    @given(
        scheme=st.sampled_from(
            [
                "postgres",
                "POSTGRES",
                " postgresql+psycopg ",
                "mysql",
                "https",
                "s3",
                "abfss",
                "file",
            ]
        ),
        left=_LOCATION_PIECE,
        right=_LOCATION_PIECE,
        tail=_LOCATION_PIECE,
    )
    def test_credential_after_a_credential_scheme_is_never_shown(self, scheme, left, right, tail):
        location = f"{scheme}:{left}{SENTINEL}{right}@{tail}"
        # With a "//" right after the scheme it is a URI, read by its own rules.
        assume(handler._scheme_separator(location) < 0)
        forget_known_secrets()
        assert SENTINEL not in _everything_a_failed_discovery_shows(location)

    @settings(max_examples=200, deadline=None)
    @given(
        head=st.text(alphabet="abcXYZ019._-/\\ ", max_size=8),
        left=_LOCATION_PIECE,
        right=_LOCATION_PIECE,
        later=st.sampled_from(
            ["?sslrootcert=file:///etc/ca.pem", "#https://x.example.test", "/s3://b/k"]
        ),
    )
    # repr() doubles the backslash in the head, and the "#" makes the span and
    # the fragment overlap, so the repr copy is only masked by its position.
    @example(head="0\\", left="", right="#0", later="?sslrootcert=file:///etc/ca.pem")
    def test_credential_before_a_later_double_slash_is_never_shown(self, head, left, right, later):
        location = f"{head}:{left}{SENTINEL}{right}@host.example.test/db{later}"
        forget_known_secrets()
        assert SENTINEL not in _everything_a_failed_discovery_shows(location)


class TestDriverReasonKeepsWordsThePasswordAlsoSpells:
    """Stock dev URIs (the postgres image's postgres:postgres, WordPress's compose
    wordpress x3) use the password as the user and database name too. The driver's
    reason keeps naming the user and database it tried; the DSN's password field
    is still masked, by its position after ``password=``."""

    @pytest.mark.parametrize(
        ("uri", "reason", "kept", "masked"),
        [
            (
                "postgresql://postgres:postgres@127.0.0.1:1/postgres",  # pragma: allowlist secret
                'IO Error: Unable to connect to Postgres at "host=127.0.0.1 port=1 '
                'user=postgres password=postgres dbname=postgres": FATAL: password '
                'authentication failed for user "postgres"',
                ["user=postgres", "dbname=postgres", 'for user "postgres"'],
                "password=***REDACTED*** dbname",
            ),
            (
                "mysql://wordpress:wordpress@127.0.0.1:1/wordpress",  # pragma: allowlist secret
                "IO Error: Failed to connect to MySQL database with parameters "
                '"host="127.0.0.1" port=1 user="wordpress" password="wordpress" '  # pragma: allowlist secret
                'database="wordpress"": Access denied',
                ['user="wordpress"', 'database="wordpress"'],
                'password="***REDACTED***" database',
            ),
            (
                "postgresql://postgres:postgres@127.0.0.1:1/postgres",  # pragma: allowlist secret
                "connect failed: {'user': 'postgres', 'password': 'postgres', "  # pragma: allowlist secret
                "'dbname': 'postgres'}",
                ["'user': 'postgres'", "'dbname': 'postgres'"],
                "'password': '***REDACTED***'",
            ),
            (
                "postgresql://alice:pa ss@127.0.0.1:1/shop",  # pragma: allowlist secret
                'IO Error: Unable to connect to Postgres at "host=127.0.0.1 port=1 '
                "user=alice password='pa ss' dbname=shop\": refused",  # pragma: allowlist secret
                ["user=alice", "dbname=shop"],
                "password='***REDACTED***' dbname",
            ),
        ],
        ids=["postgres-image", "wordpress-compose", "mapping-repr", "short-password-with-a-space"],
    )
    def test_user_and_database_named_password_masked(
        self, monkeypatch, tmp_path, uri, reason, kept, masked
    ):
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=RuntimeError(reason)))
        why = info.value.why or ""
        for word in kept:
            assert word in why, why
        assert masked in why, why
        assert "pa ss" not in why

    @pytest.mark.parametrize(
        "key",
        ["PGPASSWORD=", "pgpassword=", "MYSQL_PWD=", "mysql_pwd=", "DB_PASSWORD=", "PGPASSWORD: "],
    )
    def test_value_after_an_environment_style_password_key_masked(self, monkeypatch, tmp_path, key):
        # libpq reads PGPASSWORD and the MySQL client MYSQL_PWD; a driver or plugin
        # that echoes its environment names the password after that key.
        uri = "postgresql://postgres:postgres@127.0.0.1:1/postgres"  # pragma: allowlist secret
        reason = f"connect failed: user=postgres {key}postgres dbname=postgres"
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=RuntimeError(reason)))
        why = info.value.why or ""
        assert f"{key}***REDACTED*** dbname=postgres" in why, why
        assert "user=postgres" in why, why

    @pytest.mark.parametrize("key", ["PGPASSWORD=", "pgpassword=", "MYSQL_PWD=", "pwd="])
    @pytest.mark.parametrize(
        "after",
        # What a driver or plugin may print next: the end of a sentence, the next
        # variable of a query string, a pipe, a path, a port, an escaped newline,
        # a terminal colour reset, a percent-encoded newline, or more of a word.
        [".", "!", "&PGUSER=postgres", "|", "/tmp", ":5432", "?", "#", "@host", "\\n"]
        + ["\x1b[0m", "%0A", "x9"],
    )
    def test_value_after_a_password_key_masked_whatever_follows(
        self, monkeypatch, tmp_path, key, after
    ):
        uri = "postgresql://postgres:postgres@127.0.0.1:1/postgres"  # pragma: allowlist secret
        reason = f"connect failed: {key}postgres{after} dbname=postgres"
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, _StubDiscoverer(error=RuntimeError(reason)))
        why = info.value.why or ""
        assert f"connect failed: {key}***REDACTED***{after} dbname=postgres" in why, why


class TestUserNameOfARegisteredDatabaseScheme:
    """A JDBC discoverer added with ``register_discoverer`` reads a database URI: its
    user name is a login, not a credential, so it is shown and is not masked in the
    later output of a process that discovers several sources."""

    def test_user_name_shown_and_not_registered(self, monkeypatch, tmp_path, capsys):
        from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, JdbcSourceConfig

        class CockroachDiscoverer(JdbcDiscoverer):
            def discover(self, uri: str) -> List[DiscoveredStream]:
                return _StubDiscoverer().discover(uri)

        config = JdbcSourceConfig(
            schemes=("cockroach",),
            default_port=26257,
            extension="postgres",
            attach_alias="pg",
            attach_type="postgres",
            database_dsn_key="dbname",
        )
        uri = f"cockroach://app_user:{SENTINEL}@crdb.example.test:26257/shop"
        monkeypatch.setattr(
            handler, "get_discoverer", lambda _uri: CockroachDiscoverer(config=config)
        )
        assert handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err
        assert "cockroach://app_user:***REDACTED***@crdb.example.test:26257/shop" in _unwrapped(out)
        assert redact_secret_text("db login app_user ok") == "db login app_user ok"
        assert SENTINEL not in redact_secret_text(f"later line {SENTINEL}")

    def test_url_user_name_is_still_registered(self, monkeypatch, tmp_path):
        # Git hosts take a token as the user name, with any password.
        uri = f"https://{SENTINEL}:x-oauth-basic@git.example.test/data/orders.csv"  # pragma: allowlist secret
        assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        assert SENTINEL not in redact_secret_text(f"later line {SENTINEL}")


def _everything_shown(exc: BaseException, out: str, err: str, log: str) -> str:
    """The console, the log and every rendering of a typed error."""
    return "\n".join(
        [out, err, log, exc.what, exc.why or "", exc.render(color=False), exc.as_json()]
    )


class TestColonBeforeAnAtSignAfterTheHost:
    """A ":" before a later "@" refuses a URI wherever a parser may misread a password.

    urlsplit ends the authority at the first "/", "?" or "#" after "//" (RFC 3986,
    section 3.2). Where that authority is a host name alone, a ":" and an "@"
    after it are the path's, query's or fragment's own: an object key may hold a
    timestamp and an e-mail address. On s3:// DuckDB re-encodes a
    percent-encoded "@" (%40 is requested as %2540), so no encoding of such a key
    would do. Such a URL is used as given, as it was before this branch.

    A ":" in the authority, a port's included, reads both ways:
    ``localhost:9000/bucket/a@b.csv`` is a host and a port, or the user name
    ``localhost`` with a password that starts ``9000/``. Such a URI is refused,
    and so is a database URI with a ":" after its host: its path is a database
    name, and a credential typed with an extra "/" (``postgres:///alice:PW@host/db``)
    has that shape.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            # An object key holding a timestamp and an e-mail address.
            "s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv",
            "https://h/a:b/user@example.com/x.csv",
            "gs://bucket/exports/2024-01-01T10:00:00@v2.csv",
            "file:///srv/exports/2024-01-01T10:00:00/a@b.csv",
            # A dotless compose service with no port.
            "http://minio/bucket/2024-01-01T10:00:00/a@b.csv",
            # The refusal's remedy for a host with a port: the "@" percent-encoded.
            "http://localhost:9000/bucket/2024-01-01T10:00:00/a%40b.csv",
        ],
    )
    def test_accepted_and_written_as_given(self, monkeypatch, tmp_path, uri):
        stub = _StubDiscoverer()
        assert _run(monkeypatch, tmp_path, uri, stub) == 0
        assert stub.seen == [uri]
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": uri.partition("?")[0]}

    @pytest.mark.parametrize(
        ("uri", "stream"),
        [
            ("s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv", "x"),
            ("https://h/a:b/user@example.com/x.csv", "x"),
            ("http://minio/bucket/a@b.csv", "a_b"),
        ],
    )
    def test_real_discoverer_names_the_stream_after_the_path(
        self, monkeypatch, tmp_path, capsys, uri, stream
    ):
        _stub_remote_columns(monkeypatch)
        assert handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri) == 0
        assert [path.name for path in _contracts(tmp_path)] == [f"contract.{stream}.fluid.yaml"]
        assert _connection(_contracts(tmp_path)[0]) == {"uri": uri}

    @pytest.mark.parametrize(
        "uri",
        [
            # A MinIO or compose service on a port, an IPv6 literal, an address in
            # a query value after a port.
            "http://localhost:9000/bucket/a@b.csv",
            "http://minio:9000/bucket/2024-01-01T10:00:00/a@b.csv",
            "http://[::1]:9000/bucket/a@b.csv",
            "http://[::1]/bucket/2024-01-01T10:00:00@v2.csv",
            "https://files.example.test:8443/orders.csv?mail=ops@example.test",
            # The same in a database URI.
            "postgres://db.example.test:5432/shop?application_name=ops@laptop",
            "mysql://db.example.test:3306/shop?note=a:b@c",
        ],
    )
    def test_a_host_with_a_port_before_a_later_at_sign_is_refused(
        self, monkeypatch, tmp_path, capsys, uri
    ):
        # It reads both ways: a host and a port, or a user name and a password
        # that starts with digits. Refused before any connection, with the remedy.
        stub = _StubDiscoverer()
        with pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        assert stub.seen == []
        assert not list(tmp_path.iterdir())
        assert "%40" in (info.value.fix or "")
        assert "🔑" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "uri",
        [
            # A ":" in the host part that no port number follows.
            f"https://alice:ab/{SENTINEL}@files.example.test/a.csv",
            f"https://alice:ab?{SENTINEL}@files.example.test/a.csv",
            f"s3://KEY:ab#{SENTINEL}@bucket/a.csv",
            f"http://alice:/{SENTINEL}@files.example.test/a.csv",
            f"http://alice:99999/{SENTINEL}@files.example.test/a.csv",
            f"http://alice:+12/{SENTINEL}@files.example.test/a.csv",
            f"http://alice:１２/{SENTINEL}@files.example.test/a.csv",
            f"http://[alice:ab]/{SENTINEL}@files.example.test/a.csv",
            f"http://[::1/alice:{SENTINEL}@files.example.test/a.csv",
            # A password that starts with what reads as a port number.
            f"https://alice:12/{SENTINEL}@files.example.test/a.csv",
            f"https://alice:12?{SENTINEL}@files.example.test/data/a.csv?versionId=abc",
            f"https://alice:12?tok={SENTINEL}@files.example.test/data/a.csv",
            f"https://alice:12#{SENTINEL}@files.example.test/data/a.csv?versionId=abc",
            f"s3://alice:80?{SENTINEL}&x=1@bucket/a.csv",
            f"gs://alice:0/{SENTINEL}@bucket/a.csv",
            f"http://alice:65535/{SENTINEL}@files.example.test/a.csv",
            f"postgres://alice:12/{SENTINEL}@db.example.test/shop",
            # An "@" in the host part, and another after it.
            f"https://alice:ab@{SENTINEL}/x@files.example.test/a.csv",
            f"https://alice:{SENTINEL}@files.example.test/2024-01-01T10:00:00/a@b.csv",
            f"s3://KEY:{SENTINEL}@bucket/2024-01-01T10:00:00/user@example.com/x.csv",
            # A ":" after the host of a database URI.
            f"postgres:///alice:{SENTINEL}@db.example.test/shop",
            f"postgresql://corp/alice:{SENTINEL}@db.example.test/shop",
            f"mysql://corp?alice:{SENTINEL}@db.example.test/shop",
            f"mariadb://corp#alice:{SENTINEL}@db.example.test/shop",
            # The same rules for a discoverer registered under an RFC 3986 scheme.
            f"mydb://alice:ab/{SENTINEL}@db.example.test/shop",
        ],
    )
    def test_credential_shapes_still_refused_before_any_connection(
        self, monkeypatch, tmp_path, capsys, caplog, uri
    ):
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        assert stub.seen == []
        assert not _contracts(tmp_path)
        assert "cannot tell the password from the host" in info.value.what
        assert SENTINEL not in _everything_shown(info.value, out, err, caplog.text)

    @pytest.mark.parametrize(
        ("delimiter", "written"),
        [
            ("/", f"https://corp/alice:{SENTINEL}@files.example.test/data/a.csv"),
            ("?", "https://corp"),
            ("#", "https://corp"),
        ],
    )
    def test_url_user_name_with_a_raw_delimiter_is_used_as_given(
        self, monkeypatch, tmp_path, capsys, caplog, delimiter, written
    ):
        # Documented in the --discover help, like a token with such a character:
        # the text before it is the host, and after a "/" the contracts keep the
        # rest, password included. The display, the log and errors mask it.
        uri = f"https://corp{delimiter}alice:{SENTINEL}@files.example.test/data/a.csv"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, stub) == 0
        out, err = capsys.readouterr()
        assert stub.seen == [uri]
        assert SENTINEL not in out + err + caplog.text
        assert _connection(_contracts(tmp_path)[0]) == {"uri": written}

    @pytest.mark.parametrize("digits", [4300, 4301, 100_000])
    def test_a_run_of_digits_too_long_for_int_is_refused(
        self, monkeypatch, tmp_path, capsys, caplog, digits
    ):
        # CPython 3.11+ refuses int() of more than 4300 digits with a ValueError
        # (docs.python.org/3/library/stdtypes.html#int-max-str-digits). No int()
        # reads the text after the ":": the typed refusal, as for any other port.
        uri = f"https://alice:{'9' * digits}/{SENTINEL}@files.example.test/a.csv"
        stub = _StubDiscoverer()
        with caplog.at_level(logging.DEBUG), pytest.raises(SchemaValidationError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        out, err = capsys.readouterr()
        assert stub.seen == []
        assert not list(tmp_path.iterdir())
        assert SENTINEL not in _everything_shown(info.value, out, err, caplog.text)

    @pytest.mark.parametrize(
        "uri",
        [
            "s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv",
            "http://minio/bucket/a@b.csv",
            f"https://corp/alice:{SENTINEL}@files.example.test/data/a.csv",
        ],
    )
    def test_a_failing_driver_that_quotes_an_accepted_url_shows_no_credential(
        self, monkeypatch, tmp_path, uri
    ):
        stub = _StubDiscoverer(error=RuntimeError(f"HTTP GET error on '{uri}' ({uri!r})"))
        with pytest.raises(ConnectivityProbeError) as info:
            _run(monkeypatch, tmp_path, uri, stub)
        assert stub.seen == [uri]
        assert SENTINEL not in info.value.why + info.value.as_json()


class TestClosingLineNamesOnlyWhatTheDisplayShows:
    """The closing line names a query parameter only where the display shows that name.

    The display reads the user:password part up to the last "@". A URL whose user
    name, or whose token, holds a raw "?" is used as given, and ``urlsplit`` reads
    what follows that "?" as the query: what the display masks as the password
    or the token would be listed as parameter names. Such a name is shown as the
    display shows it, masked.
    """

    @pytest.mark.parametrize(
        "uri",
        [
            f"https://corp?alice:{SENTINEL}=x@files.example.test/data/a.csv",
            f"https://corp?alice:ab&{SENTINEL}=1@files.example.test/data/a.csv",
            f"s3://KEY?alice:{SENTINEL}=x@bucket/a.csv",
            f"https://tok?{SENTINEL}=part9@files.example.test/data/a.csv",
            f"https://{SENTINEL}?tail=part9@files.example.test/data/a.csv",
        ],
    )
    def test_no_part_of_a_masked_user_name_or_password_is_named(
        self, monkeypatch, tmp_path, capsys, caplog, uri
    ):
        with caplog.at_level(logging.DEBUG):
            assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err + caplog.text
        hint = _hint(out)
        assert "tail" not in hint and "alice" not in hint, hint
        # A piece with no "=" has no name to show: "(unnamed)", as anywhere else.
        assert handler._MASK in hint.partition("query parameter(s) ")[2], hint

    def test_a_shown_name_is_still_named(self, monkeypatch, tmp_path, capsys):
        # Read as user:password, the display masks "mail"; "code" it shows.
        uri = f"https://files.example.test/orders.csv?mail=ops@example.test&code={SENTINEL}"
        assert _run(monkeypatch, tmp_path, uri, _StubDiscoverer()) == 0
        out, _ = capsys.readouterr()
        assert f"query parameter(s) {handler._MASK}, code" in _hint(out), out


class TestRegisteredKeyThatIsNoSchemeWritesNoCredential:
    """A discoverer registered under a key that is no RFC 3986 scheme writes no credential.

    ``urlparse`` reads no authority after such a key (``my_db``, ``jdbc:postgresql``):
    the whole URI, user:password part included, is its path. The contract's
    connection is built from the text after the "://" read as it would be read
    after an RFC 3986 scheme, so it keeps no user:password part, and the project
    name is read from that path, not from the user:password part.
    """

    @pytest.mark.parametrize(
        ("key", "password"),
        [
            (key, password)
            # The passwords the refusal lets through, under such keys and under an
            # RFC 3986 scheme: the same contract. A raw "/", "?" or "#" in the
            # password is refused under either
            # (TestRegisteredKeyThatIsNoSchemeGetsTheSameRefusal).
            for key in ("my_db", "a_b", "jdbc:postgresql", "My_Db", "x y", "cockroach")
            for password in (SENTINEL, f"ab@{SENTINEL}", f"ab {SENTINEL}")
        ],
    )
    def test_contract_keeps_no_user_name_or_password(
        self, monkeypatch, tmp_path, capsys, key, password
    ):
        from fluid_build.cli.discover.registry import DISCOVERERS

        uri = f"{key}://alice:{password}@db.example.test:5432/shop?sslmode=require#part"
        stub = _StubDiscoverer()
        monkeypatch.setitem(DISCOVERERS, key, stub)
        assert handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri) == 0
        out, err = capsys.readouterr()
        assert stub.seen == [uri]
        written = _everything_written(tmp_path)
        for secret in (SENTINEL, handler._slug(SENTINEL), "alice"):
            assert secret not in written, written
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": f"{key}://db.example.test:5432/shop"}
        assert SENTINEL not in out + err
        hint = _hint(out)
        assert "the URI's user and password, query parameter(s) sslmode and #fragment" in hint

    def test_a_query_password_and_a_project_name(self, monkeypatch, tmp_path, capsys):
        from fluid_build.cli.discover.registry import DISCOVERERS

        uri = f"jdbc:postgresql://db.example.test/shop?user=alice&password={SENTINEL}"
        monkeypatch.setitem(DISCOVERERS, "jdbc:postgresql", _StubDiscoverer())
        assert handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri) == 0
        assert SENTINEL not in _everything_written(tmp_path)
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": "jdbc:postgresql://db.example.test/shop"}
        doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
        assert doc["id"] == "bronze.shop.public_orders", doc["id"]

    @pytest.mark.parametrize("key", ["postgres:replica", "mysql:x_y"])
    def test_closing_line_matches_the_contract_under_a_key_urlparse_reads_as_a_database(
        self, monkeypatch, tmp_path, capsys, key
    ):
        # urlparse reads "postgres" as the scheme of "postgres:replica://...", but the
        # contract is written under the key, as a URL: it holds no secretRef, so the
        # closing line must not send the user to one.
        from fluid_build.cli.discover.registry import DISCOVERERS

        monkeypatch.setitem(DISCOVERERS, key, _StubDiscoverer())
        uri = f"{key}://alice:{SENTINEL}@db.example.test/shop"
        assert handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri) == 0
        out, _ = capsys.readouterr()
        assert _connection(_contracts(tmp_path)[0]) == {"uri": f"{key}://db.example.test/shop"}
        hint = _hint(out)
        assert "Not written to the contracts: the URI's user and password." in hint, hint
        assert "PGPASSWORD" not in hint and "MYSQL_PWD" not in hint, hint

    def test_the_registration_docstring_says_what_the_contracts_keep(self):
        from fluid_build.cli.discover import registry

        doc = " ".join((registry.register_discoverer.__doc__ or "").split())
        for sentence in (
            "It reads the text after the ``://`` as it would be read after an RFC 3986 "
            "scheme, and refuses the URI before any connection wherever it refuses that "
            "text after one: by the rules for a database URI when ``discoverer`` is a "
            "``JdbcDiscoverer``, by the rules for a URL otherwise.",
            "The contracts are written from the same reading, so they keep no "
            "user:password part, except where a URL is used as given: a user name that "
            "holds a raw ``/`` before its ``:`` leaves the rest in the path, as it does "
            "after ``https://``.",
        ):
            assert sentence in doc, doc
        # The skipped check and the lenient driver it let quote a password are gone.
        assert "skips the" not in doc and "hd7" not in doc, doc


def _recording_jdbc_discoverer(key: str, seen: List[str]):
    """A JDBC discoverer registered for ``key`` that records each URI and connects nowhere."""
    from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer, JdbcSourceConfig

    class RecordingJdbcDiscoverer(JdbcDiscoverer):
        def discover(self, uri: str) -> List[DiscoveredStream]:
            seen.append(uri)
            return _StubDiscoverer().discover(uri)

    config = JdbcSourceConfig(
        schemes=(key,),
        default_port=26257,
        extension="postgres",
        attach_alias="pg",
        attach_type="postgres",
        database_dsn_key="dbname",
    )
    return RecordingJdbcDiscoverer(config=config)


def _discover_under(monkeypatch, tmp_path: Path, uri: str, key: str, jdbc: bool):
    """Run ``uri`` through a JDBC or a plain discoverer registered for ``key``.

    Returns the URIs the discoverer saw and the refusal, if any.
    """
    stub = _StubDiscoverer()
    discoverer = _recording_jdbc_discoverer(key, stub.seen) if jdbc else stub
    monkeypatch.setattr(handler, "get_discoverer", lambda _uri: discoverer)
    try:
        handler.run_discover(_args(tmp_path), logging.getLogger("t"), uri=uri)
    except SchemaValidationError as refusal:
        return stub.seen, refusal
    return stub.seen, None


class TestRegisteredKeyThatIsNoSchemeGetsTheSameRefusal:
    """A key that is no RFC 3986 scheme gets the refusal the same text gets after one.

    A discoverer registered under ``my_db`` or ``jdbc:cockroach`` is handed the URI
    as given, and may read a password with a raw "/", "?" or "#" in part as the
    host or port, as ``urlsplit`` does after an RFC 3986 scheme, and quote that
    part back. The text after the key's "://" is read as it would be after an RFC
    3986 scheme, and refused before any connection wherever that URI would be: for
    a JDBC discoverer by the rules of a database URI, for any other by those of a
    URL.
    """

    @pytest.mark.parametrize("jdbc", [False, True], ids=["url", "jdbc"])
    @pytest.mark.parametrize("key", ["my_db", "a_b", "jdbc:cockroach", "x y", "My_Db"])
    @pytest.mark.parametrize(
        "rest",
        [
            f"alice:ab/{SENTINEL}@db.example.test/shop",
            f"alice:ab?{SENTINEL}@db.example.test/shop",
            f"alice:ab#{SENTINEL}@db.example.test/shop",
            f"alice:12/{SENTINEL}@db.example.test/shop",
            f"alice:ab@{SENTINEL}/x@db.example.test/shop",
            f"alice:{SENTINEL}@[::1/shop",
            f"db.example.test:5432/shop?note=a:{SENTINEL}@c",
        ],
    )
    def test_refused_where_an_rfc_scheme_is(
        self, monkeypatch, tmp_path, capsys, caplog, jdbc, key, rest
    ):
        seen, control = _discover_under(
            monkeypatch, tmp_path / "rfc", f"cockroach://{rest}", "cockroach", jdbc
        )
        assert control is not None and seen == []
        capsys.readouterr()
        with caplog.at_level(logging.DEBUG):
            seen, refusal = _discover_under(
                monkeypatch, tmp_path / "key", f"{key}://{rest}", key, jdbc
            )
        out, err = capsys.readouterr()
        assert refusal is not None, out
        assert seen == []
        assert not (tmp_path / "key").exists() or not list((tmp_path / "key").iterdir())
        assert "cannot tell the password from the host" in refusal.what
        assert SENTINEL not in _everything_shown(refusal, out, err, caplog.text)

    @pytest.mark.parametrize("key", ["my_db", "a_b", "jdbc:cockroach", "x y"])
    @pytest.mark.parametrize(
        "rest",
        [
            f"corp/alice:{SENTINEL}@crdb.example.test/shop",
            f"/alice:{SENTINEL}@crdb.example.test/shop",
            f"corp?alice:{SENTINEL}@crdb.example.test/shop",
        ],
    )
    def test_a_jdbc_discoverer_refuses_a_credential_typed_after_the_host(
        self, monkeypatch, tmp_path, capsys, caplog, key, rest
    ):
        # A database URI's path is a database name: a credential typed with an
        # extra "/" (or "?") would go to "corp", or to the local socket, as one.
        with caplog.at_level(logging.DEBUG):
            seen, refusal = _discover_under(monkeypatch, tmp_path, f"{key}://{rest}", key, True)
        out, err = capsys.readouterr()
        assert refusal is not None, out
        assert seen == []
        assert not _contracts(tmp_path)
        assert SENTINEL not in _everything_shown(refusal, out, err, caplog.text)

    @pytest.mark.parametrize("key", ["my_db", "jdbc:cockroach"])
    def test_a_url_discoverer_uses_such_a_uri_as_an_rfc_scheme_does(
        self, monkeypatch, tmp_path, capsys, key
    ):
        # Documented for a URL: a user name with a raw "/" before its ":" cannot be
        # told apart from an object key with a ":" and an "@", so it is used as
        # given, under a key as under an RFC 3986 scheme.
        rest = f"corp/alice:{SENTINEL}@db.example.test/shop"
        written = {}
        for name in ("cockroach", key):
            seen, refusal = _discover_under(
                monkeypatch, tmp_path / _slug_dir(name), f"{name}://{rest}", name, False
            )
            assert refusal is None and seen == [f"{name}://{rest}"]
            (contract,) = _contracts(tmp_path / _slug_dir(name))
            written[name] = _connection(contract)["uri"].split("://", 1)[1]
        assert written["cockroach"] == written[key] == rest
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err

    def test_a_jdbc_discoverer_under_such_a_key_keeps_no_credential(
        self, monkeypatch, tmp_path, capsys
    ):
        uri = f"jdbc:cockroach://alice:{SENTINEL}@crdb.example.test:26257/shop"
        seen, refusal = _discover_under(monkeypatch, tmp_path, uri, "jdbc:cockroach", True)
        assert refusal is None and seen == [uri]
        written = _everything_written(tmp_path)
        for secret in (SENTINEL, handler._slug(SENTINEL), "alice"):
            assert secret not in written, written
        (contract,) = _contracts(tmp_path)
        assert _connection(contract) == {"uri": "jdbc:cockroach://crdb.example.test:26257/shop"}
        out, err = capsys.readouterr()
        assert SENTINEL not in out + err


def _slug_dir(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


class TestDiscoverHelpSaysWhatIsRefused:
    """The --discover help states the refusal rule and what is used as given."""

    def test_help_matches_the_rule(self, monkeypatch, tmp_path, capsys):
        shown = _init_help(monkeypatch, tmp_path, capsys)
        for sentence in (
            "A password in the URI is never written to the contracts, whatever the "
            "scheme, unless a raw '/' puts part of it in the path, as below: a Postgres "
            "or MySQL contract gets secretRef env://<VAR> in its place, and any other "
            "contract keeps no user:password part.",
            "A URL's query string and #fragment are not written either; the command "
            "names the parameters it left out, and masks a name the progress line masks "
            "as part of the user:password part.",
            "A URI with a ':' before an '@' that comes after the first '/', '?' or '#' "
            "after '://' is refused before any connection when the text before that "
            "character holds a ':' (alice:pa/ss@host), a port's included "
            "(localhost:9000/bucket/a@b.csv), or holds an '@' or a bracket, or when a "
            "URL parser cannot read the URI (an unbalanced '['): the parser may read "
            "part of the password as the host or port.",
            "For an http:// or https:// URL, percent-encode such an '@'.",
            "A ':' and an '@' that both come after that character, behind a host name "
            "alone, are the path's, query's or fragment's own "
            "(s3://bucket/2024-01-01T10:00:00/a@b.csv), and the URL is used as given.",
            "So is a URL whose user name holds a raw '/', '?' or '#' before its ':': the "
            "text before that character is contacted as the host, and after a '/' the "
            "password is written to the contracts as part of the path. The output still "
            "masks it. A database URI like that is refused.",
        ):
            # A long example may be folded at the console width: compare unwrapped.
            assert _unwrapped(sentence) in _unwrapped(shown), (sentence, shown)
        # Round 18's port reading, and the password shape it let through, are gone.
        for gone in ("is read as the port's", "is read as a port and a path"):
            assert gone not in shown, gone

    def test_help_renders_through_argparse_as_well(self, monkeypatch, tmp_path, capsys):
        # "fluid init --help" prints the help string as written (help_formatter);
        # any longer command line reaches argparse, which %-formats every help
        # string (docs.python.org/3/library/argparse.html#help): a bare "%" in it
        # breaks the whole help.
        from fluid_build.cli import main

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        with pytest.raises(SystemExit) as info:
            main(["init", "--yes", "--help"])
        out, err = capsys.readouterr()
        assert info.value.code == 0, out + err
        shown = _unwrapped(out)
        for sentence in (
            "Keep the password out of the URI",
            "percent-encode such an '@'.",
            "(s3://bucket/2024-01-01T10:00:00/a@b.csv), and the URL is used as given.",
        ):
            assert _unwrapped(sentence) in shown, (sentence, out + err)

    @pytest.mark.parametrize(
        ("uri", "refused"),
        [
            # The help's own examples, behaving as it says.
            ("https://alice:pa/ss@host/a.csv", True),
            ("http://[::1/alice:pa@host/a.csv", True),
            ("http://localhost:9000/bucket/a@b.csv", True),
            ("http://localhost:9000/bucket/a%40b.csv", False),
            ("s3://bucket/2024-01-01T10:00:00/a@b.csv", False),
            ("https://corp/alice:pa@host/a.csv", False),
            ("postgres://corp/alice:pa@host/db", True),
        ],
    )
    def test_help_examples_behave_as_it_says(self, monkeypatch, tmp_path, uri, refused):
        stub = _StubDiscoverer()
        if refused:
            with pytest.raises(SchemaValidationError):
                _run(monkeypatch, tmp_path, uri, stub)
            assert stub.seen == []
        else:
            assert _run(monkeypatch, tmp_path, uri, stub) == 0
            assert stub.seen == [uri]
