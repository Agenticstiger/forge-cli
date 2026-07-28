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

"""Tests for the JudgeAgent integration into ``forge_copilot_runtime``.

Pins the kill-switch (``FLUID_COPILOT_JUDGE``) and the flat-dict
shape returned by ``_judge_contract`` so the provenance block always
sees the same keys regardless of judge outcome.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from fluid_build.cli.forge_copilot_runtime import (
    _judge_contract,
    _judge_enabled,
)
from fluid_build.copilot.agents.judge_agent import (
    AxisScore,
    JudgeResult,
)

SAMPLE_CONTRACT = {
    "fluidVersion": "0.7.3",
    "kind": "DataProduct",
    "id": "x.y.sample",
    "name": "sample",
    "domain": "x",
    "metadata": {"layer": "Bronze", "productType": "SDP"},
    "exposes": [],
}


def _result_with(total: int = 24, model: str = "claude-opus-4-7") -> JudgeResult:
    return JudgeResult(
        axes={
            "correctness": AxisScore(score=4, reasoning=""),
            "completeness": AxisScore(score=4, reasoning=""),
            "security": AxisScore(score=3, reasoning=""),
            "governance": AxisScore(score=4, reasoning=""),
            "performance": AxisScore(score=4, reasoning=""),
            "documentation": AxisScore(score=5, reasoning=""),
        },
        total=total,
        model=model,
    )


def test_judge_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FLUID_COPILOT_JUDGE", raising=False)
    assert _judge_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_judge_disabled_via_env(monkeypatch, value):
    monkeypatch.setenv("FLUID_COPILOT_JUDGE", value)
    assert _judge_enabled() is False


def test_judge_contract_returns_flat_dict(monkeypatch):
    monkeypatch.delenv("FLUID_COPILOT_JUDGE", raising=False)
    with patch(
        "fluid_build.copilot.agents.judge_agent.JudgeAgent.judge",
        return_value=_result_with(total=24),
    ):
        out = _judge_contract(SAMPLE_CONTRACT, logger=logging.getLogger("test"))
    assert out is not None
    assert out["score"] == 24
    assert out["model"] == "claude-opus-4-7"
    assert set(out["axes"].keys()) == {
        "correctness",
        "completeness",
        "security",
        "governance",
        "performance",
        "documentation",
    }
    assert all(isinstance(v, int) for v in out["axes"].values())


def test_judge_contract_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("FLUID_COPILOT_JUDGE", "0")
    with patch(
        "fluid_build.copilot.agents.judge_agent.JudgeAgent.judge",
        return_value=_result_with(),
    ) as mocked:
        out = _judge_contract(SAMPLE_CONTRACT)
    assert out is None
    mocked.assert_not_called()


def test_judge_contract_fail_open_on_exception(monkeypatch):
    """A judge crash must NOT block a valid contract from being returned."""
    monkeypatch.delenv("FLUID_COPILOT_JUDGE", raising=False)
    with patch(
        "fluid_build.copilot.agents.judge_agent.JudgeAgent.judge",
        side_effect=RuntimeError("LLM upstream 500"),
    ):
        out = _judge_contract(SAMPLE_CONTRACT, logger=logging.getLogger("test"))
    assert out is None  # fail-open
