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
