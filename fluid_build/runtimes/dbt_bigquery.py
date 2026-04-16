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
