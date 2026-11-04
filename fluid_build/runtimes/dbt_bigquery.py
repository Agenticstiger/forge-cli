# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import logging
import shutil
import subprocess

from ..providers.base import ApplyResult


def run_dbt_bigquery(project_dir: str, profiles_dir: str = None, target: str = None) -> ApplyResult:
    if shutil.which("dbt") is None:
        return ApplyResult(False, "dbt not installed or not in PATH", error="missing_dbt")
    cmd = ["dbt", "build"]
    if profiles_dir:
        cmd += ["--profiles-dir", profiles_dir]
    if target:
        cmd += ["--target", target]
    try:
        subprocess.check_call(cmd, cwd=project_dir)
        return ApplyResult(True, "dbt build succeeded")
    except subprocess.CalledProcessError as e:
        logging.getLogger("fluid.runtimes.dbt_bigquery").exception("dbt_build_failed")
        return ApplyResult(False, "dbt build failed", error=str(e))
