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

"""Tests for fluid_build.forge.core.apply_modes — Phase-6 stage-7 matrix.

Adversarial bias: every test pins a specific semantic the pipeline
design (plan Part 1 + decisions D10–D13) depends on. Most critical is
the data-loss gate: if any test here starts passing under a behavior
regression, operators could silently drop populated tables in prod.
"""

from __future__ import annotations

import pytest

from fluid_build.forge.core.apply_modes import (
    BUILD_MODES,
    CANONICAL_CHOICES,
    DESTRUCTIVE_MODES,
    ApplyMode,
    check_data_loss_gate,
    full_refresh_required,
    is_destructive,
    is_dry_run,
    needs_build,
    parse_mode,
)

# ---------------------------------------------------------------------------
# Enum / constants
# ---------------------------------------------------------------------------


class TestApplyMode:
    def test_six_canonical_modes(self):
        """Plan Part 1 specifies exactly six modes. Regression guard — if
        someone adds a seventh without updating the matrix docstring, this
        test fires first."""
        assert len(list(ApplyMode)) == 6

    def test_canonical_choices_order(self):
        """Order is least-destructive → most-destructive, matching the
        argparse ``choices=`` list operators see in ``--help``."""
        assert CANONICAL_CHOICES == [
            "dry-run",
            "create-only",
            "amend",
            "amend-and-build",
            "replace",
            "replace-and-build",
        ]

    def test_default_is_amend(self):
        """Default mode is ``amend`` — additive schema evolution, data
        preserved. Changing this is a breaking change for every CI pipeline
        that relies on the zero-flag invocation."""
        assert ApplyMode.default() is ApplyMode.AMEND

    def test_destructive_modes_are_the_replace_variants(self):
        """Only the two replace modes DROP + recreate; mislabeling this
        would cause the data-loss gate to fire in wrong places."""
        assert DESTRUCTIVE_MODES == {
            ApplyMode.REPLACE,
            ApplyMode.REPLACE_AND_BUILD,
        }

    def test_build_modes_are_the_and_build_variants(self):
        assert BUILD_MODES == {
            ApplyMode.AMEND_AND_BUILD,
            ApplyMode.REPLACE_AND_BUILD,
        }

    def test_is_destructive_predicate(self):
        for mode in [ApplyMode.DRY_RUN, ApplyMode.CREATE_ONLY, ApplyMode.AMEND]:
            assert not is_destructive(mode), f"{mode} flagged destructive"
        for mode in [ApplyMode.AMEND_AND_BUILD]:
            assert not is_destructive(
                mode
            ), f"{mode} flagged destructive; amend-and-build preserves data"
        for mode in DESTRUCTIVE_MODES:
            assert is_destructive(mode)

    def test_needs_build_predicate(self):
        for mode in ApplyMode:
            assert needs_build(mode) == (mode in BUILD_MODES), (
                f"{mode.value}: needs_build={needs_build(mode)} != "
                f"member-of-BUILD_MODES={mode in BUILD_MODES}"
            )

    def test_is_dry_run_predicate(self):
        assert is_dry_run(ApplyMode.DRY_RUN)
        for mode in ApplyMode:
            if mode is ApplyMode.DRY_RUN:
                continue
            assert not is_dry_run(mode)

    def test_full_refresh_only_for_replace_and_build(self):
        """dbt's ``--full-refresh`` forces a fresh build. Only meaningful
        after ``replace`` (DROP+CREATE); ``amend-and-build`` keeps data and
        uses normal incremental-compatible runs."""
        assert full_refresh_required(ApplyMode.REPLACE_AND_BUILD)
        for mode in ApplyMode:
            if mode is ApplyMode.REPLACE_AND_BUILD:
                continue
            assert not full_refresh_required(mode)


# ---------------------------------------------------------------------------
# Data-loss gate — THE critical invariant
# ---------------------------------------------------------------------------


class TestDataLossGate:
    """Prevents `--mode replace` from silently dropping populated tables in
    non-dev environments without an explicit operator opt-in."""

    def test_non_destructive_always_passes(self):
        """dry-run, create-only, amend, amend-and-build never trip the gate —
        they don't drop data, so ``--allow-data-loss`` isn't required."""
        for mode in [
            ApplyMode.DRY_RUN,
            ApplyMode.CREATE_ONLY,
            ApplyMode.AMEND,
            ApplyMode.AMEND_AND_BUILD,
        ]:
            result = check_data_loss_gate(
                mode, env="prod", target_row_count=1000000, allow_data_loss=False
            )
            assert not result.blocked, f"{mode.value} wrongly blocked"

    def test_replace_in_prod_with_rows_blocked_without_opt_in(self):
        """The headline case. Gate MUST block; reason MUST mention the row
        count so the operator sees what they'd be dropping."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="prod",
            target_row_count=1234567,
            allow_data_loss=False,
        )
        assert result.blocked
        assert "1,234,567" in result.reason
        assert "--allow-data-loss" in result.reason
        assert "prod" in result.reason

    def test_replace_and_build_in_prod_with_rows_blocked(self):
        """Both destructive modes gate the same way — replace-and-build is
        not a free pass just because it also runs dbt."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE_AND_BUILD,
            env="prod",
            target_row_count=500,
            allow_data_loss=False,
        )
        assert result.blocked

    def test_replace_with_opt_in_always_passes(self):
        """``--allow-data-loss`` is the explicit confirmation. Once set,
        the gate lets the destructive DDL through regardless of env/rows."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="prod",
            target_row_count=1000000,
            allow_data_loss=True,
        )
        assert not result.blocked

    def test_replace_in_dev_with_empty_target_passes(self):
        """Dev + empty target → no opt-in required. Forcing --allow-data-loss
        on every hello-world recreate would create pointless friction."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="dev",
            target_row_count=0,
            allow_data_loss=False,
        )
        assert not result.blocked

    def test_replace_in_dev_with_rows_still_blocked(self):
        """Dev doesn't auto-bypass the gate for populated targets — dev
        products can still carry real data (e.g. an engineer's integration
        test fixtures). Gate requires --allow-data-loss."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="dev",
            target_row_count=500,
            allow_data_loss=False,
        )
        assert result.blocked

    def test_replace_with_unknown_row_count_blocked(self):
        """``target_row_count=None`` means the provider couldn't cheaply
        count rows. Gate defaults to 'treat as populated' — fail-safe. A
        false-positive block is recoverable (pass --allow-data-loss); a
        false-negative silent drop is not."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="prod",
            target_row_count=None,
            allow_data_loss=False,
        )
        assert result.blocked
        assert "unknown" in result.reason

    def test_replace_with_env_none_treated_as_non_dev(self):
        """Missing env = not dev, so gate applies. Preserves safety when
        someone forgets to set FLUID_ENV."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env=None,
            target_row_count=100,
            allow_data_loss=False,
        )
        assert result.blocked

    def test_gate_reason_mentions_backup_suffix(self):
        """The error message must tell the operator a backup will be made
        AND how rollback works. Without this, 'pass --allow-data-loss' feels
        like asking them to approve destruction without a safety net."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="prod",
            target_row_count=100,
            allow_data_loss=False,
        )
        assert "__backup_" in result.reason
        assert "fluid rollback" in result.reason

    def test_gate_reason_does_not_promise_a_snapshot_the_engine_cannot_take(self):
        """Only the native apply path plans the pre-flight CLONE and writes
        ``.fluid/rollback-state.json``. The OpenTofu engine — the default for
        every cloud provider — has no CTAS/CLONE step, so promising
        ``<target>__backup_<ts>`` there tells an operator they have a restore
        point they do not have. The message must say the opposite."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env="prod",
            target_row_count=777,
            allow_data_loss=False,
            snapshot_available=False,
        )
        assert result.blocked
        assert "__backup_<ts> table is created" in result.reason
        assert "NO SNAPSHOT WILL BE TAKEN" in result.reason
        assert "no restore point" in result.reason
        # The opt-in incantation is still spelled out — this is an honesty
        # fix, not a removal of the remediation.
        assert "--allow-data-loss" in result.reason

    @pytest.mark.parametrize("env_variant", ["dev", "DEV", "  dev  ", "development"])
    def test_dev_variants_normalized(self, env_variant):
        """``env='  dev  '`` / ``'DEV'`` / ``'development'`` all count as
        dev for the gate. Defensive: operators' env values drift in practice."""
        result = check_data_loss_gate(
            ApplyMode.REPLACE,
            env=env_variant,
            target_row_count=0,
            allow_data_loss=False,
        )
        assert not result.blocked, f"{env_variant!r} wrongly blocked"


# ---------------------------------------------------------------------------
# --mode + --build resolution (legacy compatibility)
# ---------------------------------------------------------------------------


class TestParseMode:
    """``parse_mode`` is the canonical mode parser. The legacy
    ``resolve_mode_with_build_alias`` (which auto-upgraded ``--build X``
    to ``--mode amend-and-build``) was deleted along with the ``--build``
    flag's deprecation path."""

    def test_none_yields_default(self):
        assert parse_mode(None) is ApplyMode.default()

    def test_explicit_mode_passes_through(self):
        for value in CANONICAL_CHOICES:
            assert parse_mode(value).value == value

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unknown --mode"):
            parse_mode("invalid-mode")

    def test_non_string_input_returns_default(self):
        """Defensive: argparse always sends strings, but a test passing
        a MagicMock should not blow up — return the default instead."""
        assert parse_mode(object()) is ApplyMode.default()


# ---------------------------------------------------------------------------
# CLI integration — --mode + --allow-data-loss + --build registration
# ---------------------------------------------------------------------------


class TestCliRegistration:
    """Pin the CLI flag wiring so Phase 6's stage-7 surface can't silently
    regress. Uses argparse directly — no live apply run."""

    def _parser(self):
        import argparse

        from fluid_build.cli import apply as apply_mod

        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        apply_mod.register(sub)
        return p

    def test_mode_flag_accepts_all_six_canonical_values(self):
        for value in CANONICAL_CHOICES:
            args = self._parser().parse_args(["apply", "c.yaml", "--mode", value])
            assert args.mode == value

    def test_mode_flag_rejects_unknown(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["apply", "c.yaml", "--mode", "nonsense"])

    def test_allow_data_loss_defaults_false(self):
        args = self._parser().parse_args(["apply", "c.yaml"])
        assert args.allow_data_loss is False

    def test_allow_data_loss_sets_true(self):
        args = self._parser().parse_args(
            ["apply", "c.yaml", "--mode", "replace", "--allow-data-loss"]
        )
        assert args.allow_data_loss is True

    def test_build_flag_still_parses_for_back_compat(self):
        """``--build X`` without ``--mode`` must still parse (parser layer).
        The deprecation warning fires in run(), not argparse."""
        args = self._parser().parse_args(["apply", "c.yaml", "--build", "orders"])
        assert args.build_id == "orders"
        assert args.mode is None  # deprecation resolution happens in run()

    def test_mode_default_is_none_for_resolution_logic(self):
        """``--mode`` defaults to None (not 'amend'). The real default is
        resolved by ``resolve_mode_with_build_alias`` so legacy ``--build``
        can auto-upgrade to amend-and-build without tripping the 'both flags
        set' check."""
        args = self._parser().parse_args(["apply", "c.yaml"])
        assert args.mode is None
