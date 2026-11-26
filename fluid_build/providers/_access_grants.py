# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The reading of access grants that the IaC emitters and the MCP gateway share.

Three definitions live here so that what an emitter writes into a platform policy and
what the gateway enforces are one definition, not copies:

* :class:`GrantScope` and :func:`parse_grant_scope`: which exposes a grant's
  ``resources`` name;
* :func:`logical_key`: a principal as written, with its type prefix spelled one way;
* :data:`READ_VERBS`: the permissions that read.

Only the standard library is imported, because the gateway imports this module and
``fluid_build.iac`` loads every provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Mapping, Tuple

#: Permission verbs that read (lower-cased, as ``accessPolicy`` permissions are read).
READ_VERBS = frozenset({"read", "select", "query", "admin", "owner"})

#: The verbs that read on every platform whose emitter grants from ``accessPolicy``, the
#: ones :func:`every_row_readers` counts. BigQuery reads ``owner``
#: (``roles/bigquery.dataOwner``), but the Unity Catalog table map gives it no privilege,
#: so a grant whose only read verb is ``owner`` reads nothing on Databricks; counting it
#: would let the gateway serve every row to a principal an emitter grants none.
EVERY_ROW_VERBS = frozenset({"read", "select", "query", "admin"})

#: IAM member prefixes, lower-cased, to the spelling IAM expects.
CANONICAL_PREFIXES = {
    "user": "user",
    "group": "group",
    "serviceaccount": "serviceAccount",
    "domain": "domain",
}

# The JSONPath scopes a grant's ``resources`` may carry that an emitter can evaluate: one
# expose by id (``$.exposes[?(@.exposeId=='orders')]``, either quote, spaces allowed), or
# every expose (``$.exposes[*]``, ``$.exposes``). Anything else is kept as written and
# reported as unevaluable, never read as "every expose".
_ONE_EXPOSE_RE = re.compile(
    r"""\$\.exposes\s*\[\s*\?\s*\(\s*@\.exposeId\s*==\s*(['"])([^'"]+)\1\s*\)\s*\]"""
)
_EVERY_EXPOSE_RE = re.compile(r"\$\.exposes(?:\s*\[\s*\*\s*\])?")


@dataclass(frozen=True)
class GrantScope:
    """Which exposes a grant's ``resources`` name.

    A grant without ``resources`` covers every expose (``all_exposes``). ``unevaluable``
    holds each resource that is not one of the shapes above, as written: a grant with any
    is one an emitter cannot place, and :meth:`covers` is then false for every expose.
    """

    all_exposes: bool = True
    expose_ids: FrozenSet[str] = frozenset()
    unevaluable: Tuple[str, ...] = ()

    def covers(self, expose_id: Any) -> bool:
        if self.unevaluable:
            return False
        return self.all_exposes or str(expose_id) in self.expose_ids


def parse_grant_scope(resources: Iterable[str]) -> GrantScope:
    """The :class:`GrantScope` of a grant's ``resources``."""
    resources = tuple(resources)
    if not resources:
        return GrantScope()
    every = False
    ids: set[str] = set()
    unevaluable: list[str] = []
    for resource in resources:
        text = resource.strip()
        if _EVERY_EXPOSE_RE.fullmatch(text):
            every = True
        elif one := _ONE_EXPOSE_RE.fullmatch(text):
            ids.add(one.group(2))
        else:
            unevaluable.append(resource)
    return GrantScope(all_exposes=every, expose_ids=frozenset(ids), unevaluable=tuple(unevaluable))


def logical_key(raw: Any) -> str:
    """A principal as written, with its type prefix spelled one way.

    ``serviceaccount:x`` and ``serviceAccount:x`` name the same principal, as
    ``accessPolicy`` has always read them (``iac/access.py``); the rest of the
    string is kept exactly, since the mapping is looked up by it.
    """
    text = str(raw or "").strip()
    head, sep, rest = text.partition(":")
    canonical = CANONICAL_PREFIXES.get(head.strip().lower())
    if sep and canonical and rest.strip():
        return f"{canonical}:{rest.strip()}"
    return text


def identity_key(raw: Any) -> str:
    """The address a principal names, to tell whether two spellings are one principal.

    :func:`logical_key` without any type prefix, case folded. An emitter reads an
    unprefixed principal as a member of the type it infers (``user:`` for an address,
    ``serviceAccount:`` for a service-account address), so ``alice@x`` and
    ``user:alice@x`` are one member there; keying by the address alone puts them
    together here. It is for comparison only and only ever groups more principals, so
    a subtraction that uses it narrows.
    """
    text = logical_key(raw)
    head, sep, rest = text.partition(":")
    if sep and head.strip().lower() in CANONICAL_PREFIXES and rest.strip():
        text = rest.strip()
    return text.casefold()


class ReaderRuleError(ValueError):
    """The policy that decides who reads every row cannot be read as written."""


def _principal_names(values: Iterable[Any]) -> FrozenSet[str]:
    """The non-blank strings among ``values``, trimmed."""
    return frozenset(item.strip() for item in values if isinstance(item, str) and item.strip())


def _declared_readers(authz: Mapping[str, Any]) -> FrozenSet[str]:
    raw = authz.get("readers")
    if raw is not None and not isinstance(raw, list):
        raise ReaderRuleError("policy.authz.readers must be a list of principals.")
    return _principal_names(raw or ())


def _mapped_identities(
    expose: Mapping[str, Any],
) -> Tuple[Mapping[str, Tuple[str, ...]], FrozenSet[str]]:
    """What ``binding.principals`` maps each principal to, keyed by :func:`identity_key`.

    The value is every identity (as :func:`identity_key`) that principal maps to. The
    second item is the keys of the principals mapped to ``[]``: no identity on this
    cloud, so no one to read. The shapes
    read are the ones ``fluid_build.iac.principals.principal_map`` accepts; any other is
    refused, as the emitters refuse it. A principal mapped to an identity is not
    translated for a caller: the mapping only tells which principals are one.
    """
    binding = expose.get("binding")
    raw = binding.get("principals") if isinstance(binding, Mapping) else None
    if raw is None:
        return {}, frozenset()
    if not isinstance(raw, Mapping):
        raise ReaderRuleError(
            f"binding.principals must be a mapping of logical principal to identity, got "
            f"{type(raw).__name__}."
        )
    found: dict[str, Tuple[str, ...]] = {}
    nobody: set[str] = set()
    for key, value in raw.items():
        name = logical_key(key)
        if not name:
            raise ReaderRuleError("binding.principals has an empty key.")
        if isinstance(value, str):
            identities: Tuple[Any, ...] = (value.strip(),)
        elif isinstance(value, (list, tuple)):
            identities = tuple(item.strip() for item in value if isinstance(item, str))
            if len(identities) != len(value):
                identities = identities + ("",)
        else:
            identities = ("",)
        if any(not identity for identity in identities):
            raise ReaderRuleError(
                f"binding.principals[{name!r}] must be an identity, a list of identities, or "
                "[] for none on this cloud."
            )
        slot = identity_key(name)
        if not identities:
            nobody.add(slot)
        found[slot] = found.get(slot, ()) + tuple(identity_key(i) for i in identities)
    return found, frozenset(nobody)


def _verbs(raw: Any) -> FrozenSet[str]:
    """A grant's permissions, normalized as ``fluid_build.iac.access`` reads them."""
    try:
        return frozenset(v for v in (str(i or "").strip().lower() for i in raw or ()) if v)
    except TypeError:
        return frozenset()


def _holds_ref(node: Any) -> bool:
    """Whether ``node`` holds a ``$ref`` anywhere."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            if "$ref" in item:
                return True
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _read_grantees(expose: Mapping[str, Any], contract: Mapping[str, Any]) -> FrozenSet[str]:
    """The principal of each unconditional read grant of ``accessPolicy`` covering ``expose``."""
    if not isinstance(contract, Mapping):
        raise ReaderRuleError("the contract is not a mapping, so its accessPolicy cannot be read.")
    policy = contract.get("accessPolicy")
    if policy is None:
        return frozenset()
    if not isinstance(policy, Mapping):
        raise ReaderRuleError("accessPolicy is not a mapping, so its grants cannot be read.")
    if _holds_ref(policy):
        raise ReaderRuleError(
            "accessPolicy holds an unresolved $ref, so its grants cannot be read as written; "
            "resolve the document's pointers first."
        )
    grants = policy.get("grants")
    if grants is None:
        return frozenset()
    if not isinstance(grants, list):
        raise ReaderRuleError("accessPolicy.grants is not a list, so its grants cannot be read.")
    expose_id = expose.get("exposeId")
    found: set[str] = set()
    for entry in grants:
        if not isinstance(entry, Mapping):
            continue
        principal = entry.get("principal")
        if not isinstance(principal, str) or not principal.strip():
            continue
        if not EVERY_ROW_VERBS & _verbs(entry.get("permissions")):
            continue
        conditions = entry.get("conditions")
        if conditions is not None and not (isinstance(conditions, Mapping) and not conditions):
            continue
        try:
            resources = tuple(str(r) for r in (entry.get("resources") or ()))
        except TypeError:
            continue
        if parse_grant_scope(resources).covers(expose_id):
            found.add(principal.strip())
    return frozenset(found)


def every_row_readers(
    expose: Mapping[str, Any],
    *,
    contract: Any = None,
    filtered: Iterable[Any] = (),
) -> FrozenSet[str]:
    """The principals, as written, that read every row of an expose with row filters.

    ``policy.authz.readers``, and, only when ``contract`` is given, the principal of each
    ``accessPolicy.grants[]`` entry that has a permission in :data:`EVERY_ROW_VERBS`, no
    condition (``conditions`` absent, ``None`` or empty) and a scope that covers the
    expose. Less every principal ``binding.principals`` maps to ``[]`` and every
    principal in ``filtered`` (the principals a row filter names), compared by
    :func:`identity_key`: the address with the type prefix dropped and the case folded,
    and a principal is also the one that ``binding.principals`` maps it to, so two
    principals that map to one identity are one. Each subtraction only narrows the set.

    The schema has no explicit deny for rows, so there is none to apply. A caller's
    principals match the result as written; nothing here translates a principal
    through ``binding.principals``, which is read only for the exclusion above.
    Raises :class:`ReaderRuleError` for a policy that cannot be read as written.
    """
    policy = expose.get("policy")
    authz = policy.get("authz") if isinstance(policy, Mapping) else None
    candidates = set(_declared_readers(authz) if isinstance(authz, Mapping) else ())
    if contract is not None:
        candidates |= _read_grantees(expose, contract)
    mapping, nobody = _mapped_identities(expose)

    def names(principal: Any) -> FrozenSet[str]:
        """Every key that ``principal`` is known by: its address and what it maps to."""
        key = identity_key(principal)
        return frozenset({key, *mapping.get(key, ())})

    named: set[str] = set()
    for item in filtered:
        named |= names(item)
    return frozenset(
        c for c in candidates if identity_key(c) not in nobody and not names(c) & named
    )


__all__ = [
    "CANONICAL_PREFIXES",
    "EVERY_ROW_VERBS",
    "GrantScope",
    "READ_VERBS",
    "ReaderRuleError",
    "every_row_readers",
    "identity_key",
    "logical_key",
    "parse_grant_scope",
]
