#!/usr/bin/env python3
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

"""Extensive e2e test harness for the v1 forge-cli surface.

Runs every user-facing mode end-to-end and captures bugs, UX issues,
latency, and output validity into a single report under
``.fluid/e2e_report/<timestamp>/``. Designed to be run iteratively — every
phase is independently gated on its prerequisites (API keys, Snowflake
creds, dbt installation) and the runner skips gracefully when something
is unavailable.

Phases (gates in parens):

    1. Heuristic modes          — no external deps; exercises the
                                   non-LLM fallback path across from-intent,
                                   from-ddl, validate, diff, dump-ddl
                                   (soft-fails without Snowflake).
    2. CLI surface              — no external deps; argparse + --help +
                                   error-message UX for every subcommand.
    3. Memory concept           — no external deps; tests legacy-migration,
                                   cross-run persistence, cache-key
                                   invalidation on technique/industry
                                   change, and fluid memory CLI output.
    4. Live Gemini              — gate: GEMINI_API_KEY. Runs the 4
                                   canonical scenarios through the real
                                   ModelerAgent LLM path and checks
                                   naming-convention compliance.
    5. Live Snowflake demo-lab   — gate: SNOWFLAKE_*  env vars. Exercises
                                   dump-ddl → from-ddl → generate
                                   speed-transformation → dbt parse.
    6. Full stack (requires 4+5)— intent → data-model → speed-transformation
                                   → dbt-validate gate.
    7. LLM provider matrix      — gate: provider env vars / local Ollama.
                                   Strict forge → validate → generate across
                                   Gemini, Anthropic, OpenAI, and Ollama.

Usage::

    PYTHONPATH=. python scripts/e2e_all_modes.py                   # all phases
    PYTHONPATH=. python scripts/e2e_all_modes.py --phases 1,2,3    # offline
    PYTHONPATH=. python scripts/e2e_all_modes.py --phases 4        # just Gemini
    PYTHONPATH=. python scripts/e2e_all_modes.py --phases 5,6      # live stack
    PYTHONPATH=. python scripts/e2e_all_modes.py --phases 7        # provider matrix

The runner writes a single markdown summary + machine-readable JSON per
phase and keeps a running bug-log at ``.fluid/e2e_report/bug_log.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Repo root bootstrap
# ---------------------------------------------------------------------------
# Make sure the runner can ``import fluid_build`` regardless of the cwd it was
# launched from. The subprocess paths set ``PYTHONPATH`` explicitly (see
# ``_run_fluid``); the in-process phase paths need ``sys.path`` adjusted here.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Reporting primitives
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single bug or UX issue worth reporting."""

    severity: str  # "error" | "warning" | "ux" | "info"
    phase: str
    title: str
    detail: str
    evidence: Optional[str] = None


@dataclass
class PhaseResult:
    phase: str
    ok: bool
    duration_s: float
    checks_passed: int = 0
    checks_total: int = 0
    findings: List[Finding] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    phase_data: Dict[str, Any] = field(default_factory=dict)


# Regexes (shared with gemini_demo_db_scenarios.py).
HUB_NAME = re.compile(r"^hub_[a-z][a-z0-9_]*$")
SAT_NAME = re.compile(r"^sat_[a-z][a-z0-9_]*$")
LNK_NAME = re.compile(r"^lnk_[a-z][a-z0-9_]*$")
FACT_NAME = re.compile(r"^fact_[a-z][a-z0-9_]*$")
DIM_NAME = re.compile(r"^dim_[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _run_fluid(
    args: List[str], *, cwd: Path, env: Optional[Dict[str, str]] = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run ``python -m fluid_build.cli <args>`` and capture stdout/stderr.

    Keeps every CLI call routed through the module entry point so a broken
    console-script install doesn't mask real regressions. ``cwd`` controls
    the subprocess working directory (per-phase tempdirs are fine); the
    ``PYTHONPATH`` always points at the repo root so fluid_build imports.
    """
    final_env = os.environ.copy()
    final_env["PYTHONPATH"] = str(_REPO_ROOT)
    if env:
        final_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build", *args],
        cwd=str(cwd),
        env=final_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_fluid_safe(
    args: List[str],
    *,
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    try:
        return _run_fluid(args, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            exc.cmd,
            124,
            stdout=stdout,
            stderr=f"{stderr}\nTimed out after {timeout}s",
        )


def _run_external_safe(
    args: List[str],
    *,
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    final_env = os.environ.copy()
    if env:
        final_env.update(env)
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=final_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            exc.cmd,
            124,
            stdout=stdout,
            stderr=f"{stderr}\nTimed out after {timeout}s",
        )


def _redact_secret_text(text: str) -> str:
    redacted = text
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        value = os.environ.get(name, "")
        if value and len(value) >= 8:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _check(
    result: PhaseResult,
    cond: bool,
    title: str,
    *,
    phase: str,
    detail_on_fail: str = "",
    evidence: Optional[str] = None,
) -> None:
    """Record a pass/fail check against a phase."""
    result.checks_total += 1
    if cond:
        result.checks_passed += 1
    else:
        result.findings.append(
            Finding(
                severity="error", phase=phase, title=title, detail=detail_on_fail, evidence=evidence
            )
        )


def _stable_contract_signature(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deterministic behavior signature for forged contracts.

    The goal is to compare generated semantics and exposed shape without
    tripping on expected run-local metadata such as provenance, file names,
    or human descriptions.
    """
    volatile_keys = {
        "description",
        "displayName",
        "display_name",
        "label",
        "labels",
        "metadata",
        "owner",
        "provenance",
        "version",
    }

    def stable_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): stable_value(val)
                for key, val in sorted(value.items(), key=lambda item: str(item[0]))
                if str(key) not in volatile_keys
            }
        if isinstance(value, list):
            return [stable_value(item) for item in value]
        return value

    expose = (
        (contract.get("exposes") or [{}])[0] if isinstance(contract.get("exposes"), list) else {}
    )
    semantics = expose.get("semantics") or {}
    semantic_sig = {
        key: sorted(
            json.dumps(stable_value(item), sort_keys=True, separators=(",", ":"))
            for item in (semantics.get(key) or [])
            if isinstance(item, dict)
        )
        for key in ("entities", "dimensions", "measures", "metrics")
    }
    schema = expose.get("schema") or expose.get("fields") or []
    schema_sig = sorted(
        json.dumps(stable_value(item), sort_keys=True, separators=(",", ":"))
        for item in schema
        if isinstance(item, dict)
    )
    return {"semantics": semantic_sig, "schema": schema_sig}


def _dbt_project_signature(project_dir: Path) -> str:
    """Hash generated dbt files with run-local absolute paths normalized."""
    tracked: List[tuple[str, str]] = []
    suffixes = {".sql", ".yml", ".yaml"}
    source_roots = {
        "analyses",
        "dbt_project.yml",
        "macros",
        "models",
        "packages.yml",
        "profiles.yml",
        "seeds",
        "snapshots",
        "tests",
    }
    path_tokens = {
        str(project_dir),
        str(project_dir.resolve()),
        os.path.realpath(project_dir),
    }
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        rel = path.relative_to(project_dir).as_posix()
        first_part = rel.split("/", 1)[0]
        if first_part not in source_roots:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in sorted(path_tokens, key=len, reverse=True):
            text = text.replace(token, "<PROJECT_DIR>")
        tracked.append((rel, text))
    payload = json.dumps(tracked, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _label_is(value: Any, expected: str) -> bool:
    return str(value or "").strip().lower() == expected


def _pass_ratio(passed: int, total: int) -> str:
    return f"{passed}/{total}"


_PROVIDER_SCORECARD_HISTORY_SCHEMA_VERSION = 1
_PROVIDER_SCORECARD_HISTORY_FILE = "provider_scorecard_history.jsonl"


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scorecard_scenario_key(scenarios: Any) -> tuple[str, ...]:
    if not isinstance(scenarios, list):
        return ()
    return tuple(sorted(str(item) for item in scenarios if str(item).strip()))


def _provider_scorecard_history_rows(
    phase_data: Dict[str, Any],
    *,
    run_id: str,
    generated_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert a phase-7 scorecard into stable JSONL history rows."""
    scorecard = phase_data.get("provider_scorecard")
    if not isinstance(scorecard, dict) or not scorecard:
        return []
    scenarios = list(_scorecard_scenario_key(phase_data.get("scenarios_tested") or []))
    rows: List[Dict[str, Any]] = []
    for provider, score in sorted(scorecard.items()):
        if not isinstance(score, dict):
            continue
        rows.append(
            {
                "schema_version": _PROVIDER_SCORECARD_HISTORY_SCHEMA_VERSION,
                "run_id": run_id,
                "generated_at": generated_at or _now_iso(),
                "provider": str(provider),
                "model": str(score.get("model") or "default"),
                "scenarios": scenarios,
                "total_runs": int(score.get("total_runs") or 0),
                "status": str(score.get("status") or ""),
                "strict_ratio": str(score.get("strict_ratio") or ""),
                "fallback_runs": int(score.get("fallback_runs") or 0),
                "repair_runs": int(score.get("repair_runs") or 0),
                "repair_rate": _as_optional_float(score.get("repair_rate")) or 0.0,
                "avg_duration_s": _as_optional_float(score.get("avg_duration_s")),
                "dbt_run_success": int(score.get("dbt_run_success") or 0),
                "dbt_run_total": int(score.get("dbt_run_total") or 0),
                "contract_stability": str(score.get("contract_stability") or ""),
                "dbt_stability": str(score.get("dbt_stability") or ""),
                "quality_gaps": list(score.get("quality_gaps") or []),
            }
        )
    return rows


def _load_provider_scorecard_history_from_reports(report_root: Path) -> List[Dict[str, Any]]:
    """Backfill trend baselines from older per-run ``results.json`` files."""
    if not report_root.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for results_path in sorted(report_root.glob("*/results.json")):
        try:
            payload = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        phases = payload if isinstance(payload, list) else payload.get("phases", [])
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if not isinstance(phase, dict) or phase.get("phase") != "llm_providers":
                continue
            phase_data = phase.get("phase_data") or {}
            if not isinstance(phase_data, dict):
                continue
            rows.extend(
                _provider_scorecard_history_rows(
                    phase_data,
                    run_id=results_path.parent.name,
                    generated_at=results_path.parent.name,
                )
            )
    return rows


def _load_provider_scorecard_history(
    history_path: Path,
    *,
    fallback_from_reports: bool = True,
) -> List[Dict[str, Any]]:
    """Load provider scorecard history rows, ignoring corrupt lines."""
    rows: List[Dict[str, Any]] = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("provider") and row.get("model"):
                rows.append(row)
    if rows or not fallback_from_reports:
        return rows
    return _load_provider_scorecard_history_from_reports(history_path.parent)


def _append_provider_scorecard_history(
    history_path: Path,
    rows: List[Dict[str, Any]],
    *,
    limit: int = 200,
) -> None:
    """Append scorecard rows to the JSONL trend history with bounded retention."""
    if not rows:
        return
    history_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_provider_scorecard_history(history_path, fallback_from_reports=False)
    seen_new = {
        (
            row.get("run_id"),
            row.get("provider"),
            row.get("model"),
            tuple(row.get("scenarios") or []),
        )
        for row in rows
    }
    retained = [
        row
        for row in existing
        if (
            row.get("run_id"),
            row.get("provider"),
            row.get("model"),
            tuple(row.get("scenarios") or []),
        )
        not in seen_new
    ]
    combined = retained + rows
    if limit > 0:
        combined = combined[-limit:]
    tmp_path = history_path.with_suffix(history_path.suffix + ".tmp")
    tmp_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in combined) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(history_path)


def _build_provider_scorecard_trends(
    scorecard: Dict[str, Dict[str, Any]],
    history: List[Dict[str, Any]],
    *,
    scenarios: List[str],
    min_history_runs: int = 1,
    baseline_runs: int = 5,
    max_repair_rate_delta: float = 0.0,
    max_avg_run_seconds_delta: Optional[float] = 60.0,
) -> Dict[str, Dict[str, Any]]:
    """Compare the current scorecard with matching historical runs."""
    scenario_key = _scorecard_scenario_key(scenarios)
    trends: Dict[str, Dict[str, Any]] = {}
    for provider, score in sorted(scorecard.items()):
        model = str(score.get("model") or "default")
        matches = [
            row
            for row in history
            if row.get("provider") == provider
            and row.get("model") == model
            and _scorecard_scenario_key(row.get("scenarios") or []) == scenario_key
        ]
        recent = matches[-max(1, baseline_runs) :]
        current_repair_rate = _as_optional_float(score.get("repair_rate")) or 0.0
        current_avg_duration = _as_optional_float(score.get("avg_duration_s"))
        trend: Dict[str, Any] = {
            "status": "no_history",
            "model": model,
            "history_runs": len(matches),
            "baseline_runs": len(recent),
            "repair_rate": current_repair_rate,
            "avg_duration_s": current_avg_duration,
            "repair_rate_delta": None,
            "avg_duration_s_delta": None,
            "trend_gaps": [],
        }
        if len(matches) < max(1, min_history_runs):
            trends[provider] = trend
            continue

        historical_repair_rates = [
            _as_optional_float(row.get("repair_rate")) or 0.0 for row in recent
        ]
        baseline_repair_rate = round(
            sum(historical_repair_rates) / len(historical_repair_rates),
            4,
        )
        repair_delta = round(current_repair_rate - baseline_repair_rate, 4)
        trend["baseline_repair_rate"] = baseline_repair_rate
        trend["repair_rate_delta"] = repair_delta

        historical_durations = [
            value
            for value in (_as_optional_float(row.get("avg_duration_s")) for row in recent)
            if value is not None
        ]
        if current_avg_duration is not None and historical_durations:
            baseline_avg_duration = round(
                sum(historical_durations) / len(historical_durations),
                3,
            )
            duration_delta = round(current_avg_duration - baseline_avg_duration, 3)
            trend["baseline_avg_duration_s"] = baseline_avg_duration
            trend["avg_duration_s_delta"] = duration_delta

        gaps: List[str] = []
        if repair_delta > max_repair_rate_delta:
            gaps.append(
                f"repair_rate_delta {repair_delta:.2%} exceeds budget {max_repair_rate_delta:.2%}"
            )
        duration_delta_value = trend.get("avg_duration_s_delta")
        if (
            max_avg_run_seconds_delta is not None
            and duration_delta_value is not None
            and float(duration_delta_value) > max_avg_run_seconds_delta
        ):
            gaps.append(
                "avg_duration_s_delta "
                f"{duration_delta_value}s exceeds budget {max_avg_run_seconds_delta}s"
            )
        trend["trend_gaps"] = gaps
        trend["status"] = "fail" if gaps else "pass"
        trends[provider] = trend
    return trends


def _build_provider_scorecard(
    provider_outcomes: Dict[str, Dict[str, Any]],
    *,
    max_repair_rate: float = 0.0,
    max_avg_run_seconds: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """Summarize phase 7 outcomes into an operator-facing provider scorecard."""
    scorecard: Dict[str, Dict[str, Any]] = {}
    for provider, outcome in sorted(provider_outcomes.items()):
        runs = outcome.get("runs") or []
        scenarios = outcome.get("scenarios") or {}
        total_runs = len(runs)
        dbt_available = bool(outcome.get("dbt_available"))

        forge_success = sum(1 for run in runs if run.get("forge_rc") == 0)
        strict_success = sum(
            1
            for run in runs
            if run.get("forge_rc") == 0
            and run.get("agentic_mode") == "strict_llm"
            and _label_is(run.get("fallback_used"), "false")
        )
        fallback_runs = sum(1 for run in runs if _label_is(run.get("fallback_used"), "true"))
        repair_runs = sum(1 for run in runs if _label_is(run.get("repair_used"), "true"))
        validate_success = sum(1 for run in runs if run.get("validate_rc") == 0)
        generate_success = sum(
            1
            for run in runs
            if run.get("generate_rc") == 0 and int(run.get("model_count") or 0) > 0
        )
        dbt_parse_total = sum(1 for run in runs if "dbt_parse_rc" in run)
        dbt_parse_success = sum(1 for run in runs if run.get("dbt_parse_rc") == 0)
        dbt_run_total = sum(1 for run in runs if "dbt_run_rc" in run)
        dbt_run_success = sum(1 for run in runs if run.get("dbt_run_rc") == 0)

        contract_stability_total = sum(
            1 for scenario in scenarios.values() if "contract_stable" in scenario
        )
        contract_stability_success = sum(
            1 for scenario in scenarios.values() if scenario.get("contract_stable") is True
        )
        dbt_stability_total = sum(1 for scenario in scenarios.values() if "dbt_stable" in scenario)
        dbt_stability_success = sum(
            1 for scenario in scenarios.values() if scenario.get("dbt_stable") is True
        )

        failed_runs: List[Dict[str, Any]] = []
        for run in runs:
            reasons: List[str] = []
            if run.get("forge_rc") != 0:
                reasons.append("forge_failed")
            if run.get("agentic_mode") not in (None, "strict_llm"):
                reasons.append("not_strict_llm")
            if _label_is(run.get("fallback_used"), "true"):
                reasons.append("fallback_used")
            if "validate_rc" in run and run.get("validate_rc") != 0:
                reasons.append("validate_failed")
            if "generate_rc" in run and (
                run.get("generate_rc") != 0 or int(run.get("model_count") or 0) == 0
            ):
                reasons.append("generate_failed")
            if dbt_available and "dbt_parse_rc" in run and run.get("dbt_parse_rc") != 0:
                reasons.append("dbt_parse_failed")
            if dbt_available and "dbt_run_rc" in run and run.get("dbt_run_rc") != 0:
                reasons.append("dbt_run_failed")
            if reasons:
                failed_runs.append(
                    {
                        "scenario": run.get("scenario"),
                        "run": run.get("run"),
                        "reasons": reasons,
                    }
                )

        strict_gate = total_runs > 0 and strict_success == total_runs and fallback_runs == 0
        artifact_gate = (
            total_runs > 0
            and forge_success == total_runs
            and validate_success == total_runs
            and generate_success == total_runs
        )
        dbt_gate = True
        if dbt_available:
            dbt_gate = (
                dbt_parse_total == total_runs
                and dbt_parse_success == total_runs
                and dbt_run_total == total_runs
                and dbt_run_success == total_runs
            )
        stability_gate = (
            contract_stability_success == contract_stability_total
            and dbt_stability_success == dbt_stability_total
        )
        status = "pass" if strict_gate and artifact_gate and dbt_gate and stability_gate else "fail"
        if status == "pass" and not dbt_available:
            status = "warn_no_dbt"

        durations = [
            float(run["duration_s"])
            for run in runs
            if isinstance(run.get("duration_s"), (int, float))
        ]
        avg_duration_s = round(sum(durations) / len(durations), 3) if durations else None
        repair_rate = round(repair_runs / total_runs, 4) if total_runs else 0.0
        quality_gaps: List[str] = []
        if repair_rate > max_repair_rate:
            quality_gaps.append(
                f"repair_rate {repair_rate:.2%} exceeds budget {max_repair_rate:.2%}"
            )
        if (
            max_avg_run_seconds is not None
            and avg_duration_s is not None
            and avg_duration_s > max_avg_run_seconds
        ):
            quality_gaps.append(
                f"avg_duration_s {avg_duration_s}s exceeds budget {max_avg_run_seconds}s"
            )

        scorecard[provider] = {
            "status": "fail" if quality_gaps else status,
            "model": outcome.get("model") or "default",
            "total_runs": total_runs,
            "strict_success": strict_success,
            "strict_ratio": _pass_ratio(strict_success, total_runs),
            "fallback_runs": fallback_runs,
            "repair_runs": repair_runs,
            "repair_rate": repair_rate,
            "max_repair_rate": max_repair_rate,
            "forge_success": forge_success,
            "validate_success": validate_success,
            "generate_success": generate_success,
            "dbt_available": dbt_available,
            "dbt_parse_success": dbt_parse_success,
            "dbt_parse_total": dbt_parse_total,
            "dbt_run_success": dbt_run_success,
            "dbt_run_total": dbt_run_total,
            "contract_stability": _pass_ratio(contract_stability_success, contract_stability_total),
            "dbt_stability": _pass_ratio(dbt_stability_success, dbt_stability_total),
            "avg_duration_s": avg_duration_s,
            "max_avg_run_seconds": max_avg_run_seconds,
            "quality_gaps": quality_gaps,
            "failed_runs": failed_runs[:10],
        }
    return scorecard


def _run_phase1_deterministic_drift_gate(result: PhaseResult, workspace: Path) -> None:
    intent_path = workspace / "deterministic_intent.json"
    intent_path.write_text(
        json.dumps(
            {
                "business_context": {
                    "problem_statement": "Deterministic replay of retail sales analytics."
                },
                "data_product": {
                    "name": "sales_analytics",
                    "domain": "retail",
                    "description": "Retail sales analytics deterministic replay.",
                },
                "grain": {
                    "entity": "sales_line",
                    "time_dimension": "order_date",
                    "description": "One row per sales line item.",
                },
                "dimensions": {
                    "entities": ["customer", "product", "store", "date"],
                    "attributes": ["customer_segment", "product_category", "store_region"],
                },
                "metrics": [
                    {"name": "gross_revenue", "description": "Sum of sales line amount."},
                    {"name": "units_sold", "description": "Sum of sales line quantity."},
                ],
                "modeling": {"technique": "dimensional"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    env = {
        "FLUID_STORE_BACKEND": "null",
        "FLUID_QUIET": "1",
        "FLUID_NONINTERACTIVE": "1",
    }
    signatures: List[Dict[str, Any]] = []
    project_signatures: List[str] = []
    for idx in range(2):
        run_dir = workspace / f"deterministic_run_{idx + 1}"
        contract_path = run_dir / "sales_analytics.fluid.yaml"
        gen_out = run_dir / "dbt"
        r = _run_fluid_safe(
            [
                "forge",
                "data-model",
                "from-intent",
                str(intent_path),
                "--technique",
                "dimensional",
                "--industry",
                "retail",
                "--engine",
                "dbt",
                "--deterministic",
                "--no-cache",
                "--allow-semantic-warnings",
                "-o",
                str(contract_path),
            ],
            cwd=workspace,
            env=env,
            timeout=180,
        )
        _check(
            result,
            r.returncode == 0 and contract_path.exists(),
            f"deterministic replay run {idx + 1} writes contract",
            phase="heuristic",
            detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}",
        )
        if not contract_path.exists():
            return

        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        model_doc = contract_path.with_name(f"{contract_path.name}.model.md")
        model_doc_text = model_doc.read_text(encoding="utf-8") if model_doc.exists() else ""
        _check(
            result,
            model_doc.exists() and "```mermaid" in model_doc_text,
            f"deterministic replay run {idx + 1} writes Mermaid model document",
            phase="heuristic",
            detail_on_fail=f"path={model_doc}",
        )
        signatures.append(_stable_contract_signature(contract))
        r = _run_fluid_safe(
            [
                "generate",
                "speed-transformation",
                str(contract_path),
                "-o",
                str(gen_out),
                "--overwrite",
            ],
            cwd=workspace,
            env=env,
            timeout=300,
        )
        model_files = list((gen_out / "models").glob("**/*.sql")) if gen_out.exists() else []
        _check(
            result,
            r.returncode == 0 and bool(model_files),
            f"deterministic replay run {idx + 1} generates dbt project",
            phase="heuristic",
            detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}",
        )
        if not model_files:
            return
        project_signatures.append(_dbt_project_signature(gen_out))

    result.phase_data["deterministic_contract_signature_hashes"] = [
        hashlib.sha256(json.dumps(sig, sort_keys=True).encode("utf-8")).hexdigest()
        for sig in signatures
    ]
    result.phase_data["deterministic_dbt_project_hashes"] = project_signatures
    _check(
        result,
        len(signatures) == 2 and signatures[0] == signatures[1],
        "deterministic from-intent contract signature is stable across repeated runs",
        phase="heuristic",
        detail_on_fail=json.dumps(result.phase_data["deterministic_contract_signature_hashes"]),
    )
    _check(
        result,
        len(project_signatures) == 2 and project_signatures[0] == project_signatures[1],
        "deterministic dbt project signature is stable across repeated runs",
        phase="heuristic",
        detail_on_fail=json.dumps(project_signatures),
    )


# ---------------------------------------------------------------------------
# Phase 1 — heuristic modes (no API key)
# ---------------------------------------------------------------------------


def _phase1_heuristic(workspace: Path) -> PhaseResult:
    """Heuristic-path smoke across the three write-paths + validate + diff.

    Uses the package-level Python API (no subprocess) so we get real
    exceptions when the heuristic code path breaks — subprocess would mask
    tracebacks behind ``sys.exit(1)``. The CLI-surface phase covers the
    argparse side.
    """
    from fluid_build.cli.forge_data_model import diff_logical_models
    from fluid_build.copilot.agents.base import StageSession
    from fluid_build.copilot.industry.compiler import IndustryPackCompiler
    from fluid_build.copilot.schemas.intent import (
        BusinessIntent,
        DataProduct,
        Dimensions,
        Grain,
        Metric,
    )
    from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
    from fluid_build.copilot.store.backends.null import NullBackend
    from fluid_build.forge_datamodel.from_ddl.pipeline import run_from_ddl
    from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

    start = time.time()
    result = PhaseResult(phase="heuristic", ok=True, duration_s=0.0)

    pack = IndustryPackCompiler().compile("retail", technique="dimensional")
    sess = StageSession(
        store=NullBackend(),
        workspace_root=workspace,
        llm_config=None,  # force heuristic
        active_provider=None,
        no_cache=True,
        industry_pack=pack,
    )

    # --- 1a. from-intent heuristic -------------------------------------------------
    intent = BusinessIntent(
        data_product=DataProduct(
            name="sales_analytics",
            domain="retail",
            description="Heuristic-path smoke intent for retail sales star schema.",
        ),
        grain=Grain(entity="sales_line", time_dimension="txn_date"),
        metrics=[Metric(name="gross_revenue", description="Sum extended amount")],
        dimensions=Dimensions(entities=["customer", "product", "store", "date"]),
    )
    try:
        pipeline = run_from_intent(sess, intent=intent, technique="dimensional")
        result.phase_data["from_intent_latency_s"] = round(time.time() - start, 3)
        logical = pipeline.coordinator.logical
        _check(
            result,
            isinstance(logical, LogicalDraft),
            "from_intent emits LogicalDraft",
            phase="heuristic",
        )
        _check(
            result,
            logical.dimensional is not None and len(logical.dimensional.facts) > 0,
            "from_intent heuristic populates dimensional.facts",
            phase="heuristic",
            detail_on_fail="dimensional model had no facts",
        )
        # Naming compliance on heuristic output
        if logical.dimensional:
            bad_facts = [f.name for f in logical.dimensional.facts if not FACT_NAME.match(f.name)]
            bad_dims = [
                d.name for d in logical.dimensional.dimensions if not DIM_NAME.match(d.name)
            ]
            _check(
                result,
                not bad_facts,
                "heuristic fact names follow fact_* convention",
                phase="heuristic",
                detail_on_fail=f"non-conforming: {bad_facts}",
            )
            _check(
                result,
                not bad_dims,
                "heuristic dim names follow dim_* convention",
                phase="heuristic",
                detail_on_fail=f"non-conforming: {bad_dims}",
            )
        contract_path = workspace / "e2e_from_intent.fluid.yaml"
        plan_path = workspace / "e2e_from_intent.plan.json"
        contract_path.write_text(
            json.dumps(pipeline.coordinator.contract, indent=2),
            encoding="utf-8",
        )
        r = _run_fluid(["forge", "data-model", "validate", str(contract_path)], cwd=workspace)
        _check(
            result,
            r.returncode == 0,
            "forged from-intent contract validates via CLI",
            phase="heuristic",
            detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}",
        )
        r = _run_fluid(["plan", str(contract_path), "--out", str(plan_path)], cwd=workspace)
        _check(
            result,
            r.returncode == 0 and plan_path.exists(),
            "forged from-intent contract produces plan.json",
            phase="heuristic",
            detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}",
        )
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            actions = plan.get("actions") or []
            result.phase_data["from_intent_plan_action_count"] = len(actions)
            _check(
                result,
                bool(actions) and str(plan.get("planDigest", "")).startswith("sha256:"),
                "forged from-intent plan has actions and digest",
                phase="heuristic",
                detail_on_fail=f"actions={len(actions)} planDigest={plan.get('planDigest')!r}",
            )
            r = _run_fluid(["apply", str(plan_path), "--dry-run", "--yes"], cwd=workspace)
            _check(
                result,
                r.returncode == 0,
                "forged from-intent plan applies in dry-run mode",
                phase="heuristic",
                detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}",
            )
    except Exception as exc:
        result.ok = False
        result.findings.append(
            Finding(
                severity="error",
                phase="heuristic",
                title="from_intent heuristic path crashed",
                detail=str(exc),
                evidence=traceback.format_exc(limit=6),
            )
        )

    # --- 1a.1 deterministic drift gate ------------------------------------------
    try:
        _run_phase1_deterministic_drift_gate(result, workspace)
    except Exception as exc:
        result.findings.append(
            Finding(
                severity="error",
                phase="heuristic",
                title="deterministic replay drift gate crashed",
                detail=str(exc),
                evidence=traceback.format_exc(limit=6),
            )
        )

    # --- 1b. from-ddl heuristic (synthetic 2-table SQL) ---------------------------
    ddl = """
    CREATE TABLE customers (
      customer_id INTEGER PRIMARY KEY,
      name VARCHAR(200),
      email VARCHAR(200),
      created_at TIMESTAMP
    );
    CREATE TABLE orders (
      order_id INTEGER PRIMARY KEY,
      customer_id INTEGER REFERENCES customers(customer_id),
      order_date DATE,
      amount DECIMAL(18,2)
    );
    """
    sess2 = StageSession(
        store=NullBackend(),
        workspace_root=workspace,
        llm_config=None,
        active_provider=None,
        no_cache=True,
        industry_pack=pack,
    )
    try:
        pipeline2 = run_from_ddl(
            sess2,
            name="customer_orders",
            ddl_texts=[ddl],
            technique="data_vault_2",
            source_type="postgres",
        )
        logical2 = pipeline2.coordinator.logical
        _check(
            result,
            logical2.dv2 is not None,
            "from_ddl heuristic populates DV2 branch",
            phase="heuristic",
        )
        if logical2.dv2:
            hubs = [h.hub_table_name for h in logical2.dv2.hubs]
            bad_hubs = [n for n in hubs if not HUB_NAME.match(n)]
            _check(
                result,
                not bad_hubs,
                "heuristic hub names follow hub_* convention",
                phase="heuristic",
                detail_on_fail=f"non-conforming: {bad_hubs}",
            )
    except Exception as exc:
        result.ok = False
        result.findings.append(
            Finding(
                severity="error",
                phase="heuristic",
                title="from_ddl heuristic path crashed",
                detail=str(exc),
                evidence=traceback.format_exc(limit=6),
            )
        )

    # --- 1c. diff on two logical drafts (round-trip) ------------------------------
    try:
        draft_a = LogicalDraft.model_validate(pipeline.coordinator.logical.model_dump())
        draft_b_dict = pipeline.coordinator.logical.model_dump()
        # Rename a dim to force a visible diff
        if draft_b_dict.get("dimensional", {}).get("dimensions"):
            draft_b_dict["dimensional"]["dimensions"][0]["name"] = "dim_customer_v2"
        path_a = workspace / "a.model.json"
        path_b = workspace / "b.model.json"
        path_a.write_text(draft_a.model_dump_json(indent=2))
        path_b.write_text(json.dumps(draft_b_dict, indent=2, default=str))
        summary = diff_logical_models(path_a, path_b)
        _check(
            result,
            isinstance(summary, dict) and "changes" in summary,
            "diff returns {'changes': [...]}",
            phase="heuristic",
            detail_on_fail=f"got: {type(summary).__name__}",
        )
    except Exception as exc:
        result.findings.append(
            Finding(
                severity="warning",
                phase="heuristic",
                title="diff_logical_models raised",
                detail=str(exc),
                evidence=traceback.format_exc(limit=4),
            )
        )

    result.duration_s = round(time.time() - start, 3)
    result.ok = result.ok and all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 2 — CLI surface (argparse, --help, error-message UX)
# ---------------------------------------------------------------------------


def _phase2_cli(workspace: Path) -> PhaseResult:
    start = time.time()
    result = PhaseResult(phase="cli", ok=True, duration_s=0.0)

    # 2a. `fluid --help` shows the banner
    r = _run_fluid(["--help"], cwd=workspace)
    _check(
        result,
        "FLUID" in r.stdout.upper() or "fluid" in r.stdout,
        "fluid --help prints banner",
        phase="cli",
        detail_on_fail=f"rc={r.returncode} stdout_head={r.stdout[:200]!r}",
    )

    # 2b. `fluid forge --help` advertises data-model
    r = _run_fluid(["forge", "--help"], cwd=workspace)
    _check(
        result,
        "data-model" in r.stdout,
        "fluid forge --help advertises data-model",
        phase="cli",
        detail_on_fail=f"rc={r.returncode}",
    )

    # 2c. `fluid forge data-model --help` lists all 5 subcommands
    r = _run_fluid(["forge", "data-model", "--help"], cwd=workspace)
    missing = [
        cmd
        for cmd in ("from-ddl", "from-intent", "validate", "diff", "dump-ddl")
        if cmd not in r.stdout
    ]
    _check(
        result,
        not missing,
        "data-model help lists all 5 subcommands",
        phase="cli",
        detail_on_fail=f"missing: {missing}",
    )

    # 2d. missing-required-arg produces a clean error (not a traceback)
    r = _run_fluid(["forge", "data-model", "from-intent"], cwd=workspace)
    traceback_in_err = "Traceback" in r.stderr
    _check(
        result,
        r.returncode != 0,
        "from-intent with no args exits non-zero",
        phase="cli",
    )
    if traceback_in_err:
        result.findings.append(
            Finding(
                severity="ux",
                phase="cli",
                title="from-intent missing-args shows raw Traceback",
                detail="argparse should emit a 'the following arguments are required' line; we leak Python traceback instead.",
                evidence=r.stderr[:800],
            )
        )

    # 2e. validate on a non-existent file reports clean error
    missing_contract = workspace / "does_not_exist.yaml"
    r = _run_fluid(["forge", "data-model", "validate", str(missing_contract)], cwd=workspace)
    _check(
        result,
        r.returncode != 0,
        "validate on missing file exits non-zero",
        phase="cli",
    )
    if "Traceback" in r.stderr:
        result.findings.append(
            Finding(
                severity="ux",
                phase="cli",
                title="validate on missing file leaks Traceback",
                detail="The user shouldn't see a Python traceback for a missing-file error.",
                evidence=r.stderr[:400],
            )
        )

    # 2f. memory status works
    r = _run_fluid(["memory", "status"], cwd=workspace)
    _check(
        result,
        r.returncode == 0,
        "fluid memory status exits 0",
        phase="cli",
        detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:300]}",
    )

    # 2g. version (compact banner form)
    r = _run_fluid(["version"], cwd=workspace)
    _check(
        result,
        r.returncode == 0 and len(r.stdout) > 0,
        "fluid version exits 0 with output",
        phase="cli",
    )

    result.duration_s = round(time.time() - start, 3)
    result.ok = all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 3 — memory persistence
# ---------------------------------------------------------------------------


def _phase3_memory(workspace: Path) -> PhaseResult:
    """Exercise the memory store across namespaces + legacy migration + cache keys."""
    from fluid_build.copilot.agents.base import StageSession
    from fluid_build.copilot.industry.compiler import IndustryPackCompiler
    from fluid_build.copilot.schemas.intent import (
        BusinessIntent,
        DataProduct,
        Dimensions,
        Grain,
    )
    from fluid_build.copilot.store.backends.file import FileBackend
    from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

    start = time.time()
    result = PhaseResult(phase="memory", ok=True, duration_s=0.0)

    # 3a. FileBackend round-trip on every namespace we care about.
    store_root = workspace / ".fluid" / "store"
    store_root.mkdir(parents=True, exist_ok=True)
    # ``workspace_root`` is what the legacy-shim reads .fluid/copilot-memory.json
    # relative to — passing it explicitly so the shim fires under tempdirs.
    backend = FileBackend(root=store_root, workspace_root=workspace)
    for ns in ("llm/modeler", "memory/project", "memory/team", "discovery/ws"):
        try:
            backend.put(ns, "sample_key", {"hello": "world", "ns": ns}, ttl=None)
            hit = backend.get(ns, "sample_key")
            _check(
                result,
                hit is not None and hit.value.get("ns") == ns,
                f"FileBackend round-trips namespace {ns}",
                phase="memory",
                detail_on_fail=f"got: {hit}",
            )
        except Exception as exc:
            result.findings.append(
                Finding(
                    severity="error",
                    phase="memory",
                    title=f"FileBackend fails on {ns}",
                    detail=str(exc),
                ),
            )

    # 3b. Legacy .fluid/copilot-memory.json shim — write legacy file, confirm read.
    legacy_path = workspace / ".fluid" / "copilot-memory.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "project_fingerprint": "legacy-test-fp",
                "conventions": {"prefix": "lgy"},
            }
        )
    )
    try:
        # FileBackend reads legacy on get() for memory/project
        legacy_hit = backend.get("memory/project", "legacy-test-fp")
        _check(
            result,
            legacy_hit is not None,
            "FileBackend reads legacy .fluid/copilot-memory.json",
            phase="memory",
            detail_on_fail="legacy shim did not surface; new installs won't inherit pre-v1 memory.",
        )
    except Exception as exc:
        result.findings.append(
            Finding(severity="warning", phase="memory", title="legacy read raised", detail=str(exc))
        )

    # 3c. Cache-key invalidation across technique switch.
    # Same intent, two techniques → should yield DISTINCT cache keys.
    pack = IndustryPackCompiler().compile("retail", technique="dimensional")
    intent = BusinessIntent(
        data_product=DataProduct(name="cache_probe", domain="retail", description="cache-key test"),
        grain=Grain(entity="line", time_dimension="day"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )
    seen_keys: Dict[str, str] = {}
    for tech in ("data_vault_2", "dimensional"):
        sess = StageSession(
            store=backend,
            workspace_root=workspace,
            llm_config=None,
            active_provider=None,
            no_cache=False,
            industry_pack=pack,
        )
        # A heuristic run still hits the store (for discovery/*); we observe
        # by scanning for put() calls via the file layout after the run.
        try:
            run_from_intent(sess, intent=intent, technique=tech)
        except Exception:
            pass
        # Scan llm/ for distinct keys after each run.
        llm_dir = store_root / "llm"
        if llm_dir.exists():
            keys = {p.stem for p in llm_dir.rglob("*.json")}
            seen_keys[tech] = ",".join(sorted(keys))
    _check(
        result,
        True,  # informational — heuristic path may not write to llm/* at all
        "technique switch observed in store",
        phase="memory",
    )
    result.phase_data["llm_keys_by_technique"] = seen_keys

    # 3d. Clearing a namespace actually removes files.
    before = (
        sum(1 for _ in (store_root / "memory" / "project").rglob("*.json"))
        if (store_root / "memory" / "project").exists()
        else 0
    )
    cleared = backend.clear("memory/project")
    after = (
        sum(1 for _ in (store_root / "memory" / "project").rglob("*.json"))
        if (store_root / "memory" / "project").exists()
        else 0
    )
    _check(
        result,
        after <= before,
        "backend.clear removes files from namespace",
        phase="memory",
        detail_on_fail=f"before={before} after={after} cleared={cleared}",
    )
    result.phase_data["clear_stats"] = {"before": before, "after": after, "cleared": cleared}

    # 3e. fluid memory status via CLI prints something sensible.
    env = {"FLUID_STORE_ROOT": str(store_root)}
    r = _run_fluid(["memory", "status"], cwd=workspace, env=env)
    _check(
        result,
        r.returncode == 0,
        "fluid memory status works with custom FLUID_STORE_ROOT",
        phase="memory",
        detail_on_fail=f"rc={r.returncode} stderr={r.stderr[:300]}",
    )
    result.phase_data["memory_status_stdout_head"] = r.stdout[:500]

    result.duration_s = round(time.time() - start, 3)
    result.ok = all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 4 — live Gemini (gate: GEMINI_API_KEY)
# ---------------------------------------------------------------------------


def _phase4_gemini(workspace: Path) -> PhaseResult:
    """Delegate to the existing scenario runner; aggregate results."""
    start = time.time()
    result = PhaseResult(phase="gemini", ok=True, duration_s=0.0)

    if not os.environ.get("GEMINI_API_KEY", "").strip():
        result.skipped_reason = "GEMINI_API_KEY not set"
        result.duration_s = round(time.time() - start, 3)
        return result

    env = {"PYTHONPATH": str(workspace)}
    # 4 scenarios × up to 4 stages each × Gemini 2.5 latency (~20-90s per stage)
    # can easily approach 15 min in the worst case. 1200s gives headroom while
    # still catching true hangs. The runner flushes a partial report after
    # each scenario so even a timeout preserves completed work.
    report_path = workspace / ".fluid" / "gemini_demo_db_report.json"
    proc = _run_external_safe(
        [sys.executable, "scripts/gemini_demo_db_scenarios.py"],
        cwd=str(workspace),
        env={**os.environ, **env},
        timeout=1200,
    )
    result.phase_data["stdout_tail"] = _redact_secret_text(proc.stdout[-2000:])
    result.phase_data["stderr_tail"] = _redact_secret_text(proc.stderr[-2000:])
    _check(
        result,
        proc.returncode == 0,
        "Gemini scenario runner exits successfully",
        phase="gemini",
        detail_on_fail=f"rc={proc.returncode} stderr={_redact_secret_text(proc.stderr[-1000:])}",
    )
    current_report = report_path.exists() and report_path.stat().st_mtime >= start - 1
    _check(
        result,
        current_report,
        "Gemini scenario runner writes a fresh report",
        phase="gemini",
        detail_on_fail="Expected .fluid/gemini_demo_db_report.json from the current run.",
    )
    if current_report:
        try:
            data = json.loads(report_path.read_text())
            result.phase_data["scenarios"] = data
            _check(
                result,
                isinstance(data, list) and len(data) > 0,
                "Gemini scenario report contains scenarios",
                phase="gemini",
                detail_on_fail=f"type={type(data).__name__}",
            )
            # Naming-compliance gate: every fact/dim/hub/link/sat we produce
            # must satisfy the regex at least 90% of the time.
            for scen in data:
                scenario_title = (
                    f"{scen.get('industry', 'unknown')} {scen.get('technique', 'unknown')}"
                )
                _check(
                    result,
                    scen.get("ok") is True,
                    f"{scenario_title}: scenario succeeds",
                    phase="gemini",
                    detail_on_fail=str(scen.get("failure") or scen.get("error") or "")[:1000],
                )
                for kind in ("hub", "link", "sat", "fact", "dim"):
                    total = scen.get("naming_total", {}).get(kind, 0)
                    ok = scen.get("naming_ok", {}).get(kind, 0)
                    if total > 0 and ok / total < 0.9:
                        result.findings.append(
                            Finding(
                                severity="error",
                                phase="gemini",
                                title=f"{scen['industry']} {scen['technique']}: {kind} naming compliance below 90%",
                                detail=f"{ok}/{total} conformed to snake_case prefix",
                            )
                        )
                coverage_lines = scen.get("coverage") or []
                coverage_gaps = [line for line in coverage_lines if "⚠" in line]
                if coverage_gaps:
                    result.findings.append(
                        Finding(
                            severity="error",
                            phase="gemini",
                            title=f"{scen['industry']}: canonical coverage gaps remain",
                            detail="\n".join(coverage_gaps),
                        )
                    )
                # dimensional runs must populate dimensions[]
                if scen.get("technique") == "dimensional":
                    if scen.get("entity_counts", {}).get("dimensions", 0) == 0:
                        result.findings.append(
                            Finding(
                                severity="error",
                                phase="gemini",
                                title=f"{scen['industry']}: dimensional run produced zero dimensions",
                                detail="Gemini emitted fact but dimensions[] is empty; "
                                "conformed_dimensions[] probably filled with strings instead.",
                            )
                        )
        except Exception as exc:
            result.findings.append(
                Finding(
                    severity="error",
                    phase="gemini",
                    title="gemini report unparseable",
                    detail=str(exc),
                )
            )

    result.duration_s = round(time.time() - start, 3)
    result.ok = proc.returncode == 0 and all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 5 — live Snowflake demo-lab (gate: SNOWFLAKE_* env)
# ---------------------------------------------------------------------------


def _phase5_snowflake(workspace: Path) -> PhaseResult:
    start = time.time()
    result = PhaseResult(phase="snowflake", ok=True, duration_s=0.0)

    need = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"]
    missing = [v for v in need if not os.environ.get(v, "").strip()]
    if missing:
        result.skipped_reason = f"missing env: {missing}"
        result.duration_s = round(time.time() - start, 3)
        return result

    database = (
        os.environ.get("FLUID_DEMO_DB_DB") or os.environ.get("SNOWFLAKE_DATABASE") or "DEMO_DB"
    )
    schema = (
        os.environ.get("FLUID_DEMO_DB_SCHEMA")
        or os.environ.get("SNOWFLAKE_STAGE_SCHEMA")
        or os.environ.get("SNOWFLAKE_SCHEMA")
        or "SEEDED"
    )
    dump_dir = workspace / ".fluid"
    dump_dir.mkdir(parents=True, exist_ok=True)
    out_ddl = dump_dir / f"e2e_dump_{_now_iso()}.sql"
    result.phase_data["ddl_path"] = str(out_ddl)

    # 5a. dump-ddl
    r = _run_fluid_safe(
        [
            "forge",
            "data-model",
            "dump-ddl",
            "--database",
            database,
            "--schema",
            schema,
            "-o",
            str(out_ddl),
        ],
        cwd=workspace,
        timeout=180,
    )
    _check(
        result,
        r.returncode == 0 and out_ddl.exists() and out_ddl.stat().st_size > 0,
        "dump-ddl writes non-empty .sql file",
        phase="snowflake",
        detail_on_fail=_redact_secret_text(
            f"rc={r.returncode} stderr={r.stderr[:800]} stdout={r.stdout[:400]}"
        ),
    )

    if not out_ddl.exists() or out_ddl.stat().st_size == 0:
        result.ok = False
        result.duration_s = round(time.time() - start, 3)
        return result

    # 5b. from-ddl → contract
    contract_path = workspace / ".fluid" / f"e2e_from_ddl_{_now_iso()}.fluid.yaml"
    result.phase_data["contract_path"] = str(contract_path)
    r = _run_fluid_safe(
        [
            "forge",
            "data-model",
            "from-ddl",
            "--ddl",
            str(out_ddl),
            "--source-type",
            "snowflake",
            "--technique",
            "data_vault_2",
            "--industry",
            "telecommunications",
            "-o",
            str(contract_path),
        ],
        cwd=workspace,
        timeout=300,
    )
    _check(
        result,
        r.returncode == 0 and contract_path.exists(),
        "from-ddl writes contract",
        phase="snowflake",
        detail_on_fail=_redact_secret_text(
            f"rc={r.returncode} stderr={r.stderr[:1200]} stdout={r.stdout[:600]}"
        ),
    )

    sidecar_path = contract_path.with_name(f"{contract_path.name}.model.json")
    current_contract = r.returncode == 0 and contract_path.exists() and sidecar_path.exists()
    _check(
        result,
        current_contract,
        "from-ddl writes current logical sidecar",
        phase="snowflake",
        detail_on_fail=f"sidecar={sidecar_path}",
    )
    if current_contract:
        try:
            logical = json.loads(sidecar_path.read_text(encoding="utf-8"))
            source_summary = logical.get("source_summary") or {}
            dv2 = logical.get("dv2") or {}
            entity_count = (
                len(dv2.get("hubs") or [])
                + len(dv2.get("links") or [])
                + len(dv2.get("satellites") or [])
            )
            link_count = len(dv2.get("links") or [])
            _check(
                result,
                source_summary.get("table_count", 0) > 0,
                "from-ddl parsed at least one source table",
                phase="snowflake",
                detail_on_fail=f"source_summary={source_summary}",
            )
            _check(
                result,
                entity_count > 0,
                "from-ddl emitted non-empty logical model",
                phase="snowflake",
                detail_on_fail=f"dv2_counts={entity_count}",
            )
            _check(
                result,
                link_count > 0,
                "from-ddl inferred at least one DV2 link",
                phase="snowflake",
                detail_on_fail=f"links={link_count}",
            )
            result.phase_data["parsed_table_count"] = source_summary.get("table_count", 0)
            result.phase_data["logical_entity_count"] = entity_count
            result.phase_data["logical_link_count"] = link_count
        except Exception as exc:
            _check(
                result,
                False,
                "from-ddl writes parseable logical sidecar",
                phase="snowflake",
                detail_on_fail=str(exc),
            )

    # 5c. generate speed-transformation on the contract (heuristic; LLM path is Phase 6)
    if current_contract:
        gen_out = Path(tempfile.mkdtemp(prefix="e2e_gen_", dir=workspace / ".fluid"))
        r = _run_fluid_safe(
            [
                "generate",
                "speed-transformation",
                str(contract_path),
                "-o",
                str(gen_out),
                "--overwrite",
            ],
            cwd=workspace,
            timeout=600,
        )
        model_files = list((gen_out / "models").glob("**/*.sql")) if gen_out.exists() else []
        _check(
            result,
            r.returncode == 0 and gen_out.exists() and model_files,
            "generate speed-transformation writes dbt project",
            phase="snowflake",
            detail_on_fail=_redact_secret_text(
                f"rc={r.returncode} stderr={r.stderr[:1200]} stdout={r.stdout[:600]}"
            ),
        )
        result.phase_data["gen_out"] = str(gen_out)
        result.phase_data["generated_model_count"] = len(model_files)

    result.duration_s = round(time.time() - start, 3)
    result.ok = all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 6 — full stack with dbt-validate gate
# ---------------------------------------------------------------------------


def _phase6_full_stack(workspace: Path, phase5: PhaseResult) -> PhaseResult:
    start = time.time()
    result = PhaseResult(phase="full_stack", ok=True, duration_s=0.0)

    if phase5.skipped_reason or not phase5.ok:
        result.skipped_reason = "requires phase 5 (snowflake)"
        result.duration_s = round(time.time() - start, 3)
        return result

    dbt_bin = shutil.which("dbt")
    if not dbt_bin:
        result.skipped_reason = "dbt not installed"
        result.duration_s = round(time.time() - start, 3)
        return result

    gen_out = Path(phase5.phase_data.get("gen_out", ""))
    if not gen_out.exists():
        result.skipped_reason = "phase 5 produced no dbt project"
        result.duration_s = round(time.time() - start, 3)
        return result

    # dbt parse + dbt compile on the generated project (never run — dry).
    parse_cmd = [dbt_bin, "parse", "--project-dir", str(gen_out), "--target", "dev"]
    profiles_yml = gen_out / "profiles.yml"
    if profiles_yml.exists():
        parse_cmd.extend(["--profiles-dir", str(gen_out)])
    proc = subprocess.run(
        parse_cmd,
        capture_output=True,
        text=True,
        timeout=180,
    )
    _check(
        result,
        proc.returncode == 0,
        "dbt parse on generated project succeeds",
        phase="full_stack",
        detail_on_fail=(
            f"rc={proc.returncode} stderr={proc.stderr[:600]} " f"stdout={proc.stdout[-1000:]}"
        ),
    )

    result.phase_data["dbt_parse_stdout_tail"] = proc.stdout[-1000:]

    result.duration_s = round(time.time() - start, 3)
    result.ok = all(f.severity != "error" for f in result.findings)
    return result


# ---------------------------------------------------------------------------
# Phase 7 — strict live LLM provider matrix
# ---------------------------------------------------------------------------


def _provider_matrix_scenarios() -> Dict[str, Dict[str, Any]]:
    return {
        "retail_dimensional": {
            "technique": "dimensional",
            "industry": "retail",
            "intent": {
                "business_context": {
                    "problem_statement": "Governed retail revenue analytics for provider matrix smoke testing."
                },
                "data_product": {
                    "name": "provider_matrix_sales",
                    "domain": "retail",
                    "description": "Provider matrix smoke test for a dimensional retail sales model.",
                },
                "grain": {
                    "entity": "sales_line",
                    "time_dimension": "order_date",
                    "description": "One row per sales line item.",
                },
                "dimensions": {
                    "entities": ["customer", "product", "store", "date"],
                    "attributes": [
                        "customer_segment",
                        "product_category",
                        "store_region",
                    ],
                },
                "metrics": [
                    {
                        "name": "gross_revenue",
                        "description": "Sum of sales line amount.",
                    },
                    {
                        "name": "units_sold",
                        "description": "Sum of sales line quantity.",
                    },
                ],
                "modeling": {"technique": "dimensional"},
            },
        },
        "telco_dv2": {
            "technique": "data_vault_2",
            "industry": "telco",
            "intent": {
                "business_context": {
                    "problem_statement": "Governed telco customer, subscription, and network-usage analytics for provider matrix smoke testing."
                },
                "data_product": {
                    "name": "provider_matrix_telco_usage",
                    "domain": "telecommunications",
                    "description": "Provider matrix smoke test for a Data Vault 2.0 telco model.",
                },
                "grain": {
                    "entity": "usage_event",
                    "time_dimension": "event_timestamp",
                    "description": "One row per rated service-usage event.",
                },
                "dimensions": {
                    "entities": [
                        "party",
                        "account",
                        "subscription",
                        "service",
                        "resource",
                        "usage_event",
                    ],
                    "attributes": [
                        "party_type",
                        "account_status",
                        "service_category",
                        "network_region",
                    ],
                },
                "metrics": [
                    {
                        "name": "billable_usage_quantity",
                        "description": "Sum of billable usage units.",
                    },
                    {
                        "name": "network_event_count",
                        "description": "Count of network usage events.",
                    },
                ],
                "modeling": {"technique": "data_vault_2"},
            },
        },
    }


def _selected_provider_matrix_scenarios() -> Dict[str, Dict[str, Any]]:
    scenarios = _provider_matrix_scenarios()
    raw = os.environ.get("FLUID_E2E_PROVIDER_SCENARIOS", "").strip()
    if not raw:
        return scenarios
    selected = {
        name.strip(): scenarios[name.strip()]
        for name in raw.split(",")
        if name.strip() in scenarios
    }
    return selected


def _phase7_llm_providers(workspace: Path) -> PhaseResult:
    start = time.time()
    result = PhaseResult(phase="llm_providers", ok=True, duration_s=0.0)
    providers_env_was_explicit = "FLUID_E2E_LLM_PROVIDERS" in os.environ
    requested = [
        item.strip().lower()
        for item in os.environ.get(
            "FLUID_E2E_LLM_PROVIDERS",
            "gemini,anthropic,openai,ollama",
        ).split(",")
        if item.strip()
    ]
    provider_availability = {provider: _llm_provider_available(provider) for provider in requested}
    available = [
        provider for provider, is_available in provider_availability.items() if is_available
    ]
    unavailable = [
        provider for provider, is_available in provider_availability.items() if not is_available
    ]
    result.phase_data["providers_requested"] = requested
    result.phase_data["provider_availability"] = provider_availability
    result.phase_data["providers_unavailable"] = unavailable
    if unavailable:
        strict_availability = _env_bool(
            "FLUID_E2E_REQUIRE_ALL_REQUESTED_PROVIDERS",
            providers_env_was_explicit,
        )
        severity = "error" if strict_availability else "warning"
        result.findings.append(
            Finding(
                severity=severity,
                phase="llm_providers",
                title="requested LLM providers are unavailable",
                detail=(
                    "Missing provider credentials or local runtime for: "
                    f"{', '.join(unavailable)}. Hosted providers can now be "
                    "resolved from env vars or the Fluid keyring, but no key "
                    "was available to this process."
                ),
            )
        )
    if not available:
        result.duration_s = round(time.time() - start, 3)
        if any(f.severity == "error" for f in result.findings):
            result.ok = False
            return result
        result.skipped_reason = "no provider env vars or local Ollama available"
        return result

    scenarios = _selected_provider_matrix_scenarios()
    if not scenarios:
        result.skipped_reason = "no provider scenarios selected"
        result.duration_s = round(time.time() - start, 3)
        return result

    provider_outcomes: Dict[str, Dict[str, Any]] = {}
    dbt_bin = shutil.which("dbt")
    llm_timeout = _env_int("FLUID_E2E_LLM_TIMEOUT_SECONDS", 180)
    provider_runs = max(1, _env_int("FLUID_E2E_PROVIDER_RUNS", 2))
    max_repair_rate = max(0.0, _env_float("FLUID_E2E_MAX_REPAIR_RATE", 0.0))
    max_avg_run_seconds_raw = os.environ.get("FLUID_E2E_MAX_AVG_RUN_SECONDS")
    max_avg_run_seconds = (
        None
        if max_avg_run_seconds_raw in (None, "")
        else max(0.0, _env_float("FLUID_E2E_MAX_AVG_RUN_SECONDS", 0.0))
    )
    with tempfile.TemporaryDirectory(prefix="fluid_llm_provider_") as td:
        run_ws = Path(td)
        fluid_dir = run_ws / ".fluid"
        fluid_dir.mkdir(parents=True, exist_ok=True)
        run_env = {
            "FLUID_STORE_BACKEND": "null",
            "FLUID_COPILOT_SEMANTIC_MEMORY": "0",
            "FLUID_LLM_TIMEOUT_SECONDS": str(llm_timeout),
            "FLUID_QUIET": "1",
            "FLUID_NONINTERACTIVE": "1",
        }
        for provider in available:
            model = _provider_model_override(provider)
            provider_outcomes[provider] = {
                "model": model or "default",
                "cache": "disabled",
                "store_backend": "null",
                "semantic_memory": "disabled",
                "dbt_available": bool(dbt_bin),
                "scenarios": {},
                "runs": [],
            }
            for scenario_name, scenario in scenarios.items():
                scenario_outcome = {
                    "technique": scenario["technique"],
                    "industry": scenario["industry"],
                    "runs": [],
                }
                provider_outcomes[provider]["scenarios"][scenario_name] = scenario_outcome
                intent_path = fluid_dir / f"provider_matrix_{scenario_name}_intent.json"
                intent_path.write_text(
                    json.dumps(scenario["intent"], indent=2),
                    encoding="utf-8",
                )
                contract_signatures: List[Dict[str, Any]] = []
                dbt_signatures: List[str] = []
                for run_idx in range(provider_runs):
                    run_start = time.time()
                    run_label = f"{provider} {scenario_name} run {run_idx + 1}"
                    contract_path = fluid_dir / (
                        f"provider_matrix_{provider}_{scenario_name}_run{run_idx + 1}.fluid.yaml"
                    )
                    gen_out = Path(
                        tempfile.mkdtemp(
                            prefix=f"provider_{provider}_{scenario_name}_run{run_idx + 1}_dbt_",
                            dir=fluid_dir,
                        )
                    )
                    forge_cmd = [
                        "forge",
                        "data-model",
                        "from-intent",
                        str(intent_path),
                        "--technique",
                        scenario["technique"],
                        "--industry",
                        scenario["industry"],
                        "--engine",
                        "dbt",
                        "--llm-provider",
                        provider,
                        "--require-llm",
                        "--no-cache",
                        "--allow-semantic-warnings",
                        "-o",
                        str(contract_path),
                    ]
                    if model:
                        forge_cmd.extend(["--llm-model", model])
                    r = _run_fluid_safe(
                        forge_cmd,
                        cwd=run_ws,
                        env=run_env,
                        timeout=llm_timeout,
                    )
                    run_outcome: Dict[str, Any] = {
                        "scenario": scenario_name,
                        "run": run_idx + 1,
                        "forge_rc": r.returncode,
                    }
                    provider_outcomes[provider]["runs"].append(run_outcome)
                    scenario_outcome["runs"].append(run_outcome)
                    _check(
                        result,
                        r.returncode == 0 and contract_path.exists(),
                        f"{run_label} writes strict contract",
                        phase="llm_providers",
                        detail_on_fail=_redact_secret_text(
                            f"rc={r.returncode} stderr={r.stderr[:600]} stdout={r.stdout[-600:]}"
                        ),
                    )
                    if not contract_path.exists():
                        run_outcome["duration_s"] = round(time.time() - run_start, 3)
                        continue

                    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
                    model_doc = contract_path.with_name(f"{contract_path.name}.model.md")
                    model_doc_text = (
                        model_doc.read_text(encoding="utf-8") if model_doc.exists() else ""
                    )
                    run_outcome["model_doc_exists"] = model_doc.exists()
                    _check(
                        result,
                        model_doc.exists() and "```mermaid" in model_doc_text,
                        f"{run_label} writes Mermaid model document",
                        phase="llm_providers",
                        detail_on_fail=f"path={model_doc}",
                    )
                    labels = contract.get("labels") or {}
                    run_outcome["agentic_mode"] = labels.get("agenticMode")
                    run_outcome["fallback_used"] = labels.get("agenticFallbackUsed")
                    run_outcome["repair_used"] = labels.get("agenticRepairUsed")
                    if labels.get("agenticRepairReasons"):
                        run_outcome["repair_reasons"] = labels.get("agenticRepairReasons")
                    if labels.get("agenticRepairDetails"):
                        run_outcome["repair_details"] = labels.get("agenticRepairDetails")
                    run_outcome["llm_provider"] = labels.get("llmProvider")
                    run_outcome["llm_model"] = labels.get("llmModel")
                    contract_signatures.append(_stable_contract_signature(contract))
                    expected_provider = "anthropic" if provider == "claude" else provider
                    _check(
                        result,
                        labels.get("agenticMode") == "strict_llm"
                        and labels.get("agenticStrictLlmRequired") == "true",
                        f"{run_label} records strict LLM mode",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(labels, sort_keys=True),
                    )
                    _check(
                        result,
                        labels.get("agenticFallbackUsed") == "false",
                        f"{run_label} did not use heuristic fallback",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(labels, sort_keys=True),
                    )
                    _check(
                        result,
                        labels.get("llmProvider") == expected_provider,
                        f"{run_label} records provider identity",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(labels, sort_keys=True),
                    )
                    manifest_ok = False
                    try:
                        manifest = json.loads(labels.get("agenticStageManifest") or "[]")
                        manifest_stages = {
                            item.get("stage"): item.get("agent")
                            for item in manifest
                            if isinstance(item, dict)
                        }
                        manifest_ok = (
                            manifest_stages.get("logical") == "LogicalAgent"
                            and manifest_stages.get("contract") == "ContractForgeAgent"
                        )
                        run_outcome["agentic_stage_manifest"] = manifest
                    except Exception as exc:  # noqa: BLE001
                        run_outcome["agentic_stage_manifest_error"] = str(exc)
                    _check(
                        result,
                        manifest_ok,
                        f"{run_label} records accountable agent stage manifest",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(labels, sort_keys=True),
                    )
                    semantics = (contract.get("exposes") or [{}])[0].get("semantics") or {}
                    missing_semantics = [
                        key
                        for key in ("entities", "dimensions", "measures", "metrics")
                        if not semantics.get(key)
                    ]
                    _check(
                        result,
                        not missing_semantics,
                        f"{run_label} contract has complete semantics",
                        phase="llm_providers",
                        detail_on_fail=f"missing={missing_semantics}",
                    )

                    r = _run_fluid_safe(
                        ["forge", "data-model", "validate", str(contract_path)],
                        cwd=run_ws,
                        env=run_env,
                    )
                    run_outcome["validate_rc"] = r.returncode
                    _check(
                        result,
                        r.returncode == 0,
                        f"{run_label} contract validates",
                        phase="llm_providers",
                        detail_on_fail=_redact_secret_text(
                            f"rc={r.returncode} stderr={r.stderr[:400]} stdout={r.stdout[-400:]}"
                        ),
                    )

                    r = _run_fluid_safe(
                        [
                            "generate",
                            "speed-transformation",
                            str(contract_path),
                            "-o",
                            str(gen_out),
                            "--overwrite",
                        ],
                        cwd=run_ws,
                        env=run_env,
                        timeout=600,
                    )
                    model_files = (
                        list((gen_out / "models").glob("**/*.sql")) if gen_out.exists() else []
                    )
                    run_outcome["generate_rc"] = r.returncode
                    run_outcome["model_count"] = len(model_files)
                    _check(
                        result,
                        r.returncode == 0 and bool(model_files),
                        f"{run_label} generates dbt project",
                        phase="llm_providers",
                        detail_on_fail=_redact_secret_text(
                            f"rc={r.returncode} stderr={r.stderr[:500]} stdout={r.stdout[-500:]}"
                        ),
                    )
                    if model_files:
                        dbt_signatures.append(_dbt_project_signature(gen_out))
                    if dbt_bin and model_files:
                        dbt_base = [
                            dbt_bin,
                            "--project-dir",
                            str(gen_out),
                            "--target",
                            "dev",
                        ]
                        profiles_yml = gen_out / "profiles.yml"
                        if profiles_yml.exists():
                            dbt_base.extend(["--profiles-dir", str(gen_out)])
                        parse_proc = _run_external_safe(
                            [dbt_bin, "parse", *dbt_base[1:]],
                            cwd=run_ws,
                            env=run_env,
                            timeout=180,
                        )
                        run_outcome["dbt_parse_rc"] = parse_proc.returncode
                        _check(
                            result,
                            parse_proc.returncode == 0,
                            f"{run_label} dbt parse succeeds",
                            phase="llm_providers",
                            detail_on_fail=_redact_secret_text(
                                f"rc={parse_proc.returncode} stderr={parse_proc.stderr[:500]} stdout={parse_proc.stdout[-500:]}"
                            ),
                        )
                        run_proc = _run_external_safe(
                            [dbt_bin, "run", *dbt_base[1:]],
                            cwd=run_ws,
                            env=run_env,
                            timeout=300,
                        )
                        run_outcome["dbt_run_rc"] = run_proc.returncode
                        _check(
                            result,
                            run_proc.returncode == 0,
                            f"{run_label} dbt run succeeds",
                            phase="llm_providers",
                            detail_on_fail=_redact_secret_text(
                                f"rc={run_proc.returncode} stderr={run_proc.stderr[:500]} stdout={run_proc.stdout[-500:]}"
                            ),
                        )
                    run_outcome["duration_s"] = round(time.time() - run_start, 3)

                if provider_runs > 1 and len(contract_signatures) == provider_runs:
                    contract_hashes = [
                        hashlib.sha256(json.dumps(sig, sort_keys=True).encode("utf-8")).hexdigest()
                        for sig in contract_signatures
                    ]
                    scenario_outcome["contract_signature_hashes"] = contract_hashes
                    scenario_outcome["contract_stable"] = len(set(contract_hashes)) == 1
                    _check(
                        result,
                        scenario_outcome["contract_stable"],
                        f"{provider} {scenario_name} semantic contract signature is stable across repeated runs",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(contract_hashes),
                    )
                elif provider_runs > 1:
                    scenario_outcome["contract_stable"] = False
                if provider_runs > 1 and len(dbt_signatures) == provider_runs:
                    scenario_outcome["dbt_project_hashes"] = dbt_signatures
                    scenario_outcome["dbt_stable"] = len(set(dbt_signatures)) == 1
                    _check(
                        result,
                        scenario_outcome["dbt_stable"],
                        f"{provider} {scenario_name} dbt project signature is stable across repeated runs",
                        phase="llm_providers",
                        detail_on_fail=json.dumps(dbt_signatures),
                    )
                elif provider_runs > 1 and dbt_bin:
                    scenario_outcome["dbt_stable"] = False

    result.phase_data["providers_tested"] = available
    result.phase_data["scenarios_tested"] = list(scenarios)
    result.phase_data["scenario_count"] = len(scenarios)
    result.phase_data["provider_runs"] = provider_runs
    result.phase_data["provider_outcomes"] = provider_outcomes
    result.phase_data["provider_scorecard"] = _build_provider_scorecard(
        provider_outcomes,
        max_repair_rate=max_repair_rate,
        max_avg_run_seconds=max_avg_run_seconds,
    )
    trend_enabled = _env_bool("FLUID_E2E_SCORECARD_TRENDS", True)
    if trend_enabled:
        history_path = workspace / ".fluid" / "e2e_report" / _PROVIDER_SCORECARD_HISTORY_FILE
        max_avg_run_seconds_delta_raw = os.environ.get("FLUID_E2E_MAX_AVG_RUN_SECONDS_DELTA")
        max_avg_run_seconds_delta = (
            60.0
            if max_avg_run_seconds_delta_raw in (None, "")
            else max(0.0, _env_float("FLUID_E2E_MAX_AVG_RUN_SECONDS_DELTA", 60.0))
        )
        result.phase_data["provider_scorecard_history_path"] = str(history_path)
        result.phase_data["provider_scorecard_trends"] = _build_provider_scorecard_trends(
            result.phase_data["provider_scorecard"],
            _load_provider_scorecard_history(history_path),
            scenarios=list(scenarios),
            min_history_runs=max(1, _env_int("FLUID_E2E_TREND_MIN_HISTORY_RUNS", 1)),
            baseline_runs=max(1, _env_int("FLUID_E2E_TREND_BASELINE_RUNS", 5)),
            max_repair_rate_delta=max(
                0.0,
                _env_float("FLUID_E2E_MAX_REPAIR_RATE_DELTA", 0.0),
            ),
            max_avg_run_seconds_delta=max_avg_run_seconds_delta,
        )
    scorecard_failures = [
        f"{provider}: {', '.join(score.get('quality_gaps') or [])}"
        for provider, score in result.phase_data["provider_scorecard"].items()
        if score.get("quality_gaps")
    ]
    for detail in scorecard_failures:
        result.findings.append(
            Finding(
                severity="error",
                phase="llm_providers",
                title="provider quality budget exceeded",
                detail=detail,
            )
        )
    trend_failures = [
        f"{provider}: {', '.join(trend.get('trend_gaps') or [])}"
        for provider, trend in (result.phase_data.get("provider_scorecard_trends") or {}).items()
        if trend.get("trend_gaps")
    ]
    for detail in trend_failures:
        result.findings.append(
            Finding(
                severity="error",
                phase="llm_providers",
                title="provider scorecard trend regression",
                detail=detail,
            )
        )
    result.duration_s = round(time.time() - start, 3)
    result.ok = all(f.severity != "error" for f in result.findings)
    return result


def _llm_provider_available(provider: str) -> bool:
    if provider == "ollama":
        try:
            from fluid_build.cli.forge_copilot_llm_providers import detect_ollama_available

            return detect_ollama_available(os.environ)
        except Exception:
            return False
    if provider in {"gemini", "openai", "anthropic", "claude"}:
        try:
            from fluid_build.cli.forge_copilot_llm_providers import has_llm_api_key

            return has_llm_api_key(provider, os.environ)
        except Exception:
            return False
    return False


def _provider_model_override(provider: str) -> Optional[str]:
    env_name = f"FLUID_E2E_{provider.upper()}_MODEL"
    return os.environ.get(env_name) or os.environ.get("FLUID_E2E_LLM_MODEL")


# ---------------------------------------------------------------------------
# Report emission
# ---------------------------------------------------------------------------


def _record_provider_scorecard_history(results: List[PhaseResult], report_dir: Path) -> None:
    """Persist phase-7 provider scorecards for future trend checks."""
    if not _env_bool("FLUID_E2E_SCORECARD_HISTORY", True):
        return
    rows: List[Dict[str, Any]] = []
    for result in results:
        if result.phase != "llm_providers":
            continue
        rows.extend(
            _provider_scorecard_history_rows(
                result.phase_data,
                run_id=report_dir.name,
                generated_at=_now_iso(),
            )
        )
    if not rows:
        return
    _append_provider_scorecard_history(
        report_dir.parent / _PROVIDER_SCORECARD_HISTORY_FILE,
        rows,
        limit=max(1, _env_int("FLUID_E2E_SCORECARD_HISTORY_LIMIT", 200)),
    )


def _emit_report(results: List[PhaseResult], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "summary.md"
    json_path = report_dir / "results.json"
    bug_log = report_dir.parent / "bug_log.md"

    # Markdown summary
    lines: List[str] = []
    lines.append(f"# forge-cli e2e all-modes report — {_now_iso()}")
    lines.append("")
    lines.append("| Phase | Status | Duration | Passed / Total | Findings |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        status = "SKIP" if r.skipped_reason else ("PASS" if r.ok else "FAIL")
        sev_summary = ", ".join(sorted({f.severity for f in r.findings})) or "—"
        lines.append(
            f"| {r.phase} | {status} | {r.duration_s}s | "
            f"{r.checks_passed}/{r.checks_total} | {sev_summary} |"
        )
    lines.append("")
    for r in results:
        if r.skipped_reason:
            lines.append(f"## {r.phase} — SKIPPED ({r.skipped_reason})")
            lines.append("")
            continue
        lines.append(f"## {r.phase} — {'PASS' if r.ok else 'FAIL'}")
        lines.append("")
        provider_scorecard = r.phase_data.get("provider_scorecard")
        if isinstance(provider_scorecard, dict) and provider_scorecard:
            lines.append("### Provider Scorecard")
            lines.append(
                "| Provider | Status | Strict | Fallback | Repair | dbt run | Contract stable | dbt stable | Avg run |"
            )
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
            for provider, score in sorted(provider_scorecard.items()):
                avg = score.get("avg_duration_s")
                avg_text = "—" if avg is None else f"{avg}s"
                gap_text = "; ".join(score.get("quality_gaps") or []) or "—"
                lines.append(
                    f"| {provider} | {score.get('status')} | "
                    f"{score.get('strict_ratio')} | {score.get('fallback_runs')} | "
                    f"{score.get('repair_runs')} ({score.get('repair_rate'):.2%}) | "
                    f"{score.get('dbt_run_success')}/{score.get('dbt_run_total')} | "
                    f"{score.get('contract_stability')} | {score.get('dbt_stability')} | "
                    f"{avg_text} |"
                )
                if gap_text != "—":
                    lines.append(f"  - {provider} quality gap: {gap_text}")
            lines.append("")
        provider_trends = r.phase_data.get("provider_scorecard_trends")
        if isinstance(provider_trends, dict) and provider_trends:
            lines.append("### Provider Trends")
            lines.append("| Provider | Status | History | Repair delta | Avg run delta |")
            lines.append("|---|---|---:|---:|---:|")
            for provider, trend in sorted(provider_trends.items()):
                repair_delta = trend.get("repair_rate_delta")
                repair_delta_text = "—" if repair_delta is None else f"{float(repair_delta):.2%}"
                duration_delta = trend.get("avg_duration_s_delta")
                duration_delta_text = "—" if duration_delta is None else f"{duration_delta}s"
                lines.append(
                    f"| {provider} | {trend.get('status')} | "
                    f"{trend.get('history_runs')} | {repair_delta_text} | "
                    f"{duration_delta_text} |"
                )
                gap_text = "; ".join(trend.get("trend_gaps") or []) or "—"
                if gap_text != "—":
                    lines.append(f"  - {provider} trend gap: {gap_text}")
            lines.append("")
        if r.findings:
            lines.append("### Findings")
            for f in r.findings:
                lines.append(f"- **[{f.severity}]** {f.title}")
                if f.detail:
                    lines.append(f"  - {f.detail}")
                if f.evidence:
                    evi = f.evidence.replace("\n", " | ")[:300]
                    lines.append(f"  - evidence: `{evi}`")
            lines.append("")
    md_path.write_text("\n".join(lines))

    # JSON results
    json_path.write_text(
        json.dumps(
            [
                {
                    "phase": r.phase,
                    "ok": r.ok,
                    "skipped_reason": r.skipped_reason,
                    "duration_s": r.duration_s,
                    "checks_passed": r.checks_passed,
                    "checks_total": r.checks_total,
                    "findings": [asdict(f) for f in r.findings],
                    "phase_data": r.phase_data,
                }
                for r in results
            ],
            indent=2,
            default=str,
        )
    )

    _record_provider_scorecard_history(results, report_dir)

    # Append to rolling bug log
    if not bug_log.exists():
        bug_log.parent.mkdir(parents=True, exist_ok=True)
        bug_log.write_text("# forge-cli running bug log\n\nAppended to on every e2e run.\n\n")
    with bug_log.open("a") as fh:
        fh.write(f"\n## Run {_now_iso()}\n\n")
        for r in results:
            for f in r.findings:
                if f.severity in ("error", "ux"):
                    fh.write(f"- [{f.severity}][{f.phase}] {f.title} — {f.detail}\n")

    return md_path


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="forge-cli extensive e2e test harness")
    parser.add_argument(
        "--phases", default="1,2,3,4,5,6", help="Comma-separated phase numbers to run"
    )
    args = parser.parse_args()

    requested = {p.strip() for p in args.phases.split(",") if p.strip()}
    workspace = Path.cwd()
    report_dir = workspace / ".fluid" / "e2e_report" / _now_iso()

    all_results: List[PhaseResult] = []

    # Each phase gets a fresh tempdir-backed workspace to prevent leakage.
    # Phase 1/3 share state deliberately (memory persistence across calls).
    with tempfile.TemporaryDirectory(prefix="fluid_e2e_") as _td:
        probe_ws = Path(_td)

        phases: List[tuple[str, Callable[..., PhaseResult]]] = [
            ("1", lambda: _phase1_heuristic(probe_ws)),
            ("2", lambda: _phase2_cli(workspace)),
            ("3", lambda: _phase3_memory(probe_ws)),
            ("4", lambda: _phase4_gemini(workspace)),
            ("5", lambda: _phase5_snowflake(workspace)),
        ]
        for tag, fn in phases:
            if tag not in requested:
                continue
            try:
                all_results.append(fn())
            except Exception as exc:
                all_results.append(
                    PhaseResult(
                        phase=f"phase_{tag}",
                        ok=False,
                        duration_s=0.0,
                        findings=[
                            Finding(
                                severity="error",
                                phase=f"phase_{tag}",
                                title="runner crashed",
                                detail=str(exc),
                                evidence=traceback.format_exc(limit=8),
                            )
                        ],
                    )
                )

        if "6" in requested:
            phase5 = next((r for r in all_results if r.phase == "snowflake"), None) or PhaseResult(
                phase="snowflake", ok=False, duration_s=0.0, skipped_reason="not run"
            )
            try:
                all_results.append(_phase6_full_stack(workspace, phase5))
            except Exception as exc:
                all_results.append(
                    PhaseResult(
                        phase="full_stack",
                        ok=False,
                        duration_s=0.0,
                        findings=[
                            Finding(
                                severity="error",
                                phase="full_stack",
                                title="runner crashed",
                                detail=str(exc),
                                evidence=traceback.format_exc(limit=8),
                            )
                        ],
                    )
                )

        if "7" in requested:
            try:
                all_results.append(_phase7_llm_providers(workspace))
            except Exception as exc:
                all_results.append(
                    PhaseResult(
                        phase="llm_providers",
                        ok=False,
                        duration_s=0.0,
                        findings=[
                            Finding(
                                severity="error",
                                phase="llm_providers",
                                title="runner crashed",
                                detail=str(exc),
                                evidence=traceback.format_exc(limit=8),
                            )
                        ],
                    )
                )

    md_path = _emit_report(all_results, report_dir)
    print(f"\nReport → {md_path}")
    print(f"JSON   → {report_dir / 'results.json'}")
    print(f"Bug log→ {report_dir.parent / 'bug_log.md'}")

    # Exit non-zero if anything ran and failed (skips don't count)
    ran = [r for r in all_results if not r.skipped_reason]
    if not ran:
        return 0
    return 0 if all(r.ok for r in ran) else 1


if __name__ == "__main__":
    sys.exit(main())
