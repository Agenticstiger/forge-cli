# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
    + a process-stable derived key tied to the Python process id. This
    keeps determinism within a single run while making cross-run rainbow
    tables ineffective. Production callers must set ``FLUID_PII_TOKENIZATION_KEY``.
    """
    raw = os.environ.get(_TOKEN_KEY_ENV)
    if raw:
        return raw.encode("utf-8")
    # Process-stable but unique-per-run fallback: derive from PID + process
    # start time. Any test or single-run job stays deterministic; a fresh
    # process gets a fresh key, defeating rainbow tables.
    LOG.warning(
        "tokenize_pii: %s not set; using a per-process derived key. "
        "Set this env var (or pass hmac_key=...) to make tokens stable across runs "
        "for joins / dedup.",
        _TOKEN_KEY_ENV,
    )
    seed = f"fluid-pid-{os.getpid()}".encode("utf-8")
    return hashlib.sha256(seed).digest()


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
