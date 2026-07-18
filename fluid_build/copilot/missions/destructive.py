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

"""Fail-closed contract-diff classifier for the mission destructive gate.

Anti-gaming is the whole point (RFC-deep-agents.md): a mission's
success criteria are code-owned, so the cheapest way for a model to
"pass" ``every column has a description`` is to **delete the columns
that don't**. The gate stands between EXECUTE and the write, and it is
deliberately coarse:

- any removal (expose, column, ``consumes[]`` entry, any mapping key),
- any type change,
- any policy loosening (retention grown, allow-list widened),
- **and any diff shape this classifier does not explicitly recognise
  as safe**

all classify DESTRUCTIVE. The recognised-safe set is a short allowlist
of additive/tightening shapes; everything else falls through to
destructive by construction. v1 accepts over-prompting on legitimate
removals over a single false negative — taxonomy refinement is v2.

**Borrow receipts.** The closest precedent is ``datacontract-cli``'s
``breaking`` module — a fail-closed breaking-change classifier over the
*same artifact type* (a YAML data contract), in Python, whose rule
lookup ended in an unconditional ``return Severity.ERROR`` for shapes
it had no rule for. Upstream deprecated it in 0.10.41 and deleted it in
0.11.1, so there is no dependency to take and none to track; the shape
is vendored here with attribution. Terraform's plan JSON contributes
the idea of a **closed action vocabulary** scanned for a ``delete``
token (both replace forms are encoded as pairs specifically so callers
can find the delete), and Atlas's analyzers contribute the observation
that data-loss (DS) and consumer-breakage (BC) are two axes rather than
one ladder — v1 collapses them deliberately, v2 can split them. Noted
counter-example: OPA's Terraform blast-radius policy is fail-*open* —
unmapped resource types score zero and sail through. That is the bug
this module exists to not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Free-text keys whose edits are documentation, not structure. Editing
#: these is the single recognised "value changed" safe shape for strings.
#: ``name`` / ``id`` / ``exposeId`` are deliberately absent — they are
#: load-bearing identifiers, and renaming one is a removal plus an add.
SAFE_TEXT_KEYS = frozenset(
    {
        "description",
        "comment",
        "comments",
        "doc",
        "documentation",
        "purpose",
        "summary",
        "title",
        "businessName",
        "displayName",
        "rationale",
        "notes",
    }
)

#: Keys whose numeric value must not GROW — growth loosens the policy.
#: (Retention windows, row/scan caps, rate limits handed to agents.)
TIGHTEN_ONLY_NUMERIC_KEYS = frozenset(
    {
        "maxRetentionDays",
        "retentionDays",
        "maxRows",
        "maxRowsPerQuery",
        "maxQueriesPerHour",
        "maxQueriesPerDay",
        "rateLimitPerMinute",
        "ttlDays",
    }
)

#: List-valued keys that grant access/capability. Growing one widens the
#: blast radius, so growth is destructive even though "list got longer"
#: is otherwise a safe additive shape.
ALLOWLIST_KEYS = frozenset(
    {
        "allow",
        "allowed",
        "allowedModels",
        "allowedTools",
        "allowedRoles",
        "allowedPurposes",
        "allowedConsumers",
        "grants",
        "principals",
        "readers",
        "writers",
        "scopes",
        "permissions",
    }
)

#: Keys whose value is a type declaration — any change is a type change.
TYPE_KEYS = frozenset({"type", "logicalType", "physicalType", "dataType"})

#: Top-level collections whose element removal is always destructive.
IDENTITY_COLLECTIONS = frozenset({"exposes", "consumes", "builds", "executes"})

#: Dotted paths dropped from BOTH sides before classification.
#: ``metadata.provenance`` is re-stamped by ``write_contract`` on every
#: write (``generated_at`` moves, the LLM's echo usually omits it), so
#: leaving it in would classify every single cycle as destructive and
#: gate the runner into uselessness. Volatile bookkeeping is not
#: contract content.
VOLATILE_PATHS = ("metadata.provenance",)


@dataclass
class DiffFinding:
    """One classified change. ``destructive`` is the gate's verdict."""

    path: str
    kind: str
    destructive: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "destructive": self.destructive,
            "detail": self.detail,
        }


@dataclass
class DiffVerdict:
    """Aggregate verdict over a whole old→new contract diff."""

    findings: List[DiffFinding] = field(default_factory=list)

    @property
    def destructive(self) -> bool:
        return any(f.destructive for f in self.findings)

    @property
    def changed(self) -> bool:
        return bool(self.findings)

    @property
    def destructive_findings(self) -> List[DiffFinding]:
        return [f for f in self.findings if f.destructive]

    def summary_lines(self, *, limit: int = 20) -> List[str]:
        """Human-readable lines for the confirm prompt / audit event."""
        return [f"{f.path}: {f.detail or f.kind}" for f in self.destructive_findings[:limit]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "destructive": self.destructive,
            "changed": self.changed,
            "findings": [f.to_dict() for f in self.findings],
        }


def _leaf_key(path: str) -> str:
    """Last non-index segment of a dotted path (``a.b[2].c`` → ``c``)."""
    tail = path.split(".")[-1] if path else ""
    return tail.split("[")[0]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _element_key(item: Any, index: int) -> str:
    """Stable identity for a list element.

    Identity-bearing dicts key on their declared id; everything else
    keys on position, which makes reordering visible as a change (and
    therefore destructive — we cannot prove a reorder is semantically
    inert).
    """
    if isinstance(item, Mapping):
        for candidate in ("exposeId", "id", "name", "field", "column", "ruleId"):
            value = item.get(candidate)
            if isinstance(value, str) and value:
                return f"{candidate}={value}"
    return f"#{index}"


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


def _classify_value_change(path: str, old: Any, new: Any) -> DiffFinding:
    """Classify a scalar/leaf value change. Default is DESTRUCTIVE."""
    key = _leaf_key(path)

    if key in TYPE_KEYS:
        return DiffFinding(
            path=path,
            kind="type_change",
            destructive=True,
            detail=f"type changed {old!r} → {new!r}",
        )

    if key in SAFE_TEXT_KEYS and isinstance(old, str) and isinstance(new, str):
        return DiffFinding(path=path, kind="text_edit", destructive=False, detail="text edited")

    if key in TIGHTEN_ONLY_NUMERIC_KEYS and _is_number(old) and _is_number(new):
        if new > old:
            return DiffFinding(
                path=path,
                kind="policy_loosened",
                destructive=True,
                detail=f"limit grew {old!r} → {new!r}",
            )
        return DiffFinding(
            path=path,
            kind="policy_tightened",
            destructive=False,
            detail=f"limit tightened {old!r} → {new!r}",
        )

    # Turning a boolean gate ON is only safe when the key reads as a
    # restriction; we cannot know that generically, so both directions
    # fall through to the default below.
    return DiffFinding(
        path=path,
        kind="value_changed",
        destructive=True,
        detail=f"unclassified change {old!r} → {new!r} (fail-closed)",
    )


def _classify_list(path: str, old: List[Any], new: List[Any], findings: List[DiffFinding]) -> None:
    """Classify a list change by element identity.

    A list is safe only when it is a pure superset: every old element
    survives with an unchanged identity, and any element that changed
    internally is itself recursively safe.
    """
    key = _leaf_key(path)
    old_keys = [_element_key(item, i) for i, item in enumerate(old)]
    new_keys = [_element_key(item, i) for i, item in enumerate(new)]
    old_index = {k: i for i, k in enumerate(old_keys)}
    new_index = {k: i for i, k in enumerate(new_keys)}

    removed = [k for k in old_keys if k not in new_index]
    added = [k for k in new_keys if k not in old_index]

    if removed:
        collection = key if key in IDENTITY_COLLECTIONS else "element"
        findings.append(
            DiffFinding(
                path=path,
                kind="removal",
                destructive=True,
                detail=f"{len(removed)} {collection}(s) removed: {', '.join(removed[:5])}",
            )
        )

    if added:
        widening = key in ALLOWLIST_KEYS
        findings.append(
            DiffFinding(
                path=path,
                kind="allowlist_widened" if widening else "addition",
                destructive=widening,
                detail=(
                    f"{len(added)} entr(ies) added to an allow-list: {', '.join(added[:5])}"
                    if widening
                    else f"{len(added)} entr(ies) added"
                ),
            )
        )

    # Positional churn among surviving elements: we cannot prove a
    # reorder is inert (positional keys are identity for scalar lists).
    survivors = [k for k in old_keys if k in new_index]
    if survivors != [k for k in new_keys if k in old_index]:
        findings.append(
            DiffFinding(
                path=path,
                kind="reorder",
                destructive=True,
                detail="surviving elements were reordered (fail-closed)",
            )
        )

    for k in survivors:
        _classify_node(
            _join(path, f"[{k}]"),
            old[old_index[k]],
            new[new_index[k]],
            findings,
        )


def _classify_mapping(
    path: str, old: Mapping[str, Any], new: Mapping[str, Any], findings: List[DiffFinding]
) -> None:
    for key in old:
        if key not in new:
            findings.append(
                DiffFinding(
                    path=_join(path, key),
                    kind="removal",
                    destructive=True,
                    detail="key removed",
                )
            )
    for key in new:
        child_path = _join(path, key)
        if key not in old:
            widening = key in ALLOWLIST_KEYS
            findings.append(
                DiffFinding(
                    path=child_path,
                    kind="allowlist_added" if widening else "addition",
                    destructive=widening,
                    detail="allow-list introduced" if widening else "key added",
                )
            )
            continue
        _classify_node(child_path, old[key], new[key], findings)


def _classify_node(path: str, old: Any, new: Any, findings: List[DiffFinding]) -> None:
    if old == new:
        return
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        _classify_mapping(path, old, new, findings)
        return
    if isinstance(old, list) and isinstance(new, list):
        _classify_list(path, old, new, findings)
        return
    if type(old) is not type(new) and not (_is_number(old) and _is_number(new)):
        findings.append(
            DiffFinding(
                path=path,
                kind="shape_change",
                destructive=True,
                detail=(
                    f"container/scalar shape changed "
                    f"{type(old).__name__} → {type(new).__name__} (fail-closed)"
                ),
            )
        )
        return
    findings.append(_classify_value_change(path, old, new))


def normalize_for_diff(contract: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deep-copy *contract* with :data:`VOLATILE_PATHS` removed.

    Applied to both sides before classification so re-stamped
    bookkeeping never reads as a content change.
    """
    if contract is None:
        return None
    import copy as _copy

    doc = _copy.deepcopy(dict(contract))
    for dotted in VOLATILE_PATHS:
        segments = dotted.split(".")
        node: Any = doc
        for segment in segments[:-1]:
            if not isinstance(node, dict) or segment not in node:
                node = None
                break
            node = node[segment]
        if isinstance(node, dict):
            node.pop(segments[-1], None)
    return doc


def classify_contract_diff(
    old_contract: Optional[Mapping[str, Any]],
    new_contract: Optional[Mapping[str, Any]],
) -> DiffVerdict:
    """Classify an old→new contract diff. Unknown shapes are destructive.

    A missing *old* contract is a create (no removals possible → safe).
    A missing *new* contract is a wipe → destructive. Volatile
    bookkeeping (:data:`VOLATILE_PATHS`) is stripped from both sides
    first.
    """
    verdict = DiffVerdict()
    old_contract = normalize_for_diff(old_contract)
    new_contract = normalize_for_diff(new_contract)
    if new_contract is None:
        verdict.findings.append(
            DiffFinding(
                path="",
                kind="removal",
                destructive=True,
                detail="the step produced no contract (fail-closed)",
            )
        )
        return verdict
    if old_contract is None:
        verdict.findings.append(
            DiffFinding(path="", kind="creation", destructive=False, detail="contract created")
        )
        return verdict
    _classify_node("", old_contract, new_contract, verdict.findings)
    return verdict


def is_destructive(
    old_contract: Optional[Mapping[str, Any]],
    new_contract: Optional[Mapping[str, Any]],
) -> Tuple[bool, DiffVerdict]:
    """Convenience wrapper: ``(destructive?, verdict)``."""
    verdict = classify_contract_diff(old_contract, new_contract)
    return verdict.destructive, verdict


__all__ = [
    "ALLOWLIST_KEYS",
    "DiffFinding",
    "DiffVerdict",
    "IDENTITY_COLLECTIONS",
    "SAFE_TEXT_KEYS",
    "TIGHTEN_ONLY_NUMERIC_KEYS",
    "TYPE_KEYS",
    "VOLATILE_PATHS",
    "classify_contract_diff",
    "is_destructive",
    "normalize_for_diff",
]
