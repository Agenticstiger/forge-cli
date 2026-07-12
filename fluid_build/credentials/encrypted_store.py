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

"""
Encrypted file credential storage.

Stores credentials in an encrypted file using Fernet (AES encryption).
Useful for CI/CD environments where OS keyring is not available.
"""

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - optional-dependency guard
    # We intentionally do NOT rebind ``Fernet`` / ``InvalidToken`` to a
    # sentinel here. They are referenced only by instance methods, and
    # ``EncryptedCredentialStore.__init__`` raises before any such reference
    # when the dependency is missing (``CRYPTOGRAPHY_AVAILABLE`` is False).
    # Leaving the names unbound keeps this module ``mypy --strict`` clean
    # whether or not ``cryptography`` is installed at type-check time.
    CRYPTOGRAPHY_AVAILABLE = False


def _secure_parent_dir(path: Path) -> None:
    """Create ``path.parent`` with mode 0o700.

    ``mkdir`` honors the process umask (typically 0o022 → 0o755), which
    on shared hosts leaks existence / timestamps of the credentials
    directory to other local accounts. We tighten to 0o700 after the
    fact. ``chmod`` is best-effort: on Windows / restricted filesystems
    the call may fail, which we tolerate silently.

    See SECURITY_REVIEW S-009.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except (NotImplementedError, PermissionError, OSError):
        # Windows / restricted FS — chmod is best-effort.
        return
    # S-009: verify the chmod actually applied on POSIX. A permissive
    # umask plus a silently-ignored chmod (immutable bit, SELinux, ACLs)
    # would leave the credentials dir enumerable by other local accounts.
    if os.name == "posix":
        try:
            actual = stat.S_IMODE(parent.stat().st_mode)
        except OSError:
            return
        if actual & 0o077:
            logger.warning(
                "Credentials directory %s is mode %o, expected 0o700 — "
                "other local accounts may be able to enumerate it.",
                parent,
                actual,
            )


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with restrictive permissions.

    Opens a sibling temp file with ``O_EXCL`` + the target ``mode`` (so the
    file is never briefly world-readable under a permissive umask — the
    create-then-chmod window was SECURITY_REVIEW S-009/B2), ``fsync``s it,
    then atomically ``os.replace``s it over ``path`` and fsyncs the parent
    directory so the rename survives a crash.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Parent-directory fsync is best-effort — not supported on every
        # platform / filesystem (notably Windows).
        pass


class EncryptedCredentialStore:
    """Encrypted credential storage for CI/CD environments."""

    def __init__(self, store_path: Optional[Path] = None, key_path: Optional[Path] = None):
        """
        Initialize encrypted credential store.

        Args:
            store_path: Path to encrypted credentials file (default: ~/.fluid/credentials.enc)
            key_path: Path to encryption key file (default: ~/.fluid/.key)
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            raise ImportError(
                "cryptography library required for encrypted credential storage. "
                "Install with: pip install cryptography"
            )

        self.store_path = store_path or Path.home() / ".fluid" / "credentials.enc"
        self.key_path = key_path or Path.home() / ".fluid" / ".key"
        self._ensure_key()

    def _ensure_key(self) -> None:
        """Resolve the Fernet key, in priority order:

        1. ``FLUID_ENCRYPTION_KEY`` — a raw urlsafe-base64 Fernet key
           supplied directly (e.g. from a CI secret store).
        2. ``FLUID_ENCRYPTION_PASSPHRASE`` — a passphrase; the key is
           derived via PBKDF2-HMAC-SHA256 (600k iterations) over a salt
           persisted next to the store. No key material touches disk —
           the recommended mode for CI, where an on-disk ``.key`` sitting
           next to the ciphertext otherwise defeats the encryption.
        3. On-disk ``.key`` file — desktop convenience fallback.
        """
        env_key = os.environ.get("FLUID_ENCRYPTION_KEY")
        if env_key:
            self.key = env_key.encode("utf-8") if isinstance(env_key, str) else env_key
            self.cipher = Fernet(self.key)
            logger.debug("Loaded encryption key from FLUID_ENCRYPTION_KEY")
            return

        passphrase = os.environ.get("FLUID_ENCRYPTION_PASSPHRASE")
        if passphrase:
            self.key = self._derive_key_from_passphrase(passphrase)
            self.cipher = Fernet(self.key)
            logger.debug("Derived encryption key from FLUID_ENCRYPTION_PASSPHRASE")
            return

        if not self.key_path.exists():
            # Desktop fallback. Parent dir is chmod'd 0o700 to prevent
            # other local accounts from enumerating the store (S-009);
            # the key is atomically created 0o600 (no umask window).
            _secure_parent_dir(self.key_path)
            key = Fernet.generate_key()
            _atomic_write_bytes(self.key_path, key)
            logger.info(f"Generated new encryption key: {self.key_path}")

        self.key = self.key_path.read_bytes()
        self.cipher = Fernet(self.key)
        logger.debug(f"Loaded encryption key from: {self.key_path}")

    def _derive_key_from_passphrase(self, passphrase: str) -> bytes:
        """Derive a Fernet key from a passphrase via PBKDF2-HMAC-SHA256.

        The 16-byte salt is persisted next to the store (``<store>.salt``)
        so the same passphrase yields the same key across runs; the salt
        is not secret. No key material is ever written to disk.
        """
        import base64
        import secrets as _secrets

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        salt_path = self.store_path.with_name(self.store_path.name + ".salt")
        if salt_path.exists():
            salt = salt_path.read_bytes()
        else:
            salt = _secrets.token_bytes(16)
            _secure_parent_dir(salt_path)
            _atomic_write_bytes(salt_path, salt)
        # 600k iterations meets the OWASP 2026 floor for PBKDF2-HMAC-SHA256.
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    def rotate_key(self) -> None:
        """Re-encrypt every stored credential under a freshly generated key.

        Only meaningful in the on-disk-key mode (env-var / passphrase keys
        are rotated by the operator changing the environment value). Reads
        the store with the current cipher, generates a new key, and
        atomically replaces both the key file and the ciphertext.
        """
        if os.environ.get("FLUID_ENCRYPTION_KEY") or os.environ.get("FLUID_ENCRYPTION_PASSPHRASE"):
            from fluid_build.credentials.resolver import CredentialError

            raise CredentialError(
                "rotate_key() applies to the on-disk key mode; rotate an "
                "env-var / passphrase key by changing the environment value."
            )
        data = self._load_store()
        _secure_parent_dir(self.key_path)
        new_key = Fernet.generate_key()
        _atomic_write_bytes(self.key_path, new_key)
        self.key = new_key
        self.cipher = Fernet(new_key)
        self._save_store(data)
        logger.info("Rotated encryption key; re-encrypted %d credentials", len(data))

    def set_credential(self, key: str, value: str, expires_at: Optional[str] = None) -> None:
        """
        Store encrypted credential with metadata.

        Args:
            key: Credential key (e.g., "snowflake.password")
            value: Credential value to encrypt and store
            expires_at: Optional ISO8601 expiration timestamp
        """
        data = self._load_store()
        data[key] = {
            "value": value,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
        }
        self._save_store(data)
        logger.debug(f"Stored encrypted credential: {key}")

    def get_credential(self, key: str) -> Optional[str]:
        """
        Retrieve encrypted credential value.

        Args:
            key: Credential key to retrieve

        Returns:
            Decrypted credential value or None if not found
        """
        data = self._load_store()
        entry = data.get(key)
        if entry is None:
            return None
        # Support both old format (bare string) and new format (dict with metadata)
        value = entry["value"] if isinstance(entry, dict) else entry
        if value:
            # Constant-only log: ``value`` (the credential) is in scope.
            logger.debug("Retrieved encrypted credential")
        # The store is decrypted JSON (``Dict[str, Any]``); the stored value is
        # a ``str`` by construction, so narrow it at the typed return boundary.
        return cast(Optional[str], value)

    def get_credential_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve credential metadata (stored_at, expires_at).

        Returns:
            Dict with 'stored_at', 'expires_at' keys, or None if not found.
        """
        data = self._load_store()
        entry = data.get(key)
        if entry is None:
            return None
        if isinstance(entry, dict):
            return {
                "stored_at": entry.get("stored_at"),
                "expires_at": entry.get("expires_at"),
            }
        # Old format — no metadata available
        return {"stored_at": None, "expires_at": None}

    def get_credential_with_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve credential value and metadata in a single load.

        Returns:
            Dict with 'value', 'stored_at', 'expires_at' keys, or None if not found.
        """
        data = self._load_store()
        entry = data.get(key)
        if entry is None:
            return None
        if isinstance(entry, dict):
            return {
                "value": entry.get("value"),
                "stored_at": entry.get("stored_at"),
                "expires_at": entry.get("expires_at"),
            }
        # Old format — bare string, no metadata
        return {"value": entry, "stored_at": None, "expires_at": None}

    def delete_credential(self, key: str) -> None:
        """
        Remove credential from encrypted store.

        Args:
            key: Credential key to delete
        """
        data = self._load_store()
        if key in data:
            del data[key]
            self._save_store(data)
            logger.debug(f"Deleted encrypted credential: {key}")

    def list_credentials(self) -> list[str]:
        """
        List all credential keys (without values).

        Returns:
            List of credential keys
        """
        data = self._load_store()
        return list(data.keys())

    def _load_store(self) -> Dict[str, Any]:
        """Load and decrypt credential store.

        Values are heterogeneous by design — new-format entries are metadata
        dicts (``{"value", "stored_at", "expires_at"}``) while legacy entries
        are bare strings — so the honest element type is ``Any``.
        """
        if not self.store_path.exists():
            return {}

        try:
            encrypted = self.store_path.read_bytes()
            decrypted = self.cipher.decrypt(encrypted)
            data: Dict[str, Any] = json.loads(decrypted)
            logger.debug(f"Loaded {len(data)} credentials from encrypted store")
            return data
        except InvalidToken as exc:
            # S-008: do NOT silently return {} here. The previous behavior
            # caused the next `_save_store` call to overwrite the ciphertext
            # with the *new* key, irreversibly destroying credentials that
            # were still recoverable with the old key. Raise instead so the
            # user can back up the ciphertext and investigate.
            from fluid_build.credentials.resolver import (  # noqa: PLC0415
                CredentialError,
            )

            logger.error("Encrypted credential store cannot be decrypted with the current key")
            raise CredentialError(
                "Encrypted credential store at "
                f"{self.store_path} cannot be decrypted with the current "
                f"key at {self.key_path}. This usually means the key was "
                "regenerated or the ciphertext was copied from another "
                "host. Back up the ciphertext before re-initializing.",
                suggestions=[
                    f"Back up: cp {self.store_path} {self.store_path}.bak",
                    f"Re-initialize: rm {self.store_path} (will lose stored credentials)",
                    "Restore the original key if you still have it",
                ],
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            # Narrow catch: I/O errors and plaintext-corruption shouldn't
            # masquerade as an empty store either. Re-raise as
            # CredentialError so callers see the real failure.
            from fluid_build.credentials.resolver import (  # noqa: PLC0415
                CredentialError,
            )

            logger.error("Failed to load encrypted store: %s", exc)
            raise CredentialError(
                f"Failed to read encrypted credential store at {self.store_path}: {exc}"
            ) from exc

    def _save_store(self, data: Dict[str, Any]) -> None:
        """Encrypt and save credential store."""
        try:
            _secure_parent_dir(self.store_path)
            serialized = json.dumps(data).encode()
            encrypted = self.cipher.encrypt(serialized)
            # Atomic write: temp file (0o600) → fsync → os.replace. A crash
            # mid-write can no longer leave a truncated ciphertext that
            # fails to decrypt, and there is no world-readable window.
            _atomic_write_bytes(self.store_path, encrypted)
            logger.debug(f"Saved {len(data)} credentials to encrypted store")
        except Exception as e:
            logger.error(f"Failed to save encrypted store: {e}")
            raise
