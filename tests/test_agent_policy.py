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

"""Tests for fluid_build.policy.agent_policy — AI/LLM policy validation."""

from fluid_build.policy.agent_policy import (
    AgentPolicyValidator,
    AgentPolicyViolation,
    is_model_allowed,
    is_use_case_allowed,
    validate_agent_policy,
)


class TestAgentPolicyViolation:
    def test_defaults(self):
        v = AgentPolicyViolation(severity="error", message="x")
        assert v.expose_id is None
        assert v.policy_field is None
        assert v.suggestion is None


class TestAgentPolicyValidator:
    def _contract(self, agent_policy):
        return {"exposes": [{"exposeId": "e1", "policy": {"agentPolicy": agent_policy}}]}

    def test_no_policy_valid(self):
        ok, v = AgentPolicyValidator().validate({"exposes": [{"exposeId": "e1"}]})
        assert ok is True
        assert v == []

    def test_empty_policy_valid(self):
        ok, v = AgentPolicyValidator().validate(self._contract({}))
        assert ok is True

    # Model list validation
    def test_model_conflict_error(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedModels": ["gpt-4"],
                    "deniedModels": ["gpt-4"],
                }
            )
        )
        assert ok is False
        assert any(viol.severity == "error" and "both" in viol.message.lower() for viol in v)

    def test_unknown_model_info(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedModels": ["totally-custom-model"],
                }
            )
        )
        assert ok is True  # info-level, not error
        assert any(viol.severity == "info" and "unknown" in viol.message.lower() for viol in v)

    def test_known_models_no_warning(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedModels": ["gpt-4", "claude-3-opus"],
                }
            )
        )
        info = [viol for viol in v if viol.severity == "info"]
        assert info == []

    # Use case validation
    def test_use_case_conflict_error(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedUseCases": ["inference"],
                    "deniedUseCases": ["inference"],
                }
            )
        )
        assert ok is False

    def test_invalid_use_case_error(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedUseCases": ["banana_split"],
                }
            )
        )
        assert ok is False
        assert any("invalid" in viol.message.lower() for viol in v)

    def test_valid_use_cases_pass(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "allowedUseCases": ["inference", "rag", "embedding"],
                }
            )
        )
        assert ok is True

    # Token limits
    def test_per_request_exceeds_daily_error(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "maxTokensPerRequest": 50000,
                    "maxTokensPerDay": 10000,
                }
            )
        )
        assert ok is False
        assert any("exceeds" in viol.message.lower() for viol in v)

    def test_very_low_tokens_warning(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "maxTokensPerRequest": 50,
                }
            )
        )
        assert ok is True
        assert any(viol.severity == "warning" and "very low" in viol.message.lower() for viol in v)

    # Retention policy
    def test_no_store_but_retention_warning(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "canStore": False,
                    "retentionPolicy": {"maxRetentionDays": 30},
                }
            )
        )
        assert any(viol.severity == "warning" and "canStore" in viol.message for viol in v)

    def test_require_deletion_zero_retention_info(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "retentionPolicy": {"requireDeletion": True, "maxRetentionDays": 0},
                }
            )
        )
        assert any(
            viol.severity == "info" and "immediate deletion" in viol.message.lower() for viol in v
        )

    # Reasoning constraints
    def test_no_reason_but_reasoning_allowed_warning(self):
        ok, v = AgentPolicyValidator().validate(
            self._contract(
                {
                    "canReason": False,
                    "allowedUseCases": ["reasoning"],
                }
            )
        )
        # reasoning is valid use case, but canReason=false conflict
        warnings = [
            viol for viol in v if viol.severity == "warning" and "canReason" in viol.message
        ]
        assert len(warnings) >= 1


class TestConvenienceFunctions:
    def test_validate_agent_policy_no_policy(self):
        ok, msgs = validate_agent_policy({"exposes": []})
        assert ok is True
        assert msgs == []

    def test_validate_agent_policy_with_error(self):
        ok, msgs = validate_agent_policy(
            {
                "exposes": [
                    {
                        "exposeId": "e1",
                        "policy": {
                            "agentPolicy": {
                                "allowedModels": ["gpt-4"],
                                "deniedModels": ["gpt-4"],
                            }
                        },
                    }
                ],
            }
        )
        assert ok is False
        assert any("❌" in m for m in msgs)


class TestIsModelAllowed:
    def test_no_lists_all_allowed(self):
        assert is_model_allowed({}, "gpt-4") is True

    def test_denied(self):
        assert is_model_allowed({"deniedModels": ["gpt-4"]}, "gpt-4") is False

    def test_not_in_allowlist(self):
        assert is_model_allowed({"allowedModels": ["claude-3-opus"]}, "gpt-4") is False

    def test_in_allowlist(self):
        assert is_model_allowed({"allowedModels": ["gpt-4"]}, "gpt-4") is True


class TestIsUseCaseAllowed:
    def test_no_lists_all_allowed(self):
        assert is_use_case_allowed({}, "inference") is True

    def test_denied(self):
        assert is_use_case_allowed({"deniedUseCases": ["training"]}, "training") is False

    def test_not_in_allowlist(self):
        assert is_use_case_allowed({"allowedUseCases": ["inference"]}, "training") is False

    def test_in_allowlist(self):
        assert is_use_case_allowed({"allowedUseCases": ["inference"]}, "inference") is True
