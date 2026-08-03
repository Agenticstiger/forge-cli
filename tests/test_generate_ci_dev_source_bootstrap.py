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

"""The dev-source Jenkins bootstrap must leave `fluid` callable.

Regression pin for a real bug: the generated dev-source Jenkinsfile ran
`pip uninstall -y data-product-forge` (which deletes the `fluid` console
script — `fluid` is the package's `console_scripts` entry point) and then
invoked `fluid` relying only on `PYTHONPATH=/forge-cli-src`. PYTHONPATH
supplies the *module* (so `python -m fluid_build.cli` works) but NOT the
`fluid` *command*, so stage 0 died with `fluid: not found` — the entire
dev-source pipeline was unrunnable.

The fix mirrors the non-Jenkins runners' `_render_install_setup`: keep the
installed console script (PYTHONPATH-prepend already shadows its modules
with the checkout) and never uninstall it.
"""

from __future__ import annotations

import pytest

from fluid_build.forge.core.pipeline_systems import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = [pytest.mark.unit]


def _jenkinsfile(install_mode: str) -> str:
    cfg = PipelineConfig(
        provider=PipelineProvider.JENKINS,
        complexity=PipelineComplexity("standard"),
        install_mode=install_mode,
        sink_platform="snowflake",
    )
    return PipelineTemplateGenerator().generate_pipeline(cfg)["Jenkinsfile"]


def test_dev_source_bootstrap_does_not_uninstall_the_fluid_command():
    jf = _jenkinsfile("dev-source")
    assert "pip uninstall -y data-product-forge" not in jf, (
        "dev-source bootstrap must not uninstall data-product-forge — that removes "
        "the `fluid` console script and breaks stage 0 with `fluid: not found`."
    )


def test_dev_source_bootstrap_sanity_checks_the_import():
    jf = _jenkinsfile("dev-source")
    # A clear, early failure if the bind mount / PYTHONPATH is wrong.
    assert 'python -c "import fluid_build"' in jf
    # And it still verifies the command resolves.
    assert "fluid --version" in jf


def test_dev_source_still_exports_pythonpath_to_the_bind_mount():
    jf = _jenkinsfile("dev-source")
    assert "/forge-cli-src" in jf
    assert "PYTHONPATH" in jf


def test_pypi_mode_unaffected():
    """The pypi-mode bootstrap still pip-installs the package (provides `fluid`)."""
    jf = _jenkinsfile("pypi")
    assert "pip install" in jf
    assert "data-product-forge" in jf


def test_stage8_policy_apply_mode_has_a_shell_default():
    """Stage 8 must not emit a bare `--mode "$POLICY_APPLY_MODE"`.

    The param is read as a raw shell env var; on the first build after a
    Jenkinsfile change (or any trigger that doesn't pass it) the param isn't
    injected, so a bare reference yields `--mode ` (empty) and
    `fluid policy-apply` rejects it (`invalid choice: ''`). A `:-enforce`
    shell default — matching the param's own default — keeps it valid.
    """
    jf = _jenkinsfile("dev-source")
    assert '--mode "${POLICY_APPLY_MODE}"' not in jf, (
        "stage 8 emits a bare ${POLICY_APPLY_MODE} — an unset param becomes "
        "`--mode ` and policy-apply rejects the empty choice."
    )
    assert '"${POLICY_APPLY_MODE:-enforce}"' in jf
