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

"""Integration tests for the stage-7 plan-binding gate in ``fluid apply``.

Adversarial bias: every test drives the code path an attacker would take.
Breaking any of these means the Terraform-style "apply consumes exact
plan" guarantee is gone — ``fluid apply`` would happily run a plan that
was mutated since stage 6.

Three attack shapes verified here:

    1. **Plan tamper**: attacker edits ``plan.json`` between stages 6
       and 7 (e.g. injecting a ``drop_table`` action). Must hard-fail
       with event ``apply_plan_digest_plan_tamper`` before any DDL.
    2. **Missing digest**: attacker strips ``planDigest`` to bypass the
       gate (hoping the verifier falls through to success). Must also
       hard-fail with the same event.
    3. **Emergency opt-out**: operator passes ``--no-verify-digest``
       (legit DR scenario — bundle unreachable). Gate waives with a
       WARNING-level log entry, apply proceeds.

CLI registration smoke is split out so the argparse surface regression
gets a dedicated green/red indicator separate from behaviour tests.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import _verify_plan_digests
from fluid_build.cli.apply import register as register_apply
from fluid_build.forge.core.plan_digest import inject_digests

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_plan() -> Dict[str, Any]:
    """Structurally realistic plan body — matches plan.py output shape."""
    return {
        "format_version": "0.7.1",
        "generated_at": 1700000000,
        "contract": {"id": "orders", "name": "Orders", "version": "0.7.1"},
        "actions": [
            {
                "step": 1,
                "action_id": "ensure_table_orders",
                "action_type": "ensure_table",
                "provider": "snowflake",
                "params": {"table": "orders"},
                "depends_on": [],
            }
        ],
        "total_actions": 1,
    }


def _fake_args(**overrides: Any) -> argparse.Namespace:
    """Stand-in for the argparse namespace consumed by ``_verify_plan_digests``.

    Only two attrs matter for the gate — ``no_verify_digest`` (the
    emergency waiver) and ``contract`` (for telemetry, not logic). Any
    other attribute lookups get ``MagicMock`` auto-behaviour, which we
    don't want to trigger — hence using a real Namespace, not a Mock."""
    ns = argparse.Namespace(
        contract="runtime/plan.json",
        no_verify_digest=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# _verify_plan_digests — the actual gate function
# ---------------------------------------------------------------------------


class TestVerifyPlanDigestsHappyPath:
    """Plans produced by ``inject_digests`` must pass verification cold."""

    def test_fresh_plan_passes(self, caplog: pytest.LogCaptureFixture) -> None:
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        logger = logging.getLogger("fluid_build.cli.apply.test")

        # Must NOT raise.
        _verify_plan_digests(plan, _fake_args(), logger)


class TestVerifyPlanDigestsPlanTamper:
    """Attacker shape #1: mutate plan body, rely on stale planDigest."""

    def test_injected_action_caught(self) -> None:
        plan = inject_digests(_minimal_plan(), bundle_path=None)

        # Attacker injects a drop action — plan body mutates, stored
        # planDigest no longer matches recompute.
        plan["actions"].append(
            {
                "step": 99,
                "action_id": "drop_customers",
                "action_type": "drop_table",
                "provider": "snowflake",
                "params": {"table": "customers"},
                "depends_on": [],
            }
        )

        logger = logging.getLogger("fluid_build.cli.apply.test")
        with pytest.raises(CLIError) as exc_info:
            _verify_plan_digests(plan, _fake_args(), logger)

        assert exc_info.value.exit_code == 1
        # Event tag is stable for CI log parsers.
        assert exc_info.value.event == "apply_plan_digest_plan_tamper"
        assert exc_info.value.context["kind"] == "plan-tamper"

    def test_mutated_action_params_caught(self) -> None:
        """Even a subtle change — flipping a table name inside params —
        invalidates the digest. This is the high-value path: an
        attacker redirecting a create to their own schema."""
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        plan["actions"][0]["params"]["table"] = "evil_exfil"

        logger = logging.getLogger("fluid_build.cli.apply.test")
        with pytest.raises(CLIError) as exc_info:
            _verify_plan_digests(plan, _fake_args(), logger)
        assert exc_info.value.event == "apply_plan_digest_plan_tamper"


class TestVerifyPlanDigestsMissingDigest:
    """Attacker shape #2: strip the digest field hoping verifier no-ops."""

    def test_missing_plan_digest_caught(self) -> None:
        plan = _minimal_plan()
        plan["bundleDigest"] = ""
        # No planDigest field at all.

        logger = logging.getLogger("fluid_build.cli.apply.test")
        with pytest.raises(CLIError) as exc_info:
            _verify_plan_digests(plan, _fake_args(), logger)
        assert exc_info.value.event == "apply_plan_digest_plan_tamper"

    def test_empty_plan_digest_caught(self) -> None:
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        plan["planDigest"] = ""  # defensive: empty treated as missing

        logger = logging.getLogger("fluid_build.cli.apply.test")
        with pytest.raises(CLIError):
            _verify_plan_digests(plan, _fake_args(), logger)


class TestVerifyPlanDigestsEmergencyOptOut:
    """Attacker shape #3 (inverted): legit operator DR scenario.

    --no-verify-digest is a POWERFUL flag. These tests ensure (a) it
    actually waives, (b) it logs loudly enough that audit trails see it.
    """

    def test_waiver_allows_mutated_plan(self, caplog: pytest.LogCaptureFixture) -> None:
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        plan["actions"].append(
            {
                "step": 99,
                "action_id": "recovery_op",
                "action_type": "ensure_table",
                "provider": "snowflake",
                "params": {"table": "dr_target"},
                "depends_on": [],
            }
        )

        caplog.set_level(logging.WARNING)
        logger = logging.getLogger("fluid_build.cli.apply.emergency")
        # Waiver is set → no raise even with tampered plan.
        _verify_plan_digests(plan, _fake_args(no_verify_digest=True), logger)

    def test_waiver_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Audit-trail invariant: the waiver MUST surface at WARNING
        level. A silent opt-out would defeat the purpose of the flag."""
        plan = inject_digests(_minimal_plan(), bundle_path=None)

        caplog.set_level(logging.WARNING)
        logger = logging.getLogger("fluid_build.cli.apply.emergency_log")
        _verify_plan_digests(plan, _fake_args(no_verify_digest=True), logger)

        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no-verify-digest" in m.lower() for m in messages), messages


# ---------------------------------------------------------------------------
# CLI flag registration — argparse surface
# ---------------------------------------------------------------------------


class TestNoVerifyDigestFlag:
    """The flag must be reachable via argparse so operators can pass it."""

    def _parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        register_apply(sub)
        return p

    def test_flag_defaults_false(self) -> None:
        args = self._parser().parse_args(["apply", "plan.json"])
        assert args.no_verify_digest is False

    def test_flag_sets_true(self) -> None:
        args = self._parser().parse_args(["apply", "plan.json", "--no-verify-digest"])
        assert args.no_verify_digest is True

    def test_flag_compatible_with_mode(self) -> None:
        """Passing --no-verify-digest alongside --mode must not conflict
        at parse time (semantic compatibility is tested below)."""
        args = self._parser().parse_args(
            [
                "apply",
                "plan.json",
                "--mode",
                "amend",
                "--no-verify-digest",
            ]
        )
        assert args.mode == "amend"
        assert args.no_verify_digest is True


# ---------------------------------------------------------------------------
# End-to-end: verify function is wired into apply.run()
# ---------------------------------------------------------------------------


class TestApplyRunInvokesVerification:
    """Dynamic wiring smoke — confirms apply.run() actually calls the
    verification function when loading a .json plan file.

    We drive this through ``argparse.Namespace`` + patched loaders so
    we never touch a real provider. Catches regressions where someone
    removes the ``_verify_plan_digests`` call from run()."""

    def test_tampered_plan_file_blocks_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: stage-6 produces plan.json, attacker edits it,
        stage-7 reads the file via args.contract and must hard-fail
        BEFORE touching any provider."""
        from fluid_build.cli import apply as apply_mod

        # Produce a plan and save it to disk.
        plan = inject_digests(_minimal_plan(), bundle_path=None)
        # Tamper.
        plan["actions"].append(
            {
                "step": 99,
                "action_id": "evil",
                "action_type": "drop_table",
                "provider": "snowflake",
                "params": {"table": "orders"},
                "depends_on": [],
            }
        )
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan))

        # Build a minimal Namespace the run() function can consume up
        # to the verification point. Attributes beyond that don't matter
        # because verification fires before the complex-mode branch.
        args = argparse.Namespace(
            contract=str(plan_path),
            env="dev",
            mode=None,
            allow_data_loss=False,
            no_verify_digest=False,
            build_id=None,
            dry_run=False,
            yes=True,
            verbose=False,
            debug=False,
            workspace_dir=tmp_path,
            state_file=None,
            config_override=None,
            report=str(tmp_path / "rep.html"),
            report_format="html",
            metrics_export="none",
            notify=None,
            rollback_strategy="none",
            require_approval=False,
            backup_state=False,
            validate_dependencies=False,
            timeout=120,
            parallel_phases=False,
            max_workers=4,
            keep_temp_files=False,
            profile=False,
            delay=2,
            fail_fast=False,
            no_output=False,
            provider_config=None,
        )

        # Prevent the full apply path from running in case the gate
        # somehow fails to fire — we don't want an assertion error
        # shadowed by a provider import failure. hydrate_dotenv is a
        # side-effect-only call and is safe to no-op.
        monkeypatch.setattr(apply_mod, "hydrate_dotenv", lambda *a, **kw: None)

        logger = logging.getLogger("fluid_build.cli.apply.integration")
        with pytest.raises(CLIError) as exc_info:
            apply_mod.run(args, logger)

        assert exc_info.value.event == "apply_plan_digest_plan_tamper"
