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

"""Single golden-path E2E runner for agentic data-model forge.

One command runs the whole product pipeline instead of scattered smoke
fragments::

    forge (contract) -> validate -> plan -> apply --dry-run
      -> generate dbt -> dbt parse -> dbt run

It runs the pipeline in two lanes:

* ``deterministic`` — the no-LLM heuristic forge path
  (``forge data-model from-intent --deterministic``). Always runs: no API
  key, no network, no cloud. This is the regression floor the rest of the
  tech-debt wave rides on.
* ``strict-llm`` — the strict, no-fallback LLM forge path
  (``--llm-provider <p> --require-llm``). Skipped cleanly when no LLM API
  key is in the environment, mirroring the live-LLM integration tests.

Safety: every run uses an isolated temp workspace, the null store
(``FLUID_STORE_BACKEND=null``), disabled semantic memory
(``FLUID_COPILOT_SEMANTIC_MEMORY=0``), and cache off by default. ``apply``
only ever runs ``--dry-run`` — zero warehouse DDL, zero cloud calls.

The pipeline is executed ``--repeat`` times (default 2) and the runner
**fails loudly** (non-zero exit + failure evidence in the JSON report) on:

* any required stage exiting non-zero (invalid contract, unrunnable dbt),
* drift — the normalized contract hash or dbt-project hash differing
  across repeated runs,
* fallback — the strict-LLM lane silently degrading to the heuristic path.

It emits a machine-readable JSON report (``--json-out``) carrying, per
phase: exit code, duration, contract hash, dbt-project hash, model counts,
fallback status, provider/model, and failure evidence.

Borrow receipts (see PR body): mirrors the in-repo smoke-script
conventions (``scripts/smoke_a1.py``, ``scripts/smoke_phase_6b.py``) and
adapts the stable-signature hashing helpers from
``scripts/e2e_all_modes.py``. The compare-and-fail-on-drift shape is the
dbt/"Golden Master" snapshot-testing pattern; hashing artifacts into the
report (instead of storing full output) follows the agentsnap approach.

Usage::

    # Deterministic lane only (no key, no network) — the CI floor:
    python scripts/golden_path_e2e.py --lane deterministic \
        --json-out /tmp/golden.json

    # Both lanes (strict-llm auto-skips without a key):
    python scripts/golden_path_e2e.py --lane both --json-out /tmp/golden.json

    # Point at an installed console script instead of ``python -m``:
    FLUID_BIN=/path/to/.venv/bin/fluid python scripts/golden_path_e2e.py

Exit codes: ``0`` all executed lanes passed; ``1`` a lane failed
(drift / fallback / bad stage); ``2`` a preflight / usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
RUNNER_NAME = "golden_path_e2e"

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose values are run-local / cosmetic and must not perturb the
# stable contract signature used for drift detection. Mirrors
# ``scripts/e2e_all_modes.py::_stable_contract_signature``.
_VOLATILE_KEYS = {
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

# Env names that may hold secrets; redacted from any captured evidence.
_SECRET_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

# Retail dimensional intent — the same proven shape the existing e2e
# harness exercises, kept small so the pipeline stays fast.
DETERMINISTIC_INTENT: Dict[str, Any] = {
    "business_context": {
        "problem_statement": "Governed retail sales analytics for the golden-path E2E runner."
    },
    "data_product": {
        "name": "sales_analytics",
        "domain": "retail",
        "description": "Retail sales analytics golden-path product.",
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
}


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Outcome of a single subprocess invocation."""

    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fluid_argv() -> List[str]:
    """Return the base argv for invoking the CLI.

    Honours ``FLUID_BIN`` (space-split, e.g. ``/abs/.venv/bin/fluid``) so an
    installed console script can be exercised; otherwise routes through
    ``python -m fluid_build`` so a broken console-script install can't mask a
    real regression.
    """
    raw = os.environ.get("FLUID_BIN", "").strip()
    if raw:
        return raw.split()
    return [sys.executable, "-m", "fluid_build"]


def _subprocess_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build the child env: inherit, force the safe/deterministic knobs, put
    the interpreter's ``bin`` dir on PATH so a venv-installed ``dbt`` console
    script is discoverable even when PATH wasn't otherwise set up.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(_REPO_ROOT))
    bin_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = os.pathsep.join([bin_dir, env.get("PATH", "")])
    # Safe + hermetic defaults for every stage.
    env["FLUID_STORE_BACKEND"] = "null"
    env["FLUID_COPILOT_SEMANTIC_MEMORY"] = "0"
    env["FLUID_QUIET"] = "1"
    env["FLUID_NONINTERACTIVE"] = "1"
    if extra:
        env.update(extra)
    return env


def _dbt_bin() -> Optional[str]:
    """Locate ``dbt``, including a venv ``bin`` dir next to the interpreter."""
    bin_dir = str(Path(sys.executable).resolve().parent)
    path = os.pathsep.join([bin_dir, os.environ.get("PATH", "")])
    return shutil.which("dbt", path=path)


def _run(
    argv: List[str],
    *,
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 300,
) -> RunResult:
    start = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env or _subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return RunResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=round(time.time() - start, 3),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return RunResult(
            argv=argv,
            returncode=124,
            stdout=out,
            stderr=f"{err}\nTIMEOUT after {timeout}s",
            duration_s=round(time.time() - start, 3),
        )


def _redact(text: str) -> str:
    redacted = text
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if value and len(value) >= 8:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


def _evidence(result: RunResult, *, lines: int = 40) -> str:
    """Redacted tail of combined output for a failed stage."""
    tail = "\n".join(result.combined.splitlines()[-lines:])
    return _redact(tail)


def _sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_contract_signature(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic behavior signature for a forged contract.

    Compares the exposed semantics + physical schema without tripping on
    expected run-local metadata (provenance, file names, descriptions).
    Adapted from ``scripts/e2e_all_modes.py::_stable_contract_signature``.
    """

    def stable_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): stable_value(val)
                for key, val in sorted(value.items(), key=lambda kv: str(kv[0]))
                if str(key) not in _VOLATILE_KEYS
            }
        if isinstance(value, list):
            return [stable_value(item) for item in value]
        return value

    exposes = contract.get("exposes")
    expose = exposes[0] if isinstance(exposes, list) and exposes else {}
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
    """Hash generated dbt files with run-local absolute paths normalized.

    Adapted from ``scripts/e2e_all_modes.py::_dbt_project_signature``. Only
    source-of-truth files are hashed (``target/`` build output and the
    duckdb file produced by ``dbt run`` are excluded), so the signature is
    stable whether or not ``dbt run`` has executed.
    """
    tracked: List[Tuple[str, str]] = []
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
        if rel.split("/", 1)[0] not in source_roots:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in sorted(path_tokens, key=len, reverse=True):
            text = text.replace(token, "<PROJECT_DIR>")
        tracked.append((rel, text))
    payload = json.dumps(tracked, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_contract(path: Path) -> Dict[str, Any]:
    import yaml  # local import: keeps the module import-light

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Phase model
# ---------------------------------------------------------------------------

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


def _phase(
    status: str,
    *,
    exit_code: Optional[int] = None,
    duration_s: Optional[float] = None,
    skipped_reason: Optional[str] = None,
    evidence: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status}
    if exit_code is not None:
        out["exit_code"] = exit_code
    if duration_s is not None:
        out["duration_s"] = duration_s
    if skipped_reason is not None:
        out["skipped_reason"] = skipped_reason
    if evidence is not None:
        out["evidence"] = evidence
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Provider resolution for the strict-LLM lane
# ---------------------------------------------------------------------------


def resolve_strict_provider(
    *, provider_override: Optional[str] = None, model_override: Optional[str] = None
) -> Optional[Tuple[str, Optional[str]]]:
    """Return ``(provider, model)`` for the strict lane, or ``None`` to skip.

    Skips (returns ``None``) when no LLM API key is present, mirroring
    ``tests/integration/test_mcp_output_port_live_llm.py::_resolve_provider``.
    """
    if provider_override:
        return provider_override, model_override
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", model_override or "claude-haiku-4-5-20251001"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai", model_override or "gpt-4o-mini"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini", model_override or "gemini-2.5-flash"
    return None


# ---------------------------------------------------------------------------
# One pipeline iteration
# ---------------------------------------------------------------------------


def _run_forge_stage(
    *,
    lane: str,
    intent_path: Path,
    contract_path: Path,
    workspace: Path,
    env: Dict[str, str],
    provider: Optional[str],
    model: Optional[str],
    timeout: int,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Run the forge stage; return ``(phase, contract_dict|None)``."""
    argv = [
        *_fluid_argv(),
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
        "--no-cache",
        "--allow-semantic-warnings",
        "-o",
        str(contract_path),
    ]
    if lane == "deterministic":
        argv.append("--deterministic")
    else:
        argv += ["--llm-provider", provider or "", "--require-llm"]
        if model:
            argv += ["--llm-model", model]

    result = _run(argv, cwd=workspace, env=env, timeout=timeout)
    if result.returncode != 0 or not contract_path.exists():
        return (
            _phase(
                FAIL,
                exit_code=result.returncode,
                duration_s=result.duration_s,
                evidence=_evidence(result),
            ),
            None,
        )

    contract = _load_contract(contract_path)
    labels = contract.get("labels") or {}
    signature = _stable_contract_signature(contract)
    contract_hash = _sha256_json(signature)
    raw_sha = "sha256:" + hashlib.sha256(contract_path.read_bytes()).hexdigest()
    agentic_mode = labels.get("agenticMode")
    fallback_used = str(labels.get("agenticFallbackUsed", "")).lower() == "true"
    phase = _phase(
        PASS,
        exit_code=0,
        duration_s=result.duration_s,
        contract_hash=contract_hash,
        contract_raw_sha256=raw_sha,
        agentic_mode=agentic_mode,
        fallback_used=fallback_used,
        provider=labels.get("llmProvider"),
        model=labels.get("llmModel"),
        model_doc=contract_path.with_name(f"{contract_path.name}.model.md").exists(),
    )
    # In the strict lane a silent fallback is a loud failure.
    if lane == "strict-llm" and (fallback_used or agentic_mode != "strict_llm"):
        phase["status"] = FAIL
        phase["evidence"] = (
            f"strict-llm lane degraded: agentic_mode={agentic_mode!r} "
            f"fallback_used={fallback_used}"
        )
    return phase, contract


def run_iteration(
    *,
    lane: str,
    index: int,
    intent_path: Path,
    workspace: Path,
    env: Dict[str, str],
    provider: Optional[str],
    model: Optional[str],
    timeout: int,
    run_dbt_run: bool,
) -> Dict[str, Any]:
    """Run forge -> validate -> plan -> apply --dry-run -> generate dbt ->
    dbt parse -> dbt run once; return the per-iteration record.
    """
    it_dir = workspace / f"iter_{index}"
    it_dir.mkdir(parents=True, exist_ok=True)
    contract_path = it_dir / "contract.fluid.yaml"
    plan_path = it_dir / "plan.json"
    dbt_out = it_dir / "dbt_out"
    phases: Dict[str, Any] = {}
    record: Dict[str, Any] = {"iteration": index, "phases": phases}

    def _stop(reason_phase: str) -> Dict[str, Any]:
        # Downstream phases we never reached are recorded as skipped.
        for name in (
            "forge",
            "validate",
            "plan",
            "apply_dry_run",
            "generate_dbt",
            "dbt_parse",
            "dbt_run",
        ):
            phases.setdefault(
                name,
                _phase(SKIPPED, skipped_reason=f"upstream phase '{reason_phase}' failed"),
            )
        return record

    # 1. forge
    forge_phase, contract = _run_forge_stage(
        lane=lane,
        intent_path=intent_path,
        contract_path=contract_path,
        workspace=it_dir,
        env=env,
        provider=provider,
        model=model,
        timeout=timeout,
    )
    phases["forge"] = forge_phase
    if forge_phase["status"] != PASS or contract is None:
        return _stop("forge")
    record["contract_hash"] = forge_phase["contract_hash"]
    record["agentic_mode"] = forge_phase.get("agentic_mode")
    record["fallback_used"] = forge_phase.get("fallback_used")
    record["provider"] = forge_phase.get("provider")
    record["model"] = forge_phase.get("model")

    # 2. validate
    r = _run(
        [*_fluid_argv(), "validate", str(contract_path)],
        cwd=it_dir,
        env=env,
        timeout=120,
    )
    phases["validate"] = _phase(
        PASS if r.returncode == 0 else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if r.returncode == 0 else _evidence(r),
    )
    if r.returncode != 0:
        return _stop("validate")

    # 3. plan
    r = _run(
        [*_fluid_argv(), "plan", str(contract_path), "--out", str(plan_path)],
        cwd=it_dir,
        env=env,
        timeout=180,
    )
    plan_extra: Dict[str, Any] = {}
    if r.returncode == 0 and plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_extra["plan_digest"] = plan.get("planDigest")
            plan_extra["bundle_digest"] = plan.get("bundleDigest")
            plan_extra["action_count"] = len(plan.get("actions") or [])
        except Exception as exc:  # noqa: BLE001
            plan_extra["parse_error"] = str(exc)
    phases["plan"] = _phase(
        PASS if r.returncode == 0 and plan_path.exists() else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if r.returncode == 0 else _evidence(r),
        **plan_extra,
    )
    if phases["plan"]["status"] != PASS:
        return _stop("plan")

    # 4. apply --dry-run  (never mutates anything)
    r = _run(
        [*_fluid_argv(), "apply", str(plan_path), "--dry-run", "--yes"],
        cwd=it_dir,
        env=env,
        timeout=180,
    )
    phases["apply_dry_run"] = _phase(
        PASS if r.returncode == 0 else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if r.returncode == 0 else _evidence(r),
    )
    if r.returncode != 0:
        return _stop("apply_dry_run")

    # 5. generate dbt project
    r = _run(
        [
            *_fluid_argv(),
            "generate",
            "speed-transformation",
            str(contract_path),
            "-o",
            str(dbt_out),
            "--overwrite",
        ],
        cwd=it_dir,
        env=env,
        timeout=300,
    )
    project_files = list(dbt_out.rglob("dbt_project.yml")) if dbt_out.exists() else []
    model_files = list(dbt_out.rglob("models/**/*.sql")) if dbt_out.exists() else []
    gen_ok = r.returncode == 0 and bool(project_files) and bool(model_files)
    project_dir = project_files[0].parent if project_files else None
    dbt_hash = _dbt_project_signature(project_dir) if project_dir else None
    phases["generate_dbt"] = _phase(
        PASS if gen_ok else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if gen_ok else _evidence(r),
        model_count=len(model_files),
        dbt_project_hash=dbt_hash,
        project_dir_found=bool(project_dir),
    )
    if not gen_ok or project_dir is None:
        return _stop("generate_dbt")
    record["dbt_project_hash"] = dbt_hash
    record["model_count"] = len(model_files)

    # 6. dbt parse (the floor — optional only when dbt is not installed)
    dbt_bin = _dbt_bin()
    if dbt_bin is None:
        phases["dbt_parse"] = _phase(SKIPPED, skipped_reason="dbt not installed (not on PATH)")
        phases["dbt_run"] = _phase(SKIPPED, skipped_reason="dbt not installed (not on PATH)")
        return record

    parse_cmd = [dbt_bin, "parse", "--project-dir", str(project_dir)]
    if (project_dir / "profiles.yml").exists():
        parse_cmd += ["--profiles-dir", str(project_dir)]
    r = _run(parse_cmd, cwd=project_dir, env=env, timeout=240)
    phases["dbt_parse"] = _phase(
        PASS if r.returncode == 0 else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if r.returncode == 0 else _evidence(r),
    )
    if r.returncode != 0:
        return _stop("dbt_parse")

    # 7. dbt run (optional; degrades to skipped when opted out)
    if not run_dbt_run:
        phases["dbt_run"] = _phase(SKIPPED, skipped_reason="dbt run disabled (--no-dbt-run)")
        return record
    run_cmd = [dbt_bin, "run", "--project-dir", str(project_dir), "--target", "dev"]
    if (project_dir / "profiles.yml").exists():
        run_cmd += ["--profiles-dir", str(project_dir)]
    r = _run(run_cmd, cwd=project_dir, env=env, timeout=300)
    models_run = _count_dbt_run_models(r.stdout)
    phases["dbt_run"] = _phase(
        PASS if r.returncode == 0 else FAIL,
        exit_code=r.returncode,
        duration_s=r.duration_s,
        evidence=None if r.returncode == 0 else _evidence(r),
        models_run=models_run,
    )
    return record


def _count_dbt_run_models(stdout: str) -> Optional[int]:
    """Parse the ``PASS=N`` count from dbt's run summary line."""
    for line in reversed(stdout.splitlines()):
        if "PASS=" in line:
            for token in line.split():
                if token.startswith("PASS="):
                    try:
                        return int(token.split("=", 1)[1])
                    except ValueError:
                        return None
    return None


# ---------------------------------------------------------------------------
# Lane driver (repeat + drift + fallback gates)
# ---------------------------------------------------------------------------


def run_lane(
    *,
    lane: str,
    workspace: Path,
    repeat: int,
    timeout: int,
    run_dbt_run: bool,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    provider: Optional[str] = None
    model: Optional[str] = None
    if lane == "strict-llm":
        resolved = resolve_strict_provider(
            provider_override=provider_override, model_override=model_override
        )
        if resolved is None:
            return {
                "lane": lane,
                "status": SKIPPED,
                "skipped_reason": (
                    "no LLM API key in env; set ANTHROPIC_API_KEY, OPENAI_API_KEY "
                    "or GEMINI_API_KEY to exercise the strict-llm lane"
                ),
            }
        provider, model = resolved

    lane_ws = workspace / lane
    lane_ws.mkdir(parents=True, exist_ok=True)
    intent_path = lane_ws / "intent.json"
    intent_path.write_text(json.dumps(DETERMINISTIC_INTENT, indent=2), encoding="utf-8")
    env = _subprocess_env()

    iterations: List[Dict[str, Any]] = []
    for idx in range(1, repeat + 1):
        iterations.append(
            run_iteration(
                lane=lane,
                index=idx,
                intent_path=intent_path,
                workspace=lane_ws,
                env=env,
                provider=provider,
                model=model,
                timeout=timeout,
                run_dbt_run=run_dbt_run,
            )
        )

    failure_evidence: List[Dict[str, Any]] = []
    for it in iterations:
        for name, phase in it["phases"].items():
            if phase.get("status") == FAIL:
                failure_evidence.append(
                    {
                        "iteration": it["iteration"],
                        "phase": name,
                        "exit_code": phase.get("exit_code"),
                        "evidence": phase.get("evidence"),
                    }
                )

    # Drift gate — normalized hashes must be identical across iterations.
    contract_hashes = [it.get("contract_hash") for it in iterations if it.get("contract_hash")]
    dbt_hashes = [it.get("dbt_project_hash") for it in iterations if it.get("dbt_project_hash")]
    contract_stable = len(set(contract_hashes)) <= 1 and len(contract_hashes) == len(iterations)
    dbt_stable = len(set(dbt_hashes)) <= 1 and len(dbt_hashes) == len(iterations)
    drift = {
        "contract_hash_stable": contract_stable,
        "dbt_project_hash_stable": dbt_stable,
        "contract_hashes": contract_hashes,
        "dbt_project_hashes": dbt_hashes,
    }
    if repeat > 1 and not contract_stable:
        failure_evidence.append(
            {"phase": "drift", "detail": f"contract hash drift across runs: {contract_hashes}"}
        )
    if repeat > 1 and not dbt_stable:
        failure_evidence.append(
            {"phase": "drift", "detail": f"dbt-project hash drift across runs: {dbt_hashes}"}
        )

    first = iterations[0] if iterations else {}
    status = FAIL if failure_evidence else PASS
    default_provider = "heuristic" if lane == "deterministic" else None
    return {
        "lane": lane,
        "status": status,
        "provider": first.get("provider") or provider or default_provider,
        "model": first.get("model") or model,
        "agentic_mode": first.get("agentic_mode"),
        "fallback_used": first.get("fallback_used"),
        "repeat": repeat,
        "iterations": iterations,
        "drift": drift,
        "failure_evidence": failure_evidence,
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_golden_path(
    *,
    lanes: List[str],
    repeat: int,
    timeout: int,
    run_dbt_run: bool,
    keep_workspace: bool,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix="fluid-golden-"))
    lane_reports: Dict[str, Any] = {}
    try:
        for lane in lanes:
            lane_reports[lane] = run_lane(
                lane=lane,
                workspace=workspace,
                repeat=repeat,
                timeout=timeout,
                run_dbt_run=run_dbt_run,
                provider_override=provider_override,
                model_override=model_override,
            )
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

    executed = [r for r in lane_reports.values() if r.get("status") != SKIPPED]
    if executed and all(r.get("status") == PASS for r in executed):
        overall = PASS
    elif any(r.get("status") == FAIL for r in lane_reports.values()):
        overall = FAIL
    else:
        overall = SKIPPED
    return {
        "schema_version": SCHEMA_VERSION,
        "runner": RUNNER_NAME,
        "generated_at": _now_iso(),
        "repeat": repeat,
        "dbt_installed": _dbt_bin() is not None,
        "run_dbt_run": run_dbt_run,
        "workspace_kept": str(workspace) if keep_workspace else None,
        "overall_status": overall,
        "lanes": lane_reports,
    }


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def _print_summary(report: Dict[str, Any]) -> None:
    print("── golden-path E2E ──", flush=True)
    print(f"  repeat={report['repeat']}  dbt_installed={report['dbt_installed']}", flush=True)
    for lane, r in report["lanes"].items():
        if r.get("status") == SKIPPED:
            print(f"  [{lane}] SKIPPED — {r.get('skipped_reason')}", flush=True)
            continue
        mode = r.get("agentic_mode")
        prov = r.get("provider")
        print(
            f"  [{lane}] {r['status'].upper()}  provider={prov} mode={mode} "
            f"fallback={r.get('fallback_used')}",
            flush=True,
        )
        for it in r.get("iterations", []):
            stages = " ".join(
                f"{name}:{phase.get('status')}" for name, phase in it["phases"].items()
            )
            print(f"      iter {it['iteration']}: {stages}", flush=True)
        drift = r.get("drift", {})
        print(
            f"      drift: contract_stable={drift.get('contract_hash_stable')} "
            f"dbt_stable={drift.get('dbt_project_hash_stable')}",
            flush=True,
        )
        for ev in r.get("failure_evidence", []):
            detail = ev.get("detail") or ev.get("evidence")
            print(f"      FAIL {ev.get('phase')}: {detail}", flush=True)
    print(f"  overall_status={report['overall_status']}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="golden_path_e2e",
        description="Single golden-path E2E runner for agentic data-model forge.",
    )
    p.add_argument(
        "--lane",
        choices=["deterministic", "strict-llm", "both"],
        default="both",
        help="Which lane(s) to run (default: both; strict-llm auto-skips without a key).",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=2,
        help="Times to run each lane's pipeline for the drift gate (default: 2).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-stage subprocess timeout in seconds (default: 300).",
    )
    p.add_argument(
        "--no-dbt-run",
        action="store_true",
        help="Skip the `dbt run` stage even when dbt is installed (parse stays the floor).",
    )
    p.add_argument("--provider", help="Strict-lane LLM provider override (e.g. anthropic).")
    p.add_argument("--model", help="Strict-lane LLM model override.")
    p.add_argument("--json-out", help="Write the machine-readable JSON report to this path.")
    p.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Do not delete the temp workspace (debugging).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.repeat < 1:
        print("preflight error: --repeat must be >= 1", file=sys.stderr)
        return 2
    lanes = ["deterministic", "strict-llm"] if args.lane == "both" else [args.lane]

    report = run_golden_path(
        lanes=lanes,
        repeat=args.repeat,
        timeout=args.timeout,
        run_dbt_run=not args.no_dbt_run,
        keep_workspace=args.keep_workspace,
        provider_override=args.provider,
        model_override=args.model,
    )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    _print_summary(report)
    print("\n── machine-readable report ──", flush=True)
    print(json.dumps(report, indent=2), flush=True)

    # Exit non-zero only when an executed lane actually failed. An all-skipped
    # run (e.g. strict-llm with no key) is not a failure of the runner.
    return 1 if report["overall_status"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
