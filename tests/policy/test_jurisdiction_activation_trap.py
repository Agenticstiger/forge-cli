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

"""Activating the jurisdiction gate today would refuse 100% of calls.

`OutputPortPolicy.allowed_caller_jurisdictions` carries a long note saying so.
A note is not evidence, and the previous version of that note was wrong about
which piece was missing -- it blamed the HTTP transport, which exists. So the
claim is pinned here instead, where it either holds or goes red.

The gap is that NOTHING extracts a caller's jurisdiction from any credential.
`check_tool_call` has no parameter to carry one, so the claim reaching
`decide()` is always None. Fail-closed is correct once a claim can arrive;
until then, turning the rule on makes every call fail.

If you are here because this file went red: that is the intended signal that
someone wired the identity producer. Good -- update these tests to assert the
new behaviour rather than deleting them.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict

from fluid_build.output_ports.mcp.policy import OutputPortPolicy
from fluid_build.policy.decision import ReasonCode, decide

_EXPOSE: Dict[str, Any] = {"exposeId": "e", "kind": "table"}


def _eu_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "p",
        "sovereignty": {"jurisdiction": "EU"},
    }


def test_check_tool_call_cannot_carry_a_jurisdiction() -> None:
    """The identity producer is missing, and this is the shape of its absence."""
    params = set(inspect.signature(OutputPortPolicy.check_tool_call).parameters)
    assert "caller_jurisdiction" not in params, (
        "check_tool_call now accepts a jurisdiction -- the identity producer may "
        "have landed. Re-read the note on allowed_caller_jurisdictions and update "
        "these tests; the gate may now be safe to activate."
    )


def test_a_pinned_contract_would_refuse_every_call_if_activated() -> None:
    """The concrete trap: opt in via `contract=`, and nothing gets through.

    Not a hypothetical -- this is exactly what `_build_policy` would produce if
    it passed the root contract, which is why it does not.
    """
    policy = OutputPortPolicy.from_contract_and_flags(expose=_EXPOSE, contract=_eu_contract())
    assert policy.allowed_caller_jurisdictions == ("EU",), "derivation did not fire"

    effective = policy.to_effective_policy()
    assert effective.has_jurisdiction_rules, "rule did not reach the effective policy"

    # check_tool_call has no way to supply a claim, so this is what every call gets.
    outcome = decide(
        policy=effective,
        tool="query",
        model_id=None,
        use_case=None,
        caller_jurisdiction=None,
        caller_jurisdiction_verified=False,
    )
    assert not outcome.allow
    assert outcome.reason is ReasonCode.MISSING_CALLER_JURISDICTION


def test_an_unpinned_contract_stays_inert_so_activation_is_not_uniformly_fatal() -> None:
    """The negative control. Without it the test above proves only that something refuses.

    Most contracts declare no jurisdiction, and for those the opt-in is harmless --
    which is why the trap is easy to miss in a smoke test.
    """
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=_EXPOSE,
        contract={"fluidVersion": "0.7.5", "kind": "DataProduct", "id": "p"},
    )
    assert policy.allowed_caller_jurisdictions is None
    assert not policy.to_effective_policy().has_jurisdiction_rules


def test_build_policy_does_not_pass_the_contract() -> None:
    """The deferral itself, pinned where a future edit trips over it."""
    from fluid_build.cli import mcp_output_port

    src = inspect.getsource(mcp_output_port._build_policy)
    assert "contract=" not in src, (
        "_build_policy now passes the root contract, activating the jurisdiction "
        "gate. Unless the identity producer landed too, every call against a "
        "contract with a pinned jurisdiction now fails closed."
    )
