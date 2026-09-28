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

"""Column restrictions: who may read which column, one derivation for every cloud.

``exposes[].policy.authz.columnRestrictions[]`` (``{principal, columns, access:
allow|deny}``, in every bundled schema since 0.7.1) was read only by the MCP output
port, so no cloud enforced it. This module turns it into one answer, "the set of
identities that may read column c", which each emitter writes in its own terms and
``fluid verify`` checks against the live platform:

* GCP (``iac/providers/gcp_governance.py``): each restricted column gets a Data
  Catalog policy tag, and the fine-grained reader role on the tag goes to exactly
  that set.
* AWS (:func:`lf_exclusions`): each Lake Formation grant's ``excludedColumns`` are the
  restricted columns its principal is not in the set for.

The semantics, the same on both clouds:

* A column named in any restriction is restricted.
* ``deny``: the principal may not read the columns.
* ``allow``: the columns are readable only by the principals an ``allow`` names
  (an allow list per column); a column with no ``allow`` is readable by every reader
  of the expose.
* A deny beats an allow. A restriction never grants access: the readers are
  intersected with the expose's readers (on GCP the ``accessPolicy`` read grants and
  the expose's own ``policy.authz.readers``; on AWS the Lake Formation ``SELECT``
  grants), and an allowed principal that is not a reader is reported, not added.
  With no reader at all a restriction is refused on both clouds: it would lock the
  columns for everyone, not only for the principals it names.

Principals are logical and resolve through ``binding.principals``
(:mod:`fluid_build.iac.principals`), so the same restriction names the analyst role
on AWS and the analysts' group on GCP. A restriction the platform cannot enforce is
refused, never dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .base import UnsupportedBindingError
from .principals import AWS, principal_map, resolve_principal

LOG = logging.getLogger(__name__)

ALLOW = "allow"
DENY = "deny"

#: Lake Formation permissions that let a principal read a table's rows.
LF_READ_PERMISSIONS = frozenset({"SELECT", "ALL"})


@dataclass(frozen=True)
class Restriction:
    """One ``columnRestrictions[]`` entry, validated."""

    principal: str
    columns: Tuple[str, ...]
    access: str
    #: The rule's ``tags`` and ``labels``: descriptive, carried to the cloud object
    #: that holds the restriction where it has room for them (GCP's policy tag).
    tags: Tuple[str, ...] = ()
    labels: Tuple[Tuple[str, str], ...] = ()


def _where(exposure: Mapping[str, Any], index: int) -> str:
    expose_id = exposure.get("exposeId") or exposure.get("id") or index
    return f"exposes[{expose_id}].policy.authz.columnRestrictions"


def restrictions_for(exposure: Mapping[str, Any], index: int = 0) -> Tuple[Restriction, ...]:
    """The expose's column restrictions, or ``()``; a malformed one is refused.

    Every column must be one the expose's schema declares: a restriction on a column
    the table does not have protects nothing, and on GCP it cannot be attached.
    """
    policy = exposure.get("policy") if isinstance(exposure, Mapping) else None
    authz = policy.get("authz") if isinstance(policy, Mapping) else None
    raw = authz.get("columnRestrictions") if isinstance(authz, Mapping) else None
    if not raw:
        return ()
    where = _where(exposure, index)
    if not isinstance(raw, list):
        raise UnsupportedBindingError(
            "column-restriction", f"{where} must be a list of restrictions.", ()
        )
    schema = (exposure.get("contract") or {}).get("schema") or []
    declared = {col.get("name") for col in schema if isinstance(col, Mapping)}
    out: List[Restriction] = []
    for i, entry in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(entry, Mapping):
            raise UnsupportedBindingError("column-restriction", f"{at} must be a mapping.", ())
        principal = entry.get("principal")
        columns = entry.get("columns")
        access = str(entry.get("access") or "").strip().lower()
        if not isinstance(principal, str) or not principal.strip():
            raise UnsupportedBindingError(
                "column-restriction",
                f"{at} names no principal, so there is no one to restrict.",
                ("Set principal to the logical principal the restriction applies to.",),
            )
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c for c in columns)
        ):
            raise UnsupportedBindingError(
                "column-restriction",
                f"{at} must list the columns it restricts.",
                ("Set columns to one or more column names of this expose's schema.",),
            )
        if access not in (ALLOW, DENY):
            raise UnsupportedBindingError(
                "column-restriction",
                f"{at}.access is {entry.get('access')!r}; it must be 'allow' or 'deny', so "
                "the cloud can be told which way the restriction goes.",
                (
                    "Set access: deny to hide the columns from the principal, or allow to make "
                    "the principal one of the columns' only readers.",
                ),
            )
        unknown = [c for c in columns if c not in declared]
        if unknown:
            raise UnsupportedBindingError(
                "column-restriction",
                f"{at} restricts {unknown}, which the expose's schema does not declare, so "
                "nothing would be protected.",
                ("Name columns of exposes[].contract.schema, or add the columns to it.",),
            )
        tags = entry.get("tags") or ()
        labels = entry.get("labels") or {}
        out.append(
            Restriction(
                principal=principal.strip(),
                columns=tuple(columns),
                access=access,
                tags=tuple(str(t) for t in tags) if isinstance(tags, (list, tuple)) else (),
                labels=(
                    tuple(sorted((str(k), str(v)) for k, v in labels.items()))
                    if isinstance(labels, Mapping)
                    else ()
                ),
            )
        )
    return tuple(out)


def authz_readers(exposure: Mapping[str, Any]) -> Tuple[str, ...]:
    """``exposes[].policy.authz.readers``: the expose's own readers, logical principals.

    Readers a column restriction narrows, alongside the ``accessPolicy`` read grants.
    No emitter grants them anything: their access to the table is managed elsewhere,
    and a restriction must not take a column away from them unless it names them.
    """
    policy = exposure.get("policy") if isinstance(exposure, Mapping) else None
    authz = policy.get("authz") if isinstance(policy, Mapping) else None
    raw = authz.get("readers") if isinstance(authz, Mapping) else None
    if not isinstance(raw, list):
        return ()
    return tuple(r.strip() for r in raw if isinstance(r, str) and r.strip())


def restricted_columns(
    exposure: Mapping[str, Any], restrictions: Sequence[Restriction]
) -> Tuple[str, ...]:
    """The restricted columns, in the schema's order."""
    named = {c for r in restrictions for c in r.columns}
    schema = (exposure.get("contract") or {}).get("schema") or []
    return tuple(
        col["name"] for col in schema if isinstance(col, Mapping) and col.get("name") in named
    )


def column_readers(
    exposure: Mapping[str, Any],
    restrictions: Sequence[Restriction],
    resolve: Callable[[str], Tuple[str, ...]],
    readers: FrozenSet[str],
    *,
    where: str,
) -> Dict[str, FrozenSet[str]]:
    """``{restricted column: identities that may read it}`` (see the module docstring).

    ``resolve`` turns a logical principal into its identities on the platform, and
    ``readers`` is every identity that reads the expose there.
    """
    allowed: Dict[str, set[str]] = {}
    denied: Dict[str, set[str]] = {}
    for restriction in restrictions:
        identities = set(resolve(restriction.principal))
        target = allowed if restriction.access == ALLOW else denied
        for column in restriction.columns:
            target.setdefault(column, set()).update(identities)
    out: Dict[str, FrozenSet[str]] = {}
    for column in restricted_columns(exposure, restrictions):
        if column in allowed:
            not_readers = sorted(allowed[column] - readers)
            if not_readers:
                LOG.warning(
                    "column_restriction_allow_not_a_reader %s column=%s identities=%s: a "
                    "restriction never grants access, so these identities, which have no "
                    "read grant on the expose, still cannot read it",
                    where,
                    column,
                    not_readers,
                )
            base = allowed[column] & readers
        else:
            base = set(readers)
        out[column] = frozenset(base - denied.get(column, set()))
    return out


# ── AWS: Lake Formation excluded columns ────────────────────────────────


def _lf_grant_where(where: str, index: int) -> str:
    return f"{where}: governance.lakeFormation.grants[{index}]"


def lf_exclusions(
    exposure: Mapping[str, Any], binding: Mapping[str, Any], index: int = 0
) -> Optional[Dict[int, Tuple[str, ...]]]:
    """``{grant index: excluded columns}`` for an AWS binding's Lake Formation grants.

    ``None`` when the expose declares no column restriction: every existing overlay
    then emits what it did, its hand-written ``excludedColumns`` included. With
    restrictions, each read grant's excluded columns are the restricted ones its
    principal may not read, and a grant that already declares ``excludedColumns``
    (or a ``columns`` projection) must agree, or the emit is refused rather than
    left with two policies that disagree.
    """
    restrictions = restrictions_for(exposure, index)
    if not restrictions:
        return None
    where = _where(exposure, index)
    gov = (binding.get("governance") or {}).get("lakeFormation") or {}
    grants = gov.get("grants") if isinstance(gov, Mapping) else None
    if not grants:
        raise UnsupportedBindingError(
            "column-restriction-unenforceable",
            f"{where} restricts columns, but this aws binding declares no "
            "governance.lakeFormation.grants, the only column-level control the AWS emitter "
            "writes. Nothing would enforce the restriction.",
            (
                "Add governance.lakeFormation to the aws overlay's binding: registerLocation: "
                "true and a grant for each reader; forge-cli then writes the restricted "
                "columns into each grant's excluded columns.",
            ),
        )
    mapping = principal_map(binding)

    def resolve(principal: str) -> Tuple[str, ...]:
        return resolve_principal(principal, mapping, platform=AWS, where=where)

    readers = frozenset(
        str(g.get("principal"))
        for g in grants
        if isinstance(g, Mapping)
        and g.get("principal")
        and LF_READ_PERMISSIONS & set(g.get("permissions") or ())
    )
    by_column = column_readers(exposure, restrictions, resolve, readers, where=where)
    out: Dict[int, Tuple[str, ...]] = {}
    for i, grant in enumerate(grants):
        if not isinstance(grant, Mapping):
            continue
        principal = str(grant.get("principal") or "")
        if not principal or not LF_READ_PERMISSIONS & set(grant.get("permissions") or ()):
            continue
        excluded = tuple(c for c, allowed in by_column.items() if principal not in allowed)
        declared_excluded = grant.get("excludedColumns")
        declared_columns = grant.get("columns")
        if declared_columns:
            leaked = [c for c in declared_columns if c in excluded]
            if leaked:
                raise UnsupportedBindingError(
                    "column-restriction-conflict",
                    f"{_lf_grant_where(where, i)} grants {principal} the columns {leaked}, "
                    "which the contract's column restrictions do not let it read.",
                    ("Drop those columns from the grant's columns, or change the restriction.",),
                )
            continue
        if declared_excluded is not None and set(declared_excluded) != set(excluded):
            raise UnsupportedBindingError(
                "column-restriction-conflict",
                f"{_lf_grant_where(where, i)} excludes {sorted(declared_excluded)} for "
                f"{principal}, but the contract's column restrictions exclude "
                f"{sorted(excluded)}. The two must agree.",
                (
                    "Remove excludedColumns from the grant (forge-cli writes it from the "
                    "restrictions), or change it or the restrictions until they match.",
                ),
            )
        if excluded:
            out[i] = excluded
    return out


def lf_expected_exclusions(
    exposure: Mapping[str, Any], binding: Mapping[str, Any], index: int = 0
) -> Dict[str, Tuple[str, ...]]:
    """``{principal ARN: columns it must not be able to read}``, for ``fluid verify``.

    Every read grantee whose restricted columns are not all readable, and every
    identity a ``deny`` names, whether or not the contract grants it anything: a
    grant made outside the contract must not reach a denied column either.
    ``IAM_ALLOWED_PRINCIPALS`` is held to every restricted column, since a table that
    still grants it (Lake Formation's default for a new table) lets any IAM principal
    with S3 access read every column.
    """
    restrictions = restrictions_for(exposure, index)
    if not restrictions:
        return {}
    exclusions = lf_exclusions(exposure, binding, index) or {}
    grants = ((binding.get("governance") or {}).get("lakeFormation") or {}).get("grants") or []
    where = _where(exposure, index)
    mapping = principal_map(binding)
    expected: Dict[str, set[str]] = {}
    for i, columns in exclusions.items():
        expected.setdefault(str(grants[i].get("principal")), set()).update(columns)
    for restriction in restrictions:
        if restriction.access != DENY:
            continue
        for identity in resolve_principal(
            restriction.principal, mapping, platform=AWS, where=where
        ):
            expected.setdefault(identity, set()).update(restriction.columns)
    expected.setdefault("IAM_ALLOWED_PRINCIPALS", set()).update(
        restricted_columns(exposure, restrictions)
    )
    order = restricted_columns(exposure, restrictions)
    return {
        principal: tuple(c for c in order if c in columns)
        for principal, columns in sorted(expected.items())
        if columns
    }


__all__ = [
    "ALLOW",
    "DENY",
    "LF_READ_PERMISSIONS",
    "Restriction",
    "authz_readers",
    "column_readers",
    "lf_exclusions",
    "lf_expected_exclusions",
    "restricted_columns",
    "restrictions_for",
]
