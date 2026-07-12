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
FLUID Build - Secrets Management

Provides secure secret retrieval from multiple sources:
- Environment variables
- GCP Secret Manager
- AWS Secrets Manager
- Azure Key Vault
- HashiCorp Vault
- Local encrypted files

Secrets are cached in memory for the duration of the process.
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .errors import AuthenticationError, ConfigurationError

logger = logging.getLogger(__name__)


class SecretSource(Enum):
    """Secret source types"""

    ENV = "environment"
    GCP_SECRET_MANAGER = "gcp_secret_manager"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    AZURE_KEY_VAULT = "azure_key_vault"
    HASHICORP_VAULT = "hashicorp_vault"
    LOCAL_FILE = "local_file"


@dataclass
class SecretConfig:
    """Configuration for secret retrieval"""

    source: SecretSource
    project_id: Optional[str] = None  # For GCP
    region: Optional[str] = None  # For AWS
    vault_url: Optional[str] = None  # For Vault/Azure
    vault_path: Optional[str] = None  # For Vault


class SecretManager:
    """
    Unified interface for retrieving secrets from multiple sources.

    Secrets are cached in memory to avoid repeated API calls.
    """

    def __init__(self, config: Optional[SecretConfig] = None):
        """
        Initialize secret manager.

        Args:
            config: Optional configuration (uses environment by default)
        """
        self.config = config or SecretConfig(source=SecretSource.ENV)
        self._cache: Dict[str, str] = {}
        self._initialized = False

    def get_secret(self, secret_name: str, required: bool = True) -> Optional[str]:
        """
        Retrieve a secret value.

        Args:
            secret_name: Name/ID of the secret
            required: Whether secret is required (raises if missing)

        Returns:
            Secret value or None if not required and not found

        Raises:
            ConfigurationError: If required secret is missing
            AuthenticationError: If authentication with secret provider fails
        """
        # Check cache first. CodeQL py/clear-text-logging-sensitive-data:
        # the cached value is in scope; don't interpolate ``secret_name``.
        if secret_name in self._cache:
            logger.debug("Secret retrieved from cache")
            return self._cache[secret_name]

        # Retrieve from source
        value = self._retrieve_secret(secret_name)

        if value is None and required:
            raise ConfigurationError(
                f"Required secret not found: {secret_name}",
                context={"secret_name": secret_name, "source": self.config.source.value},
                suggestions=[
                    f"Set environment variable: {secret_name}",
                    f"Check secret exists in {self.config.source.value}",
                    "Verify authentication credentials",
                ],
            )

        # Cache if found. ``value`` is in scope; log only the action.
        if value is not None:
            self._cache[secret_name] = value
            logger.debug("Secret cached")

        return value

    def _retrieve_secret(self, secret_name: str) -> Optional[str]:
        """Retrieve secret from configured source"""
        if self.config.source == SecretSource.ENV:
            return self._get_from_env(secret_name)
        elif self.config.source == SecretSource.GCP_SECRET_MANAGER:
            return self._get_from_gcp(secret_name)
        elif self.config.source == SecretSource.AWS_SECRETS_MANAGER:
            return self._get_from_aws(secret_name)
        elif self.config.source == SecretSource.AZURE_KEY_VAULT:
            return self._get_from_azure(secret_name)
        elif self.config.source == SecretSource.HASHICORP_VAULT:
            return self._get_from_vault(secret_name)
        elif self.config.source == SecretSource.LOCAL_FILE:
            return self._get_from_file(secret_name)
        else:
            raise ConfigurationError(f"Unsupported secret source: {self.config.source}")

    def _get_from_env(self, secret_name: str) -> Optional[str]:
        """Get secret from environment variable"""
        return os.environ.get(secret_name)

    def _get_from_gcp(self, secret_name: str) -> Optional[str]:
        """Get secret from GCP Secret Manager"""
        try:
            from google.cloud import secretmanager
        except ImportError:
            raise ConfigurationError(
                "GCP Secret Manager requires google-cloud-secret-manager package",
                suggestions=["pip install google-cloud-secret-manager"],
            )

        if not self.config.project_id:
            raise ConfigurationError(
                "GCP project_id required for Secret Manager",
                suggestions=["Set project_id in SecretConfig"],
            )

        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{self.config.project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            # The GCP SDK is an untyped/optional dependency, so its return
            # values are ``Any``; bind through an annotated local to keep the
            # typed boundary honest (``str``).
            payload: str = response.payload.data.decode("UTF-8")
            return payload
        except Exception as e:
            logger.warning(f"Failed to retrieve secret from GCP: {e}")
            return None

    def _get_from_aws(self, secret_name: str) -> Optional[str]:
        """Get secret from AWS Secrets Manager"""
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            raise ConfigurationError(
                "AWS Secrets Manager requires boto3 package", suggestions=["pip install boto3"]
            )

        region = self.config.region or os.environ.get("AWS_REGION", "us-east-1")

        try:
            client = boto3.client("secretsmanager", region_name=region)
            response = client.get_secret_value(SecretId=secret_name)

            # Handle both string and binary secrets. boto3 is an untyped,
            # lazily-imported dependency (its calls return ``Any``); bind the
            # result through an annotated local at the typed boundary.
            if "SecretString" in response:
                secret_string: str = response["SecretString"]
                return secret_string
            else:
                import base64

                return base64.b64decode(response["SecretBinary"]).decode("utf-8")
        except ClientError as e:
            # CodeQL py/clear-text-logging-sensitive-data: don't
            # interpolate ``secret_name`` (the surrounding ``response``
            # frame and any nearby ``value`` are in scope).
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning("Secret not found in AWS")
                return None
            else:
                logger.error("Failed to retrieve secret from AWS: %s", type(e).__name__)
                raise AuthenticationError(
                    f"Failed to access AWS Secrets Manager: {e}", original_error=e
                )
        except Exception as e:
            logger.warning(f"Failed to retrieve secret from AWS: {e}")
            return None

    def _get_from_azure(self, secret_name: str) -> Optional[str]:
        """Get secret from Azure Key Vault"""
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError:
            raise ConfigurationError(
                "Azure Key Vault requires azure-keyvault-secrets and azure-identity packages",
                suggestions=["pip install azure-keyvault-secrets azure-identity"],
            )

        if not self.config.vault_url:
            raise ConfigurationError(
                "vault_url required for Azure Key Vault",
                suggestions=[
                    "Set vault_url in SecretConfig (e.g., https://myvault.vault.azure.net/)"
                ],
            )

        try:
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=self.config.vault_url, credential=credential)
            secret = client.get_secret(secret_name)
            # Azure SDK is an untyped/optional dependency (returns ``Any``).
            secret_value: Optional[str] = secret.value
            return secret_value
        except Exception as e:
            logger.warning(f"Failed to retrieve secret from Azure: {e}")
            return None

    def _get_from_vault(self, secret_name: str) -> Optional[str]:
        """Get secret from HashiCorp Vault"""
        try:
            import hvac
        except ImportError:
            raise ConfigurationError(
                "HashiCorp Vault requires hvac package", suggestions=["pip install hvac"]
            )

        if not self.config.vault_url:
            raise ConfigurationError(
                "vault_url required for HashiCorp Vault",
                suggestions=["Set vault_url in SecretConfig"],
            )

        vault_path = self.config.vault_path or "secret/data"
        vault_token = os.environ.get("VAULT_TOKEN")

        if not vault_token:
            raise AuthenticationError(
                "VAULT_TOKEN environment variable required",
                suggestions=["export VAULT_TOKEN=<your-token>"],
            )

        try:
            client = hvac.Client(url=self.config.vault_url, token=vault_token)

            if not client.is_authenticated():
                raise AuthenticationError("Failed to authenticate with Vault")

            response = client.secrets.kv.v2.read_secret_version(
                path=secret_name, mount_point=vault_path.split("/")[0]
            )
            # hvac is an untyped/optional dependency (returns ``Any``).
            vault_value: Optional[str] = response["data"]["data"].get("value")
            return vault_value
        except Exception as e:
            logger.warning(f"Failed to retrieve secret from Vault: {e}")
            return None

    def _get_from_file(self, secret_name: str) -> Optional[str]:
        """
        Get secret from local file.

        Expects file at: ``~/.fluid/secrets/<secret_name>``

        SECURITY (path traversal / arbitrary-file-read): ``secret_name``
        is attacker-influenced — it arrives from a contract ``secretRef``
        of the form ``file://<identifier>`` (see
        ``build_runners/_acquisition_common.py::resolve_secret_ref``). A
        naive ``secrets_dir / secret_name`` is unsafe because pathlib
        DISCARDS the left operand when the right is absolute
        (``Path("/a") / "/etc/passwd" == Path("/etc/passwd")``), and a
        relative ``../../x`` walks out of the secrets dir. We therefore:

        1. Reject names that are absolute or contain ``..`` / any path
           separator BEFORE joining (cheap, fails fast on the obvious
           payloads — including the pathlib absolute-RHS footgun).
        2. Resolve the candidate and re-confirm it stays under the
           resolved secrets dir via ``relative_to`` (defence in depth
           against symlinks / normalisation surprises).

        Fails CLOSED: any escape raises ``ConfigurationError`` rather
        than reading the out-of-bounds file. A genuinely-missing
        in-bounds secret still returns ``None``.
        """
        from pathlib import Path, PurePosixPath

        secrets_dir = Path.home() / ".fluid" / "secrets"

        # (1) Pre-join rejection. ``os.sep``/``os.altsep`` cover the
        # platform separators; we also explicitly reject ``/`` and ``\``
        # so a Windows-style payload is caught on POSIX and vice-versa.
        # ``PurePosixPath(secret_name).is_absolute()`` catches a leading
        # ``/``; ``os.path.isabs`` catches a drive-letter form on Windows.
        name = secret_name or ""
        bad_separators = {"/", "\\", os.sep}
        if os.altsep:
            bad_separators.add(os.altsep)
        if (
            not name
            or name in (".", "..")
            or ".." in name.replace("\\", "/").split("/")
            or any(sep in name for sep in bad_separators)
            or os.path.isabs(name)
            or PurePosixPath(name).is_absolute()
        ):
            raise ConfigurationError(
                "Invalid secret name for file-backed secret store",
                context={"source": self.config.source.value},
                suggestions=[
                    "Secret names must be a single path component with no "
                    "'/', '\\', or '..' and must not be absolute",
                ],
            )

        base = secrets_dir.resolve()
        secret_file = (secrets_dir / name).resolve()

        # (2) Post-resolve confinement. Fail closed on any escape.
        try:
            secret_file.relative_to(base)
        except ValueError as exc:
            raise ConfigurationError(
                "Refusing to read secret outside the secrets directory",
                context={"source": self.config.source.value},
                suggestions=[
                    "Secret names must resolve to a file under ~/.fluid/secrets",
                ],
            ) from exc

        if not secret_file.exists():
            return None

        try:
            return secret_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning(f"Failed to read secret from file: {e}")
            return None

    def clear_cache(self) -> None:
        """Clear the secret cache"""
        self._cache.clear()
        logger.debug("Secret cache cleared")


# Global secret manager instance
_global_manager: Optional[SecretManager] = None


def get_secret_manager() -> SecretManager:
    """Get or create global secret manager"""
    global _global_manager
    if _global_manager is None:
        # Auto-detect source based on environment
        source = SecretSource.ENV
        config_dict: Dict[str, Any] = {"source": source}

        if os.environ.get("GCP_PROJECT"):
            source = SecretSource.GCP_SECRET_MANAGER
            config_dict = {"source": source, "project_id": os.environ.get("GCP_PROJECT")}
        elif os.environ.get("AWS_REGION"):
            source = SecretSource.AWS_SECRETS_MANAGER
            config_dict = {"source": source, "region": os.environ.get("AWS_REGION")}

        _global_manager = SecretManager(SecretConfig(**config_dict))

    return _global_manager


def get_secret(
    secret_name: str, required: bool = True, default: Optional[str] = None
) -> Optional[str]:
    """
    Convenience function to get a secret.

    Args:
        secret_name: Name of the secret
        required: Whether secret is required
        default: Default value if not found (only used if not required)

    Returns:
        Secret value or default
    """
    manager = get_secret_manager()
    value = manager.get_secret(secret_name, required=required)
    return value if value is not None else default
