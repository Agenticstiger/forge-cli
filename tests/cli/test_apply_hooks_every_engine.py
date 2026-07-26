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

"""Apply-time plugin hooks must run on EVERY apply engine.

Regression guard for the fail-open control described in the OpenTofu
cutover: ``cli/apply.py`` invoked ``_run_apply_hooks`` only from the native
path's YAML branch, several hundred lines *after* the
``resolve_apply_engine(...) == "opentofu"`` early return. Every provider in
``iac.cutover.OPENTOFU_DEFAULT_PROVIDERS`` (aws / gcp / snowflake /
confluent) took that early return, so a registered scaffold-digest-drift or
lockfile guard fired against the toy ``local`` target and silently did
nothing against the real clouds — and ``--force-pattern-drift`` had nothing
to override there.

These tests pin: hooks run before ``tofu`` is touched, a hook that reports
drift aborts the apply with exit 1 and no ``apply_via_opentofu`` call, and
``--force-pattern-drift`` still overrides.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

from fluid_build.cli import apply as apply_cli

logger = logging.getLogger("test_apply_hooks_every_engine")

_CONTRACT = {"id": "silver.community.smoke_v1", "name": "Smoke"}


def _args(contract: str, **overrides) -> argparse.Namespace:
    defaults = dict(
        contract=contract,
        env=None,
        mode="amend",
        dry_run=False,
        allow_data_loss=False,
        config_override=None,
        timeout=120,
        parallel_phases=False,
        rollback_strategy="phase_complete",
        provider=None,
        project=None,
        region=None,
        bundle=None,
        provider_config=None,
        report=None,
        report_format="html",
        state_file=None,
        workspace_dir=Path("."),
        notify=None,
        metrics_export="none",
        debug=False,
        yes=True,
        build_id=None,
        no_verify_plan_binding=False,
        no_verify_federation=True,
        force_pattern_drift=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text("id: silver.community.smoke_v1\nname: Smoke\n", encoding="utf-8")
    return path


def _opentofu_engine():
    return patch(
        "fluid_build.cli._apply_opentofu_engine.resolve_apply_engine",
        return_value="opentofu",
    )


class TestApplyHooksOnOpenTofuEngine:
    def test_hooks_run_before_tofu_on_the_opentofu_path(self, tmp_path):
        """The cloud path invokes the hook surface, and does so *before*
        ``apply_via_opentofu`` — i.e. before ``tofu init/plan/apply``."""
        contract_file = _write_yaml(tmp_path)
        order: list[str] = []

        def _hook(*_a, **_kw):
            order.append("hook")
            return 0

        def _tofu(*_a, **_kw):
            order.append("tofu")
            return 0

        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                return_value=dict(_CONTRACT),
            ),
            _opentofu_engine(),
            patch("fluid_build.cli.apply._run_apply_hooks", side_effect=_hook) as hooks,
            patch(
                "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
                side_effect=_tofu,
            ),
        ):
            rc = apply_cli.run(_args(str(contract_file)), logger)

        assert rc == 0
        assert order == ["hook", "tofu"]
        assert hooks.call_count == 1

    def test_env_is_plumbed_to_hooks_on_the_opentofu_path(self, tmp_path):
        """``--env`` reaches the hook dispatcher on the cloud path too —
        PR #368's plumbing was only reachable via the local provider."""
        contract_file = _write_yaml(tmp_path)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                return_value=dict(_CONTRACT),
            ),
            _opentofu_engine(),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks,
            patch(
                "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
                return_value=0,
            ),
        ):
            apply_cli.run(_args(str(contract_file), env="staging"), logger)

        assert hooks.call_args.kwargs["env"] == "staging"
        assert hooks.call_args.kwargs["force"] is False

    def test_reporting_hook_aborts_before_any_tofu_call(self, tmp_path):
        """A guard that reports drift blocks the cloud apply with exit 1 and
        ``apply_via_opentofu`` is never entered — no DDL, no state change."""
        contract_file = _write_yaml(tmp_path)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                return_value=dict(_CONTRACT),
            ),
            _opentofu_engine(),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=1),
            patch(
                "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
                return_value=0,
            ) as tofu,
        ):
            rc = apply_cli.run(_args(str(contract_file)), logger)

        assert rc == 1
        tofu.assert_not_called()

    def test_force_pattern_drift_overrides_on_the_opentofu_path(self, tmp_path):
        """The documented override reaches the hook runner on the cloud path."""
        contract_file = _write_yaml(tmp_path)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                return_value=dict(_CONTRACT),
            ),
            _opentofu_engine(),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks,
            patch(
                "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
                return_value=0,
            ),
        ):
            apply_cli.run(_args(str(contract_file), force_pattern_drift=True), logger)

        assert hooks.call_args.kwargs["force"] is True


class TestRunApplyHooksForSource:
    """``_run_apply_hooks_for_source`` loads both documented apply inputs."""

    def test_loads_a_yaml_contract(self, tmp_path):
        contract_file = _write_yaml(tmp_path)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay",
                return_value=dict(_CONTRACT),
            ),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks,
        ):
            rc = apply_cli._run_apply_hooks_for_source(_args(str(contract_file)), logger)

        assert rc == 0
        assert hooks.call_args.args[0] == _CONTRACT
        assert hooks.call_args.args[1] == contract_file.resolve().parent

    def test_loads_the_contract_embedded_in_a_plan_json(self, tmp_path):
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(json.dumps({"contract": _CONTRACT}), encoding="utf-8")
        with patch("fluid_build.cli.apply._run_apply_hooks", return_value=0) as hooks:
            rc = apply_cli._run_apply_hooks_for_source(_args(str(plan_file)), logger)

        assert rc == 0
        assert hooks.call_args.args[0] == _CONTRACT

    def test_unloadable_source_defers_to_the_engine(self, tmp_path):
        """A source the loader rejects is the engine's error to report — the
        hook adapter must not mask it with its own non-zero exit."""
        missing = tmp_path / "nope.fluid.yaml"
        with patch(
            "fluid_build.cli.apply.load_contract_with_overlay",
            side_effect=OSError("boom"),
        ):
            rc = apply_cli._run_apply_hooks_for_source(_args(str(missing)), logger)
        assert rc == 0

    def test_plan_json_without_an_embedded_contract_is_skipped(self, tmp_path):
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(json.dumps({"actions": []}), encoding="utf-8")
        with patch("fluid_build.cli.apply._run_apply_hooks", new=Mock()) as hooks:
            rc = apply_cli._run_apply_hooks_for_source(_args(str(plan_file)), logger)
        assert rc == 0
        hooks.assert_not_called()
