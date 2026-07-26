# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Security regressions found while exercising forge-cli against live Snowflake.

Four independent defects, each reproduced end-to-end before the fix:

1. ``quote_string_literal`` doubled the embedded quote but not the preceding
   backslash. Snowflake (and MySQL / BigQuery / Redshift) read ``\\'`` as an
   *escaped* quote, so the doubled partner terminated the literal and the rest
   of the contract-supplied value was parsed as SQL. Proven on live Snowflake:
   a ``valid_values`` entry of ``X\\') AND 1=0 --`` made ``fluid test`` execute
   ``... NOT IN ('active','inactive','X\\'') AND 1=0 --')`` and report the data
   quality gate as PASSED against a table that violated it.
2. ``--readable-paths`` never reached the driver: ``build_driver`` had no such
   parameter, so ``DuckDBDriver``'s path/attach/dbFile confinement ran with an
   empty allowlist and the ``sample`` tool returned files outside the sandbox.
3. The redaction assignment regex stopped the value at the first ``;`` ``,``
   ``}`` ``]``, leaking the tail of any secret containing one.
4. A credential-shaped ``{{ env.X }}`` placeholder was resolved into the
   contract body on the IaC path, which is serialized into ``main.tf.json``,
   the OpenTofu state, and the Snowflake table ``COMMENT``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluid_build.providers._sql_safety import (
    dialect_uses_backslash_escapes,
    quote_ansi_string_literal,
    quote_string_literal,
)

pytestmark = pytest.mark.unit


# ── 1. String-literal break-out via a trailing backslash ──────────────────


class TestQuoteStringLiteralBackslash:
    """``quote_string_literal`` must not be escapable on a backslash dialect."""

    BREAKOUT = "X\\') AND 1=0 --pwned"

    def test_backslash_is_escaped_by_default(self) -> None:
        # Default (Snowflake / MySQL / BigQuery / Redshift): the backslash is
        # doubled, so ``\\`` is one literal backslash and the following ``''``
        # is an escaped quote — nothing terminates the literal early.
        assert quote_string_literal(self.BREAKOUT) == "'X\\\\'') AND 1=0 --pwned'"

    def test_ansi_variant_leaves_backslash_alone(self) -> None:
        # DuckDB / PostgreSQL / Trino: ``\\`` is an ordinary character, so
        # doubling it would corrupt the value and buys no safety.
        assert quote_ansi_string_literal(self.BREAKOUT) == "'X\\'') AND 1=0 --pwned'"

    @pytest.mark.parametrize(
        "value",
        [
            "o'brien",
            "C:\\path\\to\\file.csv",
            "trailing backslash \\",
            "\\'; DROP TABLE T; --",
            "mixed \\\\' quote",
        ],
    )
    def test_snowflake_literal_survives_a_round_trip(self, value: str) -> None:
        """Decode the emitted literal with Snowflake's own escape rules and
        get the original value back — i.e. nothing escapes the quotes."""
        emitted = quote_string_literal(value)
        assert emitted.startswith("'") and emitted.endswith("'")
        body = emitted[1:-1]
        decoded = []
        i = 0
        while i < len(body):
            char = body[i]
            if char == "\\" and i + 1 < len(body):
                decoded.append(body[i + 1])
                i += 2
                continue
            if char == "'":
                # A lone quote here would mean the literal ended early — the
                # break-out this test exists to prevent.
                assert body[i + 1 : i + 2] == "'", f"literal terminated early: {emitted!r}"
                decoded.append("'")
                i += 2
                continue
            decoded.append(char)
            i += 1
        assert "".join(decoded) == value

    @pytest.mark.parametrize(
        "value",
        ["o'brien", "C:\\path\\to\\file.csv", "trailing backslash \\", "\\'; DROP TABLE T; --"],
    )
    def test_duckdb_round_trip_is_byte_exact(self, value: str) -> None:
        """The ANSI variant must hand DuckDB back the exact input string."""
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect()
        try:
            got = con.execute("SELECT " + quote_ansi_string_literal(value)).fetchall()[0][0]
        finally:
            con.close()
        assert got == value

    @pytest.mark.parametrize(
        "dialect,expected",
        [
            ("snowflake", True),
            ("bigquery", True),
            ("mysql", True),
            (None, True),
            ("some-new-engine", True),
            ("ansi", False),
            ("duckdb", False),
            ("postgres", False),
            ("Trino", False),
        ],
    )
    def test_dialect_hint_fails_closed(self, dialect, expected: bool) -> None:
        assert dialect_uses_backslash_escapes(dialect) is expected


class TestQualityEngineValidityLiteral:
    """The reachable Snowflake path: ``fluid test``'s ``valid_values`` rule."""

    def _sql_for(self, dialect: str, values: list[str]) -> str:
        from fluid_build.providers.quality_engine import execute_quality_checks

        captured: list[str] = []

        def _execute(sql: str, *_args, **_kwargs):
            captured.append(sql)
            return [(0,)]

        execute_quality_checks(
            rules=[
                {
                    "id": "valid_status",
                    "type": "valid_values",
                    "selector": "STATUS",
                    "validValues": values,
                }
            ],
            table_ref='"DB"."SC"."T"',
            execute_fn=_execute,
            dialect=dialect,
        )
        assert captured
        return captured[0]

    def test_snowflake_validity_literal_cannot_break_out(self) -> None:
        sql = self._sql_for("snowflake", ["active", "X\\') AND 1=0 --pwned"])
        # The injected tail stays INSIDE the literal: the backslash is doubled,
        # so the following ``''`` is an escaped quote, not a terminator.
        assert "'X\\\\'') AND 1=0 --pwned'" in sql
        # The un-escaped (break-out) form must be gone: a SINGLE backslash
        # before the doubled quote is what Snowflake reads as an escaped quote.
        assert "'X\\'') AND 1=0" not in sql
        # Everything the attacker supplied is still inside the IN list, which
        # the template's own ``)`` closes at the very end of the statement.
        assert sql.endswith("--pwned')")

    def test_ansi_validity_literal_keeps_the_value_intact(self) -> None:
        sql = self._sql_for("ansi", ["a\\b"])
        assert "'a\\b'" in sql


# ── 2. --readable-paths must actually reach the driver ────────────────────


class TestBuildDriverForwardsReadablePaths:
    def _expose(self, path: Path) -> dict:
        return {
            "exposeId": "probe",
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": str(path), "table": "probe"},
            },
            "contract": {"schema": [{"name": "k", "type": "STRING"}]},
        }

    def test_path_outside_the_allowlist_is_refused(self, tmp_path: Path) -> None:
        from fluid_build.output_ports.mcp.drivers import UnsupportedBindingError, build_driver

        sandbox = tmp_path / "sandbox"
        outside = tmp_path / "outside"
        sandbox.mkdir()
        outside.mkdir()
        secret = outside / "creds.csv"
        secret.write_text("k,v\nSNOWFLAKE_PASSWORD,TOP_SECRET\n", encoding="utf-8")

        with pytest.raises(UnsupportedBindingError, match="readable-paths"):
            build_driver(
                expose=self._expose(secret),
                contract={},
                readable_paths=(sandbox,),
            )

    def test_path_inside_the_allowlist_is_allowed(self, tmp_path: Path) -> None:
        from fluid_build.output_ports.mcp.drivers import build_driver

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        data = sandbox / "rows.csv"
        data.write_text("k,v\nINSIDE,ok\n", encoding="utf-8")

        driver = build_driver(
            expose=self._expose(data),
            contract={},
            readable_paths=(sandbox,),
        )
        assert driver.readable_paths == (sandbox,)

    def test_symlink_out_of_the_sandbox_is_refused(self, tmp_path: Path) -> None:
        from fluid_build.output_ports.mcp.drivers import UnsupportedBindingError, build_driver

        sandbox = tmp_path / "sandbox"
        outside = tmp_path / "outside"
        sandbox.mkdir()
        outside.mkdir()
        secret = outside / "creds.csv"
        secret.write_text("k,v\nSNOWFLAKE_PASSWORD,TOP_SECRET\n", encoding="utf-8")
        link = sandbox / "link.csv"
        link.symlink_to(secret)

        with pytest.raises(UnsupportedBindingError, match="readable-paths"):
            build_driver(expose=self._expose(link), contract={}, readable_paths=(sandbox,))

    def test_session_state_passes_the_policy_allowlist(self, tmp_path: Path) -> None:
        """The wiring bug itself: ``SessionState.get_driver`` dropped the
        policy's allowlist, so the confinement code never ran."""
        from fluid_build.output_ports.mcp.drivers import UnsupportedBindingError
        from fluid_build.output_ports.mcp.policy import OutputPortPolicy
        from fluid_build.output_ports.mcp.server import SessionState

        sandbox = tmp_path / "sandbox"
        outside = tmp_path / "outside"
        sandbox.mkdir()
        outside.mkdir()
        secret = outside / "creds.csv"
        secret.write_text("k,v\nSNOWFLAKE_PASSWORD,TOP_SECRET\n", encoding="utf-8")

        expose = self._expose(secret)
        state = SessionState(
            contract={"exposes": [expose]},
            expose=expose,
            policy=OutputPortPolicy.from_contract_and_flags(
                expose=expose, readable_paths=(sandbox.resolve(),)
            ),
            logger=logging.getLogger("test.output_port.readable_paths"),
        )
        with pytest.raises(UnsupportedBindingError, match="readable-paths"):
            state.get_driver()

    def test_driver_without_the_kwarg_warns_instead_of_crashing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An out-of-tree driver predating ``readable_paths`` must not blow up
        — but the operator must be told the allowlist is not enforced."""
        from fluid_build.output_ports.mcp.drivers import (
            _DRIVER_REGISTRY,
            EngineDriver,
            build_driver,
            register_driver,
        )

        class _LegacyDriver(EngineDriver):
            name = "legacy"

            def __init__(self, *, expose, contract, logger=None, connection_options=None):
                super().__init__(
                    expose=expose,
                    contract=contract,
                    logger=logger,
                    connection_options=connection_options,
                )

            def descriptor(self):  # pragma: no cover - not exercised
                raise NotImplementedError

            def execute(self, *args, **kwargs):  # pragma: no cover - not exercised
                raise NotImplementedError

            def health_check(self):  # pragma: no cover - not exercised
                raise NotImplementedError

        key = ("legacy_platform", "legacy_format")
        register_driver(key, _LegacyDriver)
        try:
            with caplog.at_level(logging.WARNING, logger="fluid.output_port.mcp.drivers"):
                driver = build_driver(
                    expose={"binding": {"platform": key[0], "format": key[1], "location": {}}},
                    contract={},
                    readable_paths=(tmp_path,),
                )
            assert isinstance(driver, _LegacyDriver)
            assert "does not accept readable_paths" in caplog.text
        finally:
            _DRIVER_REGISTRY.pop(key, None)


# ── 3. Redaction must not truncate at a delimiter inside the secret ───────


class TestRedactionValueTermination:
    @pytest.mark.parametrize(
        "secret",
        [
            "Pa55;w0rd-THE-REST-MUST-NOT-LEAK",
            "Pa55,w0rd-THE-REST-MUST-NOT-LEAK",
            "Pa55}w0rd-THE-REST-MUST-NOT-LEAK",
            "Pa55]w0rd-THE-REST-MUST-NOT-LEAK",
        ],
    )
    def test_both_layers_mask_the_whole_token(self, secret: str) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text
        from fluid_build.providers.snowflake.util.logging import redact_string

        line = f"password={secret}"
        for redacted in (redact_secret_text(line), redact_string(line)):
            assert "MUST-NOT-LEAK" not in redacted, redacted
            assert redacted.startswith("password=")

    def test_surrounding_log_text_still_survives(self) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text("connect failed for host=db1 password=hunter2 retrying")
        assert "hunter2" not in out
        assert "host=db1" in out
        assert out.endswith(" retrying")

    def test_url_query_separator_still_bounds_the_value(self) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text("https://h/x?user=bob&password=hunter2&region=eu")
        assert "hunter2" not in out
        assert "&region=eu" in out

    def test_quoted_value_masks_everything_between_the_quotes(self) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text('{"password": "hun;ter2", "user": "bob"}')
        assert "hun;ter2" not in out
        assert '"user": "bob"' in out


# ── 4. Credential-shaped env placeholders must not resolve into artifacts ──


class TestIacEnvTemplateCredentialGuard:
    def test_sensitive_placeholder_is_left_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fluid_build.cli._common import resolve_env_templates_in_contract

        monkeypatch.setenv("MY_TEST_PASSWORD", "SENTINEL_PW_VALUE")
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "FLUID_TEST")
        resolved = resolve_env_templates_in_contract(
            {
                "description": "uses password {{ env.MY_TEST_PASSWORD }} for the job",
                "exposes": [
                    {"binding": {"location": {"database": "{{ env.SNOWFLAKE_DATABASE }}"}}}
                ],
            }
        )
        assert "SENTINEL_PW_VALUE" not in str(resolved)
        assert "{{ env.MY_TEST_PASSWORD }}" in resolved["description"]
        # Non-sensitive placeholders still resolve — the emitter needs them.
        assert resolved["exposes"][0]["binding"]["location"]["database"] == "FLUID_TEST"

    def test_mixed_string_resolves_only_the_safe_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fluid_build.cli._common import resolve_env_templates_in_contract

        monkeypatch.setenv("MY_TEST_PASSWORD", "SENTINEL_PW_VALUE")
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "FLUID_TEST")
        resolved = resolve_env_templates_in_contract(
            {"note": "{{ env.SNOWFLAKE_DATABASE }} / {{ env.MY_TEST_PASSWORD }}"}
        )
        assert resolved["note"] == "FLUID_TEST / {{ env.MY_TEST_PASSWORD }}"

    def test_published_table_comment_is_redacted(self) -> None:
        """The COMMENT embeds the whole contract YAML and is world-readable to
        anyone with schema access, so it is a published sink."""
        from fluid_build.iac.providers.snowflake import _build_horizon_table_comment

        comment = _build_horizon_table_comment(
            {
                "id": "silver.community.demo",
                "description": "demo",
                "builds": [{"id": "seed", "properties": {"password": "INLINE_SECRET_98765"}}],
            }
        )
        assert "INLINE_SECRET_98765" not in comment
        assert "REDACTED" in comment
