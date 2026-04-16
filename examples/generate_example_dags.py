#!/usr/bin/env python3
"""
Generate example DAG files for all orchestration engines.

This script demonstrates the DAG export capability by generating
Airflow, Dagster, and Prefect code from a FLUID contract.
"""

import json
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluid_build.providers.aws.provider import AwsProvider


def main():
    # Load example contract
    contract_path = "examples/contracts/financial_analytics.json"

    if not os.path.exists(contract_path):
        print(f"Error: Contract not found: {contract_path}")
        return 1

    with open(contract_path, "r") as f:
        contract = json.load(f)

    # Create provider
    provider = AwsProvider(account_id="YOUR_AWS_ACCOUNT_ID", region="us-east-1")

    # Output directory
    output_dir = "examples/generated_dags"
    os.makedirs(output_dir, exist_ok=True)

    # Generate DAGs for all engines
    engines = ["airflow", "dagster", "prefect"]

    print("\n=== Generating Example DAG Files ===\n")

    for engine in engines:
        print(f"Generating {engine.upper()} code...")

        try:
            output_file = provider.export(contract, engine=engine, output_dir=output_dir)

            # Get file size
            file_size = os.path.getsize(output_file)

            # Count lines
            with open(output_file, "r") as f:
                line_count = len(f.readlines())

            print(f"  ✓ Generated: {output_file}")
            print(f"    Lines: {line_count}, Size: {file_size} bytes\n")

        except Exception as e:
            print(f"  ✗ Failed: {e}\n")
            return 1

    print("=== Generation Complete ===\n")
    print(f"Output directory: {output_dir}/")
    print("\nGenerated files:")
    print(f"  - {contract['id']}_dag.py       (Airflow/MWAA)")
    print(f"  - {contract['id']}_pipeline.py  (Dagster)")
    print(f"  - {contract['id']}_flow.py      (Prefect)\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
