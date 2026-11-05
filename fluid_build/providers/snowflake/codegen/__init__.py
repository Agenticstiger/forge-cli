# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Snowflake Code Generation for Orchestration Engines.

Generates executable DAG/pipeline code from FLUID contracts for:
- Apache Airflow
- Dagster
- Prefect
"""

from .airflow import generate_airflow_dag
from .dagster import generate_dagster_pipeline
from .prefect import generate_prefect_flow

__all__ = [
    "generate_airflow_dag",
    "generate_dagster_pipeline",
    "generate_prefect_flow",
]
