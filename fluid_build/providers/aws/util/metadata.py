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

# fluid_build/providers/aws/util/metadata.py
"""
Metadata and tagging utilities for AWS resources.

Extracts structured metadata from FLUID contracts and maps them to AWS tags.
Supports governance, cost tracking, and compliance requirements.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


class MetadataExtractor:
    """
    Extract and transform FLUID contract metadata into AWS tags.

    Follows AWS tagging best practices:
    - Max 50 tags per resource
    - Keys up to 128 characters
    - Values up to 256 characters
    - Case-sensitive
    """

    def __init__(self, tag_prefix: str = "fluid"):
        """
        Initialize metadata extractor.

        Args:
            tag_prefix: Prefix for all FLUID tags (default: "fluid")
        """
        self.tag_prefix = tag_prefix

    def extract_resource_tags(
        self,
        contract: Dict[str, Any],
        exposure: Optional[Dict[str, Any]] = None,
        extra_tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """
        Extract AWS tags from FLUID contract.

        Args:
            contract: FLUID data contract
            exposure: Specific exposure being deployed
            extra_tags: Additional custom tags

        Returns:
            Dict of AWS tags (key: value)
        """
        tags = {}

        # 1. Core contract metadata
        tags.update(self._extract_core_tags(contract))

        # 2. Governance tags
        tags.update(self._extract_governance_tags(contract))

        # 3. Exposure-specific tags
        if exposure:
            tags.update(self._extract_exposure_tags(exposure))

        # 4. Policy tags
        tags.update(self._extract_policy_tags(contract))

        # 5. Lineage tags
        tags.update(self._extract_lineage_tags(contract))

        # 6. Sovereignty tags (NEW in FLUID 0.7.1)
        tags.update(self._extract_sovereignty_tags(contract))

        # 7. AgentPolicy tags (NEW in FLUID 0.7.1)
        tags.update(self._extract_agent_policy_tags(contract, exposure))

        # 8. Custom tags
        if extra_tags:
            tags.update(extra_tags)

        # 9. Add timestamp
        tags[f"{self.tag_prefix}:created_at"] = datetime.utcnow().isoformat()

        # Ensure all values are strings and within AWS limits
        return self._sanitize_tags(tags)

    def _extract_core_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """Extract core contract identification tags."""
        tags = {}
        metadata = contract.get("metadata", {})

        # Contract ID
        if contract.get("id"):
            tags[f"{self.tag_prefix}:contract_id"] = contract["id"]

        # Contract name
        if contract.get("name"):
            tags[f"{self.tag_prefix}:contract_name"] = contract["name"]

        # Domain
        if metadata.get("domain"):
            tags[f"{self.tag_prefix}:domain"] = metadata["domain"]

        # Owner/team
        if metadata.get("owner"):
            tags[f"{self.tag_prefix}:owner"] = metadata["owner"]

        # Version
        if metadata.get("version"):
            tags[f"{self.tag_prefix}:version"] = metadata["version"]

        # Environment
        if metadata.get("environment"):
            tags[f"{self.tag_prefix}:environment"] = metadata["environment"]

        return tags

    def _extract_governance_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """Extract governance and compliance tags."""
        tags = {}
        metadata = contract.get("metadata", {})

        # Data classification
        if metadata.get("classification"):
            tags[f"{self.tag_prefix}:classification"] = metadata["classification"]

        # PII/sensitive data
        if metadata.get("contains_pii"):
            tags[f"{self.tag_prefix}:contains_pii"] = str(metadata["contains_pii"]).lower()

        # Compliance requirements
        if metadata.get("compliance"):
            compliance_list = metadata["compliance"]
            if isinstance(compliance_list, list):
                tags[f"{self.tag_prefix}:compliance"] = ",".join(compliance_list)
            else:
                tags[f"{self.tag_prefix}:compliance"] = str(compliance_list)

        # Data retention
        if metadata.get("retention_days"):
            tags[f"{self.tag_prefix}:retention_days"] = str(metadata["retention_days"])

        return tags

    def _extract_exposure_tags(self, exposure: Dict[str, Any]) -> Dict[str, str]:
        """Extract exposure-specific tags."""
        tags = {}

        # Exposure ID
        if exposure.get("id"):
            tags[f"{self.tag_prefix}:exposure_id"] = exposure["id"]

        # Exposure type
        if exposure.get("type"):
            tags[f"{self.tag_prefix}:exposure_type"] = exposure["type"]

        # Target platform
        if exposure.get("target"):
            tags[f"{self.tag_prefix}:target"] = exposure["target"]

        return tags

    def _extract_policy_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """Extract policy and access control tags."""
        tags = {}
        policy = contract.get("policy", {})

        # Access level
        if policy.get("access_level"):
            tags[f"{self.tag_prefix}:access_level"] = policy["access_level"]

        # Allowed groups
        if policy.get("allowed_groups"):
            groups = policy["allowed_groups"]
            if isinstance(groups, list):
                tags[f"{self.tag_prefix}:allowed_groups"] = ",".join(groups)

        # Encryption required
        if policy.get("encryption_required"):
            tags[f"{self.tag_prefix}:encryption_required"] = str(
                policy["encryption_required"]
            ).lower()

        return tags

    def _extract_lineage_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """Extract data lineage tags."""
        tags = {}

        # Upstream dependencies
        dependencies = contract.get("dependencies", [])
        if dependencies:
            dep_ids = [
                dep.get("id") or dep.get("name") for dep in dependencies if isinstance(dep, dict)
            ]
            dep_ids = [d for d in dep_ids if d]  # Filter None
            if dep_ids:
                tags[f"{self.tag_prefix}:upstream"] = ",".join(dep_ids[:5])  # Limit to 5

        # Data sources
        sources = contract.get("sources", [])
        if sources:
            source_ids = [s.get("id") or s.get("name") for s in sources if isinstance(s, dict)]
            source_ids = [s for s in source_ids if s]
            if source_ids:
                tags[f"{self.tag_prefix}:sources"] = ",".join(source_ids[:5])

        return tags

    def _extract_sovereignty_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """
        Extract sovereignty metadata tags (NEW in FLUID 0.7.1).

        Extracts data jurisdiction and residency constraints for GDPR,
        CCPA, and regional data protection compliance.
        """
        from .sovereignty import extract_sovereignty_tags

        return extract_sovereignty_tags(contract)

    def _extract_agent_policy_tags(
        self, contract: Dict[str, Any], exposure: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """
        Extract agentPolicy metadata tags (NEW in FLUID 0.7.1).

        Extracts AI/LLM usage policies for agentic governance of data products.
        """
        from .agent_policy import extract_agent_policy_tags

        return extract_agent_policy_tags(contract, exposure)

    def _sanitize_tags(self, tags: Dict[str, str]) -> Dict[str, str]:
        """
        Sanitize tags to meet AWS requirements.

        - Max 50 tags
        - Keys up to 128 characters
        - Values up to 256 characters
        - Remove None/empty values
        """
        sanitized = {}

        for key, value in tags.items():
            # Skip None or empty
            if value is None or value == "":
                continue

            # Convert to string
            value_str = str(value)

            # Truncate key
            if len(key) > 128:
                key = key[:128]

            # Truncate value
            if len(value_str) > 256:
                value_str = value_str[:256]

            sanitized[key] = value_str

        # Enforce 50 tag limit
        if len(sanitized) > 50:
            # Keep first 50
            items = list(sanitized.items())[:50]
            sanitized = dict(items)

        return sanitized

    def to_aws_tag_list(self, tags: Dict[str, str]) -> List[Dict[str, str]]:
        """
        Convert tag dict to AWS tag list format.

        Args:
            tags: Dict of tags

        Returns:
            List of {"Key": "...", "Value": "..."} dicts
        """
        return [{"Key": k, "Value": v} for k, v in tags.items()]

    def from_aws_tag_list(self, tag_list: List[Dict[str, str]]) -> Dict[str, str]:
        """
        Convert AWS tag list to dict format.

        Args:
            tag_list: List of {"Key": "...", "Value": "..."} dicts

        Returns:
            Dict of tags
        """
        return {tag["Key"]: tag["Value"] for tag in tag_list}


class TagManager:
    """
    Manage tags across AWS resources.

    Provides utilities for:
    - Tag propagation
    - Tag inheritance
    - Cost allocation tags
    """

    def __init__(self, boto3_session):
        """
        Initialize tag manager.

        Args:
            boto3_session: boto3.Session instance
        """
        self.session = boto3_session
        self.extractor = MetadataExtractor()

    def tag_resource(
        self, resource_arn: str, tags: Dict[str, str], service_name: Optional[str] = None
    ) -> None:
        """
        Apply tags to AWS resource.

        Args:
            resource_arn: ARN of resource to tag
            tags: Dict of tags
            service_name: AWS service name (auto-detected if None)
        """
        if not service_name:
            service_name = self._detect_service(resource_arn)

        tag_list = self.extractor.to_aws_tag_list(tags)

        # Get appropriate client
        client = self.session.client(service_name)

        # Tag resource (API varies by service)
        if service_name == "s3":
            # S3 uses different API
            bucket = self._extract_bucket_name(resource_arn)
            client.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": tag_list})
        else:
            # Most services use tag_resource
            client.tag_resource(ResourceName=resource_arn, Tags=tag_list)

    def get_resource_tags(
        self, resource_arn: str, service_name: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Get tags for AWS resource.

        Args:
            resource_arn: ARN of resource
            service_name: AWS service name (auto-detected if None)

        Returns:
            Dict of tags
        """
        if not service_name:
            service_name = self._detect_service(resource_arn)

        client = self.session.client(service_name)

        # Get tags (API varies by service)
        if service_name == "s3":
            bucket = self._extract_bucket_name(resource_arn)
            response = client.get_bucket_tagging(Bucket=bucket)
            tag_list = response.get("TagSet", [])
        else:
            response = client.list_tags_for_resource(ResourceName=resource_arn)
            tag_list = response.get("Tags", [])

        return self.extractor.from_aws_tag_list(tag_list)

    def propagate_tags(self, source_arn: str, target_arn: str, overwrite: bool = False) -> None:
        """
        Copy tags from source to target resource.

        Args:
            source_arn: ARN of source resource
            target_arn: ARN of target resource
            overwrite: Overwrite existing tags on target
        """
        source_tags = self.get_resource_tags(source_arn)

        if not overwrite:
            # Merge with existing tags
            target_tags = self.get_resource_tags(target_arn)
            source_tags = {**source_tags, **target_tags}

        self.tag_resource(target_arn, source_tags)

    def _detect_service(self, resource_arn: str) -> str:
        """
        Detect AWS service from ARN.

        Args:
            resource_arn: Resource ARN

        Returns:
            Service name
        """
        # ARN format: arn:aws:service:region:account:resource
        parts = resource_arn.split(":")
        if len(parts) >= 3:
            return parts[2]

        raise ValueError(f"Invalid ARN format: {resource_arn}")

    def _extract_bucket_name(self, resource_arn: str) -> str:
        """
        Extract S3 bucket name from ARN.

        Args:
            resource_arn: S3 bucket ARN

        Returns:
            Bucket name
        """
        # ARN format: arn:aws:s3:::bucket-name
        parts = resource_arn.split(":")
        if len(parts) >= 6:
            return parts[5]

        raise ValueError(f"Invalid S3 ARN format: {resource_arn}")


# Global extractor instance
_extractor = MetadataExtractor()


def extract_tags(
    contract: Dict[str, Any], exposure: Optional[Dict[str, Any]] = None, **kwargs
) -> Dict[str, str]:
    """
    Extract AWS tags from FLUID contract.

    Args:
        contract: FLUID data contract
        exposure: Specific exposure
        **kwargs: Additional tag parameters

    Returns:
        Dict of AWS tags
    """
    return _extractor.extract_resource_tags(contract, exposure, kwargs)


def to_tag_list(tags: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Convert tag dict to AWS tag list format.

    Args:
        tags: Dict of tags

    Returns:
        List of {"Key": "...", "Value": "..."} dicts
    """
    return _extractor.to_aws_tag_list(tags)
