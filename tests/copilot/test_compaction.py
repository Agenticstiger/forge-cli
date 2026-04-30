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

"""Unit tests for the smart-compaction module."""

from __future__ import annotations

from fluid_build.cli.forge_copilot_compaction import (
    DEFAULT_KEEP_TAIL,
    compact_messages,
    resolve_compaction_strategy,
    smart_truncate_messages,
    summarize_messages,
)


def _msg(role: str, content) -> dict:
    return {"role": role, "content": content}


class TestStrategyResolution:
    def test_default_is_truncate(self, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_COMPACTION_STRATEGY", raising=False)
        assert resolve_compaction_strategy() == "truncate"

    def test_capability_matrix_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_COMPACTION_STRATEGY", "summarize")
        assert (
            resolve_compaction_strategy({"compaction_strategy": "hybrid"}) == "hybrid"
        )

    def test_env_falls_through_when_no_capability(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_COMPACTION_STRATEGY", "summarize")
        assert resolve_compaction_strategy() == "summarize"

    def test_unknown_env_value_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_COMPACTION_STRATEGY", "weird-value")
        assert resolve_compaction_strategy() == "truncate"


class TestSmartTruncate:
    def test_short_history_unchanged(self) -> None:
        msgs = [_msg("user", "x")] + [_msg("assistant", "y")] * 3
        out = smart_truncate_messages(msgs)
        assert out == msgs

    def test_keeps_head_and_tail_intact(self) -> None:
        head = _msg("user", "ORIGINAL_GOAL")
        tail = [_msg("assistant", f"recent_{i}") for i in range(DEFAULT_KEEP_TAIL)]
        middle = [_msg("user", "x" * 2000) for _ in range(5)]
        msgs = [head] + middle + tail

        out = smart_truncate_messages(msgs)
        assert out[0] == head  # head intact
        # tail must match the originals exactly.
        assert out[-DEFAULT_KEEP_TAIL:] == tail

    def test_middle_strings_get_truncated(self) -> None:
        long_text = "x" * 2000
        msgs = [
            _msg("user", "head"),
            _msg("user", long_text),
            _msg("user", "tail1"),
            _msg("user", "tail2"),
            _msg("user", "tail3"),
            _msg("user", "tail4"),
        ]
        out = smart_truncate_messages(msgs, truncate_chars=500)
        # Middle's content should be shortened with the truncation marker.
        assert "[truncated" in out[1]["content"]
        assert len(out[1]["content"]) < len(long_text)

    def test_tool_use_blocks_keep_name_truncate_args(self) -> None:
        big_input = {"path": "x" * 2000}
        msg = _msg(
            "assistant",
            [
                {"type": "tool_use", "name": "discover_workspace", "input": big_input},
            ],
        )
        msgs = [_msg("user", "head"), msg] + [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]

        out = smart_truncate_messages(msgs, tool_call_truncate_chars=200)
        compacted_msg = out[1]
        block = compacted_msg["content"][0]
        # Tool name preserved...
        assert block["name"] == "discover_workspace"
        # ...but the input is now a structured truncation marker.
        assert isinstance(block["input"], dict)
        assert block["input"].get("_truncated") is True

    def test_tool_result_blocks_get_clipped(self) -> None:
        long_result = "y" * 4000
        msg = _msg(
            "user",
            [
                {"type": "tool_result", "content": long_result},
            ],
        )
        msgs = [_msg("user", "head"), msg] + [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        out = smart_truncate_messages(msgs, truncate_chars=300)
        block = out[1]["content"][0]
        assert "[truncated" in block["content"]
        assert len(block["content"]) < len(long_result)


class TestSummarize:
    def test_no_summarizer_falls_back_to_truncate(self) -> None:
        long_text = "x" * 5000
        msgs = (
            [_msg("user", "head")]
            + [_msg("user", long_text) for _ in range(3)]
            + [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        )
        out = summarize_messages(msgs, summarizer=None)
        # Length should still be sensible — i.e. the function returned
        # the truncated form, not raised.
        assert len(out) <= len(msgs)

    def test_summarizer_produces_single_summary_message(self) -> None:
        head = _msg("user", "head")
        middle = [_msg("user", f"mid_{i}") for i in range(5)]
        tail = [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        msgs = [head] + middle + tail

        captured: list[str] = []

        def summarizer(blob: str) -> str:
            captured.append(blob)
            return "SUMMARY"

        out = summarize_messages(msgs, summarizer=summarizer)
        # Head + 1 summary + tail.
        assert len(out) == 1 + 1 + DEFAULT_KEEP_TAIL
        assert out[0] == head
        assert "SUMMARY" in out[1]["content"]
        assert "5 messages compressed" in out[1]["content"]
        # Summarizer received the middle blob — sanity.
        assert "mid_0" in captured[0]

    def test_summarizer_failure_falls_back_to_truncate(self) -> None:
        head = _msg("user", "head")
        middle = [_msg("user", "x" * 2000) for _ in range(3)]
        tail = [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        msgs = [head] + middle + tail

        def boom(_blob: str) -> str:
            raise RuntimeError("API down")

        out = summarize_messages(msgs, summarizer=boom)
        # Truncate fallback returns same number of messages,
        # not a single-summary collapse.
        assert len(out) == len(msgs)


class TestCompactMessagesEntryPoint:
    def test_truncate_strategy_default(self, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_COMPACTION_STRATEGY", raising=False)
        msgs = [_msg("user", f"m_{i}") for i in range(10)]
        out = compact_messages(msgs)
        assert len(out) == len(msgs)

    def test_summarize_strategy_uses_summarizer(self) -> None:
        head = _msg("user", "head")
        middle = [_msg("user", f"m_{i}") for i in range(5)]
        tail = [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        msgs = [head] + middle + tail

        def summarizer(_blob: str) -> str:
            return "S"

        out = compact_messages(msgs, strategy="summarize", summarizer=summarizer)
        # Compressed: head + 1 summary + tail.
        assert len(out) == 1 + 1 + DEFAULT_KEEP_TAIL

    def test_hybrid_truncates_then_summarizes(self) -> None:
        head = _msg("user", "head")
        middle = [_msg("user", "x" * 5000) for _ in range(5)]
        tail = [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        msgs = [head] + middle + tail

        def summarizer(blob: str) -> str:
            # Even after truncation the blob should be smaller than
            # raw — sanity-check that smart_truncate ran first.
            assert "[truncated" in blob
            return "HYBRID_SUMMARY"

        out = compact_messages(msgs, strategy="hybrid", summarizer=summarizer)
        assert len(out) == 1 + 1 + DEFAULT_KEEP_TAIL
        assert "HYBRID_SUMMARY" in out[1]["content"]

    def test_hybrid_without_summarizer_returns_truncated(self) -> None:
        head = _msg("user", "head")
        middle = [_msg("user", "x" * 2000) for _ in range(3)]
        tail = [_msg("user", f"t{i}") for i in range(DEFAULT_KEEP_TAIL)]
        msgs = [head] + middle + tail
        out = compact_messages(msgs, strategy="hybrid")
        # Should NOT collapse the middle when no summarizer is given.
        assert len(out) == len(msgs)
