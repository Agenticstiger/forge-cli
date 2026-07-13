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

"""Pin the copilot semantic-drift guard (authoring/refinement).

The guard compares the LLM-authored contract's schema against a baseline —
either the discovered SOURCE schema or the PRIOR contract version (the
``--refine`` seed) — and classifies drift (dropped / renamed / type-changed
columns), then feeds corrective feedback into the existing self-healing repair
loop. Structural slice only; the LLM-judge "meaning shift" slice is a follow-up.

Drift-class vocabulary mirrors ``cli/_verify_reconcile.py`` (``ReconcileReport``
+ ``ColumnDrift.reason``) for a consistent UX across ``fluid verify`` and forge.
"""

from __future__ import annotations

import json
import os
from typing import List
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Baseline extraction helpers
# ---------------------------------------------------------------------------


def _contract_with_columns(columns, *, name="Orders"):
    """A minimal FLUID contract carrying one expose with *columns*."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.sales.orders",
        "name": name,
        "domain": "sales",
        "metadata": {"layer": "Bronze", "productType": "SDP", "owner": {"team": "d"}},
        "exposes": [
            {
                "exposeId": "main",
                "kind": "table",
                "contract": {"schema": columns},
            }
        ],
    }


# ---------------------------------------------------------------------------
# detect_schema_drift — the pure comparison
# ---------------------------------------------------------------------------


def test_source_type_change_is_flagged():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_SOURCE,
        detect_schema_drift,
    )

    baseline = {"amount": {"name": "amount", "type": "integer"}}
    authored = {"amount": {"name": "amount", "type": "string"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_SOURCE, flag_dropped=False
    )
    assert report.has_drift
    reasons = [d.reason for d in report.drifts]
    assert "type_changed" in reasons
    d = next(d for d in report.drifts if d.reason == "type_changed")
    assert d.column == "amount"
    assert d.baseline_type == "integer"
    assert d.authored_type == "string"


def test_source_rename_is_flagged():
    """A source column that reappears under a renamed identifier is drift."""
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_SOURCE,
        detect_schema_drift,
    )

    baseline = {"customer_id": {"name": "customer_id", "type": "integer"}}
    authored = {"customer_key": {"name": "customer_key", "type": "integer"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_SOURCE, flag_dropped=False
    )
    assert report.has_drift
    d = next(d for d in report.drifts if d.reason == "renamed_column")
    assert d.column == "customer_id"
    assert d.renamed_to == "customer_key"


def test_source_dropped_column_is_not_blocking():
    """Dropping a source column is a legitimate product design choice, not drift."""
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_SOURCE,
        detect_schema_drift,
    )

    baseline = {
        "a": {"name": "a", "type": "integer"},
        "b": {"name": "b", "type": "integer"},
        "c": {"name": "c", "type": "integer"},
    }
    authored = {"a": {"name": "a", "type": "integer"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_SOURCE, flag_dropped=False
    )
    assert not report.has_drift
    # b and c are surfaced as informational notes, not blocking drift.
    joined = " ".join(report.notes)
    assert "b" in joined and "c" in joined


def test_prior_contract_dropped_column_is_blocking():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_schema_drift,
    )

    baseline = {
        "id": {"name": "id", "type": "string"},
        "amount": {"name": "amount", "type": "number"},
    }
    authored = {"id": {"name": "id", "type": "string"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_PRIOR, flag_dropped=True
    )
    assert report.has_drift
    d = next(d for d in report.drifts if d.reason == "dropped_column")
    assert d.column == "amount"


def test_prior_contract_added_column_is_not_blocking():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_schema_drift,
    )

    baseline = {"id": {"name": "id", "type": "string"}}
    authored = {
        "id": {"name": "id", "type": "string"},
        "extra": {"name": "extra", "type": "string"},
    }
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_PRIOR, flag_dropped=True
    )
    assert not report.has_drift
    assert any("extra" in note for note in report.notes)


def test_aligned_contract_has_no_drift():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_schema_drift,
    )

    cols = {
        "id": {"name": "id", "type": "string"},
        "amount": {"name": "amount", "type": "number"},
    }
    report = detect_schema_drift(
        dict(cols), dict(cols), baseline_kind=BASELINE_PRIOR, flag_dropped=True
    )
    assert not report.has_drift
    assert report.drifts == []


def test_type_change_across_unknown_family_is_skipped():
    """Conservative: unknown/ambiguous types never trip a type_changed drift."""
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_schema_drift,
    )

    baseline = {"payload": {"name": "payload", "type": "widgetType"}}
    authored = {"payload": {"name": "payload", "type": "string"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_PRIOR, flag_dropped=True
    )
    assert not report.has_drift


def test_numeric_family_int_vs_number_is_not_drift():
    """integer vs number collapse to one NUMERIC family — no false positive."""
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_schema_drift,
    )

    baseline = {"amount": {"name": "amount", "type": "integer"}}
    authored = {"amount": {"name": "amount", "type": "number"}}
    report = detect_schema_drift(
        baseline, authored, baseline_kind=BASELINE_PRIOR, flag_dropped=True
    )
    assert not report.has_drift


# ---------------------------------------------------------------------------
# detect_authoring_drift — baseline resolution from context / discovery
# ---------------------------------------------------------------------------


class _DiscoveryStub:
    """Minimal DiscoveryReport stand-in (mirrors the self-healing test stub)."""

    def __init__(self, sample_files=None):
        self.sample_files = sample_files or []
        self.sql_files = []
        self.user_data_models = []
        self.detected_sources = []
        self.provider_hints = []
        self.existing_contracts = []
        self.dbt_projects = []
        self.readmes = []
        self.build_constraints = []
        self.discovery_warnings = []
        self.templates = []
        self.warnings = []
        self.notes = []
        self.sample_data_missing = not bool(sample_files)
        self.authoring_mode = "flat"
        self.workspace_roots = ["/tmp/ws"]

    def to_prompt_payload(self):
        return {}


def test_detect_authoring_drift_prefers_prior_contract():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_PRIOR,
        detect_authoring_drift,
    )

    prior = _contract_with_columns([{"name": "amount", "type": "number"}])
    prior["kind"] = "DataProduct"
    context = {"seed_contract_override": prior}
    discovery = _DiscoveryStub(sample_files=[{"path": "s.csv", "columns": {"amount": "integer"}}])
    # Authored drops the prior 'amount' column.
    authored = _contract_with_columns([{"name": "id", "type": "string"}])
    report = detect_authoring_drift(context, discovery, authored)
    assert report is not None
    assert report.baseline_kind == BASELINE_PRIOR
    assert report.has_drift


def test_detect_authoring_drift_falls_back_to_source():
    from fluid_build.cli.forge_copilot_drift_guard import (
        BASELINE_SOURCE,
        detect_authoring_drift,
    )

    context = {}
    discovery = _DiscoveryStub(sample_files=[{"path": "s.csv", "columns": {"amount": "integer"}}])
    # Authored retypes the source 'amount' column string -> drift.
    authored = _contract_with_columns([{"name": "amount", "type": "string"}])
    report = detect_authoring_drift(context, discovery, authored)
    assert report is not None
    assert report.baseline_kind == BASELINE_SOURCE
    assert report.has_drift


def test_detect_authoring_drift_returns_none_without_baseline():
    from fluid_build.cli.forge_copilot_drift_guard import detect_authoring_drift

    report = detect_authoring_drift({}, _DiscoveryStub(), _contract_with_columns([]))
    assert report is None


# ---------------------------------------------------------------------------
# drift_guard_enabled — opt-in, non-breaking
# ---------------------------------------------------------------------------


def test_drift_guard_disabled_by_default():
    from fluid_build.cli.forge_copilot_drift_guard import drift_guard_enabled

    with mock.patch.dict(os.environ, {}, clear=True):
        assert drift_guard_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "on", "yes", "TRUE"])
def test_drift_guard_enabled_by_env(val):
    from fluid_build.cli.forge_copilot_drift_guard import drift_guard_enabled

    with mock.patch.dict(os.environ, {"FLUID_FORGE_DRIFT_GUARD": val}, clear=True):
        assert drift_guard_enabled() is True


# ---------------------------------------------------------------------------
# build_semantic_drift_message — the corrective feedback
# ---------------------------------------------------------------------------


def test_build_semantic_drift_message_names_columns_and_instructs_restore():
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_semantic_drift_message,
    )

    drifts = [
        {
            "column": "amount",
            "reason": "dropped_column",
            "baseline_type": "number",
            "authored_type": None,
            "renamed_to": None,
        },
        {
            "column": "customer_id",
            "reason": "renamed_column",
            "baseline_type": "integer",
            "authored_type": "integer",
            "renamed_to": "customer_key",
        },
        {
            "column": "ts",
            "reason": "type_changed",
            "baseline_type": "timestamp",
            "authored_type": "string",
            "renamed_to": None,
        },
    ]
    msg = build_semantic_drift_message(drifts, baseline_kind="prior contract")
    assert msg["role"] == "user"
    body = msg["content"]
    # Each drifted column is named.
    assert "amount" in body
    assert "customer_id" in body
    assert "customer_key" in body
    assert "ts" in body
    # The baseline is named so the LLM knows what to reconcile against.
    assert "prior contract" in body.lower()
    # Forceful restore / do-not-rename instruction.
    body_lower = body.lower()
    assert "restore" in body_lower or "do not rename" in body_lower
    assert "rename" in body_lower


def test_build_semantic_drift_message_empty_is_blank():
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_semantic_drift_message,
    )

    msg = build_semantic_drift_message([], baseline_kind="source schema")
    assert msg["content"] == ""


# ---------------------------------------------------------------------------
# Runtime wiring — drift feeds the self-healing repair loop
# ---------------------------------------------------------------------------


def test_drift_guard_feeds_repair_loop_when_enabled():
    """With the guard enabled, an authored contract that drops a prior-contract
    column must inject drift feedback into the NEXT attempt's repair prompt.

    Mirrors ``test_self_healing_runs_schema_validator_in_repair_loop`` — the
    LLM is stubbed to emit the drifting contract every time; we assert the
    2nd attempt's user prompt mentions the drift / the dropped column.
    """
    from fluid_build.cli import forge_copilot_runtime as rt
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    # Prior contract (refine seed) has an 'amount' column.
    prior = _contract_with_columns(
        [
            {"name": "id", "type": "string"},
            {"name": "amount", "type": "number"},
        ]
    )
    # The authored contract DROPS 'amount' — that is drift vs the prior contract.
    authored = _contract_with_columns([{"name": "id", "type": "string"}])

    def _stub_llm(*_args, **_kwargs):
        return (
            '{"contract": '
            + json.dumps(authored)
            + ', "suggestions": {}, "readme_markdown": "", "additional_files": {}}'
        )

    captured_user_prompts: List[str] = []
    real_build_user_prompt = rt.build_user_prompt

    def _spy_user_prompt(**kwargs):
        prompt = real_build_user_prompt(**kwargs)
        captured_user_prompts.append(prompt)
        return prompt

    # Make validation pass so ONLY the drift guard drives the retry.
    def _stub_validate(normalized, **kwargs):
        return ([], [])

    class _MockSchemaResult:
        is_valid = True
        errors: List[str] = []

    def _stub_schema_validate(self_sm, contract):
        return _MockSchemaResult()

    from fluid_build import schema_manager as _sm_module

    with (
        mock.patch.dict(
            os.environ,
            {"FLUID_FORGE_LEGACY_COPILOT": "1", "FLUID_FORGE_DRIFT_GUARD": "1"},
            clear=False,
        ),
        mock.patch.object(rt, "_call_llm_with_optional_streaming", _stub_llm),
        mock.patch.object(rt, "build_user_prompt", _spy_user_prompt),
        mock.patch.object(rt, "validate_generated_result", _stub_validate),
        mock.patch.object(rt, "_self_evaluate_contract", return_value=None),
        mock.patch.object(
            _sm_module.FluidSchemaManager, "validate_contract", _stub_schema_validate
        ),
    ):
        try:
            rt.generate_copilot_artifacts(
                context={"project_goal": "x", "seed_contract_override": prior},
                llm_config=LlmConfig(
                    provider="openai",
                    model="gpt-4o",
                    endpoint="https://api.openai.com/v1/chat/completions",
                    api_key="test",
                ),
                discovery_report=_DiscoveryStub(),
                max_attempts=2,
            )
        except Exception:
            # The contract keeps drifting, so the run raises after retries — fine.
            pass

    assert len(captured_user_prompts) >= 2, (
        f"Expected the guard to force a 2nd attempt, got {len(captured_user_prompts)} "
        "prompt(s) — drift did not feed the repair loop."
    )
    second = captured_user_prompts[1].lower()
    assert "drift" in second or "amount" in second, (
        "The 2nd attempt's repair prompt must carry the semantic-drift feedback "
        f"(dropped 'amount'). Got: {captured_user_prompts[1][:600]}"
    )


def test_drift_guard_noop_when_disabled():
    """With the guard OFF (default), a drifting contract does NOT force a retry."""
    from fluid_build.cli import forge_copilot_runtime as rt
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    prior = _contract_with_columns(
        [{"name": "id", "type": "string"}, {"name": "amount", "type": "number"}]
    )
    authored = _contract_with_columns([{"name": "id", "type": "string"}])

    def _stub_llm(*_args, **_kwargs):
        return (
            '{"contract": '
            + json.dumps(authored)
            + ', "suggestions": {}, "readme_markdown": "", "additional_files": {}}'
        )

    prompts: List[str] = []
    real_bup = rt.build_user_prompt

    def _spy(**kwargs):
        p = real_bup(**kwargs)
        prompts.append(p)
        return p

    def _stub_validate(normalized, **kwargs):
        return ([], [])

    class _MockSchemaResult:
        is_valid = True
        errors: List[str] = []

    def _stub_schema_validate(self_sm, contract):
        return _MockSchemaResult()

    from fluid_build import schema_manager as _sm_module

    # Ensure the env var is absent so the guard is disabled.
    env = {k: v for k, v in os.environ.items() if k != "FLUID_FORGE_DRIFT_GUARD"}
    env["FLUID_FORGE_LEGACY_COPILOT"] = "1"

    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(rt, "_call_llm_with_optional_streaming", _stub_llm),
        mock.patch.object(rt, "build_user_prompt", _spy),
        mock.patch.object(rt, "validate_generated_result", _stub_validate),
        mock.patch.object(rt, "_self_evaluate_contract", return_value=None),
        mock.patch.object(rt, "_enrich_contract", return_value=None),
        mock.patch.object(rt, "_judge_contract", return_value=None),
        mock.patch.object(
            _sm_module.FluidSchemaManager, "validate_contract", _stub_schema_validate
        ),
    ):
        result = rt.generate_copilot_artifacts(
            context={"project_goal": "x", "seed_contract_override": prior},
            llm_config=LlmConfig(
                provider="openai",
                model="gpt-4o",
                endpoint="https://api.openai.com/v1/chat/completions",
                api_key="test",
            ),
            discovery_report=_DiscoveryStub(),
            max_attempts=2,
        )

    # Guard off => valid contract accepted on attempt 1, no forced retry.
    assert result is not None
    assert len(prompts) == 1
