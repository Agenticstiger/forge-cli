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

"""The audit record must say WHICH rules produced a decision.

`policySource` says where the rules came from — "contract" or "cli". It does
not distinguish the contract before an `allowedModels` edit from the contract
after it. Without a digest, a record reading `deny / not-in-allowedModels`
cannot be reconstructed once the contract moves on: you know a call was refused
and you cannot say what it was refused against.

`policy_digest()` has existed for exactly this, with "for the audit record" in
its own docstring, and was wired to nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from fluid_build.output_ports.mcp.policy import OutputPortPolicy
from fluid_build.output_ports.mcp.server import OutputPortMcpServer

_EXPOSE: Dict[str, Any] = {"exposeId": "demo", "kind": "table"}


def _server(expose: Mapping[str, Any], **flags: Any) -> OutputPortMcpServer:
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose, **flags)
    return OutputPortMcpServer(
        contract={"fluidVersion": "0.7.5", "kind": "DataProduct", "id": "d", "exposes": [expose]},
        expose=dict(expose),
        policy=policy,
        logger=logging.getLogger("test.audit_digest"),
    )


def _payload(server: OutputPortMcpServer, tool: str = "describe") -> Dict[str, Any]:
    payload, _allowed, _reason = server._evaluate_policy(
        tool_name=tool, arguments={}, model_id=None, use_case=None
    )
    return payload


def test_the_audit_record_carries_the_policy_digest() -> None:
    payload = _payload(_server(_EXPOSE))
    assert "policyDigest" in payload, (
        "the audit record names a decision's reason but not the rules that produced "
        "it — the record cannot be reconstructed once the contract changes"
    )
    assert payload["policyDigest"].startswith("jcs-sha256:")


def test_the_digest_matches_the_policy_it_was_taken_from() -> None:
    """Not any digest — the one for the rules actually in force."""
    server = _server(_EXPOSE)
    assert _payload(server)["policyDigest"] == server.state.policy.policy_digest()


def test_policy_source_is_still_recorded() -> None:
    """The new field adds to the record; it does not replace what was there."""
    payload = _payload(_server(_EXPOSE))
    assert payload["policySource"] == "default"
    assert payload["reason"] is None or isinstance(payload["reason"], str)


def test_different_rules_produce_a_different_digest() -> None:
    """The point of the field. Two policies that differ must not look alike.

    Without this, the test above is satisfied by a constant string, and a
    constant would tell an auditor nothing at all.
    """
    permissive = _payload(_server(_EXPOSE))["policyDigest"]
    restricted = _payload(_server(_EXPOSE, denied_tools=("query_sql",)))["policyDigest"]
    assert permissive != restricted


def test_the_same_rules_produce_the_same_digest_across_servers() -> None:
    """The other half: it has to be stable, or it cannot be correlated.

    Two independently-constructed servers with identical rules must agree,
    which is what RFC 8785 canonicalisation buys and why a raw hash of the
    YAML text would not do.
    """
    assert _payload(_server(_EXPOSE))["policyDigest"] == _payload(_server(_EXPOSE))["policyDigest"]


def test_a_denied_call_records_the_digest_too() -> None:
    """A denial is the record most likely to be read months later."""
    server = _server(_EXPOSE, denied_tools=("describe",))
    payload, allowed, reason = server._evaluate_policy(
        tool_name="describe", arguments={}, model_id=None, use_case=None
    )
    assert not allowed
    assert reason
    assert payload["policyDigest"] == server.state.policy.policy_digest()
