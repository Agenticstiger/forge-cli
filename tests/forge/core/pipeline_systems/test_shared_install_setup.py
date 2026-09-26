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

"""The shared setup step (every non-Jenkins system) installs with one argument
per value.

It concatenated FLUID_PIP_INDEX_URL / FLUID_PIP_EXTRA_INDEX_URL /
FLUID_PACKAGE_SPEC into a string and ran it through ``sh -c``, so a value was
re-parsed as shell: ``;`` ran a command and a space added pip options.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

from fluid_build import __version__
from fluid_build.forge.core.pipeline_templates import (
    BasePipelineTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
)


def _run_setup(tmp_path: Path, env: Dict[str, str]) -> List[str]:
    script = BasePipelineTemplate()._render_install_setup(
        PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS, complexity=PipelineComplexity.STANDARD
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.txt"
    python = bin_dir / "python"
    # One line per call, its arguments separated by the ASCII unit separator.
    python.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\037' \"$a\"; done >> '{log}'\n"
        f"echo >> '{log}'\n",
        encoding="utf-8",
    )
    fluid = bin_dir / "fluid"
    fluid.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for exe in (python, fluid):
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    subprocess.run(
        ["sh", "-c", script],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **env},
        check=True,
    )
    calls = [line.split("\037")[:-1] for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[0] == ["-m", "pip", "install", "--upgrade", "pip"]
    return calls[-1]


def test_a_value_is_one_pip_argument_and_never_shell(tmp_path):
    argv = _run_setup(
        tmp_path,
        {
            "FLUID_PIP_INDEX_URL": "https://mirror.example/simple --trusted-host evil.example",
            "FLUID_PACKAGE_SPEC": "data-product-forge; touch pwned",
        },
    )
    assert not (tmp_path / "pwned").exists()
    assert "--index-url=https://mirror.example/simple --trusted-host evil.example" in argv
    assert "--trusted-host" not in argv
    assert argv[-2:] == ["--", "data-product-forge; touch pwned"]


def test_without_parameters_it_installs_the_generating_version(tmp_path):
    argv = _run_setup(tmp_path, {})
    assert argv[-2:] == ["--", f"data-product-forge=={__version__}"]
