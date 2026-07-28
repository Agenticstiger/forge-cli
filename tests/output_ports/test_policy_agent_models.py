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

"""Unit coverage for the v0.7.4 agentPolicy runtime gate.

These tests pin the behaviour of
:meth:`OutputPortPolicy.is_model_allowed`,
:meth:`OutputPortPolicy.is_use_case_allowed`,
:meth:`OutputPortPolicy.check_tool_call`, and
:meth:`OutputPortPolicy.from_contract_and_flags` so a future
refactor of the gate logic can't silently regress the contract
enforcement guarantee.

Also covers the audit-event shape: every decision (allow + deny)
must carry the eight fields a downstream `agt verify`-style tool
needs to produce evidence.

The 14 scenarios match the matrix in
``/Users/speculator55005/.claude/plans/can-we-integrate-this-harmonic-rain.md``
section "Live LLM test scenario matrix > Unit".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.output_ports.mcp.policy import OutputPortPolicy

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _expose(
    *,
    allowed_models: Optional[list] = None,
    denied_models: Optional[list] = None,
    allowed_use_cases: Optional[list] = None,
    denied_use_cases: Optional[list] = None,
) -> Dict[str, Any]:
    """Build a minimal expose with the requested agentPolicy fields."""
    agent_policy: Dict[str, Any] = {}
    if allowed_models is not None:
        agent_policy["allowedModels"] = allowed_models
    if denied_models is not None:
        agent_policy["deniedModels"] = denied_models
    if allowed_use_cases is not None:
        agent_policy["allowedUseCases"] = allowed_use_cases
    if denied_use_cases is not None:
        agent_policy["deniedUseCases"] = denied_use_cases
    expose: Dict[str, Any] = {"exposeId": "demo", "kind": "table"}
    if agent_policy:
        expose["policy"] = {"agentPolicy": agent_policy}
    return expose


# ---------------------------------------------------------------------
# U1 — happy path: model in allowedModels, use_case in allowedUseCases
# ---------------------------------------------------------------------


def test_u1_allow_when_model_and_use_case_both_match():
    expose = _expose(
        allowed_models=["claude-haiku-4-5-20251001"],
        allowed_use_cases=["analysis"],
    )
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(
        tool="sample",
        model_id="claude-haiku-4-5-20251001",
        use_case="analysis",
    )
    assert allowed is True
    assert reason is None
    assert policy.policy_source == "contract"


# ---------------------------------------------------------------------
# U2 — model NOT in allowedModels
# ---------------------------------------------------------------------


def test_u2_deny_when_model_not_in_allowed_models():
    expose = _expose(allowed_models=["claude-haiku-4-5-20251001"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(
        tool="sample", model_id="claude-3-opus", use_case="analysis"
    )
    assert allowed is False
    assert reason == "not-in-allowedModels"


# ---------------------------------------------------------------------
# U3 — model in deniedModels (explicit deny)
# ---------------------------------------------------------------------


def test_u3_deny_when_model_in_denied_models():
    expose = _expose(denied_models=["claude-3-opus"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(tool="sample", model_id="claude-3-opus", use_case=None)
    assert allowed is False
    assert reason == "in-deniedModels"


# ---------------------------------------------------------------------
# U4 — model in BOTH lists; denylist precedence wins
# ---------------------------------------------------------------------


def test_u4_denylist_wins_over_allowlist():
    expose = _expose(
        allowed_models=["claude-haiku", "claude-3-opus"],
        denied_models=["claude-3-opus"],
    )
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(tool="sample", model_id="claude-3-opus", use_case=None)
    assert allowed is False
    assert reason == "in-deniedModels", "denylist must win over allowlist"


# ---------------------------------------------------------------------
# U5 — both lists empty (backward compat: no agentPolicy = no gate)
# ---------------------------------------------------------------------


def test_u5_no_policy_allows_any_model_with_identity():
    expose = _expose()  # no agentPolicy at all
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    assert policy.policy_source == "default"
    allowed, reason = policy.check_tool_call(tool="sample", model_id="anything", use_case=None)
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------
# U6 — use-case NOT in allowedUseCases
# ---------------------------------------------------------------------


def test_u6_deny_when_use_case_not_in_allowed_use_cases():
    expose = _expose(allowed_use_cases=["analysis", "qa"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(
        tool="sample", model_id="claude-haiku", use_case="training"
    )
    assert allowed is False
    assert reason == "not-in-allowedUseCases"


# ---------------------------------------------------------------------
# U7 — use-case in deniedUseCases (precedence over allowlist)
# ---------------------------------------------------------------------


def test_u7_use_case_denylist_precedence():
    expose = _expose(
        allowed_use_cases=["analysis", "training"],
        denied_use_cases=["training"],
    )
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(
        tool="sample", model_id="claude-haiku", use_case="training"
    )
    assert allowed is False
    assert reason == "in-deniedUseCases"


# ---------------------------------------------------------------------
# U8 — model allowed, use-case denied (composite reason)
# ---------------------------------------------------------------------


def test_u8_combined_model_allowed_use_case_denied():
    expose = _expose(
        allowed_models=["claude-haiku"],
        denied_use_cases=["training"],
    )
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(
        tool="sample", model_id="claude-haiku", use_case="training"
    )
    assert allowed is False
    assert reason == "in-deniedUseCases", "use-case deny reason should surface (model passed)"


# ---------------------------------------------------------------------
# U9 — missing clientInfo.model (no identity) → fail-closed
# ---------------------------------------------------------------------


def test_u9_missing_identity_fails_closed_when_gate_present():
    expose = _expose(allowed_models=["claude-haiku"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(tool="sample", model_id=None, use_case=None)
    assert allowed is False
    assert reason == "missing-model-identity"


def test_u9b_missing_identity_still_denied_when_only_a_denylist_exists():
    """A denylist needs identity too — otherwise a denied model slips the
    gate simply by omitting the (non-standard) ``model`` field."""
    expose = _expose(denied_models=["gpt-4"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    allowed, reason = policy.check_tool_call(tool="sample", model_id=None, use_case=None)
    assert allowed is False
    assert reason == "missing-model-identity"


def test_u9c_model_gate_is_inert_when_no_model_policy_is_declared():
    """With NO agentPolicy and no CLI model flags there is nothing to
    enforce, so the gate must not deny. Model identity is not part of the
    MCP spec — ``Implementation`` carries {name, version} only — so an
    unconditional deny refused every call, ``describe`` included, from
    every spec-compliant client while the server reported itself healthy.
    Mirrors ``is_use_case_allowed``'s "no allowlist ⇒ pass"."""
    policy = OutputPortPolicy.from_contract_and_flags(expose=_expose())
    assert policy.policy_source == "default"
    allowed, reason = policy.check_tool_call(tool="describe", model_id=None, use_case=None)
    assert allowed is True
    assert reason is None
    # …and an identified caller is still evaluated normally.
    allowed, reason = policy.check_tool_call(tool="describe", model_id="any-model", use_case=None)
    assert allowed is True


def test_u9d_inert_model_gate_does_not_weaken_the_tool_gate():
    policy = OutputPortPolicy.from_contract_and_flags(expose=_expose(), denied_tools=("query_sql",))
    allowed, reason = policy.check_tool_call(tool="query_sql", model_id=None, use_case=None)
    assert allowed is False
    assert reason == "tool-not-allowed"


# ---------------------------------------------------------------------
# U10 — CLI override wins over contract; policy_source records "cli"
# ---------------------------------------------------------------------


def test_u10_cli_override_replaces_contract_value_and_records_source():
    expose = _expose(allowed_models=["claude-haiku"])
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("gpt-4o-mini",),
    )
    assert policy.policy_source == "cli"
    assert policy.allowed_models == ("gpt-4o-mini",)
    # The contract's claude-haiku is no longer in the allowlist
    allowed, reason = policy.check_tool_call(tool="sample", model_id="claude-haiku", use_case=None)
    assert allowed is False
    assert reason == "not-in-allowedModels"


# ---------------------------------------------------------------------
# U11 — contract used when CLI is unset; policy_source records "contract"
# ---------------------------------------------------------------------


def test_u11_contract_used_when_cli_unset():
    expose = _expose(allowed_models=["claude-haiku"], denied_use_cases=["training"])
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    assert policy.policy_source == "contract"
    assert policy.allowed_models == ("claude-haiku",)
    assert policy.denied_use_cases == ("training",)


# ---------------------------------------------------------------------
# U12 — agentPolicy completely absent → all-allow + source "default"
# ---------------------------------------------------------------------


def test_u12_default_when_no_agent_policy():
    expose = _expose()
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    assert policy.policy_source == "default"
    assert policy.allowed_models is None
    assert policy.denied_models == ()
    assert policy.allowed_use_cases is None
    assert policy.denied_use_cases == ()


# ---------------------------------------------------------------------
# U13 — tool-level denylist wins independently of model gate
# ---------------------------------------------------------------------


def test_u13_tool_denylist_wins_independently():
    expose = _expose(allowed_models=["claude-haiku"])
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        denied_tools=("sample",),
    )
    allowed, reason = policy.check_tool_call(tool="sample", model_id="claude-haiku", use_case=None)
    assert allowed is False
    assert reason == "tool-not-allowed"


# ---------------------------------------------------------------------
# U15 / U16 — sliding-window rate limit on SessionState
# ---------------------------------------------------------------------


def test_u15_rate_limit_window_denies_after_threshold():
    """Sliding-window rate limit blocks the (N+1)th call within the
    window; allow + deny independently counted."""
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    state = SessionState(
        contract={},
        expose={"exposeId": "demo"},
        policy=OutputPortPolicy.from_contract_and_flags(expose={"exposeId": "demo"}),
        logger=logging.getLogger("test"),
        rate_limit_calls=3,
        rate_limit_window_seconds=60.0,
    )
    for _ in range(3):
        ok, reason = state.check_rate_limit()
        assert ok is True
        assert reason is None
    ok, reason = state.check_rate_limit()
    assert ok is False
    assert "rate-limit-exceeded" in (reason or "")


def test_u16_rate_limit_zero_disables_gate():
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    state = SessionState(
        contract={},
        expose={"exposeId": "demo"},
        policy=OutputPortPolicy.from_contract_and_flags(expose={"exposeId": "demo"}),
        logger=logging.getLogger("test"),
        rate_limit_calls=0,
    )
    for _ in range(500):
        assert state.check_rate_limit() == (True, None)


# ---------------------------------------------------------------------
# U14 — audit-event shape: every decision carries the 8 expected fields
# ---------------------------------------------------------------------


def test_u14_audit_event_shape_carries_eight_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit writer is library-level (write_audit_event); the gateway
    in server.py builds the payload. We pin the payload shape here so
    the OWASP-evidence re-emitter can be built against a stable
    contract."""
    audit_root = tmp_path / "audit"
    payload = {
        "tool": "sample",
        "exposeId": "demo",
        "contractPath": "/abs/path/to/contract.fluid.yaml",
        "modelId": "claude-haiku-4-5-20251001",
        "useCase": "analysis",
        "decision": "allow",
        "reason": None,
        "policySource": "contract",
        "argumentSummary": {"limit": 5},
    }
    landed = write_audit_event("data_access", payload=payload, root=audit_root)
    import json

    doc = json.loads(landed.read_text())
    assert doc["event"] == "data_access"
    assert doc["payload"].keys() == {
        "tool",
        "exposeId",
        "contractPath",
        "modelId",
        "useCase",
        "decision",
        "reason",
        "policySource",
        "argumentSummary",
    }
    assert "timestamp_utc" in doc
