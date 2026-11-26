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
from typing import Any, FrozenSet, Iterable, Tuple

#: Permission verbs that read (lower-cased, as ``accessPolicy`` permissions are read).
READ_VERBS = frozenset({"read", "select", "query", "admin", "owner"})

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


__all__ = [
    "CANONICAL_PREFIXES",
    "GrantScope",
    "READ_VERBS",
    "logical_key",
    "parse_grant_scope",
]
