# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = [pytest.mark.unit]


def _jenkinsfile(install_mode: str) -> str:
    cfg = PipelineConfig(
        provider=PipelineProvider.JENKINS,
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


def test_stage8_policy_apply_mode_has_a_default_in_the_environment_block():
    """Ship reads POLICY_APPLY_MODE from the environment. On the first build after
    a Jenkinsfile change (or any trigger that does not pass it) the parameter is
    null, so the environment {} block must fall back to the declared default,
    which is also what ship defaults to: a blank would be refused by ship."""
    jf = _jenkinsfile("dev-source")
    assert "POLICY_APPLY_MODE = \"${params.POLICY_APPLY_MODE == null ? 'enforce' : " in jf
