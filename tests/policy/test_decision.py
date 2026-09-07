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

"""Tests for the single agentPolicy decision function."""

from __future__ import annotations

import pytest

from fluid_build.policy.decision import (
    CHECK_ORDER,
    Decision,
    EffectivePolicy,
    ReasonCode,
    as_tuple,
    decide,
    policy_digest,
)

ALL = EffectivePolicy.of()


# ---------------------------------------------------------------------------
# structural guards: the enum, the order and the logic must not drift apart
# ---------------------------------------------------------------------------


def test_check_order_covers_every_denial_code() -> None:
    """A code missing from CHECK_ORDER can never fire, however correct it looks."""
    denials = {code for code in ReasonCode if code.denies}
    assert set(CHECK_ORDER) == denials, (
        "CHECK_ORDER and the denial codes have drifted. Missing from the order: "
        f"{sorted(c.value for c in denials - set(CHECK_ORDER))}"
    )


def test_check_order_has_no_duplicates() -> None:
    assert len(CHECK_ORDER) == len(set(CHECK_ORDER))


def test_allowed_is_not_in_check_order() -> None:
    assert ReasonCode.ALLOWED not in CHECK_ORDER


@pytest.mark.parametrize("code", [c for c in ReasonCode if c.denies], ids=lambda c: c.value)
def test_every_denial_code_is_reachable(code: ReasonCode) -> None:
    """Each code must be produced by some request, or it is decoration.

    A reason code nothing can emit is worse than no code: it appears in the
    vocabulary, dashboards allow for it, and it never arrives.
    """
    cases = {
        ReasonCode.TOOL_NOT_ALLOWED: (
            EffectivePolicy.of(denied_tools=["query_sql"]),
            {"tool": "query_sql"},
        ),
        ReasonCode.MISSING_MODEL_IDENTITY: (
            EffectivePolicy.of(allowed_models=["m"]),
            {"tool": "query"},
        ),
        ReasonCode.IN_DENIED_MODELS: (
            EffectivePolicy.of(denied_models=["bad"]),
            {"tool": "query", "model_id": "bad"},
        ),
        ReasonCode.IN_DENIED_USE_CASES: (
            EffectivePolicy.of(denied_use_cases=["advertising"]),
            {"tool": "query", "use_case": "advertising"},
        ),
        ReasonCode.NOT_IN_ALLOWED_MODELS: (
            EffectivePolicy.of(allowed_models=["good"]),
            {"tool": "query", "model_id": "other"},
        ),
        ReasonCode.MISSING_USE_CASE_WITH_ALLOWLIST: (
            EffectivePolicy.of(allowed_use_cases=["analysis"]),
            {"tool": "query"},
        ),
        ReasonCode.NOT_IN_ALLOWED_USE_CASES: (
            EffectivePolicy.of(allowed_use_cases=["analysis"]),
            {"tool": "query", "use_case": "advertising"},
        ),
    }
    policy, request = cases[code]
    decision = decide(policy=policy, **request)
    assert decision.reason is code
    assert decision.allow is False


# ---------------------------------------------------------------------------
# the precedence this module exists to fix
# ---------------------------------------------------------------------------


def test_explicit_denial_beats_absence_from_an_allowlist() -> None:
    """The regression that motivated this module.

    Before ``decide``, the gate evaluated the whole model stage before the
    use-case stage, so this request reported ``not-in-allowedModels`` while the
    docstring immediately above it promised denylists were checked first. The
    verdict was right both ways; the reported reason was not.
    """
    policy = EffectivePolicy.of(allowed_models=["good"], denied_use_cases=["advertising"])
    decision = decide(policy=policy, tool="query", model_id="bad", use_case="advertising")
    assert decision.allow is False
    assert decision.reason is ReasonCode.IN_DENIED_USE_CASES


def test_tool_gate_precedes_identity_gates() -> None:
    policy = EffectivePolicy.of(denied_tools=["query_sql"], denied_models=["bad"])
    decision = decide(policy=policy, tool="query_sql", model_id="bad")
    assert decision.reason is ReasonCode.TOOL_NOT_ALLOWED


def test_denied_model_beats_missing_use_case_allowlist() -> None:
    policy = EffectivePolicy.of(denied_models=["bad"], allowed_use_cases=["analysis"])
    decision = decide(policy=policy, tool="query", model_id="bad")
    assert decision.reason is ReasonCode.IN_DENIED_MODELS


def test_order_changes_the_reason_never_the_verdict() -> None:
    """Any request that trips at least one rule is denied, whatever the order."""
    policy = EffectivePolicy.of(
        denied_tools=["t"],
        denied_models=["m"],
        denied_use_cases=["u"],
        allowed_models=["other"],
        allowed_use_cases=["other"],
    )
    decision = decide(policy=policy, tool="t", model_id="m", use_case="u")
    assert decision.allow is False


# ---------------------------------------------------------------------------
# the inert case: a policy that says nothing must not deny
# ---------------------------------------------------------------------------


def test_empty_policy_allows_everything() -> None:
    decision = decide(policy=ALL, tool="query", model_id=None, use_case=None)
    assert decision.allow is True
    assert decision.reason is ReasonCode.ALLOWED


def test_missing_model_identity_is_inert_without_model_rules() -> None:
    """Model identity is not part of the MCP handshake.

    Denying an unidentified caller on a contract with no model rules refused
    every spec-compliant client, ``describe`` included.
    """
    policy = EffectivePolicy.of(denied_use_cases=["advertising"])
    assert decide(policy=policy, tool="query").allow is True


def test_missing_model_identity_denies_when_a_denylist_exists() -> None:
    """Otherwise a denied model slips the gate by omitting the field."""
    policy = EffectivePolicy.of(denied_models=["bad"])
    assert decide(policy=policy, tool="query").reason is ReasonCode.MISSING_MODEL_IDENTITY


def test_empty_allowlist_denies_everything() -> None:
    """``()`` is 'allow nothing' and must not be confused with 'no allowlist'."""
    policy = EffectivePolicy.of(allowed_tools=[])
    assert decide(policy=policy, tool="query").reason is ReasonCode.TOOL_NOT_ALLOWED


# ---------------------------------------------------------------------------
# the digest
# ---------------------------------------------------------------------------


def test_digest_is_independent_of_authoring_order() -> None:
    a = EffectivePolicy.of(allowed_models=["b", "a"], denied_use_cases=["z", "y"])
    b = EffectivePolicy.of(allowed_models=["a", "b"], denied_use_cases=["y", "z"])
    assert policy_digest(a) == policy_digest(b)


def test_digest_ignores_duplicates() -> None:
    a = EffectivePolicy.of(allowed_models=["a", "a", "b"])
    b = EffectivePolicy.of(allowed_models=["a", "b"])
    assert policy_digest(a) == policy_digest(b)


def test_absent_allowlist_and_empty_allowlist_digest_differently() -> None:
    """They are different policies — one allows all, the other allows nothing."""
    absent = EffectivePolicy.of(allowed_models=None)
    empty = EffectivePolicy.of(allowed_models=[])
    assert policy_digest(absent) != policy_digest(empty)


def test_digest_changes_when_any_rule_changes() -> None:
    base = EffectivePolicy.of(allowed_models=["a"])
    variants = [
        EffectivePolicy.of(allowed_models=["a"], denied_models=["b"]),
        EffectivePolicy.of(allowed_models=["a", "b"]),
        EffectivePolicy.of(allowed_models=["a"], allowed_tools=["query"]),
        EffectivePolicy.of(allowed_models=["a"], denied_use_cases=["x"]),
    ]
    digests = {policy_digest(v) for v in variants}
    assert policy_digest(base) not in digests
    assert len(digests) == len(variants), "two different policies collided"


def test_digest_names_its_scheme() -> None:
    """An unlabelled digest cannot be migrated without invalidating history."""
    scheme, _, hexdigest = policy_digest(ALL).partition(":")
    assert scheme in {"jcs-sha256", "sortkeys-sha256"}
    assert len(hexdigest) == 64


def test_digest_is_stable_across_calls() -> None:
    assert policy_digest(ALL) == policy_digest(ALL)


def test_supplied_digest_is_used_verbatim() -> None:
    decision = decide(policy=ALL, tool="query", digest="jcs-sha256:deadbeef")
    assert decision.policy_digest == "jcs-sha256:deadbeef"


# ---------------------------------------------------------------------------
# determinism and the record
# ---------------------------------------------------------------------------


def test_decide_is_deterministic() -> None:
    policy = EffectivePolicy.of(allowed_models=["good"], denied_use_cases=["advertising"])
    kwargs = {"tool": "query", "model_id": "bad", "use_case": "advertising"}
    first = decide(policy=policy, **kwargs)
    assert all(decide(policy=policy, **kwargs) == first for _ in range(5))


def test_record_carries_reason_digest_and_request() -> None:
    policy = EffectivePolicy.of(denied_models=["bad"])
    record = decide(policy=policy, tool="query", model_id="bad", use_case="analysis").to_record()
    assert record["allow"] is False
    assert record["reasonCode"] == "in-deniedModels"
    assert record["policyDigest"].endswith(policy_digest(policy).split(":")[1])
    assert record["request"] == {
        "tool": "query",
        "modelId": "bad",
        "useCase": "analysis",
    }


def test_record_has_no_timestamp_or_caller_identity() -> None:
    """Both belong to the control plane; including them would break purity."""
    record = decide(policy=ALL, tool="query").to_record()
    flat = str(record).lower()
    for forbidden in ("timestamp", "time", "principal", "user", "org"):
        assert forbidden not in flat


# ---------------------------------------------------------------------------
# back-compat with the shape the gates return today
# ---------------------------------------------------------------------------


def test_as_tuple_matches_the_legacy_shape() -> None:
    allowed, reason = as_tuple(decide(policy=ALL, tool="query"))
    assert (allowed, reason) == (True, None)

    denied, why = as_tuple(decide(policy=EffectivePolicy.of(denied_tools=["q"]), tool="q"))
    assert (denied, why) == (False, "tool-not-allowed")


def test_reason_codes_keep_their_existing_wire_values() -> None:
    """Callers already comparing against these strings must keep working."""
    assert ReasonCode.TOOL_NOT_ALLOWED == "tool-not-allowed"
    assert ReasonCode.IN_DENIED_MODELS == "in-deniedModels"
    assert ReasonCode.NOT_IN_ALLOWED_MODELS == "not-in-allowedModels"
    assert ReasonCode.MISSING_MODEL_IDENTITY == "missing-model-identity"
    assert ReasonCode.IN_DENIED_USE_CASES == "in-deniedUseCases"
    assert ReasonCode.NOT_IN_ALLOWED_USE_CASES == "not-in-allowedUseCases"
    assert ReasonCode.MISSING_USE_CASE_WITH_ALLOWLIST == "missing-use-case-with-allowlist"


def test_unknown_reason_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReasonCode("some-reason-nobody-enumerated")


def test_decision_is_frozen() -> None:
    decision = decide(policy=ALL, tool="query")
    with pytest.raises(Exception):
        decision.allow = False  # type: ignore[misc]
    assert isinstance(decision, Decision)
