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
