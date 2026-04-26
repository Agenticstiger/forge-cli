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

from __future__ import annotations

import json

from scripts import e2e_all_modes
from scripts.e2e_all_modes import (
    PhaseResult,
    _append_provider_scorecard_history,
    _build_provider_scorecard,
    _build_provider_scorecard_trends,
    _emit_report,
    _load_provider_scorecard_history,
    _provider_scorecard_history_rows,
)


def test_provider_scorecard_marks_strict_stable_provider_as_pass():
    scorecard = _build_provider_scorecard(
        {
            "ollama": {
                "model": "gemma4:latest",
                "dbt_available": True,
                "runs": [
                    {
                        "scenario": "telco_dv2",
                        "run": 1,
                        "forge_rc": 0,
                        "agentic_mode": "strict_llm",
                        "fallback_used": "false",
                        "repair_used": "true",
                        "validate_rc": 0,
                        "generate_rc": 0,
                        "model_count": 8,
                        "dbt_parse_rc": 0,
                        "dbt_run_rc": 0,
                        "duration_s": 12.0,
                    },
                    {
                        "scenario": "telco_dv2",
                        "run": 2,
                        "forge_rc": 0,
                        "agentic_mode": "strict_llm",
                        "fallback_used": "false",
                        "repair_used": "false",
                        "validate_rc": 0,
                        "generate_rc": 0,
                        "model_count": 8,
                        "dbt_parse_rc": 0,
                        "dbt_run_rc": 0,
                        "duration_s": 10.0,
                    },
                ],
                "scenarios": {
                    "telco_dv2": {
                        "contract_stable": True,
                        "dbt_stable": True,
                    }
                },
            }
        },
        max_repair_rate=0.5,
    )

    assert scorecard["ollama"]["status"] == "pass"
    assert scorecard["ollama"]["strict_ratio"] == "2/2"
    assert scorecard["ollama"]["repair_runs"] == 1
    assert scorecard["ollama"]["repair_rate"] == 0.5
    assert scorecard["ollama"]["failed_runs"] == []


def test_provider_scorecard_fails_on_fallback_or_dbt_failure():
    scorecard = _build_provider_scorecard(
        {
            "anthropic": {
                "model": "claude-sonnet-4-6",
                "dbt_available": True,
                "runs": [
                    {
                        "scenario": "retail_dimensional",
                        "run": 1,
                        "forge_rc": 0,
                        "agentic_mode": "llm_with_fallback",
                        "fallback_used": "true",
                        "repair_used": "false",
                        "validate_rc": 0,
                        "generate_rc": 0,
                        "model_count": 4,
                        "dbt_parse_rc": 0,
                        "dbt_run_rc": 1,
                    }
                ],
                "scenarios": {
                    "retail_dimensional": {
                        "contract_stable": False,
                        "dbt_stable": False,
                    }
                },
            }
        }
    )

    assert scorecard["anthropic"]["status"] == "fail"
    assert scorecard["anthropic"]["fallback_runs"] == 1
    assert scorecard["anthropic"]["failed_runs"][0]["reasons"] == [
        "not_strict_llm",
        "fallback_used",
        "dbt_run_failed",
    ]


def test_provider_scorecard_fails_when_repair_budget_is_exceeded():
    scorecard = _build_provider_scorecard(
        {
            "ollama": {
                "model": "gemma4:latest",
                "dbt_available": True,
                "runs": [
                    {
                        "scenario": "telco_dv2",
                        "run": 1,
                        "forge_rc": 0,
                        "agentic_mode": "strict_llm",
                        "fallback_used": "false",
                        "repair_used": "true",
                        "validate_rc": 0,
                        "generate_rc": 0,
                        "model_count": 8,
                        "dbt_parse_rc": 0,
                        "dbt_run_rc": 0,
                        "duration_s": 401.0,
                    }
                ],
                "scenarios": {
                    "telco_dv2": {
                        "contract_stable": True,
                        "dbt_stable": True,
                    }
                },
            }
        },
        max_repair_rate=0.0,
        max_avg_run_seconds=300.0,
    )

    assert scorecard["ollama"]["status"] == "fail"
    assert scorecard["ollama"]["quality_gaps"] == [
        "repair_rate 100.00% exceeds budget 0.00%",
        "avg_duration_s 401.0s exceeds budget 300.0s",
    ]


def test_explicit_provider_matrix_fails_when_requested_provider_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUID_E2E_LLM_PROVIDERS", "gemini")
    monkeypatch.setattr(e2e_all_modes, "_llm_provider_available", lambda provider: False)

    result = e2e_all_modes._phase7_llm_providers(tmp_path)

    assert result.skipped_reason is None
    assert result.ok is False
    assert result.findings[0].severity == "error"
    assert result.phase_data["providers_unavailable"] == ["gemini"]


def test_provider_scorecard_trends_detect_repair_rate_regression():
    trends = _build_provider_scorecard_trends(
        {
            "ollama": {
                "model": "gemma4:latest",
                "repair_rate": 0.5,
                "avg_duration_s": 20.0,
            }
        },
        [
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "scenarios": ["telco_dv2"],
                "repair_rate": 0.0,
                "avg_duration_s": 18.0,
            }
        ],
        scenarios=["telco_dv2"],
        max_repair_rate_delta=0.0,
        max_avg_run_seconds_delta=60.0,
    )

    assert trends["ollama"]["status"] == "fail"
    assert trends["ollama"]["repair_rate_delta"] == 0.5
    assert trends["ollama"]["trend_gaps"] == ["repair_rate_delta 50.00% exceeds budget 0.00%"]


def test_provider_scorecard_trends_detect_latency_regression():
    trends = _build_provider_scorecard_trends(
        {
            "ollama": {
                "model": "gemma4:latest",
                "repair_rate": 0.0,
                "avg_duration_s": 90.0,
            }
        },
        [
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "scenarios": ["telco_dv2"],
                "repair_rate": 0.0,
                "avg_duration_s": 20.0,
            }
        ],
        scenarios=["telco_dv2"],
        max_repair_rate_delta=0.0,
        max_avg_run_seconds_delta=60.0,
    )

    assert trends["ollama"]["status"] == "fail"
    assert trends["ollama"]["avg_duration_s_delta"] == 70.0
    assert trends["ollama"]["trend_gaps"] == ["avg_duration_s_delta 70.0s exceeds budget 60.0s"]


def test_provider_scorecard_trends_only_compare_matching_model_and_scenarios():
    trends = _build_provider_scorecard_trends(
        {
            "ollama": {
                "model": "gemma4:latest",
                "repair_rate": 1.0,
                "avg_duration_s": 90.0,
            }
        },
        [
            {
                "provider": "ollama",
                "model": "other-model",
                "scenarios": ["telco_dv2"],
                "repair_rate": 0.0,
                "avg_duration_s": 20.0,
            },
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "scenarios": ["retail_dimensional"],
                "repair_rate": 0.0,
                "avg_duration_s": 20.0,
            },
        ],
        scenarios=["telco_dv2"],
    )

    assert trends["ollama"]["status"] == "no_history"
    assert trends["ollama"]["history_runs"] == 0


def test_provider_scorecard_history_round_trip(tmp_path):
    phase_data = {
        "scenarios_tested": ["telco_dv2"],
        "provider_scorecard": {
            "ollama": {
                "status": "pass",
                "model": "gemma4:latest",
                "total_runs": 2,
                "strict_ratio": "2/2",
                "fallback_runs": 0,
                "repair_runs": 0,
                "repair_rate": 0.0,
                "avg_duration_s": 76.5,
                "dbt_run_success": 2,
                "dbt_run_total": 2,
                "contract_stability": "1/1",
                "dbt_stability": "1/1",
                "quality_gaps": [],
            }
        },
    }
    rows = _provider_scorecard_history_rows(
        phase_data,
        run_id="20260426T000000000000Z",
        generated_at="20260426T000000000000Z",
    )
    history_path = tmp_path / "provider_scorecard_history.jsonl"

    _append_provider_scorecard_history(history_path, rows, limit=10)
    loaded = _load_provider_scorecard_history(history_path)

    assert loaded == rows
    assert json.loads(history_path.read_text(encoding="utf-8").splitlines()[0]) == rows[0]


def test_emit_report_includes_trends_and_persists_history(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUID_E2E_SCORECARD_HISTORY", "1")
    result = PhaseResult(
        phase="llm_providers",
        ok=True,
        duration_s=1.0,
        checks_passed=1,
        checks_total=1,
        phase_data={
            "scenarios_tested": ["telco_dv2"],
            "provider_scorecard": {
                "ollama": {
                    "status": "pass",
                    "model": "gemma4:latest",
                    "total_runs": 2,
                    "strict_ratio": "2/2",
                    "fallback_runs": 0,
                    "repair_runs": 0,
                    "repair_rate": 0.0,
                    "avg_duration_s": 76.5,
                    "dbt_run_success": 2,
                    "dbt_run_total": 2,
                    "contract_stability": "1/1",
                    "dbt_stability": "1/1",
                    "quality_gaps": [],
                }
            },
            "provider_scorecard_trends": {
                "ollama": {
                    "status": "pass",
                    "history_runs": 3,
                    "repair_rate_delta": 0.0,
                    "avg_duration_s_delta": -10.0,
                    "trend_gaps": [],
                }
            },
        },
    )

    summary_path = _emit_report([result], tmp_path / "20260426T000000000000Z")

    assert "### Provider Trends" in summary_path.read_text(encoding="utf-8")
    history_path = tmp_path / "provider_scorecard_history.jsonl"
    assert history_path.exists()
    assert _load_provider_scorecard_history(history_path)[0]["provider"] == "ollama"
