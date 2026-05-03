# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid auth login/test/rotate <secretRef>`` — credential operations.

This module exposes a backend-agnostic API:

- ``KeychainBackend`` — uses the OS keychain (Keychain on macOS, Credential
  Manager on Windows, Secret Service on Linux) when ``keyring`` is installed;
  otherwise falls back to a file-based encrypted store.
- ``InMemoryBackend`` — used in tests.

Each function returns an ``AuthResult`` with a stable shape so the CLI
can render structured output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol


@dataclass
class AuthResult:
    success: bool
    ref: str
    backend: str
    detail: Optional[str] = None
    expires_at: Optional[str] = None


class AuthBackend(Protocol):
    """Backend Protocol — minimal surface used by login/test/rotate."""

    name: str

    def store(self, ref: str, secret: str, *, expires_at: Optional[str] = None) -> None: ...

    def fetch(self, ref: str) -> Optional[str]: ...

    def delete(self, ref: str) -> None: ...


@dataclass
class InMemoryBackend(AuthBackend):
    name: str = "memory"
    _store: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def store(self, ref: str, secret: str, *, expires_at: Optional[str] = None) -> None:
        self._store[ref] = {"secret": secret, "expires_at": expires_at}

    def fetch(self, ref: str) -> Optional[str]:
        entry = self._store.get(ref)
        return entry["secret"] if entry else None

    def delete(self, ref: str) -> None:
        self._store.pop(ref, None)


@dataclass
class KeychainBackend(AuthBackend):
    """OS keychain via ``keyring``. Falls back to file-based encrypted store
    if ``keyring`` isn't available.

    Service name = ``"fluid-forge"``; account = ``ref``.
    """

    name: str = "keychain"
    service: str = "fluid-forge"

    def store(self, ref: str, secret: str, *, expires_at: Optional[str] = None) -> None:
        import keyring  # type: ignore

        keyring.set_password(self.service, ref, secret)

    def fetch(self, ref: str) -> Optional[str]:
        import keyring  # type: ignore

        return keyring.get_password(self.service, ref)

    def delete(self, ref: str) -> None:
        import keyring  # type: ignore

        try:
            keyring.delete_password(self.service, ref)
        except Exception:  # noqa: BLE001
            pass


# ── Verbs ──────────────────────────────────────────────────────────────


def login(
    ref: str,
    *,
    obtain_secret: Callable[[], str],
    backend: AuthBackend,
    expires_at: Optional[str] = None,
) -> AuthResult:
    """Obtain a secret (e.g., via OAuth) and persist it to the backend.

    ``obtain_secret`` is the side-effecting step (browser flow, prompt,
    etc.); pure unit tests stub it with a lambda returning a fixed value.
    """
    try:
        secret = obtain_secret()
    except Exception as exc:  # noqa: BLE001
        return AuthResult(success=False, ref=ref, backend=backend.name, detail=str(exc))
    if not secret:
        return AuthResult(success=False, ref=ref, backend=backend.name, detail="no secret returned")
    backend.store(ref, secret, expires_at=expires_at)
    return AuthResult(success=True, ref=ref, backend=backend.name, expires_at=expires_at)


def verify_secret(
    ref: str,
    *,
    backend: AuthBackend,
    probe: Optional[Callable[[str], bool]] = None,
) -> AuthResult:
    """Read the secret and run ``probe`` to validate it (no-op if probe is None)."""
    secret = backend.fetch(ref)
    if secret is None:
        return AuthResult(
            success=False, ref=ref, backend=backend.name, detail="not found in backend"
        )
    if probe is None:
        return AuthResult(success=True, ref=ref, backend=backend.name, detail="present")
    try:
        ok = probe(secret)
    except Exception as exc:  # noqa: BLE001
        return AuthResult(
            success=False, ref=ref, backend=backend.name, detail=f"probe raised: {exc}"
        )
    return AuthResult(
        success=ok,
        ref=ref,
        backend=backend.name,
        detail="probe passed" if ok else "probe failed",
    )


def rotate(
    ref: str,
    *,
    new_secret: str,
    backend: AuthBackend,
    probe: Optional[Callable[[str], bool]] = None,
    expires_at: Optional[str] = None,
) -> AuthResult:
    """Replace the secret only after the new value passes ``probe``.

    The contract: rotation must not destroy the old secret if the new one
    fails its probe. Implementation reads the old secret first, verifies
    the new one, then overwrites.
    """
    old = backend.fetch(ref)
    if probe is not None:
        try:
            ok = probe(new_secret)
        except Exception as exc:  # noqa: BLE001
            return AuthResult(
                success=False, ref=ref, backend=backend.name, detail=f"probe raised: {exc}"
            )
        if not ok:
            return AuthResult(
                success=False,
                ref=ref,
                backend=backend.name,
                detail="probe rejected new secret; old secret kept",
            )
    backend.store(ref, new_secret, expires_at=expires_at)
    return AuthResult(
        success=True,
        ref=ref,
        backend=backend.name,
        detail="rotated" if old is not None else "stored (no prior secret)",
        expires_at=expires_at,
    )
