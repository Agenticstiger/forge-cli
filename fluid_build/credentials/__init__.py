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
FLUID CLI - Unified Credential Management

Secure credential resolution across all providers with multiple sources:
- CLI arguments (highest priority)
- Environment variables
- .env files
- OS Keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service)
- Encrypted files
- HashiCorp Vault
- Cloud Secret Managers (GCP/AWS/Azure)
- Interactive prompts (lowest priority)
"""

from .adapters import (
    AWSCredentialAdapter,
    GCPCredentialAdapter,
    SnowflakeCredentialAdapter,
)
from .resolver import (
    BaseCredentialResolver,
    CredentialConfig,
    CredentialError,
    CredentialSource,
)

# Convenience functions
_adapters = {}


def get_snowflake_adapter(config: CredentialConfig = None) -> SnowflakeCredentialAdapter:
    """Get or create Snowflake credential adapter."""
    if "snowflake" not in _adapters:
        _adapters["snowflake"] = SnowflakeCredentialAdapter(config=config)
    return _adapters["snowflake"]


def get_gcp_adapter(config: CredentialConfig = None) -> GCPCredentialAdapter:
    """Get or create GCP credential adapter."""
    if "gcp" not in _adapters:
        _adapters["gcp"] = GCPCredentialAdapter(config=config)
    return _adapters["gcp"]


def get_aws_adapter(config: CredentialConfig = None) -> AWSCredentialAdapter:
    """Get or create AWS credential adapter."""
    if "aws" not in _adapters:
        _adapters["aws"] = AWSCredentialAdapter(config=config)
    return _adapters["aws"]


def get_adapter(provider: str, config: CredentialConfig = None) -> BaseCredentialResolver:
    """Get credential adapter for any provider."""
    adapters = {
        "snowflake": get_snowflake_adapter,
        "gcp": get_gcp_adapter,
        "aws": get_aws_adapter,
    }

    adapter_func = adapters.get(provider.lower())
    if not adapter_func:
        raise ValueError(f"Unknown provider: {provider}")

    return adapter_func(config)


__all__ = [
    "BaseCredentialResolver",
    "CredentialConfig",
    "CredentialSource",
    "CredentialError",
    "SnowflakeCredentialAdapter",
    "GCPCredentialAdapter",
    "AWSCredentialAdapter",
    "get_snowflake_adapter",
    "get_gcp_adapter",
    "get_aws_adapter",
    "get_adapter",
]
