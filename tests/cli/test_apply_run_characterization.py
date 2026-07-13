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

"""Characterization ("golden-master") tests for ``cli/apply.py::run``.

These tests exist to make a structural refactor of ``run()`` provably
behaviour-preserving. Following Michael Feathers' *Working Effectively with
Legacy Code*, a characterization test observes the *current* output for a
given input and pins it, so a later refactor that changes behaviour breaks a
test rather than shipping silently.

What is pinned here is the **apply exit-code contract** — the observable
result the operator (and every CI pipeline) depends on — for the key paths
through ``run()``:

    path                              observable golden
    --------------------------------  ---------------------------------------
    success (simple mode)             return 0
    dry-run short-circuit             return 0
    no actions                        return 0
    schema-invalid contract           CLIError(exit_code=1, apply_contract_invalid)
    invalid --config-override JSON    CLIError(exit_code=2, invalid_config_override)
    plan-binding digest tamper        CLIError(exit_code=1, apply_plan_digest_*)
    destructive replace w/o override  CLIError(exit_code=1, apply_mode_data_loss_blocked)
    KeyboardInterrupt                 return 130

The plan-binding digest gate and the data-loss gate are the two
security-sensitive gates on ``fluid apply`` (the platform's mutation path).
The tests drive the *real* gates (real digest crypto, real
``check_data_loss_gate``) — no gate is stubbed on the paths that assert a
gate's exit code — so the golden captures the true, wired behaviour.

The suite is written to pass byte-for-byte on BOTH the pre-extraction and
post-extraction ``run()``; if the ``_run_simple_apply`` extraction drifts an
exit code or lets a gate leak, one of these assertions fails.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from fluid_build.cli._common import CLIError

logger = logging.getLogger("test_apply_run_characterization")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_args(contract: str, **overrides) -> argparse.Namespace:
    """A complete ``apply`` argparse namespace with safe defaults.

    Every attribute ``apply.run`` (and its callees) dereferences is present,
    so a test never trips over a missing field. Overrides steer an individual
    path (mode, dry_run, env, ...).
    """
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
        # Isolate the two apply gates from each other: skip federation
        # (needs a manifest) but keep plan-binding ON by default so the
        # tamper test drives the real gate. Individual tests flip these.
        no_verify_plan_binding=False,
        no_verify_federation=True,
        force_pattern_drift=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_contract(tmp_path: Path) -> Path:
    contract_file = tmp_path / "contract.fluid.yaml"
    contract_file.write_text("id: test\nname: Test\n", encoding="utf-8")
    return contract_file


def _minimal_contract() -> dict:
    return {"id": "test-product", "name": "Test Product"}


def _ok_provider() -> Mock:
    provider = Mock()
    provider.apply.return_value = {"failed": 0, "applied": 1, "status": "success"}
    provider.name = "local"
    return provider


def _patch_hooks():
    """Patch the three lifecycle hooks imported locally inside ``run``."""
    return (
        patch("fluid_build.cli.hooks.run_pre_apply", new=Mock(side_effect=lambda p, a, l: a)),
        patch("fluid_build.cli.hooks.run_post_apply", new=Mock()),
        patch("fluid_build.cli.hooks.run_on_error", new=Mock()),
    )


def _run(args):
    """Call ``apply.run`` and normalise a ``SystemExit`` into its code."""
    from fluid_build.cli import apply as apply_cli

    try:
        return apply_cli.run(args, logger)
    except SystemExit as exc:  # pragma: no cover — apply returns ints today
        return exc.code


# ---------------------------------------------------------------------------
# Golden exit codes — simple-mode success paths (return 0)
# ---------------------------------------------------------------------------


class TestSimpleModeSuccessGolden:
    """The three ``return 0`` paths through simple mode."""

    def test_success_returns_0(self, tmp_path):
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file))
        pre, post, on_err = _patch_hooks()
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply.build_provider", return_value=_ok_provider()),
            patch(
                "fluid_build.cli.apply._actions_from_source",
                return_value=[{"op": "ensure_dataset"}],
            ),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
            pre,
            post,
            on_err,
        ):
            rc = _run(args)
        assert rc == 0

    def test_dry_run_returns_0(self, tmp_path):
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file), dry_run=True)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply.build_provider", return_value=_ok_provider()) as bp,
            patch(
                "fluid_build.cli.apply._actions_from_source",
                return_value=[{"op": "ensure_dataset"}],
            ),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
        ):
            rc = _run(args)
        assert rc == 0
        # Dry run must never reach the provider mutation call.
        bp.return_value.apply.assert_not_called()

    def test_no_actions_returns_0(self, tmp_path):
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file))
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply.build_provider", return_value=_ok_provider()),
            patch("fluid_build.cli.apply._actions_from_source", return_value=[]),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
        ):
            rc = _run(args)
        assert rc == 0


# ---------------------------------------------------------------------------
# Golden exit codes — validation / input gates
# ---------------------------------------------------------------------------


class TestInputGateGolden:
    def test_schema_invalid_contract_raises_exit_1(self, tmp_path):
        """The pre-apply contract gate rejects a structurally-invalid
        contract with exit 1 and event ``apply_contract_invalid`` — BEFORE
        any provider is built."""
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file))
        # NOTE: _gate_contract_for_apply is NOT stubbed here — the real gate runs.
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply.build_provider") as build_provider,
        ):
            with pytest.raises(CLIError) as excinfo:
                _run(args)
        assert excinfo.value.exit_code == 1
        assert excinfo.value.event == "apply_contract_invalid"
        build_provider.assert_not_called()

    def test_invalid_config_override_raises_exit_2(self, tmp_path):
        """Malformed ``--config-override`` JSON → exit 2,
        ``invalid_config_override``, and no provider built."""
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file), config_override="{not valid json")
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply.build_provider") as build_provider,
        ):
            with pytest.raises(CLIError) as excinfo:
                _run(args)
        assert excinfo.value.exit_code == 2
        assert excinfo.value.event == "invalid_config_override"
        build_provider.assert_not_called()


# ---------------------------------------------------------------------------
# Golden exit codes — security gates (plan-binding + data-loss)
# ---------------------------------------------------------------------------


def _valid_embedded_contract() -> dict:
    """A schema-valid v0.7.x contract for embedding in a plan.json."""
    from fluid_build.forge.product_types import ProductTypeAnswer, shape_contract

    contract = shape_contract(ProductTypeAnswer(product_type="CDP", name="chargolden"))
    contract["id"] = "demo.chargolden"
    return contract


class TestPlanBindingGateGolden:
    """The cryptographic stage-7 plan-binding gate: a tampered plan.json
    aborts apply with exit 1 (event ``apply_plan_digest_*``) before any DDL."""

    def _write_plan(self, tmp_path: Path, *, tamper: bool) -> Path:
        from fluid_build.forge.core.plan_digest import inject_digests

        plan = {
            "fluid_version": "0.7.3",
            "mode": "amend",
            "contract_id": "demo.chargolden",
            "contract": _valid_embedded_contract(),
            "actions": [
                {
                    "id": "action_0",
                    "op": "provisionDataset",
                    "action_type": "provision_dataset",
                    "metadata": {"target": "demo.chargolden"},
                }
            ],
        }
        # Bind digests over the clean plan (no bundle → bundleDigest="").
        bound = inject_digests(plan, bundle_path=None)
        if tamper:
            # Mutate an action AFTER binding so the stored planDigest is stale.
            bound["actions"][0]["op"] = "TAMPERED"
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(bound), encoding="utf-8")
        return plan_path

    def test_tampered_plan_raises_exit_1(self, tmp_path):
        plan_path = self._write_plan(tmp_path, tamper=True)
        args = _base_args(str(plan_path), no_verify_plan_binding=False)
        with patch("fluid_build.cli.apply.build_provider") as build_provider:
            with pytest.raises(CLIError) as excinfo:
                _run(args)
        assert excinfo.value.exit_code == 1
        assert excinfo.value.event.startswith("apply_plan_digest")
        # The gate fires before any provider dispatch.
        build_provider.assert_not_called()

    def test_untampered_plan_passes_the_binding_gate(self, tmp_path):
        """Control: an untampered, digest-bound plan clears the binding gate
        and reaches provider dispatch (proving the tamper test above is
        actually exercising the gate, not some unrelated early failure)."""
        plan_path = self._write_plan(tmp_path, tamper=False)
        args = _base_args(str(plan_path), no_verify_plan_binding=False)
        reached = {"provider": False}

        def _spy_build_provider(*a, **k):
            reached["provider"] = True
            return _ok_provider()

        pre, post, on_err = _patch_hooks()
        with (
            patch("fluid_build.cli.apply.build_provider", side_effect=_spy_build_provider),
            patch(
                "fluid_build.cli.apply._actions_from_source",
                return_value=[{"op": "ensure_dataset"}],
            ),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
            pre,
            post,
            on_err,
        ):
            rc = _run(args)
        assert reached["provider"] is True
        assert rc == 0


class TestDataLossGateGolden:
    """The stage-7 data-loss safety gate: a destructive ``replace`` in a
    non-dev env without ``--allow-data-loss`` is blocked with exit 1 (event
    ``apply_mode_data_loss_blocked``) before any provider dispatch."""

    def test_destructive_replace_without_override_blocks_exit_1(self, tmp_path):
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file), mode="replace", env="prod", allow_data_loss=False)
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
            patch("fluid_build.cli.apply.build_provider") as build_provider,
        ):
            with pytest.raises(CLIError) as excinfo:
                _run(args)
        assert excinfo.value.exit_code == 1
        assert excinfo.value.event == "apply_mode_data_loss_blocked"
        # Gate fires before any provider dispatch — no mutation attempted.
        build_provider.assert_not_called()

    def test_destructive_replace_and_build_without_override_blocks_exit_1(self, tmp_path):
        """The build-augmented destructive mode (``replace-and-build``) must
        hit the SAME data-loss gate as plain ``replace``.

        ``replace-and-build`` is in BOTH ``DESTRUCTIVE_MODES`` and
        ``BUILD_MODES``. The ``needs_build`` branch short-circuits into
        ``run_builds_from_args`` (whose dbt path appends a destructive
        ``--full-refresh``) and returns, so the gate MUST run *before* that
        early-return — otherwise a destructive rebuild runs in prod with no
        ``--allow-data-loss`` opt-in. Regression guard: neither the build
        runner nor a provider may be reached when the gate blocks."""
        contract_file = _write_contract(tmp_path)
        args = _base_args(
            str(contract_file),
            mode="replace-and-build",
            env="prod",
            allow_data_loss=False,
        )
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
            patch("fluid_build.cli.apply.build_provider") as build_provider,
            patch("fluid_build.build_runners.run_builds_from_args") as run_builds,
        ):
            with pytest.raises(CLIError) as excinfo:
                _run(args)
        assert excinfo.value.exit_code == 1
        assert excinfo.value.event == "apply_mode_data_loss_blocked"
        # Gate fires before the build-mode early-return: no destructive
        # rebuild and no provider mutation are attempted.
        run_builds.assert_not_called()
        build_provider.assert_not_called()

    def test_destructive_replace_with_override_clears_the_gate(self, tmp_path):
        """Control: same destructive apply WITH ``--allow-data-loss`` clears
        the gate and reaches provider dispatch (return 0)."""
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file), mode="replace", env="prod", allow_data_loss=True)
        pre, post, on_err = _patch_hooks()
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply._run_apply_hooks", return_value=0),
            patch("fluid_build.cli.apply.build_provider", return_value=_ok_provider()),
            patch(
                "fluid_build.cli.apply._actions_from_source",
                return_value=[{"op": "ensure_dataset"}],
            ),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
            pre,
            post,
            on_err,
        ):
            rc = _run(args)
        assert rc == 0


# ---------------------------------------------------------------------------
# Golden exit codes — interruption
# ---------------------------------------------------------------------------


class TestInterruptGolden:
    def test_keyboard_interrupt_returns_130(self, tmp_path):
        """A ``KeyboardInterrupt`` anywhere in the execution body is caught
        and mapped to exit 130 (SIGINT convention)."""
        contract_file = _write_contract(tmp_path)
        args = _base_args(str(contract_file))
        with (
            patch(
                "fluid_build.cli.apply.load_contract_with_overlay", return_value=_minimal_contract()
            ),
            patch("fluid_build.cli.apply._gate_contract_for_apply", new=Mock()),
            patch("fluid_build.cli.apply.build_provider", side_effect=KeyboardInterrupt),
            patch(
                "fluid_build.cli.apply._actions_from_source",
                return_value=[{"op": "ensure_dataset"}],
            ),
            patch("fluid_build.cli.apply.RICH_AVAILABLE", False),
        ):
            rc = _run(args)
        assert rc == 130


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
