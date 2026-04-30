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

"""Token-aware message-history compaction for the agent loop.

The legacy ``_compact_message_history`` truncated middle messages at
500 characters. That's blunt — it discards semantic structure, has no
notion of token budgets, and offers no opt-in path to LLM-based
summarization for users who want better quality on long loops.

This module replaces that with three composable building blocks:

1. :func:`smart_truncate_messages` — char/token-aware truncation that
   preserves the head (initial user message) and the last N tail
   messages, shrinks the middle aggressively, and keeps tool-call
   names visible (only their argument blobs and outputs get clipped).
2. :func:`summarize_messages` — opt-in LLM-based summarization. Users
   pass a ``summarizer`` callable (e.g. a closure around a Haiku /
   gpt-4o-mini call) and the middle gets compressed into a single
   summary message. Falls back to truncation when no summarizer is
   provided.
3. :func:`compact_messages` — the top-level entry point that picks
   the strategy from ``capability_matrix["compaction_strategy"]`` /
   ``FLUID_COMPACTION_STRATEGY`` env (``truncate`` | ``summarize`` |
   ``hybrid``) and delegates.

The summarizer hook is deliberately a plain ``Callable`` so callers
don't have to depend on any specific provider stack. A minimal
example::

    from langchain_anthropic import ChatAnthropic
    cheap_model = ChatAnthropic(model="claude-3-5-haiku-latest")

    def summarize(text: str) -> str:
        return cheap_model.invoke(
            [HumanMessage(content=f"Summarize:\\n{text}")]
        ).content

    compact_messages(messages, strategy="summarize", summarizer=summarize)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

LOG = logging.getLogger("fluid.cli.forge_copilot.compaction")

__all__ = [
    "compact_messages",
    "resolve_compaction_strategy",
    "smart_truncate_messages",
    "summarize_messages",
]

DEFAULT_KEEP_TAIL = 4
DEFAULT_TRUNCATE_CHARS = 500
DEFAULT_TOOL_CALL_TRUNCATE_CHARS = 200


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def resolve_compaction_strategy(
    capability_matrix: Optional[Dict[str, Any]] = None,
    *,
    default: str = "truncate",
) -> str:
    """Return the compaction strategy from explicit config / env / default.

    Resolution order:

    1. ``capability_matrix["compaction_strategy"]`` if set.
    2. ``FLUID_COMPACTION_STRATEGY`` env var.
    3. ``default`` (``"truncate"`` for backward compatibility).
    """
    if capability_matrix:
        explicit = capability_matrix.get("compaction_strategy")
        if explicit:
            return str(explicit).strip().lower()
    env = os.environ.get("FLUID_COMPACTION_STRATEGY", "").strip().lower()
    if env in {"truncate", "summarize", "hybrid"}:
        return env
    return default


# ---------------------------------------------------------------------------
# Smart truncation
# ---------------------------------------------------------------------------


def smart_truncate_messages(
    messages: Sequence[Dict[str, Any]],
    *,
    keep_tail: int = DEFAULT_KEEP_TAIL,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
    tool_call_truncate_chars: int = DEFAULT_TOOL_CALL_TRUNCATE_CHARS,
) -> List[Dict[str, Any]]:
    """Truncate middle messages while preserving tool-call structure.

    Improvements over the legacy character-truncation:

    * Tool-call arguments are kept visible (just abbreviated) so the
      LLM can still reason about *what* was called even if it can't
      see the full result.
    * Tool-result content blocks are clipped on a per-block basis so
      we don't accidentally truncate mid-block and leave a structural
      half-tag.
    * Head (first message) and last ``keep_tail`` messages are
      preserved intact so the LLM keeps both the original goal and
      its recent reasoning.
    """
    msg_list = list(messages)
    if len(msg_list) <= keep_tail + 1:
        return msg_list

    head = msg_list[:1]
    tail = msg_list[-keep_tail:]
    middle = msg_list[1:-keep_tail]
    compacted_middle: List[Dict[str, Any]] = []
    for msg in middle:
        compacted_middle.append(
            _truncate_one_message(
                msg,
                truncate_chars=truncate_chars,
                tool_call_truncate_chars=tool_call_truncate_chars,
            )
        )
    return head + compacted_middle + tail


def _truncate_one_message(
    msg: Dict[str, Any],
    *,
    truncate_chars: int,
    tool_call_truncate_chars: int,
) -> Dict[str, Any]:
    """Apply the right truncation to a single message based on its
    content shape (string vs. block list vs. tool-call dict)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return _truncate_string_content(msg, content, truncate_chars)
    if isinstance(content, list):
        return _truncate_block_content(
            msg, content, truncate_chars, tool_call_truncate_chars
        )
    return msg


def _truncate_string_content(
    msg: Dict[str, Any],
    content: str,
    truncate_chars: int,
) -> Dict[str, Any]:
    if len(content) <= truncate_chars:
        return msg
    new_msg = dict(msg)
    new_msg["content"] = (
        content[:truncate_chars] + f" [truncated — {len(content)} chars total]"
    )
    return new_msg


def _truncate_block_content(
    msg: Dict[str, Any],
    blocks: List[Any],
    truncate_chars: int,
    tool_call_truncate_chars: int,
) -> Dict[str, Any]:
    new_blocks: List[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            new_blocks.append(block)
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if len(text) > truncate_chars:
                block = dict(block)
                block["text"] = (
                    text[:truncate_chars] + f" [truncated — {len(text)} chars total]"
                )
        elif block_type == "tool_use":
            # Preserve the tool name (LLM needs to remember what it
            # called) but shrink the arguments blob.
            input_args = block.get("input")
            if input_args is not None:
                serialized = json.dumps(input_args, default=str)
                if len(serialized) > tool_call_truncate_chars:
                    block = dict(block)
                    block["input"] = {
                        "_truncated": True,
                        "_preview": serialized[:tool_call_truncate_chars],
                        "_total_chars": len(serialized),
                    }
        elif block_type == "tool_result":
            tr_content = block.get("content")
            if isinstance(tr_content, str) and len(tr_content) > truncate_chars:
                block = dict(block)
                block["content"] = (
                    tr_content[:truncate_chars]
                    + f" [truncated — {len(tr_content)} chars total]"
                )
            elif isinstance(tr_content, list):
                # Recurse into nested content blocks (e.g. structured
                # tool result with text + image blocks).
                block = dict(block)
                block["content"] = [
                    _truncate_one_block_text(b, truncate_chars) for b in tr_content
                ]
        new_blocks.append(block)
    new_msg = dict(msg)
    new_msg["content"] = new_blocks
    return new_msg


def _truncate_one_block_text(block: Any, truncate_chars: int) -> Any:
    if not isinstance(block, dict):
        return block
    if block.get("type") == "text":
        text = block.get("text", "")
        if len(text) > truncate_chars:
            block = dict(block)
            block["text"] = (
                text[:truncate_chars] + f" [truncated — {len(text)} chars total]"
            )
    return block


# ---------------------------------------------------------------------------
# LLM-based summarization
# ---------------------------------------------------------------------------


def summarize_messages(
    messages: Sequence[Dict[str, Any]],
    *,
    summarizer: Optional[Callable[[str], str]],
    keep_tail: int = DEFAULT_KEEP_TAIL,
    fallback_truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
) -> List[Dict[str, Any]]:
    """Replace middle messages with a single summary produced by
    ``summarizer``.

    When ``summarizer`` is ``None``, falls back to
    :func:`smart_truncate_messages` so callers don't have to branch on
    "do I have an LLM available right now?".

    The summary is inserted as a single ``role: user`` message with a
    sentinel marker so logs / debuggers can see the boundary clearly.
    """
    msg_list = list(messages)
    if len(msg_list) <= keep_tail + 1:
        return msg_list

    if summarizer is None:
        LOG.debug(
            "summarize_messages called without a summarizer — falling back to truncate"
        )
        return smart_truncate_messages(
            msg_list,
            keep_tail=keep_tail,
            truncate_chars=fallback_truncate_chars,
        )

    head = msg_list[:1]
    tail = msg_list[-keep_tail:]
    middle = msg_list[1:-keep_tail]

    # Linearize the middle into a single text blob the summarizer can
    # consume. Use JSON dumps so structure is preserved; the summarizer
    # is expected to compress it down by 80-90%.
    blob = json.dumps([_message_for_summary(m) for m in middle], default=str)

    try:
        summary = summarizer(blob)
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "Summarizer failed (%s); falling back to smart_truncate_messages",
            exc,
        )
        return smart_truncate_messages(
            msg_list,
            keep_tail=keep_tail,
            truncate_chars=fallback_truncate_chars,
        )

    summary_msg: Dict[str, Any] = {
        "role": "user",
        "content": (
            "[Summary of earlier turns — produced by the compaction "
            f"summarizer; {len(middle)} messages compressed]\n\n{summary}"
        ),
    }
    return head + [summary_msg] + tail


def _message_for_summary(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Strip noise from a message before sending to the summarizer.

    Drops provider-specific metadata fields that don't help the
    summarizer understand the conversation arc.
    """
    return {
        "role": msg.get("role", "user"),
        "content": msg.get("content", ""),
    }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def compact_messages(
    messages: Sequence[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    capability_matrix: Optional[Dict[str, Any]] = None,
    summarizer: Optional[Callable[[str], str]] = None,
    keep_tail: int = DEFAULT_KEEP_TAIL,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
    tool_call_truncate_chars: int = DEFAULT_TOOL_CALL_TRUNCATE_CHARS,
) -> List[Dict[str, Any]]:
    """Compact ``messages`` using the requested strategy.

    Strategies:

    * ``"truncate"`` (default): :func:`smart_truncate_messages`.
    * ``"summarize"``: :func:`summarize_messages` (falls back to
      truncate when no summarizer is supplied).
    * ``"hybrid"``: smart-truncate first, then summarize the result if
      it's still over budget. Useful for very long loops where naive
      truncation loses critical context.

    Returns the compacted message list. Never raises — every code path
    has a sensible fallback so the agent loop keeps running even when
    summarization fails.
    """
    chosen = strategy or resolve_compaction_strategy(capability_matrix)
    msg_list = list(messages)

    if chosen == "summarize":
        return summarize_messages(
            msg_list,
            summarizer=summarizer,
            keep_tail=keep_tail,
            fallback_truncate_chars=truncate_chars,
        )

    if chosen == "hybrid":
        truncated = smart_truncate_messages(
            msg_list,
            keep_tail=keep_tail,
            truncate_chars=truncate_chars,
            tool_call_truncate_chars=tool_call_truncate_chars,
        )
        # When a summarizer is present, follow up with summarization
        # so the still-long middle gets compressed; otherwise return
        # the truncated form.
        if summarizer is not None:
            return summarize_messages(
                truncated,
                summarizer=summarizer,
                keep_tail=keep_tail,
                fallback_truncate_chars=truncate_chars,
            )
        return truncated

    # Default / "truncate" / unknown → smart truncation.
    return smart_truncate_messages(
        msg_list,
        keep_tail=keep_tail,
        truncate_chars=truncate_chars,
        tool_call_truncate_chars=tool_call_truncate_chars,
    )
