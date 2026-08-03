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

"""Golden-snapshot tests for the JudgeAgent parser + persistence layer.

For each contract in ``tests/fixtures/judge_eval_set/``, this module:

1. Loads the contract YAML and the ``expected_scores.json`` rubric file.
2. Mocks the LLM call to return a "perfect" judge response that matches
   the expected scores exactly.
3. Runs ``JudgeAgent.judge`` and asserts:
   - all 6 axes present in the parsed result,
   - ``total == sum(axis.score)``,
   - the persisted ``judge.json`` round-trips with no loss.

This tests the PARSER + persistence layer, NOT the LLM behavior.
Borrowed from OpenAI evals' JSONL + ideal-answer pattern
(https://github.com/openai/evals) and DeepEval's LLMTestCase shape
(input/expected_output) — adapted to a per-contract directory layout
because the contract YAML itself is the "input" and the JSON sidecar
the "expected".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest
import yaml

from fluid_build.copilot.agents.judge_agent import JudgeAgent, JudgeResult

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "judge_eval_set"


def _discover_cases() -> List[Tuple[str, Path, Path]]:
    """Return ``[(case_name, contract_path, expected_path)]`` for every case dir."""
    cases: List[Tuple[str, Path, Path]] = []
    for case_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not case_dir.is_dir():
            continue
        contract_path = case_dir / "contract.fluid.yaml"
        expected_path = case_dir / "expected_scores.json"
        if contract_path.is_file() and expected_path.is_file():
            cases.append((case_dir.name, contract_path, expected_path))
    return cases


# Discovered at import — pytest needs the parametrize values up front.
CASES = _discover_cases()


def _stub_llm_config():
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    return LlmConfig(
        provider="openai",
        model="gpt-4.1-mini",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="test-key",
    )


def _build_perfect_response(expected: Dict[str, Any]) -> str:
    """Synthesise a well-formed JSON response matching the expected scores."""
    axes_payload: Dict[str, Any] = {}
    for axis_name, score in expected["axes"].items():
        axes_payload[axis_name] = {
            "score": int(score),
            # The reasoning string is intentionally generic — the golden
            # test exercises the parser, not the prose quality.
            "reasoning": f"{axis_name} scored {score}/5 per the expected rubric.",
            "suggestions": [],
        }
    return json.dumps({"axes": axes_payload})


@pytest.mark.unit
class TestGoldenSnapshotParser:
    """Per-case golden snapshot of parser + persistence."""

    @pytest.mark.parametrize(
        "case_name,contract_path,expected_path", CASES, ids=[c[0] for c in CASES]
    )
    def test_parser_round_trips_expected_scores(
        self,
        case_name: str,
        contract_path: Path,
        expected_path: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        # WHY: golden snapshot tests target the parser + persistence layer.
        # Self-critique runs a second LLM call that's irrelevant to that
        # surface — disable so the mock answers exactly once.
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")

        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        perfect_response = _build_perfect_response(expected)

        run_id = f"golden-{case_name}"
        agent = JudgeAgent(model="judge-model-golden")

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
                return_value=perfect_response,
            ),
        ):
            result = agent.judge(contract, run_id=run_id)

        assert isinstance(result, JudgeResult)
        # All six axes present, in the canonical order.
        assert list(result.axes.keys()) == JudgeAgent.AXES
        # Total is the deterministic sum.
        expected_total = sum(expected["axes"].values())
        assert result.total == expected_total
        assert result.total == sum(a.score for a in result.axes.values())
        # Expected total in the fixture sidecar must match the per-axis sum.
        assert expected["total"] == expected_total, (
            f"Fixture {case_name} sidecar 'total' field is inconsistent "
            f"with the per-axis sum ({expected_total})."
        )
        # Round-trip through to_dict() must preserve every axis byte-perfect.
        as_dict = result.to_dict()
        assert set(as_dict["axes"].keys()) == set(JudgeAgent.AXES)
        assert as_dict["total"] == expected_total
        assert as_dict["model"] == "judge-model-golden"
        assert as_dict["run_id"] == run_id

        # Persistence: judge.json must land under .fluid/agents/<run_id>/.
        target = tmp_path / ".fluid" / "agents" / run_id / "judge.json"
        assert target.is_file(), f"expected judge.json at {target}"
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert on_disk["total"] == expected_total
        # Persisted file content matches the in-memory dataclass round-trip.
        assert on_disk == as_dict


@pytest.mark.unit
class TestFixtureInvariants:
    """Fixture-set hygiene — every case must obey the rubric invariants."""

    @pytest.mark.parametrize(
        "case_name,contract_path,expected_path", CASES, ids=[c[0] for c in CASES]
    )
    def test_expected_scores_well_formed(
        self,
        case_name: str,
        contract_path: Path,
        expected_path: Path,
    ) -> None:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        # Every axis in the canonical six must be present.
        assert set(expected["axes"].keys()) == set(JudgeAgent.AXES), f"{case_name}: axes mismatch"
        # Every score in 0..5.
        for axis_name, score in expected["axes"].items():
            assert (
                isinstance(score, int) and 0 <= score <= 5
            ), f"{case_name} axis={axis_name} score={score}"
        # Total matches per-axis sum (consistency check for fixture upkeep).
        expected_total = sum(expected["axes"].values())
        assert expected["total"] == expected_total
        # Tolerances are reasonable positive values.
        assert expected["tolerance_per_axis"] >= 0
        assert expected["tolerance_total"] >= 0
        # Rationale is non-empty (humans must justify their scores).
        assert isinstance(expected.get("rationale", ""), str)
        assert (
            len(expected["rationale"]) > 40
        ), f"{case_name} rationale is too short to be informative"

    def test_eval_set_covers_quality_spectrum(self) -> None:
        # Corpus floor: every tier represented + a non-trivial total. We
        # deliberately keep the corpus small so the snapshot suite stays
        # fast and reviewers don't drown in fixture noise; the live
        # judge + A/B tests reference specific cases by name so the
        # required set is whatever those tests pick.
        sparse = [c for c in CASES if c[0].startswith("sparse_")]
        medium = [c for c in CASES if c[0].startswith("medium_")]
        rich = [c for c in CASES if c[0].startswith("rich_")]
        assert len(sparse) >= 1, f"need >=1 sparse case, found {len(sparse)}"
        assert len(medium) >= 1, f"need >=1 medium case, found {len(medium)}"
        assert len(rich) >= 1, f"need >=1 rich case, found {len(rich)}"
        total = len(sparse) + len(medium) + len(rich)
        assert 4 <= total <= 12, f"total cases {total} outside [4, 12]"

    def test_total_score_bands_match_tier(self) -> None:
        # Rough quality-spectrum band check: sparse < medium < rich on
        # average. Bands themselves are approximate (LLM nondeterminism)
        # so the assertion is on relative ordering of the means.
        def mean_total(prefix: str) -> float:
            totals = [
                json.loads(p.read_text(encoding="utf-8"))["total"]
                for name, _, p in CASES
                if name.startswith(prefix)
            ]
            return sum(totals) / max(len(totals), 1)

        sparse_mean = mean_total("sparse_")
        medium_mean = mean_total("medium_")
        rich_mean = mean_total("rich_")
        assert sparse_mean < medium_mean < rich_mean, (
            f"quality spectrum broken: sparse_mean={sparse_mean}, "
            f"medium_mean={medium_mean}, rich_mean={rich_mean}"
        )
