# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Frozen-string prompt snapshots for :meth:`JudgeAgent._build_prompt`.

Borrowed pattern: pytest-snapshot / Jest snapshots / OpenAI evals'
fixed-string assertions. The contract YAML is the input; the
``(system_prompt, user_prompt)`` tuple is captured byte-for-byte to a
``prompt_snapshot.txt`` file inside each case's directory.

This catches ANY drift to the prompt template — axis additions, rubric
wording tweaks, one-shot example edits, ordering changes. A failing
snapshot means the operator must:

* Re-confirm the prompt change is intentional, then
* Re-run with ``JUDGE_UPDATE_SNAPSHOTS=1`` to write the new snapshot.

The env-var update path is explicit (not silent auto-update) so the
operator opts in to every refresh.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml

from fluid_build.copilot.agents.judge_agent import JudgeAgent

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "judge_eval_set"

# Sentinel separating the system prompt from the user prompt inside the
# single .txt fixture. Chosen to be obviously-non-prompt text so an
# accidental prompt edit can't introduce it.
_SNAPSHOT_SEPARATOR = "\n\n===== USER PROMPT =====\n\n"


def _discover_cases() -> List[Tuple[str, Path]]:
    cases: List[Tuple[str, Path]] = []
    for case_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not case_dir.is_dir():
            continue
        contract_path = case_dir / "contract.fluid.yaml"
        if contract_path.is_file():
            cases.append((case_dir.name, contract_path))
    return cases


CASES = _discover_cases()


def _render_snapshot(system_prompt: str, user_prompt: str) -> str:
    return f"===== SYSTEM PROMPT ====={chr(10)}{chr(10)}{system_prompt}{_SNAPSHOT_SEPARATOR}{user_prompt}"


def _should_update_snapshots() -> bool:
    val = os.environ.get("JUDGE_UPDATE_SNAPSHOTS", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


@pytest.mark.unit
class TestPromptSnapshots:
    """Frozen-string per-case snapshot of the prompt builder."""

    @pytest.mark.parametrize("case_name,contract_path", CASES, ids=[c[0] for c in CASES])
    def test_prompt_matches_snapshot(self, case_name: str, contract_path: Path) -> None:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        agent = JudgeAgent()
        system_prompt, user_prompt = agent._build_prompt(contract)
        current = _render_snapshot(system_prompt, user_prompt)

        snapshot_path = contract_path.parent / "prompt_snapshot.txt"

        if _should_update_snapshots():
            # WHY: explicit opt-in update path — running tests in CI with
            # this env unset enforces the freeze.
            snapshot_path.write_text(current, encoding="utf-8")
            pytest.skip(f"updated snapshot at {snapshot_path}")

        if not snapshot_path.is_file():
            pytest.fail(
                f"Snapshot missing at {snapshot_path}. "
                f"Run with JUDGE_UPDATE_SNAPSHOTS=1 to create it."
            )

        recorded = snapshot_path.read_text(encoding="utf-8")
        assert current == recorded, (
            f"Prompt drift detected for case {case_name!r}. "
            f"If the change is intentional, re-run with "
            f"JUDGE_UPDATE_SNAPSHOTS=1 to refresh {snapshot_path}."
        )


@pytest.mark.unit
class TestPromptInvariants:
    """Cross-case invariants the prompt builder must satisfy."""

    @pytest.mark.parametrize("case_name,contract_path", CASES, ids=[c[0] for c in CASES])
    def test_every_axis_named_in_system_prompt(self, case_name: str, contract_path: Path) -> None:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        system_prompt, _ = JudgeAgent()._build_prompt(contract)
        for axis in JudgeAgent.AXES:
            assert (
                axis in system_prompt
            ), f"axis {axis!r} missing from system prompt for case {case_name!r}"

    @pytest.mark.parametrize("case_name,contract_path", CASES, ids=[c[0] for c in CASES])
    def test_contract_id_appears_in_user_prompt(self, case_name: str, contract_path: Path) -> None:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        _, user_prompt = JudgeAgent()._build_prompt(contract)
        contract_id = contract.get("id") or ""
        assert (
            contract_id and contract_id in user_prompt
        ), f"contract id {contract_id!r} missing from user prompt for {case_name!r}"

    def test_artifacts_block_only_added_when_supplied(self) -> None:
        # Sanity-check: build_artifacts=None vs build_artifacts={} vs
        # build_artifacts={"dbt_tests": [...]} produce distinct prompts.
        contract = yaml.safe_load(
            (FIXTURE_ROOT / "sparse_01_minimal_orders" / "contract.fluid.yaml").read_text(
                encoding="utf-8"
            )
        )
        agent = JudgeAgent()
        _, prompt_none = agent._build_prompt(contract, build_artifacts=None)
        _, prompt_empty = agent._build_prompt(contract, build_artifacts={})
        _, prompt_populated = agent._build_prompt(
            contract,
            build_artifacts={
                "provider": "snowflake",
                "dbt_tests": [{"models": [{"name": "x", "columns": []}]}],
            },
        )
        assert prompt_none == prompt_empty, "empty artifacts dict must render identically to None"
        assert (
            prompt_populated != prompt_none
        ), "populated artifacts must produce a different user prompt"
        # The artifacts block exists in the populated prompt only.
        assert "Deterministic-enrichment outputs" in prompt_populated
        assert "Deterministic-enrichment outputs" not in prompt_none
