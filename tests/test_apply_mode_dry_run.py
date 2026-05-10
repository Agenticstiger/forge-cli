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

"""Pin the ``--mode dry-run`` short-circuit in ``cli/apply.py``.

History: ``apply.py`` had two parallel signals — the legacy ``--dry-run``
boolean flag and the canonical ``--mode dry-run`` value. The function
computed an ``effective_dry_run`` that combined the two, but the actual
gate at the provider-call site only checked ``args.dry_run``. That meant
``--mode dry-run`` slipped past the gate and reached the Snowflake /
GCP / AWS providers, which then attempted to authenticate. Discovered
while running biz-lab contracts: ``apply --mode dry-run`` against a
Snowflake contract failed with "Snowflake account not specified" /
"No Snowflake credentials found", because the provider was actually
called.

Fix: stomp the resolved value back onto ``args.dry_run`` after
computing ``effective_dry_run`` so every downstream check (the
confirmation-prompt guard, the dry-run early return, the rich panel
emit) sees the canonical signal.

This module pins the fix so a future refactor doesn't reintroduce the
divergence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_args(contract_path: str, *, mode: str = "dry-run", dry_run_flag: bool = False):
    """Build a minimal argparse Namespace that drives apply.run to the
    dry-run gate without other side effects."""
    return SimpleNamespace(
        contract=contract_path,
        env="dev",
        mode=mode,
        dry_run=dry_run_flag,
        target=None,
        timeout=60,
        parallel_phases=False,
        rollback_strategy="manual",
        allow_data_loss=False,
        config_override=None,
        verbose=False,
        provider=None,
        no_validate=True,
        no_verify_digest=True,
        yes=True,
        report=None,
        report_format="html",
        no_output=True,
        build_id=None,
    )


def _write_plan(tmp_path: Path, mode: str = "dry-run") -> Path:
    """Write a minimal Snowflake plan stamped with the given mode."""
    plan = {
        "fluid_version": "0.7.3",
        "mode": mode,
        "contract_id": "demo.smoke",
        "contract": {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "demo.smoke",
            "exposes": [
                {
                    "exposeId": "smoke",
                    "binding": {"platform": "snowflake"},
                }
            ],
        },
        "actions": [
            {
                "id": "action_0",
                "op": "provisionDataset",
                "action_type": "provision_dataset",
                "metadata": {"target": "demo.smoke"},
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# 1. ``--mode dry-run`` short-circuits the provider call.
# ---------------------------------------------------------------------------


class TestModeDryRunShortCircuits:
    """``apply --mode dry-run`` must NOT reach ``provider.apply()`` —
    the early-return at the dry-run gate fires first."""

    def test_mode_dry_run_skips_provider_apply(self, tmp_path: Path):
        """Stamp a plan with ``mode: dry-run``, run apply.run with
        ``--mode dry-run``, and assert the Snowflake provider's
        apply() was never called."""
        from fluid_build.cli import apply as apply_cli

        plan_path = _write_plan(tmp_path, mode="dry-run")
        args = _make_args(str(plan_path), mode="dry-run", dry_run_flag=False)

        provider_apply_calls: list = []

        class _SpyProvider:
            def apply(self, *a, **kw):  # noqa: D401
                provider_apply_calls.append((a, kw))
                return {"applied": 1, "failed": 0, "results": []}

        # Patch the provider builder so apply.run picks up our spy.
        with patch("fluid_build.cli.apply.build_provider", return_value=_SpyProvider()):
            try:
                rc = apply_cli.run(args, logging.getLogger("fluid.test"))
            except SystemExit as exc:  # apply may sys.exit on dry-run path
                rc = exc.code

        # Provider's apply() must NOT have fired in dry-run mode.
        assert provider_apply_calls == [], (
            f"provider.apply() was called {len(provider_apply_calls)} time(s) "
            "despite --mode dry-run; the gate is leaking through to the provider"
        )
        # And the run must report success (rc=0).
        assert rc == 0, f"apply --mode dry-run returned non-zero: {rc!r}"

    def test_legacy_dry_run_flag_still_short_circuits(self, tmp_path: Path):
        """The legacy ``--dry-run`` flag must keep working — backward-
        compat with operators who scripted against it."""
        from fluid_build.cli import apply as apply_cli

        plan_path = _write_plan(tmp_path, mode="amend")
        args = _make_args(str(plan_path), mode="amend", dry_run_flag=True)

        provider_apply_calls: list = []

        class _SpyProvider:
            def apply(self, *a, **kw):
                provider_apply_calls.append((a, kw))
                return {"applied": 1, "failed": 0, "results": []}

        with patch("fluid_build.cli.apply.build_provider", return_value=_SpyProvider()):
            try:
                rc = apply_cli.run(args, logging.getLogger("fluid.test"))
            except SystemExit as exc:
                rc = exc.code

        assert provider_apply_calls == []
        assert rc == 0


# ---------------------------------------------------------------------------
# 2. ``args.dry_run`` is normalised after mode resolution.
# ---------------------------------------------------------------------------


class TestArgsDryRunNormalisation:
    """After apply.run computes ``effective_dry_run``, the result is
    stomped back onto ``args.dry_run`` so every downstream code path
    (confirmation prompt, rich panel, telemetry) sees one signal."""

    def test_args_dry_run_is_true_after_mode_dry_run(self, tmp_path: Path):
        """We can't observe ``args.dry_run`` after run() returns, but
        we CAN spy on the run() body via the dry-run gate output:
        the rich ``🔍 Dry Run`` panel is the visible side-effect.
        Capture stdout and assert the panel rendered."""
        from fluid_build.cli import apply as apply_cli

        plan_path = _write_plan(tmp_path, mode="dry-run")
        args = _make_args(str(plan_path), mode="dry-run")

        class _SpyProvider:
            def apply(self, *a, **kw):  # pragma: no cover — should not fire
                raise AssertionError("provider.apply should not be called in dry-run")

        with patch("fluid_build.cli.apply.build_provider", return_value=_SpyProvider()):
            try:
                apply_cli.run(args, logging.getLogger("fluid.test"))
            except SystemExit:
                pass

        # The fix sets args.dry_run = True after mode resolution.
        assert (
            args.dry_run is True
        ), f"args.dry_run should be True after --mode dry-run normalisation, got {args.dry_run!r}"


# ---------------------------------------------------------------------------
# 3. ``--mode amend`` (default) does NOT short-circuit.
# ---------------------------------------------------------------------------


class TestNonDryRunModeReachesProvider:
    """Sanity: apply with a non-dry-run mode must still call
    provider.apply() — the fix doesn't accidentally turn EVERY apply
    into a dry-run."""

    def test_mode_amend_calls_provider(self, tmp_path: Path):
        from fluid_build.cli import apply as apply_cli

        plan_path = _write_plan(tmp_path, mode="amend")
        args = _make_args(str(plan_path), mode="amend", dry_run_flag=False)

        provider_apply_calls: list = []

        class _SpyProvider:
            def apply(self, *a, **kw):
                provider_apply_calls.append((a, kw))
                return {"applied": 1, "failed": 0, "results": []}

        with patch("fluid_build.cli.apply.build_provider", return_value=_SpyProvider()):
            try:
                apply_cli.run(args, logging.getLogger("fluid.test"))
            except (SystemExit, Exception):
                # Apply may raise on the synthetic plan (no real provider
                # config); that's fine — we only care that provider.apply
                # was reached.
                pass

        assert len(provider_apply_calls) >= 1, (
            "provider.apply() was NEVER called despite --mode amend; "
            "the dry-run gate is firing for non-dry-run modes"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
