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

"""Replay the portable agentPolicy decision vectors against this engine.

The vectors are data, not Python: any implementation in any language can read
``fluid_build/policy/data/vectors/agent-policy-vectors.json`` and check itself.
This file is simply the first implementation doing so, and it is what stops the
vectors and the code drifting apart in the meantime.

The vectors ship inside the package so a consumer who has installed
``data-product-forge`` can point their own gate at them without cloning
anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.policy.decision import (
    CHECK_ORDER,
    EffectivePolicy,
    ReasonCode,
    decide,
    policy_digest,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "fluid_build"
    / "policy"
    / "data"
    / "vectors"
    / "agent-policy-vectors.json"
)

DOC: Dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
VECTORS: List[Dict[str, Any]] = DOC["vectors"]


def _policy_of(mapping: Dict[str, Any]) -> EffectivePolicy:
    return EffectivePolicy.of(
        allowed_tools=mapping["allowedTools"],
        denied_tools=mapping["deniedTools"],
        allowed_models=mapping["allowedModels"],
        denied_models=mapping["deniedModels"],
        allowed_use_cases=mapping["allowedUseCases"],
        denied_use_cases=mapping["deniedUseCases"],
        # Absent by design on a policy with no jurisdiction rules — that
        # absence is what keeps its digest identical to the pre-feature value.
        allowed_caller_jurisdictions=mapping.get("allowedCallerJurisdictions"),
        denied_caller_jurisdictions=mapping.get("deniedCallerJurisdictions") or (),
    )


# ---------------------------------------------------------------------------
# guard the guard: a vector file that tests nothing must fail loudly
# ---------------------------------------------------------------------------


def test_vector_file_is_populated() -> None:
    assert VECTORS, "the vector file is empty — this suite would pass vacuously"
    assert len(VECTORS) >= 30


def test_vectors_cover_every_reason_code() -> None:
    """A code with no vector is a promise nothing checks."""
    covered = {v["expect"]["reasonCode"] for v in VECTORS}
    missing = {c.value for c in ReasonCode} - covered
    assert not missing, f"reason codes with no vector: {sorted(missing)}"


def test_vectors_cover_every_ordering_pair_that_can_conflict() -> None:
    """At least one vector must pin each adjacent precedence step.

    Without this, a reordering of CHECK_ORDER could pass the suite by only
    breaking pairs nothing exercises.
    """
    pinned = {v["expect"]["reasonCode"] for v in VECTORS if v["name"].startswith("precedence:")}
    assert len(pinned) >= 4, f"only {len(pinned)} distinct precedence outcomes are pinned"


def test_declared_check_order_matches_the_implementation() -> None:
    assert DOC["checkOrder"] == [c.value for c in CHECK_ORDER]


def test_declared_reason_codes_match_the_implementation() -> None:
    assert set(DOC["reasonCodes"]) == {c.value for c in ReasonCode}


def test_vector_names_are_unique() -> None:
    names = [v["name"] for v in VECTORS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# the vectors themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_vector(vector: Dict[str, Any]) -> None:
    policy = _policy_of(vector["policy"])
    request = vector["request"]
    expect = vector["expect"]

    decision = decide(
        policy=policy,
        tool=request["tool"],
        model_id=request["modelId"],
        use_case=request["useCase"],
        caller_jurisdiction=request.get("callerJurisdiction"),
        caller_jurisdiction_verified=bool(request.get("callerJurisdictionVerified")),
    )

    assert (
        decision.allow is expect["allow"]
    ), f"{vector['name']}: expected allow={expect['allow']}, got {decision.allow}"
    assert decision.reason.value == expect["reasonCode"], (
        f"{vector['name']}: expected reasonCode={expect['reasonCode']!r}, "
        f"got {decision.reason.value!r}"
    )
    assert decision.policy_digest == expect["policyDigest"], (
        f"{vector['name']}: policyDigest changed. If this was deliberate the digest "
        f"scheme must be bumped, not edited in place — stored decision records "
        f"reference the old value.\n"
        f"  expected {expect['policyDigest']}\n  got      {decision.policy_digest}"
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_vector_digest_is_reproducible_from_the_policy_alone(vector: Dict[str, Any]) -> None:
    """The digest must depend only on the policy, never on the request."""
    assert policy_digest(_policy_of(vector["policy"])) == vector["expect"]["policyDigest"]


def test_an_unverified_claim_has_a_vector() -> None:
    """The design's load-bearing rule must be pinned by data, not only by code.

    If the verified-only rule regressed, a vector asserting that a caller who
    self-asserts the right answer is still refused is what catches it.
    """
    unverified = [
        v
        for v in VECTORS
        if v["request"].get("callerJurisdiction")
        and not v["request"].get("callerJurisdictionVerified")
    ]
    assert unverified, "no vector exercises an unverified jurisdiction claim"
    assert all(
        v["expect"]["allow"] is False for v in unverified
    ), "an unverified claim satisfied a rule in some vector"
