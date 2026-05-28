# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""JudgeAgent + post-synthesis enrichment artifacts."""

from __future__ import annotations

from fluid_build.copilot.agents.judge_agent import JudgeAgent

SAMPLE_CONTRACT = {
    "fluidVersion": "0.7.3",
    "kind": "DataProduct",
    "id": "x.y.sample",
    "name": "sample",
    "domain": "x",
    "metadata": {"layer": "Bronze", "productType": "SDP"},
    "exposes": [],
}


SAMPLE_ARTIFACTS = {
    "provider": "snowflake",
    "refresh_cadence": "hourly",
    "dbt_tests": [
        {
            "version": 2,
            "models": [
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "tests": ["unique", "not_null"]},
                    ],
                }
            ],
        }
    ],
    "freshness": {
        "warn_after": {"count": 90, "period": "minute"},
        "error_after": {"count": 3, "period": "hour"},
        "filter": None,
    },
    "physical_layout": [
        {
            "clustering_keys": ["customer_id"],
            "partition_by": "created_at",
            "partition_grain": "day",
            "materialization_hint": "table",
        }
    ],
}


def test_prompt_omits_artifact_block_when_none():
    """No artifacts ⇒ prompt is identical to the contract-only path."""
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts=None)
    assert "Deterministic-enrichment outputs" not in user


def test_prompt_omits_artifact_block_when_empty_dict():
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts={})
    assert "Deterministic-enrichment outputs" not in user


def test_prompt_omits_artifact_block_when_all_fields_empty():
    """A populated dict with only empty values still suppresses the block."""
    empty_artifacts = {
        "provider": None,
        "dbt_tests": [],
        "freshness": {},
        "physical_layout": [],
    }
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts=empty_artifacts)
    assert "Deterministic-enrichment outputs" not in user


def test_prompt_includes_artifact_block_when_populated():
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts=SAMPLE_ARTIFACTS)
    assert "Deterministic-enrichment outputs" in user
    # Block contents must appear so the judge can reason about them.
    assert "snowflake" in user
    assert "warn_after" in user
    assert "clustering_keys" in user
    # The rubric instruction is the load-bearing bit — it tells the
    # judge to credit performance/governance/documentation when the
    # enrichment fills in fields the raw contract doesn't carry.
    assert "treat them as if already applied" in user


def test_prompt_artifact_block_includes_rubric_instruction():
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts=SAMPLE_ARTIFACTS)
    assert "performance" in user.lower()
    assert "governance" in user.lower()
    assert "documentation" in user.lower()


def test_prompt_artifact_block_renders_yaml_not_python_repr():
    """Artifacts must render as readable YAML, not Python's str() output."""
    agent = JudgeAgent()
    _system, user = agent._build_prompt(SAMPLE_CONTRACT, build_artifacts=SAMPLE_ARTIFACTS)
    # Python's dict str() would give us "{'provider': 'snowflake', ...}"
    # YAML gives us "provider: snowflake" with proper indentation.
    assert "provider: snowflake" in user
    assert "{'provider'" not in user
