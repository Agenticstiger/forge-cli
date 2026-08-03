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

"""A/B harness: baseline vs enriched judge scores.

Borrowed pattern: OpenAI evals' model-graded eval comparison + promptfoo's
A/B prompt-variant runs. The harness picks the sparse contracts (the
ones with the lowest baseline score, so there's maximum room for
enrichment to move the needle), runs the enrichment hook to get
artifacts, and asks the (mocked) judge to score the contract WITH and
WITHOUT those artifacts visible in the prompt.

The mock LLM is the load-bearing piece: it inspects the user_prompt
captured at call time and scores +1 per enrichment slot whose name
appears in the prompt. This proves the enrichment PLUMBING (artifacts
→ ``_format_artifacts_block`` → user prompt → score) actually moves
the number — what the user explicitly asked for as proof-of-outcomes.

We do NOT prove the enrichment makes contracts genuinely better in
the eyes of a real LLM; that's the live-judge test's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
import yaml

from fluid_build.copilot.agents.judge_agent import JudgeAgent
from fluid_build.copilot.enrichment import enrich_contract

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "judge_eval_set"

# Pick the three sparse contracts plus one medium. Sparse contracts have
# the widest room for enrichment to lift the score (they're missing
# clustering hints, freshness, dbt tests), so they're the strongest
# A/B subjects. The medium case verifies enrichment also helps when
# the contract isn't bottom-of-rubric.
SUBJECTS = [
    "sparse_01_minimal_orders",
    "sparse_02_bare_customers",
    "sparse_03_skeleton_events",
    "medium_01_pk_marked_transactions",
]


def _stub_llm_config():
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    return LlmConfig(
        provider="openai",
        model="gpt-4.1-mini",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="test-key",
    )


def _load_contract(case_name: str) -> Dict[str, Any]:
    return yaml.safe_load(
        (FIXTURE_ROOT / case_name / "contract.fluid.yaml").read_text(encoding="utf-8")
    )


# The slots the enrichment hook surfaces. Each slot, when visible in the
# prompt, gives a deterministic +1 bonus on a specific axis. This wiring
# mirrors the JudgeAgent docstring: enrichment is supposed to help
# performance / governance / documentation specifically.
_ENRICHMENT_SLOT_TO_AXIS = {
    "dbt_tests": "correctness",
    "freshness": "governance",
    "physical_layout": "performance",
    "provider": "documentation",
    "refresh_cadence": "completeness",
}


def _build_enrichment_aware_response(user_prompt: str) -> str:
    """Return a JSON judge response that scores +1 per enrichment slot in the prompt.

    Baseline (no enrichment, no slot names): every axis = 2.
    With every slot populated and visible: every axis bumped per
    ``_ENRICHMENT_SLOT_TO_AXIS``.
    """
    import json as _json

    base = 2
    scores: Dict[str, int] = {axis: base for axis in JudgeAgent.AXES}

    # We look for two signals in the user prompt:
    # 1. The enrichment block header.
    # 2. Each slot name appearing in the YAML-rendered artifacts.
    has_enrichment_block = "Deterministic-enrichment outputs" in user_prompt
    if has_enrichment_block:
        for slot_name, axis in _ENRICHMENT_SLOT_TO_AXIS.items():
            # WHY: YAML emits keys followed by ":"; this avoids matching
            # the slot name embedded inside an unrelated word.
            if f"{slot_name}:" in user_prompt:
                scores[axis] = min(5, scores[axis] + 1)

    payload = {
        "axes": {
            axis: {
                "score": s,
                "reasoning": f"deterministic mock for axis {axis} with score {s}",
                "suggestions": [],
            }
            for axis, s in scores.items()
        }
    }
    return _json.dumps(payload)


class _PromptCapturingMock:
    """Records the user_prompt each invocation sees and returns a synthesised score."""

    def __init__(self) -> None:
        self.captured_user_prompts: List[str] = []

    def __call__(self, _provider, _config, _system_prompt, user_prompt, **_kwargs):
        self.captured_user_prompts.append(user_prompt)
        return _build_enrichment_aware_response(user_prompt)


def _run_judge(
    contract: Dict[str, Any],
    build_artifacts: Dict[str, Any] | None,
    *,
    run_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_RUN_ID", raising=False)
    # WHY: JudgeAgent runs a Self-Refine self-critique pass by default
    # (FLUID_JUDGE_SELF_CRITIQUE). The A/B harness measures enrichment
    # plumbing, not self-critique; disable so each judge() invocation
    # produces exactly one LLM call we can assert against.
    monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")
    mock_call = _PromptCapturingMock()
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
            side_effect=mock_call,
        ),
    ):
        result = JudgeAgent(model="ab-test-judge").judge(
            contract, build_artifacts=build_artifacts, run_id=run_id
        )
    return result, mock_call


@pytest.mark.unit
class TestEnrichmentLiftsScore:
    """For every subject contract: enriched_total > baseline_total."""

    @pytest.mark.parametrize("case_name", SUBJECTS)
    def test_enrichment_strictly_increases_total_score(
        self,
        case_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        contract = _load_contract(case_name)

        # Step 1: baseline run — no enrichment artifacts.
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        baseline_result, baseline_mock = _run_judge(
            contract,
            None,
            run_id=f"ab-{case_name}-baseline",
            tmp_path=baseline_dir,
            monkeypatch=monkeypatch,
        )
        baseline_total = baseline_result.total
        assert len(baseline_mock.captured_user_prompts) == 1
        # The baseline prompt must NOT have the enrichment block.
        assert "Deterministic-enrichment outputs" not in (baseline_mock.captured_user_prompts[0])

        # Step 2: enrichment run — produce artifacts deterministically.
        artifacts = enrich_contract(
            contract,
            run_id=f"ab-{case_name}-enriched-pre",
            workspace_root=tmp_path / "enrichment_root",
        )
        assert artifacts is not None, "enrichment must produce artifacts (kill-switch off)"
        # At least one of the enrichment-tracked slots must be populated
        # for the bump to be possible. If the enrichment pass yields an
        # empty bag for this contract, the A/B test is meaningless.
        populated_slots = [slot for slot in _ENRICHMENT_SLOT_TO_AXIS if artifacts.get(slot)]
        assert populated_slots, (
            f"enrichment produced no populated slots for {case_name}; "
            f"contract is not a useful A/B subject"
        )

        enriched_dir = tmp_path / "enriched"
        enriched_dir.mkdir()
        enriched_result, enriched_mock = _run_judge(
            contract,
            artifacts,
            run_id=f"ab-{case_name}-enriched",
            tmp_path=enriched_dir,
            monkeypatch=monkeypatch,
        )
        enriched_total = enriched_result.total
        # The enriched prompt MUST carry the enrichment block.
        assert "Deterministic-enrichment outputs" in (enriched_mock.captured_user_prompts[0])

        # The load-bearing assertion: enrichment strictly lifts the score.
        assert enriched_total > baseline_total, (
            f"enrichment failed to lift score for {case_name}: "
            f"baseline={baseline_total} enriched={enriched_total} "
            f"populated_slots={populated_slots}"
        )

    @pytest.mark.parametrize("case_name", SUBJECTS)
    def test_per_axis_bumps_match_slot_population(
        self,
        case_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stronger assertion: each enrichment slot that ends up visible
        # in the user prompt bumps EXACTLY ONE axis by 1 (capped at 5).
        contract = _load_contract(case_name)

        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        baseline_result, _ = _run_judge(
            contract,
            None,
            run_id=f"perax-{case_name}-baseline",
            tmp_path=baseline_dir,
            monkeypatch=monkeypatch,
        )

        artifacts = enrich_contract(
            contract,
            run_id=f"perax-{case_name}-enriched-pre",
            workspace_root=tmp_path / "enrichment_root",
        )
        assert artifacts is not None

        enriched_dir = tmp_path / "enriched"
        enriched_dir.mkdir()
        enriched_result, enriched_mock = _run_judge(
            contract,
            artifacts,
            run_id=f"perax-{case_name}-enriched",
            tmp_path=enriched_dir,
            monkeypatch=monkeypatch,
        )

        user_prompt = enriched_mock.captured_user_prompts[0]
        expected_bumps: Dict[str, int] = {}
        for slot_name, axis in _ENRICHMENT_SLOT_TO_AXIS.items():
            if f"{slot_name}:" in user_prompt:
                expected_bumps[axis] = expected_bumps.get(axis, 0) + 1

        for axis_name in JudgeAgent.AXES:
            baseline_score = baseline_result.axes[axis_name].score
            enriched_score = enriched_result.axes[axis_name].score
            expected_delta = expected_bumps.get(axis_name, 0)
            # The mock caps at 5, so the realized bump is min(expected, 5 - baseline).
            realized_delta = enriched_score - baseline_score
            assert realized_delta == min(expected_delta, 5 - baseline_score), (
                f"{case_name} axis={axis_name}: "
                f"expected_delta={expected_delta} realized={realized_delta} "
                f"(baseline={baseline_score} enriched={enriched_score})"
            )
