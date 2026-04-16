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
FLUID 0.7.1 Sovereignty Validator

Validates data sovereignty constraints against infrastructure bindings.
Prevents deployment of contracts that violate jurisdiction requirements.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class EnforcementMode(Enum):
    """Sovereignty enforcement modes."""

    STRICT = "strict"  # Block deployment on violation
    ADVISORY = "advisory"  # Warn only, allow deployment
    AUDIT = "audit"  # Log for compliance tracking


@dataclass
class SovereigntyViolation:
    """Represents a sovereignty constraint violation."""

    severity: str  # "error", "warning", "info"
    message: str
    expose_id: Optional[str] = None
    region_found: Optional[str] = None
    region_expected: Optional[List[str]] = None
    suggestion: Optional[str] = None


class SovereigntyValidator:
    """Validates sovereignty constraints in FLUID 0.7.1 contracts."""

    # Region → Jurisdiction mapping (extensible)
    REGION_JURISDICTION_MAP = {
        # AWS regions
        "us-east-1": "US",
        "us-east-2": "US",
        "us-west-1": "US",
        "us-west-2": "US",
        "eu-west-1": "EU",
        "eu-west-2": "EU",
        "eu-west-3": "EU",
        "eu-central-1": "EU",
        "eu-north-1": "EU",
        "ca-central-1": "CA",
        "ap-southeast-1": "Global",
        "ap-southeast-2": "AU",
        "ap-northeast-1": "JP",
        "ap-northeast-2": "Global",
        "sa-east-1": "BR",
        # GCP regions
        "us-central1": "US",
        "us-east1": "US",
        "us-west1": "US",
        "europe-west1": "EU",
        "europe-west2": "EU",
        "europe-west3": "EU",
        "europe-west4": "EU",
        "asia-southeast1": "Global",
        "asia-northeast1": "JP",
        # Azure regions
        "eastus": "US",
        "eastus2": "US",
        "westus": "US",
        "westus2": "US",
        "westeurope": "EU",
        "northeurope": "EU",
        "canadacentral": "CA",
    }

    def validate(self, contract: Dict[str, Any]) -> Tuple[bool, List[SovereigntyViolation]]:
        """
        Validate sovereignty constraints.

        Returns:
            (is_valid, violations) - is_valid=False means BLOCK deployment in strict mode
        """
        violations = []

        # Extract sovereignty config (optional in 0.7.1)
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            return True, []  # No sovereignty constraints = always valid

        enforcement_mode = EnforcementMode(sovereignty.get("enforcementMode", "advisory"))
        allowed_regions = sovereignty.get("allowedRegions", [])
        denied_regions = sovereignty.get("deniedRegions", [])
        jurisdiction = sovereignty.get("jurisdiction")
        data_residency = sovereignty.get("dataResidency", False)
        cross_border_transfer = sovereignty.get("crossBorderTransfer", True)

        # Validate each expose's binding location
        for expose in contract.get("exposes", []):
            binding = expose.get("binding", {})
            location = binding.get("location", {})
            region = location.get("region")

            if not region:
                continue  # No region specified, skip validation

            expose_id = expose.get("exposeId", "unknown")

            # Check 1: Denied regions (always enforced regardless of mode)
            if region in denied_regions:
                violations.append(
                    SovereigntyViolation(
                        severity="error",
                        message=f"Region '{region}' is explicitly denied by sovereignty policy",
                        expose_id=expose_id,
                        region_found=region,
                        region_expected=allowed_regions,
                        suggestion=f"Use one of the allowed regions: {', '.join(allowed_regions) if allowed_regions else 'none specified'}",
                    )
                )

            # Check 2: Allowed regions (if specified)
            if allowed_regions and region not in allowed_regions:
                severity = "error" if enforcement_mode == EnforcementMode.STRICT else "warning"
                violations.append(
                    SovereigntyViolation(
                        severity=severity,
                        message=f"Region '{region}' not in allowed regions list",
                        expose_id=expose_id,
                        region_found=region,
                        region_expected=allowed_regions,
                        suggestion=f"Allowed regions: {', '.join(allowed_regions)}",
                    )
                )

            # Check 3: Jurisdiction match
            if jurisdiction and jurisdiction != "Global":
                region_jurisdiction = self.REGION_JURISDICTION_MAP.get(region, "Unknown")
                if region_jurisdiction != jurisdiction and region_jurisdiction != "Global":
                    violations.append(
                        SovereigntyViolation(
                            severity="warning",
                            message=f"Region '{region}' (jurisdiction: {region_jurisdiction}) "
                            f"does not match required jurisdiction: {jurisdiction}",
                            expose_id=expose_id,
                            suggestion=f"Consider using regions in {jurisdiction} jurisdiction",
                        )
                    )

            # Check 4: Data residency and cross-border transfer
            if data_residency and not cross_border_transfer:
                # All regions must be in same jurisdiction
                first_region_jurisdiction = None
                for exp in contract.get("exposes", []):
                    exp_region = exp.get("binding", {}).get("location", {}).get("region")
                    if exp_region:
                        exp_jurisdiction = self.REGION_JURISDICTION_MAP.get(exp_region)
                        if first_region_jurisdiction is None:
                            first_region_jurisdiction = exp_jurisdiction
                        elif exp_jurisdiction != first_region_jurisdiction:
                            violations.append(
                                SovereigntyViolation(
                                    severity="error",
                                    message="Cross-border data transfer prohibited but multiple jurisdictions detected",
                                    expose_id=expose_id,
                                    suggestion="Ensure all regions are within the same jurisdiction when crossBorderTransfer=false",
                                )
                            )
                            break

        # Determine final validity based on enforcement mode
        has_errors = any(v.severity == "error" for v in violations)

        if enforcement_mode == EnforcementMode.STRICT:
            is_valid = not has_errors
        elif enforcement_mode == EnforcementMode.ADVISORY:
            is_valid = True  # Warnings only, allow deployment
        else:  # AUDIT
            is_valid = True  # Log only, allow deployment

        return is_valid, violations


def validate_sovereignty(contract: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Convenience function for CLI integration.

    Returns:
        (is_valid, error_messages)
    """
    validator = SovereigntyValidator()
    is_valid, violations = validator.validate(contract)

    messages = []
    for v in violations:
        prefix = "❌" if v.severity == "error" else "⚠️" if v.severity == "warning" else "ℹ️"
        msg = f"{prefix} [{v.expose_id}] {v.message}"
        if v.suggestion:
            msg += f"\n   💡 {v.suggestion}"
        messages.append(msg)

    return is_valid, messages


def get_region_jurisdiction(region: str) -> str:
    """
    Get jurisdiction for a region.

    Args:
        region: Cloud region identifier

    Returns:
        Jurisdiction code (EU, US, etc.) or "Unknown"
    """
    return SovereigntyValidator.REGION_JURISDICTION_MAP.get(region, "Unknown")
