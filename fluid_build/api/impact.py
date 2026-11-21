# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a contract change does, for a pull request: ``fluid diff --baseline --impact``.

The changelog (:func:`fluid_build.api.changelog.compare_contracts`) says what
changed. This report says what that reaches, in three sections:

* **consumer interface**: the changes a reader of an expose sees, and the
  products in the workspace whose ``consumes[]`` read it;
* **governance and target**: access grants, where each expose lands (platform,
  format, location), its partitioning, and the policy changes the changelog
  found;
* **resource impact**: what the provider will do, read from an OpenTofu plan
  (``tofu show -json``), resource by resource.

Each section says what it could not see and why. With no plan, the resource
impact is ``unknown``; it never reads as "no impact". A partition change
counts as a replacement only when the plan replaces something.

The OpenTofu plan is read as its JSON format documents it: a ``format_version``
whose major is 1, and ``resource_changes[].change.actions``, where
``["delete", "create"]`` and ``["create", "delete"]`` are the two orders of a
replacement. No ``before`` or ``after`` value of the plan is copied into the
report. Grouping by create, update, replace and delete follows the PR comments
of ``byu-oit/github-action-tf-plan-comment``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fluid_build.api.changelog_types import Change, ChangelogReport

VERSION = "1.0"

UNKNOWN = "unknown"

#: Changelog kinds that change what a reader may see or do, not the data's shape.
_GOVERNANCE_KINDS = re.compile(
    r"(agent_policy_|sovereignty_|column_pii_|column_sensitivity_"
    r"|metadata_(owner|domain|tags)_changed)"
)
#: Changelog kinds that change only prose: no reader, grant or resource is reached.
#: An owner, a domain or a tag is not prose: who answers for the data, and the
#: labels a provider carries.
_DOCUMENTATION_KINDS = frozenset(
    {"column_description_changed", "metadata_description_changed", "metadata_name_changed"}
)
_INPUT_KINDS = frozenset({"consume_added", "consume_removed"})

#: A principal any caller, or any account of a domain, is.
_PUBLIC_PRINCIPALS = frozenset({"allusers", "allauthenticatedusers", "*", "public", "everyone"})
_ADMIN_PERMISSIONS = frozenset({"admin", "manage"})
_WRITE_PERMISSIONS = frozenset({"write", "insert", "update", "delete", "create"})

#: ``tofu show -json`` of a large estate runs to tens of megabytes; a contract's cap is 5.
MAX_PLAN_BYTES = 64 * 1024 * 1024

_EXPOSE_PATH = re.compile(r"^exposes\[(\d+)\]")

#: ``tofu show -json`` actions, as a tuple, and the one word this report uses for them.
_ACTIONS = {
    ("no-op",): "no-op",
    ("create",): "create",
    ("read",): "read",
    ("update",): "update",
    ("delete", "create"): "replace",
    ("create", "delete"): "replace",
    ("delete",): "delete",
    ("forget",): "forget",
}
_ACTION_ORDER = ("replace", "delete", "update", "create", "read", "forget", "no-op")


# ── consumer interface ────────────────────────────────────────────────────────


def _expose_ids(contract: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for index, expose in enumerate(contract.get("exposes") or []):
        if isinstance(expose, Mapping):
            out.append(str(expose.get("id") or expose.get("exposeId") or f"_pos_{index}"))
        else:
            out.append(f"_pos_{index}")
    return out


def _change_expose(
    change: Change, base_ids: Sequence[str], new_ids: Sequence[str]
) -> Optional[str]:
    """The expose a change sits under; a removed expose is indexed in the baseline."""
    match = _EXPOSE_PATH.match(change.path)
    if match is None:
        return None
    index = int(match.group(1))
    ids = base_ids if change.kind == "expose_removed" else new_ids
    return ids[index] if index < len(ids) else None


def _all_changes(report: ChangelogReport) -> List[Change]:
    return [*report.breaking, *report.non_breaking, *report.info]


def _section_of(change: Change) -> str:
    if change.kind in _INPUT_KINDS:
        return "inputs"
    if _GOVERNANCE_KINDS.match(change.kind):
        return "governance"
    if change.kind in _DOCUMENTATION_KINDS:
        return "documentation"
    return "interface"


def _change_dict(change: Change, expose_id: Optional[str]) -> Dict[str, Any]:
    out = {
        "path": change.path,
        "kind": change.kind,
        "severity": change.severity,
        "description": change.description,
    }
    if expose_id is not None:
        out["expose_id"] = expose_id
    return out


def _shown(path: Path) -> str:
    """``path`` relative to the working directory when it is below or beside it."""
    try:
        return Path(os.path.relpath(path, Path.cwd())).as_posix()
    except ValueError:  # another drive, on Windows
        return path.as_posix()


def _list(value: Any) -> List[Any]:
    """``value`` when it is a list, else nothing: a discovered contract is not validated."""
    return value if isinstance(value, list) else []


def find_consumers(
    product_ids: Sequence[str], contract_path: Path, new: Mapping[str, Any]
) -> Dict[str, Any]:
    """The products in the workspace that read one of ``product_ids``.

    ``product_ids`` is the product's id, and its id before the change when that
    differs: a reader still names the old one. ``status`` is ``known`` when one
    was found, ``none_in_workspace`` when a workspace was searched and none
    reads it, ``unknown`` when there is no workspace to search. Only files
    named ``contract.fluid.yaml`` (or ``.json``) in the workspace are read, and
    a reader outside it is never seen, so neither of the last two means nobody
    reads it; ``reason`` says so.
    """
    from fluid_build.util.upstream_discovery import discover_upstream_products
    from fluid_build.util.workspace_root import find_workspace_root

    # The contract's own ``consumers[]`` is reserved and read by nothing yet
    # (its schema description says so, and a test holds it): the readers here
    # are the workspace's.
    items: List[Dict[str, Any]] = []

    root = find_workspace_root(contract_path.resolve().parent)
    if root is None:
        status = "known" if items else UNKNOWN
        reason = (
            f"no fluid.workspace.yaml above {_shown(contract_path.resolve().parent)}: "
            "products that read this one are not searched"
        )
        return {"status": status, "reason": reason, "items": items}
    ids = {i for i in product_ids if i}
    if not ids:
        return {
            "status": "known" if items else UNKNOWN,
            "reason": "the contract has no id, so no consumes[] entry can name it",
            "items": items,
        }

    for other_id, contract in sorted(discover_upstream_products(root).items()):
        if other_id in ids:
            continue
        refs = [
            ref
            for ref in _list(contract.get("consumes"))
            if isinstance(ref, Mapping) and str(ref.get("productId") or ref.get("ref") or "") in ids
        ]
        if refs:
            exposes = sorted({str(r["exposeId"]) for r in refs if r.get("exposeId")})
            items.append({"product_id": other_id, "expose_ids": exposes, "source": "workspace"})

    scope = (
        "readers outside the workspace, or in files not named contract.fluid.yaml, are not listed"
    )
    if items:
        return {"status": "known", "reason": scope, "items": items}
    return {
        "status": "none_in_workspace",
        "reason": f"no contract in the workspace reads {', '.join(sorted(ids))}; {scope}",
        "items": [],
    }


def _consumer_interface(
    changes: List[Tuple[Change, Optional[str]]], consumers: Mapping[str, Any]
) -> Dict[str, Any]:
    interface = [(c, e) for c, e in changes if _section_of(c) == "interface"]
    breaking = [c for c, _ in interface if c.severity == "breaking"]
    status = "breaking" if breaking else ("changed" if interface else "unchanged")
    affected = []
    for consumer in consumers.get("items", []):
        reads = set(consumer.get("expose_ids") or [])
        hits = [
            c.path
            for c, expose in interface
            if c.severity == "breaking" and (not reads or expose is None or expose in reads)
        ]
        affected.append({**consumer, "breaking_changes": hits})
    return {
        "status": status,
        "changes": [_change_dict(c, e) for c, e in interface],
        "readers": {**consumers, "items": affected},
    }


# ── governance and target ─────────────────────────────────────────────────────


def _grant_key(grant: Any) -> Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]:
    """A grant's identity; the order its permissions and resources are written in is not."""
    return (
        grant.principal,
        grant.principal_type,
        tuple(sorted(set(grant.permissions))),
        tuple(sorted(set(grant.resources))),
    )


def _broad_reasons(grant: Any, expose_count: int) -> List[str]:
    from fluid_build.iac.access import DOMAIN

    reasons: List[str] = []
    if grant.principal_type == DOMAIN:
        reasons.append("every account of a domain")
    if grant.principal.lower() in _PUBLIC_PRINCIPALS:
        reasons.append("a public principal")
    if _ADMIN_PERMISSIONS & set(grant.permissions):
        reasons.append("admin or manage permission")
    scope = grant.scope
    if scope.all_exposes and not scope.unevaluable and expose_count > 1:
        reasons.append(f"every expose ({expose_count})")
    return reasons


def _grant_dict(grant: Any, expose_count: int) -> Dict[str, Any]:
    reasons = _broad_reasons(grant, expose_count)
    return {
        "principal": grant.principal,
        "principal_type": grant.principal_type,
        "permissions": list(grant.permissions),
        "resources": list(grant.resources),
        "writes": bool(_WRITE_PERMISSIONS & set(grant.permissions)),
        "broad": bool(reasons),
        "broad_because": reasons,
    }


def _grants(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> Dict[str, Any]:
    from fluid_build.iac.access import normalize_access_grants

    old = {_grant_key(g): g for g in normalize_access_grants(baseline)}
    now = {_grant_key(g): g for g in normalize_access_grants(new)}
    count = len(new.get("exposes") or [])
    return {
        "added": [_grant_dict(now[k], count) for k in now if k not in old],
        "removed": [
            _grant_dict(old[k], len(baseline.get("exposes") or [])) for k in old if k not in now
        ],
    }


def _exposes_by_id(contract: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    ids = _expose_ids(contract)
    return {
        ids[i]: e for i, e in enumerate(contract.get("exposes") or []) if isinstance(e, Mapping)
    }


def _binding(expose: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    binding = expose.get("binding") if isinstance(expose, Mapping) else None
    return binding if isinstance(binding, Mapping) else {}


def _location(binding: Mapping[str, Any]) -> Mapping[str, Any]:
    location = binding.get("location")
    return location if isinstance(location, Mapping) else {}


def _partitioning(binding: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "partitionBy" in _location(binding):
        out["location.partitionBy"] = _location(binding)["partitionBy"]
    iceberg = binding.get("icebergConfig")
    if isinstance(iceberg, Mapping) and "partitionSpec" in iceberg:
        out["icebergConfig.partitionSpec"] = iceberg["partitionSpec"]
    return out


def _target(binding: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {key: binding[key] for key in ("platform", "format") if key in binding}
    for key, value in _location(binding).items():
        if key != "partitionBy":
            out[f"location.{key}"] = value
    return out


def _field_changes(
    before: Mapping[str, Any], after: Mapping[str, Any], expose_id: str
) -> List[Dict[str, Any]]:
    return [
        {"expose_id": expose_id, "field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


def _bindings(
    baseline: Mapping[str, Any], new: Mapping[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    old, now = _exposes_by_id(baseline), _exposes_by_id(new)
    targets: List[Dict[str, Any]] = []
    partitions: List[Dict[str, Any]] = []
    # An expose only one side has is a table made or dropped: its target and
    # partitioning are listed against nothing.
    for expose_id in sorted(set(old) | set(now)):
        before, after = _binding(old.get(expose_id)), _binding(now.get(expose_id))
        targets += _field_changes(_target(before), _target(after), expose_id)
        partitions += _field_changes(_partitioning(before), _partitioning(after), expose_id)
    return targets, partitions


# ── resource impact ───────────────────────────────────────────────────────────


def read_plan(path: Path, *, follow: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``path``'s OpenTofu plan JSON, or ``None`` and why it says nothing about resources.

    ``path`` is the ``tofu show -json`` of a saved plan, or the plan.json
    ``fluid plan --tofu`` writes, whose ``opentofuPlan.showJson`` names that
    file beside it. That name is followed once, to a regular file in the same
    directory and never through a symlink.
    """
    from fluid_build.util.safe_yaml import read_bounded_text

    try:
        document = json.loads(read_bounded_text(path, max_bytes=MAX_PLAN_BYTES))
    except (OSError, ValueError) as exc:
        return None, f"{path.name} could not be read as JSON ({type(exc).__name__})"
    if not isinstance(document, Mapping):
        return None, f"{path.name} is not a JSON object"
    if "resource_changes" in document or "planned_values" in document:
        major = str(document.get("format_version", "")).split(".")[0]
        if major != "1":
            return None, (
                f"{path.name} has plan format_version {document.get('format_version')!r}; "
                "this report reads major version 1"
            )
        return dict(document), None
    record = document.get("opentofuPlan")
    if isinstance(record, Mapping) and isinstance(record.get("showJson"), str):
        shown = path.parent / Path(record["showJson"]).name
        if not follow or shown.name == path.name:
            return None, f"{path.name} names a plan that is not an OpenTofu plan"
        if shown.is_symlink() or not shown.is_file():
            return None, f"{path.name} names {shown.name}, which is not a file beside it"
        return read_plan(shown, follow=False)
    if "actions" in document:
        return None, (
            f"{path.name} lists fluid actions, which do not say what a provider replaces: "
            "run fluid plan --tofu and pass that plan.json or its .tfplan.json"
        )
    return None, f"{path.name} is neither an OpenTofu plan nor a fluid plan"


def _resource_impact(
    plan: Optional[Mapping[str, Any]], reason: Optional[str], source: Optional[str]
) -> Tuple[Dict[str, Any], List[Tuple[str, str, str]]]:
    """The resource impact section, and every managed resource as ``(name, address, action)``."""
    if plan is None:
        return {
            "status": UNKNOWN,
            "reason": reason or "no plan given: pass --plan with the output of fluid plan --tofu",
            "plan": None,
        }, []
    counts = {action: 0 for action in _ACTION_ORDER}
    resources: List[Dict[str, Any]] = []
    seen: List[Tuple[str, str, str]] = []
    for change in _list(plan.get("resource_changes")):
        if not isinstance(change, Mapping) or change.get("mode", "managed") != "managed":
            continue
        raw = change.get("change")
        body: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        actions = tuple(a for a in _list(body.get("actions")) if isinstance(a, str))
        action = (
            _ACTIONS.get(actions, "unknown")
            if len(actions) == len(_list(body.get("actions")))
            else "unknown"
        )
        counts[action] = counts.get(action, 0) + 1
        seen.append((str(change.get("name", "")), str(change.get("address", "")), action))
        if action in ("no-op", "read"):
            continue
        entry: Dict[str, Any] = {
            "address": str(change.get("address", "")),
            "type": str(change.get("type", "")),
            "action": action,
        }
        if change.get("action_reason"):
            entry["reason"] = str(change["action_reason"])
        if action == "replace" and _list(body.get("replace_paths")):
            entry["replace_paths"] = [
                ".".join(str(step) for step in p)
                for p in _list(body.get("replace_paths"))
                if isinstance(p, list)
            ]
        resources.append(entry)
    resources.sort(
        key=lambda r: (
            _ACTION_ORDER.index(r["action"]) if r["action"] in _ACTION_ORDER else 99,
            r["address"],
        )
    )
    return {
        "status": "known",
        "reason": None,
        "plan": {"source": source, "counts": counts, "resources": resources},
    }, seen


#: What a plan action on an expose's table says about a partition change.
_PARTITION_VERDICTS = {
    "replace": "replacement",
    "create": "create",
    "delete": "delete",
    "update": "in_place",
    "no-op": "in_place",
}


def _table_names(contract: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """Each expose's names an emitter builds its table's resource name from.

    The OpenTofu emitters name a table ``safe_ident("<id>_<table>")`` (and so
    on), so a resource whose name is, or ends in ``_`` and, one of these is
    that expose's table.
    """
    from fluid_build.iac.naming import safe_ident

    out: Dict[str, Tuple[str, ...]] = {}
    for expose_id, expose in _exposes_by_id(contract).items():
        location = _location(_binding(expose))
        names = [location.get("table"), location.get("topic"), expose_id]
        out[expose_id] = tuple(dict.fromkeys(safe_ident(n) for n in names if n))
    return out


def _matching(name: str, candidates: Sequence[str]) -> bool:
    return any(name == c or name.endswith(f"_{c}") for c in candidates)


def _partition_impact(
    partitions: List[Dict[str, Any]],
    resources: Mapping[str, Any],
    seen: Sequence[Tuple[str, str, str]],
    tables: Mapping[str, Tuple[str, ...]],
) -> None:
    """A partition change's provider impact: what the plan does to that expose's table.

    ``seen`` is every managed resource of the plan as ``(name, address,
    action)``. A replacement is read only off a resource that is the expose's
    table; a plan that does not name it, or holds actions this report does
    not read, leaves the impact ``unknown``.
    """
    for item in partitions:
        if resources["status"] != "known":
            item["provider_impact"] = UNKNOWN
            item["provider_reason"] = resources["reason"]
            continue
        if resources["plan"]["counts"].get("unknown"):
            item["provider_impact"] = UNKNOWN
            item["provider_reason"] = "the plan holds actions this report does not read"
            continue
        names = tables.get(item["expose_id"], ())
        hits = [(address, action) for name, address, action in seen if _matching(name, names)]
        verdicts = {_PARTITION_VERDICTS.get(action, UNKNOWN) for _, action in hits}
        if not hits:
            item["provider_impact"] = UNKNOWN
            item["provider_reason"] = f"no resource in the plan is the table of {item['expose_id']}"
        elif len(verdicts) == 1:
            item["provider_impact"] = verdicts.pop()
            item["provider_reason"] = "the plan: " + ", ".join(f"{a} {addr}" for addr, a in hits)
        else:
            item["provider_impact"] = (
                "replacement" if any(a == "replace" for _, a in hits) else UNKNOWN
            )
            item["provider_reason"] = "the plan: " + ", ".join(f"{a} {addr}" for addr, a in hits)


# ── the report ────────────────────────────────────────────────────────────────


def build_impact(
    baseline: Mapping[str, Any],
    new: Mapping[str, Any],
    report: ChangelogReport,
    *,
    consumers: Mapping[str, Any],
    plan: Optional[Mapping[str, Any]] = None,
    plan_reason: Optional[str] = None,
    plan_source: Optional[str] = None,
) -> Dict[str, Any]:
    """The impact document: ``{"version": "1.0", <three sections>, "documentation_only"}``.

    ``consumers`` is :func:`find_consumers`'s answer; ``plan`` the OpenTofu plan
    JSON (``None``: ``plan_reason`` says why there is none).
    """
    base_ids, new_ids = _expose_ids(baseline), _expose_ids(new)
    # A changed product id is in the changelog (``diff_identity``), first among the
    # breaking changes, so a reader's ``consumes[]`` entry that names the old id
    # is reported with the rest.
    changes = [(c, _change_expose(c, base_ids, new_ids)) for c in _all_changes(report)]

    targets, partitions = _bindings(baseline, new)
    grants = _grants(baseline, new)
    policy = [_change_dict(c, e) for c, e in changes if _section_of(c) == "governance"]
    inputs = [_change_dict(c, e) for c, e in changes if _section_of(c) == "inputs"]
    resources, seen = _resource_impact(plan, plan_reason, plan_source)
    _partition_impact(partitions, resources, seen, {**_table_names(baseline), **_table_names(new)})
    governance_changed = bool(
        grants["added"] or grants["removed"] or targets or partitions or policy or inputs
    )
    interface = _consumer_interface(changes, consumers)
    documentation = [c for c, _ in changes if _section_of(c) == "documentation"]
    return {
        "version": VERSION,
        "consumer_interface": interface,
        "governance_and_target": {
            "status": "changed" if governance_changed else "unchanged",
            "broad_grant_added": any(g["broad"] for g in grants["added"]),
            "grants": grants,
            "targets": targets,
            "partitioning": partitions,
            "policy": policy,
            "inputs": inputs,
        },
        "resource_impact": resources,
        "documentation_only": bool(documentation)
        and interface["status"] == "unchanged"
        and not governance_changed
        and _without_prose(baseline) == _without_prose(new),
    }


#: The keys that hold prose only, at any depth, and the product's display name.
_PROSE_KEYS = frozenset({"description"})


def _without_prose(node: Any, top: bool = True) -> Any:
    """``node`` without its descriptions (and, at the top, its name).

    The changelog does not compare every field (a version, a kind, a layer), so
    a change is documentation only when nothing but prose differs at all.
    """
    if isinstance(node, Mapping):
        return {
            key: _without_prose(value, False)
            for key, value in node.items()
            if key not in _PROSE_KEYS and not (top and key == "name")
        }
    if isinstance(node, list):
        return [_without_prose(item, False) for item in node]
    return node


# ── rendering ─────────────────────────────────────────────────────────────────
#
# Every value comes from a contract, a sibling contract or a plan, and a pull
# request comment is where it is read: in Markdown each one is a code span
# (``changelog._code``), so a name cannot start a heading, open an HTML comment
# that hides the rest, or mention someone; in text each is spelled on one line
# (``changelog._text``), so it cannot drive the terminal. Only the fixed words
# around them are markup.


class _Style:
    def __init__(self, markdown: bool) -> None:
        from fluid_build.api.changelog import _code, _text

        self.markdown = markdown
        self.value = _code if markdown else _text

    def heading(self, level: int, title: str) -> str:
        return f"{'#' * level} {title}" if self.markdown else title

    def strong(self, text: str) -> str:
        return f"**{text}**" if self.markdown else text

    def json(self, value: Any) -> str:
        if value is None:
            return "(unset)"
        return self.value(value if isinstance(value, str) else json.dumps(value, sort_keys=True))

    def values(self, items: Iterable[Any]) -> str:
        return ", ".join(self.value(item) for item in items)


def _consumer_lines(consumers: Mapping[str, Any], style: _Style) -> Iterable[str]:
    status = consumers.get("status")
    items = consumers.get("items") or []
    reason = style.value(consumers.get("reason") or "")
    if status == UNKNOWN:
        yield f"- Consumers: {style.strong('unknown')}: {reason}"
        return
    if not items:
        yield f"- Consumers: none found: {reason}"
        return
    yield f"- Consumers ({len(items)}; {reason}):"
    for item in items:
        reads = style.values(item.get("expose_ids") or []) or "every expose"
        hits = item.get("breaking_changes") or []
        verdict = f"breaks on {style.values(hits)}" if hits else "not broken"
        source = style.value(item.get("source", ""))
        yield f"  - {style.value(item['product_id'])} ({source}, reads {reads}): {verdict}"


def _change_line(change: Mapping[str, Any], style: _Style) -> str:
    return (
        f"- {change['severity']}: {style.value(change['path'])}: "
        f"{style.value(change['description'])}"
    )


def _render(impact: Mapping[str, Any], style: _Style) -> str:
    lines: List[str] = [style.heading(2, "Impact"), ""]
    if impact.get("documentation_only"):
        lines += ["Documentation only: no interface, grant, target or partition changed.", ""]

    interface = impact["consumer_interface"]
    lines += [
        style.heading(3, "Consumer interface"),
        "",
        f"{style.strong('Status:')} {interface['status']}",
        "",
    ]
    if not interface["changes"]:
        lines.append("- No column, field, expose or stream changed.")
    lines += [_change_line(c, style) for c in interface["changes"]]
    lines += list(_consumer_lines(interface["readers"], style))
    lines.append("")

    governance = impact["governance_and_target"]
    lines += [
        style.heading(3, "Governance and target"),
        "",
        f"{style.strong('Status:')} {governance['status']}",
        "",
    ]
    if governance["status"] == "unchanged":
        lines.append("- No grant, target, partitioning, policy or input changed.")
    for verb in ("added", "removed"):
        for grant in governance["grants"][verb]:
            broad = (
                f" {style.strong('broad')}: {', '.join(grant['broad_because'])}"
                if grant["broad"]
                else ""
            )
            principal = style.value(f"{grant['principal_type']}:{grant['principal']}")
            lines.append(f"- Grant {verb}: {principal} {style.values(grant['permissions'])}{broad}")
    for item in governance["targets"]:
        lines.append(
            f"- Target of {style.value(item['expose_id'])}: {style.value(item['field'])} "
            f"{style.json(item['before'])} to {style.json(item['after'])}"
        )
    for item in governance["partitioning"]:
        lines.append(
            f"- Partitioning of {style.value(item['expose_id'])}: "
            f"{style.value(item['field'])} {style.json(item['before'])} to "
            f"{style.json(item['after'])}; provider impact {item['provider_impact']}: "
            f"{style.value(item['provider_reason'] or '')}"
        )
    lines += [_change_line(c, style) for c in governance["policy"] + governance["inputs"]]
    lines.append("")

    resources = impact["resource_impact"]
    lines += [style.heading(3, "Resource impact"), ""]
    if resources["status"] == UNKNOWN:
        lines.append(f"{style.strong('Status:')} unknown: {style.value(resources['reason'])}")
    else:
        plan = resources["plan"]
        counts = ", ".join(f"{n} {a}" for a, n in plan["counts"].items() if n)
        lines.append(f"{style.strong('Status:')} known ({counts or 'no resource changes'})")
        if plan["resources"]:
            lines.append("")
        for item in plan["resources"]:
            why = f" ({style.value(item['reason'])})" if item.get("reason") else ""
            paths = (
                f"; replaced for {style.values(item['replace_paths'])}"
                if item.get("replace_paths")
                else ""
            )
            lines.append(f"- {item['action']}: {style.value(item['address'])}{why}{paths}")
    lines.append("")
    return "\n".join(lines)


def render_impact_markdown(impact: Mapping[str, Any]) -> str:
    """The impact document as the Markdown a pull request comment shows."""
    return _render(impact, _Style(markdown=True))


def render_impact_text(impact: Mapping[str, Any]) -> str:
    """The impact document for a terminal: every value spelled on one line."""
    return _render(impact, _Style(markdown=False))
