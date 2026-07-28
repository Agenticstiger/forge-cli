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

"""PII tokenization pre-land hook.

Replaces values in PII-classified columns with a deterministic
HMAC-SHA256 hex token (32 hex chars = 128 bits of collision resistance).
The HMAC key comes from ``FLUID_PII_TOKENIZATION_KEY`` env var or
explicit ``hmac_key`` constructor argument.

A truncated SHA-256 prefix without a key is brute-force / rainbow-table
attackable when the PII domain is small (US phone numbers, common
emails, dates of birth, etc.). HMAC with a server-side secret defeats
both lookup attacks while preserving determinism for a fixed key. Token
width of 128 bits is the floor below which collision attacks become
practical.

For format-preserving encryption, swap to a Vault client at the
``tokenize`` callable; this default is suitable for non-reversible
pseudonymization (typical Bronze use case).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fluid_build.api.hooks import HookResult

LOG = logging.getLogger("fluid.acquire.hooks.tokenize_pii")

_TOKEN_HEX_LEN = 32  # 128 bits of entropy
_TOKEN_KEY_ENV = "FLUID_PII_TOKENIZATION_KEY"


def _hmac_token(value: Any, *, key: bytes) -> str:
    """HMAC-SHA256 hex token, truncated to ``_TOKEN_HEX_LEN`` characters."""
    digest = hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:_TOKEN_HEX_LEN]


def _resolve_default_key() -> bytes:
    """Resolve the HMAC key from environment.

    A missing key is a configuration error in production but, since this
    hook may run in dev / CI without a vault, we fall back to a *warning*
    + an **ephemeral, cryptographically-random** key
    (``secrets.token_bytes(32)``). The key lives only for the lifetime of
    this ``_resolve_default_key()`` call, so tokens are deterministic
    within a single hook instance (the key is captured once in
    ``__post_init__``) but **unlinkable across runs** — there is no
    stable secret to derive a rainbow table against.

    This deliberately replaces the previous PID-derived fallback
    (``sha256("fluid-pid-<pid>")``): the keyspace was the PID space
    (typically < 2**22 on Linux / macOS), which is trivially
    brute-forceable, and — contrary to the old docstring — no
    process-start-time was ever mixed in, so it was NOT a rainbow-table
    defence.

    The fallback is intentionally **not** stable across runs. Callers
    that need cross-run stable tokens (joins / dedup against previously
    tokenized data) MUST set ``FLUID_PII_TOKENIZATION_KEY`` (or pass
    ``hmac_key=...``); do not rely on the fallback for stability.
    """
    raw = os.environ.get(_TOKEN_KEY_ENV)
    if raw:
        return raw.encode("utf-8")
    # Ephemeral, cryptographically-random per-resolution fallback. A fresh
    # call gets a fresh 256-bit key, so tokens cannot be linked across runs
    # and there is no low-entropy secret to brute-force.
    LOG.warning(
        "tokenize_pii: %s not set; using an ephemeral random key. "
        "Tokens will NOT be stable across runs — set this env var (or pass "
        "hmac_key=...) to make tokens stable for joins / dedup.",
        _TOKEN_KEY_ENV,
    )
    return secrets.token_bytes(32)


@dataclass
class TokenizePiiHook:
    name: str = "tokenize_pii"
    hmac_key: Optional[bytes] = None
    tokenize: Optional[Callable[[Any], str]] = None
    sensitive_labels: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if self.sensitive_labels is None:
            self.sensitive_labels = ["email", "phone", "ssn", "credit_card", "ip"]
        if self.tokenize is None:
            key = self.hmac_key if self.hmac_key is not None else _resolve_default_key()
            self.tokenize = lambda v, _k=key: _hmac_token(v, key=_k)

    def apply(self, records: List[Dict[str, Any]], ctx: Dict[str, Any]) -> HookResult:
        # ctx.get("classifications") is the running map from prior hooks.
        classifications: Dict[str, List[str]] = ctx.get("classifications", {})
        sensitive_cols = {
            col
            for col, labels in classifications.items()
            if any(lbl in (self.sensitive_labels or []) for lbl in labels)
        }
        if not sensitive_cols:
            return HookResult(records=records)
        assert self.tokenize is not None  # noqa: S101 — set in __post_init__
        new_records: List[Dict[str, Any]] = []
        for r in records:
            nr = dict(r)
            for col in sensitive_cols:
                if col in nr and nr[col] is not None:
                    nr[col] = self.tokenize(nr[col])
            new_records.append(nr)
        return HookResult(records=new_records)
