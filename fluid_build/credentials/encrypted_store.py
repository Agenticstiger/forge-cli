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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    Fernet = None


def _secure_parent_dir(path: Path) -> None:
    """Create ``path.parent`` with mode 0o700.

    ``mkdir`` honors the process umask (typically 0o022 → 0o755), which
    on shared hosts leaks existence / timestamps of the credentials
    directory to other local accounts. We tighten to 0o700 after the
    fact. ``chmod`` is best-effort: on Windows / restricted filesystems
    the call may fail, which we tolerate silently.

    See SECURITY_REVIEW S-009.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except (NotImplementedError, PermissionError, OSError):
        # Windows / restricted FS — chmod is best-effort.
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

    def _ensure_key(self):
        """Create or load encryption key."""
        if not self.key_path.exists():
            # Create new encryption key. Parent dir is chmod'd 0o700 to
            # prevent other local accounts from enumerating the store —
            # see SECURITY_REVIEW S-009.
            _secure_parent_dir(self.key_path)
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            self.key_path.chmod(0o600)  # Owner read/write only
            logger.info(f"Generated new encryption key: {self.key_path}")

        # Load key
        self.key = self.key_path.read_bytes()
        self.cipher = Fernet(self.key)
        logger.debug(f"Loaded encryption key from: {self.key_path}")

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
            logger.debug(f"Retrieved encrypted credential: {key}")
        return value

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

    def list_credentials(self) -> list:
        """
        List all credential keys (without values).

        Returns:
            List of credential keys
        """
        data = self._load_store()
        return list(data.keys())

    def _load_store(self) -> Dict[str, str]:
        """Load and decrypt credential store."""
        if not self.store_path.exists():
            return {}

        try:
            encrypted = self.store_path.read_bytes()
            decrypted = self.cipher.decrypt(encrypted)
            data = json.loads(decrypted)
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

    def _save_store(self, data: Dict[str, str]) -> None:
        """Encrypt and save credential store."""
        try:
            _secure_parent_dir(self.store_path)
            serialized = json.dumps(data).encode()
            encrypted = self.cipher.encrypt(serialized)
            self.store_path.write_bytes(encrypted)
            self.store_path.chmod(0o600)  # Owner read/write only
            logger.debug(f"Saved {len(data)} credentials to encrypted store")
        except Exception as e:
            logger.error(f"Failed to save encrypted store: {e}")
            raise
