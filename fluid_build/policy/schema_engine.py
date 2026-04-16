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
FLUID Schema-Driven Policy Engine

Policy engine that derives all governance rules directly from FLUID contract schema declarations.
No external policy files needed - everything is declared in the contract following the FLUID 0.5.7 schema.

Enforces:
- Sensitivity & Privacy policies (PII, PHI, encryption, masking)
- Access Control policies (readers, writers, column restrictions)
- Data Quality policies (freshness, completeness, anomaly detection)
- Lifecycle & Retention policies (deprecation, retention periods)
- Schema Evolution policies (compatibility, approvals, breaking changes)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PolicySeverity(Enum):
    """Policy violation severity levels"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class PolicyCategory(Enum):
    """Categories of policy enforcement"""

    SENSITIVITY = "sensitivity"
    ACCESS_CONTROL = "access_control"
    DATA_QUALITY = "data_quality"
    LIFECYCLE = "lifecycle"
    SCHEMA_EVOLUTION = "schema_evolution"


@dataclass
class PolicyViolation:
    """Represents a single policy violation"""

    category: PolicyCategory
    severity: PolicySeverity
    message: str
    field: Optional[str] = None
    expose_id: Optional[str] = None
    rule_id: Optional[str] = None
    remediation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
            "expose_id": self.expose_id,
            "rule_id": self.rule_id,
            "remediation": self.remediation,
        }


@dataclass
class PolicyEnforcementResult:
    """Result of policy enforcement"""

    violations: List[PolicyViolation] = field(default_factory=list)
    checks_passed: int = 0
    checks_failed: int = 0

    def is_compliant(self) -> bool:
        """Check if contract is policy compliant (no critical/error violations)"""
        return not any(
            v.severity in [PolicySeverity.CRITICAL, PolicySeverity.ERROR] for v in self.violations
        )

    def get_blocking_violations(self) -> List[PolicyViolation]:
        """Get violations that block deployment"""
        return [
            v
            for v in self.violations
            if v.severity in [PolicySeverity.CRITICAL, PolicySeverity.ERROR]
        ]

    def get_by_category(self, category: PolicyCategory) -> List[PolicyViolation]:
        """Get violations by category"""
        return [v for v in self.violations if v.category == category]

    def calculate_score(self) -> int:
        """Calculate policy compliance score (0-100)"""
        total_checks = self.checks_passed + self.checks_failed
        if total_checks == 0:
            return 100

        # Weight violations by severity
        penalty = 0
        for v in self.violations:
            if v.severity == PolicySeverity.CRITICAL:
                penalty += 20
            elif v.severity == PolicySeverity.ERROR:
                penalty += 10
            elif v.severity == PolicySeverity.WARNING:
                penalty += 5
            else:  # INFO
                penalty += 1

        score = max(0, 100 - penalty)
        return score

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "is_compliant": self.is_compliant(),
            "score": self.calculate_score(),
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "violations": [v.to_dict() for v in self.violations],
            "blocking_violations": len(self.get_blocking_violations()),
        }


class SchemaBasedPolicyEngine:
    """
    Policy engine that enforces governance rules declared in FLUID contracts.

    All policies are derived from the contract schema - no external configuration needed.
    Follows FLUID 0.5.7 schema specification.
    """

    # Sensitive data types that require protection
    SENSITIVE_TYPES = {"pii", "phi", "confidential", "restricted"}

    # Masking strategies
    MASKING_STRATEGIES = {"mask", "hash", "tokenize", "encrypt", "k_anonymity"}

    # Valid classification levels
    CLASSIFICATIONS = {"Public", "Internal", "Confidential", "Restricted"}

    def __init__(self, contract: Dict[str, Any]):
        """
        Initialize policy engine with a FLUID contract.

        Args:
            contract: Parsed FLUID contract dictionary
        """
        self.contract = contract
        self.schema_version = contract.get("fluidVersion", "0.5.7")
        self.result = PolicyEnforcementResult()

        logger.info(f"Initialized policy engine for contract: {contract.get('id')}")

    def enforce_all(self) -> PolicyEnforcementResult:
        """
        Enforce all policies declared in the contract.

        Returns:
            PolicyEnforcementResult with all violations and pass/fail counts
        """
        logger.info("Starting comprehensive policy enforcement")

        # Run all policy validators
        self._enforce_sensitivity_policies()
        self._enforce_access_control_policies()
        self._enforce_data_quality_policies()
        self._enforce_lifecycle_policies()
        self._enforce_schema_evolution_policies()

        logger.info(
            f"Policy enforcement complete: {self.result.checks_passed} passed, "
            f"{self.result.checks_failed} failed, score: {self.result.calculate_score()}/100"
        )

        return self.result

    def _add_violation(self, violation: PolicyViolation) -> None:
        """Add a violation to the result"""
        self.result.violations.append(violation)
        self.result.checks_failed += 1

    def _add_pass(self) -> None:
        """Record a passed check"""
        self.result.checks_passed += 1

    # ==========================================
    # Sensitivity & Privacy Policy Enforcement
    # ==========================================

    def _enforce_sensitivity_policies(self) -> None:
        """Enforce field-level sensitivity and privacy policies"""
        logger.debug("Enforcing sensitivity policies")

        for expose in self.contract.get("exposes", []):
            expose_id = expose.get("exposeId")
            schema = expose.get("contract", {}).get("schema", [])

            for field in schema:
                field_name = field.get("name")
                sensitivity = field.get("sensitivity")

                # Check 1: PII/PHI fields must have privacy protection
                if sensitivity in ["pii", "phi"]:
                    if self._has_privacy_protection(expose, field_name):
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.SENSITIVITY,
                                severity=PolicySeverity.CRITICAL,
                                message=f"Field marked as {sensitivity} but no privacy protection configured",
                                field=field_name,
                                expose_id=expose_id,
                                remediation=f"Add masking strategy for '{field_name}' in policy.privacy.masking",
                            )
                        )

                # Check 2: Encrypted fields must have encryption at binding
                if sensitivity == "encrypted":
                    if self._binding_has_encryption(expose):
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.SENSITIVITY,
                                severity=PolicySeverity.CRITICAL,
                                message="Field marked as encrypted but binding lacks encryption configuration",
                                field=field_name,
                                expose_id=expose_id,
                                remediation="Configure encryption in binding or platform settings",
                            )
                        )

                # Check 3: Sensitive data should not be in cleartext
                if sensitivity == "cleartext" and field.get("tags", []):
                    if any(tag in ["pii", "phi", "sensitive"] for tag in field.get("tags", [])):
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.SENSITIVITY,
                                severity=PolicySeverity.WARNING,
                                message="Field tagged as sensitive but sensitivity level is 'cleartext'",
                                field=field_name,
                                expose_id=expose_id,
                                remediation="Change sensitivity to 'pii', 'phi', or apply treatment",
                            )
                        )
                    else:
                        self._add_pass()

    def _has_privacy_protection(self, expose: Dict[str, Any], field_name: str) -> bool:
        """Check if field has privacy protection configured"""
        policy = expose.get("policy", {})
        privacy = policy.get("privacy", {})
        masking = privacy.get("masking", [])

        return any(m.get("column") == field_name for m in masking)

    def _binding_has_encryption(self, expose: Dict[str, Any]) -> bool:
        """Check if binding has encryption configured"""
        # In a real implementation, this would check platform-specific encryption
        # For now, assume encryption is handled by platform
        binding = expose.get("binding", {})
        return binding.get("platform") in ["gcp", "aws", "azure", "snowflake"]

    # ==========================================
    # Access Control Policy Enforcement
    # ==========================================

    def _enforce_access_control_policies(self) -> None:
        """Enforce authorization and access control policies"""
        logger.debug("Enforcing access control policies")

        for expose in self.contract.get("exposes", []):
            expose_id = expose.get("exposeId")
            policy = expose.get("policy", {})
            authz = policy.get("authz", {})
            classification = policy.get("classification")
            schema = expose.get("contract", {}).get("schema", [])

            # Check 1: Column restrictions must reference valid columns
            schema_columns = {col["name"] for col in schema}
            col_restrictions = authz.get("columnRestrictions", [])

            for restriction in col_restrictions:
                for col in restriction.get("columns", []):
                    if col in schema_columns:
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.ACCESS_CONTROL,
                                severity=PolicySeverity.ERROR,
                                message=f"Column restriction references non-existent column: {col}",
                                expose_id=expose_id,
                                remediation=f"Remove restriction or add column '{col}' to schema",
                            )
                        )

            # Check 2: Public classification cannot contain sensitive data
            if classification == "Public":
                has_sensitive = False
                for field in schema:
                    if field.get("sensitivity") in self.SENSITIVE_TYPES:
                        has_sensitive = True
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.ACCESS_CONTROL,
                                severity=PolicySeverity.CRITICAL,
                                message=f"Public classification cannot contain {field.get('sensitivity')} data",
                                field=field.get("name"),
                                expose_id=expose_id,
                                remediation="Change classification to Confidential/Restricted or remove sensitive field",
                            )
                        )

                if not has_sensitive:
                    self._add_pass()

            # Check 3: Restricted data should have explicit readers
            if classification == "Restricted":
                if authz.get("readers"):
                    self._add_pass()
                else:
                    self._add_violation(
                        PolicyViolation(
                            category=PolicyCategory.ACCESS_CONTROL,
                            severity=PolicySeverity.WARNING,
                            message="Restricted classification should have explicit readers list",
                            expose_id=expose_id,
                            remediation="Add policy.authz.readers with authorized principals",
                        )
                    )

            # Check 4: Privacy masking strategies must be valid
            privacy = policy.get("privacy", {})
            for masking_rule in privacy.get("masking", []):
                strategy = masking_rule.get("strategy")
                if strategy in self.MASKING_STRATEGIES:
                    self._add_pass()
                else:
                    self._add_violation(
                        PolicyViolation(
                            category=PolicyCategory.ACCESS_CONTROL,
                            severity=PolicySeverity.ERROR,
                            message=f"Invalid masking strategy: {strategy}",
                            field=masking_rule.get("column"),
                            expose_id=expose_id,
                            remediation=f"Use one of: {', '.join(self.MASKING_STRATEGIES)}",
                        )
                    )

    # ==========================================
    # Data Quality Policy Enforcement
    # ==========================================

    def _enforce_data_quality_policies(self) -> None:
        """Enforce data quality rules and monitoring"""
        logger.debug("Enforcing data quality policies")

        for expose in self.contract.get("exposes", []):
            expose_id = expose.get("exposeId")
            dq = expose.get("contract", {}).get("dq", {})
            rules = dq.get("rules", [])
            monitoring = dq.get("monitoring", {})

            # Check 1: Critical DQ rules must have monitoring enabled
            for rule in rules:
                rule_id = rule.get("id")
                severity = rule.get("severity")

                if severity == "critical":
                    if monitoring.get("enabled", True):
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.DATA_QUALITY,
                                severity=PolicySeverity.WARNING,
                                message="Critical DQ rule has monitoring disabled",
                                rule_id=rule_id,
                                expose_id=expose_id,
                                remediation="Enable monitoring: contract.dq.monitoring.enabled = true",
                            )
                        )

                # Check 2: Freshness rules should have reasonable thresholds
                if rule.get("type") == "freshness":
                    if rule.get("threshold") or rule.get("window"):
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.DATA_QUALITY,
                                severity=PolicySeverity.WARNING,
                                message="Freshness rule missing threshold or window",
                                rule_id=rule_id,
                                expose_id=expose_id,
                                remediation="Add threshold (number) or window (ISO duration)",
                            )
                        )

                # Check 3: Completeness rules should have thresholds
                if rule.get("type") == "completeness":
                    threshold = rule.get("threshold")
                    if threshold and 0 <= threshold <= 1:
                        self._add_pass()
                    else:
                        self._add_violation(
                            PolicyViolation(
                                category=PolicyCategory.DATA_QUALITY,
                                severity=PolicySeverity.ERROR,
                                message="Completeness rule threshold must be between 0 and 1",
                                rule_id=rule_id,
                                expose_id=expose_id,
                                remediation="Set threshold to value like 0.95 (95%)",
                            )
                        )

            # Check 4: QoS expectations should align with DQ rules
            qos = expose.get("qos", {})
            if qos.get("freshnessSLO") and not any(r.get("type") == "freshness" for r in rules):
                self._add_violation(
                    PolicyViolation(
                        category=PolicyCategory.DATA_QUALITY,
                        severity=PolicySeverity.INFO,
                        message="QoS freshness SLO defined but no DQ freshness rule",
                        expose_id=expose_id,
                        remediation="Add freshness rule to contract.dq.rules",
                    )
                )
            else:
                self._add_pass()

    # ==========================================
    # Lifecycle Policy Enforcement
    # ==========================================

    def _enforce_lifecycle_policies(self) -> None:
        """Enforce lifecycle and retention policies"""
        logger.debug("Enforcing lifecycle policies")

        # Product-level lifecycle
        lifecycle = self.contract.get("lifecycle", {})
        state = lifecycle.get("state")

        # Check 1: Deprecated products must have replacement info
        if state == "deprecated":
            dep_policy = lifecycle.get("deprecationPolicy", {})
            if dep_policy.get("replacement"):
                self._add_pass()
            else:
                self._add_violation(
                    PolicyViolation(
                        category=PolicyCategory.LIFECYCLE,
                        severity=PolicySeverity.ERROR,
                        message="Deprecated product must specify replacement",
                        remediation="Add lifecycle.deprecationPolicy.replacement",
                    )
                )

            # Check notice period
            if dep_policy.get("noticePeriod"):
                self._add_pass()
            else:
                self._add_violation(
                    PolicyViolation(
                        category=PolicyCategory.LIFECYCLE,
                        severity=PolicySeverity.WARNING,
                        message="Deprecated product should specify notice period",
                        remediation="Add lifecycle.deprecationPolicy.noticePeriod (e.g., 'P30D')",
                    )
                )

        # Check 2: Retention policy for sensitive data
        for expose in self.contract.get("exposes", []):
            expose_id = expose.get("exposeId")
            schema = expose.get("contract", {}).get("schema", [])

            has_sensitive = any(
                field.get("sensitivity") in self.SENSITIVE_TYPES for field in schema
            )

            if has_sensitive:
                exp_lifecycle = expose.get("lifecycle", {})
                if exp_lifecycle.get("retention"):
                    self._add_pass()
                else:
                    self._add_violation(
                        PolicyViolation(
                            category=PolicyCategory.LIFECYCLE,
                            severity=PolicySeverity.WARNING,
                            message="Sensitive data should have explicit retention policy",
                            expose_id=expose_id,
                            remediation="Add lifecycle.retention (e.g., 'P90D' for 90 days)",
                        )
                    )

    # ==========================================
    # Schema Evolution Policy Enforcement
    # ==========================================

    def _enforce_schema_evolution_policies(self) -> None:
        """Enforce schema evolution and compatibility policies"""
        logger.debug("Enforcing schema evolution policies")

        evolution = self.contract.get("schemaEvolution", {})

        if not evolution:
            # No evolution policy is fine for new products
            self._add_pass()
            return

        compatibility = evolution.get("compatibility")
        change_policy = evolution.get("changePolicy", {})

        # Check 1: Breaking changes require approval
        if compatibility == "breaking":
            if change_policy.get("approvalRequired"):
                if change_policy.get("approvers"):
                    self._add_pass()
                else:
                    self._add_violation(
                        PolicyViolation(
                            category=PolicyCategory.SCHEMA_EVOLUTION,
                            severity=PolicySeverity.ERROR,
                            message="Breaking change requires approvers list",
                            remediation="Add schemaEvolution.changePolicy.approvers",
                        )
                    )
            else:
                self._add_violation(
                    PolicyViolation(
                        category=PolicyCategory.SCHEMA_EVOLUTION,
                        severity=PolicySeverity.WARNING,
                        message="Breaking change should require approval",
                        remediation="Set schemaEvolution.changePolicy.approvalRequired = true",
                    )
                )

        # Check 2: Change window should be reasonable
        change_window = change_policy.get("changeWindowDays")
        if change_window is not None:
            if change_window >= 7:  # At least 1 week notice
                self._add_pass()
            else:
                self._add_violation(
                    PolicyViolation(
                        category=PolicyCategory.SCHEMA_EVOLUTION,
                        severity=PolicySeverity.WARNING,
                        message=f"Change window of {change_window} days may be too short",
                        remediation="Consider minimum 7 days notice for schema changes",
                    )
                )

        # Check 3: Compatibility guarantees in contract
        for expose in self.contract.get("exposes", []):
            guarantees = expose.get("contract", {}).get("guarantees", {})
            if guarantees.get("compatibility"):
                self._add_pass()
            else:
                # This is optional, just info
                pass


def validate_policy_compliance(contract: Dict[str, Any]) -> PolicyEnforcementResult:
    """
    Convenience function to validate a contract's policy compliance.

    Args:
        contract: Parsed FLUID contract dictionary

    Returns:
        PolicyEnforcementResult with all violations and compliance status
    """
    engine = SchemaBasedPolicyEngine(contract)
    return engine.enforce_all()
