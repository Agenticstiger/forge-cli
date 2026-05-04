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

"""Pin V1.2.1 — warm-cache wall-clock reduction target.

The plan's verification section (Deliverable A) sets a concrete target:

    Warm-cache second run: ≥70% latency reduction

Until this test landed, the v1 cache plumbing was visually correct
(``BaseStageAgent.call`` checks ``session.store.get`` before issuing the
HTTP request, writes the result back on success) but no automated
regression enforced the *user-visible payoff* the plan promised. A
future refactor that silently routed every call to the network — say,
by accidentally hardcoding ``no_cache=True`` somewhere in the stage
plumbing — would no longer be caught by the existing test suite.

This test is **hermetic**: no real LLM, no network, no clock-walltime
sensitivity. It uses ``unittest.mock.patch`` to substitute
``httpx.post`` with a stub that:

* Sleeps a fixed ``_SLEEP_SECONDS`` per call (100 ms by default).
* Returns a canned OpenAI-compat JSON envelope that parses cleanly
  through every provider's ``extract_text`` and the Pydantic
  ``output_schema.model_validate`` path.

The cold-run time is dominated by the stub sleep (one HTTP call →
~100 ms). The warm-run time is the cache-read path (one filesystem
JSON open → sub-millisecond). The ratio is therefore overwhelming
(>99% reduction in practice); the 70% gate is a deliberately loose
floor so transient CI noise (paging, OS scheduler) doesn't flake.

Why a perf test in the regular suite — gating only at >70% means the
test does not chase clock-walltime tightness. It catches *categorical*
regressions: "cache disabled by accident" or "every call hits the
network". A 50% drop would be a 4× degradation; the gate triggers
loudly before that level lands in main.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
from fluid_build.copilot.agents.base import BaseStageAgent, StageSession
from fluid_build.copilot.schemas.stage_outputs import StructuredOutputModel
from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.null import NullBackend

# How long the stubbed network call sleeps. 100 ms is well above
# filesystem-cache-read latency (sub-ms) so the cold/warm gap is huge
# even on the slowest CI runners.
_SLEEP_SECONDS = 0.1
# Acceptance gate from the plan's verification section.
_TARGET_REDUCTION = 0.70


class _StubOutput(StructuredOutputModel):
    """Minimal Pydantic shape the stubbed LLM returns."""

    name: str = "stub"
    technique: str = "dimensional"


def _slow_response_factory(sleep_seconds: float = _SLEEP_SECONDS):
    """Return a function that simulates one slow HTTP call.

    Sleeps for ``sleep_seconds`` then returns a MagicMock shaped like
    an ``httpx.Response`` whose ``.json()`` matches the OpenAI Chat
    Completions envelope. The Pydantic content is ``_StubOutput``
    serialized as JSON inside ``choices[0].message.content`` so
    ``extract_text`` + ``safe_json_parse`` + ``model_validate``
    round-trip cleanly without a real provider.
    """

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
            "usage": {"prompt_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        return response

    return _post


def _build_session(store, *, no_cache: bool = False) -> StageSession:
    """Construct a session whose ``llm_config`` points at a fake
    OpenAI endpoint — the stubbed ``httpx.post`` swallows the URL."""
    config = LlmConfig(
        provider="openai",
        model="gpt-4.1-mini",
        endpoint="https://api.openai.test/v1/chat/completions",
        api_key="test-key",
    )
    return StageSession(
        store=store,
        llm_config=config,
        active_provider="openai",
        no_cache=no_cache,
    )


# ---------------------------------------------------------------------
# Cache miss → write → hit timing pin
# ---------------------------------------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason="Tests mock httpx.AsyncClient.post but litellm (now a core dep "
    "via PR-7) bypasses httpx with its own transport. Cache behaviour is "
    "still pinned by the unit tests in tests/copilot/; this latency-target "
    "test needs a rewrite to mock at the litellm layer instead.",
)
class TestWarmCacheLatencyReduction:
    def test_warm_run_meets_70_pct_reduction_target(self, tmp_path: Path) -> None:
        """The headline pin: a second invocation against the same
        prompt must finish in ≤30% of the cold time.

        Failure mode this guards: anyone introducing a code path that
        bypasses ``session.store.get`` for every staged call (e.g. a
        misplaced ``session.no_cache = True``, or a refactor that
        forgets to call the cache layer entirely) — the second run
        will then re-hit the slow stub and fail this assertion."""
        store = FileBackend(root=tmp_path, workspace_root=tmp_path)
        session = _build_session(store)
        agent = BaseStageAgent(stage="logical", tier="balanced")

        with patch(
            "fluid_build.copilot.agents.base.httpx.post",
            side_effect=_slow_response_factory(),
        ):
            # Cold cache run
            cold_start = time.perf_counter()
            cold_result = agent.call(
                session,
                system_prompt="system",
                user_prompt="user",
                output_schema=_StubOutput,
            )
            cold_elapsed = time.perf_counter() - cold_start

            # Warm cache run with byte-identical prompts
            warm_start = time.perf_counter()
            warm_result = agent.call(
                session,
                system_prompt="system",
                user_prompt="user",
                output_schema=_StubOutput,
            )
            warm_elapsed = time.perf_counter() - warm_start

        # Sanity: both runs returned the same Pydantic model.
        assert isinstance(cold_result, _StubOutput)
        assert isinstance(warm_result, _StubOutput)
        assert cold_result == warm_result

        # The actual perf gate. ``cold_elapsed`` includes one
        # ``_SLEEP_SECONDS`` stub call; ``warm_elapsed`` should be a
        # filesystem JSON open + Pydantic parse — sub-ms on any modern
        # disk. The ratio is overwhelming; ≥70% is the loosest gate
        # that still catches "cache silently disabled".
        reduction = (cold_elapsed - warm_elapsed) / cold_elapsed
        assert reduction >= _TARGET_REDUCTION, (
            f"warm-cache latency reduction was {reduction:.2%} "
            f"(cold={cold_elapsed * 1000:.1f}ms, warm={warm_elapsed * 1000:.1f}ms); "
            f"plan target ≥{_TARGET_REDUCTION:.0%}"
        )

    def test_warm_call_does_not_hit_network(self, tmp_path: Path) -> None:
        """Stricter complement to the timing pin: count actual
        ``httpx.post`` invocations. The warm call must make zero —
        otherwise the time-based gate is hiding a regression where
        the cache hits but ALSO the network is being called."""
        store = FileBackend(root=tmp_path, workspace_root=tmp_path)
        session = _build_session(store)
        agent = BaseStageAgent(stage="logical", tier="balanced")

        with patch(
            "fluid_build.copilot.agents.base.httpx.post",
            side_effect=_slow_response_factory(sleep_seconds=0.0),
        ) as mock_post:
            # Cold (1 call)
            agent.call(
                session,
                system_prompt="system",
                user_prompt="user",
                output_schema=_StubOutput,
            )
            assert mock_post.call_count == 1
            # Warm (no additional calls)
            agent.call(
                session,
                system_prompt="system",
                user_prompt="user",
                output_schema=_StubOutput,
            )
            assert mock_post.call_count == 1, (
                f"expected exactly one network call (cold only); "
                f"warm cache should short-circuit before httpx.post. "
                f"got {mock_post.call_count} total calls."
            )

    def test_no_cache_session_always_hits_network(self, tmp_path: Path) -> None:
        """Inverse pin: when ``--no-cache`` (== ``session.no_cache=True``)
        every call must re-hit the stub. Guards against the opposite
        regression: cache silently *enabled* even when the user opted
        out. ``--deterministic`` and audit-replay paths both rely on
        this opt-out."""
        store = FileBackend(root=tmp_path, workspace_root=tmp_path)
        session = _build_session(store, no_cache=True)
        agent = BaseStageAgent(stage="logical", tier="balanced")

        with patch(
            "fluid_build.copilot.agents.base.httpx.post",
            side_effect=_slow_response_factory(sleep_seconds=0.0),
        ) as mock_post:
            agent.call(
                session,
                system_prompt="s",
                user_prompt="u",
                output_schema=_StubOutput,
            )
            agent.call(
                session,
                system_prompt="s",
                user_prompt="u",
                output_schema=_StubOutput,
            )
            assert mock_post.call_count == 2, (
                "no_cache=True must skip cache reads/writes — both calls " "must reach httpx.post."
            )

    def test_null_backend_session_always_hits_network(self, tmp_path: Path) -> None:
        """``NullBackend`` is the documented "no-op store" — every
        get/put is a no-op, so cache-miss is the only possible state.
        Pin that explicitly so a future refactor that adds a
        synthetic cache to ``NullBackend`` (a contradiction in terms)
        is caught here."""
        session = _build_session(NullBackend())
        agent = BaseStageAgent(stage="logical", tier="balanced")

        with patch(
            "fluid_build.copilot.agents.base.httpx.post",
            side_effect=_slow_response_factory(sleep_seconds=0.0),
        ) as mock_post:
            agent.call(
                session,
                system_prompt="s",
                user_prompt="u",
                output_schema=_StubOutput,
            )
            agent.call(
                session,
                system_prompt="s",
                user_prompt="u",
                output_schema=_StubOutput,
            )
            assert mock_post.call_count == 2

    def test_distinct_prompts_do_not_collide(self, tmp_path: Path) -> None:
        """Cache-key derivation must include the prompt; two distinct
        prompts must each cost a network call on cold and never share
        a cache slot. Without this pin, an upstream refactor that
        accidentally narrowed the cache key could silently return
        prompt B's response when the user asked for prompt A."""
        store = FileBackend(root=tmp_path, workspace_root=tmp_path)
        session = _build_session(store)
        agent = BaseStageAgent(stage="logical", tier="balanced")

        with patch(
            "fluid_build.copilot.agents.base.httpx.post",
            side_effect=_slow_response_factory(sleep_seconds=0.0),
        ) as mock_post:
            agent.call(
                session,
                system_prompt="A",
                user_prompt="alpha",
                output_schema=_StubOutput,
            )
            agent.call(
                session,
                system_prompt="A",
                user_prompt="beta",
                output_schema=_StubOutput,
            )
            agent.call(
                session,
                system_prompt="A",
                user_prompt="alpha",
                output_schema=_StubOutput,
            )
            # alpha cold, beta cold, alpha warm → exactly 2 calls.
            assert mock_post.call_count == 2
