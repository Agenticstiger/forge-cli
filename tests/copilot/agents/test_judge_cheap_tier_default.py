# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""JudgeAgent model resolution — catalog cheap tier wins over flagship.

The judge runs on every synthesis pass (and again under self-critique).
Defaulting to the flagship model would eat 30-50% of a run's token
budget. The resolution ladder (catalog-driven; no hardcoded fallback
map — ``cli/llm_models.json`` is the single source of truth, kept
fresh weekly by ``.github/workflows/update-model-catalog.yml``):

    1. explicit constructor arg (operator per-call override)
    2. catalog *explicit* ``judge`` tier (override-file knob)
    3. catalog *explicit* ``fast`` tier (haiku/flash/nano per provider)
    4. run primary model (last resort)

The "explicit" qualifier matters because ``get_catalog_tier_model``
silently falls back to the flagship when a tier isn't defined. The
:func:`_explicit_catalog_tier_or_none` helper detects that fall-through
and returns ``None`` so the ladder progresses instead of escalating to
the most expensive model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fluid_build.copilot.agents.judge_agent import (
    AxisScore,
    JudgeAgent,
    JudgeResult,
    _explicit_catalog_tier_or_none,
)


def _result_with_total(total: int = 24, model: str = "ignored") -> JudgeResult:
    return JudgeResult(
        axes={axis: AxisScore(score=4, reasoning="") for axis in JudgeAgent.AXES},
        total=total,
        model=model,
    )


def _mocked_judge_run(
    judge_agent: JudgeAgent,
    *,
    catalog_tiers_section: dict | None = None,
    catalog_provider_keys: dict | None = None,
):
    """Drive ``JudgeAgent.judge`` through one round-trip with all I/O mocked.

    Returns the resolved ``judge_model`` that was passed to the inner
    ``dataclasses.replace`` (and thus the model the actual LLM call used).

    ``catalog_tiers_section`` populates ``catalog["tiers"]["anthropic"]``
    so we can exercise the explicit-tier probe without monkeypatching the
    helper directly.
    """
    catalog = {
        "tiers": {"anthropic": catalog_tiers_section or {}},
        "providers": {"anthropic": catalog_provider_keys or {}},
    }

    def _fake_load_catalog():
        return catalog

    captured = {"model": None}

    def _capture_replace(config, **overrides):
        captured["model"] = overrides.get("model")
        return config

    with (
        patch("fluid_build.cli.forge_copilot_llm_providers.resolve_llm_config") as fake_resolve,
        patch(
            "fluid_build.cli._llm_model_catalog._resolve_load_model_catalog",
            return_value=_fake_load_catalog,
        ),
        patch("fluid_build.cli.forge_copilot_llm_providers.get_llm_provider") as fake_provider,
        patch(
            "fluid_build.cli.forge_copilot_llm_providers.call_llm",
            return_value='{"axes":{"correctness":{"score":4,"reasoning":""},'
            '"completeness":{"score":4,"reasoning":""},'
            '"security":{"score":4,"reasoning":""},'
            '"governance":{"score":4,"reasoning":""},'
            '"performance":{"score":4,"reasoning":""},'
            '"documentation":{"score":4,"reasoning":""}}}',
        ),
        patch("dataclasses.replace", side_effect=_capture_replace),
    ):
        fake_cfg = MagicMock()
        fake_cfg.provider = "anthropic"
        fake_cfg.model = "claude-opus-4-7"
        fake_resolve.return_value = fake_cfg
        fake_provider.return_value = MagicMock()
        # Disable self-critique to keep this test single-LLM-call.
        with patch.dict(
            "os.environ",
            {"FLUID_JUDGE_SELF_CRITIQUE": "0"},
            clear=False,
        ):
            judge_agent.judge({"fluidVersion": "0.7.3", "exposes": []})
    return captured["model"]


def test_explicit_model_wins_over_everything():
    agent = JudgeAgent(model="claude-sonnet-4-5")
    model = _mocked_judge_run(
        agent,
        catalog_tiers_section={"judge": "claude-haiku-4-5", "fast": "claude-haiku-4-5"},
    )
    assert model == "claude-sonnet-4-5"


def test_explicit_catalog_judge_tier_wins_over_fast():
    agent = JudgeAgent()
    model = _mocked_judge_run(
        agent,
        catalog_tiers_section={
            "judge": "claude-sonnet-4-5-cheap-judge-override",
            "fast": "should-not-be-used",
        },
    )
    assert model == "claude-sonnet-4-5-cheap-judge-override"


def test_catalog_fast_tier_wins_when_no_judge_tier():
    """No explicit judge tier in the catalog → catalog 'fast' tier wins."""
    agent = JudgeAgent()
    model = _mocked_judge_run(
        agent,
        catalog_tiers_section={"fast": "claude-haiku-4-5-20251001"},
    )
    assert model == "claude-haiku-4-5-20251001"


def test_primary_model_fallback_when_catalog_silent():
    """No catalog tiers, no explicit override → primary model is the
    last-resort fallback. NOT a hardcoded haiku — the ladder is
    catalog-driven so a provider rename can't silently regress us to a
    deprecated model id."""
    agent = JudgeAgent()
    model = _mocked_judge_run(agent, catalog_tiers_section={})
    assert model == "claude-opus-4-7"  # the primary from the fake config


def test_explicit_catalog_tier_helper_returns_none_when_tier_undefined():
    """The helper that protects against the silent-flagship-fallback bug."""
    fake_catalog = {"tiers": {"anthropic": {}}, "providers": {"anthropic": {}}}
    with patch(
        "fluid_build.cli._llm_model_catalog._resolve_load_model_catalog",
        return_value=lambda: fake_catalog,
    ):
        assert _explicit_catalog_tier_or_none("anthropic", "judge") is None
        assert _explicit_catalog_tier_or_none("anthropic", "fast") is None


def test_explicit_catalog_tier_helper_returns_value_when_tier_defined():
    """The helper returns the value when the tier is explicitly defined."""
    fake_catalog = {
        "tiers": {"anthropic": {"fast": "claude-haiku-4-5-20251001"}},
        "providers": {"anthropic": {}},
    }
    with patch(
        "fluid_build.cli._llm_model_catalog._resolve_load_model_catalog",
        return_value=lambda: fake_catalog,
    ):
        assert _explicit_catalog_tier_or_none("anthropic", "fast") == "claude-haiku-4-5-20251001"
        assert _explicit_catalog_tier_or_none("anthropic", "judge") is None  # not defined


def test_explicit_catalog_tier_helper_reads_provider_keys_too():
    """Older catalog shape: providers[provider][tier] = model."""
    fake_catalog = {
        "tiers": {"anthropic": {}},
        "providers": {"anthropic": {"fast": "claude-haiku-from-provider-keys"}},
    }
    with patch(
        "fluid_build.cli._llm_model_catalog._resolve_load_model_catalog",
        return_value=lambda: fake_catalog,
    ):
        assert (
            _explicit_catalog_tier_or_none("anthropic", "fast") == "claude-haiku-from-provider-keys"
        )
