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

from ._common import iter_exposes


class EnforcementMode(Enum):
    """Sovereignty enforcement modes."""

    STRICT = "strict"  # Block deployment on violation
    ADVISORY = "advisory"  # Warn only, allow deployment
    AUDIT = "audit"  # Log for compliance tracking


# Single source of truth for the sovereignty-block defaults, mirroring the
# ``default`` keys the JSON schema declares for ``$defs.sovereignty``. Anything
# that needs to display or apply a default reads these, so the value used to
# decide and the value shown to the operator cannot drift apart.
# ``tests/test_sovereignty.py`` pins them against the bundled schema.
DEFAULT_ENFORCEMENT_MODE = "strict"
DEFAULT_DATA_RESIDENCY = True
DEFAULT_CROSS_BORDER_TRANSFER = False

# Distinct from ``None``, which the jurisdiction map also returns for an
# unmapped region. Check 4 needs "no baseline yet" and "jurisdiction unknown"
# to be different states — see the comment there.
_UNSET = object()


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

        # Defaults MUST mirror the JSON schema's declared ``default`` keys
        # (``$defs.sovereignty`` in fluid-schema-0.7.x.json). They previously
        # did not — every one was the permissive inverse of what the schema
        # advertises — so a contract that declared a policy and relied on the
        # documented defaults was evaluated under the weakest possible
        # settings. A GDPR contract with exposes straddling EU and US printed
        # ``PASS`` because ``dataResidency`` silently became False and Check 4
        # was never entered. Same fail-open class as the empty-hook bug: the
        # control is present, and quietly does nothing.
        enforcement_mode = EnforcementMode(
            sovereignty.get("enforcementMode", DEFAULT_ENFORCEMENT_MODE)
        )
        allowed_regions = sovereignty.get("allowedRegions", [])
        denied_regions = sovereignty.get("deniedRegions", [])
        jurisdiction = sovereignty.get("jurisdiction")
        data_residency = sovereignty.get("dataResidency", DEFAULT_DATA_RESIDENCY)
        cross_border_transfer = sovereignty.get(
            "crossBorderTransfer", DEFAULT_CROSS_BORDER_TRANSFER
        )

        # Validate each expose's binding location
        for expose in iter_exposes(contract):
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

        # Check 4: Data residency and cross-border transfer.
        #
        # Hoisted OUT of the per-expose loop — it is a property of the contract
        # as a whole, and running it per-expose emitted one duplicate error per
        # expose.
        #
        # The subtlety this code exists to get right: ``None`` used to mean two
        # different things — "no baseline yet" and "this region is not in
        # REGION_JURISDICTION_MAP". Conflating them made the check both
        # over- and under-sensitive, and its verdict depended on expose order:
        #   * eu-west-1 + eu-south-1 (both EU, the latter unmapped) -> BLOCKED
        #   * the same two, reversed                                -> passed
        #   * me-central-1 + us-gov-west-1 (both unmapped)          -> passed,
        #     a genuine cross-border transfer reported as clean.
        # The map holds 31 entries and will always lag AWS/GCP/Azure, so
        # "unknown" is a real, common state that needs its own answer rather
        # than being silently folded into a jurisdiction comparison.
        if data_residency and not cross_border_transfer:
            baseline: Any = _UNSET
            for exp in iter_exposes(contract):
                exp_region = exp.get("binding", {}).get("location", {}).get("region")
                if not exp_region:
                    continue
                exp_id = exp.get("exposeId", "unknown")
                exp_jurisdiction = self.REGION_JURISDICTION_MAP.get(exp_region, "Unknown")

                if exp_jurisdiction == "Unknown":
                    # Say what we actually know. Warning severity, so an
                    # unmapped-but-legitimate region does not block a
                    # deployment on the strength of a gap in our own table.
                    violations.append(
                        SovereigntyViolation(
                            severity="warning",
                            message=(
                                f"Region '{exp_region}' has no known jurisdiction — "
                                f"cross-border transfer cannot be verified for this expose"
                            ),
                            expose_id=exp_id,
                            region_found=exp_region,
                            suggestion=(
                                "Add this region to the jurisdiction map, or pin residency "
                                "explicitly with sovereignty.allowedRegions"
                            ),
                        )
                    )
                    # Never seeds or trips the baseline — an unknown must not
                    # masquerade as agreement with another unknown.
                    continue

                if baseline is _UNSET:
                    baseline = exp_jurisdiction
                elif exp_jurisdiction != baseline:
                    violations.append(
                        SovereigntyViolation(
                            severity="error",
                            message=(
                                "Cross-border data transfer prohibited but multiple "
                                f"jurisdictions detected ({baseline} and {exp_jurisdiction})"
                            ),
                            expose_id=exp_id,
                            region_found=exp_region,
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
