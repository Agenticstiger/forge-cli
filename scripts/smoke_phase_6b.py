#!/usr/bin/env python3
"""Phase-6B scoped smoke against real Snowflake.

Validates the three Phase-6B surfaces (plan-binding digests, apply mode
matrix default path, data-loss gate) on the dev venv before Phase 7
piles on more commits.

SAFETY:
  - Every ``fluid apply`` runs --dry-run. Zero warehouse DDL.
  - Checks 5 + 6 use FLUID_ENV=smoketest (not prod) so the gate fires on
    the ``env != dev`` branch without loading a prod overlay.

USAGE:
  cd ~/path/to/snowflake-biz-lab
  ./scripts/smoke_phase_6b.py
  CONTRACT=orders.fluid.yaml ./scripts/smoke_phase_6b.py
  FLUID_BIN=/abs/path/fluid ./scripts/smoke_phase_6b.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

FLUID_BIN = os.environ.get("FLUID_BIN", ".venv.fluid-dev/bin/fluid")
CONTRACT = os.environ.get("CONTRACT", "contract.fluid.yaml")
ENV_DEV = os.environ.get("ENV_DEV", "dev")
ENV_NONDEV = os.environ.get("ENV_NONDEV", "smoketest")  # deliberately != dev


def _run(argv: List[str], timeout: int = 120) -> Tuple[int, str]:
    """Run a subprocess, return (exit_code, combined_output)."""
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, f"TIMEOUT after {timeout}s: {exc}"


def _check(
    name: str,
    expected_rc: int,
    argv: List[str],
    results: dict,
) -> None:
    print(f"\n[{name}] {' '.join(argv)}", flush=True)
    rc, out = _run(argv)
    if rc == expected_rc:
        results[name] = "pass"
        print(f"  PASS (exit {rc})", flush=True)
    else:
        results[name] = f"fail (expected exit {expected_rc}, got {rc})"
        print(f"  FAIL (expected exit {expected_rc}, got {rc})", flush=True)
        # Capture last 30 lines of output for the paste-back.
        tail = "\n".join(out.splitlines()[-30:])
        print(f"  ── output tail ──\n{tail}\n  ──────────────", flush=True)


def main() -> int:
    # Pre-flight
    if not os.access(FLUID_BIN, os.X_OK):
        print(f"pre-flight FAIL: FLUID_BIN not executable: {FLUID_BIN}")
        return 2
    if not os.path.isfile(CONTRACT):
        print(f"pre-flight FAIL: CONTRACT not found: {CONTRACT}")
        return 2

    print(f"Phase-6B smoke")
    print(f"  FLUID_BIN   = {FLUID_BIN}")
    print(f"  CONTRACT    = {CONTRACT}")
    print(f"  ENV_DEV     = {ENV_DEV}")
    print(f"  ENV_NONDEV  = {ENV_NONDEV} (!= dev, triggers gate)")

    with tempfile.TemporaryDirectory(prefix="fluid-smoke-") as tmpdir:
        plan_path = os.path.join(tmpdir, "plan.json")
        tampered_path = os.path.join(tmpdir, "plan-tampered.json")
        results: dict = {}

        # [1/6] plan emits digest fields
        _check(
            "1_plan_emits_digests",
            0,
            [FLUID_BIN, "plan", CONTRACT, "--out", plan_path, "--env", ENV_DEV],
            results,
        )

        # Secondary assertion for check 1
        if results.get("1_plan_emits_digests") == "pass":
            try:
                plan = json.load(open(plan_path))
                pd = plan.get("planDigest", "")
                bd = plan.get("bundleDigest", None)
                ok = (
                    isinstance(pd, str)
                    and pd.startswith("sha256:")
                    and len(pd) == 71
                    and bd is not None
                )
                if not ok:
                    results["1_plan_emits_digests"] = (
                        f"fail (digest assertion: planDigest={pd!r} bundleDigest={bd!r})"
                    )
                    print(
                        f"  OVERRIDE FAIL: {results['1_plan_emits_digests']}",
                        flush=True,
                    )
            except Exception as exc:
                results["1_plan_emits_digests"] = f"fail (plan.json unreadable: {exc})"
                print(f"  OVERRIDE FAIL: {results['1_plan_emits_digests']}", flush=True)

        # [2/6] default apply dry-run
        _check(
            "2_default_apply_dry_run",
            0,
            [
                FLUID_BIN,
                "apply",
                CONTRACT,
                "--env",
                ENV_DEV,
                "--yes",
                "--dry-run",
            ],
            results,
        )

        # [3/6] plan-binding verify — happy path
        if results.get("1_plan_emits_digests") == "pass":
            _check(
                "3_plan_binding_happy",
                0,
                [
                    FLUID_BIN,
                    "apply",
                    plan_path,
                    "--env",
                    ENV_DEV,
                    "--yes",
                    "--dry-run",
                ],
                results,
            )
        else:
            results["3_plan_binding_happy"] = "skipped (plan step failed)"
            print("\n[3_plan_binding_happy] SKIPPED — plan step failed", flush=True)

        # [4/6] plan-binding verify — tamper caught
        if results.get("1_plan_emits_digests") == "pass":
            plan = json.load(open(plan_path))
            plan.setdefault("actions", []).append(
                {
                    "step": 99,
                    "action_id": "smoke_evil_drop",
                    "action_type": "drop_table",
                    "provider": "snowflake",
                    "params": {"table": "customers"},
                    "depends_on": [],
                }
            )
            json.dump(plan, open(tampered_path, "w"))
            _check(
                "4_plan_binding_tamper_caught",
                1,  # must exit 1 with event apply_plan_digest_plan_tamper
                [
                    FLUID_BIN,
                    "apply",
                    tampered_path,
                    "--env",
                    ENV_DEV,
                    "--yes",
                    "--dry-run",
                ],
                results,
            )
        else:
            results["4_plan_binding_tamper_caught"] = "skipped (plan step failed)"
            print(
                "\n[4_plan_binding_tamper_caught] SKIPPED — plan step failed",
                flush=True,
            )

        # [5/6] data-loss gate blocks
        _check(
            "5_data_loss_gate_blocks",
            1,  # gate must block with exit 1
            [
                FLUID_BIN,
                "apply",
                CONTRACT,
                "--mode",
                "replace",
                "--env",
                ENV_NONDEV,
                "--yes",
                "--dry-run",
            ],
            results,
        )

        # [6/6] waiver lets it through
        _check(
            "6_data_loss_waiver_works",
            0,
            [
                FLUID_BIN,
                "apply",
                CONTRACT,
                "--mode",
                "replace",
                "--env",
                ENV_NONDEV,
                "--allow-data-loss",
                "--yes",
                "--dry-run",
            ],
            results,
        )

    # Final results block
    print("\n── results (paste this back) ──")
    print(json.dumps(results, indent=2))

    return 0 if all(v == "pass" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
