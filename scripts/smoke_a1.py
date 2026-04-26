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

"""A1 variant smoke — new 11-stage flag surface against real Snowflake.

Validates the A1 scenario (External-Reference Silver contract) with the
NEW flags we've introduced in Phases 3–6 (plus the 6C format fix):

  --mode amend        (was: no --mode; legacy amend)
  --mode amend-and-build  (was: --build <id>)
  --target datamesh-manager  (was: --catalog datamesh-manager)

SAFETY:
  Every ``fluid apply`` runs --dry-run. Every ``fluid publish`` runs
  --dry-run. Zero Snowflake DDL, zero DMM push. The goal is to exercise
  the CLI surface + plan-binding against a real lab contract — not to
  mutate anything.

USAGE:
  # Must source both launchpads first so $GREENFIELD_WORKSPACE etc resolve.
  source .../snowflake-biz-lab/runtime/generated/launchpad.local.sh
  export FLUID_DEV_BIN="$LAB_REPO/.venv.fluid-dev/bin/fluid"
  python3 /path/to/smoke_a1.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import List

FLUID_BIN = os.environ.get(
    "FLUID_BIN",
    os.path.join(
        os.environ.get("LAB_REPO", ""),
        ".venv.fluid-dev/bin/fluid",
    ),
)
GREENFIELD = os.environ.get("GREENFIELD_WORKSPACE", "")
A1_DIR = os.path.join(GREENFIELD, "variants/A1-external-reference")
CONTRACT = os.path.join(A1_DIR, "contract.fluid.yaml")
BUILD_ID = "dv2_subscriber360_reference_build"
SECRETS = os.environ.get("FLUID_SECRETS_FILE", "")


def _run(argv: List[str], cwd: str, timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, f"TIMEOUT after {timeout}s: {exc}"


def _check(name: str, expected_rc: int, argv: List[str], cwd: str, results: dict) -> None:
    print(f"\n[{name}]")
    print(f"  $ {' '.join(argv)}")
    rc, out = _run(argv, cwd)
    if rc == expected_rc:
        results[name] = "pass"
        print(f"  PASS (exit {rc})")
    else:
        results[name] = f"fail (expected exit {expected_rc}, got {rc})"
        print(f"  FAIL (expected exit {expected_rc}, got {rc})")
        tail = "\n".join(out.splitlines()[-40:])
        print(f"  ── output tail ──\n{tail}\n  ──────────────")


def main() -> int:
    # Pre-flight
    if not os.access(FLUID_BIN, os.X_OK):
        print(f"pre-flight FAIL: FLUID_BIN not executable: {FLUID_BIN}")
        return 2
    if not GREENFIELD:
        print("pre-flight FAIL: GREENFIELD_WORKSPACE not set — source launchpad.local.sh first")
        return 2
    if not os.path.isfile(CONTRACT):
        print(f"pre-flight FAIL: A1 contract not found: {CONTRACT}")
        return 2

    print("A1 smoke — new 11-stage flag surface")
    print(f"  FLUID_BIN   = {FLUID_BIN}")
    print(f"  A1_DIR      = {A1_DIR}")
    print("  CONTRACT    = contract.fluid.yaml (in A1_DIR)")
    print(f"  BUILD_ID    = {BUILD_ID}")

    results: dict = {}
    # All commands run from A1_DIR (matches playbook UX)
    cwd = A1_DIR

    # Copy Snowflake secrets into env so provider can authenticate.
    # --dry-run on apply means no actual Snowflake call, but contract
    # load + provider detection still needs credentials-shaped env.
    if SECRETS and os.path.isfile(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

    with tempfile.TemporaryDirectory(prefix="a1-smoke-") as tmpdir:
        plan_path = os.path.join(tmpdir, "plan.json")

        # [1/6] validate — existing command, unchanged by Phase 3-6.
        _check(
            "1_validate",
            0,
            [FLUID_BIN, "validate", "contract.fluid.yaml"],
            cwd,
            results,
        )

        # [2/6] plan — new: emits bundleDigest + planDigest
        _check(
            "2_plan_emits_digests",
            0,
            [
                FLUID_BIN,
                "plan",
                "contract.fluid.yaml",
                "--out",
                plan_path,
            ],
            cwd,
            results,
        )

        if results.get("2_plan_emits_digests") == "pass":
            try:
                p = json.load(open(plan_path))
                pd, bd = p.get("planDigest", ""), p.get("bundleDigest", None)
                has_full_contract = (
                    isinstance(p.get("contract"), dict)
                    and "exposes" in p["contract"]  # full contract marker
                )
                ok = (
                    pd.startswith("sha256:")
                    and len(pd) == 71
                    and bd is not None
                    and has_full_contract
                )
                if not ok:
                    results["2_plan_emits_digests"] = (
                        f"fail (plan shape: digest_ok={pd.startswith('sha256:')} "
                        f"bundle={bd!r} full_contract={has_full_contract})"
                    )
                    print(f"  OVERRIDE FAIL: {results['2_plan_emits_digests']}")
            except Exception as exc:
                results["2_plan_emits_digests"] = f"fail (plan.json parse: {exc})"
                print(f"  OVERRIDE FAIL: {results['2_plan_emits_digests']}")

        # [3/6] apply --mode amend --dry-run
        # Default amend mode (no --build, no --allow-data-loss). Dry-run
        # path exits before any Snowflake DDL.
        _check(
            "3_apply_mode_amend_dry_run",
            0,
            [
                FLUID_BIN,
                "apply",
                "contract.fluid.yaml",
                "--mode",
                "amend",
                "--env",
                "dev",
                "--yes",
                "--dry-run",
            ],
            cwd,
            results,
        )

        # [4/6] apply plan.json --dry-run (canonical stage-7 flow)
        # Exercises Phase 6C fix: plan.json (flat format, full contract
        # embedded) → apply loads it → verify digest → simple-mode dispatch.
        _check(
            "4_apply_plan_json_dry_run",
            0,
            [
                FLUID_BIN,
                "apply",
                plan_path,
                "--env",
                "dev",
                "--yes",
                "--dry-run",
            ],
            cwd,
            results,
        )

        # [5/6] publish --target (new flag) --dry-run
        # --catalog still works as deprecation alias; we test the new flag.
        _check(
            "5_publish_target_dry_run",
            0,
            [
                FLUID_BIN,
                "publish",
                "contract.fluid.yaml",
                "--target",
                "datamesh-manager",
                "--dry-run",
            ],
            cwd,
            results,
        )

        # [6/6] apply --mode replace with --allow-data-loss + dry-run
        # Exercises the mode matrix's most destructive path safely.
        # FLUID_ENV=smoketest so gate would block, --allow-data-loss waives.
        _check(
            "6_apply_mode_replace_waived_dry_run",
            0,
            [
                FLUID_BIN,
                "apply",
                "contract.fluid.yaml",
                "--mode",
                "replace",
                "--env",
                "smoketest",
                "--allow-data-loss",
                "--yes",
                "--dry-run",
            ],
            cwd,
            results,
        )

    print("\n── results (paste this back) ──")
    print(json.dumps(results, indent=2))
    return 0 if all(v == "pass" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
