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

"""Regression coverage for fluid_build/providers/snowflake/util/logging.py::redact_string.

The redactor branches on ``pattern.groups``: 2-group patterns use the
``\\1[REDACTED]\\2`` template, everything else falls through to a flat
``[REDACTED]``. A prior version used ``> 0`` instead of ``>= 2`` which made
any 1-group pattern raise ``re.error: invalid group reference 2 at position
13`` the moment the redactor ran against a matching string. That turned every
Snowflake ``sf.*`` action into a failure because event logging funnels through
this redactor.
"""

from __future__ import annotations

import re

from fluid_build.providers.snowflake.util.logging import (
    SENSITIVE_PATTERNS,
    redact_dict,
    redact_string,
)


def test_redact_string_handles_single_group_pattern() -> None:
    # Exercises pattern #18 (bare ``key[:=]value`` shape) which has exactly one group.
    out = redact_string("api_key=sk_live_abcd1234efgh5678")
    assert "[REDACTED]" in out
    assert "sk_live_abcd1234efgh5678" not in out


def test_redact_string_preserves_two_group_prefix_suffix_template() -> None:
    # Connection-string pattern: keeps scheme/user prefix + the trailing ``@``.
    redacted = redact_string("postgres://user:supersecret@db.example.com/foo")
    assert redacted.startswith("postgres://user:[REDACTED]@")
    assert "supersecret" not in redacted


def test_redact_string_does_not_raise_for_every_sensitive_pattern() -> None:
    # Catch-all: feed a string that matches each pattern and make sure the
    # redactor never raises ``re.error`` on substitution.
    samples = [
        "postgres://u:p@host",
        '"private_key": "abc"',
        '"oauth_token": "xyz"',
        "Authorization: Bearer abcdefghijklmnop",
        "password=hunter2",
        "api_key=xoxb-12345",
        '"client_secret": "sssh"',
        "AWS_SECRET_ACCESS_KEY=AKIAEXAMPLEKEY123",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.signature",
        "sk_live_abcdef0123456789",
        "ghp_abcdef0123456789abcdef0123456789abcd",
    ]
    for sample in samples:
        try:
            redact_string(sample)
        except re.error as exc:  # pragma: no cover - only hit on regression
            raise AssertionError(f"redact_string raised re.error on {sample!r}: {exc}") from exc


def test_redact_dict_redacts_sensitive_keys_and_scans_string_values() -> None:
    out = redact_dict(
        {
            "password": "hunter2",
            "url": "postgres://u:p@host/db",
            "nested": {"token": "xyz", "label": "api_key=leaked"},
        }
    )
    assert out["password"] == "[REDACTED]"
    assert "p@host" not in out["url"]
    assert out["nested"]["token"] == "[REDACTED]"
    assert "leaked" not in out["nested"]["label"]


def test_sensitive_patterns_group_counts_are_handled_by_redact_string() -> None:
    # Invariant: every pattern's group shape is one ``redact_string`` knows
    # how to substitute. The three handled shapes are:
    #   * 0 or 1 unnamed groups   -> flat ``[REDACTED]``
    #   * exactly 2 unnamed groups -> ``\1[REDACTED]\2``
    #   * named ``key``/``sep``/``value`` groups -> ``\g<key>\g<sep>[REDACTED]``
    # A pattern with 3+ groups that is NOT the named-group shape would
    # silently take the 2-group branch and emit a malformed replacement,
    # so flag that during review.
    for pat in SENSITIVE_PATTERNS:
        named_assignment = "key" in pat.groupindex and "value" in pat.groupindex
        assert pat.groups in (0, 1, 2) or named_assignment, (
            f"unhandled group shape: {pat.groups} groups, "
            f"groupindex={dict(pat.groupindex)} for {pat.pattern!r}"
        )


# ---------------------------------------------------------------------------
# Redaction-symmetry gap closure: shapes the GLOBAL filter already masked but
# this Snowflake-local twin missed (CLAUDE.md "extend both"). Each gap has a
# previously-leaking positive + negative (no-over-redaction) assertions. The
# global side is pinned in tests/test_observability_secret_redactor.py.
# ---------------------------------------------------------------------------


class TestSnowflakeRedactionSymmetryGaps:
    """Gaps ``redact_string`` previously leaked while the global
    ``redact_secret_text`` twin caught them."""

    # -- Gap 5: generic non-``eyJ`` 3-segment JWT --------------------------
    def test_generic_non_eyj_three_segment_jwt_masked(self) -> None:
        # A JWT whose header does NOT base64url-encode to ``eyJ`` (or any three
        # high-entropy base64url runs) — previously only the ``eyJ``-anchored
        # patterns existed here, so this leaked.
        jwt = "aGVhZGVyMTIzNDU2.cGF5bG9hZDEyMzQ1Ng.c2lnbmF0dXJlMTIz"
        out = redact_string(f"stage token {jwt} accepted")
        assert jwt not in out
        assert "[REDACTED]" in out

    def test_eyj_anchored_jwt_still_masked(self) -> None:
        # The tighter eyJ-anchored patterns must still win (positioned first).
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.c2lnbmF0dXJlSGVyZQ"
        assert jwt not in redact_string(f"bearer {jwt}")

    def test_short_dotted_identifiers_not_over_redacted(self) -> None:
        # Segments < 12 chars must NOT match the generic JWT shape.
        for benign in ("app version 1.2.3 released", "com.example.myservice loaded"):
            assert redact_string(benign) == benign

    # -- Gap 6: bare ``private_key=<non-PEM>`` assignment ------------------
    def test_bare_private_key_non_pem_value_masked(self) -> None:
        # Previously leaked: ``private_key`` is intentionally excluded from the
        # bare-assignment alternation (to protect multiline PEMs), so a bare
        # non-PEM ``private_key=<value>`` had no matcher until the dedicated
        # after-PEM pattern was added.
        value = "MIIEvQIBADANBgkqhkiNONPEMVALUE0123456789"
        out = redact_string(f"private_key={value}")
        assert value not in out
        assert out.startswith("private_key=")  # key name preserved
        assert "[REDACTED]" in out

    def test_bare_private_key_hyphen_and_colon_forms_masked(self) -> None:
        assert redact_string("private-key=abc123def456ghi") == "private-key=[REDACTED]"
        out = redact_string("private_key: abc123def456ghi trailing")
        assert "abc123def456ghi" not in out

    def test_multiline_pem_still_redacted_intact(self) -> None:
        # CRITICAL regression: the new bare pattern is positioned AFTER the
        # PEM-block pattern, so a ``private_key=<multiline PEM>`` header is
        # consumed by the PEM pattern first — the base64 body must NOT leak,
        # and the bare pattern must not bite into the PEM body.
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqPEMBODYMUSTVANISH0123456789\n"
            "abcdefghijklmnopqrstuvwxyz9876543210ABCDEF\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_string(f"private_key={pem}")
        assert "PEMBODYMUSTVANISH0123456789" not in out
        assert "MIIEvQIBADANBgkq" not in out
        assert "[REDACTED]" in out

    def test_private_key_prose_not_over_redacted(self) -> None:
        # No ``=``/``:`` value binding -> nothing to redact.
        safe = "loaded private key from the on-disk keystore"
        assert redact_string(safe) == safe

    # -- Cross-layer symmetry: the two layers must not drift ---------------
    def test_gaps_symmetric_with_global_twin(self) -> None:
        from fluid_build.observability.secret_redactor import redact_secret_text

        jwt = "aGVhZGVyMTIzNDU2.cGF5bG9hZDEyMzQ1Ng.c2lnbmF0dXJlMTIz"
        assert jwt not in redact_string(f"t {jwt} x")
        assert jwt not in redact_secret_text(f"t {jwt} x")

        value = "MIIEvQIBADANBgkqhkiNONPEMVALUE0123456789"
        line = f"private_key={value}"
        assert value not in redact_string(line)
        assert value not in redact_secret_text(line)
