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

# fluid_build/providers/aws/util/credentials.py
"""
Enhanced credential management for AWS provider.

Provides structured credential resolution from multiple sources:
- Explicit parameters
- Environment variables
- AWS profiles
- Instance metadata (EC2/ECS)
- Role assumption
- SSO

Inspired by Snowflake provider's credential adapter pattern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class AwsCredentials:
    """
    AWS credentials with multiple authentication methods.

    Supports:
    - Access key / secret key
    - Session tokens
    - Profile-based auth
    - Role assumption
    - SSO
    """

    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    region: Optional[str] = None
    profile: Optional[str] = None
    role_arn: Optional[str] = None
    role_session_name: Optional[str] = None
    external_id: Optional[str] = None
    endpoint_url: Optional[str] = None

    def to_boto3_config(self) -> Dict[str, Any]:
        """
        Convert to boto3 session configuration.

        Returns:
            Dict suitable for boto3.Session()
        """
        config = {}

        if self.access_key_id:
            config["aws_access_key_id"] = self.access_key_id

        if self.secret_access_key:
            config["aws_secret_access_key"] = self.secret_access_key

        if self.session_token:
            config["aws_session_token"] = self.session_token

        if self.region:
            config["region_name"] = self.region

        if self.profile:
            config["profile_name"] = self.profile

        return config

    def is_complete(self) -> bool:
        """Check if credentials are complete for authentication."""
        # Profile-based is complete
        if self.profile:
            return True

        # Access key based requires both key and secret
        if self.access_key_id and self.secret_access_key:
            return True

        # Role assumption requires ARN
        if self.role_arn:
            return True

        # Otherwise incomplete (will use default credential chain)
        return False

    def __repr__(self) -> str:
        """Safe representation (no secrets)."""
        parts = []

        if self.access_key_id:
            parts.append(f"access_key_id={self.access_key_id[:8]}...")
        if self.profile:
            parts.append(f"profile={self.profile}")
        if self.region:
            parts.append(f"region={self.region}")
        if self.role_arn:
            parts.append(f"role_arn={self.role_arn}")

        return f"AwsCredentials({', '.join(parts)})"


class AwsCredentialResolver:
    """
    Resolve AWS credentials from multiple sources.

    Resolution priority:
    1. Explicit parameters
    2. Environment variables
    3. AWS profile
    4. Role assumption
    5. Instance metadata (EC2/ECS)
    6. Default boto3 credential chain
    """

    def __init__(self):
        """Initialize credential resolver."""
        self._cache: Optional[AwsCredentials] = None

    def resolve(
        self,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        role_arn: Optional[str] = None,
        **kwargs: Any,
    ) -> AwsCredentials:
        """
        Resolve AWS credentials from multiple sources.

        Args:
            access_key_id: Explicit access key
            secret_access_key: Explicit secret key
            session_token: Explicit session token
            region: AWS region
            profile: AWS profile name
            role_arn: Role ARN to assume
            **kwargs: Additional parameters

        Returns:
            AwsCredentials instance
        """
        creds = AwsCredentials()

        # 1. Explicit parameters (highest priority)
        creds.access_key_id = access_key_id
        creds.secret_access_key = secret_access_key
        creds.session_token = session_token
        creds.region = region
        creds.profile = profile
        creds.role_arn = role_arn
        creds.role_session_name = kwargs.get("role_session_name")
        creds.external_id = kwargs.get("external_id")
        creds.endpoint_url = kwargs.get("endpoint_url")

        # 2. Environment variables
        if not creds.access_key_id:
            creds.access_key_id = os.getenv("AWS_ACCESS_KEY_ID")

        if not creds.secret_access_key:
            creds.secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")

        if not creds.session_token:
            creds.session_token = os.getenv("AWS_SESSION_TOKEN")

        if not creds.region:
            creds.region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION")

        if not creds.profile:
            creds.profile = os.getenv("AWS_PROFILE")

        if not creds.role_arn:
            creds.role_arn = os.getenv("AWS_ROLE_ARN")

        # 3. Default region if not set
        if not creds.region:
            creds.region = "us-east-1"

        return creds

    def from_environment(self) -> AwsCredentials:
        """
        Resolve credentials from environment variables only.

        Returns:
            AwsCredentials from environment
        """
        return self.resolve()

    def from_profile(self, profile: str, region: Optional[str] = None) -> AwsCredentials:
        """
        Resolve credentials from AWS profile.

        Args:
            profile: Profile name from ~/.aws/credentials
            region: Optional region override

        Returns:
            AwsCredentials for profile
        """
        return self.resolve(profile=profile, region=region)

    def assume_role(
        self,
        role_arn: str,
        role_session_name: Optional[str] = None,
        external_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> AwsCredentials:
        """
        Create credentials for role assumption.

        Args:
            role_arn: ARN of role to assume
            role_session_name: Session name (optional)
            external_id: External ID for role assumption
            region: AWS region

        Returns:
            AwsCredentials for role assumption
        """
        if not role_session_name:
            role_session_name = f"fluid-forge-{int(time.time())}"

        return self.resolve(
            role_arn=role_arn,
            role_session_name=role_session_name,
            external_id=external_id,
            region=region,
        )

    def get_boto3_session(self, credentials: Optional[AwsCredentials] = None):
        """
        Get boto3 session from credentials.

        Args:
            credentials: AwsCredentials instance (resolves from env if None)

        Returns:
            boto3.Session configured with credentials
        """
        import boto3

        if credentials is None:
            credentials = self.from_environment()

        # Handle role assumption
        if credentials.role_arn:
            return self._assume_role_session(credentials)

        # Create session from credentials
        config = credentials.to_boto3_config()
        return boto3.Session(**config)

    def _assume_role_session(self, credentials: AwsCredentials):
        """
        Create session by assuming role.

        Args:
            credentials: Credentials with role_arn set

        Returns:
            boto3.Session with assumed role credentials
        """
        import boto3

        # Create base session (for STS)
        base_config = {k: v for k, v in credentials.to_boto3_config().items() if k != "role_arn"}
        base_session = boto3.Session(**base_config)

        # Assume role
        sts = base_session.client("sts")

        assume_role_params = {
            "RoleArn": credentials.role_arn,
            "RoleSessionName": credentials.role_session_name or "fluid-forge-session",
        }

        if credentials.external_id:
            assume_role_params["ExternalId"] = credentials.external_id

        response = sts.assume_role(**assume_role_params)

        # Create session with temporary credentials
        temp_creds = response["Credentials"]
        return boto3.Session(
            aws_access_key_id=temp_creds["AccessKeyId"],
            aws_secret_access_key=temp_creds["SecretAccessKey"],
            aws_session_token=temp_creds["SessionToken"],
            region_name=credentials.region,
        )


class CredentialError(Exception):
    """Raised when credential resolution fails."""

    pass


# Global resolver instance
_resolver = AwsCredentialResolver()


def resolve_credentials(**kwargs) -> AwsCredentials:
    """
    Resolve AWS credentials.

    Args:
        **kwargs: Credential parameters

    Returns:
        AwsCredentials instance
    """
    return _resolver.resolve(**kwargs)


def get_boto3_session(**kwargs):
    """
    Get boto3 session with resolved credentials.

    Args:
        **kwargs: Credential parameters

    Returns:
        boto3.Session
    """
    credentials = resolve_credentials(**kwargs)
    return _resolver.get_boto3_session(credentials)


# For testing
import time
