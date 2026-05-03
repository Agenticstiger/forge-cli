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

"""Regression coverage: ``fluid apply`` hydrates env at entry.

Symmetric to the helper already wired into ``verify`` and ``publish``.
Without this, ``fluid apply --build`` spawns a dbt subprocess with empty
``SNOWFLAKE_USER`` / ``SNOWFLAKE_DATABASE`` when the caller only exported
``FLUID_SECRETS_FILE`` (the launchpad convention) — the generated
``profiles.yml`` ends up with ``user: ""`` and dbt aborts on parse.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

from fluid_build.cli import apply as apply_module


def test_apply_run_hydrates_dotenv_before_branch_dispatch(tmp_path: Path) -> None:
    # Build a minimal argparse.Namespace that apply.run will short-circuit on.
    # ``--mode amend-and-build`` delegates to build_runners.run_builds_from_args;
    # we stub that so we can assert hydrate_dotenv fired *before* delegation.
    args = argparse.Namespace(
        contract=str(tmp_path / "contract.fluid.yaml"),
        env=None,
        dry_run=True,
        mode="amend-and-build",
        build_id="some_build",
    )
    (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: '0.7.2'\nkind: DataProduct\n")

    call_order: list[str] = []

    def _fake_hydrate(project_root: Path, environment=None) -> None:
        call_order.append("hydrate")

    def _fake_run_builds(args, logger, *, force_run=False) -> int:
        call_order.append("run_builds")
        return 0

    with (
        patch.object(apply_module, "hydrate_dotenv", _fake_hydrate),
        patch("fluid_build.build_runners.run_builds_from_args", _fake_run_builds),
    ):
        rc = apply_module.run(args, logging.getLogger("test"))

    assert rc == 0
    # Critically: hydrate ran, and ran *before* run_builds — otherwise the dbt
    # subprocess launched inside build_runners would see empty SNOWFLAKE_*.
    assert call_order == ["hydrate", "run_builds"]


def test_apply_run_passes_env_overlay_to_hydrate(tmp_path: Path) -> None:
    # The ``--env prod`` overlay must propagate to the hydration helper so
    # the right ``.env.{environment}`` file is layered in.
    args = argparse.Namespace(
        contract=str(tmp_path / "contract.fluid.yaml"),
        env="prod",
        dry_run=True,
        mode="amend-and-build",
        build_id="some_build",
    )
    (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: '0.7.2'\nkind: DataProduct\n")

    captured: dict = {}

    def _fake_hydrate(project_root: Path, environment=None) -> None:
        captured["environment"] = environment

    with (
        patch.object(apply_module, "hydrate_dotenv", _fake_hydrate),
        patch("fluid_build.build_runners.run_builds_from_args", lambda *a, **k: 0),
    ):
        apply_module.run(args, logging.getLogger("test"))

    assert captured["environment"] == "prod"
