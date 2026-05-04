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
CLI command to generate Airflow DAGs from FLUID contracts.

Usage:
    fluid generate-airflow contract.yaml -o dags/my_dag.py

For contracts WITH an orchestration section, delegates to 'fluid export --engine airflow'.
For contracts WITHOUT orchestration (builds-only), uses the legacy DAG generator
which infers tasks from provider actions and builds.
"""

import argparse
import logging
from pathlib import Path

from fluid_build.cli.console import cprint

from ..runtimes.airflow_provider_actions import generate_airflow_dag
from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info, warn

COMMAND = "generate-airflow"


def register(subparsers: argparse._SubParsersAction):
    """Register the generate-airflow command."""
    p = subparsers.add_parser(
        COMMAND,
        help="Generate Airflow DAG from FLUID contract (0.7.0+)",
        description="""
        Generate Airflow DAG Python file from FLUID contract provider actions.
        
        This command reads a FLUID contract and generates an Airflow DAG that
        implements the orchestration workflow defined in providerActions.
        
        Supports both explicit provider actions (0.7.0+) and inferred actions
        from v0.7.x contracts.
        
        If the contract has an 'orchestration' section, delegates to 
        'fluid export --engine airflow' for richer multi-engine support.
        """,
        epilog="""
Examples:
  # Generate DAG from contract
  fluid generate-airflow contract.fluid.yaml
  
  # Specify output path
  fluid generate-airflow contract.yaml -o dags/sales_pipeline.py
  
  # Override DAG ID and schedule
  fluid generate-airflow contract.yaml --dag-id my_custom_dag --schedule "0 2 * * *"
  
  # Generate from a specific contract
  fluid generate-airflow contract.fluid.yaml -o dags/customer360.py
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    p.add_argument("contract", help="Path to FLUID contract file (YAML or JSON)")

    # Optional arguments
    p.add_argument("--output", "-o", help="Output path for generated DAG file (default: stdout)")

    p.add_argument("--dag-id", help="Override DAG ID (default: derived from contract.id)")

    p.add_argument(
        "--schedule",
        help="Override schedule interval (default: from contract.orchestration.schedule or @daily)",
    )

    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")

    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Execute Airflow DAG generation."""
    warn(
        logger,
        "Note: 'generate-airflow' is deprecated. "
        "Use 'fluid generate schedule --scheduler airflow' instead.",
    )
    try:
        # Load contract
        contract_path = Path(args.contract)
        if not contract_path.exists():
            raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

        if args.verbose:
            info(logger, f"Loading contract from {contract_path}")

        contract = load_contract_with_overlay(args.contract, args.env, logger)

        # If contract has orchestration section, delegate to export command
        if contract.get("orchestration"):
            warn(
                logger,
                "⚠️  'generate-airflow' is deprecated for orchestration contracts. "
                "Use 'fluid export --engine airflow' instead.",
            )
            from . import export as export_module

            output_dir = str(Path(args.output).parent) if args.output else "."

            export_args = argparse.Namespace(
                contract=args.contract,
                engine="airflow",
                output_dir=output_dir,
                provider=None,
                env=args.env,
                verbose=args.verbose,
                cmd="export",
            )

            result = export_module.run(export_args, logger)

            # If custom output filename specified, rename the generated file
            if result == 0 and args.output:
                output_path = Path(args.output)
                output_dir_path = output_path.parent
                generated_files = list(output_dir_path.glob("*_dag.py"))
                if generated_files and generated_files[0] != output_path:
                    import shutil

                    shutil.move(str(generated_files[0]), str(output_path))
                    if args.verbose:
                        info(logger, f"Renamed to: {output_path}")

            return result

        # No orchestration section — use legacy DAG generator (builds/providerActions)
        output_path = None
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.verbose:
            info(logger, "Generating Airflow DAG from builds/providerActions...")

        dag_code = generate_airflow_dag(
            contract=contract,
            output_path=str(output_path) if output_path else None,
            dag_id=args.dag_id,
            schedule=args.schedule,
            logger=logger,
        )

        if output_path:
            info(logger, f"✅ DAG written to: {output_path}")
            if args.verbose:
                contract_id = contract.get("id", "unknown")
                info(logger, f"   Contract ID: {contract_id}")
                info(logger, f"   DAG ID: {args.dag_id or contract_id.replace('.', '_')}")
                if args.schedule:
                    info(logger, f"   Schedule: {args.schedule}")
        else:
            cprint(dag_code)

        return 0

    except CLIError as e:
        error(logger, f"❌ {e.event}: {e.context}")
        return e.exit_code
    except Exception as e:
        error(logger, f"❌ Error generating DAG: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1
