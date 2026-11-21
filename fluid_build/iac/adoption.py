# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Taking over a table that already exists: only when asked, and only when it matches.

A table a contract names may already exist, made and filled by a team's own jobs, with
Lake Formation permissions granted outside forge. Importing it silently and letting
the plan rewrite it to the contract would change someone else's table without a word.
So a plugin marks the imports that would take over a table
(:attr:`~fluid_build.iac.importer.ImportBlock.table`), and the engine adopts them only
on ``--adopt-tables``:

* the table goes into the plan through an OpenTofu ``import`` block, so the plan shows
  the import and writes no state before the apply does;
* the plan, read back with ``tofu show -json``, must import it without replacing it, and
  the plugin's reviewer must find no difference between the table as it is and the
  table the contract declares (:func:`review_tables`). Any difference refuses the run,
  naming it.

The rule is Pulumi's ``import`` resource option: an import whose desired state differs
from the actual one fails, the attributes the program does not manage
(``ignoreChanges``) excepted (pulumi/pulumi developer docs, "Importing resources").
OpenTofu does the reading and the diffing; this module only judges the plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

#: The flag of ``fluid plan --tofu`` and ``fluid apply`` that lets a run take over tables.
ADOPT_TABLES_FLAG = "--adopt-tables"

#: What a plugin's reviewer makes of one imported resource change: ``(differences,
#: updates)``. A difference refuses the adoption; an update is what the apply still
#: changes in place on the adopted table, holding no data (a comment, a description).
#: The second argument is the resource's body as the module declares it.
Reviewer = Callable[[Mapping[str, Any], Mapping[str, Any]], Tuple[List[str], List[str]]]

#: ``action_reason`` of a replace (opentofu.org/docs/internals/json-format), in words.
_REPLACE_REASONS = {
    "replace_by_triggers": "a replace_triggered_by trigger fires",
    "replace_by_request": "a -replace option asks for it",
}


@dataclass(frozen=True)
class TableReview:
    """What adopting one existing table does, as the saved plan has it."""

    address: str
    #: ``<database>.<table>``, as the plugin names it.
    table: str
    #: Each a way the table differs from the contract; any one refuses the adoption.
    differences: Tuple[str, ...] = ()
    #: What the apply changes in place on the adopted table, none of it data.
    updates: Tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        return not self.differences

    def as_event(self) -> Dict[str, Any]:
        """The review for a structured log event or an error's context."""
        return {
            "table": self.table,
            "address": self.address,
            "differences": list(self.differences),
            "updates": list(self.updates),
        }


def planned_imports(shown: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """``{address: resource change}`` of each managed resource the saved plan imports.

    ``tofu show -json`` sets ``change.importing`` on a resource an ``import`` block
    brings in (opentofu.org/docs/internals/json-format).
    """
    found: Dict[str, Mapping[str, Any]] = {}
    for change in shown.get("resource_changes") or []:
        if not isinstance(change, Mapping) or change.get("mode", "managed") != "managed":
            continue
        detail = change.get("change")
        if isinstance(detail, Mapping) and detail.get("importing") is not None:
            found[str(change.get("address") or "")] = detail
    return found


def declared_resources(module_text: str) -> Dict[str, Mapping[str, Any]]:
    """``{address: body}`` of every resource in a rendered ``.tf.json`` module."""
    try:
        document = json.loads(module_text)
    except (TypeError, ValueError):
        return {}
    declared: Dict[str, Mapping[str, Any]] = {}
    resources = document.get("resource") if isinstance(document, Mapping) else None
    for kind, blocks in (resources or {}).items():
        if not isinstance(blocks, Mapping):
            continue
        for name, body in blocks.items():
            if isinstance(body, Mapping):
                declared[f"{kind}.{name}"] = body
    return declared


def review_tables(
    shown: Mapping[str, Any],
    tables: Mapping[str, str],
    declared: Mapping[str, Mapping[str, Any]],
    review: Reviewer,
) -> List[TableReview]:
    """One :class:`TableReview` per table in ``tables`` (``{address: name}``), sorted.

    A table the plan does not import, or replaces, differs whatever the reviewer says:
    replacing a Glue table drops it and the Lake Formation permissions granted on it.
    """
    imports = planned_imports(shown)
    reviews: List[TableReview] = []
    for address, name in sorted(tables.items()):
        change = imports.get(address)
        if change is None:
            reviews.append(TableReview(address, name, ("the plan does not import it",)))
            continue
        differences: List[str] = []
        actions = [str(action) for action in change.get("actions") or []]
        if "delete" in actions:
            differences.append(_replaced(change, actions))
        found, updates = review(change, declared.get(address) or {})
        differences.extend(found)
        reviews.append(TableReview(address, name, tuple(differences), tuple(updates)))
    return reviews


def _replaced(change: Mapping[str, Any], actions: Sequence[str]) -> str:
    if "create" not in actions:
        return "OpenTofu would delete it"
    reason = str(change.get("action_reason") or "")
    why = _REPLACE_REASONS.get(reason, "")
    if reason == "replace_because_cannot_update":
        paths = [
            ".".join(str(step) for step in path)
            for path in change.get("replace_paths") or []
            if isinstance(path, list)
        ]
        why = (", ".join(paths) + " cannot change in place") if paths else ""
    return (
        "OpenTofu would replace it"
        + (f" ({why})" if why else "")
        + ", which drops the table and its Lake Formation permissions"
    )


def difference_lines(reviews: Sequence[TableReview]) -> List[str]:
    """The refused tables and their differences, for an error message."""
    lines: List[str] = []
    for review in reviews:
        if review.matches:
            continue
        lines.append(f"  {review.table} ({review.address}):")
        lines.extend(f"    - {difference}" for difference in review.differences)
    return lines
