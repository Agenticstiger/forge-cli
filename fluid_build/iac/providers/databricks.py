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

"""Databricks IaC plugin — FLUID contract → Unity Catalog ``.tf.json``.

Translates Databricks-bound exposures into ``databricks/databricks``
resources, with no new schema field (fluid-schema 0.7.6):

* ``binding.location.database`` names an EXISTING Unity Catalog catalog. It is
  read with ``data.databricks_catalog``, so a catalog that does not exist fails
  ``tofu plan`` (``--mode dry-run`` included) rather than an apply that has
  already created part of the module. forge-cli never creates, grants on or
  destroys a catalog: it does not own one.
* ``binding.location.schema`` is a ``databricks_schema`` the product owns,
  never ``force_destroy``: removing a schema that still holds tables fails.
* ``binding.location.table`` is a managed Delta ``databricks_sql_table``. Its
  columns come from ``exposes[].contract.schema``, typed through the table the
  dbt engine uses (``engines/dbt/_types.py``), so the table and a dbt model
  contract agree on every type. ``delta.columnMapping.mode=name`` is always
  set: Delta drops and renames columns only under column mapping.
* ``accessPolicy.grants[]`` and ``policy.authz.readers`` / ``writers`` become
  one ``databricks_grant`` per (table, principal), plus ``USE_SCHEMA`` on the
  schema. ``databricks_grant`` is authoritative for its principal on its
  securable, so a principal named twice is merged into one resource rather
  than two that undo each other. It also means the product takes over each
  named principal's privileges on its schema: on a schema that existed before
  (adopted on the first apply) and that other teams share, such a principal is
  left with ``USE_SCHEMA`` alone. A product owns its schema here; a schema
  shared between products needs the packaging modes, refused for now. Readers
  also need ``USE CATALOG`` on the catalog, which the product does not own and
  never grants: the apply names who needs it (:meth:`plan_notices`), and
  ``fluid verify`` checks it.
* A dbt build that runs on Databricks (``execution.runtime.platform:
  databricks``) becomes a serverless ``databricks_job`` with a ``dbt_task``, and
  a local project its ``databricks_workspace_file`` uploads
  (``databricks_jobs.py``).
* A table a dbt build writes, locally or as a job, is rebuilt by dbt's ``table``
  materialization with CREATE OR REPLACE, so the module creates it and then
  leaves its definition to dbt: ``lifecycle.ignore_changes`` on what that
  statement rewrites, no column mapping, and the drift it hides reported on
  every apply (:meth:`suppressed_drift`); ``fluid verify`` is the gate. Every
  other table is the module's own, column for column.
* An existing table is adopted before the first apply. One without column
  mapping is refused, not upgraded: setting ``delta.columnMapping.mode`` upgrades
  the table's Delta protocol, which older readers cannot read, and that is the
  owner's call to make in Databricks, not a side effect of an apply.

Two inputs come from the environment, never from the contract:
``DATABRICKS_HOST``, which the provider reads itself (a contract that named the
host would be handed the credential, the #758 class), and
``DATABRICKS_WAREHOUSE_ID``, the SQL warehouse every table statement runs on.
The warehouse id is the one value this plugin reads from the environment. It is
an identifier, not a secret; without it the provider starts a cluster of its own
to run the DDL.

Everything the module would apply with exit 0 but not enforce, or not create,
is refused instead, in the one pass :func:`_compile` makes for both ``fluid
validate`` (through :func:`validate_databricks_binding`) and the emitter.

The provider's own diff rules, read from ``catalog/resource_sql_table.go`` at
v1.140.0, shape the apply-time checks below: with the column count unchanged it
renames columns by position; with the count changed it drops the columns whose
names are gone; a type change fails ``tofu plan`` with no column named; and a
create is ``CREATE OR REPLACE`` (databricks/terraform-provider-databricks#6140),
so an existing table must be imported before the first apply.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...providers._sql_safety import validate_ident, validate_sql_type_param_payload
from ...util.contract import is_databricks_job_build
from ..access import normalize_access_grants
from ..base import UnsupportedBindingError
from ..importer import ImportBlock
from ..naming import safe_ident, tofu_ref, unique_ident
from ..principals import (
    _ENV_TEMPLATE_RE,
    DATABRICKS,
    grant_key,
    principal_map,
    resolve_principal,
)
from ..provider_match import canonical_cloud, is_cloud
from ..versions import required_providers
from .databricks_auth import tofu_env_overlay
from .databricks_jobs import CompiledJobs, DbtJob, compile_dbt_jobs, dbt_written_exposes

#: The SQL warehouse every ``databricks_sql_table`` statement runs on.
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"

#: Seconds the provider keeps retrying one failing HTTP request. Its default is
#: 300 per request, and a plan reads every resource in turn, so an unreachable
#: host took about 25 minutes to fail (measured on 1.140.0). The provider block
#: is the only place that bounds it: there is no environment variable
#: (databricks/terraform-provider-databricks#6141).
RETRY_TIMEOUT_SECONDS = 60

#: The one binding format this plugin emits (a managed Delta table).
_DELTA_FORMATS = ("delta_table",)
_ICEBERG_FORMATS = ("iceberg", "iceberg_table")

#: The ``binding.location`` keys the emitter reads, or refuses with a message of
#: their own. Any other key the schema allows (``path``, ``region``, ...) would
#: be dropped, so it is refused.
_LOCATION_KEYS = frozenset(
    {"database", "schema", "table", "partitionBy", "host", "catalog", "warehouse"}
)

#: FLUID permission → Unity Catalog table privilege. ``create`` has no table
#: privilege and is skipped, as the BigQuery table map skips it.
_TABLE_PRIVILEGES = {
    "read": "SELECT",
    "select": "SELECT",
    "query": "SELECT",
    "write": "MODIFY",
    "insert": "MODIFY",
    "update": "MODIFY",
    "delete": "MODIFY",
    "admin": "ALL_PRIVILEGES",
    "manage": "ALL_PRIVILEGES",
}

#: A rendered column type: a name, an optional element type, optional
#: precision/scale of a few digits. Anything else would be handed to the warehouse
#: as SQL (and ``int()`` refuses a digit string past Python's limit).
_COLUMN_TYPE_RE = re.compile(r"^[a-z_]+(<[a-z_]+>)?(\([0-9]{1,4}(,[0-9]{1,4})?\))?$")

#: A rendered DECIMAL with no scale (``decimal``, ``decimal(12)``): scale 0.
_SCALE_ZERO_RE = re.compile(r"^decimal(\([0-9]+\))?$")

#: A rendered DECIMAL with arguments, in any spelling (``decimal(12)``, ``decimal(038,2)``).
_DECIMAL_ARGS_RE = re.compile(r"^decimal\(([0-9]{1,4})(?:,([0-9]{1,4}))?\)$")

#: The Delta table property the module sets on a table it owns: Delta drops and
#: renames columns only under column mapping.
COLUMN_MAPPING = "delta.columnMapping.mode"

#: What a dbt ``table`` model's CREATE OR REPLACE rewrites, so the module leaves
#: it alone on a table a dbt build writes (``dbt_written_exposes``).
_DBT_OWNED = ("column", "partitions", "cluster_keys", "properties", "comment")

#: What ``tofu plan`` prints for each plan-time error :meth:`explain_plan_failure`
#: turns into a fix-it (provider 1.140.0 messages, verbatim).
_TYPE_CHANGE = "changing the 'type' of an existing column is not supported"
_IDENTITY_CHANGE = "changing the 'identity' type of an existing column is not supported"
_MIXED_CHANGE = "detected changes in both number of columns and existing column field values"
_MISSING_CATALOG_RE = re.compile(r"Catalog '([^']+)' does not exist")
_NO_CREDENTIALS = "cannot configure default credentials"
#: Go's network errors when the workspace does not answer.
_UNREACHABLE_RE = re.compile(r"connection refused|no such host|i/o timeout")

#: How the provider normalises a column type before comparing two of them
#: (``columnTypeAliases`` in ``resource_sql_table.go``), lower case first.
_PROVIDER_TYPE_ALIASES = {
    "integer": "int",
    "long": "bigint",
    "real": "float",
    "short": "smallint",
    "byte": "tinyint",
    "decimal": "decimal(10,0)",
    "dec": "decimal(10,0)",
    "numeric": "decimal(10,0)",
}


@dataclass(frozen=True)
class _Table:
    """One Databricks-bound expose, resolved."""

    index: int
    expose_id: str
    catalog: str
    schema: str
    name: str
    columns: Tuple[Dict[str, Any], ...]
    partitions: Tuple[str, ...]
    binding: Mapping[str, Any]
    exposure: Mapping[str, Any]

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"

    @property
    def where(self) -> str:
        return f"exposes[{self.index}] ({self.expose_id})"


@dataclass
class _Compiled:
    """The module, and every refusal, for one contract."""

    resources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    imports: List[ImportBlock] = field(default_factory=list)
    refusals: List[UnsupportedBindingError] = field(default_factory=list)
    tables: List[_Table] = field(default_factory=list)
    #: OpenTofu address → the contract object that claimed it (see :func:`_claim`).
    owners: Dict[str, str] = field(default_factory=dict)
    #: ``(securable type, full name, principal, privileges)`` per grant emitted.
    grants: List[Tuple[str, str, str, List[str]]] = field(default_factory=list)
    #: The dbt jobs, and the full names of the tables a dbt build writes.
    jobs: List[DbtJob] = field(default_factory=list)
    dbt_written: List[str] = field(default_factory=list)
    #: What applies but is worth saying (``fluid validate`` warnings).
    warnings: List[str] = field(default_factory=list)


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-{kind}", message, tuple(remediation))


def _ident(
    raw: Any, where: str, refusals: List[UnsupportedBindingError], *, validating: bool
) -> Optional[str]:
    """A Unity Catalog identifier, lower-cased; ``None`` (and a refusal) if unsafe.

    Unity Catalog stores names lower-case, and the provider compares table names
    case-insensitively but column names exactly, so lower-casing here keeps a
    second plan empty. Every name ends up inside backticks in SQL the provider
    writes, so it must be a plain identifier (``providers/_sql_safety.py``).

    ``fluid validate`` runs before ``{{ env.* }}`` templates are resolved, so it
    checks each as one plain segment. On the emit path they are resolved, and
    one still present names a variable that is not set.
    """
    text = str(raw or "").strip()
    if not validating and _ENV_TEMPLATE_RE.search(text):
        refusals.append(
            _refusal(
                "identifier",
                f"{where} is {text!r}, an environment template whose variable is not set.",
                "Set the environment variable it names, then re-run.",
            )
        )
        return None
    text = text.lower()
    try:
        validate_ident(_ENV_TEMPLATE_RE.sub("env", text))
    except ValueError:
        refusals.append(
            _refusal(
                "identifier",
                f"{where} is {raw!r}, which is not a plain identifier (letters, digits and "
                "underscores, not starting with a digit).",
                "Rename it. Unity Catalog lower-cases names, so case is not significant.",
            )
        )
        return None
    return text


def _column_type(raw: Any, where: str, refusals: List[UnsupportedBindingError]) -> Optional[str]:
    """FLUID column type → Databricks SQL type, through the dbt engine's table.

    Imported here, not at module scope: ``fluid_build.engines`` loads every
    engine package, and the IaC package is imported by every ``fluid apply``.
    """
    from ...engines.dbt import _types as dbt_types

    declared = str(raw or "string")
    resolved = dbt_types.sql_type(declared, "databricks")
    if not dbt_types.known_type(declared, "databricks"):
        problem = f"which the Databricks emitter does not map: it would become {resolved!r}"
    else:
        problem = _type_problem(resolved, dbt_types.split_parameterized(resolved))
    if problem is None:
        return _canonical_type(resolved)
    refusals.append(
        _refusal(
            "column-type",
            f"{where} has type {declared!r}, {problem}.",
            "Declare a FLUID type such as string, integer, bigint, decimal(12,2), double, "
            "boolean, date, timestamp, array or object.",
        )
    )
    return None


def _type_problem(resolved: str, parts: Tuple[str, Optional[str]]) -> Optional[str]:
    """Why ``resolved`` is no Databricks column type, or ``None`` when it is one."""
    base, args = parts
    try:
        if args is not None:
            validate_sql_type_param_payload(args)
        if not _COLUMN_TYPE_RE.match(resolved):
            raise ValueError(resolved)
    except ValueError:
        return f"which does not render to a Databricks column type (got {resolved!r})"
    if base == "decimal" and args:
        precision, _, scale = args.partition(",")
        # Databricks rejects these at CREATE TABLE, after the schema was created.
        if not 1 <= int(precision) <= 38 or int(scale or 0) > int(precision):
            return "but a Databricks DECIMAL takes a precision of 1 to 38 and no larger scale"
    return None


def _canonical_type(resolved: str) -> str:
    """``resolved`` spelled as Databricks reports it back: ``decimal(12)`` is
    ``decimal(12,0)``. The provider compares the declared type with the live one
    as text, and any other spelling plans a type change, which it refuses, on
    every apply after the first."""
    match = _DECIMAL_ARGS_RE.match(resolved)
    if match is None:
        return resolved
    return f"decimal({int(match.group(1))},{int(match.group(2) or 0)})"


def _scale_zero_warnings(tables: Sequence[_Table]) -> List[str]:
    """Columns declared as a DECIMAL with no scale, which Databricks creates with
    scale 0 (a bare DECIMAL is DECIMAL(10,0))."""
    from ...engines.dbt import _types as dbt_types

    default = _PROVIDER_TYPE_ALIASES["decimal"].upper()
    out = []
    for table in tables:
        schema = (table.exposure.get("contract") or {}).get("schema") or []
        bare = [
            str(column.get("name"))
            for column in schema
            if isinstance(column, Mapping)
            and _SCALE_ZERO_RE.match(dbt_types.sql_type(str(column.get("type")), "databricks"))
        ]
        if bare:
            out.append(
                f"{table.where} declares {', '.join(bare)} with no scale, so Databricks keeps "
                f"no fractional digits (a bare DECIMAL is {default}). Declare decimal(p,s), "
                "for example decimal(18,2)."
            )
    return out


def _set(value: Any) -> bool:
    """A field the contract really declares (not absent, empty or false)."""
    return value not in (None, "", [], {}, False)


def _packaging_refusal(where: str) -> UnsupportedBindingError:
    return _refusal(
        "packaging-unmapped",
        f"{where} declares a packaging block, which the Databricks emitter does not map yet: "
        "it always references an existing catalog and owns the schema.",
        "Remove the packaging block; shared catalogs are referenced without one.",
    )


def _refuse_governance(
    where: str,
    binding: Mapping[str, Any],
    exposure: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
) -> None:
    """Governance the module would apply with exit 0 but never enforce."""
    policy = exposure.get("policy") or {}
    authz = (policy.get("authz") or {}) if isinstance(policy, Mapping) else {}
    privacy = (policy.get("privacy") or {}) if isinstance(policy, Mapping) else {}
    unmapped = [
        name
        for name, value in (
            ("policy.privacy.masking", privacy.get("masking")),
            ("policy.privacy.rowLevelPolicy", privacy.get("rowLevelPolicy")),
            ("policy.authz.rowFilters", authz.get("rowFilters")),
            ("policy.authz.columnRestrictions", authz.get("columnRestrictions")),
            ("binding.governance", binding.get("governance")),
        )
        if _set(value)
    ]
    if unmapped:
        refusals.append(
            _refusal(
                "governance-unmapped",
                f"{where} declares {', '.join(unmapped)}, which the Databricks emitter does not "
                "map yet. Applied anyway, the table would be readable without the mask, filter "
                "or restriction the contract promises.",
                "Remove them from this expose until Unity Catalog column masks, row filters "
                "and ABAC policies are mapped, and enforce access with grants.",
            )
        )
    if _set(binding.get("encryption")):
        refusals.append(
            _refusal(
                "encryption-unmapped",
                f"{where} declares binding.encryption. Unity Catalog managed tables use the "
                "workspace's storage keys; a per-table key cannot be set from here.",
                "Remove binding.encryption; configure customer-managed keys on the workspace.",
            )
        )
    lifecycle = exposure.get("lifecycle") or {}
    if isinstance(lifecycle, Mapping) and lifecycle.get("expire"):
        refusals.append(
            _refusal(
                "retention-unmapped",
                f"{where} sets lifecycle.expire, and nothing on Databricks would expire the "
                "data.",
                "Remove lifecycle.expire from the databricks binding, or schedule the delete "
                "in a build.",
            )
        )


def _refuse_shape(
    where: str,
    exposure: Mapping[str, Any],
    binding: Mapping[str, Any],
    loc: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
) -> None:
    """A binding this emitter cannot create as declared: format, kind, location fields."""
    fmt = str(binding.get("format") or "").strip().lower()
    if (
        fmt in _ICEBERG_FORMATS
        or _set(binding.get("icebergConfig"))
        or _set(exposure.get("iceberg"))
    ):
        refusals.append(
            _refusal(
                "iceberg-unsupported",
                f"{where} asks for an Iceberg table on Databricks. The emitter creates managed "
                "Delta tables only; Unity Catalog managed Iceberg is not mapped yet.",
                "Use binding.format: delta_table.",
            )
        )
    elif fmt not in _DELTA_FORMATS:
        refusals.append(
            _refusal(
                "format-unsupported",
                f"{where} has binding.format {binding.get('format')!r}; on Databricks the "
                "emitter creates managed Delta tables only.",
                "Use binding.format: delta_table.",
            )
        )
    kind = str(exposure.get("kind") or "table").strip().lower()
    if kind != "table":
        refusals.append(
            _refusal(
                "kind-unsupported",
                f"{where} is kind {kind!r}; the Databricks emitter creates tables only.",
                "Use kind: table, or bind the expose to another platform.",
            )
        )
    if loc.get("host"):
        refusals.append(
            _refusal(
                "host-in-contract",
                f"{where} sets binding.location.host. The workspace comes from DATABRICKS_HOST "
                "only: a host named in a contract would be sent the workspace credential.",
                "Remove location.host and set DATABRICKS_HOST in the environment that applies.",
            )
        )
    for key, meaning in (("catalog", "an Iceberg catalog"), ("warehouse", "an Iceberg warehouse")):
        if loc.get(key):
            refusals.append(
                _refusal(
                    "location-field",
                    f"{where} sets binding.location.{key}, which selects {meaning}. On "
                    "Databricks the Unity Catalog catalog is location.database and the SQL "
                    f"warehouse is {WAREHOUSE_ENV}.",
                    f"Remove location.{key}; put the catalog in location.database.",
                )
            )
    for key in sorted(k for k in loc if k not in _LOCATION_KEYS and _set(loc[k])):
        refusals.append(
            _refusal(
                "location-field",
                f"{where} sets binding.location.{key}, which the Databricks emitter does not "
                "map: it creates a managed Delta table in location.database and "
                "location.schema.",
                f"Remove location.{key}.",
            )
        )
    if _set(binding.get("packaging")):
        refusals.append(_packaging_refusal(f"{where} binding.packaging"))


def _location_names(
    where: str,
    loc: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
    *,
    validating: bool,
) -> Dict[str, Optional[str]]:
    """``{database, schema, table}``: the catalog, schema and table, lower-cased."""
    names: Dict[str, Optional[str]] = {}
    for key in ("database", "schema", "table"):
        if not loc.get(key):
            refusals.append(
                _refusal(
                    "location-incomplete",
                    f"{where} has no binding.location.{key}. On Databricks, database is the "
                    "Unity Catalog catalog, then schema and table.",
                    f"Set binding.location.{key}.",
                )
            )
            continue
        names[key] = _ident(
            loc.get(key), f"{where} binding.location.{key}", refusals, validating=validating
        )
    return names


def _columns(
    where: str,
    exposure: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
    *,
    validating: bool,
) -> List[Dict[str, Any]]:
    """The table's columns from ``contract.schema``, in order, named and typed."""
    schema_cols = (exposure.get("contract") or {}).get("schema") or []
    if not schema_cols:
        refusals.append(
            _refusal(
                "no-columns",
                f"{where} declares no contract.schema columns, and a Delta table needs at "
                "least one.",
                "Declare the table's columns under exposes[].contract.schema.",
            )
        )
    columns: List[Dict[str, Any]] = []
    for position, column in enumerate(schema_cols):
        if not isinstance(column, Mapping):
            continue
        col_where = f"{where} contract.schema[{position}]"
        name = _ident(column.get("name"), f"{col_where}.name", refusals, validating=validating)
        col_type = _column_type(column.get("type"), col_where, refusals)
        if name is None or col_type is None:
            continue
        if any(name == existing["name"] for existing in columns):
            refusals.append(
                _refusal(
                    "column-duplicate",
                    f"{col_where} declares column {name!r} twice (Unity Catalog names are "
                    "case-insensitive).",
                    "Rename or remove one of them.",
                )
            )
            continue
        columns.append(
            {"name": name, "type": col_type, "nullable": not bool(column.get("required"))}
        )
    return columns


def _partitions(
    where: str,
    loc: Mapping[str, Any],
    columns: Sequence[Mapping[str, Any]],
    refusals: List[UnsupportedBindingError],
    *,
    validating: bool,
) -> List[str]:
    """``location.partitionBy``, each a declared column (Delta partitions by columns)."""
    declared = {column["name"] for column in columns}
    partitions: List[str] = []
    for raw in loc.get("partitionBy") or ():
        name = _ident(raw, f"{where} binding.location.partitionBy", refusals, validating=validating)
        if name is not None and name not in declared:
            refusals.append(
                _refusal(
                    "partition-column",
                    f"{where} partitions by {name!r}, which contract.schema does not declare.",
                    "Partition by a declared column, or declare it.",
                )
            )
        elif name in partitions:
            refusals.append(
                _refusal(
                    "partition-duplicate",
                    f"{where} partitions by {name!r} twice; CREATE TABLE rejects that after "
                    "the schema was created.",
                    "List each partition column once.",
                )
            )
        elif name is not None:
            partitions.append(name)
    return partitions


def _resolve_table(
    index: int,
    exposure: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
    *,
    validating: bool,
) -> Optional[_Table]:
    """One Databricks expose → :class:`_Table`, or ``None`` with its refusals."""
    binding = exposure.get("binding") or {}
    expose_id = str(exposure.get("exposeId") or index)
    where = f"exposes[{index}] ({expose_id})"
    before = len(refusals)
    loc = binding.get("location") if isinstance(binding.get("location"), Mapping) else {}

    _refuse_shape(where, exposure, binding, loc, refusals)
    names = _location_names(where, loc, refusals, validating=validating)
    columns = _columns(where, exposure, refusals, validating=validating)
    partitions = _partitions(where, loc, columns, refusals, validating=validating)
    _refuse_governance(where, binding, exposure, refusals)
    if len(refusals) > before:
        return None
    return _Table(
        index=index,
        expose_id=expose_id,
        catalog=names["database"],
        schema=names["schema"],
        name=names["table"],
        columns=tuple(columns),
        partitions=tuple(partitions),
        binding=binding,
        exposure=exposure,
    )


@dataclass(frozen=True)
class _GrantTarget:
    """A Databricks expose grants are resolved for; ``full_name`` is ``None``
    when the table itself was refused (its principals are still checked, so
    ``fluid validate`` reports every refusal in one pass)."""

    where: str
    binding: Mapping[str, Any]
    exposure: Mapping[str, Any]
    full_name: Optional[str]


def _principal_identities(
    raw: Any,
    target: _GrantTarget,
    refusals: List[UnsupportedBindingError],
    *,
    fallback: Any = None,
) -> Tuple[str, ...]:
    try:
        return resolve_principal(
            raw,
            principal_map(target.binding),
            platform=DATABRICKS,
            where=f"{target.where} grant",
            fallback=fallback,
        )
    except UnsupportedBindingError as exc:
        refusals.append(exc)
        return ()


def _refuse_grant_shapes(
    contract: Mapping[str, Any], grants: Sequence[Any], refusals: List[UnsupportedBindingError]
) -> None:
    """Grants a Unity Catalog grant cannot express: scoped by resources, or conditional."""
    for grant in grants:
        if grant.resources:
            refusals.append(
                _refusal(
                    "grant-scope-unmapped",
                    f"accessPolicy grant to {grant_key(grant)!r} is scoped by resources, which "
                    "the Databricks emitter does not evaluate: the grant would reach no "
                    "Databricks table.",
                    "Drop resources (the grant then covers every table of the product), or "
                    "use the expose's policy.authz.readers / writers.",
                )
            )
    for entry in (contract.get("accessPolicy") or {}).get("grants") or ():
        if isinstance(entry, Mapping) and _set(entry.get("conditions")):
            refusals.append(
                _refusal(
                    "grant-conditions-unmapped",
                    f"accessPolicy grant to {entry.get('principal')!r} has conditions, which a "
                    "Unity Catalog grant cannot express: the grant would be unconditional.",
                    "Remove conditions from grants that apply to a databricks binding.",
                )
            )


def _target_privileges(
    target: _GrantTarget, grants: Sequence[Any], refusals: List[UnsupportedBindingError]
) -> Dict[str, set]:
    """``{principal: privileges}`` on one expose's table, from accessPolicy and its authz."""
    named: List[Tuple[Tuple[str, ...], set]] = []
    for grant in grants:
        if grant.resources:
            continue
        identities = _principal_identities(
            grant_key(grant), target, refusals, fallback=grant.principal
        )
        privileges = {_TABLE_PRIVILEGES[p] for p in grant.permissions if p in _TABLE_PRIVILEGES}
        named.append((identities, privileges))
    policy = target.exposure.get("policy") or {}
    authz = (policy.get("authz") or {}) if isinstance(policy, Mapping) else {}
    for key, privilege in (("readers", "SELECT"), ("writers", "MODIFY")):
        for raw in authz.get(key) or ():
            named.append((_principal_identities(raw, target, refusals), {privilege}))
    out: Dict[str, set] = {}
    for identities, privileges in named:
        for principal in identities:
            if privileges:
                out.setdefault(principal, set()).update(privileges)
    return out


def _table_privileges(
    contract: Mapping[str, Any],
    targets: Sequence[_GrantTarget],
    refusals: List[UnsupportedBindingError],
) -> Dict[str, Dict[str, set]]:
    """``{table full name: {principal: privileges}}`` from every grant surface."""
    grants = normalize_access_grants(contract)
    _refuse_grant_shapes(contract, grants, refusals)
    out: Dict[str, Dict[str, set]] = {}
    for target in targets:
        privileges = _target_privileges(target, grants, refusals)
        if target.full_name is not None:
            for principal, granted in privileges.items():
                out.setdefault(target.full_name, {}).setdefault(principal, set()).update(granted)
    return out


def _claim(compiled: _Compiled, rtype: str, key: str, owner: str, body: Dict[str, Any]) -> None:
    """Add one resource, refusing two different objects that share an address.

    Resource names join identifiers with ``_``, so ``a_b`` + ``c`` and ``a`` +
    ``b_c`` meet at one OpenTofu address, and the second would silently replace
    the first.
    """
    address = f"{rtype}.{key}"
    if compiled.owners.get(address, owner) != owner:
        compiled.refusals.append(
            _refusal(
                "name-collision",
                f"{compiled.owners[address]} and {owner} both resolve to the OpenTofu address "
                f"{address}, so one would silently replace the other.",
                "Rename one of them so the joined names differ.",
            )
        )
        return
    compiled.owners[address] = owner
    compiled.resources.setdefault(rtype, {})[key] = body


def _claim_grant(
    compiled: _Compiled,
    key: str,
    securable: Tuple[str, str, str],
    principal: str,
    privileges: List[str],
) -> None:
    """One ``databricks_grant``, the import that adopts it, and its planner record.

    ``securable`` is ``(type, full name, reference)``. The principal's existing
    privileges on it are imported first (``<type>/<full name>/<principal>``, the
    provider's import id), so the plan shows what the authoritative grant takes
    away as an update rather than a plain create. The import is best-effort: it
    fails for a securable not created yet, and the grant's create sets the same
    privileges anyway.
    """
    kind, full_name, ref = securable
    address = f"databricks_grant.{key}"
    owner = f"{principal!r} on {kind} {full_name}"
    first = address not in compiled.owners
    _claim(
        compiled,
        "databricks_grant",
        key,
        owner,
        {kind: ref, "principal": principal, "privileges": privileges},
    )
    if first and compiled.owners.get(address) == owner:
        compiled.imports.append(
            ImportBlock(address, f"{kind}/{full_name}/{principal}", best_effort=True)
        )
        compiled.grants.append((kind, full_name, principal, privileges))


def _refuse_build_hosts(
    contract: Mapping[str, Any], refusals: List[UnsupportedBindingError]
) -> None:
    """A Databricks build that names its own host: the dbt runner would send the
    workspace token there (``build_runners/dbt/profiles.py`` reads
    ``runtime.resources.host`` before ``DATABRICKS_HOST``), the #758 class."""
    for index, build in enumerate(contract.get("builds") or []):
        execution = build.get("execution") if isinstance(build, Mapping) else None
        runtime = execution.get("runtime") if isinstance(execution, Mapping) else None
        if not isinstance(runtime, Mapping) or canonical_cloud(runtime.get("platform")) != (
            "databricks"
        ):
            continue
        resources = runtime.get("resources")
        if isinstance(resources, Mapping) and resources.get("host"):
            refusals.append(
                _refusal(
                    "host-in-contract",
                    f"builds[{index}].execution.runtime.resources.host names a Databricks "
                    "host, and the dbt runner would send DATABRICKS_TOKEN to it. The "
                    "workspace comes from DATABRICKS_HOST only.",
                    "Remove runtime.resources.host and set DATABRICKS_HOST in the "
                    "environment that runs the build.",
                )
            )


def _compile(
    contract: Mapping[str, Any], env: Mapping[str, str], *, validating: bool = False
) -> _Compiled:
    """The whole module for ``contract``, and every refusal, in one pass."""
    compiled = _Compiled()
    refusals = compiled.refusals
    _refuse_build_hosts(contract, refusals)
    exposures = [
        (index, exposure)
        for index, exposure in enumerate(contract.get("exposes") or [])
        if isinstance(exposure, Mapping) and is_cloud(exposure.get("binding") or {}, "databricks")
    ]
    job_builds = any(
        isinstance(build, Mapping) and is_databricks_job_build(build)
        for build in contract.get("builds") or []
    )
    if not exposures and not job_builds:
        return compiled
    if _set(contract.get("packaging")):
        refusals.append(_packaging_refusal("the contract"))

    targets = _resolve_tables(compiled, exposures, validating=validating)
    warehouse = _warehouse(env, refusals)
    _refuse_duplicate_tables(compiled.tables, refusals)
    cid = safe_ident(contract.get("id") or contract.get("name") or "product")
    privileges = _table_privileges(contract, targets, refusals)
    dbt_written = dbt_written_exposes(contract, compiled.tables)
    for table in compiled.tables:
        _emit_table(compiled, table, cid, warehouse, dbt_owned=table.expose_id in dbt_written)
        _emit_grants(compiled, table, cid, privileges.get(table.full_name, {}))
    _merge_jobs(
        compiled, compile_dbt_jobs(contract, compiled.tables, warehouse, validating=validating)
    )

    # A grant applies to every Databricks expose, so its refusal (an unmapped
    # principal, say) would otherwise be reported once per table.
    unique: Dict[str, UnsupportedBindingError] = {}
    for refusal in refusals:
        unique.setdefault(f"{refusal.kind}:{refusal}", refusal)
    compiled.refusals = list(unique.values())
    return compiled


def _resolve_tables(
    compiled: _Compiled, exposures: Sequence[Tuple[int, Mapping[str, Any]]], *, validating: bool
) -> List[_GrantTarget]:
    """Resolve each Databricks expose into ``compiled.tables``; a grant target for every one."""
    targets: List[_GrantTarget] = []
    for index, exposure in exposures:
        table = _resolve_table(index, exposure, compiled.refusals, validating=validating)
        if table is not None:
            compiled.tables.append(table)
        targets.append(
            _GrantTarget(
                where=f"exposes[{index}] ({exposure.get('exposeId') or index})",
                binding=exposure.get("binding") or {},
                exposure=exposure,
                full_name=table.full_name if table is not None else None,
            )
        )
    return targets


def _warehouse(env: Mapping[str, str], refusals: List[UnsupportedBindingError]) -> str:
    """``DATABRICKS_WAREHOUSE_ID``, or ``""`` and a refusal."""
    warehouse = str(env.get(WAREHOUSE_ENV) or "").strip()
    if not warehouse:
        refusals.append(
            _refusal(
                "warehouse-unset",
                f"{WAREHOUSE_ENV} is not set. Every Unity Catalog table statement runs on a SQL "
                "warehouse, and the warehouse comes from the environment, never the contract.",
                f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace "
                "DATABRICKS_HOST names (SQL Warehouses > the warehouse > Connection details).",
            )
        )
    return warehouse


def _refuse_duplicate_tables(
    tables: Sequence[_Table], refusals: List[UnsupportedBindingError]
) -> None:
    bound: Dict[str, str] = {}
    for table in tables:
        if table.full_name in bound:
            refusals.append(
                _refusal(
                    "table-duplicate",
                    f"{bound[table.full_name]} and {table.where} both bind table "
                    f"{table.full_name}; one expose's columns would replace the other's.",
                    "Bind each expose to its own table.",
                )
            )
        bound.setdefault(table.full_name, table.where)


def _keys(cid: str, table: _Table) -> Tuple[str, str, str]:
    """``(catalog, schema, table)`` resource keys of one table."""
    return (
        safe_ident(f"{cid}_{table.catalog}"),
        safe_ident(f"{cid}_{table.catalog}_{table.schema}"),
        safe_ident(f"{cid}_{table.catalog}_{table.schema}_{table.name}"),
    )


def _emit_table(
    compiled: _Compiled, table: _Table, cid: str, warehouse: str, *, dbt_owned: bool
) -> None:
    """The catalog reference, the owned schema and the table, with their imports."""
    catalog_key, schema_key, table_key = _keys(cid, table)
    catalog_name = tofu_ref(f"data.databricks_catalog.{catalog_key}.name")
    compiled.data.setdefault("databricks_catalog", {})[catalog_key] = {"name": table.catalog}
    if f"databricks_schema.{schema_key}" not in compiled.owners:
        compiled.imports.append(
            ImportBlock(f"databricks_schema.{schema_key}", f"{table.catalog}.{table.schema}")
        )
    _claim(
        compiled,
        "databricks_schema",
        schema_key,
        f"schema {table.catalog}.{table.schema}",
        {"catalog_name": catalog_name, "name": table.schema},
    )
    body: Dict[str, Any] = {
        "catalog_name": catalog_name,
        "schema_name": tofu_ref(f"databricks_schema.{schema_key}.name"),
        "name": table.name,
        "table_type": "MANAGED",
        "data_source_format": "DELTA",
        "warehouse_id": warehouse,
        "column": [dict(column) for column in table.columns],
    }
    if dbt_owned:
        # dbt's table materialization rebuilds it with CREATE OR REPLACE: the
        # module creates it (its grants need it to exist) and leaves the rest to
        # dbt, as the Snowflake emitter does (iac/providers/snowflake.py).
        body["lifecycle"] = {"ignore_changes": list(_DBT_OWNED)}
        compiled.dbt_written.append(table.full_name)
    else:
        body["properties"] = {COLUMN_MAPPING: "name"}
    if table.partitions:
        body["partitions"] = list(table.partitions)
    _claim(compiled, "databricks_sql_table", table_key, f"table {table.full_name}", body)
    # Imported before the first plan: the provider creates a managed Delta
    # table with CREATE OR REPLACE, so a table that exists but is not in
    # state would otherwise be replaced, and the plan would only show +1.
    compiled.imports.append(ImportBlock(f"databricks_sql_table.{table_key}", table.full_name))


def _emit_grants(
    compiled: _Compiled, table: _Table, cid: str, privileges: Mapping[str, set]
) -> None:
    """Each principal's privileges on the table, and USE_SCHEMA on its schema."""
    _catalog_key, schema_key, table_key = _keys(cid, table)
    on_table = ("table", table.full_name, tofu_ref(f"databricks_sql_table.{table_key}.id"))
    on_schema = (
        "schema",
        f"{table.catalog}.{table.schema}",
        tofu_ref(f"databricks_schema.{schema_key}.id"),
    )
    for principal, granted in sorted(privileges.items()):
        _claim_grant(
            compiled, f"{table_key}_{unique_ident(principal)}", on_table, principal, sorted(granted)
        )
        _claim_grant(
            compiled,
            f"{schema_key}_usage_{unique_ident(principal)}",
            on_schema,
            principal,
            ["USE_SCHEMA"],
        )


def _merge_jobs(compiled: _Compiled, jobs: CompiledJobs) -> None:
    """The dbt jobs' resources, data sources, refusals and warnings, into ``compiled``."""
    compiled.refusals.extend(jobs.refusals)
    compiled.warnings.extend(jobs.warnings)
    compiled.jobs = jobs.jobs
    for rtype, key, owner, body in jobs.resources:
        _claim(compiled, rtype, key, owner, body)
    for dtype, entries in jobs.data.items():
        compiled.data.setdefault(dtype, {}).update(entries)


def _columns_of(values: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in values.get("column") or [] if isinstance(c, Mapping)]


def column_changes(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """``(dropped, renamed)`` for one ``databricks_sql_table`` update.

    Mirrors the provider's diff (``resource_sql_table.go``): with the column
    count unchanged it renames by position, keeping the old values under the new
    name; with the count changed it drops every column whose name is gone,
    which deletes its data.
    """
    old = [str(c.get("name")) for c in before]
    new = [str(c.get("name")) for c in after]
    if len(old) == len(new):
        return [], [(o, n) for o, n in zip(old, new, strict=True) if o != n]
    return [name for name in old if name not in set(new)], []


def column_reorder(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> Optional[Tuple[List[str], List[str]]]:
    """``(live, declared)`` column orders the provider cannot get from one to the
    other, else ``None``.

    The provider never moves a column. With the count unchanged it renames by
    position, so renaming onto a name the table already has (a swap) fails part
    way through the apply. With the count changed it adds and drops by name, so
    columns kept in a new order stay where they are, and the next plan, at an
    equal count, renames them by position for good.
    """
    old = [str(c.get("name")) for c in before]
    new = [str(c.get("name")) for c in after]
    if len(old) == len(new):
        onto_existing = any(o != n and n in old for o, n in zip(old, new, strict=True))
        return (old, new) if onto_existing else None
    kept = set(old) & set(new)
    live, declared = [c for c in old if c in kept], [c for c in new if c in kept]
    return (live, declared) if live != declared else None


def _norm_type(value: Any) -> str:
    """A column type as the provider compares two of them (``columnTypeAliases``)."""
    text = _canonical_type(str(value or "").replace("`", "").lower())
    return _PROVIDER_TYPE_ALIASES.get(text, text)


def _changed_columns(
    live: Sequence[Mapping[str, Any]], declared: Sequence[Mapping[str, Any]]
) -> List[str]:
    """One line per column the contract changes the type or nullability of.

    Same pairing as the provider: by position when the count is unchanged, by
    name otherwise. Types compare as the provider normalises them.
    """
    from ...engines.dbt import _types as dbt_types

    if len(live) == len(declared):
        pairs = list(zip(live, declared, strict=True))
    else:
        by_name = {str(c.get("name")): c for c in declared}
        pairs = [(c, by_name[str(c.get("name"))]) for c in live if str(c.get("name")) in by_name]
    out = []
    for old, new in pairs:
        old_type = _norm_type(old.get("type"))
        new_type = _norm_type(dbt_types.sql_type(str(new.get("type") or ""), "databricks"))
        if old_type != new_type:
            out.append(f"{new['name']}: {old_type} in the live table, {new_type} in the contract")
        if bool(old.get("nullable", True)) != bool(new.get("nullable", True)):
            live_null = "nullable" if old.get("nullable", True) else "NOT NULL"
            want_null = "nullable" if new.get("nullable", True) else "NOT NULL"
            out.append(f"{new['name']}: {live_null} in the live table, {want_null} in the contract")
    return out


def _quoted(full_name: str) -> str:
    """``catalog.schema.table`` as backtick-quoted SQL, for a fix-it to copy."""
    return ".".join(f"`{part.replace('`', '``')}`" for part in full_name.split("."))


def _refuse_protocol_upgrade(
    table: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    """Refuse an update that would turn on column mapping for an existing table.

    The module sets ``delta.columnMapping.mode=name`` on every table it owns, so
    on a table adopted without it the plan's first change is that property, and
    the provider's ``ALTER TABLE ... SET TBLPROPERTIES`` upgrades the table's
    Delta protocol to one with the column-mapping feature: older Delta readers
    and some external engines then cannot read it, and unsetting the property
    does not undo it. That is the table owner's call, so the apply stops and
    says how to make it, whatever flag is passed.
    """
    wanted = (after.get("properties") or {}).get(COLUMN_MAPPING)
    live = (before.get("properties") or {}).get(COLUMN_MAPPING) or (
        before.get("effective_properties") or {}
    ).get(COLUMN_MAPPING)
    if wanted != "name" or live == "name":
        return
    raise _refusal(
        "adopt-protocol-upgrade",
        f"{table} exists without column mapping, and taking it over would set "
        f"{COLUMN_MAPPING}=name: a Delta protocol upgrade that older Delta readers and some "
        "external engines cannot read, and that unsetting the property does not undo.",
        "If every reader of the table can read column mapping, run in Databricks: ALTER TABLE "
        f"{_quoted(table)} SET TBLPROPERTIES ('{COLUMN_MAPPING}' = 'name'), then re-run "
        "fluid apply.",
        "Otherwise leave this table out of the contract, or let a dbt build write it (the "
        "module then leaves its definition to dbt and sets no column mapping).",
    )


def _dbt_column_drift(
    compiled: _Compiled, prior_resources: Iterable[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """One record per table a dbt build writes whose live columns drifted.

    ``prior_resources`` is the refreshed pre-apply state; each table's
    ``values.column`` is what Databricks holds after dbt's last run. Types
    compare as the provider normalises them.
    """
    declared = {
        table.full_name: table.columns
        for table in compiled.tables
        if table.full_name in compiled.dbt_written
    }
    records: List[Dict[str, Any]] = []
    for resource in prior_resources:
        if resource.get("type") != "databricks_sql_table":
            continue
        values = resource.get("values") or {}
        full = ".".join(
            str(values.get(key) or "").lower() for key in ("catalog_name", "schema_name", "name")
        )
        if full not in declared:
            continue
        live = {str(c.get("name")).lower(): _norm_type(c.get("type")) for c in _columns_of(values)}
        want = {c["name"]: _norm_type(c["type"]) for c in declared[full]}
        record = {
            "table": full,
            "type_mismatches": [
                {"column": name, "declared": want[name], "live": live[name]}
                for name in want
                if name in live and live[name] != want[name]
            ],
            "missing": [name for name in want if name not in live],
            "extra": [name for name in live if name not in want],
        }
        if record["type_mismatches"] or record["missing"] or record["extra"]:
            records.append(record)
    return records


def _use_catalog_notices(compiled: _Compiled) -> List[str]:
    """Who needs ``USE CATALOG`` on each catalog, and the GRANT its owner runs.

    A reader of a table needs it on the table's catalog, which the product
    references and does not own, so the module never grants it: a
    ``databricks_grant`` is authoritative for its principal, and would revoke
    that principal's other privileges on the catalog, for every product in it.
    ``fluid verify`` checks it.
    """
    readers: Dict[str, List[str]] = {}
    for kind, full_name, principal, _privileges in compiled.grants:
        if kind == "table":
            catalog = full_name.split(".")[0]
            if principal not in readers.setdefault(catalog, []):
                readers[catalog].append(principal)
    notices = []
    for catalog, principals in sorted(readers.items()):
        grants = "; ".join(
            f"GRANT USE CATALOG ON CATALOG {_quoted(catalog)} TO `{p.replace('`', '``')}`"
            for p in sorted(principals)
        )
        notices.append(
            f"{', '.join(sorted(principals))} also need USE CATALOG on catalog {catalog}, "
            f"which forge-cli never grants (the catalog is not the product's). Its owner "
            f"runs: {grants}. fluid verify checks it."
        )
    return notices


class DatabricksIacPlugin:
    """FLUID contract → ``databricks/databricks`` Unity Catalog resources."""

    name = "databricks"
    required_providers = required_providers("databricks")
    credential_env_vars = (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
    )
    #: The committed lock ``tofu init`` verifies the provider against
    #: (``iac/module.py::dependency_lock``).
    dependency_lock = "databricks.terraform.lock.hcl"
    #: An existing table is replaced by the provider's CREATE OR REPLACE unless it
    #: is imported first, so an import that fails for any reason other than "the
    #: object does not exist" stops the apply (``_adopt_existing``).
    strict_adoption = True

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        compiled = _compile(contract, os.environ)
        if compiled.refusals:
            raise compiled.refusals[0]
        return compiled.resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        return _compile(contract, os.environ).data

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """Pin the credential the provider uses (``databricks_auth.py``): the
        environment's, or the one profile ``DATABRICKS_CONFIG_PROFILE`` names for
        ``DATABRICKS_HOST``; never a DEFAULT profile it happens to find."""
        return tofu_env_overlay(env)

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        return _compile(contract, os.environ).imports

    def provider_block(self) -> Dict[str, Any]:
        return {"retry_timeout_seconds": RETRY_TIMEOUT_SECONDS}

    def plan_data_changes(self, plan: Mapping[str, Any]) -> List[str]:
        """The in-place table updates in a ``tofu show -json`` plan that lose or move data.

        A dropped column is an in-place ``~`` update, which the data-loss gate's
        destroy count never sees. So is a rename: with the column count unchanged
        the provider renames by position, and a contract cannot say whether it
        means a rename or a drop and an add, so the old values staying under the
        new name is gated too. A column list the plan cannot show yet counts,
        fail-closed. A new column order the provider cannot reach
        (:func:`column_reorder`) is refused outright: no flag makes it apply.
        """
        lost: List[str] = []
        for change in plan.get("resource_changes") or []:
            if not isinstance(change, Mapping) or change.get("type") != "databricks_sql_table":
                continue
            detail = change.get("change") or {}
            if list(detail.get("actions") or []) != ["update"]:
                continue
            before = detail.get("before") or {}
            after = detail.get("after") or {}
            table = ".".join(
                str(before.get(key) or "") for key in ("catalog_name", "schema_name", "name")
            )
            _refuse_protocol_upgrade(table, before, after)
            if (detail.get("after_unknown") or {}).get("column") is True:
                lost.append(
                    f"{table}: the planned column list is unknown, so a drop cannot be ruled out"
                )
                continue
            reorder = column_reorder(_columns_of(before), _columns_of(after))
            if reorder is not None:
                raise _refusal(
                    "column-reorder",
                    f"{table}: the contract orders its columns {', '.join(reorder[1])}, the "
                    f"live table {', '.join(reorder[0])}. Databricks cannot move a column: the "
                    "provider renames by position or adds and drops by name, so the apply would "
                    "fail part way or leave a table no later plan matches.",
                    "Keep the existing columns in their live order and add new ones after "
                    "them; to replace a column, drop it in one apply and add the new one in "
                    "the next.",
                )
            dropped, renamed = column_changes(_columns_of(before), _columns_of(after))
            if dropped:
                lost.append(f"{table}: drops column(s) {', '.join(dropped)}")
            for old, new in renamed:
                lost.append(
                    f"{table}: renames column {old} to {new} by position, so {old}'s values "
                    f"stay under {new}"
                )
        return lost

    def suppressed_drift(
        self, contract: Mapping[str, Any], prior_resources: Iterable[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Column drift on the tables a dbt build writes, which ``ignore_changes``
        hides, in the shape the engine reports (``_report_suppressed_drift``)."""
        return _dbt_column_drift(_compile(contract, os.environ), prior_resources)

    def plan_notices(self, contract: Mapping[str, Any]) -> List[str]:
        """What the apply cannot do itself and someone must: ``USE CATALOG``."""
        return _use_catalog_notices(_compile(contract, os.environ))

    def explain_plan_failure(
        self,
        output: str,
        contract: Mapping[str, Any],
        state: Sequence[Mapping[str, Any]],
    ) -> Optional[UnsupportedBindingError]:
        """A fix-it for the provider's plan-time errors, or ``None`` for any other.

        ``state`` is the current state's resources (``runner.tofu_state_resources``),
        read to name the columns the provider's own message leaves out.
        """
        if _NO_CREDENTIALS in (output or ""):
            return _refusal(
                "credentials-missing",
                "the Databricks provider found no credentials: no workspace and token in "
                "the environment, and no profile in ~/.databrickscfg.",
                "Set DATABRICKS_HOST and DATABRICKS_TOKEN (a personal access token), or "
                "DATABRICKS_HOST with DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET (a "
                "service principal's OAuth secret), then re-run 'fluid apply'.",
            )
        if _UNREACHABLE_RE.search(output or ""):
            return _refusal(
                "workspace-unreachable",
                "the workspace DATABRICKS_HOST names did not answer (connection refused, "
                "unknown host or a timeout).",
                "Check DATABRICKS_HOST and that this machine can reach it (VPN, proxy, "
                "firewall), then re-run 'fluid apply'.",
            )
        missing = _MISSING_CATALOG_RE.search(output or "")
        if missing:
            return _refusal(
                "catalog-missing",
                f"catalog {missing.group(1)!r} (binding.location.database) does not exist in "
                "the workspace DATABRICKS_HOST names, or this identity cannot see it. "
                "forge-cli never creates a catalog.",
                f"Create it (CREATE CATALOG {missing.group(1)}) or point location.database at "
                "an existing catalog, and grant this identity USE CATALOG and CREATE SCHEMA "
                "on it.",
            )
        if _IDENTITY_CHANGE in output:
            return _refusal(
                "column-identity",
                "the live table has an IDENTITY column at a position where the contract "
                "declares a plain column, and Databricks cannot change that in place. A FLUID "
                "contract cannot declare an identity column.",
                "Leave this table out of the contract (manage it in Databricks), or recreate "
                "it without identity columns.",
            )
        if _TYPE_CHANGE in output or _MIXED_CHANGE in output:
            details = self._column_details(contract, state)
            listed = ("; ".join(details) + ". ") if details else ""
            if _MIXED_CHANGE in output:
                return _refusal(
                    "column-change-mixed",
                    "Databricks cannot add or remove columns and change existing ones in the "
                    f"same apply. {listed}",
                    "Apply the change in two steps: first only add or remove columns, then "
                    "change the existing ones (or the other way round).",
                )
            return _refusal(
                "column-type-change",
                "Databricks cannot change the type of an existing column in place. " + listed,
                "Keep the live type: set the contract's type back to it.",
                "Or migrate in two applies: add a new column with the new type and fill it "
                "in your build, then remove the old one (removing a column is gated: "
                "--allow-data-loss).",
                "Or recreate the table, losing its data: DROP TABLE it in Databricks, then "
                "re-run fluid apply.",
            )
        return None

    @staticmethod
    def _column_details(
        contract: Mapping[str, Any], state: Sequence[Mapping[str, Any]]
    ) -> List[str]:
        live: Dict[str, List[Dict[str, Any]]] = {}
        for resource in state:
            if resource.get("type") != "databricks_sql_table":
                continue
            values = resource.get("values") or {}
            full = ".".join(
                str(values.get(key) or "").lower()
                for key in ("catalog_name", "schema_name", "name")
            )
            live[full] = _columns_of(values)
        out: List[str] = []
        for index, exposure in enumerate(contract.get("exposes") or []):
            if not isinstance(exposure, Mapping):
                continue
            binding = exposure.get("binding") or {}
            if not is_cloud(binding, "databricks"):
                continue
            loc = binding.get("location") or {}
            full = ".".join(
                str(loc.get(key) or "").lower() for key in ("database", "schema", "table")
            )
            declared = [
                {
                    "name": str(c.get("name") or "").lower(),
                    "type": c.get("type"),
                    "nullable": not bool(c.get("required")),
                }
                for c in (exposure.get("contract") or {}).get("schema") or []
                if isinstance(c, Mapping)
            ]
            if full in live:
                out.extend(f"{full}.{line}" for line in _changed_columns(live[full], declared))
        return out


def _job_actions(jobs: Sequence[DbtJob]) -> List[Dict[str, Any]]:
    """Plan actions for the dbt jobs and the project files they upload."""
    actions: List[Dict[str, Any]] = []
    for job in jobs:
        actions.append(
            {
                "op": "databricks.job.ensure",
                "resource_type": "databricks_job",
                "resource_id": job.name,
                "build": job.build_id,
                "source": job.source,
                "trigger": job.trigger,
            }
        )
        actions.extend(
            {
                "op": "databricks.workspace_file.ensure",
                "resource_type": "databricks_workspace_file",
                # Under the applying identity's home, which only the apply knows.
                "resource_id": f"~{job.root}/{rel}",
                "build": job.build_id,
            }
            for rel in job.files
        )
    return actions


def planned_objects(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The objects the module manages, as plan actions, for the native planner.

    ``providers/databricks`` plans through this, not a second reading of the
    contract, so ``fluid diff`` and ``--provider databricks`` see exactly what
    ``fluid apply`` creates. Each action names its ``resource_type`` and
    ``resource_id`` (the shape ``cli/diff.py`` keys resources by). A contract the
    emitter refuses is refused here too: a refused table leaves no action, and
    a plan without it would be a smaller plan, not an error.
    """
    compiled = _compile(contract, os.environ)
    if compiled.refusals:
        raise compiled.refusals[0]
    actions: List[Dict[str, Any]] = []
    for body in compiled.data.get("databricks_catalog", {}).values():
        actions.append(
            {
                "op": "databricks.catalog.reference",
                "resource_type": "databricks_catalog",
                "resource_id": body["name"],
            }
        )
    for table in compiled.tables:
        actions.append(
            {
                "op": "databricks.schema.ensure",
                "resource_type": "databricks_schema",
                "resource_id": f"{table.catalog}.{table.schema}",
                "schema": table.schema,
            }
        )
        actions.append(
            {
                "op": "databricks.table.ensure",
                "resource_type": "databricks_sql_table",
                "resource_id": table.full_name,
                "table": table.name,
                "columns": [dict(column) for column in table.columns],
                "partitions": list(table.partitions),
            }
        )
    for securable, full_name, principal, privileges in compiled.grants:
        actions.append(
            {
                "op": "databricks.grant",
                "resource_type": "databricks_grant",
                # The provider's own import id for a grant.
                "resource_id": f"{securable}/{full_name}/{principal}",
                "principal": principal,
                "privileges": list(privileges),
            }
        )
    actions.extend(_job_actions(compiled.jobs))
    unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for action in actions:
        unique.setdefault((action["resource_type"], action["resource_id"]), action)
    return list(unique.values())


def validate_databricks_binding(
    contract: Mapping[str, Any], env: Optional[Mapping[str, str]] = None
) -> Tuple[List[str], List[str]]:
    """``(errors, warnings)``: what ``fluid generate iac`` / ``fluid apply`` would refuse.

    The same pass the emitter runs (:func:`_compile`), so ``fluid validate``
    reports every refusal at stage 2, with the emitter's message, instead of
    the first one at apply. The warnings name what applies but loses precision.
    """
    compiled = _compile(contract, os.environ if env is None else env, validating=True)
    errors = [f"{exc} {' '.join(exc.remediation)}".strip() for exc in compiled.refusals]
    return errors, _scale_zero_warnings(compiled.tables) + compiled.warnings


@dataclass(frozen=True)
class ManagedTable:
    """A table the module manages, as ``fluid verify`` checks it."""

    expose_id: str
    full_name: str
    #: ``{name, type, nullable}`` in contract order; types as the provider compares them.
    columns: Tuple[Dict[str, Any], ...]
    #: Principals granted a privilege on it, who need USE CATALOG on its catalog.
    readers: Tuple[str, ...]
    #: Written by a dbt build: the module ignores its column drift, verify gates it.
    dbt_written: bool


def managed_objects(
    contract: Mapping[str, Any], env: Optional[Mapping[str, str]] = None
) -> Tuple[List[ManagedTable], List[DbtJob]]:
    """``(tables, jobs)`` the module manages for ``contract``, from the emitter's own
    pass, so ``fluid verify`` checks exactly what ``fluid apply`` provisions."""
    compiled = _compile(contract, os.environ if env is None else env, validating=True)
    readers: Dict[str, List[str]] = {}
    for kind, full_name, principal, _privileges in compiled.grants:
        if kind == "table" and principal not in readers.setdefault(full_name, []):
            readers[full_name].append(principal)
    tables = [
        ManagedTable(
            expose_id=table.expose_id,
            full_name=table.full_name,
            columns=tuple(
                {**column, "type": _norm_type(column["type"])} for column in table.columns
            ),
            readers=tuple(readers.get(table.full_name, ())),
            dbt_written=table.full_name in compiled.dbt_written,
        )
        for table in compiled.tables
    ]
    return tables, list(compiled.jobs)


__all__ = [
    "COLUMN_MAPPING",
    "RETRY_TIMEOUT_SECONDS",
    "WAREHOUSE_ENV",
    "DatabricksIacPlugin",
    "ManagedTable",
    "column_changes",
    "managed_objects",
    "normalize_column_type",
    "validate_databricks_binding",
]

#: How the provider compares two column types, for a live type ``fluid verify`` reads.
normalize_column_type = _norm_type
