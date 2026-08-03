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

"""URL-userinfo credential redaction, across both symmetric redaction layers.

A credentialed URL (`scheme://user:password@host`) — e.g. an Iceberg REST
catalog `binding.location.uri`, a JDBC endpoint, or a `connection_url` — must
have its password masked before it reaches a log line or a persisted run record.
The password must NOT survive; the scheme/user/host (URLs are safe to log) and
ordinary (userinfo-free) URLs must be left intact.

Both layers share the single `_URL_USERINFO_RE` source of truth (the global
`secret_redactor` defines it; the Snowflake twin imports it) — these tests pin
the behaviour on both so they can never drift apart (CLAUDE.md "extend both").
"""

from __future__ import annotations

import time

import pytest

from fluid_build.observability import secret_redactor as g
from fluid_build.providers.snowflake.util import logging as sf

pytestmark = [pytest.mark.unit]

_CRED_URLS = [
    "https://u:secret@host/path",
    "postgres://admin:hunter2@db.internal:5432/app",
    "jdbc:postgresql://svc:p4ss@10.0.0.1:5432/warehouse",
    "thrift://nessie:topsecret@catalog.example.com:19120/api/v1",
    # password-only userinfo (no username) — the redis / AMQP / Celery broker form
    "redis://:supersecretpw@cache.prod:6379/0",
    "amqps://:mqpassword@broker:5671/vhost",
]


@pytest.mark.parametrize("url", _CRED_URLS)
def test_global_text_path_masks_url_password(url: str) -> None:
    out = g.redact_secret_text(url)
    password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password not in out, f"password leaked: {out}"
    # user + host survive (URL is safe to log; only the password is masked)
    user = url.split("://", 1)[1].split(":", 1)[0]
    host = url.split("@", 1)[1]
    assert user in out and host in out, f"over-redacted: {out}"


@pytest.mark.parametrize("url", _CRED_URLS)
def test_snowflake_twin_masks_url_password(url: str) -> None:
    out = sf.redact_string(url)
    password = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password not in out, f"twin leaked: {out}"


def test_global_dict_path_masks_url_in_neutral_key() -> None:
    # `uri` is not itself a credential-shaped key, so the value recurses to the
    # text path — which is exactly where the URL pattern must catch the password.
    out = g.redact_value({"uri": "https://u:secret@host", "note": "fine"})
    assert "secret" not in out["uri"]
    assert out["note"] == "fine"


def test_snowflake_dict_path_masks_url_in_neutral_key() -> None:
    out = sf.redact_dict({"uri": "https://u:secret@host"})
    assert "secret" not in out["uri"]


def test_both_layers_share_one_regex_object() -> None:
    # Drift-proofing: the twin must use the global's compiled object, not a copy.
    assert g._URL_USERINFO_RE in sf.SENSITIVE_PATTERNS


@pytest.mark.parametrize(
    "safe",
    [
        "https://host:8080/path",  # port, no userinfo
        "https://example.com/a@b",  # '@' in path, no userinfo
        "https://host:8080/q?to=a@b.com",  # '@' in query, no userinfo
        "scheme://user@host",  # user but no password (nothing to mask)
        "plain text with no url at all",
    ],
)
def test_non_secret_urls_untouched(safe: str) -> None:
    assert g.redact_secret_text(safe) == safe
    assert sf.redact_string(safe) == safe


def test_redos_bound_holds_on_adversarial_input() -> None:
    # Worst case for an unbounded userinfo run: a ``scheme://user:`` prefix then a
    # very long password-shaped run that never reaches an '@'. The bounded
    # quantifiers keep this LINEAR (it was polynomial / multi-second — ~19s at
    # this size — before the bound; see the security review). The ceiling is
    # generous (2s) so a slow/loaded CI runner can't flake it while still
    # catching a return of the quadratic blow-up by orders of magnitude.
    evil = "https://u:" + ("a" * 200_000)  # 200k chars, never an '@'
    start = time.monotonic()
    out = g.redact_secret_text(evil)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"possible ReDoS regression: {elapsed:.3f}s"
    assert out == evil  # nothing to redact (no '@')


@pytest.mark.parametrize("plen", [300, 371, 1024])
def test_long_userinfo_password_still_masked(plen: int) -> None:
    # >256-char userinfo secrets are real (Azure SAS tokens, signed-URL secrets,
    # base64 service credentials). The password bound must comfortably exceed
    # them — a tighter cap silently leaks the tail of the secret.
    secret = ("a1B2c3" * (plen // 6 + 1))[:plen]  # plen chars, no /?#@ or space
    url = f"https://acct:{secret}@blob.core.windows.net/container"
    assert secret not in g.redact_secret_text(url), f"len={plen} leaked (global)"
    assert secret not in sf.redact_string(url), f"len={plen} leaked (twin)"
