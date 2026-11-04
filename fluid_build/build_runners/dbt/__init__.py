# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Runtime dbt build runner. Distinct from ``fluid_build.engines.dbt``
which generates dbt project files at ``fluid generate speed-transformation``
time; this package executes dbt builds at ``fluid apply --build`` time."""

from .profiles import (
    DbtProfileError,
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
    "DbtProfileError",
    "_build_generated_dbt_profile",
    "_create_temp_dbt_profiles_dir",
    "_load_dbt_project_config",
    "build_dbt_command",
    "execute_dbt_build",
    "resolve_dbt_profiles_dir",
    "resolve_dbt_project_path",
]
