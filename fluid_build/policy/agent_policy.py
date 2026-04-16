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
FLUID 0.7.1 AgentPolicy Validator

Validates AI/LLM usage policies in data product contracts.
Ensures model access, use cases, and retention policies are enforceable.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AgentPolicyViolation:
    """Represents an agentPolicy validation issue."""

    severity: str  # "error", "warning", "info"
    message: str
    expose_id: Optional[str] = None
    policy_field: Optional[str] = None
    suggestion: Optional[str] = None


class AgentPolicyValidator:
    """Validates agentPolicy constraints in FLUID 0.7.1 contracts."""

    # Known AI/LLM models (extensible - this is for validation warnings)
    KNOWN_MODELS = {
        # OpenAI
        "gpt-4",
        "gpt-4-turbo",
        "gpt-4-32k",
        "gpt-3.5-turbo",
        "gpt-3.5-turbo-16k",
        # Anthropic Claude
        "claude-3-opus",
        "claude-3-sonnet",
        "claude-3-haiku",
        "claude-2.1",
        "claude-2",
        "claude-instant",
        # Google
        "gemini-pro",
        "gemini-ultra",
        "palm-2",
        # Meta Llama
        "llama-3-70b",
        "llama-3-8b",
        "llama-2-70b",
        "llama-2-13b",
        "llama-2-7b",
        # Mistral
        "mistral-large",
        "mistral-medium",
        "mistral-small",
        "mistral-tiny",
        # Other
        "cohere-command",
        "cohere-command-light",
    }

    # Valid use case types from FLUID 0.7.1 schema
    VALID_USE_CASES = {
        "inference",
        "reasoning",
        "analysis",
        "summarization",
        "classification",
        "embedding",
        "search",
        "qa",
        "code_generation",
        "fine_tuning",
        "training",
        "rag",
    }

    def validate(self, contract: Dict[str, Any]) -> Tuple[bool, List[AgentPolicyViolation]]:
        """
        Validate agentPolicy constraints.

        Returns:
            (is_valid, violations)
        """
        violations = []

        for expose in contract.get("exposes", []):
            policy = expose.get("policy", {})
            agent_policy = policy.get("agentPolicy")

            if not agent_policy:
                continue  # No agentPolicy = no AI restrictions

            expose_id = expose.get("exposeId", "unknown")

            # Validate model lists
            self._validate_model_lists(agent_policy, expose_id, violations)

            # Validate use cases
            self._validate_use_cases(agent_policy, expose_id, violations)

            # Validate token limits
            self._validate_token_limits(agent_policy, expose_id, violations)

            # Validate retention policy
            self._validate_retention_policy(agent_policy, expose_id, violations)

            # Validate reasoning constraints
            self._validate_reasoning_constraints(agent_policy, expose_id, violations)

        # All agentPolicy violations are errors or warnings, not blockers by default
        has_errors = any(v.severity == "error" for v in violations)
        is_valid = not has_errors

        return is_valid, violations

    def _validate_model_lists(
        self, agent_policy: Dict[str, Any], expose_id: str, violations: List[AgentPolicyViolation]
    ) -> None:
        """Validate allowedModels and deniedModels."""
        allowed_models = agent_policy.get("allowedModels", [])
        denied_models = agent_policy.get("deniedModels", [])

        # Check 1: Model conflicts (same model in both lists)
        conflicts = set(allowed_models) & set(denied_models)
        if conflicts:
            violations.append(
                AgentPolicyViolation(
                    severity="error",
                    message=f"Models appear in both allowedModels and deniedModels: {', '.join(conflicts)}",
                    expose_id=expose_id,
                    policy_field="allowedModels/deniedModels",
                    suggestion="Remove duplicates from one of the lists",
                )
            )

        # Check 2: Unknown models (warning only - allows custom/future models)
        all_specified_models = set(allowed_models + denied_models)
        unknown_models = all_specified_models - self.KNOWN_MODELS
        if unknown_models:
            violations.append(
                AgentPolicyViolation(
                    severity="info",
                    message=f"Unknown/custom AI models specified: {', '.join(unknown_models)}",
                    expose_id=expose_id,
                    policy_field="allowedModels/deniedModels",
                    suggestion="Verify model names are correct (custom models are allowed)",
                )
            )

    def _validate_use_cases(
        self, agent_policy: Dict[str, Any], expose_id: str, violations: List[AgentPolicyViolation]
    ) -> None:
        """Validate allowedUseCases and deniedUseCases."""
        allowed_cases = agent_policy.get("allowedUseCases", [])
        denied_cases = agent_policy.get("deniedUseCases", [])

        # Check 1: Use case conflicts
        case_conflicts = set(allowed_cases) & set(denied_cases)
        if case_conflicts:
            violations.append(
                AgentPolicyViolation(
                    severity="error",
                    message=f"Use cases in both allowed and denied lists: {', '.join(case_conflicts)}",
                    expose_id=expose_id,
                    policy_field="allowedUseCases/deniedUseCases",
                    suggestion="Remove duplicates from one of the lists",
                )
            )

        # Check 2: Invalid use case names
        all_cases = set(allowed_cases + denied_cases)
        invalid_cases = all_cases - self.VALID_USE_CASES
        if invalid_cases:
            violations.append(
                AgentPolicyViolation(
                    severity="error",
                    message=f"Invalid use case names: {', '.join(invalid_cases)}",
                    expose_id=expose_id,
                    policy_field="allowedUseCases/deniedUseCases",
                    suggestion=f"Valid use cases: {', '.join(sorted(self.VALID_USE_CASES))}",
                )
            )

    def _validate_token_limits(
        self, agent_policy: Dict[str, Any], expose_id: str, violations: List[AgentPolicyViolation]
    ) -> None:
        """Validate token limit constraints."""
        max_tokens_per_request = agent_policy.get("maxTokensPerRequest")
        max_tokens_per_day = agent_policy.get("maxTokensPerDay")

        # Check: Per-request limit must be <= daily limit
        if max_tokens_per_request and max_tokens_per_day:
            if max_tokens_per_request > max_tokens_per_day:
                violations.append(
                    AgentPolicyViolation(
                        severity="error",
                        message=f"maxTokensPerRequest ({max_tokens_per_request}) exceeds maxTokensPerDay ({max_tokens_per_day})",
                        expose_id=expose_id,
                        policy_field="maxTokensPerRequest",
                        suggestion="Daily limit must be >= per-request limit",
                    )
                )

        # Warn about very small limits
        if max_tokens_per_request and max_tokens_per_request < 100:
            violations.append(
                AgentPolicyViolation(
                    severity="warning",
                    message=f"Very low maxTokensPerRequest ({max_tokens_per_request}) may limit AI functionality",
                    expose_id=expose_id,
                    policy_field="maxTokensPerRequest",
                    suggestion="Consider if this limit is intentional",
                )
            )

    def _validate_retention_policy(
        self, agent_policy: Dict[str, Any], expose_id: str, violations: List[AgentPolicyViolation]
    ) -> None:
        """Validate retention policy constraints."""
        can_store = agent_policy.get("canStore", True)
        retention_policy = agent_policy.get("retentionPolicy", {})
        max_retention_days = retention_policy.get("maxRetentionDays")
        require_deletion = retention_policy.get("requireDeletion", False)

        # Check 1: Storage disabled but retention policy allows storage
        if not can_store and max_retention_days and max_retention_days > 0:
            violations.append(
                AgentPolicyViolation(
                    severity="warning",
                    message="canStore=false but retentionPolicy allows data storage",
                    expose_id=expose_id,
                    policy_field="canStore/retentionPolicy",
                    suggestion="Set maxRetentionDays=0 for ephemeral processing when canStore=false",
                )
            )

        # Check 2: Deletion required but no retention limit
        if require_deletion and (not max_retention_days or max_retention_days == 0):
            violations.append(
                AgentPolicyViolation(
                    severity="info",
                    message="requireDeletion=true with maxRetentionDays=0 enforces immediate deletion",
                    expose_id=expose_id,
                    policy_field="retentionPolicy",
                    suggestion="This is valid for zero-retention policies",
                )
            )

    def _validate_reasoning_constraints(
        self, agent_policy: Dict[str, Any], expose_id: str, violations: List[AgentPolicyViolation]
    ) -> None:
        """Validate reasoning-related constraints."""
        can_reason = agent_policy.get("canReason", True)
        allowed_cases = agent_policy.get("allowedUseCases", [])
        agent_policy.get("deniedUseCases", [])

        # Check: canReason=false but reasoning in allowed use cases
        if not can_reason and "reasoning" in allowed_cases:
            violations.append(
                AgentPolicyViolation(
                    severity="warning",
                    message="canReason=false but 'reasoning' is in allowedUseCases",
                    expose_id=expose_id,
                    policy_field="canReason",
                    suggestion="Remove 'reasoning' from allowedUseCases or set canReason=true",
                )
            )


def validate_agent_policy(contract: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Convenience function for CLI integration.

    Returns:
        (is_valid, messages)
    """
    validator = AgentPolicyValidator()
    is_valid, violations = validator.validate(contract)

    messages = []
    for v in violations:
        prefix = "❌" if v.severity == "error" else "⚠️" if v.severity == "warning" else "ℹ️"
        msg = f"{prefix} [{v.expose_id}] {v.message}"
        if v.suggestion:
            msg += f"\n   💡 {v.suggestion}"
        messages.append(msg)

    return is_valid, messages


def is_model_allowed(agent_policy: Dict[str, Any], model: str) -> bool:
    """
    Check if a specific AI model is allowed by the policy.

    Args:
        agent_policy: The agentPolicy dict
        model: Model identifier (e.g., "gpt-4")

    Returns:
        True if model is allowed, False otherwise
    """
    allowed_models = agent_policy.get("allowedModels", [])
    denied_models = agent_policy.get("deniedModels", [])

    # If denied explicitly, not allowed
    if model in denied_models:
        return False

    # If allowlist exists and model not in it, not allowed
    if allowed_models and model not in allowed_models:
        return False

    # Otherwise allowed
    return True


def is_use_case_allowed(agent_policy: Dict[str, Any], use_case: str) -> bool:
    """
    Check if a specific use case is allowed by the policy.

    Args:
        agent_policy: The agentPolicy dict
        use_case: Use case type (e.g., "inference", "training")

    Returns:
        True if use case is allowed, False otherwise
    """
    allowed_cases = agent_policy.get("allowedUseCases", [])
    denied_cases = agent_policy.get("deniedUseCases", [])

    # If denied explicitly, not allowed
    if use_case in denied_cases:
        return False

    # If allowlist exists and use case not in it, not allowed
    if allowed_cases and use_case not in allowed_cases:
        return False

    # Otherwise allowed
    return True
