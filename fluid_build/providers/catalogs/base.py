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

"""Base catalog provider for marketplace integrations

Similar to market.py's BaseCatalogConnector but focused on WRITE operations (publish, update, delete).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class CatalogAsset:
    """Normalized asset representation for catalog publishing

    This is the standardized format that all catalog providers accept.
    Contract data is mapped to this format before publishing.
    """

    id: str  # contract.id
    name: str  # contract.name
    description: str  # contract.description
    type: str  # contract.kind (e.g., 'DataProduct', 'DataStream')
    domain: str  # contract.domain
    owner: str  # contract.metadata.owner.team
    owner_email: str  # contract.metadata.owner.email
    layer: str  # contract.metadata.layer (Bronze/Silver/Gold)
    tags: List[str]  # contract.metadata.tags
    version: str  # contract.version
    platform: str  # gcp, aws, snowflake
    location: Dict[str, Any]  # Platform-specific location info
    schema: Optional[List[Dict[str, Any]]] = None  # From exposes[0].contract.schema
    sensitivity: str = "internal"  # internal, public, confidential
    contract_yaml: Optional[str] = None  # Raw YAML content of the contract file


@dataclass
class PublishResult:
    """Result of catalog publish operation"""

    success: bool
    catalog_id: str
    asset_id: str
    catalog_url: Optional[str] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class BaseCatalogProvider(ABC):
    """Base class for all catalog integrations

    Provides common functionality:
    - Contract to asset mapping
    - Authentication setup
    - Error handling patterns
    - Logging

    Subclasses implement catalog-specific operations.
    """

    name: str = "base"  # Override in subclasses

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.endpoint = config.get("endpoint", config.get("url", ""))
        self.auth = config.get("auth", {})
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Resilience settings
        self.max_retries = config.get("max_retries", 3)
        self.timeout = config.get("timeout", 30.0)

    @abstractmethod
    async def publish(self, asset: CatalogAsset) -> PublishResult:
        """Publish asset to catalog

        Args:
            asset: Normalized asset to publish

        Returns:
            PublishResult with success/failure details
        """
        pass

    @abstractmethod
    async def update(self, asset: CatalogAsset) -> PublishResult:
        """Update existing asset in catalog

        Args:
            asset: Normalized asset with updated information

        Returns:
            PublishResult with success/failure details
        """
        pass

    @abstractmethod
    async def verify(self, asset_id: str) -> bool:
        """Verify asset exists in catalog

        Args:
            asset_id: Unique identifier of the asset

        Returns:
            True if asset exists, False otherwise
        """
        pass

    async def delete(self, asset_id: str) -> bool:
        """Delete asset from catalog (optional)

        Args:
            asset_id: Unique identifier of the asset

        Returns:
            True if deleted, False otherwise
        """
        self.logger.warning(f"Delete not implemented for {self.name}")
        return False

    def map_contract_to_asset(self, contract: Dict[str, Any]) -> CatalogAsset:
        """Map FLUID contract to catalog asset

        This is a common mapping that works for most contracts.
        Subclasses can override for custom mapping logic.

        Args:
            contract: FLUID contract dictionary

        Returns:
            CatalogAsset instance
        """
        metadata = contract.get("metadata", {})
        owner = metadata.get("owner", {})
        exposes = contract.get("exposes", [])

        # Extract platform and location from binding
        platform = "unknown"
        location = {}
        schema = None
        sensitivity = "internal"

        if exposes:
            first_expose = exposes[0]
            binding = first_expose.get("binding", {})
            platform = binding.get("platform", "unknown")
            location = binding.get("location", {})

            # Extract schema
            contract_spec = first_expose.get("contract", {})
            schema = contract_spec.get("schema")

            # Extract sensitivity
            sensitivity = first_expose.get("sensitivity", "internal")

        return CatalogAsset(
            id=contract.get("id", contract.get("name", "unknown")),
            name=contract["name"],
            description=contract.get("description", ""),
            type=contract.get("kind", "DataProduct").lower(),
            domain=contract.get("domain", "general"),
            owner=owner.get("team", owner.get("name", "unknown")),
            owner_email=owner.get("email", ""),
            layer=metadata.get("layer", "Bronze"),
            tags=metadata.get("tags", []),
            version=contract.get("version", "1.0.0"),
            platform=platform,
            location=location,
            schema=schema,
            sensitivity=sensitivity,
        )

    def validate_asset(self, asset: CatalogAsset) -> tuple[bool, Optional[str]]:
        """Validate asset before publishing

        Args:
            asset: Asset to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not asset.name:
            return False, "Asset name is required"

        if not asset.id:
            return False, "Asset ID is required"

        if not asset.owner:
            return False, "Asset owner is required"

        if not asset.domain:
            return False, "Asset domain is required"

        return True, None

    async def health_check(self) -> bool:
        """Check if catalog is accessible

        Returns:
            True if healthy, False otherwise
        """
        try:
            # Default implementation - subclasses should override
            return True
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return False
