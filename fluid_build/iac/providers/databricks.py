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
* A contract's governance labels, the same ones AWS tags and GCP labels carry
  (``iac/governance_labels.py``: ``labels``, classification, jurisdiction, regulation,
  residency), are Unity Catalog tags: one ``databricks_entity_tag_assignment`` per
  tag on each owned schema (the contract's own) and each table (the expose's too).
  A tag key may not contain ``-``, so each ``-`` becomes ``_``
  (``data-classification`` is ``data_classification``); two labels that meet at one
  key are ``databricks-tag-collision`` and more than the 50 tags a securable holds is
  ``databricks-tags-too-many``, refused rather than overwritten or dropped. An
  existing assignment is imported best-effort before the first apply, and removing a
  label destroys only its assignment, which holds no data. Whether a tag survives a
  dbt ``CREATE OR REPLACE`` is undocumented, so ``fluid verify`` is the gate.
* ``policy.authz.columnRestrictions`` and ``policy.authz.rowFilters`` are enforced by Unity
  Catalog: SQL functions bound on the table, with the semantics GCP and AWS give them
  (``databricks_access.py``). ``deny`` is a mask that returns NULL (Unity Catalog has no
  column grant, so the column stays listed), ``mask`` returns ``last_four`` / ``first_four`` /
  NULL, and a row filter (one per table) is the union of its principals' predicates for the
  members of those principals and every row for everyone else. The functions are created once
  under names that carry a hash of their definition, since Unity Catalog refuses to replace one
  a filter uses, and are bound with ``ALTER TABLE`` on the SQL warehouse through the SQL
  Statement Execution API, outside OpenTofu: the provider has no function resource and no mask
  or filter attribute (terraform-provider-databricks#4550). :meth:`DatabricksIacPlugin.pre_apply`
  binds on the tables that exist before ``tofu apply`` runs, :meth:`~DatabricksIacPlugin.post_apply`
  on the ones it created (revoking ``SELECT`` from the restricted principals of a table it
  cannot bind) and drops what the contract no longer restricts (``databricks_enforce.py``).
  A predicate that is not one boolean expression over the table's columns is
  ``databricks-row-filter-invalid``; ``access: allow``, a principal with no SQL test and a text
  mask on a non-string column are refused too. ``fluid verify`` checks the bindings.
* A dbt build that runs on Databricks (``execution.runtime.platform:
  databricks``) becomes a serverless ``databricks_job`` with a ``dbt_task``, and
  a local project its ``databricks_workspace_file`` uploads
  (``databricks_jobs.py``).
* ``lifecycle.expire`` is accepted on a table that is rebuilt in full on every run (a dbt
  ``table`` model, or forge-cli's DuckDB acquisition) when ``lifecycle.retention`` covers the
  table's Delta history, so ``VACUUM`` removes older copies; anything else is refused
  (``databricks_retention.py``). ``fluid verify`` reads the live properties.
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

* ``binding.location.region`` is the region the workspace is in, declared so a
  contract's ``sovereignty`` policy can check it (``policy/sovereignty.py``
  treats Databricks as a region-placed platform). A declaration is verified,
  not trusted: the module reads the workspace's metastore
  (``data.databricks_current_metastore``) and every schema it owns carries a
  ``lifecycle.precondition`` that the metastore's region equals the declared
  one, so ``tofu plan`` fails, before anything is written, when
  ``DATABRICKS_HOST`` names a workspace in another region. The value must be a
  plain lower-case region name as written (the policy compares it verbatim, and
  it is interpolated into the module): ``databricks-region-invalid`` otherwise;
  two exposes declaring different regions is ``databricks-region-conflict``, a
  workspace having one. The policy itself is held where the data would land, as
  the GCP emitter holds it: :meth:`DatabricksIacPlugin.emit`, the one way
  ``fluid apply`` and ``generate iac`` reach this module, raises
  ``SovereigntyViolationError`` on a placement the policy refuses (a missing
  region under ``strict`` included). A contract that declares no region and no
  sovereignty block gets the module it always did.

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

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...providers._sql_safety import validate_ident, validate_sql_type_param_payload
from ...util.contract import is_databricks_job_build
from ..access import normalize_access_grants
from ..base import UnsupportedBindingError
from ..governance_labels import governance_labels
from ..importer import ImportBlock
from ..naming import TofuExpr, safe_ident, tofu_ref, unique_ident
from ..principals import (
    _ENV_TEMPLATE_RE,
    DATABRICKS,
    grant_key,
    principal_map,
    resolve_principal,
)
from ..provider_match import canonical_cloud, is_cloud
from ..versions import required_providers
from .databricks_access import TableEnforcement, compile_enforcement
from .databricks_auth import tofu_env_overlay
from .databricks_enforce import Target, post_apply, pre_apply, preview, unprotected
from .databricks_jobs import (
    CompiledJobs,
    DbtJob,
    compile_dbt_jobs,
    dbt_written_exposes,
    run_as_allowlist,
)
from .databricks_retention import Retention, check_expire

_LOG = logging.getLogger(__name__)

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
    {"database", "schema", "table", "partitionBy", "host", "catalog", "warehouse", "region"}
)

#: A declared workspace region, written lower-case: ``eu-central-1``,
#: ``westeurope``, ``us-east1``. It is interpolated into the tf.json, where
#: ``${`` and ``%{`` are template syntax, and the sovereignty policy compares it
#: verbatim, so this is a security boundary, not a style check.
_REGION_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

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
    #: The declared workspace region, lower-cased and validated; ``""`` if none.
    region: str = ""
    #: The column masks and row filter Unity Catalog enforces on it, if the expose has any.
    enforcement: Optional[TableEnforcement] = None
    #: The ``lifecycle.expire`` it promises and this module accepted (``databricks_retention.py``).
    retention: Optional[Retention] = None

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
    #: ``(entity type, full name, tag key, tag value)`` per tag assignment emitted.
    tags: List[Tuple[str, str, str, str]] = field(default_factory=list)


#: Unity Catalog allows 50 tags on a securable ("Apply tags to Unity Catalog securable
#: objects", Databricks docs). Its keys may not contain ``. , - = / :``.
_MAX_TAGS = 50


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
                "Remove them from this expose until they are mapped (landing-time masking and "
                "ABAC policies are not), and enforce access with grants. policy.authz."
                "columnRestrictions and policy.authz.rowFilters are mapped: they become Unity "
                "Catalog column masks and a row filter.",
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
    contract: Mapping[str, Any],
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
    region = _region(where, loc, refusals)
    _refuse_governance(where, binding, exposure, refusals)
    retention, retention_refusals = check_expire(
        where, exposure, names.get("table") or "", contract, validating=validating
    )
    refusals.extend(retention_refusals)
    enforcement: Optional[TableEnforcement] = None
    if len(refusals) == before:
        try:
            enforcement = compile_enforcement(
                where=where,
                index=index,
                exposure=exposure,
                binding=binding,
                catalog=names["database"],
                schema=names["schema"],
                table=names["table"],
                columns=columns,
            )
        except UnsupportedBindingError as exc:
            refusals.append(exc)
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
        region=region,
        enforcement=enforcement,
        retention=retention,
    )


def _region(where: str, loc: Mapping[str, Any], refusals: List[UnsupportedBindingError]) -> str:
    """The declared workspace region, exactly as written; ``""`` if none, or refused.

    Never normalised: the sovereignty policy compares ``binding.location.region``
    verbatim with ``allowedRegions`` and ``deniedRegions`` (``policy/
    sovereignty.py``), so a ``US-WEST-2`` this module lower-cased passed a
    ``deniedRegions: [us-west-2]`` check and then planned against a us-west-2
    workspace. ``fullmatch``, not ``match``: ``$`` also matches before a final
    newline.
    """
    raw = loc.get("region")
    if not _set(raw):
        return ""
    if not isinstance(raw, str) or not _REGION_RE.fullmatch(raw):
        refusals.append(
            _refusal(
                "region-invalid",
                f"{where} has binding.location.region {raw!r}, which is not a region name as "
                "the sovereignty policy compares it: lower-case letters, digits and hyphens, "
                "starting with a letter, no spaces (eu-central-1, westeurope). It is also "
                "written into the module the plan runs.",
                "Set location.region, written that way, to the region of the workspace "
                "DATABRICKS_HOST names.",
            )
        )
        return ""
    return raw


def _enforce_sovereignty(contract: Mapping[str, Any], tables: Sequence[_Table]) -> None:
    """Raise when the contract's ``sovereignty`` policy refuses where the tables land.

    ``fluid validate`` and ``plan --check-sovereignty`` run the policy over the
    whole contract, but ``fluid apply`` and ``generate iac`` reach this module
    through :meth:`DatabricksIacPlugin.emit` alone, so the policy is held there
    too, where the data would land, as the GCP emitter holds it
    (``providers/gcp/util/sovereignty.py::enforce_gcp_sovereignty``). Each
    Databricks table is one placement, in the region its expose declares (none
    is check 0); the module's precondition then proves the workspace is there.
    Not in :func:`_compile`: ``fluid validate`` reports the policy itself, and
    would report each finding twice.
    """
    sovereignty = contract.get("sovereignty")
    if not isinstance(sovereignty, Mapping) or not sovereignty or not tables:
        return
    from ..._errors import SovereigntyViolationError, doc_url
    from ...policy.sovereignty import SovereigntyValidator

    placements = [(table.expose_id, table.region or None) for table in tables]
    _, violations = SovereigntyValidator().check_placements(
        sovereignty, placements, region_placed={where for where, _ in placements}
    )
    errors = [v for v in violations if v.severity == "error"]
    for v in violations:
        if v.severity != "error":
            _LOG.warning("databricks sovereignty (%s): %s: %s", v.severity, v.expose_id, v.message)
    if not errors:
        return
    allowed = [str(r) for r in (sovereignty.get("allowedRegions") or [])]
    denied = [str(r) for r in (sovereignty.get("deniedRegions") or [])]
    if allowed:
        rule = f"allows {', '.join(allowed)}"
    elif sovereignty.get("jurisdiction"):
        rule = f"requires jurisdiction {sovereignty['jurisdiction']!r}"
    elif denied:
        rule = f"denies {', '.join(denied)}"
    else:
        rule = "requires a region it can check"
    # ``where: message``, de-duplicated in order. Not ``[where]``: the CLI
    # renders errors through rich, which reads square brackets as markup.
    findings = "; ".join(dict.fromkeys(f"{v.expose_id}: {v.message}" for v in errors))
    raise SovereigntyViolationError(
        what=f"Databricks placement refused by the sovereignty policy: {findings}",
        why=(
            "contract.sovereignty is enforced "
            f"({sovereignty.get('enforcementMode', 'strict')}) and {rule}; the tables would "
            "be created in the workspace DATABRICKS_HOST names."
        ),
        fix=(
            "Set binding.location.region on every databricks expose to the region of the "
            "workspace DATABRICKS_HOST names, one the policy allows, or change the policy."
        ),
        doc=doc_url("sovereignty"),
    )


def _refuse_region_conflict(
    tables: Sequence[_Table], refusals: List[UnsupportedBindingError]
) -> None:
    """One workspace has one region: two exposes declaring different ones cannot both hold."""
    declared: Dict[str, str] = {}
    for table in tables:
        if table.region:
            declared.setdefault(table.region, table.where)
    if len(declared) > 1:
        named = "; ".join(f"{where} declares {region}" for region, where in declared.items())
        refusals.append(
            _refusal(
                "region-conflict",
                f"The Databricks exposes declare different regions ({named}), but the module "
                "applies to one workspace (DATABRICKS_HOST), which is in one region.",
                "Declare the same location.region on every Databricks expose, or split the "
                "exposes into products that apply to their own workspace.",
            )
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

    targets = _resolve_tables(compiled, exposures, contract, validating=validating)
    warehouse = _warehouse(env, refusals)
    _refuse_duplicate_tables(compiled.tables, refusals)
    _refuse_region_conflict(compiled.tables, refusals)
    # One workspace, one region: whichever expose declares it, every schema the
    # module owns is checked against it. Taken per table, a schema two exposes
    # share kept the body of whichever was emitted last (``_claim``), and lost
    # the check when that one declared no region.
    region = next((table.region for table in compiled.tables if table.region), "")
    cid = safe_ident(contract.get("id") or contract.get("name") or "product")
    privileges = _table_privileges(contract, targets, refusals)
    dbt_written = dbt_written_exposes(contract, compiled.tables)
    for table in compiled.tables:
        _emit_table(
            compiled, table, cid, warehouse, region, dbt_owned=table.expose_id in dbt_written
        )
        _emit_grants(compiled, table, cid, privileges.get(table.full_name, {}))
        _emit_tags(compiled, table, cid, contract)
    _merge_jobs(
        compiled,
        compile_dbt_jobs(
            contract,
            compiled.tables,
            warehouse,
            validating=validating,
            run_as_allowed=run_as_allowlist(env),
        ),
    )

    # A grant applies to every Databricks expose, so its refusal (an unmapped
    # principal, say) would otherwise be reported once per table.
    unique: Dict[str, UnsupportedBindingError] = {}
    for refusal in refusals:
        unique.setdefault(f"{refusal.kind}:{refusal}", refusal)
    compiled.refusals = list(unique.values())
    return compiled


def _resolve_tables(
    compiled: _Compiled,
    exposures: Sequence[Tuple[int, Mapping[str, Any]]],
    contract: Mapping[str, Any],
    *,
    validating: bool,
) -> List[_GrantTarget]:
    """Resolve each Databricks expose into ``compiled.tables``; a grant target for every one."""
    targets: List[_GrantTarget] = []
    for index, exposure in exposures:
        table = _resolve_table(
            index, exposure, compiled.refusals, contract=contract, validating=validating
        )
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
    compiled: _Compiled, table: _Table, cid: str, warehouse: str, region: str, *, dbt_owned: bool
) -> None:
    """The catalog reference, the owned schema and the table, with their imports.

    ``region`` is the workspace region the contract declares (``""`` if none).
    """
    catalog_key, schema_key, table_key = _keys(cid, table)
    catalog_name = tofu_ref(f"data.databricks_catalog.{catalog_key}.name")
    compiled.data.setdefault("databricks_catalog", {})[catalog_key] = {"name": table.catalog}
    if f"databricks_schema.{schema_key}" not in compiled.owners:
        compiled.imports.append(
            ImportBlock(f"databricks_schema.{schema_key}", f"{table.catalog}.{table.schema}")
        )
    schema_body: Dict[str, Any] = {"catalog_name": catalog_name, "name": table.schema}
    if region:
        schema_body["lifecycle"] = {"precondition": [_region_precondition(compiled, cid, region)]}
    _claim(
        compiled,
        "databricks_schema",
        schema_key,
        f"schema {table.catalog}.{table.schema}",
        schema_body,
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


def _region_precondition(compiled: _Compiled, cid: str, region: str) -> Dict[str, Any]:
    """The plan-time check that the workspace is in the declared ``region``.

    ``data.databricks_current_metastore`` calls the metastore summary of the
    workspace ``DATABRICKS_HOST`` names (provider ``catalog/
    data_current_metastore.go``, v1.140.0), which carries the metastore's
    region; a schema's precondition fails the plan when it differs. The
    region interpolated here passed :data:`_REGION_RE`, so it carries no
    template syntax.
    """
    key = safe_ident(f"{cid}_metastore")
    compiled.data.setdefault("databricks_current_metastore", {})[key] = {}
    actual = f"data.databricks_current_metastore.{key}.metastore_info[0].region"
    return {
        "condition": tofu_ref(f'lower({actual}) == "{region}"'),
        "error_message": TofuExpr(
            f"The contract declares binding.location.region {region}, but the workspace "
            "DATABRICKS_HOST names is in ${" + actual + "}. Residency is checked against the "
            f"workspace's metastore. Point DATABRICKS_HOST at a workspace in {region}, "
            "or correct binding.location.region."
        ),
    }


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


def _uc_tags(
    where: str,
    contract: Mapping[str, Any],
    exposure: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
) -> Dict[str, str]:
    """The governance labels of ``where`` as Unity Catalog tags, or ``{}`` and a refusal.

    ``governance_labels`` keys are ``[a-z0-9_-]``, and a tag key may not contain
    ``-``, so each ``-`` becomes ``_``; the values go as they are. Two labels that
    meet at one key, or more tags than a securable holds, are refused: the module
    would otherwise overwrite one tag with another, or drop some without a word.
    """
    labels = governance_labels(contract, exposure, limit=None)
    tags: Dict[str, str] = {}
    source: Dict[str, str] = {}
    for label, value in labels.items():
        key = label.replace("-", "_")
        if key in tags:
            refusals.append(
                _refusal(
                    "tag-collision",
                    f"{where}: the labels {source[key]!r} and {label!r} are both the Unity "
                    f"Catalog tag key {key!r} (a tag key may not contain '-', so each '-' "
                    "becomes '_'), and one would silently replace the other.",
                    "Rename one of the labels so they differ once '-' is read as '_'.",
                )
            )
            return {}
        tags[key] = value
        source[key] = label
    if len(tags) > _MAX_TAGS:
        refusals.append(
            _refusal(
                "tags-too-many",
                f"{where} would carry {len(tags)} governance tags, and Unity Catalog allows "
                f"{_MAX_TAGS} on a securable, so the rest would be dropped without a word.",
                f"Remove labels from the contract (or the expose) until {_MAX_TAGS} or fewer "
                "remain; the classification, jurisdiction, regulation and residency each count "
                "as one.",
            )
        )
        return {}
    return tags


def _claim_tags(
    compiled: _Compiled,
    prefix: str,
    entity: Tuple[str, str, str],
    tags: Mapping[str, str],
) -> None:
    """One ``databricks_entity_tag_assignment`` per tag, and the import that adopts it.

    ``entity`` is ``(entity type, full name, reference)``. The reference orders the
    assignment after the schema or table it tags (their ``id`` is the full name, read
    from the provider's ``resource_schema.go`` and ``resource_sql_table.go`` at
    v1.140.0). The import id is the provider's, ``entity_type,entity_name,tag_key``;
    it is best-effort because it fails for an entity not created yet, and the
    assignment's create sets the same value anyway.
    """
    kind, full_name, ref = entity
    for tag_key, tag_value in sorted(tags.items()):
        key = f"{prefix}_{unique_ident(tag_key)}"
        address = f"databricks_entity_tag_assignment.{key}"
        owner = f"tag {tag_key!r} on {kind} {full_name}"
        first = address not in compiled.owners
        _claim(
            compiled,
            "databricks_entity_tag_assignment",
            key,
            owner,
            {
                "entity_type": kind,
                "entity_name": ref,
                "tag_key": tag_key,
                "tag_value": tag_value,
            },
        )
        if first and compiled.owners.get(address) == owner:
            compiled.imports.append(
                ImportBlock(address, f"{kind},{full_name},{tag_key}", best_effort=True)
            )
            compiled.tags.append((kind, full_name, tag_key, tag_value))


def _emit_tags(compiled: _Compiled, table: _Table, cid: str, contract: Mapping[str, Any]) -> None:
    """The governance labels on the table's schema and on the table.

    The schema carries the contract's own (no expose's), so two exposes in one schema
    claim one and the same body at each address (:func:`_claim` keeps the last).
    """
    _catalog_key, schema_key, table_key = _keys(cid, table)
    schema_name = f"{table.catalog}.{table.schema}"
    on_schema = _uc_tags(f"schema {schema_name}", contract, {}, compiled.refusals)
    _claim_tags(
        compiled,
        f"{schema_key}_schema_tag",
        ("schemas", schema_name, tofu_ref(f"databricks_schema.{schema_key}.id")),
        on_schema,
    )
    on_table = _uc_tags(table.where, contract, table.exposure, compiled.refusals)
    _claim_tags(
        compiled,
        f"{table_key}_table_tag",
        ("tables", table.full_name, tofu_ref(f"databricks_sql_table.{table_key}.id")),
        on_table,
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
        _enforce_sovereignty(contract, compiled.tables)
        for job, _tables in _jobs_to_pause(compiled, contract):
            _pause(compiled, job)
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

    def standalone_module_refusal(
        self, contract: Mapping[str, Any]
    ) -> Optional[UnsupportedBindingError]:
        """Why this contract's module must not be applied on its own, or ``None``.

        Column masks and row filters are not in the module: they are bound on the SQL
        warehouse around ``tofu apply`` (``pre_apply`` / ``post_apply``). A module written for
        review and a plain ``tofu apply`` would create the table and grant SELECT with none of
        them. ``fluid generate iac`` asks this before it writes one; ``emit`` stays pure, and
        ``fluid apply``, ``plan`` and ``validate`` are unaffected. A contract that does not
        compile is left to ``emit``, which raises its own refusal.
        """
        compiled = _compile(contract, os.environ)
        if compiled.refusals:
            return None
        guarded = [t for t in compiled.tables if t.enforcement is not None]
        if not guarded:
            return None
        return _refusal(
            "enforcement-outside-module",
            f"{', '.join(t.where for t in guarded)} declare column restrictions or row filters, "
            "which Unity Catalog enforces through functions bound on the SQL warehouse by "
            "fluid apply, around tofu apply. They are not in the module generate iac writes, "
            "and a plain tofu apply of it would create the table and grant SELECT with no mask "
            "or row filter, exposing unmasked data to the principals the contract restricts.",
            "Use fluid apply, which binds the masks and row filters before it grants and "
            "revokes access when it cannot (fluid apply --mode dry-run shows the statements).",
            "Or remove the column restrictions and row filters from the contract.",
        )

    @staticmethod
    def _enforcement_targets(contract: Mapping[str, Any]) -> List[Target]:
        compiled = _compile(contract, os.environ)
        if compiled.refusals:
            raise compiled.refusals[0]
        return [Target(table.full_name, table.enforcement) for table in compiled.tables]

    def pre_apply(
        self,
        contract: Mapping[str, Any],
        say: Optional[Callable[[str], None]] = None,
        dry_run: bool = False,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Before ``tofu apply``: bind the contract's column masks and row filter on the
        tables that already exist (``databricks_enforce.pre_apply``), so a grant the apply
        adds lands on a protected table. With ``dry_run`` it prints every statement either
        hook would run, and runs none. Raises :class:`UnsupportedBindingError` to stop the
        run before the apply."""
        targets = self._enforcement_targets(contract)
        say = say or print
        if dry_run:
            preview(targets, say, client_factory)
        else:
            pre_apply(targets, say, client_factory)

    def post_apply(
        self,
        contract: Mapping[str, Any],
        say: Optional[Callable[[str], None]] = None,
        client_factory: Optional[Callable[[], Any]] = None,
        after_failed_apply: bool = False,
    ) -> None:
        """After ``tofu apply``: bind on the tables it created, drop what the contract no
        longer restricts, and revoke ``SELECT`` from the restricted principals of any table
        it could not bind (``databricks_enforce.post_apply``). ``after_failed_apply`` is the
        bind-only mode for an apply that failed part way: it drops nothing and skips a table
        that was never created."""
        post_apply(
            self._enforcement_targets(contract),
            say or print,
            client_factory,
            after_failed_apply=after_failed_apply,
        )

    def plan_notices(self, contract: Mapping[str, Any]) -> List[str]:
        """What the apply cannot do itself and someone must: ``USE CATALOG``; and the dbt jobs
        it creates paused until the tables they write are protected."""
        compiled = _compile(contract, os.environ)
        notices = _use_catalog_notices(compiled)
        for job, tables in _jobs_to_pause(compiled, contract):
            notices.append(
                f"job {job.name!r} is created paused (pause_status PAUSED): it writes "
                f"{', '.join(sorted(tables))}, whose masks and row filter are not bound yet, and "
                "a run before that would fill an unprotected table. The next fluid apply, once "
                "they are bound, starts its schedule; an explicit run-now works meanwhile."
            )
        return notices

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


def _jobs_to_pause(
    compiled: _Compiled, contract: Mapping[str, Any]
) -> List[Tuple[DbtJob, Dict[str, str]]]:
    """``(job, {table: why})`` for each dbt job that writes a table that is not yet protected.

    The masks and row filters are bound around ``tofu apply``, not by it, so a job created in
    the same apply as its table and the grant on it could run (continuous, or a frequent cron)
    and fill the table before ``post_apply`` binds them. Such a job is created paused and
    started by the next apply, once the live bindings match. A manual job has nothing to pause.
    Read live, only for a contract with both a restricted table and a job; unreadable counts as
    not protected.
    """
    from ...util.contract import builds_writing_expose

    guarded = [t for t in compiled.tables if t.enforcement is not None]
    if not guarded or not compiled.jobs:
        return []
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    writes = {
        t.full_name: {
            str(b.get("id")) for b in builds_writing_expose(t.exposure.get("exposeId"), builds)
        }
        for t in guarded
    }
    candidates = [
        job
        for job in compiled.jobs
        if job.trigger != "manual" and any(job.build_id in ids for ids in writes.values())
    ]
    if not candidates:
        return []
    open_tables = unprotected([Target(t.full_name, t.enforcement) for t in guarded])
    out: List[Tuple[DbtJob, Dict[str, str]]] = []
    for job in candidates:
        why = {
            name: reason
            for name, reason in open_tables.items()
            if job.build_id in writes.get(name, ())
        }
        if why:
            out.append((job, why))
    return out


def _pause(compiled: _Compiled, job: DbtJob) -> None:
    """``pause_status: PAUSED`` on the job's schedule or continuous block."""
    key = job.address.split(".", 1)[1]
    body = compiled.resources.get("databricks_job", {}).get(key) or {}
    for block in ("schedule", "continuous"):
        if isinstance(body.get(block), dict):
            body[block]["pause_status"] = "PAUSED"


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
    for kind, full_name, tag_key, tag_value in compiled.tags:
        actions.append(
            {
                "op": "databricks.tag",
                "resource_type": "databricks_entity_tag_assignment",
                # The provider's own import id for a tag assignment.
                "resource_id": f"{kind},{full_name},{tag_key}",
                "tag_key": tag_key,
                "tag_value": tag_value,
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
    #: The workspace region the contract declares, lower-cased; ``""`` if none.
    region: str = ""
    #: ``(key, value)`` tags the table, and its schema, are meant to carry (the module's
    #: ``databricks_entity_tag_assignment`` resources), sorted by key.
    tags: Tuple[Tuple[str, str], ...] = ()
    schema_tags: Tuple[Tuple[str, str], ...] = ()
    #: The row filter Unity Catalog must have bound: ``(function full name, input columns)``.
    row_filter: Optional[Tuple[str, Tuple[str, ...]]] = None
    #: ``(column, function full name)`` per column mask it must have bound.
    masks: Tuple[Tuple[str, str], ...] = ()
    #: The whole expectation, with each function's definition: what ``fluid verify`` checks
    #: a bound function's owner, body, parameters and return type against.
    enforcement: Optional[TableEnforcement] = None
    #: The accepted ``lifecycle.expire``: its retention as written and in seconds, which the
    #: table's Delta history must not exceed. ``None`` when the expose sets no expire.
    retention_period: Optional[str] = None
    retention_seconds: Optional[int] = None


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
    # The module checks every schema against the one declared region, so verify
    # checks every table against it too, not only the expose that declared it.
    region = next((table.region for table in compiled.tables if table.region), "")
    tagged: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for kind, full_name, tag_key, tag_value in compiled.tags:
        tagged.setdefault((kind, full_name), []).append((tag_key, tag_value))
    tables = [
        ManagedTable(
            expose_id=table.expose_id,
            full_name=table.full_name,
            columns=tuple(
                {**column, "type": _norm_type(column["type"])} for column in table.columns
            ),
            readers=tuple(readers.get(table.full_name, ())),
            dbt_written=table.full_name in compiled.dbt_written,
            region=region,
            tags=tuple(sorted(tagged.get(("tables", table.full_name), ()))),
            schema_tags=tuple(
                sorted(tagged.get(("schemas", f"{table.catalog}.{table.schema}"), ()))
            ),
            row_filter=(
                (
                    table.enforcement.row_filter.function.full_name,
                    table.enforcement.row_filter.columns,
                )
                if table.enforcement and table.enforcement.row_filter
                else None
            ),
            masks=tuple(
                (mask.column, mask.function.full_name)
                for mask in (table.enforcement.masks if table.enforcement else ())
            ),
            enforcement=table.enforcement,
            retention_period=table.retention.period if table.retention else None,
            retention_seconds=table.retention.seconds if table.retention else None,
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
