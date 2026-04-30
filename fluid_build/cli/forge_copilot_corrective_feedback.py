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

"""Structured corrective feedback for the multi-turn agent loop.

The legacy loop already passes typed-error tool results back to the
LLM via ``provider_adapter.build_tool_result_messages``. The error
dict shape (``{"error": <ExcName>, "message": "...see server logs"}``)
is intentionally vague for security — the LLM never learns the actual
exception text — but this means the LLM also never learns *what to do
differently next turn*. It often retries the same broken call until
the iteration cap kicks in.

This module adds a parallel "guidance" message that gets appended to
the conversation right after the tool results, telling the LLM what
*kind* of mistake it made and how to correct it. The guidance never
quotes server-side state, so the security posture is unchanged.

Public API:

* :func:`build_corrective_messages` — given a list of tool results,
  return a list of ``{"role": "user", "content": str}`` messages to
  append to the conversation. Empty list when every result is a
  success.

Usage in :mod:`forge_copilot_agent_loop`::

    results = _dispatch_tools(tool_calls, workspace_root=ws_root)
    result_msgs = provider_adapter.build_tool_result_messages(tool_calls, results)
    messages.extend(result_msgs)
    # NEW: append corrective guidance for any failed tools.
    messages.extend(build_corrective_messages(tool_calls, results))
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

__all__ = [
    "build_corrective_messages",
    "diagnose_tool_failure",
    "TOOL_ERROR_GUIDANCE",
]


# ---------------------------------------------------------------------------
# Error-class → guidance lookup
#
# Keep guidance generic and security-safe — never quote the actual
# exception message back to the LLM. The guidance MUST be enough to
# nudge the model toward correction without revealing internal state.
# ---------------------------------------------------------------------------


TOOL_ERROR_GUIDANCE: Dict[str, str] = {
    "ToolValidationError": (
        "Your tool arguments did not match the tool's input schema. "
        "Re-read the tool's schema definition above and submit values "
        "that satisfy every required field with the correct types."
    ),
    "UnknownTool": (
        "You called a tool that does not exist. Use only the tools "
        "listed in the system prompt; do not invent tool names."
    ),
    "ValidationError": (
        "Your tool arguments failed Pydantic validation. Check that "
        "every required field is present and that field types match "
        "the schema."
    ),
    "PathTraversalError": (
        "Path arguments must be relative paths under the workspace "
        "root. Absolute paths and ``..`` traversal are rejected for "
        "security. Pass paths like ``data/customers.csv``, not "
        "``/etc/passwd`` or ``../../private``."
    ),
    "ForbiddenPathError": (
        "The path you supplied is on the forbidden-paths list (system "
        "directories, credential stores, etc.). Pick a path under the "
        "workspace root instead."
    ),
    "FileNotFoundError": (
        "The path does not exist. Use the ``discover_workspace`` tool "
        "first to enumerate available files, then pass one of those."
    ),
    "PermissionError": (
        "The path is not readable. Check the workspace permissions " "or pick a different file."
    ),
    "JSONDecodeError": (
        "Your arguments were not valid JSON. Tool argument blocks "
        "must be syntactically valid JSON objects."
    ),
    "RateLimitError": (
        "The provider rate-limited this request. Wait briefly before "
        "retrying with the same arguments."
    ),
    "ContextOverflowError": (
        "The prompt exceeds the model's context window. Cannot retry "
        "with the same arguments — the run will need to compact "
        "earlier tool results."
    ),
    # Generic fallback used when the error class isn't recognised.
    "_default": (
        "The tool call failed. Re-read the tool's schema and the "
        "previous tool result, then try a different approach. Do "
        "not repeat the same call with the same arguments."
    ),
}


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def diagnose_tool_failure(result: Any) -> Tuple[bool, str]:
    """Return ``(is_failure, error_class_name)`` for ``result``.

    The legacy tool dispatch returns either:

    * a success dict (whatever shape the tool produces) — ``is_failure``
      is ``False``,
    * an error dict ``{"error": "<ClassName>", "message": "..."}`` —
      ``is_failure`` is ``True`` and the error class is the value of
      ``error``,
    * the new :class:`ToolDispatchResult` from ``@forge_tool`` —
      ``is_failure`` is ``not result.ok`` and the class is
      ``result.error_type``.

    Anything that isn't recognisably-shaped is treated as a success
    (we don't want to spam corrective feedback on perfectly normal
    tool outputs that happen to have an ``error`` key for unrelated
    reasons).
    """
    # New ToolDispatchResult (from forge_tool decorator)
    ok = getattr(result, "ok", None)
    if ok is False:
        return True, str(getattr(result, "error_type", "") or "_default")
    if ok is True:
        return False, ""

    # Legacy error-dict shape.
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, str) and error:
            return True, error
    return False, ""


def build_corrective_messages(
    tool_calls: Sequence[Dict[str, Any]],
    results: Iterable[Any],
) -> List[Dict[str, str]]:
    """Return user-role corrective messages for any failed tool calls.

    One message per failed call (so the LLM sees per-tool guidance
    rather than a merged blob). Returns an empty list when every
    result was a success — the loop should append nothing in that
    case so the conversation stays compact.

    The messages are built deterministically from the typed error
    class — callers can intercept the catalog
    (:data:`TOOL_ERROR_GUIDANCE`) to add new error types without
    changing the loop.
    """
    messages: List[Dict[str, str]] = []
    # ``strict=False`` is intentional — when the provider's
    # ``extract_tool_calls`` and the dispatcher's results lists ever
    # come in mismatched lengths (e.g. partial-stream truncation), we
    # want to emit guidance for whatever pairs we have rather than
    # raising mid-loop.
    paired = list(zip(list(tool_calls), list(results), strict=False))
    for tc, result in paired:
        is_failure, error_class = diagnose_tool_failure(result)
        if not is_failure:
            continue
        guidance = TOOL_ERROR_GUIDANCE.get(error_class) or TOOL_ERROR_GUIDANCE["_default"]
        tool_name = tc.get("name", "<unknown>")
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Tool ``{tool_name}`` failed with error class "
                    f"``{error_class}``. {guidance}"
                ),
            }
        )
    return messages
