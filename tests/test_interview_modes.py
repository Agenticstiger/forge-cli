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

"""Pin the mode-aware interview short-circuits.

The user's complaint: picking ``compose`` or ``refine`` ran the same
generic 'tell us about your data product' interview as a fresh-product
flow, even though the system already knew the upstream schemas / the
existing contract. World-class fix: each mode has its own minimal
interview that asks ONLY for the user-specific delta.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest import mock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Console:
    def __init__(self):
        self.lines: List[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))


def _state_with_context(ctx: Dict[str, Any]):
    """Produce a CopilotInterviewState seeded with *ctx*."""
    from fluid_build.cli.forge_copilot_interview import (
        CopilotInterviewState,
    )

    state = CopilotInterviewState()
    state.normalized_context = dict(ctx)
    return state


# ---------------------------------------------------------------------------
# _detect_interview_mode
# ---------------------------------------------------------------------------


def test_detect_mode_compose_when_composition_set():
    from fluid_build.cli.forge_copilot_interview import _detect_interview_mode

    assert (
        _detect_interview_mode({"composition": {"upstream_products": [{"id": "x.y.z"}]}})
        == "compose"
    )


def test_detect_mode_refine_when_existing_contract_set():
    from fluid_build.cli.forge_copilot_interview import _detect_interview_mode

    assert _detect_interview_mode({"refine_existing_contract": {"id": "x"}}) == "refine"


def test_detect_mode_standard_otherwise():
    from fluid_build.cli.forge_copilot_interview import _detect_interview_mode

    assert _detect_interview_mode({}) == "standard"
    assert _detect_interview_mode({"composition": {}}) == "standard"


# ---------------------------------------------------------------------------
# Compose interview — 3 questions max, skips DDL/sample/domain
# ---------------------------------------------------------------------------


def _compose_context() -> Dict[str, Any]:
    return {
        "composition": {
            "target_type": "ADP",
            "upstream_products": [
                {
                    "id": "bronze.commerce.orders_v1",
                    "name": "Orders",
                    "productType": "SDP",
                    "layer": "Bronze",
                    "domain": "commerce",
                    "exposes": [
                        {
                            "exposeId": "main_output",
                            "schema": [
                                {"name": "order_id", "type": "string"},
                                {"name": "customer_id", "type": "string"},
                                {"name": "amount", "type": "decimal"},
                            ],
                        }
                    ],
                },
                {
                    "id": "bronze.commerce.customers_v1",
                    "name": "Customers",
                    "productType": "SDP",
                    "layer": "Bronze",
                    "domain": "commerce",
                    "exposes": [
                        {
                            "exposeId": "main_output",
                            "schema": [
                                {"name": "customer_id", "type": "string"},
                                {"name": "email", "type": "string"},
                            ],
                        }
                    ],
                },
            ],
        }
    }


def test_compose_interview_asks_at_most_three_questions():
    """Compose mode must NOT ask the full standard bootstrap."""
    from fluid_build.cli.forge_copilot_interview import _run_compose_interview

    asked: List[str] = []

    def _stub_ask(_console, prompt, **_kw):
        asked.append(prompt)
        # Goal / type / join keys answers in order
        if "goal" in prompt.lower():
            return "Customer 360 view with order history"
        if "type" in prompt.lower():
            return "ADP"
        if "join keys" in prompt.lower():
            return ""  # accept default
        return ""

    state = _state_with_context(_compose_context())
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_compose_interview(state, _Console())

    # The compose flow asks at most 3 questions — and never the
    # generic "data sources" / "DDL" / "sample" / "industry" ones.
    assert len(asked) <= 3, f"compose asked {len(asked)} questions: {asked}"
    joined = "\n".join(asked).lower()
    for forbidden in ("data source", "ddl", "sample", "industry", "use case"):
        assert (
            forbidden not in joined
        ), f"compose interview asked a generic question that the system already knows: {forbidden!r}"


def test_compose_interview_prefills_consumes_from_upstreams():
    from fluid_build.cli.forge_copilot_interview import _run_compose_interview

    def _stub_ask(_c, prompt, **_kw):
        return "Customer 360" if "goal" in prompt.lower() else ""

    state = _state_with_context(_compose_context())
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_compose_interview(state, None)

    consumes = state.normalized_context.get("consumes") or []
    assert len(consumes) == 2
    ids = {c["productId"] for c in consumes}
    assert ids == {
        "bronze.commerce.orders_v1",
        "bronze.commerce.customers_v1",
    }
    for c in consumes:
        assert "exposeId" in c, "consumes[] must carry exposeId for v0.7.3 schema"


def test_compose_interview_infers_domain_from_upstreams():
    from fluid_build.cli.forge_copilot_interview import _run_compose_interview

    def _stub_ask(_c, _p, **_kw):
        return ""

    state = _state_with_context(_compose_context())
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_compose_interview(state, None)

    assert state.normalized_context.get("domain") == "commerce"


def test_compose_interview_suggests_join_keys_from_schema_overlap():
    from fluid_build.cli.forge_copilot_interview import _suggest_join_keys

    upstreams = _compose_context()["composition"]["upstream_products"]
    suggestions = _suggest_join_keys(upstreams)
    # Both upstreams have customer_id → should be suggested
    assert "customer_id" in suggestions


def test_compose_interview_records_target_type():
    from fluid_build.cli.forge_copilot_interview import _run_compose_interview

    def _stub_ask(_c, prompt, **_kw):
        if "type" in prompt.lower():
            return "CDP"
        return "x" if "goal" in prompt.lower() else ""

    state = _state_with_context(_compose_context())
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_compose_interview(state, None)

    assert state.normalized_context.get("data_product_type") == "CDP"
    assert state.normalized_context.get("layer") == "Gold"


# ---------------------------------------------------------------------------
# Refine interview — one question + existing contract loaded as seed
# ---------------------------------------------------------------------------


def _refine_context(tmp_path) -> Dict[str, Any]:
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text("kind: DataProduct\nid: x.y.z\n")
    return {
        "refine_existing_contract": {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.commerce.orders_v1",
            "name": "Orders",
            "domain": "commerce",
            "metadata": {"layer": "Bronze", "productType": "SDP"},
            "exposes": [{"exposeId": "main_output"}],
        },
        "refine_contract_path": str(contract_path),
    }


def test_refine_interview_asks_only_what_to_change(tmp_path):
    """Refine mode must NOT run the new-product interview."""
    from fluid_build.cli.forge_copilot_interview import _run_refine_interview

    asked: List[str] = []

    def _stub_ask(_c, prompt, **_kw):
        asked.append(prompt)
        return "Add an LTV measure" if "change" in prompt.lower() else ""

    state = _state_with_context(_refine_context(tmp_path))
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_refine_interview(state, None)

    assert len(asked) == 1, f"refine should ask one question, got {len(asked)}: {asked}"
    assert "change" in asked[0].lower()


def test_refine_interview_records_change_request_and_seed_override(tmp_path):
    from fluid_build.cli.forge_copilot_interview import _run_refine_interview

    def _stub_ask(_c, _p, **_kw):
        return "Switch engine from sql to dbt"

    state = _state_with_context(_refine_context(tmp_path))
    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _stub_ask):
        _run_refine_interview(state, None)

    assert state.normalized_context.get("refine_request") == "Switch engine from sql to dbt"
    seed = state.normalized_context.get("seed_contract_override")
    assert isinstance(seed, dict)
    assert seed["id"] == "bronze.commerce.orders_v1"


def test_refine_seed_override_returned_by_build_seed_contract(tmp_path):
    """build_seed_contract must return the existing contract verbatim
    when refine override is set — the LLM modifies the user's contract,
    not a new one."""
    from fluid_build.cli.forge_copilot_contract_helpers import build_seed_contract

    class _Discovery:
        sample_files = []

    ctx = {
        "seed_contract_override": {
            "kind": "DataProduct",
            "id": "bronze.commerce.orders_v1",
            "fluidVersion": "0.7.3",
            "metadata": {"layer": "Bronze", "productType": "SDP"},
        }
    }
    seed = build_seed_contract(
        context=ctx,
        discovery_report=_Discovery(),
        template_name="starter",
        provider_name="local",
        project_memory=None,
        map_inferred_type_fn=lambda t: "string",
    )
    assert seed["id"] == "bronze.commerce.orders_v1"


# ---------------------------------------------------------------------------
# Mode detection wired into run_adaptive_copilot_interview
# ---------------------------------------------------------------------------


def test_run_adaptive_copilot_interview_short_circuits_on_compose():
    """The runtime must NOT run the bootstrap questions when composition
    context is present."""
    from fluid_build.cli import forge_copilot_interview as iv_mod

    bootstrap_called = {"count": 0}

    def _spy_bootstrap(*_args, **_kw):
        bootstrap_called["count"] += 1

    with (
        mock.patch.object(iv_mod, "_ask_bootstrap_questions", _spy_bootstrap),
        mock.patch.object(iv_mod, "_run_compose_interview", lambda *_a, **_kw: None),
    ):
        iv_mod.run_adaptive_copilot_interview(
            initial_context=_compose_context(),
            console=None,
            llm_config=mock.MagicMock(),
            discovery_report=mock.MagicMock(sample_files=[]),
            capability_matrix={},
            project_memory=None,
        )

    assert (
        bootstrap_called["count"] == 0
    ), "compose mode must short-circuit the standard bootstrap interview"


def test_run_adaptive_copilot_interview_short_circuits_on_refine(tmp_path):
    from fluid_build.cli import forge_copilot_interview as iv_mod

    bootstrap_called = {"count": 0}

    def _spy_bootstrap(*_args, **_kw):
        bootstrap_called["count"] += 1

    with (
        mock.patch.object(iv_mod, "_ask_bootstrap_questions", _spy_bootstrap),
        mock.patch.object(iv_mod, "_run_refine_interview", lambda *_a, **_kw: None),
    ):
        iv_mod.run_adaptive_copilot_interview(
            initial_context=_refine_context(tmp_path),
            console=None,
            llm_config=mock.MagicMock(),
            discovery_report=mock.MagicMock(sample_files=[]),
            capability_matrix={},
            project_memory=None,
        )

    assert (
        bootstrap_called["count"] == 0
    ), "refine mode must short-circuit the standard bootstrap interview"


def test_run_adaptive_copilot_interview_runs_bootstrap_for_standard():
    """Standard mode (no composition + no refine) MUST still run bootstrap."""
    from fluid_build.cli import forge_copilot_interview as iv_mod

    bootstrap_called = {"count": 0}

    def _spy_bootstrap(*_args, **_kw):
        bootstrap_called["count"] += 1
        # Mark state as ready so the function returns
        return None

    with (
        mock.patch.object(iv_mod, "_ask_bootstrap_questions", _spy_bootstrap),
        mock.patch.object(iv_mod, "is_context_sufficient", lambda _ctx: True),
    ):
        iv_mod.run_adaptive_copilot_interview(
            initial_context={"project_goal": "x"},
            console=None,
            llm_config=mock.MagicMock(),
            discovery_report=mock.MagicMock(sample_files=[]),
            capability_matrix={},
            project_memory=None,
        )

    assert bootstrap_called["count"] == 1
