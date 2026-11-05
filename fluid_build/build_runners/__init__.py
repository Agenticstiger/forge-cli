# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Runtime build execution — runs dbt projects and plain Python scripts.

Public API::

    from fluid_build.build_runners import run_builds_from_args

    # Typically called from ``fluid apply --build <id>``.
    return run_builds_from_args(args, logger, force_run=True)

This package is the runtime counterpart of ``fluid_build.engines.*`` —
the latter generates dbt/SQL project *files* at ``fluid generate
speed-transformation`` time; this one *executes* those projects at
``fluid apply --build`` time.

Callers invoke :func:`run_builds_from_args` directly.
"""

from .base import (
    ENV_PLACEHOLDER_RE,
    SENSITIVE_ENV_KEY_RE,
    _resolve_env_placeholders,
    is_dbt_build,
    run_builds_from_args,
)
from .dbt.runner import (
    build_dbt_command,
    execute_dbt_build,
    resolve_dbt_project_path,
)
from .python.runner import execute_build, resolve_script_path

__all__ = [
    "ENV_PLACEHOLDER_RE",
    "SENSITIVE_ENV_KEY_RE",
    "_resolve_env_placeholders",
    "build_dbt_command",
    "execute_build",
    "execute_dbt_build",
    "is_dbt_build",
    "resolve_dbt_project_path",
    "resolve_script_path",
    "run_builds_from_args",
]
