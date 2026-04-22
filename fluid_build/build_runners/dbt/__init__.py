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

"""Runtime dbt build runner. Distinct from ``fluid_build.engines.dbt``
which generates dbt project files at ``fluid generate speed-transformation``
time; this package executes dbt builds at ``fluid apply --build`` time."""

from .profiles import (
    _build_generated_dbt_profile,
    _create_temp_dbt_profiles_dir,
    _load_dbt_project_config,
    resolve_dbt_profiles_dir,
)
from .runner import (
    build_dbt_command,
    execute_dbt_build,
    resolve_dbt_project_path,
)

__all__ = [
    "_build_generated_dbt_profile",
    "_create_temp_dbt_profiles_dir",
    "_load_dbt_project_config",
    "build_dbt_command",
    "execute_dbt_build",
    "resolve_dbt_profiles_dir",
    "resolve_dbt_project_path",
]
