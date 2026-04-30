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

"""Tests for the thread-local streaming-usage capture.

Closes the "streaming runs always record_missing_usage" gap. Each
provider's ``iter_stream_chunks`` now extracts the per-call usage
from SSE events into a thread-local stash; ``consume_streaming_usage``
pops it on the same thread.

These tests exercise the stash + the per-provider SSE-event parsing
without making any real network calls — synthetic SSE bytes are fed
to the iterator via a stub ``httpx.Response``-shaped object.
"""

from __future__ import annotations

from typing import List

from fluid_build.cli.forge_copilot_llm_providers import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAIProvider,
    consume_streaming_usage,
)


class StubResponse:
    """Minimal substitute for ``httpx.Response`` exposing
    ``iter_lines`` over an in-memory list of SSE-formatted lines."""

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines

    def iter_lines(self):
        return iter(self._lines)


class TestStreamingUsageStash:
    def setup_method(self) -> None:
        # Drain any leftover state so each test starts clean.
        consume_streaming_usage()

    def test_consume_returns_none_when_empty(self) -> None:
        assert consume_streaming_usage() is None

    def test_consume_pops_so_second_call_is_none(self) -> None:
        # Manually push then pop.
        from fluid_build.cli.forge_copilot_llm_providers import (
            _record_streaming_usage,
        )

        _record_streaming_usage(input_tokens=10, output_tokens=5)
        first = consume_streaming_usage()
        assert first is not None
        assert first["input_tokens"] == 10
        assert first["output_tokens"] == 5
        # Second consume must return None — the stash is single-shot.
        assert consume_streaming_usage() is None


class TestOpenAIStreamingUsage:
    def setup_method(self) -> None:
        consume_streaming_usage()

    def test_openai_terminal_chunk_usage_is_captured(self) -> None:
        provider = OpenAIProvider()
        # Two content chunks then a terminal usage-only chunk.
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":7,"total_tokens":49}}',
            "data: [DONE]",
        ]
        text = "".join(provider.iter_stream_chunks(StubResponse(sse_lines)))
        assert text == "Hello world"
        usage = consume_streaming_usage()
        assert usage is not None
        assert usage["input_tokens"] == 42
        assert usage["output_tokens"] == 7
        assert usage["total_tokens"] == 49


class TestAnthropicStreamingUsage:
    def setup_method(self) -> None:
        consume_streaming_usage()

    def test_anthropic_message_start_and_message_delta_usage_captured(self) -> None:
        provider = AnthropicProvider()
        # Anthropic sends usage in two events:
        # 1. message_start carries input_tokens (and cache fields)
        # 2. message_delta near message_stop carries output_tokens
        sse_lines = [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":120,"output_tokens":1,"cache_read_input_tokens":80,"cache_creation_input_tokens":40}}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
            'data: {"type":"message_delta","usage":{"input_tokens":120,"output_tokens":15}}',
            'data: {"type":"message_stop"}',
        ]
        chunks = list(provider.iter_stream_chunks(StubResponse(sse_lines)))
        assert chunks == ["Hi"]
        usage = consume_streaming_usage()
        assert usage is not None
        assert usage["input_tokens"] == 120
        # output_tokens must reflect the FINAL value (15), not the
        # initial 1 from message_start.
        assert usage["output_tokens"] == 15
        assert usage["cache_read_tokens"] == 80
        assert usage["cache_write_tokens"] == 40


class TestOllamaStreamingUsage:
    """``OllamaProvider`` extends ``OpenAIProvider`` so it inherits the
    OpenAI SSE-usage-event parser. Verify that path actually fires
    against an Ollama-shaped chat completions stream — recent Ollama
    (>= 0.3.x) emits the OpenAI-compat terminal ``usage`` chunk when
    ``stream_options.include_usage`` is set, which the OpenAI provider
    requests by default in ``build_streaming_request``."""

    def setup_method(self) -> None:
        consume_streaming_usage()

    def test_ollama_terminal_chunk_usage_is_captured(self) -> None:
        provider = OllamaProvider()
        # Recent Ollama emits OpenAI-compat usage on the last chunk
        # when stream_options.include_usage is set (which OpenAIProvider
        # already does in build_streaming_request).
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            'data: {"choices":[{"delta":{"content":" there"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":4,"total_tokens":21}}',
            "data: [DONE]",
        ]
        text = "".join(provider.iter_stream_chunks(StubResponse(sse_lines)))
        assert text == "hi there"
        usage = consume_streaming_usage()
        assert usage is not None
        assert usage["input_tokens"] == 17
        assert usage["output_tokens"] == 4
        assert usage["total_tokens"] == 21

    def test_ollama_without_usage_chunk_records_nothing(self) -> None:
        """Older Ollama servers (< 0.3.x) don't emit a terminal usage
        chunk. The stash should stay empty in that case so the cost
        tracker records ``missing usage`` cleanly rather than reporting
        garbage zeros."""
        provider = OllamaProvider()
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "data: [DONE]",
        ]
        text = "".join(provider.iter_stream_chunks(StubResponse(sse_lines)))
        assert text == "hi"
        assert consume_streaming_usage() is None


class TestGeminiStreamingUsage:
    def setup_method(self) -> None:
        consume_streaming_usage()

    def test_gemini_terminal_usagemetadata_captured(self) -> None:
        provider = GeminiProvider()
        sse_lines = [
            'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}',
            'data: {"candidates":[{"content":{"parts":[{"text":" again"}]}}]}',
            'data: {"candidates":[],"usageMetadata":{"promptTokenCount":58,"candidatesTokenCount":12,"cachedContentTokenCount":4}}',
        ]
        text = "".join(provider.iter_stream_chunks(StubResponse(sse_lines)))
        assert text == "Hello again"
        usage = consume_streaming_usage()
        assert usage is not None
        assert usage["input_tokens"] == 58
        assert usage["output_tokens"] == 12
        assert usage["cache_read_tokens"] == 4


class TestStreamingUsageThreadLocality:
    """The stash must be per-thread to avoid races between concurrent
    stage agents running in the coordinator's ThreadPoolExecutor."""

    def test_stash_is_isolated_per_thread(self) -> None:
        from threading import Event, Thread

        from fluid_build.cli.forge_copilot_llm_providers import (
            _record_streaming_usage,
        )

        consume_streaming_usage()  # clear main thread

        ready = Event()
        thread_saw: List = []

        def worker() -> None:
            # Worker thread should NOT see the main thread's stash.
            thread_saw.append(consume_streaming_usage())
            _record_streaming_usage(input_tokens=99, output_tokens=99)
            ready.set()

        # Stash a record on the main thread first.
        _record_streaming_usage(input_tokens=1, output_tokens=2)

        t = Thread(target=worker)
        t.start()
        ready.wait(timeout=2)
        t.join()

        # Worker thread saw nothing (its stash was empty).
        assert thread_saw == [None]
        # Main thread's stash is intact and still has the original
        # record despite the worker's _record call.
        main_usage = consume_streaming_usage()
        assert main_usage is not None
        assert main_usage["input_tokens"] == 1
        assert main_usage["output_tokens"] == 2
