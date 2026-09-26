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

"""Contract <-> live target schema comparison for ``fluid diff``.

Stage 5 of the generated pipeline is a drift gate: it runs ``fluid diff
--exit-on-drift`` before ``plan`` so that nothing is planned against a table
somebody changed by hand. Without a ``--state`` baseline the comparison used
to have nothing to compare against, so the gate was always downgraded and
``has_drift`` was true on the first run and the tenth alike. This module reads
each expose's target as it exists now and compares it with the columns the
contract declares.

Every expose ends in exactly one outcome:

``match``        the target exists and has the declared columns and types
``drift``        the target exists and differs: a column added, removed or
                 retyped outside the contract
``absent``       the target does not exist yet; apply will create it. Not drift.
``not_checked``  forge has no live inspector for this binding (Snowflake, a
                 bare object-store prefix, ...), or the contract declares no
                 schema. Reported, never counted as a pass or a failure.
``error``        the inspector ran and could not answer (credentials, network,
                 a missing SDK). A gate must not read this as "absent".

Design provenance:

- The gate semantics follow OpenTofu's ``plan -refresh-only
  -detailed-exitcode``: compare with what really exists, say "no changes",
  "changes" or "failed" as three different answers, and let a failure win
  over a result (``internal/command/plan.go`` returns the failed operation's
  status before it looks at ``-detailed-exitcode``), because a comparison
  that could not finish might have found drift too. One difference is
  deliberate: refresh-only compares with the last applied state, this
  compares with the contract, so a column the contract has just added or
  dropped reads as drift until ``fluid apply`` has run. ``--state`` is the
  mode that compares with a prior apply.
- The column vocabulary mirrors dbt's model-contract mismatch table
  (``get_contract_mismatches``: "missing in contract", "missing in
  definition", "data type mismatch", sorted by column) and SQLMesh's
  ``SchemaDiff`` (added / removed / modified, names compared lower-cased),
  spelled the way ``fluid verify --reconcile-dbt`` already spells it.
- Nothing here reads a warehouse by itself. The target's schema comes from the
  inspectors forge already ships (``providers/*_validation.py``, used by
  ``fluid test`` and ``fluid contract-validation``). Which table to read, and
  in which region, comes from the IaC emitters' own resolvers
  (``_bq_table_name``, ``provider_block_for``), and the declared types go
  through the emitters' own type tables (``_hive_type``, ``_bq_type``), so the
  comparison is against what ``fluid apply`` created.

Those inspectors were chosen over ``verify.py``'s per-target functions
because a gate needs "not found" and "could not look" to be different answers.
``verify_bigquery_table`` maps every ``get_table`` failure, a 403 included, to
"Table not found", which here would turn a credentials problem into
"absent, to be created" and pass the gate. The validation providers return
``None`` only for not-found (``NotFound`` / ``EntityNotFoundException``) and
raise for everything else. ``verify.py``'s local reader returns column names
without types, so it cannot see a retyped column, and it has no Glue reader at
all. What is imported from ``verify.py`` is its GCP dispatch
(``_gcp_provisioned_kind``) and its BigQuery type synonyms
(``_bq_canonical_type``), so the two commands classify a binding and a type
the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

MATCH = "match"
DRIFT = "drift"
ABSENT = "absent"
NOT_CHECKED = "not_checked"
ERROR = "error"

#: Column-level reasons. ``missing_in_target`` is a declared column the target
#: no longer has (removed outside the contract), ``missing_in_contract`` is a
#: column the target has that the contract never declared (added outside it).
MISSING_IN_TARGET = "missing_in_target"
MISSING_IN_CONTRACT = "missing_in_contract"
TYPE_MISMATCH = "type_mismatch"

# Error text lands in the report file and the CI log, so it is redacted and
# capped before it is stored.
_MAX_DETAIL_CHARS = 500

# A BigQuery project/dataset/table id goes into the REST path of the
# authenticated ``tables.get`` call unencoded (``TableReference.path``), so a
# contract value holding ``/``, ``?`` or ``#`` would change which URL is read.
# BigQuery ids never contain those, nor a dot (the part separator),
# whitespace or control characters.
_BQ_ID_PART = re.compile(r"[^/\\?#%.\s\x00-\x1f\x7f]+")

TypeKey = Callable[[str], Optional[str]]


@dataclass
class ColumnDrift:
    """One column that differs between the contract and the live target."""

    column: str
    reason: str
    contract_type: Optional[str] = None
    target_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "reason": self.reason,
            "contract_type": self.contract_type,
            "target_type": self.target_type,
        }

    def human(self) -> str:
        if self.reason == MISSING_IN_TARGET:
            return f"{self.column}: declared ({self.contract_type or '?'}) but not in the target"
        if self.reason == MISSING_IN_CONTRACT:
            return f"{self.column}: in the target ({self.target_type or '?'}) but not declared"
        return (
            f"{self.column}: type changed, contract={self.contract_type} "
            f"target={self.target_type}"
        )


@dataclass
class ExposeLiveResult:
    """The live comparison of one expose."""

    expose_id: str
    platform: str
    target: str
    status: str
    columns: List[ColumnDrift] = field(default_factory=list)
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expose_id": self.expose_id,
            "platform": self.platform,
            "target": self.target,
            "status": self.status,
            "columns": [c.to_dict() for c in self.columns],
            "detail": self.detail,
        }


@dataclass
class LiveDriftReport:
    """Every expose's live comparison, plus the aggregate the gate reads."""

    exposes: List[ExposeLiveResult] = field(default_factory=list)

    def with_status(self, status: str) -> List[ExposeLiveResult]:
        return [e for e in self.exposes if e.status == status]

    @property
    def has_drift(self) -> bool:
        return bool(self.with_status(DRIFT))

    @property
    def has_errors(self) -> bool:
        return bool(self.with_status(ERROR))

    @property
    def compared(self) -> int:
        """Exposes whose target was actually looked at and answered."""
        return sum(1 for e in self.exposes if e.status in (MATCH, DRIFT, ABSENT))

    def counts(self) -> Dict[str, int]:
        return {
            status: len(self.with_status(status))
            for status in (MATCH, DRIFT, ABSENT, NOT_CHECKED, ERROR)
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_drift": self.has_drift,
            "compared": self.compared,
            "counts": self.counts(),
            "exposes": [e.to_dict() for e in self.exposes],
        }


def printable(text: str) -> str:
    """``text`` with every non-printable character replaced by ``?``.

    Column names come from the live target, which is exactly what someone
    other than the contract can change. A column named with a newline or an
    ANSI escape would otherwise forge extra lines (``... match``) in the CI log
    under the gate's own output.
    """
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_columns(
    declared: List[Tuple[str, Optional[str]]],
    actual: List[Tuple[str, str]],
    *,
    contract_key: TypeKey,
    target_key: TypeKey,
) -> List[ColumnDrift]:
    """Column differences between the declared and the live schema.

    Names match case-insensitively: Glue stores every column name lower-case
    and BigQuery and DuckDB resolve names without case. A type is compared
    only when both keys resolve; ``None`` means the key cannot judge that type
    and the column is not reported, the conservative rule
    ``_verify_reconcile.normalize_type`` follows for the same reason.
    """
    live = {name.lower(): (name, typ) for name, typ in actual}
    declared_keys = set()
    drifts: List[ColumnDrift] = []
    for name, contract_type in declared:
        key = name.lower()
        declared_keys.add(key)
        hit = live.get(key)
        if hit is None:
            drifts.append(ColumnDrift(name, MISSING_IN_TARGET, contract_type=contract_type))
            continue
        if not contract_type:
            continue
        want, got = contract_key(contract_type), target_key(hit[1])
        if want is not None and got is not None and want != got:
            drifts.append(
                ColumnDrift(name, TYPE_MISMATCH, contract_type=contract_type, target_type=hit[1])
            )
    for key, (name, typ) in live.items():
        if key not in declared_keys:
            drifts.append(ColumnDrift(name, MISSING_IN_CONTRACT, target_type=typ))
    return sorted(drifts, key=lambda d: (d.column.lower(), d.reason))


def _family_key(value: str) -> Optional[str]:
    """Coarse type family, or ``None`` when the family is unknown.

    Used for local files, whose column types are whatever the build wrote:
    an ``integer`` column landing as BIGINT is not drift, text landing where a
    number was declared is.
    """
    from ._verify_reconcile import normalize_type

    family = normalize_type(value)
    return None if family == "UNKNOWN" else family


def _compact_lower(value: str) -> Optional[str]:
    return re.sub(r"\s+", "", str(value)).lower() or None


def _hive_key(value: str) -> Optional[str]:
    """The Glue column type apply creates for a declared type."""
    from fluid_build.iac.providers.aws import _hive_type

    return _compact_lower(_hive_type(value))


def _bq_contract_key(value: str) -> Optional[str]:
    """The BigQuery column type apply creates for a declared type.

    ``None`` for an array: BigQuery reports a repeated column by its element
    type and a ``REPEATED`` mode, so the two cannot be compared by type name.
    """
    from fluid_build.iac.providers.gcp import _bq_type

    from .verify import _bq_canonical_type

    key = _bq_canonical_type(_bq_type(value))
    return None if key in ("", "array") else key


def _bq_target_key(value: str) -> Optional[str]:
    from .verify import _bq_canonical_type

    return _bq_canonical_type(value) or None


# ---------------------------------------------------------------------------
# Per-expose inspection
# ---------------------------------------------------------------------------


def _declared_columns(expose: Mapping[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """``(name, type)`` for every column the expose declares."""
    contract_block = expose.get("contract") or {}
    raw = (contract_block.get("schema") if isinstance(contract_block, dict) else None) or (
        expose.get("schema") or []
    )
    if isinstance(raw, dict):
        raw = raw.get("fields") or []
    columns: List[Tuple[str, Optional[str]]] = []
    for col in raw if isinstance(raw, list) else []:
        if isinstance(col, dict) and col.get("name"):
            typ = col.get("type")
            columns.append((str(col["name"]), str(typ) if typ else None))
    return columns


def _safe_detail(exc: BaseException) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    text = f"{type(exc).__name__}: {exc}"
    text = redact_secret_text(" ".join(text.split()))
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 3] + "..."
    return text


def _compared(
    base: ExposeLiveResult,
    declared: List[Tuple[str, Optional[str]]],
    fields: List[Any],
    *,
    contract_key: TypeKey,
    target_key: TypeKey,
) -> ExposeLiveResult:
    actual = [(str(f.name), str(f.type or "")) for f in fields]
    base.columns = compare_columns(
        declared, actual, contract_key=contract_key, target_key=target_key
    )
    base.status = DRIFT if base.columns else MATCH
    return base


def _inspect_local(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    declared: List[Tuple[str, Optional[str]]],
    base: ExposeLiveResult,
    contract_dir: Path,
) -> ExposeLiveResult:
    loc = binding.get("location") or {}
    raw_path = loc.get("path") or (loc.get("properties") or {}).get("path")
    if not raw_path or "://" in str(raw_path):
        base.status = NOT_CHECKED
        base.detail = "the binding names no local file path"
        return base
    # Relative paths are rooted at the contract's directory: the DuckDB build
    # runner writes them under ``workdir = contract_dir``, and ``fluid
    # validate`` reads them from there too.
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = contract_dir / path
    base.target = str(path)
    if not path.exists():
        base.status = ABSENT
        base.detail = "target does not exist yet; apply will create it"
        return base
    if not path.is_file():
        base.status = NOT_CHECKED
        base.detail = "the path is not a single file; a directory of files is not inspected"
        return base

    from fluid_build.providers.local_validation import LocalValidationProvider

    provider = LocalValidationProvider({"base_dir": str(contract_dir)})
    schema = provider.get_resource_schema(dict(expose))
    if schema is None:
        # The file exists, so ``None`` here means "no reader for this file",
        # never "absent".
        base.status = NOT_CHECKED
        base.detail = f"forge reads no schema from '{path.suffix or path.name}' files"
        return base
    return _compared(
        base, declared, schema.fields, contract_key=_family_key, target_key=_family_key
    )


def _aws_region(contract: Mapping[str, Any]) -> Optional[str]:
    """The region ``fluid apply`` puts the AWS resources in, or ``None``.

    Resolved by the emitter's own ``provider_block_for``, so the two cannot
    disagree: the region every AWS binding's ``location.region`` names, or,
    when they name none or several, the environment's (``None`` here, which
    boto3 resolves from ``AWS_REGION`` the same way the tofu provider does).
    The global ``--region`` flag is not used, because it defaults to a GCP
    region.
    """
    from fluid_build.iac.providers.aws import AwsIacPlugin

    region = AwsIacPlugin().provider_block_for(contract).get("region")
    return str(region) if region else None


def _inspect_glue(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    declared: List[Tuple[str, Optional[str]]],
    base: ExposeLiveResult,
    contract: Mapping[str, Any],
) -> ExposeLiveResult:
    from fluid_build.iac.providers.aws import _GLUE_CATALOG_FORMATS

    loc = binding.get("location") or {}
    database, table = loc.get("database"), loc.get("table")
    # Same default as ``AwsIacPlugin.emit``: a binding with no format is
    # provisioned as parquet.
    fmt = str(binding.get("format") or "parquet").lower()
    if not (database and table) or fmt not in _GLUE_CATALOG_FORMATS:
        # Apply creates a Glue table only for a file/lakehouse format with a
        # database and a table; anything else is a bare bucket prefix.
        base.status = NOT_CHECKED
        base.detail = "no Glue table for this binding; the object store itself is not inspected"
        return base
    region = _aws_region(contract)
    base.target = f"glue:{database}.{table}" + (f" ({region})" if region else "")

    from fluid_build.providers import aws_validation

    if not aws_validation.BOTO3_AVAILABLE:
        base.status = ERROR
        base.detail = "boto3 is not installed; install the 'aws' extra to inspect Glue"
        return base
    provider = aws_validation.AWSValidationProvider({"region": region})
    schema = provider.get_resource_schema(dict(expose))
    if schema is None:
        base.status = ABSENT
        base.detail = "Glue table does not exist yet; apply will create it"
        return base
    return _compared(
        base, declared, schema.fields, contract_key=_hive_key, target_key=_compact_lower
    )


def _bigquery_table(
    expose: Mapping[str, Any], binding: Mapping[str, Any]
) -> Tuple[Optional[str], str, str]:
    """``(project, dataset, table)`` the emitter declares for this expose.

    Mirrors ``_emit_bigquery``: the table is ``_bq_table_name``, the dataset
    defaults to ``default``, and a binding that names no project is created in
    the ambient one (``None`` here; the BigQuery client resolves it the same
    way). A legacy ``properties.target`` of ``project.dataset.table`` wins, as
    it does in ``fluid verify``.
    """
    from fluid_build.iac.providers.gcp import _bq_table_name

    props = expose.get("properties") or {}
    target = props.get("target") if isinstance(props, dict) else None
    if isinstance(target, str) and len(target.split(".")) == 3:
        project, dataset, table = target.split(".")
        return project, dataset, table
    loc = binding.get("location") or {}
    bound_project = loc.get("project")
    return (
        str(bound_project) if bound_project else None,
        str(loc.get("dataset") or "default"),
        str(_bq_table_name(expose, loc)),
    )


def _inspect_bigquery(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    declared: List[Tuple[str, Optional[str]]],
    base: ExposeLiveResult,
) -> ExposeLiveResult:
    project, dataset, table = _bigquery_table(expose, binding)
    table_id = ".".join(p for p in (project, dataset, table) if p)
    base.target = f"bigquery:{table_id}" + ("" if project else " (default project)")
    if not all(_BQ_ID_PART.fullmatch(p) for p in (project or "x", dataset, table)):
        base.status = ERROR
        base.detail = "the binding's BigQuery project, dataset or table is not a valid id"
        return base
    try:
        from fluid_build.providers import bigquery_validation
    except ImportError:
        base.status = ERROR
        base.detail = "google-cloud-bigquery is not installed; install the 'gcp' extra"
        return base
    provider = bigquery_validation.BigQueryValidationProvider({"project_id": project})
    # The provider's own resolver reads ``location.properties`` and
    # ``binding.resource`` only, so it is handed the emitter's table id rather
    # than the binding; given ``location.{project,dataset,table}`` it would
    # resolve nothing and report the table as absent.
    schema = provider.get_resource_schema({"binding": {"resource": table_id}})
    if schema is None:
        base.status = ABSENT
        base.detail = "BigQuery table does not exist yet; apply will create it"
        return base
    return _compared(
        base, declared, schema.fields, contract_key=_bq_contract_key, target_key=_bq_target_key
    )


def _inspect_expose(
    expose: Mapping[str, Any], contract: Mapping[str, Any], contract_dir: Path
) -> ExposeLiveResult:
    from fluid_build.iac.provider_match import canonical_cloud

    from .verify import _GCP_BIGQUERY, _GCP_NO_VERIFIER, _gcp_provisioned_kind

    binding = expose.get("binding") or {}
    if not isinstance(binding, dict):
        binding = {}
    expose_id = str(expose.get("exposeId") or expose.get("id") or "<unnamed>")
    platform = canonical_cloud(binding.get("platform")) or str(binding.get("platform") or "")
    base = ExposeLiveResult(expose_id=expose_id, platform=platform, target="", status=ERROR)

    declared = _declared_columns(expose)
    if not declared:
        base.status = NOT_CHECKED
        base.detail = "the contract declares no schema for this expose"
        return base

    try:
        # Same dispatch as ``fluid verify``: a GCP binding is classified by
        # what the emitter provisions for it, not by its format string.
        gcp_kind = _gcp_provisioned_kind(binding)
        fmt = str(binding.get("format") or "").lower()
        if gcp_kind == _GCP_BIGQUERY or fmt == "bigquery_table":
            return _inspect_bigquery(expose, binding, declared, base)
        if gcp_kind == _GCP_NO_VERIFIER:
            base.status = NOT_CHECKED
            base.detail = f"no live inspector for this GCP binding (format: {fmt or 'unset'})"
            return base
        if platform == "aws":
            return _inspect_glue(expose, binding, declared, base, contract)
        if platform in ("", "local"):
            return _inspect_local(expose, binding, declared, base, contract_dir)
    except Exception as exc:  # noqa: BLE001 - an inspector failure is reported, not fatal
        # The inspectors raise for everything except not-found, so this is
        # "could not look", which must never read as "absent".
        base.status = ERROR
        base.columns = []
        base.detail = _safe_detail(exc)
        return base
    base.status = NOT_CHECKED
    base.detail = f"no live inspector for platform '{platform}'"
    return base


def compare_live(contract: Mapping[str, Any], contract_dir: Path) -> LiveDriftReport:
    """Compare every expose's live target with the schema the contract declares."""
    exposes = contract.get("exposes") or []
    if isinstance(exposes, dict):
        exposes = [
            {"exposeId": key, **value} for key, value in exposes.items() if isinstance(value, dict)
        ]
    report = LiveDriftReport()
    for expose in exposes if isinstance(exposes, list) else []:
        if isinstance(expose, dict):
            report.exposes.append(_inspect_expose(expose, contract, contract_dir))
    return report
