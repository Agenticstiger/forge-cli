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
Provider System for FLUID Forge

The provider system enables infrastructure-specific configuration and deployment
for different cloud platforms and runtimes. Each provider handles:
- Platform-specific configuration
- Resource provisioning
- Environment setup
- Deployment configuration

Built-in providers:
- local: Local development environment
- gcp: Google Cloud Platform
- aws: Amazon Web Services
- snowflake: Snowflake Data Cloud
- azure: Microsoft Azure

Teams can extend this system by creating custom providers that follow
the InfrastructureProvider interface.
"""

from typing import Any, Dict, List, Optional

from ..core.interfaces import InfrastructureProvider
from ..core.registry import ProviderRegistry
from .aws import AWSProvider
from .gcp import GCPProvider

# Import built-in providers
from .local import LocalProvider
from .snowflake import SnowflakeProvider


def register_providers(registry: ProviderRegistry) -> None:
    """Register all built-in providers with the registry"""

    providers = [
        ("local", LocalProvider),
        ("gcp", GCPProvider),
        ("aws", AWSProvider),
        ("snowflake", SnowflakeProvider),
    ]

    for name, provider_class in providers:
        registry.register(name, provider_class, source="builtin")


__all__ = ["LocalProvider", "GCPProvider", "AWSProvider", "SnowflakeProvider", "register_providers"]
