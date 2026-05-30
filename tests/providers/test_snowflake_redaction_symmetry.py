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

"""Snowflake-side secret-redaction hardening + cross-layer symmetry.

forge-cli runs TWO parallel secret-redaction layers that MUST stay
symmetric (CLAUDE.md: "two parallel redaction layers ... extend both"):

* the global ``SecretRedactingFilter`` / ``redact_secret_text`` in
  ``fluid_build/observability/secret_redactor.py``;
* the Snowflake-provider-local ``redact_string`` / ``redact_dict`` in
  ``fluid_build/providers/snowflake/util/logging.py``.

This module pins the Snowflake-side behaviour of the provider-API-key /
Fernet / PEM hardening and asserts the two layers cover the same
provider-key shapes — the contract that they do not silently drift.
"""

from __future__ import annotations

import pytest

from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.providers.snowflake.util.logging import (
    SENSITIVE_PATTERNS,
    redact_dict,
    redact_string,
)


# Sample provider secrets, assembled from parts at runtime so GitHub
# secret-scanning push protection does not flag literal token prefixes in
# the source file. The keys mirror the global-layer symmetry test in
# tests/test_observability_secret_redactor.py.
def _provider_secret_samples() -> dict[str, str]:
    return {
        "openai": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        # Modern project-/service-account-scoped OpenAI keys carry hyphens
        # and underscores in the body — both redaction layers' char class
        # must admit them. A ``sk-proj-`` key escaped the original
        # ``[A-Za-z0-9]`` class (matching stopped at the first ``-``) and
        # leaked verbatim until the class was widened to ``[A-Za-z0-9_-]``.
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


class TestSnowflakeProviderKeyRedaction:
    """Every provider-API-key shape must be masked by ``redact_string``."""

    @pytest.mark.parametrize("name", sorted(_provider_secret_samples()))
    def test_provider_key_redacted(self, name: str) -> None:
        secret = _provider_secret_samples()[name]
        out = redact_string(f"snowflake stage credential {secret} configured")
        assert secret not in out, f"{name} secret leaked through redact_string"
        assert "[REDACTED]" in out

    def test_fernet_token_redacted(self) -> None:
        token = "gAAAAA" + "C" * 60
        out = redact_string(f"encrypted blob {token}")
        assert token not in out

    def test_pem_private_key_block_redacted_multiline(self) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAsnowflakeKeyMaterialMustVanish\n"
            "abcdefghijklmnopqrstuvwxyz0123456789\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_string(f"key payload:\n{pem}\ntrailing")
        assert "snowflakeKeyMaterialMustVanish" not in out
        assert "MIIEowIBAAKCAQEA" not in out


class TestSnowflakeAssignmentKeyNamePreserved:
    """The bare-assignment regex was rewritten with named groups so the
    key name survives redaction (was: whole-match-redacted to
    ``[REDACTED]``, destroying the ``password=`` prefix)."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("password=hunter2", "password=[REDACTED]"),
            ("api_key=ABC1234567890", "api_key=[REDACTED]"),
            ("api-key=ABC1234567890", "api-key=[REDACTED]"),
            ("secret=topsecretvalue", "secret=[REDACTED]"),
            ("token=abc.def.ghi", "token=[REDACTED]"),
            ("passphrase=letmein", "passphrase=[REDACTED]"),
            # Newly symmetric with the global layer (key alternation widened):
            ("client_secret=topsecret123", "client_secret=[REDACTED]"),
            ("oauth_token=abc.def.ghi", "oauth_token=[REDACTED]"),
            ("authorization=Bearerabc123", "authorization=[REDACTED]"),
            # Env-var prefix (``MY_…``) now matches, mirroring the global regex:
            ("MY_API_KEY=zzz999", "MY_API_KEY=[REDACTED]"),
        ],
    )
    def test_assignment_keeps_key_name(self, line: str, expected: str) -> None:
        assert redact_string(line) == expected

    def test_colon_separator_keeps_key_name(self) -> None:
        out = redact_string("password: hunter2 trailing")
        assert "hunter2" not in out
        assert out.startswith("password: [REDACTED]")

    def test_assignment_stops_at_semicolon_terminator(self) -> None:
        # Value terminator set mirrors the global layer: stop at ``;``.
        out = redact_string("password=hunter2;account=acme")
        assert out == "password=[REDACTED];account=acme"

    def test_password_assignment_not_whole_match_redacted(self) -> None:
        # Regression guard: a prior 0-group ``password=`` pattern shadowed
        # the named-group pattern and emitted a bare ``[REDACTED]``.
        out = redact_string("password=hunter2")
        assert out != "[REDACTED]"
        assert out == "password=[REDACTED]"

    def test_private_key_pem_adjacent_not_corrupted(self) -> None:
        # Regression guard: ``private_key`` must stay OUT of the bare-assignment
        # alternation. If it were in, the assignment regex would run before the
        # PEM-block pattern, mangle the ``-----BEGIN…-----`` header, and the
        # base64 body would leak. The PEM-block pattern must win here.
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEvQIBADBODYMUSTVANISH0123456789\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_string(f"private_key={pem}")
        assert "MIIEvQIBADBODYMUSTVANISH0123456789" not in out
        assert "MIIEvQIBAD" not in out


class TestSnowflakeRedactDict:
    """``redact_dict`` redacts newly covered sensitive key names."""

    @pytest.mark.parametrize(
        "key",
        [
            "connection_string",
            "credential",
            "bearer",
            "jwt",
            "aws_access_key",
            "aws_secret_key",
            "azure_sas_token",
            # Newly mirrored from the global key set:
            "auth_token",
            "session_token",
        ],
    )
    def test_sensitive_key_redacted(self, key: str) -> None:
        out = redact_dict({key: "leaked-value", "safe": "keep-me"})
        assert out[key] == "[REDACTED]"
        assert out["safe"] == "keep-me"


class TestRedactStringDoesNotRaise:
    """``redact_string`` must never raise on the new patterns — the
    named-group pattern uses a ``\\g<key>`` template that would fail if
    routed through the 2-group branch."""

    def test_no_re_error_on_new_pattern_samples(self) -> None:
        import re

        samples = [sec for sec in _provider_secret_samples().values()]
        samples += [
            "password=hunter2",
            "api_key=abc",
            "gAAAAA" + "x" * 30,
            "-----BEGIN EC PRIVATE KEY-----\nabc\n-----END EC PRIVATE KEY-----",
            "eyJ" + "a" * 12 + ".eyJ" + "b" * 12,
        ]
        for sample in samples:
            try:
                redact_string(sample)
            except re.error as exc:  # pragma: no cover - only on regression
                raise AssertionError(f"redact_string raised re.error on {sample!r}: {exc}")


class TestProviderKeyPatternSymmetry:
    """The Snowflake ``SENSITIVE_PATTERNS`` and the global redactor MUST
    cover the same provider-key shapes. This is the lock-step contract:
    a secret masked by one layer must be masked by the other."""

    @pytest.mark.parametrize("name", sorted(_provider_secret_samples()))
    def test_both_layers_mask_provider_secret(self, name: str) -> None:
        secret = _provider_secret_samples()[name]
        line = f"leaked credential {secret} in log line"

        global_out = redact_secret_text(line)
        snowflake_out = redact_string(line)

        assert secret not in global_out, f"{name} leaked through global redactor"
        assert secret not in snowflake_out, f"{name} leaked through Snowflake redactor"

    def test_both_layers_mask_two_segment_jwt(self) -> None:
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJ1c2VyMSJ9"
        line = f"id_token {jwt} issued"
        assert jwt not in redact_secret_text(line)
        assert jwt not in redact_string(line)

    def test_both_layers_mask_fernet_token(self) -> None:
        token = "gAAAAA" + "D" * 50
        line = f"fernet {token} stored"
        assert token not in redact_secret_text(line)
        assert token not in redact_string(line)

    def test_both_layers_mask_pem_block(self) -> None:
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqSYMMETRYMUSTHOLD\n"
            "-----END PRIVATE KEY-----"
        )
        line = f"key:\n{pem}\nend"
        assert "SYMMETRYMUSTHOLD" not in redact_secret_text(line)
        assert "SYMMETRYMUSTHOLD" not in redact_string(line)

    def test_snowflake_named_assignment_pattern_is_dispatchable(self) -> None:
        # The named-group bare-assignment pattern carries 3 groups; the
        # invariant is that ``redact_string`` can substitute it via the
        # ``key``/``value`` groupindex check (not the 2-group branch).
        named = [
            pat
            for pat in SENSITIVE_PATTERNS
            if "key" in pat.groupindex and "value" in pat.groupindex
        ]
        assert named, "expected a named-group assignment pattern in SENSITIVE_PATTERNS"
        for pat in named:
            assert "sep" in pat.groupindex, f"named pattern missing 'sep' group: {pat.pattern!r}"
