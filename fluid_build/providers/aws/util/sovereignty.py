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
            "dataResidency": true,
            "allowedRegions": ["eu-west-1", "eu-central-1"],
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
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# Re-export the typed catalog class so the AWS sovereignty validator
# raises the SAME `SovereigntyViolationError` identity used elsewhere in
# the codebase. Existing imports of this symbol stay valid; downstream
# `except SovereigntyViolationError` blocks now see the typed Panel.
from fluid_build.cli._errors import SovereigntyViolationError  # noqa: E402,F401

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
                what=f"Unknown AWS region '{region}' — cannot determine jurisdiction",
                why=f"region '{region}' is not in REGION_JURISDICTIONS; the deploy target's jurisdiction can't be verified.",
                fix="Use an AWS region from the supported list, or update REGION_JURISDICTIONS to map this region.",
                doc="https://forge.fluid.dev/ref/sovereignty",
                extras={"region": region},
            )

        # Check if jurisdiction matches
        if actual_jurisdiction != required_jurisdiction:
            raise SovereigntyViolationError.for_connector(
                connector=f"aws:{region}",
                jurisdiction=str(required_jurisdiction),
            )

    @staticmethod
    def _allowed_regions(sovereignty: Dict[str, Any]) -> List[str]:
        """The region allow-list, read from the key that actually holds it.

        ``allowedRegions`` is a real sibling key of ``dataResidency`` in every
        0.7.x schema. This used to read the allow-list out of ``dataResidency``
        itself, which is a *boolean* — see :meth:`_validate_data_residency`.

        A non-boolean ``dataResidency`` is not schema-valid, but two such shapes
        exist in the wild — a list (this module's own former docstring and a
        stale fixture) and a dict (what ``cli/init_scan.py`` emits). They are
        read **only as a fallback when ``allowedRegions`` is absent**, never
        merged into it.

        Precedence, not union, and deliberately so. Honouring a lone legacy
        value is right: ignoring it would turn a stated constraint into no
        constraint at all. But once ``allowedRegions`` is present there is no
        such risk — a valid constraint already exists, and every region the
        invalid key could contribute is by construction one the valid
        allow-list deliberately excluded. Unioning them would let a
        schema-invalid field silently *widen* a policy that a reviewer, an OPA
        gate or a marketplace facet reads from ``allowedRegions`` alone.
        """
        allowed = sovereignty.get("allowedRegions") or []
        if allowed:
            return [str(r) for r in allowed]

        legacy = sovereignty.get("dataResidency")
        if isinstance(legacy, (list, tuple, set)):
            regions = [str(r) for r in legacy]
        elif isinstance(legacy, dict):
            # ``{"allowedRegions": [...]}`` — the init_scan shape. Reading it as
            # a plain container tested the dict's KEYS, so a legitimate region
            # was refused and the tag value came out as the literal string
            # "allowedRegions".
            regions = [str(r) for r in (legacy.get("allowedRegions") or [])]
        else:
            return []

        if regions:
            logger.warning(
                "sovereignty.dataResidency carries a region list; the schema types it as a "
                "boolean and puts the allow-list in sovereignty.allowedRegions. Honouring it "
                "as a fallback — move these regions to allowedRegions."
            )
        return regions

    def _validate_data_residency(self, sovereignty: Dict[str, Any], region: str) -> None:
        """
        Validate data residency constraints.

        ``dataResidency`` is a **boolean** — "must this data stay inside the
        declared jurisdiction?" — not a list of regions. Reading it as a list
        meant the documented, schema-default, example-endorsed ``true`` blew up
        with ``TypeError: argument of type 'bool' is not a container or
        iterable``, while ``false`` quietly disabled the check. The strict
        setting was the one that broke and the permissive one was the one that
        worked, which is exactly backwards for a governance control.

        The regions themselves come from ``allowedRegions`` / ``deniedRegions``,
        and **neither is gated on the boolean**. ``allowedRegions`` is a
        standalone constraint: the canonical engine
        (``policy/sovereignty.py``, which ``fluid validate`` runs) enforces it
        with a bare ``if allowed_regions and region not in allowed_regions``,
        and gating it here would make the AWS provider quietly more permissive
        than the stage before it. It would also invert the schema's own
        ``default: true`` — an omitted ``dataResidency`` reads as falsy in
        Python, so "unspecified" would have meant "opt out" for the very field
        whose default is the strict setting.

        Args:
            sovereignty: Sovereignty configuration
            region: AWS region

        Raises:
            SovereigntyViolationError: If data residency is violated
        """
        from fluid_build.cli._errors import ResidencyViolationError

        allowed_regions = self._allowed_regions(sovereignty)

        # Deny beats allow, and is never gated — the schema is explicit that
        # deniedRegions takes precedence over allowedRegions.
        denied = [str(r) for r in (sovereignty.get("deniedRegions") or [])]
        if region in denied:
            raise ResidencyViolationError.for_transfer(
                from_region=str(region),
                to_region="<denied>",
                jurisdiction=", ".join(allowed_regions) or "<none declared>",
            )

        if not allowed_regions:
            # No allow-list declared; the jurisdiction check is the binding
            # constraint for this contract.
            return

        if region not in allowed_regions:
            raise ResidencyViolationError.for_transfer(
                from_region=str(region),
                to_region="<denied>",
                jurisdiction=", ".join(allowed_regions),
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

        # Data residency enforcement tag. ``dataResidency`` is a boolean, so it
        # says *whether* residency is enforced; the regions come from
        # ``allowedRegions``. Joining the boolean itself raised
        # ``TypeError: can only join an iterable``, and joining a dict silently
        # emitted its keys — one cloud tag literally read
        # ``fluid:allowed_regions = "allowedRegions"``.
        #
        # The default comes from the policy engine rather than a bare
        # ``.get(...)``, so an omitted key tags as the schema says it behaves
        # (``default: true``). Getting this wrong is not cosmetic: detective
        # controls downstream — an AWS Config rule, a tag-based SCP — key on
        # these tags, so a missing tag silently disarms them.
        from fluid_build.policy.sovereignty import DEFAULT_DATA_RESIDENCY

        residency = sovereignty.get("dataResidency", DEFAULT_DATA_RESIDENCY)
        allowed = self._allowed_regions(sovereignty)
        if residency or allowed:
            tags["fluid:data_residency"] = "enforced" if residency else "regions-pinned"
            if allowed:
                tags["fluid:allowed_regions"] = sanitize_tag_value(",".join(allowed))

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
