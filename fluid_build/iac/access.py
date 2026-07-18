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

"""One canonical read of a contract's access grants for the IaC emitters.

**Why this module exists.** The GCP IaC plugin historically read
``contract["metadata"]["policies"]`` to emit BigQuery ``access[]`` entries
and GCS IAM members. That surface is **not in any shipped schema** — every
version from 0.7.1 to 0.7.6 declares ``metadata`` with
``additionalProperties: false`` and no ``policies`` property — so a contract
carrying it fails ``fluid validate``::

    metadata: Additional properties are not allowed ('policies' was unexpected)

``fluid generate iac`` does not run schema validation, so the emit path
worked while the contract was unusable everywhere else. Users could not
express a cross-project grant in a contract that validates.

**The fix.** Read the schema-valid, already-documented ``accessPolicy``
surface (present in every version, also consumed by ``cli/policy_compile.py``
and ``providers/opds/``), and keep reading ``metadata.policies`` as a
deprecated fallback so existing out-of-tree contracts keep emitting.

**A bug this fixes for free.** The legacy reader classified a principal by
``"@" in principal`` — user if it has an ``@``, group otherwise. Group
addresses contain ``@`` too, so *every group was emitted as a BigQuery
``user_by_email`` entry*. ``accessPolicy`` principals carry an explicit
``user:`` / ``group:`` / ``serviceAccount:`` prefix, so the type is declared
rather than guessed.

**Deliberately not unified here: Snowflake.** ``providers/snowflake.py``
reads ``contract["security"]["access_control"]["grants"]`` and
``contract["security"]["policies"]``, and ``security`` is *also* absent from
every schema. But its shape (``{role, privilege, object_type, object_name}``
— Snowflake-native RBAC against Snowflake objects) does not map onto
``accessPolicy``'s ``{principal, permissions, resources}`` without real loss,
and masking / row-access policies have no ``accessPolicy`` equivalent at all.
Forcing them together would be worse than leaving them separate. That surface
needs its own schema decision; it is tracked, not silently papered over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

#: Principal type prefixes the ``accessPolicy`` schema documents.
USER = "user"
GROUP = "group"
SERVICE_ACCOUNT = "serviceAccount"
DOMAIN = "domain"

_KNOWN_PREFIXES = {
    "user": USER,
    "group": GROUP,
    "serviceaccount": SERVICE_ACCOUNT,
    "domain": DOMAIN,
}

#: Suffix that identifies a Google service account when no prefix is given
#: (the legacy ``metadata.policies`` surface carries bare emails).
_SERVICE_ACCOUNT_SUFFIX = ".gserviceaccount.com"


@dataclass(frozen=True)
class AccessGrant:
    """One normalized access grant, provider-agnostic.

    ``principal`` is the bare identity (prefix stripped);
    ``principal_type`` is declared by ``accessPolicy`` and *inferred* only
    for the legacy surface. ``resources`` carries the optional JSONPath
    scoping expressions from ``accessPolicy`` (empty = applies to all
    exposed resources); the legacy surface has no equivalent.
    """

    principal: str
    principal_type: str
    permissions: Tuple[str, ...]
    resources: Tuple[str, ...] = ()

    @property
    def is_service_account(self) -> bool:
        return self.principal_type == SERVICE_ACCOUNT


def _split_principal(raw: Any) -> Optional[Tuple[str, str]]:
    """``"group:x@y.com"`` → ``("x@y.com", "group")``.

    An unprefixed value is the legacy shape: infer ``serviceAccount`` from
    the Google SA suffix, otherwise fall back to ``user``. The inference is
    only ever applied to unprefixed input — a declared prefix always wins.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    head, sep, rest = text.partition(":")
    if sep and rest.strip():
        mapped = _KNOWN_PREFIXES.get(head.strip().lower())
        if mapped:
            return rest.strip(), mapped
        # Unknown prefix — keep the value whole rather than truncating it.

    # Unprefixed: reproduce the legacy inference EXACTLY, so contracts on the
    # old surface emit byte-identically. Note the residual weakness this
    # carries — a group address like ``data-team@company.com`` has an ``@``
    # and is therefore inferred ``user``. That mis-classification is the bug
    # ``accessPolicy`` fixes by *declaring* the type; it cannot be fixed by
    # inference, and silently changing it here would rewrite existing users'
    # emitted ACLs. Declare ``group:`` to get a group entry.
    lowered = text.lower()
    if lowered.endswith(_SERVICE_ACCOUNT_SUFFIX):
        return text, SERVICE_ACCOUNT
    if "@" not in text:
        return text, GROUP
    return text, USER


def _normalize_permissions(raw: Any) -> Tuple[str, ...]:
    """Lower-case, de-duplicate, and order-preserve the permission verbs."""
    out: list[str] = []
    for item in raw or ():
        verb = str(item or "").strip().lower()
        if verb and verb not in out:
            out.append(verb)
    return tuple(out)


def _from_access_policy(contract: Mapping[str, Any]) -> Tuple[AccessGrant, ...]:
    """The schema-valid surface: ``accessPolicy.grants[]``."""
    policy = contract.get("accessPolicy")
    if not isinstance(policy, Mapping):
        return ()
    grants: list[AccessGrant] = []
    for entry in policy.get("grants") or ():
        if not isinstance(entry, Mapping):
            continue
        split = _split_principal(entry.get("principal"))
        if not split:
            continue
        principal, principal_type = split
        resources = tuple(str(r) for r in (entry.get("resources") or ()) if str(r).strip())
        grants.append(
            AccessGrant(
                principal=principal,
                principal_type=principal_type,
                permissions=_normalize_permissions(entry.get("permissions")),
                resources=resources,
            )
        )
    return tuple(grants)


def grants_from_legacy_policies(policies: Any) -> Tuple[AccessGrant, ...]:
    """Convert the legacy ``{name: {principals, permissions}}`` mapping.

    Two callers need this shape:

    * the deprecated contract surface ``metadata.policies``, and
    * the native planner's **action payloads** (``action["policies"]``),
      which are internal and not schema-governed — they legitimately keep
      this shape, but should still flow through one converter so principal
      typing and de-duplication behave identically everywhere.

    Produces no ``resources`` scoping: the legacy shape never had any.
    """
    if not isinstance(policies, Mapping):
        return ()
    grants: list[AccessGrant] = []
    for config in policies.values():
        if not isinstance(config, Mapping):
            continue
        permissions = _normalize_permissions(config.get("permissions"))
        if not permissions:
            continue
        for raw_principal in config.get("principals") or ():
            split = _split_principal(raw_principal)
            if not split:
                continue
            principal, principal_type = split
            grants.append(
                AccessGrant(
                    principal=principal,
                    principal_type=principal_type,
                    permissions=permissions,
                )
            )
    return tuple(grants)


def _from_legacy_metadata_policies(contract: Mapping[str, Any]) -> Tuple[AccessGrant, ...]:
    """The deprecated contract surface: ``metadata.policies`` (schema-invalid)."""
    return grants_from_legacy_policies((contract.get("metadata") or {}).get("policies"))


def uses_legacy_policy_surface(contract: Mapping[str, Any]) -> bool:
    """True when the contract carries the deprecated ``metadata.policies``."""
    return isinstance((contract.get("metadata") or {}).get("policies"), Mapping)


def normalize_access_grants(contract: Mapping[str, Any]) -> Tuple[AccessGrant, ...]:
    """Every access grant on the contract, from whichever surface declares it.

    ``accessPolicy`` (schema-valid) is read first; ``metadata.policies``
    (deprecated, schema-invalid) is appended for back-compat. Both are read
    rather than either/or so a contract mid-migration does not silently drop
    half its grants. Duplicates — same principal, type, and permission set —
    collapse, and order is stable for deterministic emit.
    """
    seen: set[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]] = set()
    out: list[AccessGrant] = []
    for grant in _from_access_policy(contract) + _from_legacy_metadata_policies(contract):
        if not grant.permissions:
            continue
        key = (grant.principal, grant.principal_type, grant.permissions, grant.resources)
        if key in seen:
            continue
        seen.add(key)
        out.append(grant)
    return tuple(out)


def role_grants(
    grants: Iterable[AccessGrant], role_map: Mapping[str, str]
) -> Tuple[Tuple[str, AccessGrant], ...]:
    """Expand grants to ``(role, grant)`` pairs through a permission→role map.

    Unmapped verbs are skipped. A principal granted two verbs that map to the
    same role yields that role once.
    """
    seen: set[Tuple[str, str, str]] = set()
    out: list[Tuple[str, AccessGrant]] = []
    for grant in grants:
        for permission in grant.permissions:
            role = role_map.get(permission)
            if not role:
                continue
            key = (role, grant.principal, grant.principal_type)
            if key in seen:
                continue
            seen.add(key)
            out.append((role, grant))
    return tuple(out)


__all__ = [
    "AccessGrant",
    "DOMAIN",
    "GROUP",
    "SERVICE_ACCOUNT",
    "USER",
    "normalize_access_grants",
    "role_grants",
    "uses_legacy_policy_surface",
]
