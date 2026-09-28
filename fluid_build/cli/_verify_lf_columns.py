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

"""``fluid verify``: Lake Formation lets no principal read a column the contract restricts from it.

The ``columnRestrictions`` dimension of the S3 + Glue verifier, present only when
the expose declares ``policy.authz.columnRestrictions``. What is expected comes
from ``iac/column_access.py`` (``lf_expected_exclusions``), the derivation the
emitter writes each grant's excluded columns from: for every read grantee, every
principal a ``deny`` names, and ``IAM_ALLOWED_PRINCIPALS``, the columns it must not
read. ``lakeformation:ListPermissions`` on tables then fails the check when any of
that principal's ``SELECT`` (or ``ALL``) permissions on this table reaches one of
them: a table-level grant (every column), a column list naming it, or a column
wildcard that does not exclude it. So a grant made outside the contract is caught
too. A mismatch is CRITICAL; a check that could not run is an error.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

LOG = logging.getLogger("fluid.cli.verify.lf_columns")

ClientFactory = Callable[[str, str], Any]


def _readable(resource: Mapping[str, Any], database: str, table: str) -> Optional[Set[str]]:
    """Columns of ``database.table`` this permission's resource reaches; ``None`` for none.

    The empty set with the ``"*"`` member means every column.
    """
    whole = {"*"}
    if "Table" in resource:
        ref = resource.get("Table") or {}
        if ref.get("DatabaseName") != database:
            return None
        if ref.get("Name") == table or "TableWildcard" in ref:
            return whole
        return None
    if "TableWithColumns" in resource:
        ref = resource.get("TableWithColumns") or {}
        if ref.get("DatabaseName") != database or ref.get("Name") != table:
            return None
        if ref.get("ColumnNames"):
            return set(ref["ColumnNames"])
        wildcard = ref.get("ColumnWildcard")
        if wildcard is not None:
            return {"*", *(f"-{c}" for c in (wildcard.get("ExcludedColumnNames") or ()))}
        return whole
    return None


def _leaks(readable: Set[str], forbidden: List[str]) -> List[str]:
    if "*" in readable:
        return [c for c in forbidden if f"-{c}" not in readable]
    return [c for c in forbidden if c in readable]


def _table_permissions(lf: Any) -> List[Mapping[str, Any]]:
    """Every table permission the caller can see (``ResourceType: TABLE``, paginated).

    One listing, filtered here by principal and table. ListPermissions takes a
    ``Principal`` only together with a ``Resource`` ("Resource is mandatory if
    Principal is set"), and a ``Table`` resource does not return the column-level
    grants, which are the ones this check is about; ``IAM_ALLOWED_PRINCIPALS`` is
    not an ARN either. The caller must be allowed to list the table's
    permissions (a Lake Formation administrator, or a grantable holder).
    """
    entries: List[Mapping[str, Any]] = []
    kwargs: Dict[str, Any] = {"ResourceType": "TABLE"}
    while True:
        page = lf.list_permissions(**kwargs)
        entries.extend(page.get("PrincipalResourcePermissions") or [])
        token = page.get("NextToken")
        if not token:
            return entries
        kwargs["NextToken"] = token


def column_restrictions_dimension(
    expose_id: str,
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    region: str,
    factory: ClientFactory,
) -> Optional[Dict[str, Any]]:
    """The ``columnRestrictions`` dimension, or ``None`` when the expose declares none."""
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.column_access import LF_READ_PERMISSIONS, lf_expected_exclusions

    exposure = {
        "exposeId": expose_id,
        "policy": expose.get("policy"),
        "contract": expose.get("contract"),
    }
    try:
        expected = lf_expected_exclusions(exposure, binding)
    except UnsupportedBindingError as exc:
        return {"status": "error", "message": f"Column restrictions: {exc}"}
    if not expected:
        return None
    loc = binding.get("location") or {}
    database, table = str(loc.get("database") or ""), str(loc.get("table") or "")
    try:
        lf = factory("lakeformation", region)
    except Exception as exc:  # noqa: BLE001 — e.g. boto3 missing
        return {
            "status": "error",
            "message": f"Column restrictions: could not create the Lake Formation client: {exc}",
        }
    try:
        permissions = _table_permissions(lf)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        from fluid_build.cli._verify_athena import _error_code

        return {
            "status": "error",
            "message": (
                "Column restrictions: lakeformation:ListPermissions failed "
                f"({_error_code(exc) or type(exc).__name__})"
            ),
        }
    problems: List[str] = []
    checked: Dict[str, List[str]] = {}
    for principal, forbidden in expected.items():
        leaked: List[str] = []
        for entry in permissions:
            holder = (entry.get("Principal") or {}).get("DataLakePrincipalIdentifier")
            if holder != principal:
                continue
            if not LF_READ_PERMISSIONS & set(entry.get("Permissions") or ()):
                continue
            readable = _readable(entry.get("Resource") or {}, database, table)
            if readable is None:
                continue
            leaked.extend(c for c in _leaks(readable, list(forbidden)) if c not in leaked)
        checked[principal] = list(forbidden)
        if leaked:
            problems.append(
                f"{principal} can read {', '.join(leaked)} of {database}.{table} through "
                "Lake Formation, which the contract's column restrictions do not allow"
            )
    LOG.info(
        "verify_lf_columns expose=%s principals=%d problems=%d",
        expose_id,
        len(checked),
        len(problems),
    )
    return {
        "status": "fail" if problems else "pass",
        "checked": checked,
        "message": (
            "Column restrictions: " + "; ".join(problems)
            if problems
            else "Column restrictions: no principal can read a column restricted from it"
        ),
    }


__all__ = ["column_restrictions_dimension"]
