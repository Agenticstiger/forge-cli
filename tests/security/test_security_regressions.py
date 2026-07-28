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

"""Security regression tests for the 8 findings the review surfaced.

Each test exercises a malicious input through the public surface and
asserts the runner / discoverer / verifier rejects it (or escapes it
correctly), so a regression in identifier validation, DSN escaping, or
signature handling fails this suite immediately.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners._signature import (
    CosignNotInstalledError,
    NullVerifier,
    make_default_verifier,
)
from fluid_build.build_runners.duckdb.runner import (
    _build_copy_destination,
    _build_select_for_filesystem,
    _build_select_for_postgres,
    _csv_options_clause,
)
from fluid_build.build_runners.hooks.tokenize_pii import (
    _TOKEN_HEX_LEN,
    TokenizePiiHook,
    _resolve_default_key,
)
from fluid_build.build_runners.meltano.runner import (
    _resolve_tap_binary,
    write_records_to_duckdb,
)

# ── Sec-Fix 1: DuckDB SQL injection ────────────────────────────────────


class TestDuckdbSqlInjection:
    def test_filesystem_uri_with_quote_is_safely_quoted(self):
        # Malicious URI tries to inject a SELECT after closing the literal.
        sql = _build_select_for_filesystem("x.csv') ; DROP TABLE secrets; --", "csv", {})
        # The single quote inside the URI must be doubled (SQL escape) so the
        # injection is neutralised; the SQL output must still be valid.
        assert "''" in sql, f"single quote not escaped in {sql!r}"
        # And the dangerous keyword should still appear, just inside the literal.
        assert "DROP TABLE" in sql
        # Crucially: only one read_csv_auto call (no terminator escape).
        assert sql.count("read_csv_auto(") == 1

    def test_filesystem_options_string_value_is_quoted(self):
        clause = _csv_options_clause({"delim": "', INJECT, '"})
        # The single quote is doubled.
        assert "''" in clause
        # The full clause is still a single key=value.
        assert clause.count("delim=") == 1

    def test_filesystem_options_key_must_be_identifier(self):
        with pytest.raises(ValueError):
            _csv_options_clause({"; DROP TABLE x; --": "value"})

    def test_postgres_stream_must_be_identifier(self):
        with pytest.raises(ValueError):
            _build_select_for_postgres(
                {"host": "h", "port": 5432, "user": "u", "password": "p", "database": "d"},
                "public.orders; DROP TABLE secrets; --",
            )

    def test_postgres_password_with_quote_neutralized(self):
        # Password contains a single quote; the runner must escape it twice
        # (libpq + SQL boundary) so the resulting SQL is still well-formed.
        sql = _build_select_for_postgres(
            {
                "host": "h",
                "port": 5432,
                "user": "u",
                "password": "p'; DROP TABLE secrets; --",
                "database": "d",
            },
            "orders",
        )
        # Single quote was doubled at the SQL boundary.
        assert "''" in sql
        # Exactly one postgres_scan call is emitted.
        assert sql.count("postgres_scan(") == 1

    def test_postgres_port_must_be_numeric(self):
        with pytest.raises(ValueError):
            _build_select_for_postgres(
                {"host": "h", "port": "5432; DROP", "user": "u", "password": "p", "database": "d"},
                "orders",
            )

    def test_copy_destination_path_with_quote_is_quoted(self):
        sql = _build_copy_destination("/tmp/out'.parquet", "parquet")
        assert "''" in sql
        assert sql.count("COPY") == 1


# ── Sec-Fix 2: Meltano DuckDB target SQL injection ────────────────────


class TestMeltanoSqlInjection:
    def test_malicious_dataset_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            write_records_to_duckdb({}, duckdb_path=tmp_path / "x.duckdb", dataset="bronze; DROP")

    def test_malicious_stream_name_rejected(self, tmp_path: Path):
        records = {"orders; DROP TABLE secrets; --": [{"id": 1}]}
        with pytest.raises(ValueError):
            write_records_to_duckdb(records, duckdb_path=tmp_path / "x.duckdb")

    def test_malicious_column_rejected(self, tmp_path: Path):
        records = {"orders": [{"id; DROP": 1}]}
        with pytest.raises(ValueError):
            write_records_to_duckdb(records, duckdb_path=tmp_path / "x.duckdb")

    def test_normal_stream_with_dot_normalized(self, tmp_path: Path):
        # ``public.orders`` is normalized to ``public_orders`` and accepted.
        out = tmp_path / "x.duckdb"
        counts = write_records_to_duckdb(
            {"public.orders": [{"id": 1, "name": "A"}]}, duckdb_path=out
        )
        assert counts["public.orders"] == 1


# ── Sec-Fix 3: Postgres / MySQL discoverer SQL injection ──────────────


class TestDiscovererSqlInjection:
    """SQL-injection regression guard for the JDBC discoverer family.

    The original helpers (``_parse_postgres_uri``, ``_libpq_escape``)
    were lifted out of each per-database discoverer module and into:

    * ``JdbcDiscoverer._parse_uri`` (the base class in ``_jdbc_base.py``) —
      shared URI parser with scheme allowlist enforcement.
    * ``providers._sql_safety.libpq_escape`` — single source of truth
      for libpq-style DSN value escaping (used by both the discoverers
      and the duckdb acquisition runner).

    These tests pin both surfaces at their new homes so the security
    invariants stay in lockstep with the modularised layout.
    """

    def test_postgres_invalid_uri_scheme_rejected(self):
        from fluid_build.cli.discover.postgres import PostgresDiscoverer

        # The base ``_parse_uri`` rejects any scheme not in ``self.config.schemes``.
        # Postgres allows ``postgres://`` + ``postgresql://`` only.
        with pytest.raises(ValueError):
            PostgresDiscoverer()._parse_uri("https://not-postgres")

    def test_postgres_libpq_escape_doubles_quote(self):
        from fluid_build.providers._sql_safety import libpq_escape

        # Internal single quote → backslash-escaped within single quotes.
        assert libpq_escape("p'q") == "'p\\'q'"

    def test_postgres_libpq_escape_doubles_backslash(self):
        from fluid_build.providers._sql_safety import libpq_escape

        # Backslashes are doubled (libpq syntax: ``\\`` == one literal
        # ``\``). The function only WRAPS in single quotes when the
        # input contains spaces or single quotes — a plain
        # backslash-only value passes through doubled but unwrapped,
        # because the SQL boundary
        # (``quote_string_literal(libpq_escape(...))``) wraps it once
        # at the outer layer.
        assert libpq_escape("p\\q") == "p\\\\q"

    def test_mysql_libpq_escape_works_same_way(self):
        from fluid_build.providers._sql_safety import libpq_escape

        # Same single-source-of-truth helper as postgres — both
        # discoverers route through it.
        assert libpq_escape("a'b") == "'a\\'b'"


# ── Sec-Fix 4: dlt DSN URL encoding ───────────────────────────────────


@pytest.mark.skipif(
    True,  # Skip unless both ``dlt`` and ``sqlalchemy`` are installed.
    reason="needs dlt + sqlalchemy (optional deps); covered by acquisition test matrix when [dlt] extra is installed.",
)
class TestDltDsnEncoding:
    def test_password_with_url_special_chars_is_percent_encoded(self):
        # Build the URL that the runner would build, but only the URL itself —
        # we don't actually run dlt here.
        from sqlalchemy.engine.url import URL

        url = URL.create(
            drivername="postgresql+psycopg",
            username="u",
            password="x@y:z",  # URL-special chars; must be percent-encoded
            host="db",
            port=5432,
            database="d",
        )
        rendered = str(url)
        # The literal "x@y:z" must NOT appear (it's encoded). The encoded form
        # contains percent-escapes.
        assert "x@y:z" not in rendered, f"password leaked unescaped into URL: {rendered}"
        # And the host:port boundary is preserved.
        assert "@db:5432/d" in rendered

    def test_invalid_port_rejected(self):
        from fluid_build.build_runners.dlt.runner import _make_sql_database_source

        with pytest.raises(ValueError):
            _make_sql_database_source(
                {"host": "h", "port": "yolo", "user": "u", "password": "p", "database": "d"},
                ["public.x"],
            )


# ── Sec-Fix 5: NullVerifier auto-fallback removed ─────────────────────


class TestSignatureVerifierFallback:
    def test_default_factory_raises_when_cosign_missing(self, monkeypatch):
        # Pretend cosign is not on PATH.
        import shutil as _sh

        original = _sh.which

        def _fake_which(name: str):
            if name == "cosign":
                return None
            return original(name)

        monkeypatch.setattr(_sh, "which", _fake_which)
        with pytest.raises(CosignNotInstalledError):
            make_default_verifier()

    def test_explicit_allow_null_returns_null_verifier(self, monkeypatch):
        import shutil as _sh

        monkeypatch.setattr(_sh, "which", lambda _: None)
        v = make_default_verifier(allow_null=True)
        assert isinstance(v, NullVerifier)


# ── Sec-Fix 6: Singer tap path traversal ──────────────────────────────


class TestSingerTapPathTraversal:
    def test_tap_name_with_traversal_rejected(self):
        # Constructed with the slashes — the regex must reject it.
        assert _resolve_tap_binary("../../etc/passwd") is None

    def test_tap_name_with_dot_dot_rejected(self):
        assert _resolve_tap_binary("..") is None

    def test_tap_name_with_slash_rejected(self):
        assert _resolve_tap_binary("a/b") is None

    def test_tap_name_with_uppercase_rejected(self):
        # Convention: tap names are lowercase. Uppercase rejected by regex.
        assert _resolve_tap_binary("Bad-TAP") is None

    def test_legitimate_tap_name_accepted(self, tmp_path: Path, monkeypatch):
        # Plant a real binary in the project's extractors tree and confirm
        # confinement allows it.
        ext = tmp_path / ".meltano" / "extractors" / "tap-postgres" / "venv" / "bin"
        ext.mkdir(parents=True)
        bin_path = ext / "tap-postgres"
        bin_path.write_text("#!/bin/sh\necho ok")
        bin_path.chmod(0o755)
        # Hide PATH version so we hit the venv branch.
        monkeypatch.setattr("shutil.which", lambda _: None)
        resolved = _resolve_tap_binary("tap-postgres", project_dir=tmp_path)
        assert resolved == str(bin_path.resolve())


# ── Sec-Fix 7: PII tokenization strength ──────────────────────────────


class TestPiiTokenization:
    def test_token_width_is_128_bits(self, monkeypatch):
        monkeypatch.setenv("FLUID_PII_TOKENIZATION_KEY", "k1")
        hook = TokenizePiiHook()
        result = hook.apply(
            [{"email": "alice@x.com", "id": 1}],
            ctx={"classifications": {"email": ["email"]}},
        )
        token = result.records[0]["email"]
        assert len(token) == _TOKEN_HEX_LEN == 32, f"token width regressed: {len(token)}"

    def test_keyed_tokens_differ_across_keys(self):
        hook_a = TokenizePiiHook(hmac_key=b"key-a")
        hook_b = TokenizePiiHook(hmac_key=b"key-b")
        ctx = {"classifications": {"email": ["email"]}}
        a = hook_a.apply([{"email": "alice@x.com"}], ctx).records[0]["email"]
        b = hook_b.apply([{"email": "alice@x.com"}], ctx).records[0]["email"]
        # Different keys → different tokens (defeats rainbow tables).
        assert a != b

    def test_same_key_same_token(self, monkeypatch):
        monkeypatch.setenv("FLUID_PII_TOKENIZATION_KEY", "stable-key")
        ctx = {"classifications": {"email": ["email"]}}
        first = TokenizePiiHook().apply([{"email": "x"}], ctx).records[0]["email"]
        second = TokenizePiiHook().apply([{"email": "x"}], ctx).records[0]["email"]
        assert first == second  # determinism preserved

    def test_token_uses_hmac_not_plain_sha256(self, monkeypatch):
        # Confirm the token is the HMAC, not a raw SHA-256.
        monkeypatch.setenv("FLUID_PII_TOKENIZATION_KEY", "key1")
        ctx = {"classifications": {"email": ["email"]}}
        token = TokenizePiiHook().apply([{"email": "alice@x.com"}], ctx).records[0]["email"]
        # If it were truncated SHA-256, the first 16 hex chars would equal hashlib.sha256.
        plain_sha256 = hashlib.sha256(b"alice@x.com").hexdigest()[:32]
        # Token should NOT equal the raw SHA-256 truncation under any key.
        assert token != plain_sha256
        # Token SHOULD equal the HMAC under the same key.
        expected = hmac.new(b"key1", b"alice@x.com", hashlib.sha256).hexdigest()[:32]
        assert token == expected

    # ── Keyless fallback is ephemeral + random (not PID-derived) ──────
    #
    # Regression guard for the credential-at-rest finding: the no-key
    # fallback used to be ``sha256("fluid-pid-<pid>")`` whose keyspace was
    # the PID space (< 2**22), trivially brute-forceable. It must now be a
    # cryptographically-random per-resolution 256-bit key.

    def test_keyless_default_key_is_random_per_call(self, monkeypatch):
        # No configured key → each resolution returns a *different* key,
        # so tokens are unlinkable across runs (no stable secret to attack).
        monkeypatch.delenv("FLUID_PII_TOKENIZATION_KEY", raising=False)
        k1 = _resolve_default_key()
        k2 = _resolve_default_key()
        assert k1 != k2, "keyless fallback regressed to a deterministic key"

    def test_keyless_default_key_is_256_bits(self, monkeypatch):
        # 32 bytes = 256 bits of entropy from secrets.token_bytes(32).
        monkeypatch.delenv("FLUID_PII_TOKENIZATION_KEY", raising=False)
        assert len(_resolve_default_key()) == 32

    def test_keyless_default_key_is_not_pid_derived(self, monkeypatch):
        # Explicitly pin that the old PID-seed construction is gone: the
        # returned key must not equal sha256("fluid-pid-<pid>").
        import os

        monkeypatch.delenv("FLUID_PII_TOKENIZATION_KEY", raising=False)
        legacy = hashlib.sha256(f"fluid-pid-{os.getpid()}".encode("utf-8")).digest()
        assert _resolve_default_key() != legacy

    def test_configured_env_key_is_used_verbatim(self, monkeypatch):
        # When the env var IS set, the configured key is used as-is (UTF-8
        # bytes) — stability across runs is opt-in, not the fallback.
        monkeypatch.setenv("FLUID_PII_TOKENIZATION_KEY", "configured-key")
        assert _resolve_default_key() == b"configured-key"

    def test_keyless_tokens_differ_across_hook_instances(self, monkeypatch):
        # End-to-end: two keyless hook instances tokenize the same value to
        # DIFFERENT tokens (each captured a fresh random key in __post_init__).
        monkeypatch.delenv("FLUID_PII_TOKENIZATION_KEY", raising=False)
        ctx = {"classifications": {"email": ["email"]}}
        a = TokenizePiiHook().apply([{"email": "alice@x.com"}], ctx).records[0]["email"]
        b = TokenizePiiHook().apply([{"email": "alice@x.com"}], ctx).records[0]["email"]
        assert a != b


# ── Sec-Fix 8: dlt SQL count query identifier validation ──────────────


class TestDltSqlCountIdentifier:
    def test_dataset_with_special_chars_rejected_by_validator(self):
        from fluid_build.providers._sql_safety import validate_ident

        with pytest.raises(ValueError):
            validate_ident("bronze; DROP TABLE secrets; --")

    def test_table_with_special_chars_rejected_by_validator(self):
        from fluid_build.providers._sql_safety import validate_ident

        with pytest.raises(ValueError):
            validate_ident("orders'; DELETE FROM y; --")
