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

"""Coverage for the inter-agent scratchpad (Missing #1).

The scratchpad is the typed shared-state primitive for the staged
forge pipeline. Tests pin the four typed-slot APIs (critic
findings, retrievals, stage feedback, raw) plus the
``StageSession.get_scratchpad`` lazy accessor.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from fluid_build.copilot.scratchpad import (
    CriticFinding,
    RetrievalResult,
    Scratchpad,
    StageFeedback,
)


class TestCriticFindings:
    def test_add_and_filter_by_stage(self):
        pad = Scratchpad()
        pad.add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="warning",
                message="orphan entity 'order_line'",
                suggestion="link via lnk_order_order_line",
            )
        )
        pad.add_critic_finding(
            CriticFinding(
                stage="builder",
                severity="error",
                message="contract has no exposes[]",
            )
        )

        logical_findings = pad.critic_findings_for_stage("logical")
        builder_findings = pad.critic_findings_for_stage("builder")

        assert len(logical_findings) == 1
        assert len(builder_findings) == 1
        assert logical_findings[0].suggestion == "link via lnk_order_order_line"

    def test_critic_findings_filter_returns_snapshot(self):
        """Mutating the returned list must NOT affect the
        scratchpad. Defensive copying keeps the inter-agent
        contract clean."""
        pad = Scratchpad()
        pad.add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="info",
                message="x",
            )
        )

        snapshot = pad.critic_findings_for_stage("logical")
        snapshot.clear()
        # Original still intact.
        assert len(pad.critic_findings) == 1


class TestRetrievals:
    def test_top_k_orders_by_similarity(self):
        pad = Scratchpad()
        pad.add_retrieval(
            RetrievalResult(
                namespace="memory/semantic",
                key="a",
                similarity=0.7,
            )
        )
        pad.add_retrieval(
            RetrievalResult(
                namespace="memory/semantic",
                key="b",
                similarity=0.95,
            )
        )
        pad.add_retrieval(
            RetrievalResult(
                namespace="memory/semantic",
                key="c",
                similarity=0.4,
            )
        )

        top = pad.top_retrievals(limit=2)
        assert [r.key for r in top] == ["b", "a"]

    def test_top_k_filters_by_namespace(self):
        pad = Scratchpad()
        pad.add_retrieval(
            RetrievalResult(
                namespace="memory/semantic",
                key="x",
                similarity=0.9,
            )
        )
        pad.add_retrieval(
            RetrievalResult(
                namespace="discovery",
                key="y",
                similarity=0.95,
            )
        )

        semantic_only = pad.top_retrievals(namespace="memory/semantic")
        assert len(semantic_only) == 1
        assert semantic_only[0].key == "x"


class TestFeedback:
    def test_filter_by_target_stage(self):
        pad = Scratchpad()
        pad.add_feedback(
            StageFeedback(
                source_stage="validator",
                target_stage="builder",
                summary="missing partition_by on orders fact",
            )
        )
        pad.add_feedback(
            StageFeedback(
                source_stage="critic",
                target_stage="logical",
                summary="hub_customer's business_key_columns is empty",
            )
        )

        for_builder = pad.feedback_for_stage("builder")
        for_logical = pad.feedback_for_stage("logical")
        for_readme = pad.feedback_for_stage("readme")

        assert len(for_builder) == 1
        assert len(for_logical) == 1
        assert for_readme == []


class TestRaw:
    def test_set_get_round_trip(self):
        pad = Scratchpad()
        pad.set_raw("custom_key", {"x": 1})
        assert pad.get_raw("custom_key") == {"x": 1}

    def test_get_raw_default_when_missing(self):
        pad = Scratchpad()
        assert pad.get_raw("missing", default="fallback") == "fallback"


class TestThreadSafety:
    def test_concurrent_writes_dont_lose_findings(self):
        """Parallel-physical fanout writes from multiple threads.
        Pin that the lock prevents lost updates under concurrency."""
        pad = Scratchpad()
        N_THREADS = 8
        N_PER_THREAD = 50

        def worker(idx):
            for i in range(N_PER_THREAD):
                pad.add_critic_finding(
                    CriticFinding(
                        stage=f"thread_{idx}",
                        severity="info",
                        message=f"finding {i}",
                    )
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(pad.critic_findings) == N_THREADS * N_PER_THREAD


class TestStageSessionIntegration:
    def test_get_scratchpad_lazy_create(self, tmp_path: Path):
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.store.backends.null import NullBackend

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        # Not constructed yet.
        assert session.scratchpad is None

        pad = session.get_scratchpad()
        assert pad is not None

        # Idempotent — same instance returned on subsequent calls.
        pad_again = session.get_scratchpad()
        assert pad_again is pad
