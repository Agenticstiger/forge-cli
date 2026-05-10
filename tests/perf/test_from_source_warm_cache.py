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

"""Pin V1.5 — warm-cache regression on the ``from-source`` (catalog) path.

The general warm-cache regression at
``tests/perf/test_warm_cache_regression.py`` covers the ``from-intent``
and ``from-ddl`` paths. Catalog forging (``fluid forge data-model
from-source``) goes through the same staged pipeline AND a fresh
adapter dispatch on every run — adding a second axis where caching
could silently regress.

Specifically, the catalog dispatch must:

1. **Cache the LLM stages** the same way ``from-intent`` / ``from-ddl``
   do — once the catalog has been read, the modeler's input is a
   deterministic ``TableDefinition[]`` and the cache key collapses to
   the same shape.
2. **NOT re-read the catalog on a warm cache.** The adapter is read-only
   but a re-read is wasted seconds; the LogicalAgent should pull from
   cache before invoking the adapter again. (When this test lands,
   that's a sub-second optimisation; without the test, a future PR
   could regress this without anyone noticing.)

The test is **hermetic** — it uses a stub adapter that sleeps to
simulate catalog latency, and a stub LLM that sleeps to simulate
provider latency. We measure cold vs. warm wall-clock and assert
≥70% reduction (same gate as the general warm-cache regression).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
from fluid_build.copilot.agents.base import BaseStageAgent, StageSession
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogScope,
    CatalogTable,
)
from fluid_build.copilot.schemas.stage_outputs import StructuredOutputModel
from fluid_build.copilot.store.backends.file import FileBackend

# Same gate values as the general warm-cache test.
_LLM_SLEEP_SECONDS = 0.1
_CATALOG_SLEEP_SECONDS = 0.05  # adapter list+inspect for one table
_TARGET_REDUCTION = 0.70


class _StubOutput(StructuredOutputModel):
    """Minimal Pydantic shape the stubbed LLM returns."""

    name: str = "stub_from_catalog"
    technique: str = "data_vault_2"


class _SlowStubAdapter:
    """Stand-in for a real CatalogAdapter that sleeps to simulate
    network latency. Tracks call counts so the test can assert
    cache-driven adapter-skip behavior.

    Implements only the methods the test needs:
    ``list_tables`` + ``get_table`` + ``audit_context`` + ``name``.
    """

    name = "stub_catalog"

    def __init__(self) -> None:
        self.list_calls = 0
        self.get_calls = 0

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        self.list_calls += 1
        time.sleep(_CATALOG_SLEEP_SECONDS)
        return [
            CatalogTable(
                fqn="db.schema.orders",
                name="orders",
                description="Order events",
                columns=[],
                primary_key_columns=["order_id"],
            )
        ]

    def get_table(self, fqn: str) -> CatalogTable:
        self.get_calls += 1
        time.sleep(_CATALOG_SLEEP_SECONDS)
        return CatalogTable(
            fqn=fqn,
            name="orders",
            description="Order events",
            columns=[
                CatalogColumn(
                    name="order_id",
                    data_type="VARCHAR",
                    nullable=False,
                    primary_key=True,
                    description="Order identifier",
                ),
            ],
            primary_key_columns=["order_id"],
        )

    def audit_context(self) -> dict:
        return {"catalog_name": self.name}


def _slow_llm_response_factory(sleep_seconds: float = _LLM_SLEEP_SECONDS):
    def _post(*_args, **_kwargs):
        time.sleep(sleep_seconds)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(_StubOutput().model_dump(mode="json")),
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        return response

    return _post


@pytest.fixture
def perf_session(tmp_path: Path) -> StageSession:
    """Hermetic StageSession with a real FileBackend over tmp_path."""
    backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
    return StageSession(
        store=backend,
        workspace_root=tmp_path,
        llm_config=LlmConfig(
            provider="openai",
            model="gpt-4.1-mini",
            endpoint="https://example.test/api",
            api_key="test-key",
        ),
        active_provider="openai",
    )


def _measure_pipeline(
    session: StageSession,
    *,
    adapter: _SlowStubAdapter,
) -> float:
    """Single forward pass through the catalog → modeler → cache
    flow. Returns wall-clock seconds.

    Uses :class:`BaseStageAgent` directly with a per-test stage name
    so each test invocation gets its own cache namespace
    (``llm/perf_from_source_test``)."""
    agent = BaseStageAgent(stage="perf_from_source_test", tier="balanced")

    # Step 1 — catalog adapter dispatch (the part that's NEW for V1.5).
    catalog_tables = adapter.list_tables(CatalogScope(database="db", schema_name="schema"))
    full_tables = [adapter.get_table(t.fqn) for t in catalog_tables]
    # Build a stable prompt blob reflecting the catalog read.
    prompt_blob = json.dumps([t.model_dump(mode="json") for t in full_tables], sort_keys=True)

    # Step 2 — staged LLM call (cached on warm runs).
    with patch("httpx.post", side_effect=_slow_llm_response_factory()):
        start = time.perf_counter()
        agent.call(
            session,
            system_prompt="conformance-stub",
            user_prompt=prompt_blob,
            output_schema=_StubOutput,
        )
        elapsed = time.perf_counter() - start
    return elapsed


@pytest.mark.xfail(
    strict=False,
    reason="Stubs LLM at the httpx layer; litellm (core dep via PR-7) bypasses "
    "httpx with its own transport. Cache behaviour is still covered by unit "
    "tests; this latency test needs a rewrite for the litellm path.",
)
def test_warm_cache_reduces_from_source_latency(perf_session):
    """Cold run pays the LLM stub's full sleep; warm run hits the
    on-disk cache and skips the LLM. Catalog adapter dispatch
    happens on both runs (catalog metadata isn't cached at the
    adapter layer today — only LLM outputs are). The LLM-stage
    speedup alone clears the 70% gate.
    """
    adapter = _SlowStubAdapter()

    # Cold pass — populates the cache.
    cold = _measure_pipeline(perf_session, adapter=adapter)
    # Warm pass — same prompt blob, same cache key → cache hit.
    warm = _measure_pipeline(perf_session, adapter=adapter)

    reduction = (cold - warm) / cold if cold else 0.0
    assert reduction >= _TARGET_REDUCTION, (
        f"warm-cache reduction {reduction:.2%} < target {_TARGET_REDUCTION:.0%} "
        f"(cold {cold * 1000:.1f}ms, warm {warm * 1000:.1f}ms). The catalog "
        f"forge path's LLM stage must hit the on-disk cache on a re-run "
        f"with identical inputs."
    )


@pytest.mark.xfail(
    strict=False,
    reason="Same fixture as test_warm_cache_reduces_from_source_latency — needs "
    "a litellm-aware rewrite.",
)
def test_cache_key_stable_across_catalog_runs(perf_session):
    """Two consecutive runs with identical catalog state must produce
    the same cache key — otherwise the warm-cache test would never
    hit. This regression-pins the input → cache-key invariant."""
    adapter = _SlowStubAdapter()

    # Run twice; second invocation should hit cache (no LLM call).
    _measure_pipeline(perf_session, adapter=adapter)

    with patch("httpx.post") as mock_post:
        # If the cache hit, httpx.post is NOT called.
        _measure_pipeline(perf_session, adapter=adapter)
        assert mock_post.call_count == 0, (
            f"warm-pass made {mock_post.call_count} HTTP call(s); cache miss "
            f"means the from-source cache key is non-deterministic across "
            f"identical catalog reads."
        )
