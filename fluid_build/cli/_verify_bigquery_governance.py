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

"""``fluid verify``: the BigQuery dataset and table apply the governance the contract declares.

Three dimensions of the BigQuery verifier (``verify.verify_bigquery_table``), each
present only when the expose declares it, read from the live dataset and table:

* ``retention`` — ``exposes[].lifecycle {retention, expire: true}``. The table must
  be partitioned by day (on ``binding.location.partitionBy`` when it names a
  column), its partitions must expire after exactly the period, and the table
  itself must carry no expiration time, which would delete the product.
* ``encryption`` — ``binding.encryption.kms``. ``kmsKeyName`` of the table, and of
  the dataset's default when this product owns the dataset, must be the declared
  key (the product key's resource name, or the one written).
* ``columnRestrictions`` — ``exposes[].policy.authz.columnRestrictions``. Every
  restricted column must carry a policy tag; the members holding
  ``roles/datacatalog.categoryFineGrainedReader`` on it (Data Catalog
  ``getIamPolicy``) must be exactly the readers the contract derives, a denied
  principal among them fails and so does one granted outside the contract; and the
  tag's taxonomy must enforce fine-grained access control.

What is expected comes from ``iac/providers/gcp_governance.py``, the derivation
``fluid apply`` emits from, so verify checks what apply wrote. A mismatch is CRITICAL
(``fluid verify --strict`` fails); a check that could not run is an error, which
fails ``fluid verify`` with or without ``--strict``. The same shape as the S3 +
Glue storage checks (``_verify_storage_policy.py``).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.verify.bigquery_governance")

#: The Data Catalog API that holds policy tags and their IAM policies.
DATACATALOG_API = "https://datacatalog.googleapis.com/v1"

SessionFactory = Callable[[], Any]

#: The variable python-bigquery reads for an emulator endpoint. No emulator
#: serves Data Catalog, so under it the policy tags' readers cannot be read.
_BIGQUERY_EMULATOR_HOST = "BIGQUERY_EMULATOR_HOST"


def _catalog_unreachable() -> Optional[str]:
    """Why Data Catalog cannot be asked here, or ``None`` when it can."""
    if os.environ.get(_BIGQUERY_EMULATOR_HOST, "").strip():
        return (
            f"{_BIGQUERY_EMULATOR_HOST} points BigQuery at an emulator, and no emulator "
            "serves Data Catalog, where the policy tags' readers are"
        )
    return None


def default_session() -> Any:
    """An authorized HTTP session with Application Default Credentials."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)  # type: ignore[no-untyped-call]


def _key_name(value: Any) -> Optional[str]:
    """A ``kmsKeyName`` without the ``/cryptoKeyVersions/<n>`` suffix, or ``None``."""
    text = str(value or "").strip()
    if not text:
        return None
    return text.split("/cryptoKeyVersions/", 1)[0]


def _retention_dimension(bq_table: Any, expected: Any) -> Dict[str, Any]:
    partitioning = getattr(bq_table, "time_partitioning", None)
    expires = getattr(bq_table, "expires", None)
    problems: List[str] = []
    actual_ms = getattr(partitioning, "expiration_ms", None) if partitioning else None
    if partitioning is None:
        problems.append("the table is not partitioned, so no partition ever expires")
    else:
        kind = str(getattr(partitioning, "type_", "") or "")
        if kind.upper() != "DAY":
            problems.append(f"the table is partitioned by {kind or 'nothing'}, not by DAY")
        field = getattr(partitioning, "field", None) or None
        if field != expected.field:
            problems.append(
                f"the table is partitioned on {field or 'ingestion time'}, not on "
                f"{expected.field or 'ingestion time'}"
            )
        if actual_ms != expected.expiration_ms:
            shown = "never" if not actual_ms else f"after {int(actual_ms) / 86_400_000:g} days"
            problems.append(
                f"its partitions expire {shown}, but lifecycle.retention is {expected.period} "
                f"({expected.days} days)"
            )
    if expires is not None:
        when = expires.isoformat() if hasattr(expires, "isoformat") else str(expires)
        problems.append(
            f"the table itself expires at {when}, which deletes the whole product, not only "
            "the partitions older than the retention"
        )
    return {
        "status": "fail" if problems else "pass",
        "expected_expiration_ms": expected.expiration_ms,
        "actual_expiration_ms": actual_ms,
        "message": (
            "Retention: " + "; ".join(problems)
            if problems
            else f"Retention: daily partitions expire after {expected.days} days"
        ),
    }


def _encryption_dimension(
    bq_dataset: Any, bq_table: Any, expected: str, *, dataset_owned: bool
) -> Dict[str, Any]:
    table_config = getattr(bq_table, "encryption_configuration", None)
    table_key = _key_name(getattr(table_config, "kms_key_name", None))
    problems: List[str] = []
    if table_key != expected:
        problems.append(f"the table is encrypted with {table_key or 'a Google-managed key'}")
    dataset_key = None
    if dataset_owned:
        dataset_config = getattr(bq_dataset, "default_encryption_configuration", None)
        dataset_key = _key_name(getattr(dataset_config, "kms_key_name", None))
        if dataset_key != expected:
            problems.append(f"the dataset's default key is {dataset_key or 'a Google-managed key'}")
    return {
        "status": "fail" if problems else "pass",
        "expected": expected,
        "table": table_key,
        "dataset": dataset_key,
        "message": (
            f"Encryption: {'; '.join(problems)}, not {expected}"
            if problems
            else f"Encryption: {expected}"
        ),
    }


class _CatalogError(Exception):
    pass


def _catalog_json(session: Any, method: str, url: str) -> Mapping[str, Any]:
    try:
        response = (
            session.post(url, json={}) if method == "POST" else session.get(url)
        )  # noqa: S113 — AuthorizedSession applies its own timeout
    except Exception as exc:  # noqa: BLE001 — reported as the dimension's error
        raise _CatalogError(f"{method} {url} failed: {type(exc).__name__}") from None
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise _CatalogError(f"{method} {url} returned HTTP {status}")
    body = response.json()
    return body if isinstance(body, Mapping) else {}


def _column_access_dimension(
    bq_table: Any, groups: List[Any], session_factory: SessionFactory
) -> Dict[str, Any]:
    """Every restricted column's tag, then (through Data Catalog) each tag's readers.

    The tags are on the table, so a column that carries none is reported from
    the table alone. The Data Catalog session is opened only for a tag whose
    readers must be read: it used to be opened first, so a table with no tags
    at all, checked where no credentials were (the BigQuery emulator), was an
    error about credentials instead of the failure it is.
    """
    fields = {getattr(f, "name", None): f for f in (getattr(bq_table, "schema", None) or [])}
    problems: List[str] = []
    tagged: List[Tuple[Any, Dict[str, List[str]]]] = []
    for group in groups:
        by_tag: Dict[str, List[str]] = {}
        for column in group.columns:
            field = fields.get(column)
            tags = getattr(field, "policy_tags", None) if field is not None else None
            names = list(getattr(tags, "names", None) or ())
            if len(names) != 1:
                problems.append(
                    f"column {column} carries no policy tag, so every reader of the table "
                    "can read it"
                )
                continue
            by_tag.setdefault(str(names[0]), []).append(column)
        tagged.append((group, by_tag))
    if not any(by_tag for _, by_tag in tagged):
        return _column_access_result(problems, [])

    unreachable = _catalog_unreachable()
    session: Any = None
    if unreachable is None:
        try:
            session = session_factory()
        except Exception as exc:  # noqa: BLE001 — reported, with what was found
            unreachable = f"no Data Catalog session: {type(exc).__name__}: {exc}"
    if unreachable is not None:
        note = f"the readers of the tagged columns were not checked: {unreachable}"
        if problems:
            # What the table shows is conclusive; say what could not be read.
            return _column_access_result(problems + [note], [])
        if _catalog_unreachable() is not None:
            return {
                "status": "unsupported",
                "tags": [],
                "message": (
                    "Column restrictions: every restricted column carries a policy tag; " + note
                ),
            }
        raise _CatalogError(unreachable)
    try:
        return _column_access_readers(tagged, problems, session)
    except _CatalogError as exc:
        if not problems:
            raise
        return _column_access_result(
            problems + [f"the readers of the tagged columns were not all checked: {exc}"], []
        )


def _column_access_result(problems: List[str], checked: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "fail" if problems else "pass",
        "tags": checked,
        "message": (
            "Column restrictions: " + "; ".join(problems)
            if problems
            else "Column restrictions: every restricted column is readable only by its readers"
        ),
    }


def _column_access_readers(
    tagged: List[Tuple[Any, Dict[str, List[str]]]], problems: List[str], session: Any
) -> Dict[str, Any]:
    """Each tag's fine-grained readers against the group's, and its taxonomy's enforcement."""
    from fluid_build.iac.providers.gcp_governance import FINE_GRAINED_READER_ROLE

    checked: List[Dict[str, Any]] = []
    taxonomies: Dict[str, bool] = {}
    for group, by_tag in tagged:
        for tag, columns in by_tag.items():
            policy = _catalog_json(session, "POST", f"{DATACATALOG_API}/{tag}:getIamPolicy")
            members: set[str] = set()
            for binding in policy.get("bindings") or ():
                if isinstance(binding, Mapping) and binding.get("role") == FINE_GRAINED_READER_ROLE:
                    members.update(str(m) for m in binding.get("members") or ())
            expected = set(group.readers)
            extra = sorted(members - expected)
            missing = sorted(expected - members)
            if extra:
                problems.append(
                    f"{', '.join(extra)} can read {', '.join(columns)} (fine-grained reader on "
                    "its policy tag), which the contract's column restrictions do not allow"
                )
            if missing:
                problems.append(
                    f"{', '.join(missing)} cannot read {', '.join(columns)}, which the "
                    "contract allows"
                )
            taxonomy = tag.split("/policyTags/", 1)[0]
            if taxonomy not in taxonomies:
                body = _catalog_json(session, "GET", f"{DATACATALOG_API}/{taxonomy}")
                taxonomies[taxonomy] = "FINE_GRAINED_ACCESS_CONTROL" in (
                    body.get("activatedPolicyTypes") or ()
                )
                if not taxonomies[taxonomy]:
                    problems.append(
                        f"the taxonomy {taxonomy} does not enforce fine-grained access "
                        "control, so its policy tags restrict no one"
                    )
            checked.append({"tag": tag, "columns": columns, "readers": sorted(members)})
    return _column_access_result(problems, checked)


def governance_dimensions(
    expose: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    bq_dataset: Any,
    bq_table: Any,
    project: str,
    index: int = 0,
    session_factory: Optional[SessionFactory] = None,
) -> Dict[str, Dict[str, Any]]:
    """The ``retention``, ``encryption`` and ``columnRestrictions`` dimensions ``expose`` declares.

    Never raises for a GCP failure or a declaration apply would refuse: the reason
    is the dimension (``status: error``).
    """
    from fluid_build.cli._verify_athena import _resolved_binding
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.naming import safe_ident
    from fluid_build.iac.providers import gcp_governance as gov
    from fluid_build.iac.providers.gcp import _bq_table_name

    binding = _resolved_binding(expose.get("binding"))
    resolved = {**expose, "binding": binding}
    loc = binding.get("location") or {}
    dimensions: Dict[str, Dict[str, Any]] = {}
    cid = safe_ident(contract.get("id") or contract.get("name") or "product")
    dataset = str(loc.get("dataset") or "default")

    def attempt(name: str, check: Callable[[], Optional[Dict[str, Any]]]) -> None:
        try:
            dimension = check()
        except UnsupportedBindingError as exc:
            dimension = {"status": "error", "message": f"{name}: {exc}"}
        except _CatalogError as exc:
            dimension = {"status": "error", "message": f"{name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 — every GCP failure is reported, not raised
            dimension = {"status": "error", "message": f"{name}: {type(exc).__name__}: {exc}"}
        if dimension is not None:
            dimensions[name] = dimension

    def retention() -> Optional[Dict[str, Any]]:
        expected = gov.retention_for(resolved, index)
        return None if expected is None else _retention_dimension(bq_table, expected)

    def encryption() -> Optional[Dict[str, Any]]:
        expected = gov.encryption_for(binding, gov.dataset_location(loc))
        if expected is None:
            return None
        key = (
            gov.product_key_name(
                str(loc.get("project") or project),
                expected.location,
                gov.product_key_ring(cid, dataset),
            )
            if expected.product_key
            else expected.kms
        )
        from fluid_build.iac.packaging import resolve_packaging
        from fluid_build.iac.providers.gcp import _placement

        # A shared (pool) dataset's default key is its owner's; only the table is ours.
        owned = not _placement(resolve_packaging(contract), resolved).dataset_referenced
        return _encryption_dimension(bq_dataset, bq_table, key, dataset_owned=owned)

    def columns() -> Optional[Dict[str, Any]]:
        groups = gov.tag_groups(
            contract, resolved, cid, dataset, _bq_table_name(resolved, loc), index
        )
        if not groups:
            return None
        return _column_access_dimension(bq_table, groups, session_factory or default_session)

    attempt("retention", retention)
    attempt("encryption", encryption)
    attempt("columnRestrictions", columns)
    for name, dimension in dimensions.items():
        LOG.info("verify_bigquery_governance dimension=%s status=%s", name, dimension["status"])
    return dimensions


#: What to do about a failed dimension.
_ACTIONS = {
    "retention": (
        "Re-apply so the table's partition expiration matches lifecycle.retention (adding "
        "partitioning replaces the table: --allow-data-loss), and remove any table "
        "expiration set outside the contract"
    ),
    "encryption": (
        "Re-apply so the dataset's default key and the table's key are the declared key "
        "(re-keying a table replaces it: --allow-data-loss)"
    ),
    "columnRestrictions": (
        "Re-apply so each restricted column carries its policy tag with exactly the "
        "contract's fine-grained readers, and revoke roles/datacatalog.categoryFineGrainedReader "
        "from any principal granted it outside the contract"
    ),
}


def governance_problems(dimensions: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """``(message, action)`` for every failed governance dimension."""
    return [
        (str(dimension["message"]), _ACTIONS[name])
        for name, dimension in dimensions.items()
        if name in _ACTIONS and dimension.get("status") == "fail"
    ]


def governance_errors(dimensions: Mapping[str, Any]) -> List[str]:
    """The message of every governance dimension that could not be checked."""
    return [
        str(dimension["message"])
        for name, dimension in dimensions.items()
        if name in _ACTIONS and dimension.get("status") == "error"
    ]


def with_governance_severity(
    severity: Dict[str, Any], dimensions: Mapping[str, Any]
) -> Dict[str, Any]:
    """``severity`` raised to CRITICAL with each failed governance dimension's reason."""
    problems = governance_problems(dimensions)
    if not problems:
        return severity
    already = severity.get("level") == "CRITICAL"
    return {
        "level": "CRITICAL",
        "impact": "HIGH",
        "symbol": "🔴",
        "remediation": "MANUAL_INTERVENTION_REQUIRED",
        "reason": "; ".join(([severity["reason"]] if already else []) + [p[0] for p in problems]),
        "actions": (list(severity.get("actions") or []) if already else [])
        + [p[1] for p in problems],
    }


__all__ = [
    "DATACATALOG_API",
    "default_session",
    "governance_dimensions",
    "governance_errors",
    "governance_problems",
    "with_governance_severity",
]
