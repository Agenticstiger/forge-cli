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

"""Lock the libpq DSN redaction guarantee for :class:`PostgresBackend`.

When a connection string contains a password, the ``PostgresBackend``
must keep the plaintext value local to ``psycopg.connect`` and stash
only the redacted form on the instance, so a stray ``repr(backend)``
or log statement cannot leak the credential.

Two shapes are supported and both must be redacted:

* URL form          ``postgres://user:secret@host:5432/db``
* libpq keyword     ``host=... user=... password=secret dbname=...``
                    (with bare, single-quoted, and double-quoted values)

The tests don't open a live connection — that's covered by
:mod:`test_store_backends_e2e`. They focus on the redaction
guarantee, which is the part we want frozen so it can never silently
regress.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from fluid_build.copilot.store.backends.postgres import PostgresBackend, _redact_dsn

# ---------------------------------------------------------------------
# Pure-function redaction
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "dsn, banned_substring, expected_marker",
    [
        # URL form — single user:pass@host
        (
            "postgres://alice:topsecret@db.prod.example.com:5432/forge",
            "topsecret",
            "alice:***@",
        ),
        # URL form — scheme with ``+`` driver suffix + URL-encoded secret
        (
            "postgresql+psycopg://bob:p%40ssw0rd@host/app",
            "p%40ssw0rd",
            "bob:***@",
        ),
        # libpq keyword form — bare value
        (
            "host=db user=alice password=letmein dbname=forge",
            "letmein",
            "password=***",
        ),
        # libpq keyword form — single-quoted value with whitespace
        (
            "host=db user=bob password='hello world' dbname=forge",
            "hello world",
            "password=***",
        ),
        # libpq keyword form — double-quoted value with shell metacharacters
        (
            'host=db user=carol password="complex $ecret!" dbname=forge',
            "complex",
            "password=***",
        ),
    ],
)
def test_redact_dsn_strips_secret_and_leaves_marker(
    dsn: str,
    banned_substring: str,
    expected_marker: str,
) -> None:
    redacted = _redact_dsn(dsn)
    assert (
        banned_substring not in redacted
    ), f"redacted DSN still contains the secret substring {banned_substring!r}: {redacted!r}"
    assert (
        expected_marker in redacted
    ), f"redacted DSN missing the expected marker {expected_marker!r}: {redacted!r}"


def test_redact_dsn_passes_through_dsn_without_credentials() -> None:
    """A DSN with no embedded credential is unchanged."""

    dsn = "postgres://db.example.com:5432/forge"
    assert _redact_dsn(dsn) == dsn


def test_redact_dsn_handles_empty_input() -> None:
    """Empty / falsey input is returned unchanged (defensive)."""

    assert _redact_dsn("") == ""


# ---------------------------------------------------------------------
# PostgresBackend instance hardening
# ---------------------------------------------------------------------


class _FakeConnection:
    """Minimal stand-in for ``psycopg.Connection`` so we don't need a
    live database to assert the redaction contract."""

    def __init__(self) -> None:
        self.commits = 0

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor()

    def commit(self) -> None:
        self.commits += 1


class _FakeCursor:
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return None


@pytest.fixture
def fake_psycopg(monkeypatch):
    """Replace the lazily-imported ``psycopg`` with a stub so
    ``PostgresBackend.__init__`` succeeds without a live database.
    The stub records the DSN passed to ``connect`` so we can assert
    the plaintext value DID flow into the driver, while the public
    instance attribute stayed redacted."""

    captured = {"connect_args": None}

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str) -> _FakeConnection:
            captured["connect_args"] = dsn
            return _FakeConnection()

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psycopg":
            return _FakePsycopg
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    return captured


@pytest.mark.parametrize(
    "dsn, banned_substring",
    [
        ("postgres://alice:topsecret@db.prod.example.com:5432/forge", "topsecret"),
        ("host=db user=bob password=letmein dbname=forge", "letmein"),
    ],
)
def test_postgres_backend_stashes_redacted_dsn_on_instance(
    fake_psycopg, dsn: str, banned_substring: str
) -> None:
    """The plaintext DSN must be passed to ``psycopg.connect``, but
    must NOT survive on the instance after construction."""

    backend = PostgresBackend(dsn)

    # 1. The driver received the plaintext (otherwise authentication
    #    would never work).
    assert fake_psycopg["connect_args"] == dsn

    # 2. The public instance attribute is the redacted form — no
    #    secret substring survives.
    assert banned_substring not in backend.dsn
    assert "***" in backend.dsn


@pytest.mark.parametrize(
    "dsn, banned_substring",
    [
        ("postgres://alice:topsecret@db.prod.example.com:5432/forge", "topsecret"),
        ("host=db user=bob password=letmein dbname=forge", "letmein"),
    ],
)
def test_postgres_backend_repr_does_not_leak_secret(
    fake_psycopg, dsn: str, banned_substring: str
) -> None:
    """``repr(backend)`` is the most common accidental log path —
    confirm it never echoes the password."""

    backend = PostgresBackend(dsn)
    rendered = repr(backend)

    assert banned_substring not in rendered
    assert "PostgresBackend" in rendered
    assert "***" in rendered
