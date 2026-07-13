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

"""Tests for central log secret redaction."""

from __future__ import annotations

import logging
import sys
from io import StringIO

import pytest

from fluid_build.cli import _setup_enhanced_logging
from fluid_build.observability.secret_redactor import SecretRedactingFilter


@pytest.fixture
def isolated_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_filters = list(root.filters)
    original_level = root.level

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for log_filter in list(root.filters):
        root.removeFilter(log_filter)

    yield root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for log_filter in list(root.filters):
        root.removeFilter(log_filter)

    root.setLevel(original_level)
    for log_filter in original_filters:
        root.addFilter(log_filter)
    for handler in original_handlers:
        root.addHandler(handler)


def test_secret_redacting_filter_redacts_args_form_dict_repr_and_exception_text():
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.direct")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info(
        "password=%s payload=%s",
        "hunter2",
        {"oauth_token": "tok-123", "name": "safe-name"},
    )
    logger.info("formatted leak SNOWFLAKE_PASSWORD=hunter2")

    try:
        raise RuntimeError("oauth_token=eyJhbGciOiJIUzI1NiJ9.payload.signature")
    except RuntimeError:
        logger.exception("private_key=super-secret")

    output = stream.getvalue()
    assert "hunter2" not in output
    assert "tok-123" not in output
    assert "super-secret" not in output
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in output
    assert "safe-name" in output
    assert "***REDACTED***" in output


def test_setup_enhanced_logging_installs_secret_redaction(monkeypatch, isolated_root_logger):
    stream = StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    _setup_enhanced_logging("INFO", None)
    logging.getLogger("fluid.test").error("AWS_SECRET_ACCESS_KEY=%s", "very-secret")

    output = stream.getvalue()
    assert "very-secret" not in output
    assert "***REDACTED***" in output


def test_filter_preserves_non_sensitive_positional_args():
    """Regression: a sensitive placeholder must not cause unrelated args to
    be redacted. Scalars bound to non-sensitive placeholders stay intact."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_positional")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info("user=%s password=%s took=%dms", "alice", "hunter2", 42)

    output = stream.getvalue()
    assert "alice" in output
    assert "42ms" in output
    assert "hunter2" not in output
    assert "password=***REDACTED***" in output


def test_filter_handles_named_placeholder_mapping():
    """Named (``%(name)s``) args are scoped by placeholder adjacency AND by
    mapping key. Sensitive values are redacted; unrelated values stay."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_named")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    logger.info("auth=%(token)s request_id=%(id)s", {"token": "abc-123", "id": "req-42"})

    output = stream.getvalue()
    assert "abc-123" not in output
    assert "req-42" in output
    assert "auth=***REDACTED***" in output


def test_filter_preserves_custom_object_repr_when_not_sensitive():
    """Non-string, non-container args (custom objects, bools, floats) pass
    through unchanged when their placeholder isn't sensitive."""
    stream = StringIO()
    logger = logging.getLogger("test.secret_redactor.scoped_object")
    logger.handlers = []
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)

    class _CustomPayload:
        def __repr__(self) -> str:
            return "CustomPayload(ok=True)"

    logger.info("status=%s latency_ms=%s payload=%r", "ok", 17.5, _CustomPayload())

    output = stream.getvalue()
    assert "status=ok" in output
    assert "latency_ms=17.5" in output
    assert "CustomPayload(ok=True)" in output
    assert "***REDACTED***" not in output


# ---------------------------------------------------------------------------
# S-010: provider-specific token shapes
# ---------------------------------------------------------------------------


class TestProviderTokenShapes:
    """SECURITY_REVIEW S-010: add explicit coverage for common third-party
    token shapes. These are high-value targets for secret-scanning — if a
    key leaks anywhere in the log stream, a bare-string pattern match is
    the last line of defense."""

    def _redact(self, text: str) -> str:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return redact_secret_text(text)

    # NOTE ON FIXTURE CONSTRUCTION: the "secret" strings below are
    # assembled from parts at runtime rather than written as literals.
    # GitHub's secret-scanning push protection flags literal Stripe /
    # GitHub token prefixes in source files — even obvious placeholders
    # — so we have to prevent the pattern from appearing in the
    # file-level bytes while keeping the runtime value realistic enough
    # that the redactor's regex still matches.

    def test_stripe_live_key_redacted(self):
        secret = "sk_" + "live" + "_51AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        out = self._redact(f"using key {secret} for billing")
        assert secret not in out
        assert "***REDACTED***" in out

    def test_stripe_test_key_redacted(self):
        secret = "sk_" + "test" + "_51AbCdEfGhIjKlMnOpQrStUvWxYz"
        out = self._redact(f"charge with {secret}")
        assert secret not in out

    def test_github_classic_token_redacted(self):
        secret = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        out = self._redact(f"Authorization: token {secret}")
        assert secret not in out

    def test_github_oauth_token_redacted(self):
        secret = "gh" + "o_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        out = self._redact(f"token {secret}")
        assert secret not in out

    def test_github_fine_grained_pat_redacted(self):
        secret = "github_" + "pat_" + "11ABCDEFG0abcdefghijklmnopqr"
        out = self._redact(f"auth: {secret}")
        assert secret not in out

    def test_api_key_assignment_redacted(self):
        out = self._redact("api_key=XYZ-1234567890-abcd")
        assert "XYZ-1234567890-abcd" not in out
        assert "api_key" in out  # key-name preserved, value redacted
        assert "***REDACTED***" in out

    def test_bearer_token_redacted(self):
        out = self._redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature")
        assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in out

    def test_non_secret_looking_text_passes_through(self):
        safe = "this is a perfectly safe error message with no secrets"
        assert self._redact(safe) == safe


# ---------------------------------------------------------------------------
# Provider API-key shapes (detect-secrets / gitleaks rule prefixes)
# ---------------------------------------------------------------------------


def _redact_global(text: str) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(text)


# Sample provider secrets. Built from parts at runtime: GitHub secret-
# scanning push protection flags literal token prefixes in source files,
# so we keep the realistic prefix out of the file-level bytes while still
# producing a value the redactor's regex matches. The same list is reused
# by the cross-layer symmetry test below.
def _provider_secret_samples() -> dict[str, str]:
    return {
        "openai": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        # Modern project-/service-account-scoped OpenAI keys carry hyphens
        # and underscores in the body — the redactor char class must admit
        # them. A ``sk-proj-`` key escaped the original ``[A-Za-z0-9]`` class.
        "openai_project": "sk-proj-" + "A1b2C3-d4E5f6_G7h8I9j0-K1l2M3n4",
        "openai_svcacct": "sk-svcacct-" + "Xy9_aBc-DeF456ghiJKL012mnoPQR",
        "anthropic": "sk-ant-" + "api03-" + "x" * 40,
        "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
        "aws_temp_key": "ASIA" + "IOSFODNN7EXAMPLE",
        "gcp_api_key": "AIza" + "Sy" + "C" * 33,
        "slack_bot": "xox" + "b-" + "1111111111-abcdefghij",
        "slack_user": "xox" + "p-" + "2222222222abcd",
        "huggingface": "hf_" + "q" * 35,
        "replicate": "r8_" + "Z" * 35,
        "gitlab_pat": "glp" + "at-" + "a" + "k" * 24,
        "vercel": "vc_" + "9" * 24,
        "fernet": "gAAAAA" + "B" * 40,
    }


class TestProviderApiKeyShapes:
    """Each provider API-key shape must be masked anywhere in the stream."""

    @pytest.mark.parametrize("name", sorted(_provider_secret_samples()))
    def test_provider_key_redacted(self, name: str):
        secret = _provider_secret_samples()[name]
        out = _redact_global(f"connecting with {secret} now")
        assert secret not in out, f"{name} secret leaked through global redactor"
        assert "***REDACTED***" in out

    def test_anthropic_key_redacted_when_openai_prefix_overlaps(self):
        # ``sk-ant-`` is a strict prefix of the looser OpenAI ``sk-`` shape;
        # the full Anthropic token must be gone, not just the ``sk-...`` tail.
        secret = "sk-ant-" + "api03-" + "z" * 40
        out = _redact_global(f"anthropic={secret}")
        assert secret not in out
        assert "ant-" not in out  # no Anthropic-shaped remnant survives


class TestFernetAndPemRedaction:
    """Fernet tokens and multiline PEM private-key blocks."""

    def test_fernet_token_redacted(self):
        token = "gAAAAA" + "C" * 60
        out = _redact_global(f"decrypt token {token}")
        assert token not in out
        assert "***REDACTED***" in out

    def test_pem_private_key_block_redacted_inline(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAsecretkeymaterialhere\n"
            "abcdefghijklmnopqrstuvwxyz0123456789\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = _redact_global(f"loaded key:\n{pem}\ndone")
        assert "secretkeymaterialhere" not in out
        assert "MIIEowIBAAKCAQEA" not in out
        assert "***REDACTED***" in out

    def test_pem_private_key_redacted_in_multiline_exception_text(self):
        """SECURITY: a PEM block embedded in a traceback must be scrubbed
        across line boundaries via the ``exc_text`` path of the filter."""
        stream = StringIO()
        logger = logging.getLogger("test.secret_redactor.pem_traceback")
        logger.handlers = []
        logger.filters = []
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(SecretRedactingFilter())
        logger.addHandler(handler)

        pem = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "MHcCAQEEILeakedEcKeyMaterialThatMustBeScrubbed\n"
            "-----END EC PRIVATE KEY-----"
        )
        try:
            raise RuntimeError(f"failed to parse key:\n{pem}")
        except RuntimeError:
            logger.exception("key load failed")

        output = stream.getvalue()
        assert "LeakedEcKeyMaterialThatMustBeScrubbed" not in output
        assert "MHcCAQEEIL" not in output


class TestTwoSegmentJwt:
    """An ``eyJ``-anchored two-segment (header.payload) JWT — no signature."""

    def test_two_segment_jwt_redacted(self):
        # header.payload only — would NOT match the 3-segment ``_JWT_RE``.
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJ1c2VyMSJ9"
        out = _redact_global(f"id_token={jwt}")
        assert jwt not in out
        assert "***REDACTED***" in out

    def test_three_segment_jwt_still_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.c2lnbmF0dXJlSGVyZQ"
        out = _redact_global(f"bearer {jwt}")
        assert jwt not in out


class TestSensitiveKeyParts:
    """Newly added substring-match key parts must mark a mapping key
    sensitive (``connection_string``, ``bearer``, ``jwt`` etc.)."""

    @pytest.mark.parametrize(
        "key",
        [
            "connection_string",
            "db_connection_string",
            "bearer",
            "bearer_token",
            "jwt",
            "id_jwt",
            "credential",
            "service_credential",
            "aws_access_key",
            "aws_access_key_id",
            "aws_secret_key",
            "azure_sas_token",
        ],
    )
    def test_new_key_part_marks_key_sensitive(self, key: str):
        from fluid_build.observability.secret_redactor import (
            is_sensitive_key_name,
            redact_value,
        )

        assert is_sensitive_key_name(key) is True
        redacted = redact_value({key: "leaked-value-here"})
        assert redacted[key] == "***REDACTED***"


class TestAssignmentRegexBacktrackingBound:
    """The assignment regex quantifiers are upper-bounded so an adversarial
    log line cannot trigger catastrophic backtracking."""

    def test_adversarial_assignment_line_completes_quickly(self):
        import time

        adversarial = "a_" * 300 + "password" + " " * 8000 + ":" + "x" * 8000
        start = time.perf_counter()
        _redact_global(adversarial)
        elapsed = time.perf_counter() - start
        # Linear-time behaviour finishes in well under a second; a
        # catastrophic-backtracking regression would hang for minutes.
        assert elapsed < 1.0, f"assignment redaction took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Cross-layer symmetry: the global SecretRedactingFilter and the Snowflake
# provider-local redact_string MUST cover the same provider-key shapes.
# This is the contract that keeps the two redaction layers in lock-step
# (CLAUDE.md: "two parallel redaction layers ... extend both").
# ---------------------------------------------------------------------------


class TestRedactionLayerSymmetry:
    """Both redaction layers must mask every provider-key shape."""

    @pytest.mark.parametrize("name", sorted(_provider_secret_samples()))
    def test_both_layers_mask_provider_secret(self, name: str):
        from fluid_build.providers.snowflake.util.logging import redact_string

        secret = _provider_secret_samples()[name]
        line = f"observed credential {secret} in payload"

        global_out = _redact_global(line)
        snowflake_out = redact_string(line)

        assert secret not in global_out, f"{name} leaked through global redactor"
        assert secret not in snowflake_out, f"{name} leaked through Snowflake redactor"

    def test_both_layers_mask_pem_private_key_block(self):
        from fluid_build.providers.snowflake.util.logging import redact_string

        pem = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmVMUSTBEGONE\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        line = f"key:\n{pem}\nend"
        assert "MUSTBEGONE" not in _redact_global(line)
        assert "MUSTBEGONE" not in redact_string(line)

    def test_both_layers_preserve_key_name_on_assignment(self):
        """Both layers redact only the value of ``password=...`` and keep
        the ``password`` key name (the Snowflake named-group fix)."""
        from fluid_build.providers.snowflake.util.logging import redact_string

        global_out = _redact_global("password=hunter2")
        snowflake_out = redact_string("password=hunter2")

        assert "hunter2" not in global_out
        assert "hunter2" not in snowflake_out
        # key name survives in both
        assert global_out.startswith("password=")
        assert snowflake_out == "password=[REDACTED]"


# ---------------------------------------------------------------------------
# Redaction-symmetry gap closure: shapes the SNOWFLAKE twin already masked
# but the GLOBAL filter missed (CLAUDE.md "extend both"). Each gap has a
# previously-leaking positive + a couple of negative (no-over-redaction)
# assertions. The Snowflake side is pinned in
# tests/providers/test_snowflake_util_logging.py.
# ---------------------------------------------------------------------------


class TestGlobalRedactionSymmetryGaps:
    """Gaps the global ``redact_secret_text`` / ``redact_value`` previously
    leaked while the Snowflake-local twin caught them."""

    # -- Gap 1: bare ``passphrase=<value>`` assignment ---------------------
    def test_bare_passphrase_assignment_masked(self):
        # Previously leaked: ``passphrase`` was absent from _ASSIGNMENT_RE.
        out = _redact_global("passphrase=SuperSecret123")
        assert "SuperSecret123" not in out
        assert "passphrase" in out  # key name preserved
        assert "***REDACTED***" in out

    def test_passphrase_colon_separator_masked(self):
        out = _redact_global("client_passphrase: hunter2trailing next")
        assert "hunter2trailing" not in out
        assert "***REDACTED***" in out

    def test_passphrase_prose_not_over_redacted(self):
        # No ``=``/``:`` value binding -> nothing to redact.
        safe = "the passphrase policy is documented in the runbook"
        assert _redact_global(safe) == safe

    # -- Gap 2: quoted-JSON ``"credentials": "..."`` -----------------------
    def test_quoted_json_credentials_value_masked(self):
        # Previously leaked: the assignment regex had no optional closing
        # quote before ``:`` so a quoted JSON key slipped past on the text path.
        out = _redact_global('{"credentials": "hunter2secretvalue"}')
        assert "hunter2secretvalue" not in out
        assert "***REDACTED***" in out

    def test_bare_credentials_assignment_masked(self):
        out = _redact_global("credentials=topsecretblob123")
        assert "topsecretblob123" not in out
        assert "credentials" in out

    def test_benign_json_not_over_redacted(self):
        safe = '{"username": "alice", "id": "req-42"}'
        assert _redact_global(safe) == safe

    # -- Gap 3: dict key literally named ``auth`` --------------------------
    def test_auth_dict_key_masked(self):
        from fluid_build.observability.secret_redactor import (
            is_sensitive_key_name,
            redact_value,
        )

        assert is_sensitive_key_name("auth") is True
        out = redact_value({"auth": "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="})
        assert out["auth"] == "***REDACTED***"

    def test_auth_dict_key_negatives(self):
        from fluid_build.observability.secret_redactor import redact_value

        out = redact_value({"username": "alice", "region": "us-east-1"})
        assert out["username"] == "alice"
        assert out["region"] == "us-east-1"

    # -- Gap 4: dict keys ``conn_str`` / ``connection_url`` ----------------
    def test_conn_str_and_connection_url_dict_keys_masked(self):
        from fluid_build.observability.secret_redactor import (
            is_sensitive_key_name,
            redact_value,
        )

        assert is_sensitive_key_name("conn_str") is True
        assert is_sensitive_key_name("connection_url") is True
        out = redact_value(
            {
                "conn_str": "Driver={ODBC};Server=h;Pwd=topsecretpw;",
                "connection_url": "jdbc:pg://user:topsecretpw@host/db",
            }
        )
        assert out["conn_str"] == "***REDACTED***"
        assert out["connection_url"] == "***REDACTED***"

    def test_plain_url_key_not_over_redacted(self):
        # ``conn_str`` addition must not broaden to a plain ``url`` key.
        from fluid_build.observability.secret_redactor import (
            is_sensitive_key_name,
            redact_value,
        )

        assert is_sensitive_key_name("url") is False
        out = redact_value({"url": "https://example.com/page"})
        assert out["url"] == "https://example.com/page"

    # -- Cross-layer symmetry: the two layers must not drift ---------------
    def test_gaps_symmetric_with_snowflake_twin(self):
        from fluid_build.observability.secret_redactor import redact_value
        from fluid_build.providers.snowflake.util.logging import (
            redact_dict,
            redact_string,
        )

        for line, secret in [
            ("passphrase=SuperSecret123", "SuperSecret123"),
            ('{"credentials": "hunter2secretvalue"}', "hunter2secretvalue"),
        ]:
            assert secret not in _redact_global(line)
            assert secret not in redact_string(line)

        for key in ("auth", "conn_str", "connection_url"):
            gv = redact_value({key: "leaked-value-here"})
            sv = redact_dict({key: "leaked-value-here"})
            assert gv[key] == "***REDACTED***"
            assert sv[key] == "[REDACTED]"
