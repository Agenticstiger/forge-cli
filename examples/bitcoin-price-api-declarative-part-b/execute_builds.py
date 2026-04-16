#!/usr/bin/env python3
"""
Execute build jobs from FLUID contract.
Reads execution.trigger configuration and runs the specified script.
"""

import subprocess
import sys
import time
from pathlib import Path

import yaml


def load_contract(contract_path):
    """Load FLUID contract from YAML."""
    with open(contract_path) as f:
        return yaml.safe_load(f)


def execute_build(contract_path):
    """Execute build jobs defined in contract."""
    contract = load_contract(contract_path)
    builds = contract.get("builds", [])

    if not builds:
        print("No builds defined in contract")
        return 0

    print(f"Found {len(builds)} build(s) in contract")
    print("=" * 80)

    total_runs = 0

    for build in builds:
        build_id = build.get("id", "unknown")
        execution = build.get("execution", {})
        trigger = execution.get("trigger", {})
        trigger_type = trigger.get("type", "manual")

        # Get script path
        repository = build.get("repository", "./")
        properties = build.get("properties", {})
        model = properties.get("model", "ingest")

        # Build path: repository contains relative path from contract
        script_path = Path(contract_path).parent / repository / f"{model}.py"

        if not script_path.exists():
            print(f"⚠️  Script not found: {script_path}")
            continue

        print(f"\n📋 Build: {build_id}")
        print(f"   Script: {script_path}")
        print(f"   Trigger: {trigger_type}")

        if trigger_type == "manual":
            iterations = trigger.get("iterations", 1)
            print(f"   Iterations: {iterations}")
            print()

            for i in range(iterations):
                print(f"🚀 Run {i+1}/{iterations}")
                print("-" * 80)

                # Run from contract directory, not script directory
                result = subprocess.run(
                    [sys.executable, str(script_path)], cwd=Path(contract_path).parent
                )

                if result.returncode != 0:
                    print(f"❌ Run {i+1} failed with exit code {result.returncode}")
                    return 1

                total_runs += 1
                print("-" * 80)

                # Sleep between iterations (except last one)
                if i < iterations - 1:
                    print("⏳ Waiting 2 seconds before next run...\n")
                    time.sleep(2)

        elif trigger_type == "schedule":
            cron = trigger.get("cron", "")
            print(f"   Cron: {cron}")
            print("   ⚠️  Scheduled execution requires Cloud Composer (paid tier)")
            print("   💡 For free tier, use trigger.type: manual with iterations")

        else:
            print(f"   ⚠️  Unknown trigger type: {trigger_type}")

    print("\n" + "=" * 80)
    print(f"✅ Completed {total_runs} execution(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python execute_builds.py <contract.yaml>")
        sys.exit(1)

    sys.exit(execute_build(sys.argv[1]))
