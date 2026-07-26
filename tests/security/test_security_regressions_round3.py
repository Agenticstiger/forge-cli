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
   ``}`` ``]``, leaking the tail of any secret containing one. Fixed by adding
   an exact-value layer (the credential's literal is already held at redaction
   time), NOT by widening the regex — widening it regressed inputs the shipped
   terminator set handles correctly.
4. A credential-shaped ``{{ env.X }}`` placeholder was resolved into the
   contract body on the IaC path, which is serialized into ``main.tf.json``,
   the OpenTofu state, and the Snowflake table ``COMMENT``.
"""

from __future__ import annotations

import logging
import re
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
#
# The pattern layer CANNOT fix this on its own. It must decide where an
# unquoted value ends, and every candidate terminator (``;`` ``,`` ``}`` ``]``
# whitespace ``"`` ``&``) is a character a real password may contain — so any
# choice tail-leaks some secret. A first attempt swapped ``;,}]`` for ``"&``:
# it fixed four inputs and broke others the shipped set handles, turning
# ``jdbc:snowflake://h/?password="p@ss"&user=x`` from fully masked into
# emitting the password verbatim. Net: strictly worse.
#
# The fix is the exact-value layer (``mask_known_secrets``, borrowed from
# ``misprint``): at redaction time the credential's literal value is already
# held, so it is matched literally — delimiter-agnostic, cannot truncate. The
# pattern layer is left EXACTLY as shipped and is only the net for values we
# do not hold.
#
# Both halves are proven below: the defect is gone for a held secret
# (TestRedactionExactValueLayer), AND the pattern layer's shipped behaviour is
# byte-identical to the released build (TestPatternLayerNotRegressed).

# One canary per delimiter that has ever been proposed as a terminator, plus
# whitespace, which no assignment pattern can ever terminate on without
# destroying the surrounding log text.
_DELIMITER_SECRETS = [
    ("semicolon", "Pa55head;w0rdTAILMUSTNOTLEAK"),
    ("comma", "Pa55head,w0rdTAILMUSTNOTLEAK"),
    ("brace", "Pa55head}w0rdTAILMUSTNOTLEAK"),
    ("bracket", "Pa55head]w0rdTAILMUSTNOTLEAK"),
    ("space", "Pa55head w0rdTAILMUSTNOTLEAK"),
    ("double-quote", 'Pa55head"w0rdTAILMUSTNOTLEAK'),
    ("single-quote", "Pa55head'w0rdTAILMUSTNOTLEAK"),
    ("ampersand", "Pa55head&w0rdTAILMUSTNOTLEAK"),
    ("paren", "Pa55head)w0rdTAILMUSTNOTLEAK"),
    ("every-delimiter-at-once", "Pa55;head,brace}brk]dq\"amp&sq'TAILMUSTNOTLEAK"),
]


@pytest.fixture()
def clean_registry():
    """Isolate the process-wide exact-match registry around each test."""
    from fluid_build.observability.secret_redactor import forget_known_secrets

    forget_known_secrets()
    yield
    forget_known_secrets()


def _through_a_real_filter(line: str, name: str) -> str:
    """Render ``line`` through a real StreamHandler + SecretRedactingFilter.

    Function-level redaction is not enough evidence: the reported defect was
    observed end-to-end, so the end-to-end path is what gets asserted.
    """
    import io

    from fluid_build.observability.secret_redactor import install_secret_redacting_filter

    logger = logging.getLogger(f"test.redaction.{name}")
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for filt in list(logger.filters):
        logger.removeFilter(filt)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    install_secret_redacting_filter(logger)
    logger.info(line)
    handler.flush()
    return stream.getvalue().rstrip("\n")


class TestRedactionExactValueLayer:
    """A credential we HOLD is masked whole, whatever it contains."""

    @pytest.mark.parametrize(("label", "secret"), _DELIMITER_SECRETS)
    def test_every_delimiter_is_masked_in_all_three_paths(
        self, label: str, secret: str, clean_registry: None
    ) -> None:
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )
        from fluid_build.providers.snowflake.util.logging import redact_string

        assert register_secret(secret) is True
        line = f"password={secret}"
        for path, out in (
            ("global", redact_secret_text(line)),
            ("snowflake-twin", redact_string(line)),
            ("logging-filter", _through_a_real_filter(line, label)),
        ):
            assert "TAILMUSTNOTLEAK" not in out, f"{path}: {out!r}"
            assert "Pa55" not in out, f"{path}: {out!r}"
            assert out.startswith("password="), f"{path}: {out!r}"

    @pytest.mark.parametrize(("label", "secret"), _DELIMITER_SECRETS)
    def test_secret_is_masked_without_any_assignment_syntax(
        self, label: str, secret: str, clean_registry: None
    ) -> None:
        """Exact-value matching does not need a ``key=`` prefix at all — a
        credential echoed bare by a driver error is masked too, which no
        assignment pattern can do."""
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )
        from fluid_build.providers.snowflake.util.logging import redact_string

        register_secret(secret)
        line = f"250001 (08001): Incorrect username or password was specified: {secret} <- raw"
        for out in (redact_secret_text(line), redact_string(line)):
            assert "TAILMUSTNOTLEAK" not in out, out
            assert out.endswith("<- raw"), out

    def test_registration_is_delimiter_agnostic_in_the_prover_inputs(
        self, clean_registry: None
    ) -> None:
        """The three inputs the prover measured, with the secret held."""
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )
        from fluid_build.providers.snowflake.util.logging import redact_string

        register_secret("p@ssPROVER")
        for line in (
            'jdbc:snowflake://h/?password="p@ssPROVER"&user=x',
            '<conn password="p@ssPROVER"/>',
        ):
            for out in (redact_secret_text(line), redact_string(line)):
                assert "p@ssPROVER" not in out, out

    def test_environment_harvest_masks_a_delimiter_bearing_password(
        self, clean_registry: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real-world case: the account's own SNOWFLAKE_PASSWORD contains a
        ``;``. Harvesting the env is what the CLI does at logging setup."""
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secrets_from_environ,
        )

        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "Pa55head;w0rdTAILMUSTNOTLEAK")
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ZSCXYPE-CU29385")
        assert register_secrets_from_environ() >= 1
        out = redact_secret_text("connect failed password=Pa55head;w0rdTAILMUSTNOTLEAK retrying")
        assert "TAILMUSTNOTLEAK" not in out
        assert out.endswith(" retrying")
        # A non-credential env var is NOT registered — the account id must
        # still be readable in logs.
        assert "ZSCXYPE-CU29385" in redact_secret_text("account=ZSCXYPE-CU29385 ok")

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            # Every one of these was collected by an earlier substring-matching
            # version of the harvest, measured on a real shell. The first one
            # made a live ``fluid apply`` log ``"provider": "***REDACTED***"``.
            ("SNOWFLAKE_AUTHENTICATOR", "snowflake"),
            ("SSH_AUTH_SOCK", "/private/tmp/com.apple.launchd.XYZ/Listeners"),
            ("CLAUDE_CODE_OAUTH_SCOPES", "user:inference user:profile"),
            ("SNOWFLAKE_PRIVATE_KEY_PATH", "/home/me/keys/sf_rsa.p8"),
            ("GOOGLE_APPLICATION_CREDENTIALS", "/home/me/gcp/sa.json"),
            ("SNOWFLAKE_CONNECTION_URL", "snowflake://acct.snowflakecomputing.com"),
        ],
    )
    def test_non_secret_env_vars_are_not_registered(
        self, name: str, value: str, clean_registry: None
    ) -> None:
        """A name that merely CONTAINS a credential word is not a credential.
        Registering these masks ordinary vocabulary — a provider name, a socket
        path, a scope list — everywhere it appears, which destroys the logs and
        protects nothing. Env harvesting uses a suffix allowlist for this
        reason."""
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secrets_from_environ,
        )

        assert register_secrets_from_environ({name: value}) == 0
        assert value in redact_secret_text(f"using {value} now")

    @pytest.mark.parametrize(
        "name",
        [
            "SNOWFLAKE_PASSWORD",
            "SF_PASSWORD",
            "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
            "SNOWFLAKE_OAUTH_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "MY_CLIENT_SECRET",
        ],
    )
    def test_real_credential_env_vars_are_registered(self, name: str, clean_registry: None) -> None:
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secrets_from_environ,
        )

        assert register_secrets_from_environ({name: "Pa55head;w0rdTAILMUSTNOTLEAK"}) == 1
        assert "TAILMUSTNOTLEAK" not in redact_secret_text("raw echo Pa55head;w0rdTAILMUSTNOTLEAK")

    def test_env_harvest_is_quiet_on_an_ordinary_shell(
        self, clean_registry: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A boolean-valued env var whose name merely contains 'OAUTH' must not
        produce a warning on every CLI invocation — it did before the harvest
        switched to a suffix allowlist."""
        from fluid_build.observability.secret_redactor import register_secrets_from_environ

        with caplog.at_level(logging.WARNING, logger="fluid.observability.redaction"):
            register_secrets_from_environ(
                {
                    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH": "1",
                    "USE_LOCAL_OAUTH": "",
                    "SNOWFLAKE_AUTHENTICATOR": "snowflake",
                    "PATH": "/usr/bin",
                }
            )
        assert caplog.text == ""

    def test_unresolved_env_placeholder_is_not_registered(self, clean_registry: None) -> None:
        """The IaC guard deliberately leaves ``{{ env.X }}`` literal so the
        operator can see which variable was refused; masking it would hide
        that signal, and a placeholder is the ABSENCE of a credential."""
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )

        assert register_secret("{{ env.MY_TEST_PASSWORD }}") is False
        assert "{{ env.MY_TEST_PASSWORD }}" in redact_secret_text(
            "left literal: {{ env.MY_TEST_PASSWORD }}"
        )

    def test_too_short_a_secret_is_refused_loudly(
        self, clean_registry: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Refusing must never be silent: a 3-char secret would match inside
        ordinary words, so it is not registered — and the operator is told so
        it is not mistaken for coverage."""
        from fluid_build.observability.secret_redactor import register_secret

        with caplog.at_level(logging.WARNING, logger="fluid.observability.redaction"):
            assert register_secret("abc") is False
        assert "exact-match" in caplog.text
        assert "pattern layer" in caplog.text
        assert "abc" not in caplog.text.replace("exact-match", "")

    @pytest.mark.parametrize(
        ("weak_secret", "line", "expected"),
        [
            # THE measured one: SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=phrase makes
            # 'phrase' a registered literal, and 'passphrase' contains it.
            ("phrase", "passphrase=letmein", "passphrase=[REDACTED]"),
            ("secret", "client_secret=topsecret123", "client_secret=[REDACTED]"),
            ("bearer", "password=hunter2", "password=[REDACTED]"),
            ("password", "password=hunter2", "password=[REDACTED]"),
            ("private", "private_key=abc123def456ghi", "private_key=[REDACTED]"),
        ],
    )
    def test_a_secret_that_is_a_key_name_fragment_cannot_disable_the_pattern_layer(
        self, weak_secret: str, line: str, expected: str, clean_registry: None
    ) -> None:
        """Exact-value masking runs BEFORE the pattern layer, so a registered
        literal that is a substring of a key name would rewrite the key and
        stop the pattern from matching — leaking a DIFFERENT secret on the same
        line. Measured before the guard: with 'phrase' registered,
        ``passphrase=letmein`` came out as ``pass[REDACTED]=letmein``.

        Such values are refused (loudly) rather than registered.
        """
        from fluid_build.observability.secret_redactor import register_secret
        from fluid_build.providers.snowflake.util.logging import redact_string

        assert register_secret(weak_secret) is False
        assert redact_string(line) == expected

    def test_full_registry_refuses_loudly_rather_than_silently(
        self, clean_registry: None, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        """The cap is a fail-open boundary: past it, a credential falls back to
        the pattern layer. That must never be silent."""
        from fluid_build.observability import secret_redactor as sr

        monkeypatch.setattr(sr, "_MAX_REGISTERED_SECRETS", 2)
        assert sr.register_secret("first_secret_value") is True
        assert sr.register_secret("second_secret_value") is True
        with caplog.at_level(logging.WARNING, logger="fluid.observability.redaction"):
            assert sr.register_secret("third_secret_value") is False
        assert "registry is full" in caplog.text
        assert "fix the caller" in caplog.text
        assert sr.known_secret_count() == 2

    def test_key_fragment_refusal_is_loud(
        self, clean_registry: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fluid_build.observability.secret_redactor import register_secret

        with caplog.at_level(logging.WARNING, logger="fluid.observability.redaction"):
            assert register_secret("phrase") is False
        assert "pattern layer" in caplog.text
        assert "Rotate" in caplog.text

    def test_longest_secret_wins_when_one_contains_another(self, clean_registry: None) -> None:
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )

        register_secret("hunter2short")
        register_secret("hunter2shortLONGERTAIL")
        out = redact_secret_text("password=hunter2shortLONGERTAIL")
        assert "LONGERTAIL" not in out

    def test_ordinary_log_lines_are_untouched(self, clean_registry: None) -> None:
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )
        from fluid_build.providers.snowflake.util.logging import redact_string

        register_secret("Pa55head;w0rdTAILMUSTNOTLEAK")
        for benign in (
            "starting apply for contract silver.demo",
            "table FLUID_TEST.FIN_SEC.T created in 1.2s",
            "plan: +1 ~2 -0",
        ):
            assert redact_secret_text(benign) == benign
            assert redact_string(benign) == benign


class TestPatternLayerNotRegressed:
    """The shipped pattern layer must behave EXACTLY as released.

    These are the inputs an earlier rework broke. They are asserted with the
    registry EMPTY, so only the pattern layer is under test.
    """

    # ``layers`` names exactly which redaction paths the RELEASED build masks
    # this input in, measured against the released tree rather than assumed.
    # ``&`` is a terminator in the Snowflake twin's shipped value class but not
    # in the global one, so the two differ on ``delim-amp`` — that asymmetry is
    # released behaviour, and pinning it honestly is the point of this table.
    @pytest.mark.parametrize(
        ("line", "must_not_contain", "layers"),
        [
            # The prover's three measured regressions — masked in ALL layers by
            # the released build, and emitted verbatim by the widened one.
            ('jdbc:snowflake://h/?password="p@ssPROVER"&user=x', "p@ssPROVER", "GSF"),
            ('<conn password="p@ssPROVER"/>', "p@ssPROVER", "GSF"),
            ('password=Pa55"wordTAILMUSTNOTLEAK', "wordTAILMUSTNOTLEAK", "GSF"),
            ('{"password": "hun;ter2xy", "user": "bob"}', "hun;ter2xy", "GSF"),
            # Neighbouring shape the same widening broke in the global layer.
            # The twin truncates here in the released build too (``&`` is one
            # of its terminators), so it is NOT claimed — the exact-value layer
            # is what covers it, proven in TestRedactionExactValueLayer.
            ("password=Pa55head&w0rdTAILMUSTNOTLEAK", "w0rdTAILMUSTNOTLEAK", "GF"),
        ],
    )
    def test_pattern_layer_still_masks_what_it_always_masked(
        self, line: str, must_not_contain: str, layers: str, clean_registry: None
    ) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text
        from fluid_build.providers.snowflake.util.logging import redact_string

        checks = {
            "G": ("global", redact_secret_text),
            "S": ("snowflake-twin", redact_string),
            "F": ("logging-filter", lambda text: _through_a_real_filter(text, "noreg")),
        }
        for key in layers:
            name, fn = checks[key]
            assert must_not_contain not in fn(line), f"{name}: {fn(line)!r}"

    def test_jaas_value_is_still_masked_by_the_snowflake_twin(self, clean_registry: None) -> None:
        """The widening also broke this: the JAAS value regex ends at ``"``, and
        a value class that stopped at ``"`` matched zero characters first."""
        from fluid_build.providers.snowflake.util.logging import redact_string

        line = (
            'sasl.jaas.config="org.apache.kafka.common.security.plain.PlainLoginModule '
            'required username=\\"u\\" password=\\"topsecretpw\\";"'
        )
        assert "topsecretpw" not in redact_string(line)

    def test_surrounding_log_text_still_survives(self, clean_registry: None) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text("connect failed for host=db1 password=hunter2 retrying")
        assert "hunter2" not in out
        assert "host=db1" in out
        assert out.endswith(" retrying")

    def test_quoted_value_masks_everything_between_the_quotes(self, clean_registry: None) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text('{"password": "hun;ter2", "user": "bob"}')
        assert "hun;ter2" not in out
        assert '"user": "bob"' in out

    def test_pattern_layer_truncation_is_a_documented_limit_not_a_claim(
        self, clean_registry: None
    ) -> None:
        """Honesty guard. For a secret we do NOT hold, the pattern layer still
        truncates at ``;`` — this is the shipped behaviour, kept because every
        alternative terminator set leaks something else. The test exists so the
        limit stays visible and nobody mistakes the pattern layer for complete
        coverage: registering the value is what closes it (asserted directly
        below).
        """
        from fluid_build.observability.secret_redactor import (
            redact_secret_text,
            register_secret,
        )

        line = "password=Pa55head;w0rdTAILMUSTNOTLEAK"
        assert "TAILMUSTNOTLEAK" in redact_secret_text(line), (
            "pattern-layer behaviour changed — re-verify against the released "
            "build before updating this expectation"
        )
        register_secret("Pa55head;w0rdTAILMUSTNOTLEAK")
        assert "TAILMUSTNOTLEAK" not in redact_secret_text(line)


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

    @pytest.mark.parametrize(
        "secret",
        [s for _, s in _DELIMITER_SECRETS] + ['Pa55"word-LEAKS-TO-CATALOG'],
    )
    def test_published_comment_masks_a_delimiter_bearing_inline_password(self, secret: str) -> None:
        """The live leak the prover measured: a contract with
        ``builds[0].properties.password: 'Pa55"word-LEAKS-TO-CATALOG'`` applied
        to Snowflake and the table's COMMENT read
        ``password: ***REDACTED***"word-LEAKS-TO-CATALOG`` — the tail published
        to the catalog, readable by every role with schema access.

        The comment builder HOLDS the contract, so it masks these by exact
        value; no delimiter inside the password can truncate it.
        """
        from fluid_build.iac.providers.snowflake import _build_horizon_table_comment

        comment = _build_horizon_table_comment(
            {
                "id": "silver.community.demo",
                "description": "demo",
                "builds": [{"id": "seed", "properties": {"password": secret}}],
            }
        )
        for fragment in re.split(r"[;,}\]&\"'() ]+", secret):
            if len(fragment) >= 4:
                assert fragment not in comment, comment
        assert "REDACTED" in comment

    def test_comment_secrets_do_not_leak_into_the_global_registry(self) -> None:
        """One contract's inline strings must not change how anything else in
        the run is redacted — the comment builder scopes them to its own call."""
        from fluid_build.iac.providers.snowflake import _build_horizon_table_comment
        from fluid_build.observability.secret_redactor import (
            forget_known_secrets,
            known_secret_count,
            redact_secret_text,
        )

        forget_known_secrets()
        _build_horizon_table_comment(
            {
                "id": "silver.community.demo",
                "builds": [{"id": "seed", "properties": {"password": "SCOPED_ONLY_123"}}],
            }
        )
        assert known_secret_count() == 0
        assert "SCOPED_ONLY_123" in redact_secret_text("unrelated line SCOPED_ONLY_123")

    def test_comment_keeps_non_credential_contract_fields(self) -> None:
        """Over-redaction guard: only values under a credential-shaped key are
        masked. A username sitting next to the password must survive."""
        from fluid_build.iac.providers.snowflake import _build_horizon_table_comment

        comment = _build_horizon_table_comment(
            {
                "id": "silver.community.demo",
                "description": "nightly seed job",
                "domain": "community",
                "builds": [
                    {
                        "id": "seed",
                        "properties": {
                            "password": "Pa55head;w0rdTAILMUSTNOTLEAK",
                            "user": "SVC_LOADER",
                            "warehouse": "COMPUTE_WH",
                        },
                    }
                ],
            }
        )
        assert "TAILMUSTNOTLEAK" not in comment
        assert "SVC_LOADER" in comment
        assert "COMPUTE_WH" in comment
        assert "nightly seed job" in comment
