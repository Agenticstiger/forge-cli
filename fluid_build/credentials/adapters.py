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
Provider-specific credential adapters.

Each adapter enhances the base resolver with provider-specific
credential resolution logic while maintaining backward compatibility.
"""

import logging
from typing import Any, Dict, List, Optional

from .resolver import BaseCredentialResolver, CredentialConfig

logger = logging.getLogger(__name__)


class SnowflakeCredentialAdapter(BaseCredentialResolver):
    """
    Snowflake credential adapter.

    Snowflake doesn't have a native credential chain like GCP or AWS,
    so we rely entirely on our unified resolver chain.
    """

    def __init__(self, config: Optional[CredentialConfig] = None):
        super().__init__(provider="snowflake", config=config)

    # The keys resolve_snowflake_settings (providers/snowflake/util/config.py)
    # also reads as SF_<KEY> and the pairing rule (protected_env, the
    # Snowflake group) accepts so spelled: the password. Read it here too,
    # after the SNOWFLAKE_ spellings and before the bare one.
    _SF_SPELLED_KEYS = frozenset({"password"})

    def _project_endpoint_in_effect(self) -> bool:
        """A Snowflake account or host from the project's dotenv files is in effect."""
        from .dotenv_store import project_endpoint_in_effect

        return project_endpoint_in_effect("Snowflake")

    def _with_sf_spelling(self, key: str, names: List[str]) -> List[str]:
        if key.lower() not in self._SF_SPELLED_KEYS:
            return names
        return names[:-1] + [f"SF_{key.upper()}"] + names[-1:]

    def _env_names(self, key: str) -> List[str]:
        return self._with_sf_spelling(key, super()._env_names(key))

    def _dotenv_names(self, key: str) -> List[str]:
        return self._with_sf_spelling(key, super()._dotenv_names(key))

    def _get_provider_default(self, key: str, **kwargs) -> Optional[str]:
        """
        Snowflake doesn't have SDK-based defaults.
        Returns None to rely on the resolver chain.
        """
        return None

    def get_connection_params(self, **kwargs) -> Dict[str, Any]:
        """
        Build Snowflake connection parameters with secure credential resolution.

        Returns:
            Dictionary of connection parameters for snowflake-connector-python
        """
        params = {}

        # Required parameters
        params["account"] = self.get_credential("account", **kwargs)
        params["user"] = self.get_credential("user", **kwargs)

        # Authentication (password or key-pair or OAuth or SSO)
        # Try password first
        password = self.get_credential("password", required=False, **kwargs)
        if password:
            params["password"] = password
        else:
            # Try key-pair authentication
            private_key_path = self.get_credential("private_key_path", required=False, **kwargs)
            if private_key_path:
                params["private_key_path"] = private_key_path
                private_key_passphrase = self.get_credential(
                    "private_key_passphrase", required=False, **kwargs
                )
                if private_key_passphrase:
                    params["private_key_passphrase"] = private_key_passphrase
            else:
                # Try OAuth token
                oauth_token = self.get_credential("oauth_token", required=False, **kwargs)
                if oauth_token:
                    params["oauth_token"] = oauth_token
                elif self._project_endpoint_in_effect():
                    # An account the project's files chose goes only with the
                    # project's own credential (protected_env): never sign in
                    # to it with the operator's browser session.
                    authenticator = self.get_credential("authenticator", required=False, **kwargs)
                    if authenticator:
                        params["authenticator"] = authenticator
                else:
                    # Fallback to external browser (SSO)
                    params["authenticator"] = self.get_credential(
                        "authenticator", required=False, cli_value="externalbrowser", **kwargs
                    )

        # Optional parameters
        for key in ["warehouse", "database", "schema", "role"]:
            value = self.get_credential(key, required=False, **kwargs)
            if value:
                params[key] = value

        return params


class GCPCredentialAdapter(BaseCredentialResolver):
    """
    GCP credential adapter.

    Enhances google.auth.default() with additional credential sources
    while maintaining compatibility with Application Default Credentials (ADC).
    """

    def __init__(self, config: Optional[CredentialConfig] = None):
        super().__init__(provider="gcp", config=config)

    def _get_provider_default(self, key: str, **kwargs) -> Optional[Any]:
        """
        Use google.auth.default() as fallback.

        This maintains compatibility with existing GCP auth:
        - GOOGLE_APPLICATION_CREDENTIALS
        - gcloud ADC
        - Workload Identity
        - Compute Engine/GKE service accounts
        """
        if key == "credentials":
            try:
                from google.auth import default as google_auth_default
                from google.auth.transport.requests import Request

                credentials, project = google_auth_default()

                # Refresh if needed
                if hasattr(credentials, "refresh") and hasattr(credentials, "expired"):
                    if credentials.expired and credentials.refresh_token:
                        credentials.refresh(Request())

                return credentials

            except ImportError:
                logger.debug("google-auth library not available")
                return None
            except Exception as e:
                logger.debug(f"Failed to get GCP default credentials: {e}")
                return None

        return None

    def get_credentials(self, mode: str = "adc", **kwargs):
        """
        Get GCP credentials using enhanced resolution.

        Args:
            mode: Authentication mode ("adc", "sa-key", "external")
            **kwargs: Additional parameters (e.g., service_account_key path)

        Returns:
            Google credentials object
        """
        # If user explicitly set service account key, use that
        if mode == "sa-key" or "service_account_key" in kwargs:
            sa_key_path = kwargs.get("service_account_key") or self.get_credential(
                "service_account_key", required=False, **kwargs
            )

            if sa_key_path:
                try:
                    from google.oauth2.service_account import Credentials as SACredentials

                    credentials = SACredentials.from_service_account_file(
                        sa_key_path,
                        scopes=kwargs.get(
                            "scopes", ["https://www.googleapis.com/auth/cloud-platform"]
                        ),
                    )
                    return credentials
                except Exception as e:
                    logger.error(f"Failed to load service account key: {e}")
                    raise

        # Otherwise use default chain (includes our enhancements + ADC)
        return self._get_provider_default("credentials", **kwargs)


class AWSCredentialAdapter(BaseCredentialResolver):
    """
    AWS credential adapter.

    Enhances boto3.Session() with additional credential sources
    while maintaining compatibility with AWS credential chain.
    """

    def __init__(self, config: Optional[CredentialConfig] = None):
        super().__init__(provider="aws", config=config)

    def _get_provider_default(self, key: str, **kwargs) -> Optional[Any]:
        """
        Use boto3's credential chain as fallback.

        This maintains compatibility with:
        - ~/.aws/credentials
        - ~/.aws/config
        - IAM roles (EC2/ECS/Lambda)
        - SSO sessions
        - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
        """
        if key == "session":
            try:
                import boto3

                return boto3.Session(
                    region_name=kwargs.get("region"), profile_name=kwargs.get("profile")
                )

            except ImportError:
                logger.debug("boto3 library not available")
                return None
            except Exception as e:
                logger.debug(f"Failed to create AWS session: {e}")
                return None

        return None

    def get_session(self, **kwargs):
        """
        Get AWS session using enhanced resolution.

        Returns:
            boto3.Session object
        """
        # If user explicitly set access keys, use those
        access_key = self.get_credential("aws_access_key_id", required=False, **kwargs)
        secret_key = self.get_credential("aws_secret_access_key", required=False, **kwargs)

        if access_key and secret_key:
            try:
                import boto3

                session = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=kwargs.get("region"),
                )
                return session

            except Exception as e:
                logger.error(f"Failed to create AWS session with explicit keys: {e}")
                raise

        # Otherwise use boto3's default chain
        return self._get_provider_default("session", **kwargs)
