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

"""Unit tests for :mod:`fluid_build.copilot.agents.judge_agent`.

Pins:

* :data:`JudgeAgent.AXES` matches the rubric spec exactly.
* Well-formed JSON parses into :class:`JudgeResult` with all 6 axes
  and ``total == sum(axis.score)``.
* Malformed JSON raises :class:`JudgeAgent.ParseError`.
* Score-out-of-range / missing axis / non-int score all raise ParseError.
* With an explicit ``run_id`` and a workspace-rooted cwd, the
  ``judge.json`` lands under ``.fluid/agents/<run_id>/``.
* :meth:`JudgeAgent._build_prompt` mentions every axis in the system
  prompt (smoke test).

These pins protect the integration call site: the coordinator (or
CLI hook) will instantiate JudgeAgent, call ``judge(contract,
run_id=...)``, and trust the persisted file's path/shape.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch

import pytest

from fluid_build.copilot.agents.judge_agent import (
    JudgeAgent,
    JudgeResult,
)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _well_formed_judge_response() -> str:
    """A canonical valid LLM response — 6 axes, mixed scores."""
    return json.dumps(
        {
            "axes": {
                "correctness": {
                    "score": 4,
                    "reasoning": "Types match the sample; one numeric is STRING.",
                    "suggestions": ["Retype amount_cents as INT64."],
                },
                "completeness": {
                    "score": 3,
                    "reasoning": "Owner and SLA set; 4 column descriptions missing.",
                    "suggestions": ["Add descriptions for: customer_id, created_at, status, tier."],
                },
                "security": {
                    "score": 5,
                    "reasoning": "PII tagged; email masked in exposes[].",
                    "suggestions": [],
                },
                "governance": {
                    "score": 4,
                    "reasoning": "Owner + retention set; access policies thin.",
                    "suggestions": ["Add an explicit access role for analytics."],
                },
                "performance": {
                    "score": 2,
                    "reasoning": "No clustering on the largest table.",
                    "suggestions": ["Cluster on event_date."],
                },
                "documentation": {
                    "score": 3,
                    "reasoning": "README present but column descriptions sparse.",
                    "suggestions": ["Expand the README's 'how to consume' section."],
                },
            }
        }
    )


_FAKE_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.3",
    "id": "orders_v1",
    "metadata": {
        "owner": "data-platform@example.com",
        "domain": "commerce",
        "layer": "Silver",
        "productType": "ADP",
    },
    "exposes": [
        {
            "name": "orders",
            "schema": [
                {"name": "order_id", "type": "STRING"},
                {"name": "amount_cents", "type": "INT64"},
            ],
        }
    ],
}


def _stub_llm_config():
    """Build a real LlmConfig stub without hitting any network or env."""
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    return LlmConfig(
        provider="openai",
        model="gpt-4.1-mini",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="test-key",
    )


# ---------------------------------------------------------------------
# Axes spec pin
# ---------------------------------------------------------------------


class TestAxesSpec:
    def test_axes_match_spec_exactly(self):
        # The 6-axis rubric is the API contract — downstream UIs +
        # regression diffs key on this order. Any reorder/insert is a
        # breaking change.
        assert JudgeAgent.AXES == [
            "correctness",
            "completeness",
            "security",
            "governance",
            "performance",
            "documentation",
        ]


# ---------------------------------------------------------------------
# Prompt-composition smoke test
# ---------------------------------------------------------------------


class TestPromptComposition:
    def test_all_six_axes_named_in_system_prompt(self):
        agent = JudgeAgent()
        system_prompt, user_prompt = agent._build_prompt(_FAKE_CONTRACT)
        for axis in JudgeAgent.AXES:
            assert axis in system_prompt, f"axis {axis!r} missing from system prompt"
        # User prompt embeds the contract.
        assert "orders_v1" in user_prompt
        # Reasoning-before-score is explicit in the instructions.
        assert "reasoning" in system_prompt.lower()
        assert "before" in system_prompt.lower()


# ---------------------------------------------------------------------
# Happy-path parse
# ---------------------------------------------------------------------


class TestWellFormedResponse:
    def test_parse_returns_all_axes_and_correct_total(self, tmp_path, monkeypatch):
        # cwd-isolate so the best-effort persistence lands under tmp_path.
        monkeypatch.chdir(tmp_path)

        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=_well_formed_judge_response(),
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="test-run-001")

        assert isinstance(result, JudgeResult)
        assert set(result.axes.keys()) == set(JudgeAgent.AXES)
        # 4 + 3 + 5 + 4 + 2 + 3 = 21
        assert result.total == 21
        assert result.total == sum(a.score for a in result.axes.values())
        # Model recorded for comparability across runs.
        assert result.model == "judge-model-x"
        # run_id from the explicit kwarg is preserved end-to-end.
        assert result.run_id == "test-run-001"

    def test_markdown_fenced_response_still_parses(self, tmp_path, monkeypatch):
        # Some providers wrap JSON in markdown ```json fences; the
        # safe_json_parse() utility handles that — pin it here too.
        monkeypatch.chdir(tmp_path)

        fenced = "Here is my evaluation:\n\n```json\n" + _well_formed_judge_response() + "\n```\n"
        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=fenced,
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="test-run-002")

        assert result.total == 21


# ---------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------


class TestParseErrors:
    def _judge_with_response(self, response_text: str, monkeypatch, tmp_path) -> JudgeAgent:
        monkeypatch.chdir(tmp_path)
        agent = JudgeAgent(model="judge-model-x")
        # Stash patches on the test instance so the caller can run judge().
        self._patches = [
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=response_text,
            ),
        ]
        for p in self._patches:
            p.start()
        return agent

    def teardown_method(self) -> None:
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_malformed_json_raises_parse_error(self, tmp_path, monkeypatch):
        agent = self._judge_with_response(
            "this is not JSON at all, just prose",
            monkeypatch,
            tmp_path,
        )
        with pytest.raises(JudgeAgent.ParseError):
            agent.judge(_FAKE_CONTRACT, run_id="err-001")

    def test_missing_axes_key_raises_parse_error(self, tmp_path, monkeypatch):
        agent = self._judge_with_response(
            json.dumps({"score": 30, "reasoning": "looks fine"}),
            monkeypatch,
            tmp_path,
        )
        with pytest.raises(JudgeAgent.ParseError):
            agent.judge(_FAKE_CONTRACT, run_id="err-002")

    def test_missing_one_axis_raises_parse_error(self, tmp_path, monkeypatch):
        # Drop 'performance' to trigger the missing-axis branch.
        payload = json.loads(_well_formed_judge_response())
        del payload["axes"]["performance"]
        agent = self._judge_with_response(json.dumps(payload), monkeypatch, tmp_path)
        with pytest.raises(JudgeAgent.ParseError):
            agent.judge(_FAKE_CONTRACT, run_id="err-003")

    def test_score_out_of_range_raises_parse_error(self, tmp_path, monkeypatch):
        payload = json.loads(_well_formed_judge_response())
        payload["axes"]["security"]["score"] = 7  # invalid: outside 0..5
        agent = self._judge_with_response(json.dumps(payload), monkeypatch, tmp_path)
        with pytest.raises(JudgeAgent.ParseError):
            agent.judge(_FAKE_CONTRACT, run_id="err-004")

    def test_non_integer_score_raises_parse_error(self, tmp_path, monkeypatch):
        payload = json.loads(_well_formed_judge_response())
        payload["axes"]["correctness"]["score"] = "high"
        agent = self._judge_with_response(json.dumps(payload), monkeypatch, tmp_path)
        with pytest.raises(JudgeAgent.ParseError):
            agent.judge(_FAKE_CONTRACT, run_id="err-005")


# ---------------------------------------------------------------------
# Persistence — write judge.json under .fluid/agents/<run-id>/
# ---------------------------------------------------------------------


class TestPersistence:
    def test_writes_judge_json_under_run_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Defensively clear any env-var-supplied run-id so the kwarg
        # is the only source of truth.
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)

        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=_well_formed_judge_response(),
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="test-run-001")

        target = tmp_path / ".fluid" / "agents" / "test-run-001" / "judge.json"
        assert target.is_file(), f"expected judge.json at {target}"

        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert set(on_disk["axes"].keys()) == set(JudgeAgent.AXES)
        assert on_disk["total"] == result.total
        assert on_disk["model"] == "judge-model-x"
        assert on_disk["run_id"] == "test-run-001"
        # Round-trip an axis fully — score + reasoning + suggestions all
        # land in the persisted file as written.
        sec = on_disk["axes"]["security"]
        assert sec["score"] == 5
        assert "masked" in sec["reasoning"]
        assert sec["suggestions"] == []

    def test_persistence_failure_does_not_break_judge(self, tmp_path, monkeypatch):
        # Spec: persistence is best-effort. A write failure must NOT
        # raise into the caller; the JudgeResult must still come back.
        monkeypatch.chdir(tmp_path)
        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=_well_formed_judge_response(),
            ),
            patch(
                "fluid_build.copilot.agents.judge_agent.JudgeAgent._persist",
                side_effect=OSError("disk full"),
            ),
        ):
            # Spec: judge() catches persistence errors at the call site
            # and logs at DEBUG. We re-patch _persist to raise so we can
            # confirm the swallow. Note that judge() catches Exception
            # around the _persist call.
            result = agent.judge(_FAKE_CONTRACT, run_id="persist-fail-001")

        assert isinstance(result, JudgeResult)
        assert result.total == 21


# ---------------------------------------------------------------------
# Cost-receipt persistence — write cost.json alongside judge.json
# ---------------------------------------------------------------------
#
# P1d follow-up: ``RunCostTracker.persist_to_run_dir`` is the single
# chokepoint that makes a run visible to ``fluid stats``. It was wired
# into the from-source / from-intent / from-ddl paths but NOT into
# ``JudgeAgent.judge()``. Live OpenAI smoke confirmed: tokens + USD
# accumulate correctly in the in-memory tracker, but ``cost.json``
# never lands on disk, so ``fluid stats`` reported zero judge-only
# runs. These pins close the gap.


class TestCostReceiptPersistence:
    def test_cost_json_written_alongside_judge_json(self, tmp_path, monkeypatch):
        """``judge()`` MUST emit ``cost.json`` next to ``judge.json``.

        Without this, a judge-only run (no ``fluid forge`` / from-source
        wrapper) is invisible to ``fluid stats``.
        """
        from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        # Pin: fresh tracker so the test's recorded tokens aren't
        # mixed in with anything stale from earlier tests.
        reset_run_tracker()

        agent = JudgeAgent(model="judge-model-x")

        # ``call_llm`` doesn't actually feed the tracker in our mock —
        # simulate the real provider path by recording known token
        # counts BEFORE the persist call.
        def fake_call_llm(*args, **kwargs):
            get_run_tracker().record_call(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=1847,
                output_tokens=421,
            )
            return _well_formed_judge_response()

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="cost-run-001")

        cost_path = tmp_path / ".fluid" / "agents" / "cost-run-001" / "cost.json"
        judge_path = tmp_path / ".fluid" / "agents" / "cost-run-001" / "judge.json"
        # Both receipts MUST land in the same per-run directory so
        # ``fluid stats`` and the judge-quality UI line up.
        assert cost_path.is_file(), f"expected cost.json at {cost_path}"
        assert judge_path.is_file(), f"expected judge.json at {judge_path}"

    def test_cost_json_carries_token_counts(self, tmp_path, monkeypatch):
        """Persisted ``cost.json`` MUST reflect the tracker's running
        totals — input / output token counts that the real provider
        recorded during the judge call.
        """
        from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        # Disable critique — default-ON would fire a second LLM call
        # and double-count tokens against our pinned expectations.
        # The critique-pass cost is tested in test_judge_self_critique.
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")
        reset_run_tracker()

        agent = JudgeAgent(model="judge-model-x")

        def fake_call_llm(*args, **kwargs):
            # Numbers chosen to match the live OpenAI smoke from
            # the P1d hand-off note (1847 in / 421 out).
            get_run_tracker().record_call(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=1847,
                output_tokens=421,
            )
            return _well_formed_judge_response()

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="cost-run-tokens")

        cost_path = tmp_path / ".fluid" / "agents" / "cost-run-tokens" / "cost.json"
        on_disk = json.loads(cost_path.read_text(encoding="utf-8"))
        assert on_disk["input_tokens"] == 1847
        assert on_disk["output_tokens"] == 421
        assert on_disk["total_tokens"] == 1847 + 421
        assert on_disk["total_calls"] == 1
        # ``total_usd`` should be priced from the embedded table for
        # gpt-4.1-mini (or from litellm if installed) — either way it
        # MUST be non-None when one row with known price was recorded.
        assert on_disk["total_usd"] is not None
        assert on_disk["total_usd"] > 0
        # ``wall_clock_seconds`` is non-negative (we don't pin a
        # specific value — it depends on host clock).
        assert on_disk["wall_clock_seconds"] >= 0.0

    def test_cost_json_mode_is_llm_when_calls_recorded(self, tmp_path, monkeypatch):
        """``mode`` MUST be the provider/model identity (not
        ``deterministic``) when at least one LLM call was recorded.
        """
        from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        # Single-LLM-call run so ``mode`` is a clean ``provider/model``
        # not ``"mixed"`` — disable the critique pass.
        monkeypatch.setenv("FLUID_JUDGE_SELF_CRITIQUE", "0")
        reset_run_tracker()

        agent = JudgeAgent(model="judge-model-x")

        def fake_call_llm(*args, **kwargs):
            get_run_tracker().record_call(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=100,
                output_tokens=50,
            )
            return _well_formed_judge_response()

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            agent.judge(_FAKE_CONTRACT, run_id="cost-run-mode")

        cost_path = tmp_path / ".fluid" / "agents" / "cost-run-mode" / "cost.json"
        on_disk = json.loads(cost_path.read_text(encoding="utf-8"))
        # Single-row run: ``mode`` is the ``provider/model`` shape,
        # NOT ``"deterministic"`` (the latter is reserved for zero-call
        # runs so ``fluid stats`` can distinguish heuristic from LLM).
        assert on_disk["mode"] != "deterministic"
        assert "openai" in on_disk["mode"]
        assert "gpt-4.1-mini" in on_disk["mode"]

    def test_cost_receipt_failure_does_not_break_judge(self, tmp_path, monkeypatch):
        """Spec: cost-receipt persistence is best-effort. A write
        failure MUST NOT raise — the :class:`JudgeResult` must still
        come back to the caller, and the existing ``judge.json``
        write must still land.
        """
        from fluid_build.copilot.cost import reset_run_tracker

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_RUN_ID", raising=False)
        reset_run_tracker()

        agent = JudgeAgent(model="judge-model-x")

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config",
                return_value=_stub_llm_config(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_llm_provider",
                return_value=object(),
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.get_catalog_tier_model",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.call_llm",
                return_value=_well_formed_judge_response(),
            ),
            patch(
                "fluid_build.copilot.agents.judge_agent.JudgeAgent._persist_cost_receipt",
                side_effect=OSError("disk full"),
            ),
        ):
            result = agent.judge(_FAKE_CONTRACT, run_id="cost-fail-001")

        # JudgeResult comes back intact.
        assert isinstance(result, JudgeResult)
        assert result.total == 21
        # ``judge.json`` still landed (independent of the cost-receipt
        # failure — they're under separate try/except blocks).
        judge_path = tmp_path / ".fluid" / "agents" / "cost-fail-001" / "judge.json"
        assert judge_path.is_file()
