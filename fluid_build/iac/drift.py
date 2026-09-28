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

"""Read an OpenTofu saved plan as drift and pending changes.

``tofu plan -out=<file>`` refreshes every managed object from the cloud
before it plans, and ``tofu show -json <file>`` gives two lists
(opentofu.org/docs/internals/json-format): ``resource_drift``, what the
refresh found changed outside OpenTofu since the state was written, and
``resource_changes``, what an apply would do to reach the configuration.
``fluid diff`` reads the apply's own state this way instead of re-reading
each resource through the cloud SDKs.

Neither list is the answer on its own, and neither is the exit code:

- ``resource_drift`` is noisy. Straight after a clean apply against moto the
  refresh reports the Glue database and table as updated, because the
  provider reads back ``{}`` / ``[]`` where state held ``null``
  (``parameters``, ``storage_descriptor[0].bucket_columns``, ...), and an
  out-of-band ``PutBucketVersioning`` shows up as a change to the bucket's
  computed ``versioning`` block. ``tofu plan -refresh-only
  -detailed-exitcode`` therefore exits 2 on a clean apply.
- ``relevant_attributes`` is OpenTofu's own filter for the drift that fed the
  plan, but it lists attributes *other* resources read (the table's
  ``database_name`` reading the database's ``name``). A tag added to a bucket
  by hand is reverted by the plan and appears in no ``relevant_attributes``.
- ``resource_changes`` mixes both causes: the contract changed since the last
  apply, or the cloud did.

So a change is drift when the refresh saw an attribute change outside
OpenTofu **and** the plan would put that attribute back (it overlaps a path
the planned change touches, or a ``relevant_attributes`` path another planned
change reads). What the refresh saw and the plan leaves alone is reported as
``outside_contract``: the module does not declare it (a computed attribute,
a setting the contract does not manage), so it is not drift from the
contract. A planned change with no such drift behind it is ``pending``: the
contract moved and the next apply will follow it. ``null``, ``{}`` and
``[]`` compare equal, which is what a provider reading back an empty block
means.

Values never leave this module: a plan carries every attribute, sensitive
ones included, so the report holds attribute paths and actions only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple, Union

#: Per-resource outcomes, spelled as ``fluid diff``'s live comparison spells
#: them so one gate reads both.
DRIFT = "drift"
PENDING = "pending"
MATCH = "match"
RESOURCE_STATUSES = (DRIFT, PENDING, MATCH)

#: Whether the state pass ran. ``not_checked`` is "there is no apply state to
#: read" (or no ``tofu`` to read it with), never a pass or a failure;
#: ``error`` is "the state is there and could not be compared".
CHECKED = "checked"
NOT_CHECKED = "not_checked"
ERROR = "error"

#: Data sources that look up a container the contract references and does not
#: own (a shared pool bucket or dataset). State holds the lookup, not the
#: container, so a change to it is not in ``resource_drift``.
REFERENCED_CONTAINER_TYPES = frozenset(
    {"aws_s3_bucket", "google_storage_bucket", "google_bigquery_dataset"}
)

#: Attribute paths printed per resource before the rest is counted.
_MAX_PATHS_SHOWN = 6

PathStep = Union[str, int]
AttrPath = Tuple[PathStep, ...]
_NO_OP_ACTIONS: Tuple[List[str], ...] = (["no-op"], ["read"], [])


@dataclass
class ResourceState:
    """One managed resource, as the refreshed plan sees it."""

    address: str
    type: str
    status: str
    #: The planned actions (``["update"]``, ``["delete", "create"]``, ...);
    #: empty when the plan leaves the resource alone.
    actions: List[str] = field(default_factory=list)
    #: Attributes changed outside OpenTofu that the plan would put back.
    drifted: List[str] = field(default_factory=list)
    #: Attributes the planned change touches.
    planned: List[str] = field(default_factory=list)
    #: Attributes changed outside OpenTofu that the module does not declare;
    #: the plan leaves them as they are.
    outside_contract: List[str] = field(default_factory=list)
    #: The refresh found the object gone.
    deleted_outside: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "type": self.type,
            "status": self.status,
            "actions": list(self.actions),
            "drifted": list(self.drifted),
            "planned": list(self.planned),
            "outside_contract": list(self.outside_contract),
            "deleted_outside": self.deleted_outside,
        }

    def human(self) -> str:
        """One line for the CLI, without the address."""
        if self.status == DRIFT:
            if self.deleted_outside:
                return "drift: deleted outside the apply; apply will create it again"
            return "drift: changed outside the apply, " + shown(self.drifted)
        if self.status == PENDING:
            verb = _action_label(self.actions)
            text = f"pending: apply will {verb} it"
            return text + (f", {shown(self.planned)}" if self.planned and verb == "update" else "")
        return "match"


@dataclass
class StateDriftReport:
    """The OpenTofu state pass over one contract."""

    status: str
    #: Where the state lives, as ``fluid apply`` prints it.
    state: Optional[str] = None
    #: Why the pass did not run, or what failed.
    detail: Optional[str] = None
    resources: List[ResourceState] = field(default_factory=list)
    #: Data-source addresses of containers the contract references only.
    referenced: List[str] = field(default_factory=list)
    #: ``tofu plan -detailed-exitcode``: 0 no changes, 2 changes. Recorded,
    #: never gated on (see the module docstring).
    plan_exit_code: Optional[int] = None

    def with_status(self, status: str) -> List[ResourceState]:
        return [r for r in self.resources if r.status == status]

    @property
    def checked(self) -> bool:
        return self.status == CHECKED

    @property
    def has_drift(self) -> bool:
        return bool(self.with_status(DRIFT))

    @property
    def has_errors(self) -> bool:
        return self.status == ERROR

    def counts(self) -> Dict[str, int]:
        return {status: len(self.with_status(status)) for status in RESOURCE_STATUSES}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "state": self.state,
            "detail": self.detail,
            "has_drift": self.has_drift,
            "counts": self.counts(),
            "plan_exit_code": self.plan_exit_code,
            "resources": [r.to_dict() for r in self.resources],
            "referenced": list(self.referenced),
        }


def _action_label(actions: List[str]) -> str:
    if actions == ["create"]:
        return "create"
    if actions == ["delete"]:
        return "delete"
    if actions == ["forget"]:
        return "stop managing"
    if "delete" in actions and "create" in actions:
        return "replace"
    return "update"


def shown(paths: List[str]) -> str:
    """Up to six paths, then a count of the rest."""
    head = ", ".join(paths[:_MAX_PATHS_SHOWN])
    rest = len(paths) - _MAX_PATHS_SHOWN
    return head + (f" (+{rest} more)" if rest > 0 else "")


# ---------------------------------------------------------------------------
# Attribute paths
# ---------------------------------------------------------------------------


def _empty(value: Any) -> bool:
    return value is None or value == {} or value == []


def changed_paths(before: Any, after: Any, prefix: AttrPath = ()) -> Set[AttrPath]:
    """The leaf paths where ``before`` and ``after`` differ.

    ``null``, ``{}`` and ``[]`` are equal. Lists of one length compare by
    index; lists of different lengths differ as a whole, since nothing says
    which element went where. A whole object created or removed is the path
    ``()``.
    """
    if _empty(before) and _empty(after):
        return set()
    if isinstance(before, dict) and isinstance(after, dict):
        out: Set[AttrPath] = set()
        for key in set(before) | set(after):
            out |= changed_paths(before.get(key), after.get(key), prefix + (str(key),))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = set()
        for index, (b, a) in enumerate(zip(before, after, strict=True)):
            out |= changed_paths(b, a, prefix + (index,))
        return out
    return set() if before == after else {prefix}


def unknown_paths(after_unknown: Any, prefix: AttrPath = ()) -> Set[AttrPath]:
    """Paths ``after_unknown`` marks ``true``: values known only after apply."""
    if after_unknown is True:
        return {prefix}
    out: Set[AttrPath] = set()
    if isinstance(after_unknown, dict):
        for key, value in after_unknown.items():
            out |= unknown_paths(value, prefix + (str(key),))
    elif isinstance(after_unknown, list):
        for index, value in enumerate(after_unknown):
            out |= unknown_paths(value, prefix + (index,))
    return out


def _overlaps(path: AttrPath, others: Iterable[AttrPath]) -> bool:
    """Is ``path`` inside, or around, any of ``others``?"""
    return any(path[: len(o)] == o or o[: len(path)] == path for o in others)


def format_path(path: AttrPath) -> str:
    """``storage_descriptor[0].columns``; ``()`` is the whole resource."""
    if not path:
        return "(the whole resource)"
    text = ""
    for step in path:
        if isinstance(step, int):
            text += f"[{step}]"
        elif step.replace("_", "").isalnum():
            text += ("." if text else "") + step
        else:
            text += f"[{step!r}]"
    return text


def _sorted(paths: Iterable[AttrPath]) -> List[str]:
    return sorted({format_path(p) for p in paths})


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _entries(doc: Mapping[str, Any], key: str) -> Iterator[Mapping[str, Any]]:
    for entry in doc.get(key) or []:
        if isinstance(entry, dict) and entry.get("address"):
            yield entry


def _change(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    change = entry.get("change")
    return change if isinstance(change, dict) else {}


def _state_resources(doc: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Every resource in the plan's refreshed ``prior_state``, child modules included."""
    root = ((doc.get("prior_state") or {}).get("values") or {}).get("root_module") or {}
    out: List[Mapping[str, Any]] = []
    stack = [root]
    while stack:
        module = stack.pop()
        out.extend(r for r in module.get("resources") or [] if isinstance(r, dict))
        stack.extend(m for m in module.get("child_modules") or [] if isinstance(m, dict))
    return out


def _managed(entry: Mapping[str, Any]) -> bool:
    return str(entry.get("mode") or "managed") == "managed"


def managed_in_state(doc: Mapping[str, Any]) -> List[str]:
    """Addresses of the managed resources the refreshed state holds."""
    return sorted(
        str(r["address"]) for r in _state_resources(doc) if _managed(r) and r.get("address")
    )


def referenced_containers(doc: Mapping[str, Any]) -> List[str]:
    """Data sources that look up a container the contract does not own."""
    found: Set[str] = set()
    for entry in list(_state_resources(doc)) + list(_entries(doc, "resource_changes")):
        if entry.get("mode") == "data" and entry.get("type") in REFERENCED_CONTAINER_TYPES:
            found.add(str(entry["address"]))
    return sorted(found)


def _relevant(doc: Mapping[str, Any]) -> Dict[str, Set[AttrPath]]:
    out: Dict[str, Set[AttrPath]] = {}
    for item in doc.get("relevant_attributes") or []:
        if not isinstance(item, dict) or not item.get("resource"):
            continue
        attribute = item.get("attribute")
        path = tuple(s if isinstance(s, int) else str(s) for s in attribute or [])
        out.setdefault(str(item["resource"]), set()).add(path)
    return out


def classify_plan(doc: Mapping[str, Any]) -> List[ResourceState]:
    """Every managed resource the plan knows of, classified.

    ``doc`` is ``tofu show -json <planfile>``. A resource the state holds and
    the plan leaves alone is ``match``, whatever the refresh saw in
    attributes the module does not declare.
    """
    changes = {str(e["address"]): e for e in _entries(doc, "resource_changes") if _managed(e)}
    drifts = {str(e["address"]): e for e in _entries(doc, "resource_drift") if _managed(e)}
    relevant = _relevant(doc)
    types: Dict[str, str] = {}
    for entry in list(_state_resources(doc)) + list(changes.values()) + list(drifts.values()):
        if _managed(entry) and entry.get("address"):
            types.setdefault(str(entry["address"]), str(entry.get("type") or ""))

    return [
        _classify(
            address,
            types[address],
            _change(changes[address]) if address in changes else {},
            _change(drifts[address]) if address in drifts else {},
            relevant.get(address, set()),
        )
        for address in sorted(types)
    ]


def _planned(change: Mapping[str, Any]) -> Tuple[List[str], Set[AttrPath]]:
    """``(actions, paths)`` a planned change makes; ``([], set())`` for none."""
    actions = [str(a) for a in change.get("actions") or []]
    if actions in _NO_OP_ACTIONS:
        return [], set()
    paths = changed_paths(change.get("before"), change.get("after"))
    return actions, paths | unknown_paths(change.get("after_unknown"))


def _classify(
    address: str,
    type_: str,
    planned_change: Mapping[str, Any],
    drift_change: Mapping[str, Any],
    read_elsewhere: Set[AttrPath],
) -> ResourceState:
    """One resource: drift where the refresh and the plan meet, else pending or match."""
    actions, planned = _planned(planned_change)
    seen = changed_paths(drift_change.get("before"), drift_change.get("after"))
    reverted = {p for p in seen if _overlaps(p, planned) or _overlaps(p, read_elsewhere)}
    status = DRIFT if reverted else (PENDING if actions else MATCH)
    return ResourceState(
        address=address,
        type=type_,
        status=status,
        actions=actions,
        drifted=_sorted(reverted),
        planned=[] if actions in ([], ["create"]) else _sorted(planned),
        outside_contract=_sorted(seen - reverted),
        deleted_outside=bool(reverted) and (drift_change.get("actions") or []) == ["delete"],
    )


def report_from_plan(
    doc: Mapping[str, Any], *, state: Optional[str], plan_exit_code: Optional[int]
) -> StateDriftReport:
    """A checked report for ``doc``, or ``not_checked`` when state holds nothing.

    A state with no managed resource is the first apply still to come:
    every resource would read ``pending: create``, which says nothing the
    live comparison does not already say.
    """
    if doc.get("errored"):
        return StateDriftReport(
            status=ERROR,
            state=state,
            detail="the plan errored before it finished, so it cannot say there is no drift",
            plan_exit_code=plan_exit_code,
        )
    if not managed_in_state(doc) and not any(_managed(e) for e in _entries(doc, "resource_drift")):
        return StateDriftReport(
            status=NOT_CHECKED,
            state=state,
            detail="the apply state holds no resources for this contract yet",
            plan_exit_code=plan_exit_code,
        )
    return StateDriftReport(
        status=CHECKED,
        state=state,
        resources=classify_plan(doc),
        referenced=referenced_containers(doc),
        plan_exit_code=plan_exit_code,
    )
