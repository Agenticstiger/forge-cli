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

# fluid_build/providers/snowflake/util/metadata.py
"""
Snowflake metadata extraction utilities.

Mirrors GCP's label extraction approach for Snowflake tags and metadata.
"""

import re
from typing import Any, Dict


def extract_snowflake_tags(contract: Dict[str, Any], exposure: Dict[str, Any]) -> Dict[str, str]:
    """
    Extract Snowflake tags from contract and exposure metadata.

    Mirrors GCP's 8-source label extraction for consistency:
    1. Contract ID/name
    2. Metadata (layer, domain, owner)
    3. Contract custom labels → tags
    4. Contract tags → Snowflake tags
    5. Exposure labels → tags
    6. Exposure tags → Snowflake tags
    7. Policy classification/authn
    8. Policy labels/tags

    Args:
        contract: FLUID contract dict
        exposure: Exposure specification dict

    Returns:
        Dictionary of Snowflake tag name → value mappings
    """
    tags = {}

    def sanitize_tag_key(key: str) -> str:
        """Snowflake tag names: alphanumeric + underscore only."""
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", key.upper())
        if sanitized and not sanitized[0].isalpha():
            sanitized = f"TAG_{sanitized}"
        return sanitized[:256]  # Snowflake max identifier length

    def sanitize_tag_value(value: str) -> str:
        """Snowflake tag values: escape single quotes."""
        return str(value).replace("'", "''")[:256]

    # 1. Contract-level metadata
    if contract.get("id"):
        tags["FLUID_CONTRACT_ID"] = sanitize_tag_value(contract["id"])
    if contract.get("name"):
        tags["FLUID_CONTRACT_NAME"] = sanitize_tag_value(contract["name"])

    metadata = contract.get("metadata", {})
    if metadata.get("layer"):
        tags["FLUID_LAYER"] = sanitize_tag_value(metadata["layer"])
    if metadata.get("domain"):
        tags["FLUID_DOMAIN"] = sanitize_tag_value(metadata.get("domain", ""))
    if metadata.get("owner", {}).get("team"):
        tags["FLUID_TEAM"] = sanitize_tag_value(metadata["owner"]["team"])

    # 2. Contract custom labels → tags
    for key, value in contract.get("labels", {}).items():
        sanitized_key = sanitize_tag_key(key)
        if sanitized_key:
            tags[sanitized_key] = sanitize_tag_value(str(value))

    # 3. Contract tags → Snowflake tags
    for tag in contract.get("tags", []):
        safe_tag = sanitize_tag_key(tag)
        if safe_tag:
            tags[f"TAG_{safe_tag}"] = "true"

    # 4. Exposure-level labels → tags
    for key, value in exposure.get("labels", {}).items():
        sanitized_key = sanitize_tag_key(key)
        if sanitized_key:
            tags[sanitized_key] = sanitize_tag_value(str(value))

    # 5. Exposure tags → Snowflake tags
    for tag in exposure.get("tags", []):
        safe_tag = sanitize_tag_key(tag)
        if safe_tag:
            tags[f"TAG_{safe_tag}"] = "true"

    # 6. Policy governance metadata
    policy = exposure.get("policy", {})
    if policy.get("classification"):
        tags["DATA_CLASSIFICATION"] = sanitize_tag_value(policy["classification"])
    if policy.get("authn"):
        tags["AUTHN_METHOD"] = sanitize_tag_value(policy["authn"])

    # 7. Policy labels → tags
    for key, value in policy.get("labels", {}).items():
        sanitized_key = sanitize_tag_key(f"POLICY_{key}")
        if sanitized_key:
            tags[sanitized_key] = sanitize_tag_value(str(value))

    # 8. Policy tags → Snowflake tags
    for tag in policy.get("tags", []):
        safe_tag = sanitize_tag_key(tag)
        if safe_tag:
            tags[f"POLICY_{safe_tag}"] = "true"

    # 9. Sovereignty metadata (NEW in 0.7.1)
    sovereignty = contract.get("sovereignty", {})
    if sovereignty.get("jurisdiction"):
        tags["DATA_JURISDICTION"] = sanitize_tag_value(sovereignty["jurisdiction"])
    if sovereignty.get("dataResidency"):
        tags["DATA_RESIDENCY"] = "enforced"

    # Sovereignty tags
    for tag in sovereignty.get("tags", []):
        safe_tag = sanitize_tag_key(tag)
        if safe_tag:
            tags[f"SOVEREIGNTY_{safe_tag}"] = "true"

    return tags


def extract_column_tags(field: Dict[str, Any]) -> Dict[str, str]:
    """
    Extract Snowflake tags from column/field labels.

    Supports:
    - Direct Snowflake tags (snowflakeTag/tagValue)
    - GCP-style policy tags (mapped to Snowflake)
    - Classification and sensitivity labels
    - PII/PHI indicators

    Args:
        field: Field specification from contract schema

    Returns:
        Dictionary of tag name → value for this column
    """
    labels = field.get("labels", {})
    tags = {}

    def sanitize_tag_key(key: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", key.upper())[:256]

    def sanitize_tag_value(value: str) -> str:
        return str(value).replace("'", "''")[:256]

    # Direct Snowflake tag
    if "snowflakeTag" in labels:
        tag_name = sanitize_tag_key(labels["snowflakeTag"])
        tag_value = sanitize_tag_value(labels.get("tagValue", "true"))
        tags[tag_name] = tag_value

    # GCP-style policy tag (map to Snowflake)
    if "policyTag" in labels:
        policy_tag = labels["policyTag"]
        taxonomy = labels.get("taxonomy", "default")
        tag_name = sanitize_tag_key(f"{taxonomy}_{policy_tag}")
        tags[tag_name] = "true"

    # Data classification
    if "classification" in labels:
        tags["DATA_CLASSIFICATION"] = sanitize_tag_value(labels["classification"])

    # Sensitivity level (from FLUID schema)
    sensitivity = field.get("sensitivity")
    if sensitivity:
        tags["SENSITIVITY"] = sanitize_tag_value(sensitivity)

    # Semantic type
    semantic_type = field.get("semanticType")
    if semantic_type:
        tags["SEMANTIC_TYPE"] = sanitize_tag_value(semantic_type)

    # PII indicator
    if labels.get("pii") == "true" or labels.get("contains_pii") == "true":
        tags["PII"] = "true"

    # PHI indicator
    if labels.get("phi") == "true" or labels.get("contains_phi") == "true":
        tags["PHI"] = "true"

    # Currency indicator (financial data)
    if labels.get("currency"):
        tags["CURRENCY"] = sanitize_tag_value(labels["currency"])

    # Constraint hints
    if labels.get("constraint") == "primary_key":
        tags["PRIMARY_KEY"] = "true"
    if labels.get("unique") == "true":
        tags["UNIQUE_CONSTRAINT"] = "true"

    return tags
