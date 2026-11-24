# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Base credential resolver with multi-source fallback support.
"""

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional

from fluid_build._errors import FluidUserError, doc_url

logger = logging.getLogger(__name__)


class CredentialSource(Enum):
    """Credential source priority order."""

    CLI_ARGUMENT = 1  # Highest priority
    ENVIRONMENT = 2  # Environment variables
    DOTENV = 3  # .env files
    KEYRING = 4  # OS keyring
    SECRET_STORE = 5  # The operator's explicit store (FLUID_CREDENTIAL_STORE)
    PROVIDER_DEFAULT = 6  # Provider-specific defaults (e.g., ADC for GCP)
    PROMPT = 7  # Interactive prompt (lowest priority)


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


#: Snowflake settings only the operator sets: who signs in and how. A contract is
#: project content, so a value written there would sign the operator in to the
#: account the contract names with the operator's identity or login.
SNOWFLAKE_OPERATOR_ONLY_FIELDS = ("authenticator", "user")


@dataclass
class ContractLoginSettingError(FluidUserError, ValueError):
    """A contract sets the Snowflake user or authenticator.

    A ``ValueError`` as well, so the callers that already treat a bad Snowflake
    setting as one (the connection builders) see it the same way. Names the
    field's path in the contract, never a value.
    """

    code: str = "ContractLoginSettingError"

    def __str__(self) -> str:
        return f"{self.what}. {self.why} Fix: {self.fix}"


def refuse_contract_login_settings(section: Any, path: str) -> None:
    """Raise :class:`ContractLoginSettingError` when ``section`` sets a Snowflake
    ``user`` or ``authenticator``.

    ``section`` is a ``location``, ``properties`` or ``resources`` mapping of the
    contract and ``path`` is where it sits (``exposes[0].binding.properties``).
    Anything that is not a mapping, and a blank value, is left alone.
    """
    if not isinstance(section, Mapping):
        return
    for name in SNOWFLAKE_OPERATOR_ONLY_FIELDS:
        value = section.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        variable = f"SNOWFLAKE_{name.upper()}"
        raise ContractLoginSettingError(
            what=f"The contract sets the Snowflake {name} at {path}.{name}",
            why=(
                f"Who signs in to Snowflake, and how, is the operator's to choose. A contract "
                f"that names its own {name} (it may also name the account) would sign the "
                "operator in to that account with the operator's own identity or login."
            ),
            fix=(
                f"Remove {path}.{name} from the contract and set {variable} in the "
                "environment of the process that runs fluid."
            ),
            doc=doc_url("secrets"),
            where=f"{path}.{name}",
            extras={"field": name, "path": f"{path}.{name}"},
        )


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
        # Cache entries are ``(value, monotonic_expiry)`` tuples so the
        # advertised ``cache_duration_seconds`` TTL is actually honored —
        # a long-running process must not hold a decrypted credential in
        # memory indefinitely.
        self._cache: Dict[str, Any] = {}

        logger.debug(f"Initialized {provider} credential resolver")

    def get_credential(
        self,
        key: str,
        required: bool = True,
        cli_value: Optional[str] = None,
        *,
        pin_to_project: bool = False,
        **kwargs,
    ) -> Optional[str]:
        """
        Resolve credential using priority chain.

        Resolution order:
        1. CLI argument (explicit override)
        2. Environment variable (current session)
        3. .env file (project-specific)
        4. OS Keyring (secure local storage)
        5. The operator's secret store, only when FLUID_CREDENTIAL_STORE names
           one (vault://<mount>/<prefix> or aws-sm://<prefix>; never chosen by
           ambient variables such as AWS_REGION or GCP_PROJECT)
        6. Provider-specific default (e.g., ADC for GCP)
        7. Interactive prompt (if allowed)

        The Fernet file store (``~/.fluid/credentials.enc``) and plaintext
        credentials in ``~/.fluidrc.yaml`` were retired on 2026-11-15: keep a
        credential in the keyring, a secret store, or the environment.

        Args:
            key: Credential key (e.g., "password", "account")
            required: Whether credential is required
            cli_value: Value from CLI argument (highest priority)
            pin_to_project: Read only the CLI argument, the environment and the
                project's files, as for an endpoint the project's files chose
                (see :meth:`_project_endpoint_in_effect`). For a caller whose
                endpoint came from the contract and that the operator did not
                choose: no keyring, no secret store, no provider default, no
                prompt and no cache.
            **kwargs: Additional provider-specific parameters

        Returns:
            Credential value or None if not required and not found

        Raises:
            CredentialError: If required credential not found
        """
        # A provider's endpoint chosen by the project's dotenv files gets only
        # the project's credentials: the CLI argument, the environment and
        # those files. Nothing from the operator's stores below, and nothing
        # cached, which may have come from them.
        pinned = pin_to_project or self._project_endpoint_in_effect()

        # Check cache first — honor the configured TTL.
        cache_key = f"{self.provider}.{key}"
        cached = None if pinned else self._cache.get(cache_key)
        if cached is not None:
            cached_value, expires_at = cached
            if time.monotonic() < expires_at:
                # Constant-only log: ``cached_value`` is in scope.
                logger.debug("Credential retrieved from cache")
                return cached_value
            # TTL elapsed — drop the stale entry and re-resolve.
            del self._cache[cache_key]

        # Try each source in priority order
        value = None

        # 1. CLI argument (highest priority)
        # CodeQL py/clear-text-logging-sensitive-data: ``value`` is in
        # scope at every LOG site below. Don't interpolate ``key`` —
        # log only the resolution source so operators can trace the
        # ladder without leaking credential identifiers / values.
        if cli_value is not None:
            value = cli_value
            logger.debug("Credential resolved from: CLI argument")

        # 2. Environment variable
        if value is None:
            value = self._get_from_env(key)
            if value:
                logger.debug("Credential resolved from: environment variable")

        # 3. .env file
        if value is None:
            value = self._get_from_dotenv(key)
            if value:
                logger.debug("Credential resolved from: .env file")

        # Reading the project's files may have just loaded its endpoint.
        pinned = pinned or self._project_endpoint_in_effect()
        if value is None and pinned:
            logger.debug("Credential sources outside the environment skipped (project endpoint)")

        # 4. OS Keyring
        if value is None and not pinned:
            value = self._get_from_keyring(key)
            if value:
                logger.debug("Credential resolved from: OS keyring")

        # 5. The operator's secret store, when one is named
        if value is None and not pinned:
            value = self._get_from_secret_store(key)
            if value:
                logger.debug("Credential resolved from: secret store")

        # 6. Provider-specific default
        if value is None and not pinned:
            value = self._get_provider_default(key, **kwargs)
            if value:
                logger.debug("Credential resolved from: provider default")

        # 7. Interactive prompt (lowest priority)
        if value is None and self.config.allow_prompt and required and not pinned:
            value = self._get_from_prompt(key)
            if value:
                logger.debug("Credential resolved from: interactive prompt")

        # Handle not found
        if value is None and required:
            suggestions = self._get_suggestions(key)
            raise CredentialError(
                f"Required credential not found: {self.provider}.{key}", suggestions=suggestions
            )

        # Cache the result with a monotonic expiry stamp.
        if value is not None and not pinned:
            self._cache[cache_key] = (
                value,
                time.monotonic() + self.config.cache_duration_seconds,
            )

        return value

    def _project_endpoint_in_effect(self) -> bool:
        """Whether this provider's endpoint in effect came from project dotenv files.

        Then ``get_credential`` reads only the CLI argument, the environment
        and the project's files: the project files paired that endpoint with
        a credential of their own (``credentials.protected_env``), and the
        operator's keyring or secret store
        must not supply one for a host the project chose. Providers whose
        endpoints are listed there override this.
        """
        return False

    def _env_names(self, key: str) -> List[str]:
        """The environment variables ``key`` is read from, in order."""
        return [
            f"{self.provider.upper()}_{key.upper()}",
            f"{self.provider.upper()}__{key.upper()}",
            key.upper(),
        ]

    def _dotenv_names(self, key: str) -> List[str]:
        """The names ``key`` is read under from the project's .env files, in order."""
        return [f"{self.provider.upper()}_{key.upper()}", key.upper()]

    def _get_from_env(self, key: str) -> Optional[str]:
        """Get credential from environment variable."""
        for env_key in self._env_names(key):
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
            for env_key in self._dotenv_names(key):
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
        except KeyringError:
            # Never interpolate the exception — keyring backend errors can
            # echo credential material. The first-failure WARNING is
            # emitted inside ``KeyringCredentialStore.get_credential``.
            logger.debug("Failed to read from keyring (backend error)")
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Unexpected error reading from keyring: %s", e)
            return None

    def _get_from_secret_store(self, key: str) -> Optional[str]:
        """Get a credential from the store the operator named, through the secret registry.

        ``FLUID_CREDENTIAL_STORE`` (operator-only: ``credentials/protected_env``)
        is a reference prefix such as ``vault://kv/fluid/credentials`` or
        ``aws-sm://fluid/credentials``; the credential is read from
        ``<prefix>/<provider>#<key>``: one secret per provider, one field per
        key. Unset means no store is asked. The store used to be picked from
        ambient variables (GCP Secret Manager when ``GCP_PROJECT`` was set, else
        AWS Secrets Manager when ``AWS_REGION`` was), so setting a region
        silently sent every missing credential lookup to Secrets Manager.

        A secret that is not there falls through to the next source. Any other
        failure (a store that is unreachable, a denied read, a reference the
        operator's policy refuses) raises :class:`CredentialError`: falling
        through would hide a broken store behind a provider default or a prompt.
        """
        store = os.environ.get("FLUID_CREDENTIAL_STORE", "").strip()
        if not store:
            return None
        scheme = store.partition("://")[0].strip().lower()
        if scheme not in ("vault", "openbao", "aws-sm", "aws") or "://" not in store:
            raise CredentialError(
                "FLUID_CREDENTIAL_STORE must name a store whose secrets hold fields, "
                "such as vault://<mount>/<prefix> or aws-sm://<prefix>",
                suggestions=["export FLUID_CREDENTIAL_STORE=vault://kv/fluid/credentials"],
            )
        from ..secrets import SecretError, resolve_credential

        reference = f"{store.rstrip('/')}/{self.provider}#{key}"
        try:
            secret = resolve_credential(reference)
        except SecretError as exc:
            if exc.extras.get("reason") == "not_found":
                return None
            raise CredentialError(
                f"{exc.what}: {exc.why}", suggestions=[exc.fix] if exc.fix else []
            ) from None
        value: str = secret.get_secret_value()
        return value

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
