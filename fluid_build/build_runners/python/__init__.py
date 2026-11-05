# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Plain-Python script runner for builds that are not dbt projects."""

from .runner import execute_build, resolve_script_path

__all__ = ["execute_build", "resolve_script_path"]
