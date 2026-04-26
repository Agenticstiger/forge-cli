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

"""Coverage for the shipped ``--deterministic`` flag.

The flag is documented as "Force deterministic settings (cache off,
tiering off) and emit audit metadata". True deterministic *output* is
only possible on the heuristic path (no LLM); once an LLM is in the
loop the model can't be made byte-stable without caching, which the
flag explicitly disables.

This suite pins the two invariants that *are* testable:

1. ``_build_session`` under ``--deterministic`` sets ``tiered=False``,
   ``no_cache=True``, and disables live LLM calls regardless of any
   upstream tiered/cache/provider preference.
2. Heuristic ``run_from_intent`` with the same intent twice produces a
   byte-identical ``LogicalDraft`` JSON dump — i.e. the heuristic path
   is replay-stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
from fluid_build.cli.forge_data_model import _build_session
from fluid_build.copilot.store.backends.null import NullBackend


class TestDeterministicSessionFlag:
    def test_deterministic_disables_tiered_and_cache(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")  # stay hermetic
        import logging

        args = SimpleNamespace(
            deterministic=True,
            tiered=True,  # should be overridden to False
            no_cache=False,  # should be overridden to True
            llm_provider=None,
            llm_model=None,
            llm_endpoint=None,
            industry=None,
        )
        session = _build_session(args, workspace_root=tmp_path, logger=logging.getLogger("t"))
        assert session.tiered is False
        assert session.no_cache is True
        assert session.llm_config is None

    def test_deterministic_disables_configured_llm(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        import logging

        args = SimpleNamespace(
            deterministic=True,
            tiered=True,
            no_cache=False,
            llm_provider="gemini",
            llm_model="gemini-2.5-pro",
            llm_endpoint=None,
            industry=None,
        )

        session = _build_session(args, workspace_root=tmp_path, logger=logging.getLogger("t"))

        assert session.llm_config is None
        assert session.tiered is False
        assert session.no_cache is True

    def test_deterministic_conflicts_with_required_llm(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        import logging

        args = SimpleNamespace(
            deterministic=True,
            require_llm=True,
            tiered=False,
            no_cache=False,
            llm_provider="gemini",
            llm_model="gemini-2.5-pro",
            llm_endpoint=None,
            industry=None,
        )

        with pytest.raises(CopilotGenerationError, match="copilot_conflicting_llm_modes"):
            _build_session(args, workspace_root=tmp_path, logger=logging.getLogger("t"))

    def test_non_deterministic_preserves_tiered_and_cache(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        import logging

        args = SimpleNamespace(
            deterministic=False,
            tiered=True,
            no_cache=False,
            llm_provider=None,
            llm_model=None,
            llm_endpoint=None,
            industry=None,
        )
        session = _build_session(args, workspace_root=tmp_path, logger=logging.getLogger("t"))
        assert session.tiered is True
        assert session.no_cache is False


class TestDeterministicHeuristicReplay:
    def test_heuristic_from_intent_is_byte_stable_across_runs(self, tmp_path: Path):
        """Two heuristic runs with identical input must produce identical JSON."""
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.schemas.intent import BusinessIntent
        from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

        intent = BusinessIntent.model_validate(
            {
                "business_context": {
                    "problem_statement": "track customer orders for revenue reporting"
                },
                "data_product": {
                    "name": "orders_domain",
                    "domain": "sales",
                    "description": "Orders data product",
                    "owner": "data-platform@example.com",
                },
            }
        )

        def _run_once() -> str:
            session = StageSession(
                store=NullBackend(),
                workspace_root=tmp_path,
                llm_config=None,  # no LLM → pure heuristic path
                no_cache=True,
                tiered=False,
            )
            pipeline = run_from_intent(session, intent=intent, technique="dimensional")
            return pipeline.coordinator.logical.model_dump_json(indent=2, by_alias=True)

        first = _run_once()
        second = _run_once()
        assert first == second, (
            "heuristic path must be byte-stable when no LLM is configured "
            "(deterministic replay invariant)"
        )
        # Sanity: non-trivial output.
        decoded = json.loads(first)
        assert decoded["technique"] == "dimensional"
        assert decoded["name"] == "orders_domain"


class TestDeterministicAuditMetadata:
    def test_audit_payload_carries_deterministic_flag(self, tmp_path: Path):
        """When --deterministic is set, the audit event payload must record it."""
        from fluid_build.copilot.store.audit_trail import write_audit_event

        path = write_audit_event(
            "forge_data_model",
            payload={
                "output_path": "/tmp/contract.fluid.yaml",
                "technique": "dimensional",
                "deterministic": True,
            },
            root=tmp_path,
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["payload"]["deterministic"] is True
