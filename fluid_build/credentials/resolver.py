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
Base credential resolver with multi-source fallback support.
"""

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CredentialSource(Enum):
    """Credential source priority order."""

    CLI_ARGUMENT = 1  # Highest priority
    ENVIRONMENT = 2  # Environment variables
    DOTENV = 3  # .env files
    KEYRING = 4  # OS keyring
    ENCRYPTED_FILE = 5  # Encrypted local file
    CONFIG_FILE = 6  # Config file
    VAULT = 7  # HashiCorp Vault
    SECRET_MANAGER = 8  # Cloud secret managers
    PROVIDER_DEFAULT = 9  # Provider-specific defaults (e.g., ADC for GCP)
    PROMPT = 10  # Interactive prompt (lowest priority)


@dataclass
class CredentialConfig:
    """Configuration for credential resolution.

    Fields:
        allow_prompt: Whether to prompt user interactively for missing creds.
        cache_duration_seconds: In-memory cache TTL.
        required_sources: Restrict resolution to specific sources.
        project_root: Project root for locating .env files.
        environment: Named environment (dev, staging, prod).
        on_keyring_save: Optional callback invoked after a credential is
            successfully saved to the OS keyring. Used to surface a
            user-visible confirmation without the resolver importing any
            CLI/UI module (see CODE_REVIEW C-007 — layering inversion).
            Receives a short human-readable message as its only argument.
    """

    allow_prompt: bool = False
    cache_duration_seconds: int = 3600
    required_sources: Optional[List[CredentialSource]] = None
    project_root: Optional[str] = None
    environment: str = "dev"  # dev, staging, prod
    on_keyring_save: Optional[Callable[[str], None]] = field(default=None, repr=False)


class CredentialError(Exception):
    """Raised when required credential cannot be found."""

    def __init__(self, message: str, suggestions: List[str] = None):
        super().__init__(message)
        self.suggestions = suggestions or []


class BaseCredentialResolver(ABC):
    """
    Base credential resolver with common resolution logic.

    Implements a priority-based credential resolution chain that tries
    multiple sources in order until a credential is found.
    """

    def __init__(self, provider: str, config: Optional[CredentialConfig] = None):
        """
        Initialize credential resolver.

        Args:
            provider: Provider name (e.g., "snowflake", "gcp", "aws")
            config: Optional configuration
        """
        self.provider = provider
        self.config = config or CredentialConfig()
        self._cache: Dict[str, Any] = {}

        logger.debug(f"Initialized {provider} credential resolver")

    def get_credential(
        self, key: str, required: bool = True, cli_value: Optional[str] = None, **kwargs
    ) -> Optional[str]:
        """
        Resolve credential using priority chain.

        Resolution order:
        1. CLI argument (explicit override)
        2. Environment variable (current session)
        3. .env file (project-specific)
        4. OS Keyring (secure local storage)
        5. Encrypted file (~/.fluid/credentials.enc)
        6. Config file (~/.fluidrc.yaml)
        7. Vault (HashiCorp Vault)
        8. Secret Manager (GCP/AWS/Azure)
        9. Provider-specific default (e.g., ADC for GCP)
        10. Interactive prompt (if allowed)

        Args:
            key: Credential key (e.g., "password", "account")
            required: Whether credential is required
            cli_value: Value from CLI argument (highest priority)
            **kwargs: Additional provider-specific parameters

        Returns:
            Credential value or None if not required and not found

        Raises:
            CredentialError: If required credential not found
        """
        # Check cache first
        cache_key = f"{self.provider}.{key}"
        if cache_key in self._cache:
            logger.debug(f"Credential '{key}' retrieved from cache")
            return self._cache[cache_key]

        # Try each source in priority order
        value = None

        # 1. CLI argument (highest priority)
        if cli_value is not None:
            value = cli_value
            logger.debug(f"Credential '{key}' from CLI argument")

        # 2. Environment variable
        if value is None:
            value = self._get_from_env(key)
            if value:
                logger.debug(f"Credential '{key}' from environment variable")

        # 3. .env file
        if value is None:
            value = self._get_from_dotenv(key)
            if value:
                logger.debug(f"Credential '{key}' from .env file")

        # 4. OS Keyring
        if value is None:
            value = self._get_from_keyring(key)
            if value:
                logger.debug(f"Credential '{key}' from OS keyring")

        # 5. Encrypted file
        if value is None:
            value = self._get_from_encrypted_file(key)
            if value:
                logger.debug(f"Credential '{key}' from encrypted file")

        # 6. Config file
        if value is None:
            value = self._get_from_config(key)
            if value:
                logger.debug(f"Credential '{key}' from config file")

        # 7. Vault
        if value is None:
            value = self._get_from_vault(key)
            if value:
                logger.debug(f"Credential '{key}' from Vault")

        # 8. Secret Manager
        if value is None:
            value = self._get_from_secret_manager(key)
            if value:
                logger.debug(f"Credential '{key}' from secret manager")

        # 9. Provider-specific default
        if value is None:
            value = self._get_provider_default(key, **kwargs)
            if value:
                logger.debug(f"Credential '{key}' from provider default")

        # 10. Interactive prompt (lowest priority)
        if value is None and self.config.allow_prompt and required:
            value = self._get_from_prompt(key)
            if value:
                logger.debug(f"Credential '{key}' from interactive prompt")

        # Handle not found
        if value is None and required:
            suggestions = self._get_suggestions(key)
            raise CredentialError(
                f"Required credential not found: {self.provider}.{key}", suggestions=suggestions
            )

        # Cache the result
        if value is not None:
            self._cache[cache_key] = value

        return value

    def _get_from_env(self, key: str) -> Optional[str]:
        """Get credential from environment variable."""
        env_keys = [
            f"{self.provider.upper()}_{key.upper()}",
            f"{self.provider.upper()}__{key.upper()}",
            key.upper(),
        ]

        for env_key in env_keys:
            value = os.environ.get(env_key)
            if value:
                return value

        return None

    def _get_from_dotenv(self, key: str) -> Optional[str]:
        """Get credential from .env file.

        C-009: narrowed catch list. ``ImportError`` covers the optional
        ``python-dotenv`` dependency. ``OSError`` covers I/O (missing
        read permission, path too long). ``ValueError`` covers
        ``python-dotenv``'s own parser complaints. Unknown errors
        surface at ``warning`` and still return ``None`` so a single
        source failure doesn't collapse the fallback chain.
        """
        try:
            from .dotenv_store import DotEnvCredentialStore
        except ImportError:
            logger.debug("python-dotenv not available, skipping .env file")
            return None

        try:
            store = DotEnvCredentialStore(
                project_root=self.config.project_root, environment=self.config.environment
            )

            # Try provider-prefixed key first, then fallback to plain key
            env_keys = [f"{self.provider.upper()}_{key.upper()}", key.upper()]

            for env_key in env_keys:
                value = store.get_credential(env_key)
                if value:
                    return value

            return None
        except ImportError as e:
            logger.debug("python-dotenv not installed; skipping .env file (%s)", e)
            return None
        except (OSError, ValueError) as e:
            logger.debug("Failed to read from .env file: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading .env file: %s", e)
            return None

    def _get_from_keyring(self, key: str) -> Optional[str]:
        """Get credential from OS keyring.

        C-009: narrowed to ``KeyringError`` (covers ``NoKeyringError``,
        ``PasswordDeleteError``, etc. — all subclass ``KeyringError``).
        Unknown errors log at ``warning`` but still fall through.
        """
        try:
            from .keyring_store import KeyringCredentialStore
        except ImportError:
            logger.debug("keyring library not available, skipping OS keyring")
            return None

        try:
            from keyring.errors import KeyringError
        except ImportError:
            KeyringError = Exception  # type: ignore[assignment,misc]

        keyring_key = f"{self.provider}.{key}"
        try:
            return KeyringCredentialStore.get_credential(keyring_key)
        except KeyringError as e:
            logger.debug("Failed to read from keyring: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading from keyring: %s", e)
            return None

    def _get_from_encrypted_file(self, key: str) -> Optional[str]:
        """Get credential from encrypted file.

        C-009: narrowed exception surface. ``CredentialError`` is what
        ``EncryptedCredentialStore._load_store`` raises on an
        ``InvalidToken`` (see S-008 / ``encrypted_store.py``) — this is
        a hard failure we deliberately re-raise so the user is told the
        store is unreadable rather than silently falling through (which
        would mask a ciphertext/key mismatch). ``OSError`` is I/O.
        """
        try:
            from .encrypted_store import EncryptedCredentialStore
        except ImportError:
            logger.debug("cryptography library not available, skipping encrypted file")
            return None

        keyring_key = f"{self.provider}.{key}"
        try:
            store = EncryptedCredentialStore()
            return store.get_credential(keyring_key)
        except CredentialError:
            # Key mismatch / corruption — surface to caller; silent
            # fall-through is the bug S-008 explicitly fixed.
            raise
        except ImportError as e:
            logger.debug("cryptography not installed; skipping encrypted file (%s)", e)
            return None
        except OSError as e:
            logger.debug("Failed to read encrypted credential file: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading encrypted file: %s", e)
            return None

    def _get_from_config(self, key: str) -> Optional[str]:
        """Get credential from config file (~/.fluidrc.yaml or ~/.fluid/config.yaml)."""
        try:
            import yaml as _yaml
        except ImportError:
            return None

        config_paths = [
            os.path.expanduser("~/.fluidrc.yaml"),
            os.path.expanduser("~/.fluid/config.yaml"),
        ]

        for config_path in config_paths:
            if not os.path.exists(config_path):
                continue
            try:
                with open(config_path) as f:
                    config = _yaml.safe_load(f) or {}
                # Try provider-scoped key first, then global
                provider_section = config.get(self.provider, {})
                if isinstance(provider_section, dict) and key in provider_section:
                    return str(provider_section[key])
                if key in config:
                    return str(config[key])
            except Exception as e:
                logger.warning(f"Failed to parse config file {config_path}: {e}")

        return None

    def _get_from_vault(self, key: str) -> Optional[str]:
        """Get credential from HashiCorp Vault.

        C-009: narrowed to the vendor/domain errors we expect — the
        ``secrets`` module raises ``ConfigurationError`` /
        ``AuthenticationError`` for misconfig; ``ImportError`` covers
        the optional ``hvac`` dep; ``OSError`` covers network I/O.
        Unknown errors log at ``warning`` so operational problems
        (transient 5xx, DNS) become visible in logs instead of
        disappearing silently.
        """
        try:
            from ..errors import AuthenticationError, ConfigurationError
            from ..secrets import get_secret
        except ImportError:
            logger.debug("secrets backend unavailable, skipping Vault")
            return None

        secret_name = f"{self.provider}/{key}"
        try:
            return get_secret(secret_name, required=False)
        except (ConfigurationError, AuthenticationError, OSError) as e:
            logger.debug("Failed to read from Vault: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading from Vault: %s", e)
            return None

    def _get_from_secret_manager(self, key: str) -> Optional[str]:
        """Get credential from cloud secret manager (GCP/AWS/Azure).

        C-009: same narrowing rationale as ``_get_from_vault``.
        """
        try:
            from ..errors import AuthenticationError, ConfigurationError
            from ..secrets import get_secret_manager
        except ImportError:
            logger.debug("secrets backend unavailable, skipping secret manager")
            return None

        secret_name = f"{self.provider}/{key}"
        try:
            manager = get_secret_manager()
            if manager is None:
                return None
            return manager.get_secret(secret_name, required=False)
        except (ConfigurationError, AuthenticationError, OSError) as e:
            logger.debug("Failed to read from secret manager: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading from secret manager: %s", e)
            return None

    def _get_from_prompt(self, key: str) -> Optional[str]:
        """Get credential from interactive prompt."""
        try:
            from getpass import getpass

            prompt = f"Enter {self.provider} {key}: "
            # Use no-echo input (getpass) for any credential-shaped key name.
            # Biased toward getpass: credential identifiers rarely benefit
            # from visible input, and the prior 3-substring check missed
            # ``access_key``, ``private_key``, ``passphrase``, ``api_key``,
            # ``credential`` and similar. See SECURITY_REVIEW S-012.
            if re.search(
                r"(?i)pass(?:word|phrase)?|secret|token|key|credential|auth",
                key,
            ):
                value = getpass(prompt)
            else:
                value = input(prompt)

            # Ask if user wants to save
            if value and self._confirm_save(key):
                self._save_to_keyring(key, value)

            return value

        except (KeyboardInterrupt, EOFError) as e:
            # C-009: user aborted the prompt — treat as "no credential"
            # without surfacing a confusing traceback.
            logger.debug("Credential prompt cancelled: %s", type(e).__name__)
            return None
        except OSError as e:
            # No TTY / closed stdin.
            logger.debug("Failed to prompt for credential: %s", e)
            return None

    def _confirm_save(self, key: str) -> bool:
        """Ask user if they want to save credential to keyring.

        C-009: only silence ``KeyboardInterrupt`` / ``EOFError`` /
        ``OSError`` — the expected set when there's no TTY or the user
        hits ctrl-c. Any other exception should propagate.
        """
        try:
            response = input(
                f"Save {self.provider} {key} to secure keyring for future use? (y/n): "
            )
            return response.lower() in ("y", "yes")
        except (KeyboardInterrupt, EOFError, OSError):
            return False

    def _save_to_keyring(self, key: str, value: str):
        """Save credential to OS keyring.

        C-007: the resolver must not import from ``fluid_build.cli.*``
        (that's a layering inversion — ``credentials`` is upstream of
        ``cli``). Surface the "saved" notification via an optional
        callback on ``CredentialConfig.on_keyring_save``; otherwise log
        at ``info`` level.

        C-009: narrowed to ``ImportError`` (optional dep) and
        ``KeyringError`` (the common runtime failure). Unknown errors
        log at ``warning`` to remain visible.
        """
        try:
            from .keyring_store import KeyringCredentialStore
        except ImportError as e:
            logger.warning("Cannot save to keyring — library unavailable: %s", e)
            return

        try:
            from keyring.errors import KeyringError
        except ImportError:
            KeyringError = Exception  # type: ignore[assignment,misc]

        keyring_key = f"{self.provider}.{key}"
        try:
            KeyringCredentialStore.set_credential(keyring_key, value)
        except KeyringError as e:
            logger.warning("Failed to save to keyring: %s", e)
            return
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error saving to keyring: %s", e)
            return

        callback = self.config.on_keyring_save
        if callback is not None:
            # User-facing notification — includes the credential key name
            # so operators can confirm which slot was written.
            message = f"Saved {self.provider} {key} to secure keyring"
            try:
                callback(message)
            except Exception as e:  # pragma: no cover - defensive
                # The callback is user-facing; never let it break the
                # credential-save path.
                logger.debug("on_keyring_save callback raised: %s", e)
        else:
            # Static operator log — do NOT interpolate the credential
            # ``key`` name here. CodeQL's ``py/clear-text-logging-sensitive-data``
            # rule flags any f-string in this file that references ``key``
            # because it conflates the credential *name* (e.g. the literal
            # "password") with the credential *value*. The value is never
            # logged; the provider label alone is enough for observability.
            logger.debug("Credential stored in keyring (provider=%s)", self.provider)

    @abstractmethod
    def _get_provider_default(self, key: str, **kwargs) -> Optional[str]:
        """
        Provider-specific credential retrieval.

        Override this in subclasses to implement provider-specific
        credential resolution (e.g., ADC for GCP, IAM roles for AWS).
        """
        pass

    def _get_suggestions(self, key: str) -> List[str]:
        """Get suggestions for finding credential."""
        return [
            f"Set environment variable: {self.provider.upper()}_{key.upper()}",
            f"Store in keyring: fluid auth set --provider {self.provider} --key {key}",
            f"Create .env file with: {self.provider.upper()}_{key.upper()}=your_value",
            f"Use CLI argument: --{key.replace('_', '-')}",
        ]

    def clear_cache(self):
        """Clear credential cache."""
        self._cache.clear()
        logger.debug(f"Cleared credential cache for {self.provider}")
