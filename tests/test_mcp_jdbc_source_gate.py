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

"""The ``forge_from_source`` JDBC path is gated like an inline credential block.

A JDBC URI reaches a database — and this host's credentials, files and
identity — over the LLM-facing wire, so it is classified before any connection:

* a password in the URI (inline material)      -> --allow-inline-credentials
* no password (would use this host's material)  -> --allow-metadata-service
* a sqlite URI (names a server-side file)       -> --allow-metadata-service

Refused otherwise with a typed ``McpToolRefusal`` naming the exact flag. The
client's own ``allow_metadata_service`` argument is not read on this path:
granting is the operator's call alone. The operator's own CLI never passes
through this gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.cli.mcp.dispatch import McpToolRefusal, _call_tool, _gate_jdbc_source

PG_NO_PW = "postgresql://fluid@db.internal:5432/sales"
PG_PW = "postgresql://fluid:s3cret@db.internal:5432/sales"  # pragma: allowlist secret
MYSQL_PW = "mysql://u:s3cret@db.internal:3306/sales"  # pragma: allowlist secret
SQLITE = "sqlite:///var/lib/app/data.db"


def _gate(source, uri, *, inline=False, metadata=False):
    _gate_jdbc_source(source, uri, allow_inline_credentials=inline, allow_metadata_service=metadata)


class TestGateClassification:
    def test_no_password_refused_naming_metadata_service(self):
        with pytest.raises(McpToolRefusal, match="no password"):
            _gate("postgres", PG_NO_PW)
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("postgres", PG_NO_PW)

    def test_no_password_allowed_with_operator_metadata_flag(self):
        _gate("postgres", PG_NO_PW, metadata=True)  # no raise

    def test_no_password_allowed_with_env_opt_in(self, monkeypatch):
        monkeypatch.setenv("FLUID_ALLOW_METADATA_SERVICE", "1")
        _gate("postgres", PG_NO_PW)  # no raise

    def test_password_refused_naming_inline_credentials(self):
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("postgres", PG_PW)

    def test_password_allowed_with_inline_flag(self):
        _gate("postgres", PG_PW, inline=True)  # no raise

    def test_password_not_allowed_by_metadata_flag_alone(self):
        # Inline material needs the inline opt-in, not the metadata one.
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("postgres", PG_PW, metadata=True)

    def test_mysql_password_refused_naming_inline_credentials(self):
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("mysql", MYSQL_PW)

    def test_postgresql_alias_is_classified(self):
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("postgresql", PG_PW)

    def test_sqlite_refused_naming_metadata_service(self):
        with pytest.raises(McpToolRefusal, match="sqlite"):
            _gate("sqlite", SQLITE)
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("sqlite", SQLITE)

    def test_sqlite_allowed_with_metadata_flag(self):
        _gate("sqlite", SQLITE, metadata=True)  # no raise

    def test_missing_uri_defers_to_the_dispatcher(self):
        # The dispatcher raises its own 'requires uri' error; the gate no-ops.
        _gate("postgres", None)
        _gate("postgres", "")


class TestCallToolWiring:
    """The gate fires inside ``_call_tool`` before any connection is attempted,
    so nothing is written and no host credential ever leaves the box."""

    def test_no_password_refused_before_any_connection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PGPASSWORD", "hostsecret")  # would be the host's own
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _call_tool(
                "forge_from_source",
                {"source": "postgres", "uri": PG_NO_PW, "output_path": str(out)},
                read_only=False,
            )
        assert not out.exists()

    def test_password_refused_before_any_connection(self, tmp_path):
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _call_tool(
                "forge_from_source",
                {"source": "postgres", "uri": PG_PW, "output_path": str(out)},
                read_only=False,
            )
        assert not out.exists()

    def test_client_allow_metadata_service_arg_does_not_grant(self, tmp_path):
        # A client self-granting via the argument must not bypass the operator gate.
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(McpToolRefusal):
            _call_tool(
                "forge_from_source",
                {
                    "source": "postgres",
                    "uri": PG_NO_PW,
                    "output_path": str(out),
                    "allow_metadata_service": True,
                },
                read_only=False,
            )
        assert not out.exists()

    def test_sqlite_refused_before_opening_the_file(self, tmp_path):
        db = tmp_path / "source.db"
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _call_tool(
                "forge_from_source",
                {"source": "sqlite", "uri": f"sqlite:///{db}", "output_path": str(out)},
                read_only=False,
            )
        assert not db.exists()
        assert not out.exists()


class TestGateClassificationMatrix:
    """Every URI shape is classified before a connection; anything that would use
    this host's identity (its OS user, its local socket) needs the metadata gate."""

    @pytest.mark.parametrize(
        "uri", [["postgresql://u:p@h/db"], {"uri": "x"}, 5, b"postgresql://u:p@h/db"]
    )
    def test_non_string_uri_is_refused(self, uri):
        with pytest.raises(McpToolRefusal, match="must be a string"):
            _gate("postgres", uri, inline=True, metadata=True)

    @pytest.mark.parametrize(
        "source,uri",
        [
            ("postgres", "mysql://u:p@h:3306/db"),
            ("postgresql", "sqlite:///tmp/x.db"),
            ("mysql", "postgresql://u:p@h:5432/db"),
            ("mysql", "mariadb://u:p@h:3306/db"),
            ("mysql", "mysql+pymysql://u:p@h:3306/db"),
            ("postgres", "jdbc:postgresql://u:p@h:5432/db"),
            ("sqlite", "file:///etc/passwd"),
            ("sqlite", "duckdb:///tmp/x.duckdb"),
            ("sqlite", "/var/lib/app/data.db"),
        ],
    )
    def test_scheme_must_match_the_source(self, source, uri):
        with pytest.raises(McpToolRefusal, match="scheme"):
            _gate(source, uri, inline=True, metadata=True)

    @pytest.mark.parametrize(
        "source,uri",
        [
            ("postgres", "POSTGRESQL://u:s3cret@h:5432/db"),  # pragma: allowlist secret
            ("postgres", "postgres://u:s3cret@h:5432/db"),  # pragma: allowlist secret
            ("mysql", "MySQL://u:s3cret@h:3306/db"),  # pragma: allowlist secret
        ],
    )
    def test_scheme_match_is_case_insensitive(self, source, uri):
        _gate(source, uri, inline=True)  # no raise

    def test_uppercase_sqlite_scheme_is_still_a_server_path(self):
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("sqlite", "SQLITE:///var/lib/app/data.db", inline=True)

    @pytest.mark.parametrize(
        "source,uri",
        [
            ("postgres", "postgresql://fluid:@db.internal:5432/sales"),
            ("mysql", "mysql://fluid:@db.internal:3306/sales"),
        ],
    )
    def test_empty_password_is_no_password(self, source, uri):
        # libpq and the mysql client both fall back to the host's credential.
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate(source, uri, inline=True)

    @pytest.mark.parametrize(
        "source,uri",
        [
            ("postgres", "postgresql://:s3cret@db.internal:5432/sales"),  # pragma: allowlist secret
            ("mysql", "mysql://:s3cret@db.internal:3306/sales"),  # pragma: allowlist secret
        ],
    )
    def test_empty_user_uses_this_hosts_login_name(self, source, uri):
        # With no user, libpq takes PGUSER or the OS login, and the mysql client
        # sends the OS login name: this host's identity, not the caller's.
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate(source, uri, inline=True)
        _gate(source, uri, inline=True, metadata=True)  # no raise

    @pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "localhost."])
    def test_mysql_localhost_is_the_hosts_unix_socket(self, host):
        # The mysql client maps "localhost" to the default unix socket, where
        # socket authentication identifies the server's OS user.
        uri = f"mysql://u:s3cret@{host}:3306/sales"  # pragma: allowlist secret
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("mysql", uri, inline=True)
        _gate("mysql", uri, inline=True, metadata=True)  # no raise

    @pytest.mark.parametrize("hosts", [",", "db.internal,", ",db.internal", "a,,b", "a, ,b"])
    def test_postgres_host_list_with_an_empty_element_is_the_hosts_unix_socket(self, hosts):
        # libpq reads an empty element of a host list as the default unix
        # socket directory, where peer or trust authentication identifies the
        # server's OS user whatever password the URI carries.
        uri = f"postgresql://u:s3cret@{hosts}:5432/sales"  # pragma: allowlist secret
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("postgres", uri, inline=True)
        _gate("postgres", uri, inline=True, metadata=True)  # no raise

    def test_postgres_host_list_of_named_hosts_uses_the_inline_gate(self):
        uri = (
            "postgresql://u:s3cret@db1.internal,db2.internal:5432/sales"  # pragma: allowlist secret
        )
        _gate("postgres", uri, inline=True)  # no raise

    def test_mysql_loopback_address_uses_tcp_and_the_inline_gate(self):
        uri = "mysql://u:s3cret@127.0.0.1:3306/sales"  # pragma: allowlist secret
        _gate("mysql", uri, inline=True)  # no raise

    @pytest.mark.parametrize(
        "query",
        [
            "?password=x",
            "?passfile=/home/op/.pgpass",
            "?service=prod",
            "?sslkey=/home/op/.postgresql/postgresql.key",
            "?sslcert=/home/op/.postgresql/postgresql.crt",
            "?host=/var/run/postgresql",
            "?options=-c%20log_statement=all",
        ],
    )
    def test_query_parameters_never_reach_the_connection(self, query):
        # _parse_uri keeps host, port, user, password and database only, so a
        # query parameter can neither add credential material nor redirect it.
        from fluid_build.cli.discover._jdbc_introspect import _attach_string_postgres, _parse_uri

        uri = f"postgresql://u:s3cret@db.internal:5432/sales{query}"  # pragma: allowlist secret
        # Classified on the URI's own password; a host= naming a socket directory
        # also needs the metadata-service opt-in (see the host-list table below).
        _gate("postgres", uri, inline=True, metadata=query.startswith("?host="))
        sql = _attach_string_postgres(_parse_uri(uri, "postgres"), "s")
        assert query.split("=", 1)[1] not in sql


# Each row: a postgres URI that carries a password, and the operator flag it needs
# beyond --allow-inline-credentials. HOST_IDENTITY rows reach this host's own
# unix-domain socket, where peer or trust authentication identifies the
# server's OS user, so they need --allow-metadata-service too.
#
# Checked against libpq's connection-string rules
# (https://www.postgresql.org/docs/16/libpq-connect.html, "Connection URIs" and
# the host / hostaddr keywords) and against libpq 16.15 itself (psql asked to
# connect to each URI with no server, which names every target it tries): an
# empty host, an empty element of a host list, a host that is an absolute path
# (a percent-encoded '/' decodes to one) and a host= parameter that is empty or
# a path all select a unix-domain socket, and a URI query parameter overrides
# the host part. libpq percent-decodes the whole host part before it splits it
# on ',', so an encoded comma ('%2C' or '%2c') separates hosts as a bare one
# does: 'db%2C' is the list 'db' then the default socket. Two rows are refused more strictly than libpq would connect: a
# blank element (' '), which libpq looks up as a host name, and an empty
# hostaddr=, which libpq treats as unset (it then uses the host part). The gate
# does not reproduce those rules, so it refuses both without the opt-in.
HOST_IDENTITY = "--allow-metadata-service"
INLINE_ONLY = "--allow-inline-credentials"
HOST_LIST_TABLE = [
    ("postgres://u:p@,/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db,/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@,db/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@a,,b/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@a, ,b/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@a:5432,,b:5433/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@:5432/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@%2Fvar%2Frun%2Fpostgresql/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db/db?host=/var/run/postgresql", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db/db?host=", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db/db?hostaddr=", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@[::1],[::2]/db", INLINE_ONLY),  # pragma: allowlist secret
    ("postgres://u:p@[::1],/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@a:5432,b:5433/db", INLINE_ONLY),  # pragma: allowlist secret
    ("postgres://u:p@db/db", INLINE_ONLY),  # pragma: allowlist secret
    ("postgres://u:p@db%2C/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@%2Cdb/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db%2c%2cb/db", HOST_IDENTITY),  # pragma: allowlist secret
    ("postgres://u:p@db%2Cdb2/db", INLINE_ONLY),  # pragma: allowlist secret
]


class TestHostListClassificationTable:
    """Which operator flag each postgres host-list spelling requires over MCP.

    The gate must classify every row with a typed refusal that names the flag,
    not fail on a spelling its URI parser does not expect: a ValueError is
    neither a classification nor a message the client can act on.
    """

    @pytest.mark.parametrize("uri,needs", HOST_LIST_TABLE)
    def test_the_flag_each_uri_requires(self, uri, needs):
        if needs == HOST_IDENTITY:
            with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
                _gate("postgres", uri, inline=True)
        else:
            _gate("postgres", uri, inline=True)  # no raise
        # Every row carries a password, so the inline opt-in is always needed.
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("postgres", uri, metadata=True)
        with pytest.raises(McpToolRefusal):
            _gate("postgres", uri)
        _gate("postgres", uri, inline=True, metadata=True)  # no raise

    @pytest.mark.parametrize(
        "uri",
        [
            # libpq: a host that starts with '@' is an abstract unix socket.
            "postgres://u:p@%40fluid/db",  # pragma: allowlist secret
            # A lower-case percent escape decodes to '/' as well.
            "postgres://u:p@%2fvar%2frun%2fpostgresql/db",  # pragma: allowlist secret
            # A host list given as a parameter, with an empty element.
            "postgres://u:p@db/db?host=a,,b",  # pragma: allowlist secret
            # A host= given twice: libpq keeps the last one.
            "postgres://u:p@db/db?host=db2&host=/tmp",  # pragma: allowlist secret
            # An encoded comma inside brackets: libpq decodes it to two empty hosts.
            "postgres://u:p@[%2C]/db",  # pragma: allowlist secret
        ],
    )
    def test_more_spellings_of_a_local_socket(self, uri):
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("postgres", uri, inline=True)
        _gate("postgres", uri, inline=True, metadata=True)  # no raise

    @pytest.mark.parametrize(
        "uri",
        [
            "postgres://u:p@a:5432,b:5433/db",  # pragma: allowlist secret
            "postgres://u:p@[::1],[::2]/db",  # pragma: allowlist secret
        ],
    )
    def test_a_named_list_the_attach_cannot_read_never_connects(self, monkeypatch, tmp_path, uri):
        # The gate lets these through on the inline opt-in; the attach's own URI
        # parser then refuses them, before any connection is attempted.
        pytest.importorskip("duckdb")
        import fluid_build.providers._duckdb_sandbox as sandbox

        connects: list = []
        monkeypatch.setattr(sandbox, "secure_duckdb_connect", lambda *a, **k: connects.append(a))
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                {"source": "postgres", "uri": uri, "output_path": str(out)},
                read_only=False,
                allow_inline_credentials=True,
            )
        assert connects == []
        assert not out.exists()


# A password typed with an unencoded '@'. libpq, given the raw URI, would end
# the password at the first '@' and read the rest as the host. The attach never
# gives libpq the raw URI: _parse_uri reads it with urllib, whose user
# information runs to the last '@' before the host, and the ATTACH is built as
# key=value pairs from those fields. So each of these connects over TCP to the
# named host, with the whole password, and the gate classifies that host.
# (uri, the password the attach sends, the host it connects to)
AT_SIGN_PASSWORDS = [
    (
        "postgresql://app_user:Summer2024@@db.internal.example:5432/shop",  # pragma: allowlist secret
        "Summer2024@",  # pragma: allowlist secret
        "db.internal.example",
    ),
    (
        "postgresql://app_user:Pa@:ss-77@db.internal.example:5432/shop",  # pragma: allowlist secret
        "Pa@:ss-77",  # pragma: allowlist secret
        "db.internal.example",
    ),
    (
        "postgresql://app_user:Pa@,ss-77@db.internal.example:5432/shop",  # pragma: allowlist secret
        "Pa@,ss-77",  # pragma: allowlist secret
        "db.internal.example",
    ),
    # libpq's own reading of this one is the abstract socket '@abs'; the attach
    # connects to the host 'abs'.
    ("postgres://u:p@@abs/db", "p@", "abs"),  # pragma: allowlist secret
]
AT_SIGN_IDS = ["ends_with_at", "at_colon", "at_comma", "at_then_named_host"]


def _record_attach(monkeypatch) -> list:
    """Record the ATTACH the attach would run; refuse the connection itself."""
    pytest.importorskip("duckdb")
    import fluid_build.providers._duckdb_sandbox as sandbox

    attaches: list = []

    class _Con:
        def execute(self, sql, *_a, **_k):
            attaches.append(sql)
            raise RuntimeError("no connection in this test")

    def connect(*_a, before_lock=None, **_k):
        if before_lock is not None:
            before_lock(_Con())
        raise RuntimeError("no connection in this test")

    monkeypatch.setattr(sandbox, "secure_duckdb_connect", connect)
    return attaches


class TestUnencodedAtSignInThePassword:
    """The gate classifies the host the attach connects to, not libpq's reading
    of a URI the attach never hands to libpq."""

    @pytest.mark.parametrize("uri,password,host", AT_SIGN_PASSWORDS, ids=AT_SIGN_IDS)
    def test_a_named_host_needs_only_the_inline_opt_in(self, uri, password, host):
        _gate("postgres", uri, inline=True)  # no raise
        with pytest.raises(McpToolRefusal, match="--allow-inline-credentials"):
            _gate("postgres", uri, metadata=True)

    @pytest.mark.parametrize("uri,password,host", AT_SIGN_PASSWORDS, ids=AT_SIGN_IDS)
    def test_the_attach_connects_over_tcp_to_that_host(
        self, uri, password, host, monkeypatch, tmp_path
    ):
        from fluid_build.providers._sql_safety import libpq_escape

        attaches = _record_attach(monkeypatch)
        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                {"source": "postgres", "uri": uri, "output_path": str(tmp_path / "c.yaml")},
                read_only=False,
                allow_inline_credentials=True,
            )
        (attach,) = attaches
        # key=value pairs built from urllib's fields: the named host, the whole
        # password, and the pin. The raw URI never reaches libpq.
        assert f"host={host} " in attach
        assert f" password={libpq_escape(password)} " in attach
        assert "passfile=" in attach and "sslcert=" in attach
        assert "://" not in attach

    @pytest.mark.parametrize(
        "uri",
        [
            # The host part urllib reads is still read with libpq's rules.
            "postgres://u:p@@%2Ftmp/db",  # pragma: allowlist secret
            "postgres://u:p@@%40abs/db",  # pragma: allowlist secret
            "postgres://u:p@@db,/db",  # pragma: allowlist secret
            "postgres://u:p@@db%2C/db",  # pragma: allowlist secret
            "postgres://u:p@@db/db?host=/tmp",  # pragma: allowlist secret
        ],
    )
    def test_a_socket_after_the_last_at_sign_still_needs_the_metadata_opt_in(self, uri):
        with pytest.raises(McpToolRefusal, match="--allow-metadata-service"):
            _gate("postgres", uri, inline=True)
        _gate("postgres", uri, inline=True, metadata=True)  # no raise

    @pytest.mark.parametrize(
        "uri",
        [
            # urllib finds no host after the last '@'; libpq's host after the
            # first one is the abstract socket '@'.
            "postgres://u:p@@/db",  # pragma: allowlist secret
            # urllib cannot read the port; libpq's host after the first '@' is empty.
            "postgresql://app_user:Pa@:ss-77@db:notaport/db",  # pragma: allowlist secret
        ],
    )
    def test_a_uri_urllib_cannot_read_is_classified_on_libpqs_reading_and_says_why(self, uri):
        # The attach would refuse these before connecting. The gate still
        # classifies them, on libpq's reading, and says the '@' is ambiguous
        # rather than calling a password fragment a socket path.
        with pytest.raises(McpToolRefusal) as caught:
            _gate("postgres", uri, inline=True)
        message = str(caught.value)
        assert "--allow-metadata-service" in message
        assert "more than one '@'" in message
        assert "encod" not in message.lower()
        assert "p@" not in message and "ss-77" not in message
        _gate("postgres", uri, inline=True, metadata=True)  # no raise

    def test_an_unambiguous_empty_host_keeps_its_own_message(self):
        with pytest.raises(McpToolRefusal) as caught:
            _gate("postgres", "postgres://u:p@:5432/db", inline=True)  # pragma: allowlist secret
        assert "more than one '@'" not in str(caught.value)
        assert "empty, blank or a socket path" in str(caught.value)


UNUSABLE_MYSQL_PW = "fakeMyPw7Q"  # pragma: allowlist secret
# mysql URIs the attach cannot use: no host, a port that is not one, or a
# netloc urllib cannot read. No second reading would name a usable host.
MYSQL_NO_HOST = [
    f"mysql://u:{UNUSABLE_MYSQL_PW}@/app",
    f"mysql://u:{UNUSABLE_MYSQL_PW}@:3306/app",
    "mysql://u@/app",
]
MYSQL_NO_USABLE_HOST = MYSQL_NO_HOST + [
    f"mysql://u:{UNUSABLE_MYSQL_PW}@db:notaport/app",
    f"mysql://u:{UNUSABLE_MYSQL_PW}@db:99999/app",
    f"mysql://u:{UNUSABLE_MYSQL_PW}/x@db/app",
    f"mysql://u:{UNUSABLE_MYSQL_PW}[x]@db/app",
]
_FLAG_SETS = [{}, {"inline": True}, {"metadata": True}, {"inline": True, "metadata": True}]


class TestMysqlUriWithNoUsableHost:
    """A mysql URI that cannot be used is a typed refusal, not a parser error.

    The attach would refuse it before connecting whatever the flags, so the
    refusal says no flag would help instead of asking the operator for one.
    """

    @pytest.mark.parametrize("flags", _FLAG_SETS, ids=["none", "inline", "metadata", "both"])
    @pytest.mark.parametrize("uri", MYSQL_NO_USABLE_HOST)
    def test_typed_password_free_and_refused_whatever_the_flags(self, uri, flags):
        with pytest.raises(McpToolRefusal) as caught:
            _gate("mysql", uri, **flags)
        message = str(caught.value)
        assert "no usable host" in message
        assert "No flag would make it connect" in message
        # Not the opt-in refusals' call to action: granting a flag would not help.
        assert "restart the server" not in message
        assert UNUSABLE_MYSQL_PW not in message
        assert caught.value.__cause__ is None and caught.value.__context__ is None

    @pytest.mark.parametrize("uri", MYSQL_NO_HOST)
    def test_an_empty_host_is_named_as_this_servers_socket(self, uri):
        # A mysql client reads an empty host as localhost, the class that needs
        # --allow-metadata-service when a URI names it; the refusal says so and
        # what to name instead.
        with pytest.raises(McpToolRefusal) as caught:
            _gate("mysql", uri, inline=True)
        message = str(caught.value)
        assert "localhost" in message and "--allow-metadata-service" in message
        assert "127.0.0.1" in message

    @pytest.mark.parametrize("uri", MYSQL_NO_USABLE_HOST)
    def test_refused_before_any_connection(self, uri, monkeypatch, tmp_path):
        attaches = _record_attach(monkeypatch)
        out = tmp_path / "contract.fluid.yaml"
        with pytest.raises(McpToolRefusal, match="no usable host"):
            _call_tool(
                "forge_from_source",
                {"source": "mysql", "uri": uri, "output_path": str(out)},
                read_only=False,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert attaches == []
        assert not out.exists()


class TestRefusalsOfferWhatTheJdbcPathReads:
    """A refusal's way out must work on the JDBC path.

    That path reads only ``uri`` (``_dispatch_forge_from_jdbc_source``), so
    ``credentials.credential_id``, a catalog-source field, is never read there
    and no JDBC refusal offers it. The no-password refusal names what a
    connection with no password falls back to (libpq's PGPASSWORD, ~/.pgpass
    and service file, the duckdb mysql extension's MYSQL_PWD) and nothing more.
    """

    @pytest.mark.parametrize(
        ("source", "uri"),
        [
            ("sqlite", SQLITE),
            ("postgres", "postgresql://db.internal:5432/sales"),  # no user name
            ("mysql", "mysql://u@localhost:3306/sales"),  # this host's socket
            ("postgres", PG_PW),  # inline password, no inline opt-in
            ("postgres", PG_NO_PW),
            ("mysql", "mysql://u@db.internal:3306/sales"),
        ],
        ids=["sqlite", "no-user", "mysql-localhost", "password", "pg-no-password", "my-no-pw"],
    )
    def test_no_refusal_offers_credential_id(self, source, uri):
        with pytest.raises(McpToolRefusal) as caught:
            _gate(source, uri)
        message = str(caught.value)
        assert "credential_id" not in message
        assert "fluid ai setup" not in message
        assert "from the CLI" in message

    def test_a_credential_id_beside_a_uri_is_not_read(self, monkeypatch, tmp_path):
        # The JDBC branch connects with the URI alone: a credential_id the
        # client adds changes neither the refusal nor the attach.
        attaches = _record_attach(monkeypatch)
        out = tmp_path / "contract.fluid.yaml"
        args = {"source": "postgres", "uri": PG_PW, "output_path": str(out)}
        with_id = {**args, "credentials": {"credential_id": "saved-pg"}}
        with pytest.raises(McpToolRefusal) as plain:
            _call_tool("forge_from_source", args, read_only=False)
        with pytest.raises(McpToolRefusal) as named:
            _call_tool("forge_from_source", with_id, read_only=False)
        assert str(named.value) == str(plain.value)
        for call in (args, with_id):
            with pytest.raises(RuntimeError, match="JDBC introspection failed"):
                _call_tool(
                    "forge_from_source", call, read_only=False, allow_inline_credentials=True
                )
        assert len(attaches) == 2 and attaches[0] == attaches[1]

    def test_no_password_refusal_names_only_what_the_attach_falls_back_to(self):
        with pytest.raises(McpToolRefusal) as caught:
            _gate("mysql", "mysql://u@db.internal:3306/sales")
        message = str(caught.value)
        for name in ("PGPASSWORD", "~/.pgpass", "libpq service", "MYSQL_PWD"):
            assert name in message
        assert "option file" not in message


MYSQL_LOCALHOST_PW = "mysql://u:s3cret@localhost:3306/sales"  # pragma: allowlist secret


class TestClientMetadataArgumentIsIgnored:
    """The schema says a JDBC source ignores the client's ``allow_metadata_service``.

    Only the operator's flag or ``FLUID_ALLOW_METADATA_SERVICE=1`` decides a
    JDBC host-identity row: the argument neither grants it nor is refused.
    """

    @pytest.mark.parametrize("uri", [PG_NO_PW, SQLITE, MYSQL_LOCALHOST_PW])
    def test_refusal_is_the_same_with_or_without_it(self, uri, tmp_path):
        source = uri.split(":", 1)[0].replace("postgresql", "postgres")
        args = {"source": source, "uri": uri, "output_path": str(tmp_path / "c.fluid.yaml")}
        with pytest.raises(McpToolRefusal) as without:
            _call_tool("forge_from_source", args, read_only=False, allow_inline_credentials=True)
        with pytest.raises(McpToolRefusal) as asked:
            _call_tool(
                "forge_from_source",
                {**args, "allow_metadata_service": True},
                read_only=False,
                allow_inline_credentials=True,
            )
        assert str(asked.value) == str(without.value)

    def test_an_allowed_call_is_not_refused_for_asking(self, monkeypatch, tmp_path):
        # Unlike a catalog call, asking without the operator's opt-in is no error.
        attaches = _record_attach(monkeypatch)
        with pytest.raises(RuntimeError, match="JDBC introspection failed"):
            _call_tool(
                "forge_from_source",
                {
                    "source": "postgres",
                    "uri": PG_PW,
                    "output_path": str(tmp_path / "c.fluid.yaml"),
                    "allow_metadata_service": True,
                },
                read_only=False,
                allow_inline_credentials=True,
            )
        assert len(attaches) == 1
