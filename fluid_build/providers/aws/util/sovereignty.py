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
AWS Sovereignty Validation and Metadata Extraction.

NEW in FLUID 0.7.1: Data sovereignty constraints for jurisdiction and residency compliance.

This module validates and extracts sovereignty metadata from FLUID contracts,
ensuring AWS resources comply with data jurisdiction and residency requirements
(GDPR, CCPA, regional data protection laws).

Example contract:
    {
        "sovereignty": {
            "jurisdiction": "EU",
            "dataResidency": ["eu-west-1", "eu-central-1"],
            "tags": ["gdpr-compliant", "schrems-ii"]
        },
        "binding": {
            "location": {
                "region": "eu-west-1"
            }
        }
    }
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class SovereigntyViolationError(Exception):
    """Raised when sovereignty constraints are violated."""

    pass


# AWS region to jurisdiction mapping
REGION_JURISDICTIONS = {
    # US Regions
    "us-east-1": "US",
    "us-east-2": "US",
    "us-west-1": "US",
    "us-west-2": "US",
    "us-gov-east-1": "US-GOV",
    "us-gov-west-1": "US-GOV",
    # EU Regions
    "eu-west-1": "EU",
    "eu-west-2": "EU",
    "eu-west-3": "EU",
    "eu-central-1": "EU",
    "eu-central-2": "EU",
    "eu-north-1": "EU",
    "eu-south-1": "EU",
    "eu-south-2": "EU",
    # Asia Pacific
    "ap-south-1": "APAC",
    "ap-south-2": "APAC",
    "ap-northeast-1": "APAC",
    "ap-northeast-2": "APAC",
    "ap-northeast-3": "APAC",
    "ap-southeast-1": "APAC",
    "ap-southeast-2": "APAC",
    "ap-southeast-3": "APAC",
    "ap-southeast-4": "APAC",
    "ap-east-1": "APAC",
    # Canada
    "ca-central-1": "CA",
    # Middle East
    "me-south-1": "ME",
    "me-central-1": "ME",
    # South America
    "sa-east-1": "SA",
    # Africa
    "af-south-1": "AF",
}


def sanitize_tag_value(value: str) -> str:
    """
    Sanitize tag values for AWS tags.

    AWS tag value constraints:
    - Max 256 characters
    - Letters, numbers, spaces, and +-=._:/@

    Args:
        value: Raw tag value

    Returns:
        Sanitized tag value
    """
    if not value:
        return ""

    # Convert to string and truncate
    value = str(value)[:256]

    # Replace invalid characters with underscore
    sanitized = ""
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-=._:/@")
    for char in value:
        sanitized += char if char in valid_chars else "_"

    return sanitized


def sanitize_tag_key(key: str) -> str:
    """
    Sanitize tag keys for AWS tags.

    AWS tag key constraints:
    - Max 128 characters
    - Letters, numbers, spaces, and +-=._:/@
    - Cannot start with "aws:"

    Args:
        key: Raw tag key

    Returns:
        Sanitized tag key
    """
    if not key:
        return ""

    # Convert to string and truncate
    key = str(key)[:128]

    # Remove aws: prefix if present
    if key.lower().startswith("aws:"):
        key = key[4:]

    # Replace invalid characters with underscore
    sanitized = ""
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-=._:/@")
    for char in key:
        sanitized += char if char in valid_chars else "_"

    return sanitized


class SovereigntyValidator:
    """
    Validates data sovereignty constraints for AWS deployments.

    Ensures AWS resources are deployed in regions that comply with
    jurisdiction and data residency requirements.
    """

    def __init__(self):
        """Initialize sovereignty validator."""
        self.logger = logging.getLogger(__name__)

    def validate(self, contract: Dict[str, Any], binding: Dict[str, Any]) -> None:
        """
        Validate sovereignty constraints against binding location.

        Args:
            contract: FLUID contract with sovereignty section
            binding: Provider binding with location details

        Raises:
            SovereigntyViolationError: If constraints are violated
        """
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            # No sovereignty constraints - validation passes
            return

        # Extract location from binding
        location = binding.get("location", {})
        region = location.get("region")

        if not region:
            self.logger.warning("No region specified in binding - cannot validate sovereignty")
            return

        # Validate jurisdiction
        self._validate_jurisdiction(sovereignty, region)

        # Validate data residency
        self._validate_data_residency(sovereignty, region)

        self.logger.info(f"✓ Sovereignty validation passed for region: {region}")

    def _validate_jurisdiction(self, sovereignty: Dict[str, Any], region: str) -> None:
        """
        Validate jurisdiction constraints.

        Args:
            sovereignty: Sovereignty configuration
            region: AWS region

        Raises:
            SovereigntyViolationError: If jurisdiction is violated
        """
        required_jurisdiction = sovereignty.get("jurisdiction")
        if not required_jurisdiction:
            return

        # Get actual jurisdiction from region
        actual_jurisdiction = REGION_JURISDICTIONS.get(region)

        if not actual_jurisdiction:
            raise SovereigntyViolationError(
                f"Unknown AWS region: {region}. Cannot determine jurisdiction."
            )

        # Check if jurisdiction matches
        if actual_jurisdiction != required_jurisdiction:
            raise SovereigntyViolationError(
                f"Jurisdiction violation: Region '{region}' is in jurisdiction '{actual_jurisdiction}', "
                f"but contract requires '{required_jurisdiction}'. "
                f"Update binding.location.region to use a region in '{required_jurisdiction}'."
            )

    def _validate_data_residency(self, sovereignty: Dict[str, Any], region: str) -> None:
        """
        Validate data residency constraints.

        Args:
            sovereignty: Sovereignty configuration
            region: AWS region

        Raises:
            SovereigntyViolationError: If data residency is violated
        """
        allowed_regions = sovereignty.get("dataResidency", [])
        if not allowed_regions:
            return

        if region not in allowed_regions:
            raise SovereigntyViolationError(
                f"Data residency violation: Region '{region}' is not in allowed list: {allowed_regions}. "
                f"Update binding.location.region to use an allowed region."
            )

    def extract_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """
        Extract sovereignty metadata as AWS tags.

        Args:
            contract: FLUID contract with sovereignty section

        Returns:
            Dictionary of AWS tags for sovereignty metadata
        """
        sovereignty = contract.get("sovereignty", {})
        if not sovereignty:
            return {}

        tags = {}

        # Jurisdiction tag
        if sovereignty.get("jurisdiction"):
            tags["fluid:data_jurisdiction"] = sanitize_tag_value(sovereignty["jurisdiction"])

        # Data residency enforcement tag
        if sovereignty.get("dataResidency"):
            tags["fluid:data_residency"] = "enforced"
            # Store allowed regions as tag (truncated if needed)
            allowed = ",".join(sovereignty["dataResidency"])
            tags["fluid:allowed_regions"] = sanitize_tag_value(allowed)

        # Custom sovereignty tags
        for tag in sovereignty.get("tags", []):
            safe_tag = sanitize_tag_key(tag)
            if safe_tag:
                tags[f"fluid:sovereignty_{safe_tag}"] = "true"

        # Compliance framework tags (if specified)
        if sovereignty.get("complianceFramework"):
            frameworks = sovereignty["complianceFramework"]
            if isinstance(frameworks, list):
                frameworks_str = ",".join(frameworks)
            else:
                frameworks_str = str(frameworks)
            tags["fluid:compliance_framework"] = sanitize_tag_value(frameworks_str)

        self.logger.info(f"Extracted {len(tags)} sovereignty tags")
        return tags


def validate_sovereignty(contract: Dict[str, Any], binding: Dict[str, Any]) -> None:
    """
    Convenience function to validate sovereignty constraints.

    Args:
        contract: FLUID contract
        binding: Provider binding

    Raises:
        SovereigntyViolationError: If constraints are violated
    """
    validator = SovereigntyValidator()
    validator.validate(contract, binding)


def extract_sovereignty_tags(contract: Dict[str, Any]) -> Dict[str, str]:
    """
    Convenience function to extract sovereignty tags.

    Args:
        contract: FLUID contract

    Returns:
        Dictionary of AWS tags
    """
    validator = SovereigntyValidator()
    return validator.extract_tags(contract)
