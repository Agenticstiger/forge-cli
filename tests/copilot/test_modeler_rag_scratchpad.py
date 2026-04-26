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

"""Sprint #1 pin — modeler RAG retrieval populates the scratchpad.

The modeler's existing ``_retrieve_prior_similar_models`` reads
from ``memory/semantic`` and feeds results into its own prompt.
Sprint #1 makes it ALSO write to the session scratchpad so other
agents (CriticAgent, BuilderAgent, ValidatorAgent) and external
observers can read retrievals from one place.

Without this pin, a future refactor could drop the scratchpad
write and the rest of the agentic pipeline would silently lose
RAG signal again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.store.backends.null import NullBackend


@pytest.fixture
def session(tmp_path: Path) -> StageSession:
    return StageSession(store=NullBackend(), workspace_root=tmp_path)


def _stub_record(key: str, similarity: float, value: dict) -> SimpleNamespace:
    return SimpleNamespace(key=key, similarity=similarity, value=value)


def test_retrieval_populates_scratchpad(session):
    """When the modeler's RAG retrieval finds records, they must
    land on the session scratchpad — not just the modeler's
    in-memory prompt payload."""
    fake_records = [
        _stub_record("forge_a", 0.92, {"description": "Customer 360 model"}),
        _stub_record("forge_b", 0.81, {"description": "Order Vault model"}),
    ]
    # Patch VectorBackend.search to return our stub records without
    # touching the real store.
    with patch(
        "fluid_build.copilot.store.backends.vector.VectorBackend.search",
        return_value=fake_records,
    ):
        modeler = ModelerAgent()
        result = modeler._retrieve_prior_similar_models(
            session,
            query="customer orders",
        )
    # Original return — modeler's own prompt sees this.
    assert len(result) == 2
    # NEW behaviour — scratchpad has the same records.
    scratchpad = session.get_scratchpad()
    assert len(scratchpad.retrievals) == 2
    keys = {r.key for r in scratchpad.retrievals}
    assert keys == {"forge_a", "forge_b"}


def test_no_retrievals_no_scratchpad_writes(session):
    """When the store returns nothing, the scratchpad MUST stay
    empty — no phantom retrievals."""
    with patch(
        "fluid_build.copilot.store.backends.vector.VectorBackend.search",
        return_value=[],
    ):
        modeler = ModelerAgent()
        result = modeler._retrieve_prior_similar_models(
            session,
            query="anything",
        )
    assert result == []
    assert session.get_scratchpad().retrievals == []


def test_retrieval_failure_doesnt_pollute_scratchpad(session):
    """Vector backend offline / search exception must NOT leave a
    half-populated scratchpad. The whole retrieval call returns
    empty when the backend errors — that's the existing
    contract — and the scratchpad reflects 'nothing to feed
    back'."""
    with patch(
        "fluid_build.copilot.store.backends.vector.VectorBackend.search",
        side_effect=RuntimeError("vector index offline"),
    ):
        modeler = ModelerAgent()
        result = modeler._retrieve_prior_similar_models(
            session,
            query="x",
        )
    assert result == []
    assert session.get_scratchpad().retrievals == []
