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

"""One decision function for agentPolicy, with a closed reason vocabulary.

Every gate in this engine that decides whether an agent may make a call should
route through :func:`decide`. Before this module there were two independent
implementations — the consumer-side output port and the authoring-side CLI —
each returning a bare string, each free to invent a new one, and neither able to
say *which policy* produced the answer.

Three properties this adds, in the order they matter:

**A closed vocabulary.** :class:`ReasonCode` enumerates every outcome. A denial
carries a code an operator can route on and a dashboard can count, not a message
that changes when someone rewords it. The codes keep their existing kebab-case
wire values so callers already comparing against ``"tool-not-allowed"`` keep
working — the type is new, the strings are not.

**A normative check order.** When more than one rule would deny a request, which
one gets reported was previously an accident of statement order, and the reported
reason disagreed with the documented precedence (see :data:`CHECK_ORDER`).

**A policy digest.** :func:`policy_digest` is a SHA-256 over the RFC 8785 (JCS)
canonical form of the *effective* policy. Two engines that enforced the same
rules produce the same digest; a decision record carrying one can be checked
years later against the policy that produced it. This is the same canonicalisation
the FLUX enforcement vectors already use, so the two engines agree on one digest
rather than two.

What this module deliberately does NOT do: decide *whether* a policy applies,
resolve a contract, or talk to a transport. It is pure, synchronous and total —
given the same inputs it returns the same :class:`Decision`, which is what makes
it testable against portable vectors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CHECK_ORDER",
    "Decision",
    "EffectivePolicy",
    "ReasonCode",
    "decide",
    "policy_digest",
]


class ReasonCode(str, Enum):
    """Every outcome the agentPolicy gate can produce.

    A ``str`` enum on purpose: ``decision.reason == "tool-not-allowed"`` still
    holds, so this is additive for existing callers, while
    ``ReasonCode("nonsense")`` now raises instead of silently becoming a new
    reason nobody enumerated.
    """

    ALLOWED = "allowed"

    #: The tool is denied outright, or is absent from a declared tool allowlist.
    TOOL_NOT_ALLOWED = "tool-not-allowed"

    #: The caller declared no model identity while the policy has something to
    #: enforce about models. Fail-closed: without identity the gate cannot
    #: decide, and allowing would defeat the contract.
    MISSING_MODEL_IDENTITY = "missing-model-identity"

    #: The model appears in ``deniedModels``.
    IN_DENIED_MODELS = "in-deniedModels"

    #: The use case appears in ``deniedUseCases``.
    IN_DENIED_USE_CASES = "in-deniedUseCases"

    #: A model allowlist exists and this model is not on it.
    NOT_IN_ALLOWED_MODELS = "not-in-allowedModels"

    #: A use-case allowlist exists and the caller declared no use case.
    MISSING_USE_CASE_WITH_ALLOWLIST = "missing-use-case-with-allowlist"

    #: A use-case allowlist exists and this use case is not on it.
    NOT_IN_ALLOWED_USE_CASES = "not-in-allowedUseCases"

    @property
    def denies(self) -> bool:
        return self is not ReasonCode.ALLOWED


#: The normative order in which rules are evaluated. First match wins.
#:
#: Two principles fix this order, and both are about which reason is the most
#: *useful* one to report, never about the verdict:
#:
#: 1. **Bounded surfaces first.** The tool set is finite and known before any
#:    identity is considered, so a call for a tool that does not exist on this
#:    server should say so rather than complain about a model.
#: 2. **Explicit denial beats absence from an allowlist.** "You are on the deny
#:    list" is a decision someone made about you; "you are not on the allow
#:    list" is the default for everyone unnamed. When both apply, the first is
#:    the actionable one.
#:
#: The verdict is unaffected by ordering. If any rule denies, the request is
#: denied; order decides only *which* reason is named. This matters because the
#: previous implementation evaluated the whole model gate before the use-case
#: gate, so a request whose model was merely absent from an allowlist AND whose
#: use case was explicitly denied reported ``not-in-allowedModels`` — while the
#: docstring directly above it promised denylists were checked first. Two
#: implementations reading the code and the prose would disagree about the same
#: request. Now the order is data, and the conformance vectors pin it.
CHECK_ORDER: Tuple[ReasonCode, ...] = (
    ReasonCode.TOOL_NOT_ALLOWED,
    ReasonCode.MISSING_MODEL_IDENTITY,
    ReasonCode.IN_DENIED_MODELS,
    ReasonCode.IN_DENIED_USE_CASES,
    ReasonCode.NOT_IN_ALLOWED_MODELS,
    ReasonCode.MISSING_USE_CASE_WITH_ALLOWLIST,
    ReasonCode.NOT_IN_ALLOWED_USE_CASES,
)


def _norm(values: Optional[Iterable[str]]) -> Optional[Tuple[str, ...]]:
    """Normalise a rule list: ``None`` stays ``None``, everything else sorts.

    ``None`` and ``()`` are different policies and must digest differently:
    a missing allowlist means "no allowlist"; an empty one means "allow
    nothing". Sorting makes the digest independent of authoring order, so two
    contracts listing the same models in a different sequence are recognised as
    the same policy.
    """
    if values is None:
        return None
    return tuple(sorted({str(v) for v in values}))


@dataclass(frozen=True)
class EffectivePolicy:
    """The rules actually in force for one expose, after CLI overrides.

    Deliberately six flat lists rather than a contract fragment: this is what
    gets enforced and what gets digested, and keeping it flat means the digest
    cannot drift when an unrelated part of the contract changes.
    """

    allowed_tools: Optional[Tuple[str, ...]] = None
    denied_tools: Tuple[str, ...] = ()
    allowed_models: Optional[Tuple[str, ...]] = None
    denied_models: Tuple[str, ...] = ()
    allowed_use_cases: Optional[Tuple[str, ...]] = None
    denied_use_cases: Tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        *,
        allowed_tools: Optional[Iterable[str]] = None,
        denied_tools: Iterable[str] = (),
        allowed_models: Optional[Iterable[str]] = None,
        denied_models: Iterable[str] = (),
        allowed_use_cases: Optional[Iterable[str]] = None,
        denied_use_cases: Iterable[str] = (),
    ) -> "EffectivePolicy":
        return cls(
            allowed_tools=_norm(allowed_tools),
            denied_tools=_norm(denied_tools) or (),
            allowed_models=_norm(allowed_models),
            denied_models=_norm(denied_models) or (),
            allowed_use_cases=_norm(allowed_use_cases),
            denied_use_cases=_norm(denied_use_cases) or (),
        )

    def as_canonical_mapping(self) -> Dict[str, Any]:
        """The exact object the digest is taken over."""
        return {
            "allowedTools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "deniedTools": list(self.denied_tools),
            "allowedModels": list(self.allowed_models) if self.allowed_models is not None else None,
            "deniedModels": list(self.denied_models),
            "allowedUseCases": (
                list(self.allowed_use_cases) if self.allowed_use_cases is not None else None
            ),
            "deniedUseCases": list(self.denied_use_cases),
        }


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """RFC 8785 (JCS) serialisation, with a documented stdlib fallback.

    ``rfc8785`` is a declared dependency. The fallback exists so that importing
    this module never fails in a partially-installed environment, but it is NOT
    equivalent: JCS and ``json.dumps`` differ on number formatting (notably
    exponent case and float shortest-round-trip), so a digest produced under the
    fallback can differ from a conformant one. The scheme discriminator on the
    digest records which was used, so a mismatch is legible instead of silent.
    """
    try:
        import rfc8785

        return rfc8785.dumps(dict(payload))
    except ImportError:  # pragma: no cover - dependency is declared
        return json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def _digest_scheme() -> str:
    try:
        import rfc8785  # noqa: F401

        return "jcs-sha256"
    except ImportError:  # pragma: no cover
        return "sortkeys-sha256"


def policy_digest(policy: EffectivePolicy) -> str:
    """Stable identifier for the rules that were enforced.

    Returned as ``"<scheme>:<hex>"``. The scheme is part of the value on
    purpose: a digest whose algorithm is implied rather than stated cannot be
    migrated without silently invalidating every stored record, which is the
    trap the plan-digest work is separately untangling.
    """
    payload = policy.as_canonical_mapping()
    return f"{_digest_scheme()}:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


@dataclass(frozen=True)
class Decision:
    """What was decided, why, and under which policy."""

    allow: bool
    reason: ReasonCode
    policy_digest: str
    tool: Optional[str] = None
    model_id: Optional[str] = None
    use_case: Optional[str] = None

    def to_record(self) -> Dict[str, Any]:
        """A decision record: the minimum an auditor needs, and nothing more.

        No timestamp and no caller identity. Both belong to the control plane
        that persists this — the engine has no clock it can be trusted about and
        no notion of who is asking. Keeping them out is what lets this function
        stay pure and lets the same inputs be replayed to the same record.
        """
        return {
            "allow": self.allow,
            "reasonCode": self.reason.value,
            "policyDigest": self.policy_digest,
            "request": {
                "tool": self.tool,
                "modelId": self.model_id,
                "useCase": self.use_case,
            },
        }


def decide(
    *,
    policy: EffectivePolicy,
    tool: Optional[str] = None,
    model_id: Optional[str] = None,
    use_case: Optional[str] = None,
    digest: Optional[str] = None,
) -> Decision:
    """Evaluate ``policy`` against one request. Pure and total.

    ``digest`` may be supplied by a caller that has already computed it for this
    policy, so a server handling many calls under one policy does not re-hash on
    every request. It is not otherwise used.
    """
    resolved_digest = digest if digest is not None else policy_digest(policy)

    def _denied(reason: ReasonCode) -> Decision:
        return Decision(
            allow=False,
            reason=reason,
            policy_digest=resolved_digest,
            tool=tool,
            model_id=model_id,
            use_case=use_case,
        )

    for candidate in CHECK_ORDER:
        if _fires(candidate, policy, tool, model_id, use_case):
            return _denied(candidate)

    return Decision(
        allow=True,
        reason=ReasonCode.ALLOWED,
        policy_digest=resolved_digest,
        tool=tool,
        model_id=model_id,
        use_case=use_case,
    )


def _fires(
    reason: ReasonCode,
    policy: EffectivePolicy,
    tool: Optional[str],
    model_id: Optional[str],
    use_case: Optional[str],
) -> bool:
    """Does this single rule deny the request? One branch per reason code.

    Written as a lookup rather than a chain so that :data:`CHECK_ORDER` is the
    only thing that decides precedence. Adding a reason code without adding its
    predicate here raises immediately, so the enum and the logic cannot drift.
    """
    if reason is ReasonCode.TOOL_NOT_ALLOWED:
        if tool is None:
            return False
        if tool in policy.denied_tools:
            return True
        return policy.allowed_tools is not None and tool not in policy.allowed_tools

    if reason is ReasonCode.MISSING_MODEL_IDENTITY:
        if model_id:
            return False
        # Inert when the policy says nothing about models: denying here refused
        # every spec-compliant MCP client, since model identity is not part of
        # the MCP `initialize` handshake.
        return policy.allowed_models is not None or bool(policy.denied_models)

    if reason is ReasonCode.IN_DENIED_MODELS:
        return bool(model_id) and model_id in policy.denied_models

    if reason is ReasonCode.IN_DENIED_USE_CASES:
        return bool(use_case) and use_case in policy.denied_use_cases

    if reason is ReasonCode.NOT_IN_ALLOWED_MODELS:
        return (
            bool(model_id)
            and policy.allowed_models is not None
            and model_id not in policy.allowed_models
        )

    if reason is ReasonCode.MISSING_USE_CASE_WITH_ALLOWLIST:
        return not use_case and policy.allowed_use_cases is not None

    if reason is ReasonCode.NOT_IN_ALLOWED_USE_CASES:
        return (
            bool(use_case)
            and policy.allowed_use_cases is not None
            and use_case not in policy.allowed_use_cases
        )

    raise AssertionError(  # pragma: no cover - guarded by test_every_code_has_a_predicate
        f"{reason!r} is in ReasonCode but has no predicate in _fires(); "
        "every code must be decidable or CHECK_ORDER silently skips it."
    )


def as_tuple(decision: Decision) -> Tuple[bool, Optional[str]]:
    """Adapter to the ``(allowed, reason_or_None)`` shape the gates return today."""
    return decision.allow, (None if decision.allow else decision.reason.value)


def check_order_codes() -> Sequence[str]:
    """The normative order as wire values — for docs and vector generation."""
    return [code.value for code in CHECK_ORDER]
