# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dev-source install every non-Jenkins wrapper runs, run in a real shell.

It used to export ``PYTHONPATH=/forge-cli-src`` and call ``fluid``: nothing put
the command on PATH, and the export died with the GitHub step or the ( ... )
subshell the other systems wrap it in, so every ``fluid ship`` step failed with
``fluid: command not found``. It now installs the mounted checkout editable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fluid_build.forge.core.pipeline_systems import PipelineConfig, PipelineProvider
from fluid_build.forge.core.pipeline_systems.github_actions import GitHubActionsTemplate

pytestmark = [pytest.mark.unit]

MOUNT = "/forge-cli-src"

#: A ``python`` that installs a ``fluid`` command when pip installs the mount
#: editable, and records every call.
_PYTHON = """#!/bin/sh
echo "$*" >> "$RECORD"
if [ "$1 $2 $3" = "-m pip install" ]; then
  [ -n "${PIP_FAILS:-}" ] && exit 1
  printf '#!/bin/sh\\necho fluid 0.0.0\\n' > "$BIN/fluid"
  chmod +x "$BIN/fluid"
fi
"""


def _setup(mount: str) -> str:
    config = PipelineConfig(provider=PipelineProvider.GITHUB_ACTIONS, install_mode="dev-source")
    script = GitHubActionsTemplate()._render_install_setup(config)
    assert MOUNT in script
    return script.replace(MOUNT, mount)


def _run(tmp_path: Path, script: str, **env: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "python").write_text(_PYTHON)
    (bin_dir / "python").chmod(0o700)
    # Subshell, as Tekton, GitLab, Azure, Bitbucket and CircleCI run it, in a
    # shell that stops on the first failure, as theirs do.
    wrapped = "set -e\n(\n" + script + "\n)\nfluid --version\n"
    return subprocess.run(
        [shutil.which("sh") or "/bin/sh", "-c", wrapped],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "RECORD": str(tmp_path / "record"),
            "BIN": str(bin_dir),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_install_puts_fluid_on_path_beyond_the_subshell(tmp_path):
    mount = tmp_path / "src"
    mount.mkdir()
    (mount / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    run = _run(tmp_path, _setup(str(mount)))
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[-1] == "fluid 0.0.0"
    calls = (tmp_path / "record").read_text().splitlines()
    assert calls == [f"-m pip install --quiet -e {mount}"]


def test_nothing_has_to_outlive_the_script():
    script = _setup(MOUNT)
    assert "export" not in script
    assert "PYTHONPATH" not in script


def test_a_missing_mount_fails_loudly(tmp_path):
    run = _run(tmp_path, _setup(str(tmp_path / "absent")))
    assert run.returncode == 2
    assert "FATAL: dev-source install mode requires the forge-cli checkout" in run.stderr
    assert not (tmp_path / "record").exists()


def test_a_failed_install_fails_loudly(tmp_path):
    mount = tmp_path / "src"
    mount.mkdir()
    (mount / "pyproject.toml").write_text("")
    run = _run(tmp_path, _setup(str(mount)), PIP_FAILS="1")
    assert run.returncode == 3
    assert f"FATAL: pip install -e {mount} failed" in run.stderr
