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

"""Airflow DAG generation helpers — physical extraction from ``init.py``.

The DAG-emission utilities (``should_generate_dag``,
``generate_dag_for_project``, ``create_basic_dag``,
``create_dags_readme``) used to live inline in ``init.py`` (~250 LOC).
They are self-contained — no shared state with the init flow — so
they extracted cleanly into this sibling module.

``init.py`` re-imports each function at module top so existing
test patches that target ``fluid_build.cli.init.<dag_helper>`` still
resolve via the namespace.
"""

from __future__ import annotations

import logging
import os
import re
import re as _re  # alias used inside ``create_basic_dag`` to disambiguate
from pathlib import Path
from typing import Any, Dict, Optional

# ``_init`` indirection — these helpers reference ``RICH_AVAILABLE``
# (and a few other module toggles) on the original ``cli.init``
# namespace. Tests routinely
# ``patch("fluid_build.cli.init.RICH_AVAILABLE", False)`` to exercise
# the no-rich code path; resolving via attribute access at call time
# means those patches still flow through after the physical extraction.
from fluid_build.cli import init as _init  # noqa: E402
from fluid_build.cli._logging import info, warn
from fluid_build.cli.console import cprint, success


def _rich_available() -> bool:
    """Read ``RICH_AVAILABLE`` from the canonical ``cli.init`` module
    so test patches on ``fluid_build.cli.init.RICH_AVAILABLE`` flow
    through to the moved DAG helpers."""
    return getattr(_init, "RICH_AVAILABLE", False)


def should_generate_dag(contract: dict, template: str = None) -> bool:
    """
    Determine if DAG should be auto-generated for this project.

    Auto-generate DAGs when:
    1. Contract has explicit orchestration config
    2. Template is orchestration-focused (customer-360, sales-analytics, ml-features, data-quality)
    3. Project has multiple provider actions (complex pipeline)
    """
    # Check for explicit orchestration config
    if "orchestration" in contract:
        return True

    # Check for orchestration-focused templates
    orchestrated_templates = ["customer-360", "sales-analytics", "ml-features", "data-quality"]
    if template and template in orchestrated_templates:
        return True

    # Check for complex pipelines (multiple actions)
    binding = contract.get("binding", {})
    provider_actions = binding.get("providerActions", [])
    if len(provider_actions) > 1:
        return True

    return False


def generate_dag_for_project(
    project_dir: Path, contract: dict, logger, console, template: str = None
) -> bool:
    """
    Generate Airflow DAG using existing generate-airflow command.

    Creates dags/ folder with:
    - DAG Python file (contract_name_dag.py)
    - README.md with usage instructions
    """
    try:
        import subprocess

        # Get contract details
        contract_name = contract.get("name", "my_product")
        orchestration = contract.get("orchestration", {})

        # Prepare DAG parameters — sanitize to prevent injection
        schedule = orchestration.get("schedule", "@daily")
        dag_id = contract_name.replace("-", "_").replace(" ", "_")
        # Strict identifier validation: only alphanumeric + underscore
        dag_id = re.sub(r"[^a-zA-Z0-9_]", "", dag_id) or "fluid_dag"
        # Validate schedule is a plausible cron/preset string
        if not re.match(r"^[@a-zA-Z0-9_ */,-]+$", schedule):
            schedule = "@daily"

        # Call generate-airflow command
        dag_dir = project_dir / "dags"
        dag_dir.mkdir(exist_ok=True)

        # Build command
        cmd = [
            "fluid",
            "generate-airflow",
            str(project_dir / "contract.fluid.yaml"),
            "--output-dir",
            str(dag_dir),
            "--dag-id",
            dag_id,
            "--schedule",
            schedule,
        ]

        if _rich_available():
            console.print("\n[cyan]📅 Generating Airflow DAG...[/cyan]")

        # Execute command
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_dir))

        if result.returncode != 0:
            # generate-airflow may not exist yet - create DAG manually.
            # Resolve via ``_init`` so test patches on
            # ``fluid_build.cli.init.create_basic_dag`` flow through.
            logger.warning("generate-airflow command not available, creating basic DAG template")
            _init.create_basic_dag(project_dir, contract, logger)

        # Create README
        dag_filename = f"{dag_id}_dag.py"
        create_dags_readme(dag_dir, dag_id, schedule, dag_filename)

        if _rich_available():
            console.print(f"[green]✅ DAG created: dags/{dag_filename}[/green]")

        return True

    except Exception as e:
        logger.warning(f"Failed to generate DAG: {e}")
        return False


def create_basic_dag(project_dir: Path, contract: dict, logger):
    """Create a basic DAG template if generate-airflow is not available."""

    import re as _re

    contract_name = contract.get("name", "my_product")
    orchestration = contract.get("orchestration", {})
    # Sanitize values to prevent code injection in generated Python.
    dag_id = _re.sub(r"[^a-zA-Z0-9_]", "_", contract_name)[:128]
    schedule = _re.sub(r"[^a-zA-Z0-9@*/, _-]", "", orchestration.get("schedule", "@daily"))[:64]
    contract_name = _re.sub(r"[^a-zA-Z0-9 _.-]", "_", contract_name)[:128]

    dag_content = f'''"""
Airflow DAG for FLUID contract: {contract_name}
Auto-generated by fluid init
"""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.bash import BashOperator

default_args = {{
    'owner': 'fluid',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': {orchestration.get("retries", 3)},
    'retry_delay': timedelta(minutes={orchestration.get("retry_delay", "5m").replace("m", "")}),
}}

with DAG(
    dag_id='{dag_id}',
    default_args=default_args,
    description='FLUID data product: {contract_name}',
    schedule_interval='{schedule}',
    catchup=False,
    tags=['fluid', 'data-product'],
) as dag:

    # Validate contract
    validate = BashOperator(
        task_id='validate_contract',
        bash_command='cd {project_dir.absolute()} && fluid validate contract.fluid.yaml',
    )

    # Plan execution
    plan = BashOperator(
        task_id='plan_execution',
        bash_command='cd {project_dir.absolute()} && fluid plan contract.fluid.yaml',
    )

    # Apply changes
    apply = BashOperator(
        task_id='apply_contract',
        bash_command='cd {project_dir.absolute()} && fluid apply contract.fluid.yaml --auto-approve',
    )

    validate >> plan >> apply
'''

    dag_dir = project_dir / "dags"
    dag_dir.mkdir(exist_ok=True)
    dag_file = dag_dir / f"{dag_id}_dag.py"

    with open(dag_file, "w") as f:
        f.write(dag_content)

    logger.info(f"Created basic DAG template: {dag_file}")


def create_dags_readme(dag_dir: Path, dag_id: str, schedule: str, dag_filename: str):
    """Create README in dags/ folder with usage instructions."""

    readme_content = f"""# Airflow DAG Configuration

This folder contains the Airflow DAG for your FLUID data product.

## Generated DAG

- **DAG ID**: `{dag_id}`
- **Schedule**: `{schedule}`
- **File**: `{dag_filename}`

## Usage

### Local Development

Run the DAG locally using Airflow:

```bash
# Start Airflow (from project root)
docker-compose --profile airflow up -d

# Access Airflow UI
open http://localhost:8080

# Default credentials
# Username: admin
# Password: admin
```

### Manual Execution

Run the FLUID pipeline manually:

```bash
# Validate contract
fluid validate contract.fluid.yaml

# Plan execution
fluid plan contract.fluid.yaml

# Apply changes
fluid apply contract.fluid.yaml --auto-approve
```

### CI/CD Integration

This DAG can be deployed to:
- Cloud Composer (GCP)
- MWAA (AWS)
- Astronomer
- Self-hosted Airflow

See `.jenkins/` folder for CI/CD pipeline configuration.

## Customization

To customize the DAG:

1. Edit `{dag_filename}`
2. Add custom operators or sensors
3. Configure alerting and notifications
4. Update schedule interval as needed

## Next Steps

- **Add data quality checks**: Use Great Expectations or Soda
- **Set up alerting**: Configure email/Slack notifications
- **Add lineage tracking**: Enable OpenLineage integration
- **Monitor performance**: Use Airflow metrics

For more information, see: https://fluid.dev/docs/orchestration
"""

    readme_path = dag_dir / "README.md"
    with open(readme_path, "w") as f:
        f.write(readme_content)


# ============================================================================
# MODE HANDLERS
# ============================================================================
