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

"""
CLI command to export FLUID contracts as orchestration DAGs/pipelines.

Universal export for Airflow, Dagster, Prefect, and other orchestration engines.

Usage:
    fluid export contract.yaml --engine airflow -o dags/
    fluid export contract.yaml --engine dagster -o pipelines/
    fluid export contract.yaml --engine prefect -o flows/
"""

import argparse
import logging
from pathlib import Path

from ._common import CLIError, build_provider, load_contract_with_overlay
from ._logging import error, info

COMMAND = "export"


def register(subparsers: argparse._SubParsersAction):
    """Register the export command."""
    p = subparsers.add_parser(
        COMMAND,
        help="Export FLUID contract as orchestration code (DAG/pipeline/flow)",
        description="""
        Export FLUID contract as executable orchestration code.
        
        Generates ready-to-run code for orchestration engines:
        - Airflow/MWAA: Python DAG files
        - Dagster: Pipeline definitions with resources
        - Prefect: Flow definitions with deployments
        - Step Functions: State machine definitions (future)
        
        The export command uses the provider's export() method to generate
        engine-specific code from orchestration.tasks in the contract.
        """,
        epilog="""
Examples:
  # Export as Airflow DAG
  fluid export contract.yaml --engine airflow -o dags/
  
  # Export as Dagster pipeline
  fluid export contract.yaml --engine dagster -o pipelines/
  
  # Export as Prefect flow
  fluid export contract.yaml --engine prefect -o flows/
  
  # Export with provider override
  fluid export contract.yaml --provider aws --engine airflow -o dags/
  
  # Export with environment overlay
  fluid export contract.yaml --engine airflow --env prod -o dags/prod/
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    p.add_argument("contract", help="Path to FLUID contract file (YAML or JSON)")

    # Export options
    p.add_argument(
        "--engine",
        choices=["airflow", "mwaa", "dagster", "prefect"],
        default="airflow",
        help="Orchestration engine (default: airflow)",
    )

    p.add_argument(
        "--output-dir",
        "-o",
        default=".",
        help="Output directory for generated files (default: current directory)",
    )

    # Provider override
    p.add_argument(
        "--provider",
        choices=["aws", "gcp", "azure", "snowflake"],
        help="Override provider (default: auto-detect from contract.provider)",
    )

    # Environment and customization
    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")

    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Execute orchestration code export."""
    try:
        # Load contract
        contract_path = Path(args.contract)
        if not contract_path.exists():
            raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

        if args.verbose:
            info(logger, f"Loading contract from {contract_path}")

        contract = load_contract_with_overlay(args.contract, args.env, logger)

        # Validate orchestration section
        orchestration = contract.get("orchestration")
        if not orchestration:
            raise CLIError(
                1,
                "missing_orchestration",
                {
                    "message": "Contract missing orchestration section - cannot export DAG",
                    "hint": "Add orchestration.tasks to your contract",
                },
            )

        # Determine provider
        provider_name = args.provider
        if not provider_name:
            # Auto-detect from contract
            provider_name = contract.get("provider", "aws")
            if args.verbose:
                info(logger, f"Auto-detected provider: {provider_name}")

        # Get provider instance
        provider = build_provider(
            provider_name, getattr(args, "project", None), getattr(args, "region", None), logger
        )

        if not hasattr(provider, "export"):
            raise CLIError(
                1,
                "export_not_supported",
                {
                    "provider": provider_name,
                    "message": f"Provider '{provider_name}' does not support export() method",
                    "hint": "Supported providers: AWS, GCP, Snowflake",
                },
            )

        # Prepare output directory
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Export DAG
        if args.verbose:
            info(logger, f"Exporting {args.engine} code...")

        output_path = provider.export(
            contract=contract, engine=args.engine, output_dir=str(output_dir)
        )

        # Success
        info(logger, f"✅ {args.engine.capitalize()} code exported to: {output_path}")

        if args.verbose:
            # Show file stats
            output_file = Path(output_path)
            if output_file.exists():
                file_size = output_file.stat().st_size
                with open(output_file) as f:
                    line_count = len(f.readlines())

                info(logger, f"   Contract ID: {contract.get('id', 'unknown')}")
                info(logger, f"   Engine: {args.engine}")
                info(logger, f"   Lines: {line_count}")
                info(logger, f"   Size: {file_size} bytes")

                # Show next steps
                _print_next_steps(args.engine, output_path, logger)

        return 0

    except CLIError as e:
        error(logger, f"❌ {e.event}: {e.context}")
        return e.exit_code
    except Exception as e:
        error(logger, f"❌ Error exporting orchestration code: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def _print_next_steps(engine: str, output_path: str, logger: logging.Logger):
    """Print next steps for deploying the generated code."""
    info(logger, "\n📝 Next steps:")

    if engine in ["airflow", "mwaa"]:
        info(logger, "   1. Review the generated DAG file")
        info(logger, "   2. Install Airflow providers: pip install apache-airflow-providers-amazon")
        info(logger, "   3. Copy DAG to Airflow dags/ folder or MWAA S3 bucket")
        info(logger, "   4. DAG will be auto-discovered by Airflow/MWAA")

    elif engine == "dagster":
        info(logger, "   1. Review the generated pipeline file")
        info(logger, "   2. Install Dagster: pip install dagster dagster-aws")
        info(logger, "   3. Run: dagster dev -f " + output_path)
        info(logger, "   4. Access Dagster UI at http://localhost:3000")

    elif engine == "prefect":
        info(logger, "   1. Review the generated flow file")
        info(logger, "   2. Install Prefect: pip install prefect prefect-aws")
        info(logger, "   3. Start Prefect server: prefect server start")
        info(logger, "   4. Deploy flow: python " + output_path)
        info(logger, "   5. Start agent: prefect agent start -q default")
