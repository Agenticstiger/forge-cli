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

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "build_corrective_messages",
    "build_schema_validation_message",
    "build_join_key_repair_message",
    "strip_additional_props_from_contract",
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
        "The path is not readable. Check the workspace permissions or pick a different file."
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
                    f"Tool ``{tool_name}`` failed with error class ``{error_class}``. {guidance}"
                ),
            }
        )
    return messages


# ---------------------------------------------------------------------------
# additionalProperties error parsing helpers
# ---------------------------------------------------------------------------

# Matches the jsonschema error message shape for additionalProperties:
#   "Additional properties are not allowed ('foo' was unexpected)"
#   "Additional properties are not allowed ('foo', 'bar' were unexpected)"
_ADDITIONAL_PROPS_RE = re.compile(
    r"Additional properties are not allowed \((.+?) w(?:as|ere) unexpected\)",
    re.IGNORECASE,
)

# Matches a quoted key name from the additionalProperties error message.
_QUOTED_KEY_RE = re.compile(r"'([^']+)'")


def _parse_additional_props_error(error_text: str) -> Optional[Tuple[str, List[str]]]:
    """Parse an additionalProperties error string.

    Handles two shapes:
    * Raw jsonschema message:
      ``Additional properties are not allowed ('policy' was unexpected)``
    * Path-prefixed validator message (from ``schema_manager.py``):
      ``exposes[0].semantics: Additional properties are not allowed ('policy' was unexpected)``
    * Schema-validation-prefixed (from ``forge_copilot_runtime.py``):
      ``Schema validation: exposes[0].semantics: Additional properties are not allowed (...)``

    Returns ``(json_path, [offending_keys])`` or ``None`` when the text
    doesn't match the additionalProperties pattern.
    """
    match = _ADDITIONAL_PROPS_RE.search(error_text)
    if not match:
        return None

    offending_keys = _QUOTED_KEY_RE.findall(match.group(1))
    if not offending_keys:
        return None

    # Extract leading path prefix (everything before the "Additional…" message).
    prefix_text = error_text[: match.start()].strip().rstrip(":")

    # Strip a "Schema validation: " prefix emitted by the runtime wrapper.
    for strip_prefix in ("Schema validation:", "schema validation:"):
        if prefix_text.lower().startswith(strip_prefix.lower()):
            prefix_text = prefix_text[len(strip_prefix) :].strip().lstrip(":")

    json_path = prefix_text.strip() or "root"
    return json_path, offending_keys


def _build_additional_props_instruction(json_path: str, offending_keys: List[str]) -> str:
    """Return an explicit removal instruction for an additionalProperties violation."""
    keys_fmt = ", ".join(f"``{k}``" for k in offending_keys)
    return (
        f"  [REMOVE REQUIRED] At JSON path ``{json_path}``: "
        f"the key(s) {keys_fmt} are NOT part of the schema "
        f"(``additionalProperties`` is ``false`` there). "
        f"You MUST DELETE {keys_fmt} from your output — "
        f"do not rename, move, or keep them under a different parent. "
        f"If you need to express this concept, place it inside a field "
        f"that the schema allows (e.g. ``metadata.tags`` or ``description``)."
    )


def build_schema_validation_message(errors: Sequence[str]) -> Dict[str, str]:
    """Phase 3 — self-healing schema validation as a corrective message.

    Same shape as the tool-error feedback so the agent loop's existing
    plumbing handles it. Use after a contract emit fails schema
    validation: append the result of this function to the message
    history and the next agent turn re-emits with the violations
    listed by JSON path.

    ``additionalProperties`` errors receive a special, forceful treatment:
    the exact offending key(s) and their JSON path are named explicitly,
    and the LLM is told to **remove** those keys (not rename or relocate
    them). This is critical because a vague "fix schema errors" message
    causes the LLM to re-emit the same violation on every repair attempt.

    Returns a single ``user``-role message; the LLM treats it the
    same as a tool-call error.
    """
    if not errors:
        return {"role": "user", "content": ""}

    bullet_lines: List[str] = []
    additional_props_instructions: List[str] = []

    for error in errors[:30]:
        parsed = _parse_additional_props_error(error)
        if parsed is not None:
            json_path, offending_keys = parsed
            # Emit a forceful, explicit removal instruction instead of the
            # raw error text so the LLM understands it must DELETE the key.
            instruction = _build_additional_props_instruction(json_path, offending_keys)
            bullet_lines.append(instruction)
            additional_props_instructions.append(instruction)
        else:
            bullet_lines.append(f"  - {error}")

    bullets = "\n".join(bullet_lines)

    removal_section = ""
    if additional_props_instructions:
        removal_section = (
            "\n\nCRITICAL — schema ``additionalProperties`` violations detected:\n"
            "The schema blocks extra keys with ``additionalProperties: false``. "
            "You MUST remove every key flagged above — the schema rejects any "
            "key that is not in its explicit ``properties`` list. "
            "Do NOT simply rename the key or move it to a sibling; DELETE it."
        )

    return {
        "role": "user",
        "content": (
            "The contract you produced has schema validation errors:\n"
            f"{bullets}"
            f"{removal_section}\n\n"
            "Please re-emit the contract with these issues fixed. "
            "Read the existing seed_contract for the expected shape and "
            "match every field name + value enumeration exactly. "
            "Do NOT change unrelated parts of the contract."
        ),
    }


# ---------------------------------------------------------------------------
# Last-resort programmatic key stripping for additionalProperties violations
#
# When the LLM fails all repair attempts with the same additionalProperties
# error, the only way to unblock is to surgically DELETE the offending keys
# from the emitted contract before the final validation pass. This is purely
# structural — it never invents content, only removes keys that the schema
# explicitly forbids. The resulting contract is then re-validated; if that
# passes, it's returned as the generation result (with a warning logged).
#
# Design note: stripping is ONLY applied on the last attempt (attempt ==
# max_attempts) when EVERY remaining error is an additionalProperties
# violation whose path we can parse. Mixed errors (missing required fields,
# type mismatches, etc.) are left for the LLM — stripping would create a
# structurally incomplete contract in those cases.
# ---------------------------------------------------------------------------


def _resolve_json_path(obj: Any, path_str: str) -> Any:
    """Walk a dict/list using a path string like ``exposes[0].semantics``.

    Returns the target node, or ``None`` when any segment is missing or
    the path is not reachable. Raises nothing — failure is silent.
    """
    if path_str in ("root", "", None):
        return obj
    current = obj
    # Split on "." but keep array-index tokens like "[0]" attached to their
    # preceding key (e.g. "exposes[0]" → key "exposes", index 0).
    # We iterate character-by-character to handle nested arrays cleanly.
    tokens: List[str] = []
    buf = ""
    for ch in path_str:
        if ch == ".":
            if buf:
                tokens.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        tokens.append(buf)

    for token in tokens:
        if current is None:
            return None
        # token may be "key[0]" or "key[0][1]" etc.
        key_part, *index_parts = token.split("[")
        if key_part:
            if not isinstance(current, dict):
                return None
            current = current.get(key_part)
        for idx_raw in index_parts:
            idx_str = idx_raw.rstrip("]")
            try:
                idx = int(idx_str)
            except ValueError:
                return None
            if not isinstance(current, list) or idx >= len(current):
                return None
            current = current[idx]
    return current


def strip_additional_props_from_contract(
    contract: Dict[str, Any],
    schema_errors: Sequence[str],
) -> Tuple[Dict[str, Any], List[str]]:
    """Programmatically remove keys that violate ``additionalProperties: false``.

    Parses every error in *schema_errors* via
    :func:`_parse_additional_props_error`. For each parsed violation,
    resolves the JSON path inside *contract* and deletes the offending
    key(s) in-place on a deep copy. Returns ``(patched_contract,
    stripped_log)`` where *stripped_log* lists every key that was
    removed (for structured logging / audit). Keys that cannot be
    located (e.g. because the path is not reachable) are silently
    skipped — the caller still gets the best-effort patched dict.

    This is a LAST-RESORT operation. It is only safe when the errors
    are purely additionalProperties violations — the caller is
    responsible for checking that no missing-required-property or
    type-mismatch errors are mixed in.

    Args:
        contract: The contract dict as emitted by the LLM.
        schema_errors: Iterable of error strings (may include the
            ``"Schema validation: "`` prefix).

    Returns:
        ``(patched_contract, stripped_log)`` — a deep copy of
        *contract* with offending keys removed, plus a list of
        ``"<path>.<key>"`` strings describing what was deleted.
    """
    import copy

    patched = copy.deepcopy(contract)
    stripped_log: List[str] = []

    for error in schema_errors:
        parsed = _parse_additional_props_error(error)
        if parsed is None:
            continue
        json_path, offending_keys = parsed
        node = _resolve_json_path(patched, json_path)
        if not isinstance(node, dict):
            continue
        for key in offending_keys:
            if key in node:
                del node[key]
                stripped_log.append(f"{json_path}.{key}" if json_path != "root" else key)

    return patched, stripped_log


# ---------------------------------------------------------------------------
# Phase-3 #14 — Join-key composition self-healing
#
# When an ADP / CDP composes upstream products via ``consumes[]`` and
# the join key the LLM picked doesn't exist in the upstream's schema,
# generic schema-validation feedback isn't enough — the LLM has no
# signal about which *alternative* keys are available. This builder
# emits a structured "alternative keys: A, B, C" prompt that tells
# the LLM exactly what columns are present in the upstream + what the
# canonical primary key is, so the next repair turn can pick a real
# key instead of guessing again.
# ---------------------------------------------------------------------------


def build_join_key_repair_message(
    *,
    upstream_product_id: str,
    requested_key: str,
    available_columns: Sequence[str],
    primary_key_columns: Sequence[str] = (),
    foreign_key_candidates: Sequence[str] = (),
) -> Dict[str, str]:
    """Build a targeted corrective message for a join-key mismatch.

    Used by the composition pipeline (``forge_datamodel.from_data_products``)
    when an emitted contract references an upstream column that doesn't
    exist in the upstream's schema. The message includes:

    * The upstream product id + the bad key the LLM picked.
    * The full list of columns the upstream actually exposes.
    * The upstream's primary key columns (the canonical join target).
    * A short list of likely-foreign-key candidates (columns ending in
      ``_id``, columns matching the requested key by edit distance).

    Returns a single ``user``-role message ready to append to the
    repair conversation. Same shape as
    :func:`build_schema_validation_message` so the existing repair
    loop in :mod:`forge_copilot_runtime` plumbs it identically.

    Args:
        upstream_product_id: ``productId`` from ``consumes[].productId``.
        requested_key: The bad column name the LLM picked.
        available_columns: Every column the upstream's schema declares.
        primary_key_columns: The upstream's PK columns (subset of
            ``available_columns``); empty if the upstream has no PK.
        foreign_key_candidates: Optional pre-ranked list of likely
            replacement keys. When empty, callers can populate via
            ``_rank_join_key_candidates`` below.
    """
    if not requested_key:
        return {"role": "user", "content": ""}

    cols_preview = ", ".join(sorted(available_columns)[:30]) or "(none — upstream schema is empty)"
    pk_str = ", ".join(primary_key_columns) if primary_key_columns else "(none declared)"
    fk_hint = (
        ", ".join(foreign_key_candidates[:5]) if foreign_key_candidates else "(no obvious matches)"
    )

    return {
        "role": "user",
        "content": (
            f"Join-key mismatch on consumes[productId={upstream_product_id!r}]: "
            f"you used join key {requested_key!r}, but that column does not "
            f"exist in the upstream product's schema.\n\n"
            f"Available upstream columns: {cols_preview}\n"
            f"Upstream primary key:       {pk_str}\n"
            f"Likely replacement keys:    {fk_hint}\n\n"
            f"Re-emit the contract with one of the actual columns above as "
            f"the join key. Prefer the upstream primary key when the join "
            f"is one-to-one with the upstream entity; pick a *_id-suffixed "
            f"column when the join is a foreign-key reference. Do NOT "
            f"invent a new column name."
        ),
    }


def _rank_join_key_candidates(
    requested_key: str,
    available_columns: Sequence[str],
    primary_key_columns: Sequence[str] = (),
) -> List[str]:
    """Heuristic ranker for likely join-key replacements.

    Used by :func:`build_join_key_repair_message` callers to pre-fill
    the ``foreign_key_candidates`` list. Ranks columns by:

    1. Exact match minus case (``CustomerID`` vs ``customer_id``).
    2. Same lowercased stem (``customer_id`` vs ``customer_pk``).
    3. PK columns (always included near the top).
    4. ``*_id``-suffixed columns (foreign-key idiom).
    5. Edit-distance proximity to the requested key (last-resort).

    Returns a deduplicated list, longest-relevance-first, capped at 8.
    """
    if not (requested_key and available_columns):
        return list(primary_key_columns)[:8]

    requested_lower = requested_key.lower().replace("-", "_")
    requested_stem = requested_lower.rstrip("_id").rstrip("_pk")

    scored: List[Tuple[float, str]] = []
    for col in available_columns:
        col_lower = col.lower()
        col_stem = col_lower.rstrip("_id").rstrip("_pk")
        score = 0.0
        if col_lower == requested_lower:
            score += 100.0  # exact-case-insensitive match
        if col_stem == requested_stem and col_stem:
            score += 50.0  # same logical stem, different suffix
        if col in primary_key_columns:
            score += 30.0
        if col_lower.endswith("_id") or col_lower.endswith("_pk"):
            score += 10.0
        if requested_stem and requested_stem in col_lower:
            score += 5.0
        if col_lower in requested_lower:
            score += 5.0
        if score > 0:
            scored.append((score, col))

    scored.sort(reverse=True, key=lambda x: (x[0], x[1]))
    seen = set()
    ranked: List[str] = []
    # PKs always anchor the list when present.
    for col in primary_key_columns:
        if col not in seen:
            ranked.append(col)
            seen.add(col)
    for _, col in scored:
        if col not in seen:
            ranked.append(col)
            seen.add(col)
    return ranked[:8]
