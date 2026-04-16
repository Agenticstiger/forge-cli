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
AWS AgentPolicy Extraction and Enforcement.

NEW in FLUID 0.7.1: Agentic governance for AI/LLM usage policies on data products.

This module extracts and enforces AI/LLM usage policies from FLUID contracts,
enabling governance of how data products can be used by AI agents and language models.

Example contract:
    {
        "agentPolicy": {
            "allowedModels": ["gpt-4", "claude-3-opus", "gemini-pro"],
            "deniedModels": ["public-llm"],
            "usageConstraints": {
                "prohibitedPurposes": ["advertising", "profiling", "surveillance"],
                "allowedPurposes": ["analytics", "research", "reporting"]
            },
            "requiresHumanApproval": true,
            "auditLevel": "full"
        }
    }
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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


class AgentPolicyExtractor:
    """
    Extracts AI/LLM usage policies from FLUID contracts.

    Converts agentPolicy specifications into AWS tags for governance and
    optional enforcement through AWS Lake Formation or IAM policies.
    """

    def __init__(self):
        """Initialize agent policy extractor."""
        self.logger = logging.getLogger(__name__)

    def extract_tags(
        self, contract: Dict[str, Any], exposure: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """
        Extract agentPolicy metadata as AWS tags.

        Args:
            contract: FLUID contract with agentPolicy section
            exposure: Optional specific exposure configuration

        Returns:
            Dictionary of AWS tags for agent policy metadata
        """
        # Check both contract-level and exposure-level agentPolicy
        agent_policy = contract.get("agentPolicy", {})

        if exposure:
            # Exposure-level policy overrides contract-level
            exposure_contract = exposure.get("contract", {})
            if "agentPolicy" in exposure_contract:
                agent_policy = exposure_contract["agentPolicy"]

        if not agent_policy:
            return {}

        tags = {}

        # 1. Allowed models
        allowed_models = agent_policy.get("allowedModels", [])
        if allowed_models:
            models_str = ",".join(allowed_models)
            tags["fluid:agent_allowed_models"] = sanitize_tag_value(models_str)

        # 2. Denied models
        denied_models = agent_policy.get("deniedModels", [])
        if denied_models:
            models_str = ",".join(denied_models)
            tags["fluid:agent_denied_models"] = sanitize_tag_value(models_str)

        # 3. Usage constraints
        usage_constraints = agent_policy.get("usageConstraints", {})

        # Prohibited purposes
        prohibited = usage_constraints.get("prohibitedPurposes", [])
        if prohibited:
            purposes_str = ",".join(prohibited)
            tags["fluid:agent_prohibited_purposes"] = sanitize_tag_value(purposes_str)

        # Allowed purposes
        allowed = usage_constraints.get("allowedPurposes", [])
        if allowed:
            purposes_str = ",".join(allowed)
            tags["fluid:agent_allowed_purposes"] = sanitize_tag_value(purposes_str)

        # 4. Human approval requirement
        if agent_policy.get("requiresHumanApproval"):
            tags["fluid:agent_requires_approval"] = "true"

        # 5. Audit level
        if agent_policy.get("auditLevel"):
            tags["fluid:agent_audit_level"] = sanitize_tag_value(str(agent_policy["auditLevel"]))

        # 6. Access tier (if specified)
        if agent_policy.get("accessTier"):
            tags["fluid:agent_access_tier"] = sanitize_tag_value(str(agent_policy["accessTier"]))

        # 7. Rate limits (if specified)
        rate_limits = agent_policy.get("rateLimits", {})
        if rate_limits:
            if rate_limits.get("requestsPerDay"):
                tags["fluid:agent_rate_limit_daily"] = str(rate_limits["requestsPerDay"])
            if rate_limits.get("requestsPerHour"):
                tags["fluid:agent_rate_limit_hourly"] = str(rate_limits["requestsPerHour"])

        # 8. Custom agent tags
        for tag in agent_policy.get("tags", []):
            safe_tag = sanitize_tag_key(tag)
            if safe_tag:
                tags[f"fluid:agent_{safe_tag}"] = "true"

        self.logger.info(f"Extracted {len(tags)} agent policy tags")
        return tags

    def generate_lake_formation_policy(
        self, contract: Dict[str, Any], resource_arn: str
    ) -> Optional[Dict[str, Any]]:
        """
        Generate AWS Lake Formation data filter for agent policy enforcement.

        This is an optional advanced feature that creates Lake Formation
        cell-level security policies based on agent policy constraints.

        Args:
            contract: FLUID contract with agentPolicy
            resource_arn: ARN of the data resource (Glue table)

        Returns:
            Lake Formation data filter specification or None if not applicable
        """
        agent_policy = contract.get("agentPolicy", {})
        if not agent_policy:
            return None

        # Only generate Lake Formation policies if explicitly requested
        if not agent_policy.get("enforceThroughLakeFormation", False):
            return None

        # Build Lake Formation data filter
        data_filter = {
            "Name": f"AgentPolicy_{contract.get('id', 'unknown')}",
            "TableCatalogId": self._extract_account_from_arn(resource_arn),
            "DatabaseName": self._extract_database_from_arn(resource_arn),
            "TableName": self._extract_table_from_arn(resource_arn),
            "RowFilter": {"FilterExpression": "1=1"},  # Default: allow all rows
            "ColumnWildcard": {},  # Default: allow all columns
        }

        # Apply column restrictions if specified
        restricted_columns = agent_policy.get("restrictedColumns", [])
        if restricted_columns:
            # Exclude restricted columns
            data_filter["ColumnWildcard"] = {"ExcludedColumnNames": restricted_columns}

        self.logger.info("Generated Lake Formation data filter for agent policy")
        return data_filter

    def _extract_account_from_arn(self, arn: str) -> str:
        """Extract AWS account ID from ARN."""
        parts = arn.split(":")
        return parts[4] if len(parts) > 4 else ""

    def _extract_database_from_arn(self, arn: str) -> str:
        """Extract database name from Glue table ARN."""
        # ARN format: arn:aws:glue:region:account:table/database/table_name
        parts = arn.split("/")
        return parts[1] if len(parts) > 1 else ""

    def _extract_table_from_arn(self, arn: str) -> str:
        """Extract table name from Glue table ARN."""
        # ARN format: arn:aws:glue:region:account:table/database/table_name
        parts = arn.split("/")
        return parts[2] if len(parts) > 2 else ""


# Global extractor instance
_extractor = AgentPolicyExtractor()


def extract_agent_policy_tags(
    contract: Dict[str, Any], exposure: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """
    Extract agentPolicy tags from FLUID contract.

    Args:
        contract: FLUID data contract
        exposure: Optional specific exposure

    Returns:
        Dict of AWS tags for agent policy
    """
    return _extractor.extract_tags(contract, exposure)


def generate_lake_formation_policy(
    contract: Dict[str, Any], resource_arn: str
) -> Optional[Dict[str, Any]]:
    """
    Generate Lake Formation data filter for agent policy enforcement.

    Args:
        contract: FLUID contract
        resource_arn: ARN of the data resource

    Returns:
        Lake Formation data filter specification or None
    """
    return _extractor.generate_lake_formation_policy(contract, resource_arn)
