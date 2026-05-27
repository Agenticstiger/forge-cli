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

"""Pin the Gap 6 Self-Refine-style critique pass over :class:`JudgeAgent`.

The critique is **default-ON** in v1.6+; kill switch via
``FLUID_JUDGE_SELF_CRITIQUE=0``. These tests pin every branch of the
spec:

* default-on path → second LLM call fires, critique adopted under the
  ``|Δ| > 1`` merge rule, ``critique_applied = True``, audit-trail
  annotation appended to each axis's reasoning, ``critique_summary``
  block added to persisted JSON.
* kill-switch path → exactly one LLM call, ``critique_applied = False``.
* cost-aware skip → ``FLUID_COST_LIMIT_USD`` set with running total
  near the cap → critique skipped, one LLM call only.
* fail-open paths → malformed critique JSON / missing axes key →
  initial result preserved, ``critique_applied = False``.
* over-tweak guard → critique scores differing by 0 or 1 → initial
  preserved.
* adoption → critique scores differing by ≥ 2 → critique adopted.

Prior art the implementation borrowed from (see module docstring of
``judge_agent.py``): Madaan et al. Self-Refine (NeurIPS 2023),
G-Eval CoT, DSPy Assertions / Suggest, Patronus / Confident-AI
LLM-judge best practices.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from fluid_build.copilot.agents.judge_agent import (
    AxisScore,
    JudgeAgent,
    JudgeResult,
)
from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

# ---------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------


_FAKE_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.3",
    "id": "orders_v1",
    "metadata": {
        "owner": "data-platform@example.com",
        "domain": "commerce",
        "layer": "Silver",
        "productType": "ADP",
    },
    "exposes": [
        {
            "name": "orders",
            "schema": [
                {"name": "order_id", "type": "STRING"},
                {"name": "amount_cents", "type": "INT64"},
            ],
        }
    ],
}


def _initial_response() -> str:
    """Initial pass returns a mixed-score scorecard."""
    return json.dumps(
        {
            "axes": {
                "correctness": {
                    "score": 4,
                    "reasoning": "Types match the sample; one numeric is STRING.",
                    "suggestions": ["Retype amount_cents as INT64."],
                },
                "completeness": {
                    "score": 3,
                    "reasoning": "Owner and SLA set; 4 column descriptions missing.",
                    "suggestions": ["Add column descriptions."],
                },
                "security": {
                    "score": 5,
                    "reasoning": "PII tagged; email masked.",
                    "suggestions": [],
                },
                "governance": {
                    "score": 4,
                    "reasoning": "Owner + retention set.",
                    "suggestions": [],
                },
                "performance": {
                    "score": 2,
                    "reasoning": "No clustering on the largest table.",
                    "suggestions": ["Cluster on event_date."],
                },
                "documentation": {
                    "score": 3,
                    "reasoning": "README present but column descriptions sparse.",
                    "suggestions": ["Expand the README."],
                },
            }
        }
    )


def _critique_response_with_big_delta() -> str:
    """Critique adopts a 2+ delta on 'performance' (2 → 5) and 1 delta
    on 'completeness' (3 → 4 — within threshold, must be ignored).
    Other axes unchanged."""
    return json.dumps(
        {
            "axes": {
                "correctness": {
                    "score": 4,
                    "reasoning": "stands as-is on review",
                    "suggestions": [],
                },
                "completeness": {
                    "score": 4,  # +1 — within threshold, initial wins
                    "reasoning": "On re-read, 4/12 missing isn't fatal.",
                    "suggestions": [],
                },
                "security": {
                    "score": 5,
                    "reasoning": "stands as-is on review",
                    "suggestions": [],
                },
                "governance": {
                    "score": 4,
                    "reasoning": "stands as-is on review",
                    "suggestions": [],
                },
                "performance": {
                    "score": 5,  # +3 — adopt
                    "reasoning": (
                        "Missed the build_artifacts clustering hint on "
                        "re-read; clustering IS specified at the enrichment "
                        "layer."
                    ),
                    "suggestions": [],
                },
                "documentation": {
                    "score": 3,
                    "reasoning": "stands as-is on review",
                    "suggestions": [],
                },
            }
        }
    )


def _critique_response_identical_to_initial() -> str:
    """The critique stands by every initial score — used to assert
    that an unchanged second pass still flips ``critique_applied`` to
    True and writes the annotation, but doesn't modify any axis."""
    return _initial_response()


def _critique_response_malformed() -> str:
    """Critique blew up — return prose that won't parse as JSON.
    Spec: the run fails open, keeps the initial result intact."""
    return "I'm sorry, I cannot evaluate this further."


def _stub_llm_config():
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    return LlmConfig(
        provider="openai",
        model="gpt-4.1-mini",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="test-key",
    )


class _CallSequence:
    """Two-pass call_llm stub.

    First invocation returns ``initial``; second returns ``critique``.
    Subsequent calls raise (the spec says exactly two calls per run
    when critique is enabled). Used as ``side_effect`` on the
    ``call_llm`` mock so we can pin the call shape AND assert the
    order/count.
    """

    def __init__(self, initial: str, critique: str):
        self.responses: List[str] = [initial, critique]
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, provider, config, system_prompt, user_prompt, **kwargs):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_prompt,
                "kwargs": dict(kwargs),
            }
        )
        if not self.responses:
            raise AssertionError(
                f"call_llm invoked more times than expected: got {len(self.calls)}"
            )
        return self.responses.pop(0)


# ---------------------------------------------------------------------
# Default-ON path: critique runs, axes reflect threshold merge
# ---------------------------------------------------------------------


class TestCritiqueEnabledByDefault:
    def test_critique_applied_flag_flips_true_and_big_delta_axes_adopted(
        self, tmp_path, monkeypatch
    ):
        # cwd-isolate for persistence-side-effect cleanliness.
        monkeypatch.chdir(tmp_path)
        # Pin: default-ON, no kill switch / cost cap in this run.
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="critique-run-001")

        # Exactly two LLM calls fired — initial + critique.
        assert len(seq.calls) == 2

        # Flag flipped.
        assert isinstance(result, JudgeResult)
        assert result.critique_applied is True

        # |delta| > 1: 'performance' adopted (2 → 5). |delta| <= 1:
        # 'completeness' (3, critique 4) preserved at 3.
        assert result.axes["performance"].score == 5
        assert result.axes["completeness"].score == 3
        # Untouched axes: score preserved exactly.
        assert result.axes["correctness"].score == 4
        assert result.axes["security"].score == 5
        assert result.axes["governance"].score == 4
        assert result.axes["documentation"].score == 3

        # Audit-trail annotation appears on every axis's reasoning
        # (the critique commented on each — either adopted or stood by).
        for axis_name, axis in result.axes.items():
            assert (
                "_critique:" in axis.reasoning
            ), f"axis {axis_name!r} reasoning missing _critique: annotation"

        # Total is recomputed AFTER merge: 4 + 3 + 5 + 4 + 5 + 3 = 24.
        # Initial total was 4 + 3 + 5 + 4 + 2 + 3 = 21.
        assert result.total == 24

        # critique_summary block populated correctly.
        assert result.critique_summary is not None
        assert result.critique_summary["before_total"] == 21
        assert result.critique_summary["after_total"] == 24
        assert result.critique_summary["axes_changed"] == ["performance"]

    def test_second_call_uses_lower_temperature(self, tmp_path, monkeypatch):
        """Industry consensus is 0.0-0.2 for the corrector pass; the
        implementation pins ``extra_payload={"temperature": 0.1}``."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="critique-run-temp")

        # Initial call: no extra_payload (default temperature path).
        assert seq.calls[0]["kwargs"] == {}
        # Critique call: temperature override via extra_payload.
        critique_kwargs = seq.calls[1]["kwargs"]
        assert critique_kwargs.get("extra_payload") == {"temperature": 0.1}


# ---------------------------------------------------------------------
# Kill switch — FLUID_JUDGE_SELF_CRITIQUE=0 skips the second pass
# ---------------------------------------------------------------------


class TestKillSwitch:
    def test_critique_skipped_when_env_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="kill-switch-001")

        # Exactly ONE LLM call — the kill switch prevented the second.
        assert len(seq.calls) == 1
        # Flag stays False.
        assert result.critique_applied is False
        # Scores are the initial scorecard's, untouched.
        assert result.axes["performance"].score == 2
        assert result.total == 21
        # No _critique annotation when the critique didn't run.
        for axis in result.axes.values():
            assert "_critique:" not in axis.reasoning
        # critique_summary stays None.
        assert result.critique_summary is None

    @pytest.mark.parametrize("falsey", ["0", "false", "False", "no", "off"])
    def test_critique_kill_switch_accepts_common_falsey_values(self, falsey, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", falsey)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id=f"kill-{falsey}")

        assert len(seq.calls) == 1
        assert result.critique_applied is False


# ---------------------------------------------------------------------
# Cost-aware skip — FLUID_COST_LIMIT_USD near the cap
# ---------------------------------------------------------------------


class TestCostAwareSkip:
    def test_critique_skipped_when_projected_spend_exceeds_limit(self, tmp_path, monkeypatch):
        """Mirror of ``StageCoordinator._cooperation_would_exceed_budget``:
        when the running total + average-per-call would exceed the cap,
        the critique skips."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        # Set the cap to $0.10 and pre-seed the tracker with $0.09
        # over 9 calls (avg = $0.01). Projected = $0.09 + $0.01 = $0.10
        # which is NOT > $0.10. So we need a slightly tighter setup —
        # use a cap that the (running + avg) clearly exceeds. $0.05
        # cap with $0.09 running already exceeds the cap; the critique
        # MUST skip.
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.05")
        reset_run_tracker()
        # Pre-seed via record_call with a litellm-style usd_override so
        # ``breakdown().total_usd`` is non-None.
        for _ in range(9):
            get_run_tracker().record_call(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=0,
                output_tokens=0,
                usd_override=0.01,
            )

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="cost-skip-001")

        # Critique skipped — only one LLM call fired.
        assert len(seq.calls) == 1
        assert result.critique_applied is False
        # Scores match the initial pass.
        assert result.axes["performance"].score == 2

    def test_critique_runs_when_no_limit_configured(self, tmp_path, monkeypatch):
        """No ``FLUID_COST_LIMIT_USD`` → cost-aware check returns True
        (within budget) → critique runs."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="no-cap-001")

        assert len(seq.calls) == 2
        assert result.critique_applied is True


# ---------------------------------------------------------------------
# Fail-open — critique returns malformed JSON
# ---------------------------------------------------------------------


class TestCritiqueFailOpen:
    def test_malformed_critique_preserves_initial(self, tmp_path, monkeypatch, caplog):
        """Spec: critique returns garbage → initial result returned,
        ``critique_applied=False``, logged at DEBUG."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_malformed(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            with caplog.at_level("DEBUG", logger="fluid.copilot.judge"):
                result = agent.judge(_FAKE_CONTRACT, run_id="malformed-001")

        # Two calls fired (the critique was attempted) — but the
        # malformed response failed to merge, so the initial result
        # is preserved.
        assert len(seq.calls) == 2
        assert result.critique_applied is False
        # Initial scores intact.
        assert result.axes["performance"].score == 2
        assert result.total == 21
        # No _critique annotation — failure path doesn't write one.
        for axis in result.axes.values():
            assert "_critique:" not in axis.reasoning
        # DEBUG log fired (parse_failed → judge_critique_failed).
        # We don't assert the exact message because logger formatting
        # can vary; we assert that the agent's debug-channel has any
        # message tagged with critique failure.
        debug_messages = " ".join(
            r.getMessage()
            for r in caplog.records
            if r.name == "fluid.copilot.judge" and r.levelname == "DEBUG"
        )
        assert "critique" in debug_messages.lower()

    def test_critique_missing_axes_key_fails_open(self, tmp_path, monkeypatch):
        """A critique response that's valid JSON but missing 'axes'
        also fails open — initial result preserved."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            json.dumps({"score": 30, "reasoning": "looks fine"}),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="missing-axes-001")

        assert len(seq.calls) == 2
        assert result.critique_applied is False
        # Initial scores intact.
        assert result.total == 21


# ---------------------------------------------------------------------
# Merge rule — |delta| <= 1 keeps initial; |delta| >= 2 adopts critique
# ---------------------------------------------------------------------


class TestMergeRule:
    def test_delta_zero_keeps_initial_with_stands_as_is_annotation(self, tmp_path, monkeypatch):
        """Critique reaffirms every initial score → all initial scores
        preserved; reasoning annotated with 'stands as-is on review'."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_identical_to_initial(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="delta-zero-001")

        # critique_applied flips even when no axis changed.
        assert result.critique_applied is True
        # Every initial score preserved exactly.
        assert result.axes["correctness"].score == 4
        assert result.axes["completeness"].score == 3
        assert result.axes["security"].score == 5
        assert result.axes["governance"].score == 4
        assert result.axes["performance"].score == 2
        assert result.axes["documentation"].score == 3
        assert result.total == 21
        # Audit-trail annotation present on every axis.
        for axis_name, axis in result.axes.items():
            assert "_critique:" in axis.reasoning, f"axis {axis_name!r} missing _critique: trail"
            assert (
                "stands as-is" in axis.reasoning
            ), f"axis {axis_name!r} missing 'stands as-is' annotation"
        # No axes changed in the summary.
        assert result.critique_summary == {
            "axes_changed": [],
            "before_total": 21,
            "after_total": 21,
        }

    def test_delta_one_keeps_initial_with_threshold_annotation(self, tmp_path, monkeypatch):
        """Every axis nudged by exactly 1 → all initial scores
        preserved (the over-tweak guard); annotation cites the
        within-threshold rejection."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        # Build a critique that nudges every axis by exactly +1
        # (capping at 5). Threshold = 1 → none adopted.
        initial = json.loads(_initial_response())
        nudged = {"axes": {}}
        for axis_name, entry in initial["axes"].items():
            new_score = min(5, entry["score"] + 1)
            nudged["axes"][axis_name] = {
                "score": new_score,
                "reasoning": "tiny adjustment",
                "suggestions": [],
            }
        seq = _CallSequence(_initial_response(), json.dumps(nudged))

        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="delta-one-001")

        # All scores preserved (no axis exceeded the 1-pt threshold).
        assert result.axes["correctness"].score == 4
        assert result.axes["completeness"].score == 3
        assert result.axes["security"].score == 5
        assert result.axes["governance"].score == 4
        assert result.axes["performance"].score == 2
        assert result.axes["documentation"].score == 3
        assert result.total == 21
        assert result.critique_summary["axes_changed"] == []
        # Threshold annotation appears for axes where critique nudged.
        # 'security' is 5 → critique tried 6 capped to 5 (delta 0,
        # 'stands as-is'); the others have delta 1.
        # Check at least one axis has the within-threshold note.
        has_threshold_note = any(
            "within" in axis.reasoning and "threshold" in axis.reasoning
            for axis in result.axes.values()
        )
        assert has_threshold_note, (
            "expected at least one axis annotated with 'within ... threshold' "
            "marker for the 1-pt nudge"
        )

    def test_delta_two_or_more_adopts_critique_with_revision_annotation(
        self, tmp_path, monkeypatch
    ):
        """Two-pt+ shift → adopt critique. Audit-trail annotation cites
        the revision direction."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="delta-two-001")

        # performance: 2 → 5 (delta 3) → adopted.
        assert result.axes["performance"].score == 5
        # Annotation cites the revision.
        perf_reasoning = result.axes["performance"].reasoning
        assert "_critique:" in perf_reasoning
        assert "revised from 2" in perf_reasoning
        assert "5" in perf_reasoning


# ---------------------------------------------------------------------
# Persistence — judge.json includes critique block
# ---------------------------------------------------------------------


class TestPersistedCritiqueSummary:
    def test_judge_json_carries_critique_summary_when_applied(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        reset_run_tracker()

        seq = _CallSequence(
            _initial_response(),
            _critique_response_with_big_delta(),
        )
        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=seq,
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="persist-critique-001")

        target = tmp_path / ".fluid" / "agents" / "persist-critique-001" / "judge.json"
        assert target.is_file(), f"expected judge.json at {target}"

        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert on_disk["critique_applied"] is True
        assert "critique_summary" in on_disk
        assert on_disk["critique_summary"]["before_total"] == 21
        assert on_disk["critique_summary"]["after_total"] == 24
        assert on_disk["critique_summary"]["axes_changed"] == ["performance"]
        # Per-axis annotated reasoning landed in the JSON too.
        assert "_critique:" in on_disk["axes"]["performance"]["reasoning"]
        # The adopted critique's reasoning is present.
        assert "Missed the build_artifacts" in on_disk["axes"]["performance"]["reasoning"]

    def test_judge_json_omits_critique_summary_when_disabled(self, tmp_path, monkeypatch):
        """Spec: legacy persisted-file shape unchanged for non-critique
        runs — the ``critique_summary`` key is absent when critique
        didn't run."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        reset_run_tracker()

        agent = JudgeAgent(model="judge-model-x")
        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=_initial_response(),
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="persist-no-critique-001")

        target = tmp_path / ".fluid" / "agents" / "persist-no-critique-001" / "judge.json"
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        # critique_applied is False (still present — it's an additive
        # field on the dataclass).
        assert on_disk["critique_applied"] is False
        # ...but critique_summary is omitted entirely to keep legacy
        # consumers byte-identical.
        assert "critique_summary" not in on_disk


# ---------------------------------------------------------------------
# Helper-level pin — _self_critique_enabled / _critique_within_budget
# ---------------------------------------------------------------------


class TestHelpers:
    def test_self_critique_enabled_defaults_on(self, monkeypatch):
        from fluid_build.copilot.agents.judge_agent import _self_critique_enabled

        monkeypatch.delenv("FLUID_JUDGE_SELF_CRITIQUE", raising=False)
        assert _self_critique_enabled() is True

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("0", False),
            ("false", False),
            ("False", False),
            ("no", False),
            ("off", False),
            ("1", True),
            ("true", True),
            ("on", True),
            # Unparseable values fall through to ON.
            ("garbage", True),
        ],
    )
    def test_self_critique_enabled_env_var_parsing(self, monkeypatch, value, expected):
        from fluid_build.copilot.agents.judge_agent import _self_critique_enabled

        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", value)
        assert _self_critique_enabled() is expected

    def test_critique_within_budget_true_when_no_limit(self, monkeypatch):
        from fluid_build.copilot.agents.judge_agent import _critique_within_budget

        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        reset_run_tracker()
        assert _critique_within_budget() is True

    def test_critique_within_budget_false_when_projected_exceeds_limit(self, monkeypatch):
        from fluid_build.copilot.agents.judge_agent import _critique_within_budget

        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.05")
        reset_run_tracker()
        # Pre-seed total spend > limit so projection definitely exceeds.
        for _ in range(9):
            get_run_tracker().record_call(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=0,
                output_tokens=0,
                usd_override=0.01,
            )
        assert _critique_within_budget() is False


# ---------------------------------------------------------------------
# Public-API regression — JudgeResult.to_dict() shape
# ---------------------------------------------------------------------


class TestJudgeResultToDict:
    def test_to_dict_includes_critique_applied_flag(self):
        """``critique_applied`` lands in every serialised dict, default
        False so legacy CI consumers don't break."""
        result = JudgeResult(
            axes={
                axis: AxisScore(score=3, reasoning="", suggestions=[]) for axis in JudgeAgent.AXES
            },
            total=18,
            model="judge-model-x",
            run_id="rid",
        )
        d = result.to_dict()
        assert d["critique_applied"] is False
        assert "critique_summary" not in d  # omitted when None

    def test_to_dict_includes_critique_summary_when_set(self):
        result = JudgeResult(
            axes={
                axis: AxisScore(score=3, reasoning="", suggestions=[]) for axis in JudgeAgent.AXES
            },
            total=18,
            model="judge-model-x",
            run_id="rid",
            critique_applied=True,
            critique_summary={
                "axes_changed": ["performance"],
                "before_total": 16,
                "after_total": 18,
            },
        )
        d = result.to_dict()
        assert d["critique_applied"] is True
        assert d["critique_summary"]["axes_changed"] == ["performance"]
        assert d["critique_summary"]["before_total"] == 16
        assert d["critique_summary"]["after_total"] == 18
