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

"""Branch-coverage tests for fluid_build.policy.schema_engine"""

from fluid_build.policy.schema_engine import (
    PolicyCategory,
    PolicyEnforcementResult,
    PolicySeverity,
    PolicyViolation,
    SchemaBasedPolicyEngine,
    validate_policy_compliance,
)

# ===================== PolicyViolation =====================


class TestPolicyViolation:
    def test_to_dict_full(self):
        v = PolicyViolation(
            category=PolicyCategory.SENSITIVITY,
            severity=PolicySeverity.CRITICAL,
            message="field unprotected",
            field="ssn",
            expose_id="exp1",
            rule_id="r1",
            remediation="add masking",
        )
        d = v.to_dict()
        assert d["category"] == "sensitivity"
        assert d["severity"] == "critical"
        assert d["field"] == "ssn"
        assert d["remediation"] == "add masking"

    def test_to_dict_minimal(self):
        v = PolicyViolation(
            category=PolicyCategory.ACCESS_CONTROL,
            severity=PolicySeverity.INFO,
            message="info",
        )
        d = v.to_dict()
        assert d["field"] is None
        assert d["rule_id"] is None


# ===================== PolicyEnforcementResult =====================


class TestPolicyEnforcementResult:
    def test_is_compliant_empty(self):
        r = PolicyEnforcementResult()
        assert r.is_compliant() is True

    def test_is_compliant_with_warnings_only(self):
        r = PolicyEnforcementResult()
        r.violations.append(
            PolicyViolation(
                category=PolicyCategory.SENSITIVITY, severity=PolicySeverity.WARNING, message="warn"
            )
        )
        assert r.is_compliant() is True

    def test_is_not_compliant_with_error(self):
        r = PolicyEnforcementResult()
        r.violations.append(
            PolicyViolation(
                category=PolicyCategory.SENSITIVITY, severity=PolicySeverity.ERROR, message="err"
            )
        )
        assert r.is_compliant() is False

    def test_is_not_compliant_with_critical(self):
        r = PolicyEnforcementResult()
        r.violations.append(
            PolicyViolation(
                category=PolicyCategory.SENSITIVITY,
                severity=PolicySeverity.CRITICAL,
                message="crit",
            )
        )
        assert r.is_compliant() is False

    def test_get_blocking_violations(self):
        r = PolicyEnforcementResult()
        r.violations.extend(
            [
                PolicyViolation(PolicyCategory.SENSITIVITY, PolicySeverity.CRITICAL, "c"),
                PolicyViolation(PolicyCategory.ACCESS_CONTROL, PolicySeverity.ERROR, "e"),
                PolicyViolation(PolicyCategory.DATA_QUALITY, PolicySeverity.WARNING, "w"),
                PolicyViolation(PolicyCategory.LIFECYCLE, PolicySeverity.INFO, "i"),
            ]
        )
        blocking = r.get_blocking_violations()
        assert len(blocking) == 2

    def test_get_by_category(self):
        r = PolicyEnforcementResult()
        r.violations.extend(
            [
                PolicyViolation(PolicyCategory.SENSITIVITY, PolicySeverity.WARNING, "s"),
                PolicyViolation(PolicyCategory.ACCESS_CONTROL, PolicySeverity.ERROR, "a"),
                PolicyViolation(PolicyCategory.SENSITIVITY, PolicySeverity.INFO, "s2"),
            ]
        )
        sens = r.get_by_category(PolicyCategory.SENSITIVITY)
        assert len(sens) == 2

    def test_calculate_score_no_checks(self):
        r = PolicyEnforcementResult()
        assert r.calculate_score() == 100

    def test_calculate_score_penalties(self):
        r = PolicyEnforcementResult(checks_passed=10, checks_failed=4)
        r.violations.extend(
            [
                PolicyViolation(PolicyCategory.SENSITIVITY, PolicySeverity.CRITICAL, "c"),
                PolicyViolation(PolicyCategory.ACCESS_CONTROL, PolicySeverity.ERROR, "e"),
                PolicyViolation(PolicyCategory.DATA_QUALITY, PolicySeverity.WARNING, "w"),
                PolicyViolation(PolicyCategory.LIFECYCLE, PolicySeverity.INFO, "i"),
            ]
        )
        score = r.calculate_score()
        # 20 + 10 + 5 + 1 = 36 penalty => 100 - 36 = 64
        assert score == 64

    def test_calculate_score_floor_zero(self):
        r = PolicyEnforcementResult(checks_passed=0, checks_failed=10)
        for _ in range(10):
            r.violations.append(
                PolicyViolation(PolicyCategory.SENSITIVITY, PolicySeverity.CRITICAL, "c")
            )
        assert r.calculate_score() == 0

    def test_to_dict(self):
        r = PolicyEnforcementResult(checks_passed=3, checks_failed=1)
        r.violations.append(
            PolicyViolation(PolicyCategory.ACCESS_CONTROL, PolicySeverity.ERROR, "e")
        )
        d = r.to_dict()
        assert "is_compliant" in d
        assert "score" in d
        assert d["blocking_violations"] == 1


# ===================== SchemaBasedPolicyEngine =====================


class TestSchemaBasedPolicyEngine:
    def _make_contract(self, **kwargs):
        base = {
            "id": "test-product",
            "fluidVersion": "0.5.7",
            "exposes": [],
        }
        base.update(kwargs)
        return base

    # -- Sensitivity policies --

    def test_pii_without_protection(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "email", "sensitivity": "pii"}]},
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert any(
            v.severity == PolicySeverity.CRITICAL and "pii" in v.message.lower()
            for v in engine.result.violations
        )

    def test_pii_with_protection(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "email", "sensitivity": "pii"}]},
                    "policy": {"privacy": {"masking": [{"column": "email", "strategy": "mask"}]}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert engine.result.checks_passed >= 1
        assert not any(v.severity == PolicySeverity.CRITICAL for v in engine.result.violations)

    def test_phi_without_protection(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "diagnosis", "sensitivity": "phi"}]},
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert any("phi" in v.message.lower() for v in engine.result.violations)

    def test_encrypted_with_binding(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "ssn", "sensitivity": "encrypted"}]},
                    "binding": {"platform": "gcp"},
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert engine.result.checks_passed >= 1

    def test_encrypted_no_binding(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "ssn", "sensitivity": "encrypted"}]},
                    "binding": {},
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert any(v.severity == PolicySeverity.CRITICAL for v in engine.result.violations)

    def test_cleartext_with_sensitive_tags(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "schema": [{"name": "id", "sensitivity": "cleartext", "tags": ["pii"]}]
                    },
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert any(v.severity == PolicySeverity.WARNING for v in engine.result.violations)

    def test_cleartext_no_sensitive_tags(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "schema": [{"name": "id", "sensitivity": "cleartext", "tags": ["internal"]}]
                    },
                    "policy": {},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_sensitivity_policies()
        assert engine.result.checks_passed >= 1

    # -- Access control policies --

    def test_column_restriction_valid(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "email"}, {"name": "name"}]},
                    "policy": {"authz": {"columnRestrictions": [{"columns": ["email"]}]}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert engine.result.checks_passed >= 1

    def test_column_restriction_invalid_column(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "email"}]},
                    "policy": {"authz": {"columnRestrictions": [{"columns": ["nonexistent"]}]}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert any(v.severity == PolicySeverity.ERROR for v in engine.result.violations)

    def test_public_classification_with_sensitive_data(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "ssn", "sensitivity": "pii"}]},
                    "policy": {"classification": "Public", "authz": {}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert any(v.severity == PolicySeverity.CRITICAL for v in engine.result.violations)

    def test_public_classification_no_sensitive_data(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "id"}]},
                    "policy": {"classification": "Public", "authz": {}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert engine.result.checks_passed >= 1

    def test_restricted_with_readers(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": []},
                    "policy": {"classification": "Restricted", "authz": {"readers": ["team-a"]}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert engine.result.checks_passed >= 1

    def test_restricted_no_readers(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": []},
                    "policy": {"classification": "Restricted", "authz": {}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert any(v.severity == PolicySeverity.WARNING for v in engine.result.violations)

    def test_valid_masking_strategy(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": []},
                    "policy": {
                        "privacy": {"masking": [{"column": "email", "strategy": "hash"}]},
                        "authz": {},
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert engine.result.checks_passed >= 1

    def test_invalid_masking_strategy(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": []},
                    "policy": {
                        "privacy": {"masking": [{"column": "email", "strategy": "invalid_strat"}]},
                        "authz": {},
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_access_control_policies()
        assert any("Invalid masking" in v.message for v in engine.result.violations)

    # -- Data quality policies --

    def test_critical_dq_rule_monitoring_enabled(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {
                            "rules": [
                                {
                                    "id": "r1",
                                    "severity": "critical",
                                    "type": "completeness",
                                    "threshold": 0.95,
                                }
                            ],
                            "monitoring": {"enabled": True},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert engine.result.checks_passed >= 1

    def test_critical_dq_rule_monitoring_disabled(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {
                            "rules": [{"id": "r1", "severity": "critical"}],
                            "monitoring": {"enabled": False},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert any("monitoring disabled" in v.message.lower() for v in engine.result.violations)

    def test_freshness_rule_with_threshold(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {
                            "rules": [{"id": "r1", "type": "freshness", "threshold": 3600}],
                            "monitoring": {},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert engine.result.checks_passed >= 1

    def test_freshness_rule_no_threshold(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {"rules": [{"id": "r1", "type": "freshness"}], "monitoring": {}}
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert any("missing threshold" in v.message.lower() for v in engine.result.violations)

    def test_completeness_rule_valid(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {
                            "rules": [{"id": "r1", "type": "completeness", "threshold": 0.95}],
                            "monitoring": {},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert engine.result.checks_passed >= 1

    def test_completeness_rule_invalid(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {
                            "rules": [{"id": "r1", "type": "completeness", "threshold": 2.0}],
                            "monitoring": {},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert any("between 0 and 1" in v.message for v in engine.result.violations)

    def test_completeness_rule_no_threshold(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {
                        "dq": {"rules": [{"id": "r1", "type": "completeness"}], "monitoring": {}}
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert any("between 0 and 1" in v.message for v in engine.result.violations)

    def test_qos_freshness_no_matching_dq_rule(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "qos": {"freshnessSLO": "1h"},
                    "contract": {"dq": {"rules": [], "monitoring": {}}},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert any("freshness SLO" in v.message for v in engine.result.violations)

    def test_qos_freshness_with_matching_dq_rule(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "qos": {"freshnessSLO": "1h"},
                    "contract": {
                        "dq": {
                            "rules": [{"type": "freshness", "threshold": 3600}],
                            "monitoring": {},
                        }
                    },
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_data_quality_policies()
        assert engine.result.checks_passed >= 1

    # -- Lifecycle policies --

    def test_deprecated_with_replacement(self):
        contract = self._make_contract(
            lifecycle={
                "state": "deprecated",
                "deprecationPolicy": {"replacement": "new-product", "noticePeriod": "P30D"},
            }
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_lifecycle_policies()
        assert engine.result.checks_passed >= 2

    def test_deprecated_no_replacement(self):
        contract = self._make_contract(lifecycle={"state": "deprecated", "deprecationPolicy": {}})
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_lifecycle_policies()
        assert any(v.severity == PolicySeverity.ERROR for v in engine.result.violations)

    def test_deprecated_no_notice_period(self):
        contract = self._make_contract(
            lifecycle={"state": "deprecated", "deprecationPolicy": {"replacement": "new"}}
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_lifecycle_policies()
        assert any("notice period" in v.message.lower() for v in engine.result.violations)

    def test_sensitive_data_with_retention(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "ssn", "sensitivity": "pii"}]},
                    "lifecycle": {"retention": "P90D"},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_lifecycle_policies()
        assert engine.result.checks_passed >= 1

    def test_sensitive_data_no_retention(self):
        contract = self._make_contract(
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "ssn", "sensitivity": "pii"}]},
                }
            ]
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_lifecycle_policies()
        assert any("retention" in v.message.lower() for v in engine.result.violations)

    # -- Schema evolution policies --

    def test_no_evolution_policy(self):
        contract = self._make_contract()
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert engine.result.checks_passed >= 1

    def test_breaking_with_approval_and_approvers(self):
        contract = self._make_contract(
            schemaEvolution={
                "compatibility": "breaking",
                "changePolicy": {"approvalRequired": True, "approvers": ["team-lead"]},
            }
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert engine.result.checks_passed >= 1

    def test_breaking_with_approval_no_approvers(self):
        contract = self._make_contract(
            schemaEvolution={
                "compatibility": "breaking",
                "changePolicy": {"approvalRequired": True},
            }
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert any("approvers" in v.message.lower() for v in engine.result.violations)

    def test_breaking_no_approval(self):
        contract = self._make_contract(
            schemaEvolution={"compatibility": "breaking", "changePolicy": {}}
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert any("approval" in v.message.lower() for v in engine.result.violations)

    def test_change_window_sufficient(self):
        contract = self._make_contract(schemaEvolution={"changePolicy": {"changeWindowDays": 14}})
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert engine.result.checks_passed >= 1

    def test_change_window_too_short(self):
        contract = self._make_contract(schemaEvolution={"changePolicy": {"changeWindowDays": 3}})
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert any("too short" in v.message.lower() for v in engine.result.violations)

    def test_expose_compatibility_guarantee(self):
        contract = self._make_contract(
            schemaEvolution={"changePolicy": {}},
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"guarantees": {"compatibility": "backward"}},
                }
            ],
        )
        engine = SchemaBasedPolicyEngine(contract)
        engine._enforce_schema_evolution_policies()
        assert engine.result.checks_passed >= 1

    # -- Full enforcement --

    def test_enforce_all(self):
        contract = self._make_contract(
            lifecycle={"state": "active"},
            exposes=[
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "id"}], "dq": {"rules": [], "monitoring": {}}},
                    "policy": {"authz": {}, "classification": "Internal"},
                }
            ],
        )
        engine = SchemaBasedPolicyEngine(contract)
        result = engine.enforce_all()
        assert result.checks_passed >= 0
        assert isinstance(result.calculate_score(), int)


# ===================== Convenience function =====================


class TestConvenience:
    def test_validate_policy_compliance(self):
        contract = {
            "id": "test",
            "exposes": [
                {
                    "exposeId": "e1",
                    "contract": {"schema": [{"name": "id"}]},
                    "policy": {"authz": {}},
                }
            ],
        }
        result = validate_policy_compliance(contract)
        assert isinstance(result, PolicyEnforcementResult)
