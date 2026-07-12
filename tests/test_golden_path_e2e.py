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

"""Pytest wrapper for the golden-path E2E runner.

The deterministic (no-LLM) lane ALWAYS runs — no API key, no network, no
cloud — so this is a real CI regression net. The strict-LLM lane skips
cleanly when no key is present, mirroring
``tests/integration/test_mcp_output_port_live_llm.py``.

The runner itself lives at ``scripts/golden_path_e2e.py`` and is loaded by
path here so the same code runs both as ``python scripts/golden_path_e2e.py``
and under pytest.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER_PATH = _REPO_ROOT / "scripts" / "golden_path_e2e.py"

_LLM_KEY_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("golden_path_e2e", _RUNNER_PATH)
    assert spec and spec.loader, f"cannot load runner from {_RUNNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module's namespace.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _has_llm_key() -> bool:
    return any(os.environ.get(name) for name in _LLM_KEY_ENV)


# ---------------------------------------------------------------------------
# Deterministic lane — always runs (the CI floor)
# ---------------------------------------------------------------------------
# Only the full subprocess pipeline is slow/integration-marked; the fail-loud
# gate tests below stay unmarked so they run in the fast unit lane too.


@pytest.mark.integration
@pytest.mark.slow
def test_deterministic_lane_golden_path(tmp_path: Path) -> None:
    """forge(no-LLM) -> validate -> plan -> apply --dry-run -> generate dbt
    -> dbt parse (-> dbt run) must pass twice with zero drift and no
    fallback. This is the regression net; it must never need a key.
    """
    dbt_available = runner._dbt_bin() is not None
    lane = runner.run_lane(
        lane="deterministic",
        workspace=tmp_path,
        repeat=2,
        timeout=300,
        run_dbt_run=dbt_available,
    )

    assert lane["status"] == "pass", f"lane failed: {lane.get('failure_evidence')}"

    # Provenance is machine-readable and honest: deterministic == heuristic,
    # never a silent fallback.
    assert lane["agentic_mode"] == "heuristic"
    assert lane["fallback_used"] is False
    assert lane["provider"] == "heuristic"

    # Two iterations, each carrying the required per-phase machine-readable data.
    assert len(lane["iterations"]) == 2
    required_ok_phases = ["forge", "validate", "plan", "apply_dry_run", "generate_dbt"]
    for it in lane["iterations"]:
        phases = it["phases"]
        for name in required_ok_phases:
            assert phases[name]["status"] == "pass", f"{name} not pass: {phases[name]}"
        # Machine-readable phase data present.
        assert phases["forge"]["contract_hash"].startswith("sha256:")
        assert phases["plan"]["plan_digest"].startswith("sha256:")
        assert phases["generate_dbt"]["model_count"] > 0
        assert phases["generate_dbt"]["dbt_project_hash"].startswith("sha256:")
        if dbt_available:
            assert phases["dbt_parse"]["status"] == "pass", phases["dbt_parse"]
            assert phases["dbt_run"]["status"] == "pass", phases["dbt_run"]
            assert phases["dbt_run"]["models_run"] and phases["dbt_run"]["models_run"] > 0
        else:
            assert phases["dbt_parse"]["status"] == "skipped"
            assert "dbt not installed" in phases["dbt_parse"]["skipped_reason"]

    # Repeated runs must be byte-for-byte stable on the normalized hashes.
    drift = lane["drift"]
    assert drift["contract_hash_stable"] is True, drift["contract_hashes"]
    assert drift["dbt_project_hash_stable"] is True, drift["dbt_project_hashes"]
    assert not lane["failure_evidence"]


# ---------------------------------------------------------------------------
# Strict-LLM lane — skips cleanly with no key
# ---------------------------------------------------------------------------


def test_strict_llm_lane_skips_without_key(tmp_path: Path) -> None:
    """Without an API key the strict lane must skip cleanly (not fail)."""
    if _has_llm_key():
        pytest.skip(
            "LLM API key present — the strict lane makes real billed calls; "
            "exercise it via the standalone runner, not the unit wrapper."
        )
    lane = runner.run_lane(
        lane="strict-llm",
        workspace=tmp_path,
        repeat=1,
        timeout=60,
        run_dbt_run=False,
    )
    assert lane["status"] == "skipped"
    assert "API key" in lane["skipped_reason"]


# ---------------------------------------------------------------------------
# Fail-loud gates — fast unit tests (no subprocess, no network)
# ---------------------------------------------------------------------------


def test_drift_gate_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Divergent contract hashes across iterations must fail the lane loudly."""
    hashes = iter(["sha256:aaa", "sha256:bbb"])

    def _fake_iteration(*, index: int, **_: object) -> dict:
        return {
            "iteration": index,
            "phases": {"forge": {"status": "pass"}},
            "contract_hash": next(hashes),
            "dbt_project_hash": "sha256:same",
            "agentic_mode": "heuristic",
            "fallback_used": False,
            "provider": None,
            "model": None,
        }

    monkeypatch.setattr(runner, "run_iteration", _fake_iteration)
    lane = runner.run_lane(
        lane="deterministic",
        workspace=tmp_path,
        repeat=2,
        timeout=1,
        run_dbt_run=False,
    )
    assert lane["status"] == "fail"
    assert lane["drift"]["contract_hash_stable"] is False
    assert any(ev.get("phase") == "drift" for ev in lane["failure_evidence"])


def test_strict_fallback_gate_marks_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A strict-lane forge that silently fell back to heuristic must fail."""
    fake_ok = runner.RunResult(argv=["forge"], returncode=0, stdout="", stderr="", duration_s=0.1)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: fake_ok)
    monkeypatch.setattr(
        runner,
        "_load_contract",
        lambda _p: {
            "exposes": [{"semantics": {}, "schema": []}],
            "labels": {
                "agenticMode": "heuristic",
                "agenticFallbackUsed": "true",
                "llmProvider": "anthropic",
            },
        },
    )
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text("exposes: []\n", encoding="utf-8")
    phase, _contract = runner._run_forge_stage(
        lane="strict-llm",
        intent_path=tmp_path / "intent.json",
        contract_path=contract_path,
        workspace=tmp_path,
        env={},
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        timeout=1,
    )
    assert phase["status"] == "fail"
    assert "degraded" in phase["evidence"]


def test_provider_resolution_skips_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    assert runner.resolve_strict_provider() is None
    # Explicit override still resolves (lets a caller force the lane).
    assert runner.resolve_strict_provider(provider_override="anthropic") == ("anthropic", None)


def test_report_schema_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The top-level machine-readable report carries the required keys."""

    def _fake_iteration(*, index: int, **_: object) -> dict:
        return {
            "iteration": index,
            "phases": {
                "forge": {"status": "pass", "contract_hash": "sha256:x"},
                "generate_dbt": {
                    "status": "pass",
                    "model_count": 6,
                    "dbt_project_hash": "sha256:y",
                },
            },
            "contract_hash": "sha256:x",
            "dbt_project_hash": "sha256:y",
            "agentic_mode": "heuristic",
            "fallback_used": False,
            "provider": None,
            "model": None,
            "model_count": 6,
        }

    monkeypatch.setattr(runner, "run_iteration", _fake_iteration)
    report = runner.run_golden_path(
        lanes=["deterministic"],
        repeat=2,
        timeout=1,
        run_dbt_run=False,
        keep_workspace=False,
    )
    assert report["schema_version"] == runner.SCHEMA_VERSION
    assert report["runner"] == "golden_path_e2e"
    assert report["overall_status"] == "pass"
    det = report["lanes"]["deterministic"]
    assert det["drift"]["contract_hash_stable"] is True
    assert det["iterations"][0]["phases"]["generate_dbt"]["model_count"] == 6
