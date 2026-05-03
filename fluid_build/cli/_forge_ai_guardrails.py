# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""AI-mode forge guardrails.

When ``fluid forge --ai`` is used for a Bronze (acquisition) contract,
the AI is allowed to draft *some* fields — names, doc strings, schedule
hints, classification rationale narration — but **never** safety-critical
fields. This module enforces that boundary:

* Hard-blocked paths (validator rejects an AI-provenance value):
  ``connection.*``, ``secretRef``, types, ``sovereignty.*``,
  ``image_signature.*``, ``cost.budget.*``.

* Soft-allowed paths (AI may draft, but the field carries a
  ``provenance: ai`` marker so reviewers see what's machine-drafted vs
  human-authored).

The flow is two-step: ``write_suggestion(...)`` writes a YAML/JSON
sidecar with per-field provenance; ``apply_suggestion(...)`` merges
those values into the contract after re-checking every blocked path.
The CLI's ``fluid contract apply-suggestion`` ultimately calls
``apply_suggestion``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Hard-blocked paths ───────────────────────────────────────────────────


# Each entry is a dotted path under ``contract`` that may NOT be filled
# by AI. The check is prefix-based: ``connection.host`` is blocked
# because ``connection`` is in the list. Use ``builds[].properties.X``
# to denote per-build paths.
BLOCKED_PATH_PREFIXES: Tuple[str, ...] = (
    # Source connection details (DSNs, hosts, ports, secrets) — facts only.
    "builds[].properties.source.connection",
    # Any explicit secret reference.
    "builds[].properties.source.connection.secretRef",
    "builds[].properties.airbyte.deployment.auth.secretRef",
    "builds[].properties.airbyte.deployment.auth",
    # Schemas / types — must come from introspection or template, not LLM.
    "exposes[].contract.schema",
    # Sovereignty constraints — legal/compliance is never AI-imagined.
    "sovereignty",
    # Supply-chain integrity.
    "builds[].properties.airbyte.image_signature",
    "builds[].properties.debezium.image_signature",
    "builds[].properties.kafka-connect.image_signature",
    # Cost budgets — concrete dollar/row amounts must come from a human.
    "builds[].properties.cost.budget",
)


PROVENANCE_VALUES: Tuple[str, ...] = ("ai", "introspection", "template", "user")


# ── Suggestion file shape ────────────────────────────────────────────────


@dataclass
class FieldSuggestion:
    """One AI-or-introspection-drafted field.

    ``path`` uses dotted segments; ``[i]`` denotes a list index. For
    arrays of objects (``builds[0].properties.source.streams[]``), use
    the index in place of the empty bracket.
    """

    path: str
    value: Any
    provenance: str = "ai"
    rationale: Optional[str] = None  # one-line explanation for human review


@dataclass
class Suggestion:
    """Bundled suggestion file the user reviews before applying."""

    contract_id: str
    fields: List[FieldSuggestion] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "fields": [
                {
                    "path": f.path,
                    "value": f.value,
                    "provenance": f.provenance,
                    "rationale": f.rationale,
                }
                for f in self.fields
            ],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Suggestion":
        return cls(
            contract_id=d["contract_id"],
            fields=[
                FieldSuggestion(
                    path=item["path"],
                    value=item.get("value"),
                    provenance=item.get("provenance", "ai"),
                    rationale=item.get("rationale"),
                )
                for item in d.get("fields", [])
            ],
        )


# ── Path matching ─────────────────────────────────────────────────────────


def _normalize_path_for_match(path: str) -> str:
    """Convert ``builds[0].properties.source.connection.host`` to
    ``builds[].properties.source.connection.host`` for prefix matching.
    """
    out = []
    in_brackets = False
    for ch in path:
        if ch == "[":
            in_brackets = True
            out.append("[")
            continue
        if ch == "]":
            in_brackets = False
            out.append("]")
            continue
        if not in_brackets:
            out.append(ch)
    return "".join(out)


def is_blocked_path(path: str) -> bool:
    """Return True when ``path`` falls under any blocked prefix."""
    norm = _normalize_path_for_match(path)
    for prefix in BLOCKED_PATH_PREFIXES:
        # Exact match or any descendant via dot.
        if norm == prefix or norm.startswith(prefix + ".") or norm.startswith(prefix + "["):
            return True
    return False


# ── Validation ────────────────────────────────────────────────────────────


class GuardrailViolation(ValueError):
    """Raised when an AI-provenance suggestion lands on a blocked path."""


def validate_suggestion(suggestion: Suggestion) -> List[str]:
    """Return a list of violation messages. Empty list = OK to apply."""
    out: List[str] = []
    for fs in suggestion.fields:
        if fs.provenance not in PROVENANCE_VALUES:
            out.append(
                f"{fs.path}: invalid provenance '{fs.provenance}', "
                f"must be one of {PROVENANCE_VALUES}"
            )
            continue
        if fs.provenance == "ai" and is_blocked_path(fs.path):
            out.append(
                f"{fs.path}: AI provenance is not allowed on safety-critical "
                "fields (connection details, secrets, sovereignty, image "
                "signatures, cost budgets). Source this from introspection or "
                "a human-authored template."
            )
    return out


def assert_no_ai_in_critical_paths(suggestion: Suggestion) -> None:
    """Raise ``GuardrailViolation`` if any AI-provenance field lands on a
    blocked path. Used by ``fluid validate`` and by ``apply_suggestion``.
    """
    violations = validate_suggestion(suggestion)
    if violations:
        raise GuardrailViolation("AI-mode guardrail violations:\n  " + "\n  ".join(violations))


# ── Apply ─────────────────────────────────────────────────────────────────


def _set_by_dotted(target: Dict[str, Any], path: str, value: Any) -> None:
    """Set ``target[path] = value`` where ``path`` is dotted with optional
    ``[i]`` indices. Lists are extended with empty dicts when the index
    is past the end (used when the contract draft is incomplete).
    """
    parts = _split_path(path)
    cursor: Any = target
    for i, key in enumerate(parts[:-1]):
        next_part = parts[i + 1]
        is_next_index = isinstance(next_part, int)
        if isinstance(key, int):
            while len(cursor) <= key:
                cursor.append([] if is_next_index else {})
            cursor = cursor[key]
        else:
            if key not in cursor or not isinstance(cursor[key], (dict, list)):
                cursor[key] = [] if is_next_index else {}
            cursor = cursor[key]

    last = parts[-1]
    if isinstance(last, int):
        while len(cursor) <= last:
            cursor.append(None)
        cursor[last] = value
    else:
        cursor[last] = value


def _split_path(path: str) -> List[Any]:
    """Convert ``a.b[2].c`` into ``["a", "b", 2, "c"]``."""
    out: List[Any] = []
    buf = ""
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            if buf:
                out.append(buf)
                buf = ""
        elif ch == "[":
            if buf:
                out.append(buf)
                buf = ""
            j = path.index("]", i)
            out.append(int(path[i + 1 : j]))
            i = j
        else:
            buf += ch
        i += 1
    if buf:
        out.append(buf)
    return out


def apply_suggestion(
    contract: Dict[str, Any],
    suggestion: Suggestion,
    *,
    accept_provenance: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Merge ``suggestion`` into ``contract`` — only after guardrail check.

    ``accept_provenance`` filters which fields apply: by default all
    are applied. Useful when the user wants to accept only the
    introspection-sourced fields and reject AI ones.

    Returns a new contract dict; the input is not mutated.
    """
    assert_no_ai_in_critical_paths(suggestion)

    accepted = accept_provenance or PROVENANCE_VALUES
    out = json.loads(json.dumps(contract))  # deep copy
    for fs in suggestion.fields:
        if fs.provenance not in accepted:
            continue
        _set_by_dotted(out, fs.path, fs.value)
    return out


# ── I/O helpers ──────────────────────────────────────────────────────────


def write_suggestion_file(suggestion: Suggestion, path: str | Path) -> Path:
    """Persist a Suggestion as JSON. Intentionally JSON, not YAML —
    deterministic, widely tooled, and we can add YAML later if user
    feedback demands it.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(suggestion.to_dict(), indent=2, sort_keys=True))
    return p


def read_suggestion_file(path: str | Path) -> Suggestion:
    p = Path(path)
    return Suggestion.from_dict(json.loads(p.read_text()))
