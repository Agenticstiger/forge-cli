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

"""Tests for ``supported_data_product_types`` in agent specs (Phase 1.3).

Pin:

1. **Built-in agent specs all parse with the new field** — none of the
   four canonical agents (finance / healthcare / retail / telco)
   regress on the existing schema.
2. **`get_supported_data_product_types` returns canonical codes** for
   each built-in.
3. **Empty agent name returns all three** types (no filter).
4. **Unknown agent name fails open with all types** (never blocks the
   interview).
5. **Unknown product-type code in spec raises** (loud failure on typo).
6. **Aliases (Bronze / Silver / Gold) resolve to canonical codes**
   in the spec parser.
7. **The ``InterviewSignals`` integration filters the inference to the
   allowed set** — workspace lock for a forbidden type falls back to
   the first allowed type.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fluid_build.cli._world_class_interview import (
    InterviewSignals,
    _question_data_product_type,
)
from fluid_build.cli.forge_agent_specs import (
    AgentSpecError,
    load_agent_spec_from_path,
    load_builtin_agent_spec,
)
from fluid_build.cli.forge_agents import get_supported_data_product_types

# ---------------------------------------------------------------------------
# Built-in specs parse cleanly + carry the new field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["finance", "healthcare", "retail", "telco"])
def test_builtin_agent_spec_carries_supported_data_product_types(agent):
    spec = load_builtin_agent_spec(agent)
    # Every built-in declares all three; field is non-empty.
    assert spec.supported_data_product_types == ["SDP", "ADP", "CDP"]


# ---------------------------------------------------------------------------
# get_supported_data_product_types — public helper
# ---------------------------------------------------------------------------


def test_helper_returns_all_types_for_empty_name():
    out = get_supported_data_product_types("")
    assert sorted(out) == ["ADP", "CDP", "SDP"]


def test_helper_returns_canonical_for_each_builtin():
    for agent in ("finance", "healthcare", "retail", "telco"):
        out = get_supported_data_product_types(agent)
        # Each built-in supports every canonical type today; if a future
        # spec narrows this, update the spec AND the test together so
        # the contract is explicit.
        assert sorted(out) == [
            "ADP",
            "CDP",
            "SDP",
        ], f"{agent} regressed away from full type coverage"


def test_helper_fails_open_for_unknown_agent():
    """A typo'd agent name must never block the interview."""
    out = get_supported_data_product_types("totally-not-a-real-agent-zzzzz")
    assert sorted(out) == ["ADP", "CDP", "SDP"]


# ---------------------------------------------------------------------------
# Spec parser validation
# ---------------------------------------------------------------------------


_MINIMAL_SPEC = """
name: test-agent
domain: test
description: Minimal spec for unit tests.
questions:
  - key: q1
    question: anything?
    type: text
    required: false
resolver_defaults: {}
suggestion_defaults:
  recommended_template: starter
  recommended_provider: local
""".lstrip()


def _write_spec(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "test-agent.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_spec_parser_resolves_aliases_to_canonical_codes(tmp_path):
    body = _MINIMAL_SPEC + "supported_data_product_types: [Bronze, Silver, Gold]\n"
    spec = load_agent_spec_from_path(_write_spec(tmp_path, body))
    # Aliases resolve to canonical codes — the test agent doesn't see
    # "Bronze" leak through.
    assert spec.supported_data_product_types == ["SDP", "ADP", "CDP"]


def test_spec_parser_dedupes_codes(tmp_path):
    body = _MINIMAL_SPEC + "supported_data_product_types: [SDP, Bronze, SDP]\n"
    spec = load_agent_spec_from_path(_write_spec(tmp_path, body))
    # Three references to the same canonical code → one entry.
    assert spec.supported_data_product_types == ["SDP"]


def test_spec_parser_rejects_unknown_type(tmp_path):
    body = _MINIMAL_SPEC + "supported_data_product_types: [SDP, Platinum]\n"
    with pytest.raises(AgentSpecError) as exc:
        load_agent_spec_from_path(_write_spec(tmp_path, body))
    assert "Platinum" in str(exc.value)


def test_spec_parser_rejects_non_list(tmp_path):
    body = _MINIMAL_SPEC + "supported_data_product_types: SDP\n"
    with pytest.raises(AgentSpecError):
        load_agent_spec_from_path(_write_spec(tmp_path, body))


def test_spec_parser_treats_missing_field_as_no_filter(tmp_path):
    spec = load_agent_spec_from_path(_write_spec(tmp_path, _MINIMAL_SPEC))
    # No field → empty list → caller interprets as "no filter".
    assert spec.supported_data_product_types == []
    # Helper-level: no filter means all three.
    assert sorted(get_supported_data_product_types_via_path(spec)) == [
        "ADP",
        "CDP",
        "SDP",
    ]


def get_supported_data_product_types_via_path(spec):
    """Mimic the helper resolution path for the file-loaded spec."""
    return spec.supported_data_product_types or ["SDP", "ADP", "CDP"]


# ---------------------------------------------------------------------------
# Interview integration — inference honours the allowlist
# ---------------------------------------------------------------------------


def test_inference_falls_back_to_first_allowed_when_lock_is_forbidden():
    """An agent that only allows ADP should not return CDP from inference."""
    sig = InterviewSignals(
        workspace_lock="CDP",  # workspace says CDP
        allowed_data_product_types=["ADP"],  # but the agent only allows ADP
    )
    out = _question_data_product_type({}, sig)
    assert out == "ADP"


def test_inference_returns_workspace_lock_when_allowed():
    sig = InterviewSignals(
        workspace_lock="ADP",
        allowed_data_product_types=["SDP", "ADP", "CDP"],
    )
    assert _question_data_product_type({}, sig) == "ADP"


def test_inference_no_filter_returns_lock_directly():
    """Empty allowlist = no filter; lock passes through."""
    sig = InterviewSignals(
        workspace_lock="CDP",
        allowed_data_product_types=[],
    )
    assert _question_data_product_type({}, sig) == "CDP"


def test_inference_returns_none_when_no_signal_and_no_filter():
    """No lock + no scan + no filter → ask the user."""
    sig = InterviewSignals()
    assert _question_data_product_type({}, sig) is None


def test_inference_existing_products_falls_back_under_allowlist():
    """existing_products>=1 normally infers ADP; if ADP forbidden,
    fall back to first allowed (e.g. CDP)."""
    sig = InterviewSignals(
        existing_products=2,
        allowed_data_product_types=["CDP"],
    )
    assert _question_data_product_type({}, sig) == "CDP"
