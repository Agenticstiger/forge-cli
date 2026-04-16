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

# fluid_build/providers/snowflake/credentials.py
"""
Snowflake credential management with secure multi-source resolution.

Integrates with UnifiedCredentialResolver to support:
- OS Keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service)
- Encrypted file storage
- Environment variables
- Cloud Secret Managers (GCP/AWS/Azure)
- Interactive prompts

Priority order:
1. CLI arguments (highest priority)
2. Environment variables
3. OS Keyring
4. Encrypted file (~/.fluid/credentials.enc)
5. Cloud Secret Manager
6. Interactive prompt (if allowed)
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fluid_build.cli.console import success, warning


@dataclass
class SnowflakeCredentials:
    """Snowflake connection credentials."""

    account: str
    user: Optional[str] = None
    password: Optional[str] = None
    private_key: Optional[str] = None
    private_key_passphrase: Optional[str] = None
    authenticator: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    role: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Snowflake connector."""
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def is_complete(self) -> bool:
        """Check if credentials are complete for connection."""
        # Need account + one auth method
        if not self.account:
            return False

        # Check authentication methods
        has_password_auth = self.user and self.password
        has_key_auth = self.user and self.private_key
        has_external_auth = self.authenticator and self.authenticator != "SNOWFLAKE"

        return has_password_auth or has_key_auth or has_external_auth


class SnowflakeCredentialAdapter:
    """
    Adapter for resolving Snowflake credentials from multiple sources.

    Supports all Snowflake authentication methods:
    - Username/password
    - Key-pair authentication
    - External OAuth
    - SSO/SAML
    """

    PROVIDER = "snowflake"

    # Credential key mappings
    CREDENTIAL_KEYS = [
        "account",
        "user",
        "password",
        "private_key",
        "private_key_passphrase",
        "authenticator",
        "warehouse",
        "database",
        "schema",
        "role",
    ]

    def __init__(self, allow_prompt: bool = False):
        """
        Initialize credential adapter.

        Args:
            allow_prompt: Whether to prompt user for missing credentials
        """
        self.allow_prompt = allow_prompt
        self._resolver = None

    def _get_resolver(self):
        """Lazy-load credential resolver."""
        if self._resolver is None:
            try:
                from fluid_build.credentials.resolver import (
                    CredentialConfig,
                    UnifiedCredentialResolver,
                )

                config = CredentialConfig(allow_prompt=self.allow_prompt)
                self._resolver = UnifiedCredentialResolver(config)
            except ImportError:
                # Fallback to environment-only resolver
                self._resolver = _EnvironmentOnlyResolver()

        return self._resolver

    def resolve_credentials(
        self, cli_args: Optional[Dict[str, Any]] = None, required: bool = True
    ) -> SnowflakeCredentials:
        """
        Resolve Snowflake credentials from multiple sources.

        Args:
            cli_args: Optional CLI arguments (highest priority)
            required: Whether to raise error if credentials incomplete

        Returns:
            SnowflakeCredentials object

        Raises:
            CredentialError: If required credentials not found
        """
        cli_args = cli_args or {}
        resolver = self._get_resolver()

        # Resolve each credential
        creds = {}
        for key in self.CREDENTIAL_KEYS:
            cli_value = cli_args.get(key)

            # Account is always required
            key_required = key == "account"

            value = resolver.get_credential(
                key=key, provider=self.PROVIDER, required=key_required, cli_value=cli_value
            )

            if value:
                creds[key] = value

        credentials = SnowflakeCredentials(**creds)

        # Validate completeness
        if required and not credentials.is_complete():
            raise CredentialError(
                "Incomplete Snowflake credentials",
                details={
                    "account": credentials.account,
                    "user": "✓" if credentials.user else "✗",
                    "password": "✓" if credentials.password else "✗",
                    "private_key": "✓" if credentials.private_key else "✗",
                    "authenticator": credentials.authenticator or "default",
                },
                suggestions=[
                    "Provide username and password",
                    "Or configure key-pair authentication",
                    "Or use external authenticator (SSO)",
                    "",
                    "Examples:",
                    "  fluid auth set snowflake --user myuser --password",
                    "  export SNOWFLAKE_USER=myuser SNOWFLAKE_PASSWORD=secret",
                    "  fluid apply contract.yaml --user myuser --password secret",
                ],
            )

        return credentials

    def validate_credentials(self, credentials: SnowflakeCredentials) -> bool:
        """
        Validate credentials by attempting connection.

        Args:
            credentials: Credentials to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            import snowflake.connector

            # Attempt connection
            conn = snowflake.connector.connect(**credentials.to_dict())

            # Test with simple query
            cursor = conn.cursor()
            cursor.execute("SELECT CURRENT_VERSION()")
            cursor.fetchone()[0]
            cursor.close()
            conn.close()

            return True
        except Exception:
            return False

    def store_credentials(self, credentials: SnowflakeCredentials):
        """
        Store credentials securely in OS keyring.

        Args:
            credentials: Credentials to store
        """
        try:
            from fluid_build.credentials.keyring_store import KeyringCredentialStore

            for key in self.CREDENTIAL_KEYS:
                value = getattr(credentials, key, None)
                if value:
                    keyring_key = f"{self.PROVIDER}.{key}"
                    KeyringCredentialStore.set_credential(keyring_key, str(value))

            success("Snowflake credentials saved securely to OS keyring")
        except ImportError:
            # Try encrypted file storage
            try:
                from fluid_build.credentials.encrypted_store import EncryptedCredentialStore

                store = EncryptedCredentialStore()

                for key in self.CREDENTIAL_KEYS:
                    value = getattr(credentials, key, None)
                    if value:
                        store_key = f"{self.PROVIDER}.{key}"
                        store.set_credential(store_key, str(value))

                success("Snowflake credentials saved to encrypted file")
            except ImportError:
                warning("Could not save credentials - keyring and cryptography not available")


class CredentialError(Exception):
    """Credential resolution or validation error."""

    def __init__(self, message: str, details: Dict = None, suggestions: list = None):
        super().__init__(message)
        self.details = details or {}
        self.suggestions = suggestions or []

    def __str__(self):
        msg = super().__str__()

        if self.details:
            msg += "\n\nDetails:"
            for key, value in self.details.items():
                msg += f"\n  {key}: {value}"

        if self.suggestions:
            msg += "\n\nSuggestions:"
            for suggestion in self.suggestions:
                if suggestion:
                    msg += f"\n  {suggestion}"
                else:
                    msg += "\n"

        return msg


class _EnvironmentOnlyResolver:
    """Fallback resolver that only uses environment variables."""

    def get_credential(
        self, key: str, provider: str, required: bool = False, cli_value: Optional[str] = None
    ) -> Optional[str]:
        """Get credential from environment variables only."""
        if cli_value:
            return cli_value

        # Try various environment variable formats
        env_keys = [
            f"{provider.upper()}_{key.upper()}",
            f"{provider.upper()}__{key.upper()}",
            key.upper(),
        ]

        for env_key in env_keys:
            value = os.environ.get(env_key)
            if value:
                return value

        if required:
            raise CredentialError(
                f"Required credential not found: {provider}.{key}",
                suggestions=[
                    f"Set environment variable: {provider.upper()}_{key.upper()}=value",
                    f"Or provide via CLI: --{key.replace('_', '-')} value",
                ],
            )

        return None


def get_snowflake_credentials(
    cli_args: Optional[Dict[str, Any]] = None, allow_prompt: bool = False, validate: bool = False
) -> SnowflakeCredentials:
    """
    Convenience function to resolve Snowflake credentials.

    Args:
        cli_args: Optional CLI arguments
        allow_prompt: Whether to prompt for missing credentials
        validate: Whether to validate by connecting

    Returns:
        SnowflakeCredentials object

    Example:
        >>> creds = get_snowflake_credentials({"account": "abc123"})
        >>> conn = snowflake.connector.connect(**creds.to_dict())
    """
    adapter = SnowflakeCredentialAdapter(allow_prompt=allow_prompt)
    credentials = adapter.resolve_credentials(cli_args)

    if validate:
        if not adapter.validate_credentials(credentials):
            raise CredentialError(
                "Snowflake credentials validation failed",
                suggestions=[
                    "Check that account, user, and password are correct",
                    "Verify network connectivity to Snowflake",
                    "Test credentials: fluid auth test snowflake",
                ],
            )

    return credentials
