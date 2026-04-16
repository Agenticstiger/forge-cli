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
OS Keyring credential storage.

Securely stores credentials using the operating system's keyring:
- macOS: Keychain
- Windows: Credential Manager
- Linux: Secret Service (GNOME Keyring, KWallet)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError

    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    keyring = None


class KeyringCredentialStore:
    """Secure credential storage using OS keyring."""

    SERVICE_NAME = "fluid-cli"

    @staticmethod
    def set_credential(key: str, value: str) -> None:
        """
        Store credential securely in OS keyring.

        Args:
            key: Credential key (e.g., "snowflake.password")
            value: Credential value to store

        Raises:
            ImportError: If keyring library not available
            KeyringError: If keyring operation fails
        """
        if not KEYRING_AVAILABLE:
            raise ImportError(
                "keyring library required for OS keyring support. "
                "Install with: pip install keyring"
            )

        try:
            keyring.set_password(KeyringCredentialStore.SERVICE_NAME, key, value)
            logger.debug(f"Stored credential in keyring: {key}")
        except KeyringError as e:
            logger.error(f"Failed to store credential in keyring: {e}")
            raise

    @staticmethod
    def get_credential(key: str) -> Optional[str]:
        """
        Retrieve credential from OS keyring.

        Args:
            key: Credential key (e.g., "snowflake.password")

        Returns:
            Credential value or None if not found
        """
        if not KEYRING_AVAILABLE:
            logger.debug("keyring library not available")
            return None

        try:
            value = keyring.get_password(KeyringCredentialStore.SERVICE_NAME, key)
            if value:
                logger.debug(f"Retrieved credential from keyring: {key}")
            return value
        except KeyringError as e:
            logger.debug(f"Failed to retrieve credential from keyring: {e}")
            return None

    @staticmethod
    def delete_credential(key: str) -> None:
        """
        Remove credential from OS keyring.

        Args:
            key: Credential key to delete
        """
        if not KEYRING_AVAILABLE:
            raise ImportError(
                "keyring library required for OS keyring support. "
                "Install with: pip install keyring"
            )

        try:
            keyring.delete_password(KeyringCredentialStore.SERVICE_NAME, key)
            logger.debug(f"Deleted credential from keyring: {key}")
        except PasswordDeleteError:
            # Credential doesn't exist, that's fine
            logger.debug(f"Credential not found in keyring: {key}")
        except KeyringError as e:
            logger.error(f"Failed to delete credential from keyring: {e}")
            raise

    @staticmethod
    def list_credentials() -> list:
        """
        List all credentials stored for fluid-cli.

        Note: Not all keyring backends support listing credentials.
        Returns empty list if backend doesn't support it.

        Returns:
            List of credential keys (without values)
        """
        # Most keyring backends don't support listing
        # This is a limitation of the keyring library
        logger.warning("Keyring backend may not support listing credentials")
        return []
