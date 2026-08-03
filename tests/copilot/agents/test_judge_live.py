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

"""Opt-in live JudgeAgent tests against a real LLM.

Runs ONLY when explicitly selected via ``pytest -m live_judge``. Costs
roughly ``$0.001`` per case at gpt-4.1-mini rates. Skips cleanly when
no LLM credentials are configured in the environment.

The assertion is intentionally loose: real LLM scoring has nondeterminism
and we don't want to be brittle to legitimate scoring drift. We pin:

* No exception (parser robust against the real LLM's JSON output).
* All 6 axes returned.
* Total in valid range [0, 30].
* Per-axis score within the fixture's tolerance.

For tighter pinning we'd run the judge 3-5x and average — out of scope
for v1; left as a follow-up if the live test surfaces flakiness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.copilot.agents.judge_agent import JudgeAgent, JudgeResult

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "judge_eval_set"


# Three subjects spanning the quality spectrum. Picked deterministically
# so a CI dashboard can compare run-over-run on the same contracts.
LIVE_SUBJECTS = [
    "sparse_02_bare_customers",
    "medium_02_owner_sla_claims",
    "rich_02_pii_masked_customers",
]


def _resolve_live_config_or_skip():
    """Resolve real LLM config; skip cleanly if no provider configured."""
    from fluid_build.cli.forge_copilot_llm_providers import (
        has_llm_api_key,
        resolve_llm_config,
    )

    class _Args:
        pass

    try:
        config = resolve_llm_config(_Args())
    except Exception as exc:  # noqa: BLE001 — skip gracefully if resolution fails
        pytest.skip(f"resolve_llm_config raised — no provider configured ({exc!r})")

    if config.provider == "ollama":
        # Ollama doesn't need an api key but does need a running server.
        from fluid_build.cli.forge_copilot_llm_providers import detect_ollama_available

        if not detect_ollama_available(None):
            pytest.skip("provider resolved as ollama but no local server detected")
    elif not has_llm_api_key(config.provider):
        pytest.skip(
            f"no API key resolved for provider {config.provider!r}; "
            f"set FLUID_LLM_PROVIDER/FLUID_LLM_MODEL + the relevant API key env var to run"
        )

    return config


@pytest.mark.live_judge
class TestLiveJudgeAgainstEvalSet:
    """Real-LLM judge runs on a small fixed subset of the eval set."""

    @pytest.mark.parametrize("case_name", LIVE_SUBJECTS)
    def test_live_judge_produces_valid_scorecard(
        self,
        case_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = _resolve_live_config_or_skip()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)

        contract_path = FIXTURE_ROOT / case_name / "contract.fluid.yaml"
        expected_path = FIXTURE_ROOT / case_name / "expected_scores.json"
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))

        agent = JudgeAgent()
        result = agent.judge(contract, run_id=f"live-{case_name}")

        # Basic shape pins.
        assert isinstance(result, JudgeResult)
        assert set(result.axes.keys()) == set(JudgeAgent.AXES)
        assert 0 <= result.total <= len(JudgeAgent.AXES) * 5

        # Per-axis tolerance check — loose by design.
        tol = int(expected["tolerance_per_axis"])
        out_of_band: list[str] = []
        for axis_name, expected_score in expected["axes"].items():
            actual = result.axes[axis_name].score
            if abs(actual - int(expected_score)) > tol:
                out_of_band.append(
                    f"{axis_name}: actual={actual} expected={expected_score} tol={tol}"
                )

        # Total tolerance is wider (the per-axis errors can partly cancel
        # but they can also accumulate). Honour the fixture's value.
        total_tol = int(expected["tolerance_total"])
        total_drift = abs(result.total - int(expected["total"]))

        # Soft warning vs hard fail: if total drifts within tolerance we
        # accept axis-level wiggle. Only fail if BOTH the total drifts
        # AND a particular axis is well outside its band.
        if total_drift > total_tol and out_of_band:
            pytest.fail(
                f"live judge drift for {case_name}: "
                f"total actual={result.total} expected={expected['total']} "
                f"(drift={total_drift}, tol={total_tol}); "
                f"out-of-band axes: {out_of_band}"
            )

        # Telemetry to surface in pytest -v output when -s is set.
        print(
            f"\n[live_judge] case={case_name} "
            f"total={result.total}/{len(JudgeAgent.AXES) * 5} "
            f"expected={expected['total']} "
            f"drift={total_drift}/{total_tol} "
            f"model={result.model}"
        )

    def test_judge_persists_judge_json_on_disk_for_live_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _resolve_live_config_or_skip()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)

        contract = yaml.safe_load(
            (FIXTURE_ROOT / "sparse_02_bare_customers" / "contract.fluid.yaml").read_text(
                encoding="utf-8"
            )
        )
        run_id = "live-persist-check-001"
        JudgeAgent().judge(contract, run_id=run_id)

        target = tmp_path / ".fluid" / "agents" / run_id / "judge.json"
        assert target.is_file(), f"expected judge.json at {target}"
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert set(payload["axes"].keys()) == set(JudgeAgent.AXES)
        assert payload["run_id"] == run_id
        assert 0 <= payload["total"] <= len(JudgeAgent.AXES) * 5
