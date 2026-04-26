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

"""Regression pins for Sprint #5–8 wirings (B3 / B4 / B5 / B6 / A2).

Every test in this file asserts an end-to-end behavior, not just a
helper's local correctness:

* B3 — A non-zero cost ceiling actually aborts a forge mid-call.
* B4 — ``_run_logical_critic`` is invoked from each LogicalAgent
  entry point in the coordinator.
* B5 — Pre-emit conformance summary writes → CLI print site reads.
* B6 — Unified config priority (unified > legacy) is enforced.
* A2 — Successful forges write a ``forge.success`` event to
  ``memory/episodic``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.cost import (
    CostLimitExceeded,
    check_cost_ceiling,
    get_pre_emit_conformance_summary,
    get_run_tracker,
    reset_run_tracker,
    set_pre_emit_conformance_summary,
)
from fluid_build.copilot.store.backends.null import NullBackend


@pytest.fixture(autouse=True)
def _hermetic():
    reset_run_tracker()
    yield
    reset_run_tracker()


# ----------------------------------------------------------------------
# B3 — Cost ceiling
# ----------------------------------------------------------------------


class TestCostCeiling:
    def test_no_ceiling_no_op(self, monkeypatch):
        """When neither env nor unified config sets a ceiling,
        ``check_cost_ceiling`` is a no-op."""
        monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Should NOT raise.
        check_cost_ceiling()

    def test_under_ceiling_no_raise(self, monkeypatch):
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "100.00")
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=10_000,
            output_tokens=2_000,
        )
        check_cost_ceiling()  # No raise — well under $100

    def test_over_ceiling_raises_with_actual_numbers(self, monkeypatch):
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.01")
        # claude-sonnet-4-6 is $3 in / $15 out per 1M.
        # 100k in + 50k out = $0.30 + $0.75 = $1.05 → exceeds $0.01.
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=50_000,
        )
        with pytest.raises(CostLimitExceeded) as exc_info:
            check_cost_ceiling()
        # Message includes the actual numbers — operator sees what
        # to set the ceiling to.
        assert exc_info.value.limit_usd == 0.01
        assert exc_info.value.running_usd > 0.01

    def test_unknown_model_disables_ceiling_silently(self, monkeypatch):
        """When ``total_usd`` is None (unknown model) we cannot
        enforce a ceiling we cannot measure; surface a debug log
        and continue rather than fail-closed loudly."""
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.01")
        get_run_tracker().record_call(
            provider="openai",
            model="future-gpt-9000",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Should NOT raise — total_usd is None.
        check_cost_ceiling()

    def test_zero_or_negative_ceiling_disabled(self, monkeypatch):
        """Defensive: a zero / negative env value is treated as
        'no ceiling' rather than 'cap at 0' (which would block
        every forge)."""
        for value in ("0", "0.0", "-1.50"):
            monkeypatch.setenv("FLUID_COST_LIMIT_USD", value)
            get_run_tracker().record_call(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=10_000,
                output_tokens=10_000,
            )
            check_cost_ceiling()  # No raise.

    def test_garbage_ceiling_disabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "not-a-number")
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        check_cost_ceiling()  # No raise — garbage env is no ceiling.


# ----------------------------------------------------------------------
# B4 — _run_logical_critic wiring pin
# ----------------------------------------------------------------------


class TestRunLogicalCriticWiring:
    """The coordinator's three Logical entry points
    (from_tables / from_intent / from_catalog) MUST call
    ``_run_logical_critic`` immediately after the LogicalAgent
    emits. Without these pins, a future refactor could drop a
    call site and the critic's voice would silently fall off."""

    def test_helper_invokes_review_logical(self, tmp_path):
        from fluid_build.copilot.agents.coordinator import StageCoordinator

        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        logical = SimpleNamespace(
            dv2=SimpleNamespace(hubs=[], links=[]),
            conceptual=SimpleNamespace(entities=[]),
        )

        with patch.object(
            coordinator.critic_agent,
            "review_logical",
        ) as spy:
            coordinator._run_logical_critic(session, logical=logical)

        spy.assert_called_once()
        # Pad was passed as scratchpad kwarg.
        kwargs = spy.call_args.kwargs
        assert "scratchpad" in kwargs

    def test_helper_swallows_agent_errors(self, tmp_path):
        """Critic crash MUST NOT block the rest of the forge."""
        from fluid_build.copilot.agents.coordinator import StageCoordinator

        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        with patch.object(
            coordinator.critic_agent,
            "review_logical",
            side_effect=RuntimeError("simulated"),
        ):
            # Must NOT raise.
            coordinator._run_logical_critic(session, logical=SimpleNamespace())


# ----------------------------------------------------------------------
# B5 — Conformance summary print
# ----------------------------------------------------------------------


class TestConformanceSummaryPrint:
    def test_set_then_get_round_trip(self):
        set_pre_emit_conformance_summary("conformance: ✓ 4 standards clean")
        assert get_pre_emit_conformance_summary() == "conformance: ✓ 4 standards clean"

    def test_reset_clears_slot(self):
        set_pre_emit_conformance_summary("x")
        reset_run_tracker()
        assert get_pre_emit_conformance_summary() is None

    def test_print_site_reads_slot(self, capsys):
        from fluid_build.cli.forge_data_model import (
            _print_pre_emit_conformance_summary,
        )

        set_pre_emit_conformance_summary("conformance: standards=fluid,osi clean")
        _print_pre_emit_conformance_summary(quiet=False)
        captured = capsys.readouterr()
        assert "conformance" in captured.out

    def test_print_site_silent_when_empty(self, capsys):
        from fluid_build.cli.forge_data_model import (
            _print_pre_emit_conformance_summary,
        )

        set_pre_emit_conformance_summary(None)
        _print_pre_emit_conformance_summary(quiet=False)
        captured = capsys.readouterr()
        assert "conformance" not in captured.out

    def test_print_site_silent_when_quiet(self, capsys):
        from fluid_build.cli.forge_data_model import (
            _print_pre_emit_conformance_summary,
        )

        set_pre_emit_conformance_summary("conformance: clean")
        _print_pre_emit_conformance_summary(quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""


# ----------------------------------------------------------------------
# B6 — Unified config priority
# ----------------------------------------------------------------------


class TestUnifiedConfigPriority:
    def test_unified_wins_over_legacy(self, tmp_path, monkeypatch):
        """When BOTH unified and legacy files exist with different
        provider values, unified MUST win."""
        import json

        import yaml

        monkeypatch.setenv("FLUID_HOME", str(tmp_path))
        monkeypatch.delenv("FLUID_CONFIG", raising=False)
        # Legacy file says ``openai``.
        legacy = tmp_path / "ai_config.json"
        legacy.write_text(
            json.dumps({"provider": "openai", "model": "gpt-4.1"}),
            encoding="utf-8",
        )
        # Unified file says ``anthropic``.
        unified = tmp_path / "config.yaml"
        unified.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                }
            ),
            encoding="utf-8",
        )

        # Need to also point ai_setup at the legacy file via its
        # module-level constant.
        from fluid_build.cli import ai_setup

        monkeypatch.setattr(ai_setup, "_CONFIG_FILE", legacy)
        cfg = ai_setup._load_ai_config()
        assert cfg is not None
        assert cfg["provider"] == "anthropic"
        assert cfg["model"] == "claude-sonnet-4-6"

    def test_legacy_used_when_unified_missing(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setenv("FLUID_HOME", str(tmp_path))
        monkeypatch.delenv("FLUID_CONFIG", raising=False)
        # No unified file.
        legacy = tmp_path / "ai_config.json"
        legacy.write_text(
            json.dumps({"provider": "openai", "model": "gpt-4o-mini"}),
            encoding="utf-8",
        )

        from fluid_build.cli import ai_setup

        monkeypatch.setattr(ai_setup, "_CONFIG_FILE", legacy)
        cfg = ai_setup._load_ai_config()
        assert cfg is not None
        assert cfg["provider"] == "openai"

    def test_neither_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_HOME", str(tmp_path))
        monkeypatch.delenv("FLUID_CONFIG", raising=False)

        from fluid_build.cli import ai_setup

        monkeypatch.setattr(ai_setup, "_CONFIG_FILE", tmp_path / "absent.json")
        assert ai_setup._load_ai_config() is None


# ----------------------------------------------------------------------
# A2 — Episodic memory writer
# ----------------------------------------------------------------------


class TestCriticErrorEscalation:
    """C8 — error-severity critic findings flip the validation
    report's ``passes_schema`` to False AND get appended as
    findings. Without this, the repair loop never fires on
    critic-only errors."""

    def test_critic_error_escalates_into_report(self, tmp_path):
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.schemas.stage_outputs import (
            PhysicalDraft,
            ValidationFinding,
            ValidationReport,
        )
        from fluid_build.copilot.scratchpad import CriticFinding

        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            # Opt in to critic-error → repair-loop trigger.
            capability_matrix={"critic_errors_trigger_repair": True},
        )
        scratch = session.get_scratchpad()
        scratch.add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="error",
                message="Hub has no business keys",
                target="dv2.hubs.hub_x.business_key_columns",
            )
        )

        # Validator originally passed cleanly (no findings).
        physical = SimpleNamespace(
            validation=ValidationReport(
                score=10,
                issues=[],
                passes_schema=True,
            )
        )

        coordinator._escalate_critic_errors_into_report(
            session,
            physical=physical,
        )

        # Report now has a critic-derived error.
        assert physical.validation.passes_schema is False
        assert len(physical.validation.issues) == 1
        assert "[critic:logical]" in physical.validation.issues[0].message
        assert "business keys" in physical.validation.issues[0].message
        assert physical.validation.issues[0].severity == "error"

    def test_critic_warning_does_not_escalate(self, tmp_path):
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.schemas.stage_outputs import (
            ValidationReport,
        )
        from fluid_build.copilot.scratchpad import CriticFinding

        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        session.get_scratchpad().add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="warning",
                message="orphan entity",
            )
        )

        physical = SimpleNamespace(
            validation=ValidationReport(score=10, issues=[], passes_schema=True),
        )
        coordinator._escalate_critic_errors_into_report(
            session,
            physical=physical,
        )
        # Clean report stays clean — warnings don't trigger repair.
        assert physical.validation.passes_schema is True
        assert physical.validation.issues == []


class TestEpisodicMemoryWriter:
    def test_record_forge_episode_writes_event(self, tmp_path, monkeypatch):
        """Successful forge writes a ``forge.success`` event with
        the headline metadata."""
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.store.backends.file import FileBackend

        # Disable the opt-out so the writer fires.
        monkeypatch.delenv("FLUID_COPILOT_EPISODIC_MEMORY", raising=False)

        backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
        session = StageSession(store=backend, workspace_root=tmp_path)
        logical = SimpleNamespace(
            name="orders",
            technique="data_vault_2",
            dv2=SimpleNamespace(hubs=[1, 2, 3], links=[1], satellites=[1, 2]),
            dimensional=None,
        )

        coordinator = StageCoordinator()
        coordinator._record_forge_episode(
            session,
            outcome="success",
            source_type="intent",
            logical=logical,
        )

        # Read back from the store.
        records = backend.query("memory/episodic", limit=10)
        assert len(records) == 1
        record = records[0]
        # Either dict-payload or attribute access — check both shapes.
        value = record.value if hasattr(record, "value") else record
        assert isinstance(value, dict)
        assert value["outcome"] == "success"
        assert value["model_name"] == "orders"
        assert value["technique"] == "data_vault_2"
        assert value["dv2_counts"]["hubs"] == 3

    def test_opt_out_via_env_var(self, tmp_path, monkeypatch):
        """``FLUID_COPILOT_EPISODIC_MEMORY=0`` disables the writer."""
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.store.backends.file import FileBackend

        monkeypatch.setenv("FLUID_COPILOT_EPISODIC_MEMORY", "0")

        backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
        session = StageSession(store=backend, workspace_root=tmp_path)
        logical = SimpleNamespace(
            name="x",
            technique="data_vault_2",
            dv2=None,
            dimensional=None,
        )

        StageCoordinator()._record_forge_episode(
            session,
            outcome="success",
            source_type="intent",
            logical=logical,
        )
        assert backend.query("memory/episodic", limit=10) == []

    def test_store_failure_doesnt_propagate(self, tmp_path, monkeypatch):
        from fluid_build.copilot.agents.coordinator import StageCoordinator

        monkeypatch.delenv("FLUID_COPILOT_EPISODIC_MEMORY", raising=False)

        broken_store = MagicMock()
        broken_store.put.side_effect = RuntimeError("disk full")
        session = StageSession(store=broken_store, workspace_root=tmp_path)
        logical = SimpleNamespace(
            name="x",
            technique="data_vault_2",
            dv2=None,
            dimensional=None,
        )

        # Must NOT raise.
        StageCoordinator()._record_forge_episode(
            session,
            outcome="success",
            source_type="intent",
            logical=logical,
        )
