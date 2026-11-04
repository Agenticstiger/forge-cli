# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/snowflake/orchestration/__init__.py
"""
Snowflake Provider Orchestration Support.

Generates orchestration artifacts (DAGs, pipelines, flows) from FLUID 0.7.1
orchestration specifications for Airflow, Dagster, and Prefect.
"""

from .airflow_generator import generate_airflow_dag
from .common import OrchestrationConfig, TaskDependency

__all__ = [
    "generate_airflow_dag",
    "OrchestrationConfig",
    "TaskDependency",
]
