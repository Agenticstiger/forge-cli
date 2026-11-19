# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The access a contract declares on its Lakekeeper tables, as stage-8 bindings.

An Iceberg expose whose ``binding.location.catalog`` is ``lakekeeper`` lives in
that catalog, so its table grants belong to Lakekeeper's own authorization
(OpenFGA), not to a cloud's IAM. ``fluid policy compile`` writes one binding
per table and logical principal, and ``fluid policy apply`` applies them
through Lakekeeper's management API (``policy/lakekeeper_apply.py``). This
module is pure: it reads the contract and imports nothing heavy.

What a contract declares, and what it becomes in Lakekeeper:

* ``accessPolicy.grants[]`` (and the deprecated ``metadata.policies``, read by
  ``iac/access.py``), each limited to the exposes its ``resources`` name;
* ``exposes[].policy.authz.readers`` (``select``) and ``writers`` (``modify``);
* ``read``, ``select``, ``query`` become ``select``; ``write``, ``insert``,
  ``update``, ``delete``, ``manage`` and ``admin`` become ``modify``, the
  strongest data relation, as every other forge compiler maps ``manage`` to
  write access and never to ownership or grant management. ``create`` has no
  table relation in Lakekeeper (only a namespace or warehouse takes it), so it
  is reported, never widened to the namespace.

Lakekeeper's table relations (OpenFGA model v4.13, Lakekeeper v0.14.0,
``authz/openfga/v4.13/components/lakekeeper_table.fga``) are ownership,
pass_grants, manage_grants, describe, select, modify and manage_tags, where
modify implies select and select implies describe. Stage 8 manages the three
data relations only, on tables the contract declares access for, the way dbt's
``grants`` config manages a model's privileges (dbt-adapters
``diff_of_two_dicts``; ownership excluded, as dbt-snowflake's
``standardize_grants_dict`` excludes OWNERSHIP): what the contract declares is
granted, and any other direct describe/select/modify on that table is revoked.
Ownership and grant management stay with the table's owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

if TYPE_CHECKING:
    from ..iac.access import AccessGrant

#: ``bindings[].provider`` of a Lakekeeper grant; ``policy-apply`` dispatches on it.
PROVIDER = "lakekeeper"
RESOURCE_TYPE = "lakekeeper.table"

#: The table relations stage 8 owns on a managed table, strongest last.
MANAGED_RELATIONS: Tuple[str, ...] = ("describe", "select", "modify")

#: ``accessPolicy`` permission verbs, as the schema enumerates them, to a relation.
VERB_RELATIONS: Mapping[str, str] = {
    "read": "select",
    "select": "select",
    "query": "select",
    "write": "modify",
    "insert": "modify",
    "update": "modify",
    "delete": "modify",
    "manage": "modify",
    "admin": "modify",
}

#: A grant as stage 8 applies it: (relation, principal kind ``user``/``role``, id).
Assignment = Tuple[str, str, str]


def is_lakekeeper_binding(binding: Any) -> bool:
    """Is this ``exposes[].binding`` an Iceberg table cataloged in Lakekeeper?"""
    from ..providers._iceberg_catalog import binding_catalog_kind, is_iceberg_format

    if not isinstance(binding, Mapping) or not isinstance(binding.get("location"), Mapping):
        return False
    return is_iceberg_format(binding.get("format")) and binding_catalog_kind(binding) == PROVIDER


def namespace_levels(value: Any) -> Tuple[str, ...]:
    """``location.database`` as Iceberg namespace levels: ``a.b`` is ``("a", "b")``,
    as Spark and Trino read a dotted namespace."""
    return tuple(part.strip() for part in str(value or "").split(".") if part.strip())


def _table_target(binding: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The table a Lakekeeper binding names, and the location keys it lacks."""
    from ..providers._iceberg_catalog import iceberg_namespace

    loc = binding.get("location") or {}
    target = {
        "catalog_uri": str(loc.get("uri") or "").strip(),
        "warehouse": str(loc.get("warehouse") or "").strip(),
        "namespace": list(namespace_levels(iceberg_namespace(binding))),
        "table": str(loc.get("table") or binding.get("table") or "").strip(),
    }
    missing = [
        key
        for key, present in (
            ("location.uri", target["catalog_uri"]),
            ("location.warehouse", target["warehouse"]),
            ("location.database", target["namespace"]),
            ("location.table", target["table"]),
        )
        if not present
    ]
    return target, missing


def relations_for(permissions: Iterable[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(relations, verbs with no table relation)`` for a grant's permission verbs."""
    relations: Set[str] = set()
    unmapped: List[str] = []
    for verb in permissions:
        relation = VERB_RELATIONS.get(str(verb).strip().lower())
        if relation:
            relations.add(relation)
        elif verb not in unmapped:
            unmapped.append(verb)
    ordered = tuple(r for r in MANAGED_RELATIONS if r in relations)
    return ordered, tuple(unmapped)


def _unenforced_policy(expose: Mapping[str, Any]) -> List[str]:
    """The expose policy blocks Lakekeeper has no permission for: column-level
    restrictions, masking and row filters. Lakekeeper (OpenFGA) grants whole
    tables; those rules belong to the query engine (Trino's OPA plugin)."""
    policy = expose.get("policy")
    if not isinstance(policy, Mapping):
        return []
    found = []
    authz = policy.get("authz")
    if isinstance(authz, Mapping) and authz.get("columnRestrictions"):
        found.append("policy.authz.columnRestrictions")
    privacy = policy.get("privacy")
    if isinstance(privacy, Mapping):
        found += [f"policy.privacy.{k}" for k in ("masking", "rowLevelPolicy") if privacy.get(k)]
    return found


def _written_roles(contract: Mapping[str, Any]) -> Set[str]:
    """The principals the contract itself writes as ``role:<name>``, as written.

    ``iac/access.py`` keeps a prefix it does not know whole and types it ``group``,
    so ``role:analysts`` and ``group:role:analysts`` both reach here as the group
    ``role:analysts``. Only the first, the one the contract spelled with ``role:``,
    is a Lakekeeper role.
    """
    written: List[Any] = []
    policy = contract.get("accessPolicy")
    for entry in (policy.get("grants") or ()) if isinstance(policy, Mapping) else ():
        if isinstance(entry, Mapping):
            written.append(entry.get("principal"))
    legacy = (contract.get("metadata") or {}).get("policies")
    for config in legacy.values() if isinstance(legacy, Mapping) else ():
        if isinstance(config, Mapping):
            written.extend(config.get("principals") or ())
    return {
        p.strip()
        for p in written
        if isinstance(p, str) and p.strip().partition(":")[0].strip().lower() == "role"
    }


def _logical(grant: "AccessGrant", written_roles: Set[str]) -> str:
    """A grant's logical principal: ``role:<name>`` for one the contract wrote as a
    role (:func:`_written_roles`), else its typed key (``group:...``, ``user:...``)."""
    from ..iac.principals import grant_key

    if grant.principal in written_roles:
        return "role:" + grant.principal.partition(":")[2].strip()
    return grant_key(grant)


def compile_lakekeeper_bindings(
    contract: Mapping[str, Any], exposes: Iterable[Mapping[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One binding per Lakekeeper table and logical principal, plus warnings.

    A table no grant and no ``policy.authz`` names gets no binding, so stage 8
    leaves it alone. A principal mapped to ``[]`` in ``binding.principals`` is
    granted nothing. Anything this cannot express is a warning that names the
    expose, the principal and the fix, never a silent drop.

    It fails closed. Stage 8 is authoritative on a table it has bindings for, so
    a table where one declared principal cannot be compiled gets no binding at
    all: with the others alone, stage 8 would revoke that principal's grants. A
    grant whose ``resources`` cannot be evaluated could cover any table, so then
    no Lakekeeper table gets a binding.
    """
    from ..iac.access import normalize_access_grants
    from ..iac.base import UnsupportedBindingError
    from ..iac.column_access import authz_principals
    from ..iac.principals import LAKEKEEPER, principal_map, resolve_principal

    grants = normalize_access_grants(contract)
    written_roles = _written_roles(contract)
    bindings: List[Dict[str, Any]] = []
    warnings: List[str] = []

    unevaluable = [g for g in grants if g.scope.unevaluable]
    if unevaluable:
        for grant in unevaluable:
            warnings.append(
                f"Lakekeeper: the grant to {_logical(grant, written_roles)} names resources "
                f"{list(grant.scope.unevaluable)}, which are not $.exposes[*] or "
                "$.exposes[?(@.exposeId=='<id>')]. It could cover any table, so no Lakekeeper "
                "grant was compiled, and policy-apply leaves every Lakekeeper table's grants as "
                "they are. Scope the grant to exposes by id."
            )
        return bindings, warnings

    for expose in exposes:
        expose_id = str(expose.get("exposeId") or expose.get("id") or "?")
        binding = expose.get("binding") or {}
        # (logical principal, relations, the grant's bare spelling to look it up by)
        declared: List[Tuple[str, Tuple[str, ...], Optional[str]]] = []
        for grant in grants:
            if not grant.scope.covers(expose_id):
                continue
            relations, unmapped = relations_for(grant.permissions)
            if unmapped:
                warnings.append(
                    f"Lakekeeper expose '{expose_id}': {_logical(grant, written_roles)} "
                    f"permission(s) {list(unmapped)} have no table relation in Lakekeeper "
                    "(create is a namespace or warehouse permission), so they were not granted. "
                    "Grant them on the namespace in Lakekeeper itself."
                )
            if relations:
                declared.append((_logical(grant, written_roles), relations, grant.principal))
        for key, relation in (("readers", "select"), ("writers", "modify")):
            for principal in authz_principals(expose, key):
                declared.append((principal, (relation,), None))
        if not declared:
            continue

        unenforced = _unenforced_policy(expose)
        if unenforced:
            warnings.append(
                f"Lakekeeper expose '{expose_id}': {', '.join(unenforced)} cannot be enforced "
                "by Lakekeeper, which grants whole tables: a principal granted select here reads "
                "every column and row with any engine. Grant this table to the identity of a "
                "query engine that enforces those rules (Trino's OPA plugin), not to the readers "
                "the rules restrict."
            )

        target, missing = _table_target(binding)
        if missing:
            principals = sorted({key for key, _, _ in declared})
            warnings.append(
                f"Iceberg expose '{expose_id}' is cataloged in 'lakekeeper', but its binding "
                f"names no {', '.join(missing)}, so no Lakekeeper grant was compiled for "
                f"{principals}. Add them: policy-apply finds the table in Lakekeeper by its "
                "catalog uri, warehouse name, namespace and table."
            )
            continue

        try:
            mapping = principal_map(binding)
        except UnsupportedBindingError as exc:
            warnings.append(f"Lakekeeper expose '{expose_id}': {exc} No grant was compiled.")
            continue

        merged: Dict[str, Tuple[Tuple[str, ...], Set[str]]] = {}
        failed: List[str] = []
        for key, relations, fallback in declared:
            try:
                identities = resolve_principal(
                    key,
                    mapping,
                    platform=LAKEKEEPER,
                    where=f"Lakekeeper expose '{expose_id}'",
                    fallback=fallback,
                )
            except UnsupportedBindingError as exc:
                fix = f" {exc.remediation[0]}" if exc.remediation else ""
                warnings.append(f"{exc}{fix}")
                failed.append(key)
                continue
            if not identities:
                continue  # mapped to []: no identity in Lakekeeper
            known, held = merged.get(key, ((), set()))
            merged[key] = (known or identities, held | set(relations))
        if failed:
            warnings.append(
                f"Lakekeeper expose '{expose_id}': no grant was compiled, because "
                f"{sorted(set(failed))} could not be mapped to a Lakekeeper principal. "
                "policy-apply leaves the table's grants as they are until every principal maps; "
                "granting the others alone would revoke the unmapped ones."
            )
            continue

        table_name = ".".join([*target["namespace"], target["table"]])
        for key in sorted(merged):
            identities, held = merged[key]
            bindings.append(
                {
                    "provider": PROVIDER,
                    "resource_type": RESOURCE_TYPE,
                    "resource_id": f"{target['warehouse']}/{table_name}",
                    "exposeId": expose_id,
                    **target,
                    "principal": key,
                    "identities": list(identities),
                    "relations": [r for r in MANAGED_RELATIONS if r in held],
                }
            )
    return bindings, warnings


def plan_changes(
    desired: Iterable[Assignment], current: Iterable[Assignment]
) -> Tuple[List[Assignment], List[Assignment]]:
    """``(writes, deletes)`` that make the managed relations equal ``desired``.

    ``current`` is every direct assignment on the table; only those in
    :data:`MANAGED_RELATIONS` are compared, so ownership and grant management
    are never revoked. Both lists are sorted, so a plan prints the same twice.
    """
    want = set(desired)
    have = {a for a in current if a[0] in MANAGED_RELATIONS}
    return sorted(want - have), sorted(have - want)


__all__ = [
    "MANAGED_RELATIONS",
    "PROVIDER",
    "RESOURCE_TYPE",
    "VERB_RELATIONS",
    "compile_lakekeeper_bindings",
    "is_lakekeeper_binding",
    "namespace_levels",
    "plan_changes",
    "relations_for",
]
