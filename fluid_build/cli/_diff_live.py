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
``evolved``      the target differs only in ways the expose's ``schemaPolicy``
                 lets a build make (a column ``evolve_safe`` included). Not drift.
``pending``      the contract changed a column since the last apply and the
                 target is still as that apply left it; apply will change it.
                 Not drift. Needs the last-applied baseline.
``drift``        the target exists and differs: a column added, removed or
                 retyped outside the contract
``absent``       the target does not exist yet; apply will create it. Not drift.
``not_checked``  forge has no live inspector for this binding (Snowflake, a
                 bare object-store prefix, ...), or the contract declares no
                 schema. Reported, never counted as a pass or a failure.
``error``        the inspector ran and could not answer (credentials, network,
                 a missing SDK). A gate must not read this as "absent".

Which differences count as drift:

- A local file or DuckDB table is written by the build, with the columns the
  source had, as far as ``schemaPolicy`` allows (``evolve_safe``, the runtime
  default, includes a new source column and drops a removed one). A
  difference is classified by the same decision engine the build runs
  (``build_runners._schema_evolution.resolve``), so only what that policy
  would refuse (``fail``) is drift.
- A Glue or BigQuery table's columns are declared by the IaC from the
  contract; the build loads rows into them and never changes them. Any
  difference is one ``tofu plan`` would report as changed outside OpenTofu,
  so it is drift whatever the policy says.
- With a last-applied baseline (the plan ``fluid apply`` last ran, see
  ``diff --last-applied``), the comparison is three-way, the way ``kubectl
  apply`` uses its last-applied-configuration: a column where the contract
  moved away from the last apply and the target did not is ``pending``, and
  only a target that moved away from the last apply is measured against the
  policy. Without it the baseline is the contract, and a column the contract
  has just changed cannot be told from one changed by hand.

Design provenance:

- The gate semantics follow OpenTofu's ``plan -refresh-only
  -detailed-exitcode``: compare with what really exists, say "no changes",
  "changes" or "failed" as three different answers, and let a failure win
  over a result (``internal/command/plan.go`` returns the failed operation's
  status before it looks at ``-detailed-exitcode``), because a comparison
  that could not finish might have found drift too.
- The column vocabulary mirrors dbt's model-contract mismatch table
  (``get_contract_mismatches``: "missing in contract", "missing in
  definition", "data type mismatch", sorted by column) and SQLMesh's
  ``SchemaDiff`` (added / removed / modified, names compared lower-cased),
  spelled the way ``fluid verify --reconcile-dbt`` already spells it.
- Nothing here reads a warehouse by itself. The target's schema comes from the
  inspectors forge already ships (``providers/*_validation.py``, used by
  ``fluid test`` and ``fluid contract-validation``). Which table to read, and
  in which region or project, comes from the IaC emitters' and the load
  path's own resolvers (``_bq_table_name``, ``provider_block_for``,
  ``bigquery_load_target``), and the declared types go through the emitters'
  own type tables (``_hive_type``, ``_bq_type``), so the comparison is
  against what ``fluid apply`` created.

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

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

MATCH = "match"
EVOLVED = "evolved"
PENDING = "pending"
DRIFT = "drift"
ABSENT = "absent"
NOT_CHECKED = "not_checked"
ERROR = "error"

#: Every expose status, in the order ``counts`` lists them.
STATUSES = (MATCH, EVOLVED, PENDING, DRIFT, ABSENT, NOT_CHECKED, ERROR)

#: Column-level reasons, always relative to the contract.
#: ``missing_in_target`` is a declared column the target does not have,
#: ``missing_in_contract`` is a column the target has that the contract does
#: not declare.
MISSING_IN_TARGET = "missing_in_target"
MISSING_IN_CONTRACT = "missing_in_contract"
TYPE_MISMATCH = "type_mismatch"

#: What a column difference means for the gate (``ColumnDrift.classification``).
#: ``allowed``: the policy lets a build make it. ``pending``: the contract
#: changed it since the last apply. Only ``drift`` fails the gate.
COLUMN_DRIFT = "drift"
COLUMN_ALLOWED = "allowed"
COLUMN_PENDING = "pending"

#: The policy the build runs under when the contract names none
#: (``_acquisition_common.enforce_schema_policy_or_raise``).
DEFAULT_SCHEMA_POLICY = "evolve_safe"

# Error text lands in the report file and the CI log, so it is redacted and
# capped before it is stored.
_MAX_DETAIL_CHARS = 500

# A BigQuery project/dataset/table id goes into the REST path of the
# authenticated ``tables.get`` call unencoded (``TableReference.path``), so a
# contract value holding ``/``, ``?`` or ``#`` would change which URL is read.
# BigQuery ids never contain those, nor a dot (the part separator),
# whitespace or control characters.
_BQ_ID_PART = re.compile(r"[^/\\?#%.\s\x00-\x1f\x7f]+")

# Files that carry no column types of their own: DuckDB sniffs them on read,
# so a VARCHAR column of digits reads back as BIGINT. Only names are compared.
_UNTYPED_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".ndjson")
_DUCKDB_SUFFIXES = (".duckdb", ".db")

# DuckDB type names the shared family table (``_verify_reconcile``) does not
# list, so a retyped column would otherwise not be judged. From the DuckDB
# data type overview (duckdb.org/docs/current/sql/data_types/overview).
_DUCKDB_FAMILIES = {
    "utinyint": "NUMERIC",
    "usmallint": "NUMERIC",
    "uinteger": "NUMERIC",
    "ubigint": "NUMERIC",
    "hugeint": "NUMERIC",
    "uhugeint": "NUMERIC",
    "bignum": "NUMERIC",
    "varint": "NUMERIC",
    "timestamp_ns": "TIMESTAMP",
    "timestamp_ms": "TIMESTAMP",
    "timestamp_s": "TIMESTAMP",
    "time with time zone": "TIME",
    "timetz": "TIME",
    "enum": "TEXT",
    "list": "STRUCTURED",
    "union": "STRUCTURED",
}

# The AWS region environment variables in the order the AWS SDKs read them
# (aws-sdk-go-v2 ``config/env_config.go``, which the tofu AWS provider uses).
# boto3 1.x reads only the second, so a shell with just ``AWS_REGION`` set
# would otherwise send the Glue read to another region than ``tofu apply``.
_AWS_REGION_ENV = ("AWS_REGION", "AWS_DEFAULT_REGION")

TypeKey = Callable[[str], Optional[str]]
Column = Tuple[str, Optional[str]]


@dataclass
class ColumnDrift:
    """One column that differs between the contract and the live target."""

    column: str
    reason: str
    contract_type: Optional[str] = None
    target_type: Optional[str] = None
    classification: str = COLUMN_DRIFT
    #: The schema-evolution event the difference is, measured from the
    #: baseline (the last apply when there is one, else the contract):
    #: ``added`` / ``removed`` / ``type_widened`` / ``type_narrowed`` /
    #: ``type_changed``. ``None`` for a pending column.
    event: Optional[str] = None
    #: The policy's decision for ``event`` (``fail`` / ``include`` / ``drop``
    #: / ``warn`` / ``cast`` / ``ok``). ``None`` for a pending column.
    action: Optional[str] = None
    #: The column's type in the last-applied baseline, and whether that
    #: baseline declared it at all; both unset without a baseline.
    last_applied_type: Optional[str] = None
    in_last_applied: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "reason": self.reason,
            "contract_type": self.contract_type,
            "target_type": self.target_type,
            "classification": self.classification,
            "event": self.event,
            "action": self.action,
            "in_last_applied": self.in_last_applied,
            "last_applied_type": self.last_applied_type,
        }

    def human(self) -> str:
        if self.reason == MISSING_IN_TARGET:
            text = f"{self.column}: declared ({self.contract_type or '?'}) but not in the target"
        elif self.reason == MISSING_IN_CONTRACT:
            text = f"{self.column}: in the target ({self.target_type or '?'}) but not declared"
        else:
            text = (
                f"{self.column}: type changed, contract={self.contract_type} "
                f"target={self.target_type}"
            )
        if self.classification == COLUMN_PENDING:
            return f"{text} (pending: the contract changed it since the last apply)"
        if self.classification == COLUMN_ALLOWED:
            return f"{text} (allowed by schemaPolicy: {self.event} -> {self.action})"
        return text


@dataclass
class ExposeLiveResult:
    """The live comparison of one expose."""

    expose_id: str
    platform: str
    target: str
    status: str
    columns: List[ColumnDrift] = field(default_factory=list)
    detail: Optional[str] = None
    #: What the target was measured against: ``contract`` or ``last_applied``.
    baseline: str = "contract"
    #: The ``schemaPolicy`` differences were classified with, or ``None``
    #: where the IaC declares the columns and every difference is drift.
    schema_policy: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expose_id": self.expose_id,
            "platform": self.platform,
            "target": self.target,
            "status": self.status,
            "baseline": self.baseline,
            "schema_policy": self.schema_policy,
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
        return sum(1 for e in self.exposes if e.status in (MATCH, EVOLVED, PENDING, DRIFT, ABSENT))

    def counts(self) -> Dict[str, int]:
        return {status: len(self.with_status(status)) for status in STATUSES}

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


def _types_differ(
    declared_type: Optional[str],
    target_type: Optional[str],
    contract_key: TypeKey,
    target_key: TypeKey,
) -> bool:
    """Does the target's type differ from a declared type?

    A declared type the key cannot classify is not judged: that is the
    conservative rule ``_verify_reconcile.normalize_type`` follows. A target
    type the key cannot classify, against a declared type it can, is a
    difference: the column was retyped into something the contract never
    names (a list, an interval, ...), and skipping it would report ``match``.
    """
    want = contract_key(declared_type) if declared_type else None
    if want is None:
        return False
    got = target_key(target_type) if target_type else None
    return got != want


def _declared_differ(a: Optional[str], b: Optional[str], contract_key: TypeKey) -> bool:
    """Do two declared types (the contract's and the last apply's) differ?"""
    ka = contract_key(a) if a else None
    kb = contract_key(b) if b else None
    if ka is not None and kb is not None:
        return ka != kb
    return _compact(a) != _compact(b)


def _compact(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _evolution_decision(
    column: str,
    baseline: Optional[Column],
    target: Optional[Column],
    *,
    policy: Any,
    overrides: Mapping[str, str],
    contract_key: TypeKey,
    target_key: TypeKey,
) -> Tuple[str, str]:
    """``(event, action)`` the build's own decision engine gives this difference.

    The types handed to ``resolve`` are the comparison keys, not the raw
    names, so an ``integer`` declared column landing as BIGINT is no event at
    all, and a widening the engine knows (``int`` to ``bigint``) is named as
    one.
    """
    from fluid_build.api.schema import EvolutionAction, SchemaColumn
    from fluid_build.build_runners._schema_evolution import resolve

    def _col(typ: str) -> Any:
        return SchemaColumn(name=column.lower(), type=typ)

    base_cols: List[Any] = []
    cur_cols: List[Any] = []
    if baseline is not None and target is not None:
        base_cols = [_col(contract_key(baseline[1] or "") or _compact(baseline[1]))]
        cur_cols = [_col(target_key(target[1] or "") or _compact(target[1]) or "?")]
    elif baseline is not None:
        base_cols = [_col(_compact(baseline[1]))]
    elif target is not None:
        cur_cols = [_col(_compact(target[1]))]
    plan = resolve(baseline=base_cols, current=cur_cols, policy=policy, overrides=dict(overrides))
    if not plan.decisions:
        # The keys differ but the engine sees equal names; refuse rather than
        # guess.
        return "type_changed", EvolutionAction.FAIL.value
    decision = plan.decisions[0]
    return decision.event, decision.action.value


def compare_columns(
    declared: List[Column],
    actual: List[Tuple[str, str]],
    *,
    contract_key: TypeKey,
    target_key: TypeKey,
    last_applied: Optional[List[Column]] = None,
    policy: Any = None,
    overrides: Optional[Mapping[str, str]] = None,
) -> List[ColumnDrift]:
    """Column differences between the declared and the live schema.

    Names match case-insensitively: Glue stores every column name lower-case
    and BigQuery and DuckDB resolve names without case.

    With ``last_applied`` (the columns the last apply declared) the
    comparison is three-way: a column the contract changed since that apply,
    whose target still agrees with that apply, is ``pending``. Every other
    difference is measured from the baseline (the last apply, else the
    contract) and classified through ``policy`` and ``overrides`` with the
    build's own decision engine: only a ``fail`` decision is drift. With no
    ``policy`` every difference is drift, as under ``strict``.
    """
    from fluid_build.api.schema import EvolutionAction, SchemaPolicy

    effective_policy = policy if policy is not None else SchemaPolicy.STRICT
    want = {name.lower(): (name, typ) for name, typ in declared}
    live = {name.lower(): (name, typ) for name, typ in actual}
    last = None if last_applied is None else {n.lower(): (n, t) for n, t in last_applied}

    drifts: List[ColumnDrift] = []
    for key in sorted(set(want) | set(live)):
        c, t = want.get(key), live.get(key)
        if c is not None and t is not None:
            if not _types_differ(c[1], t[1], contract_key, target_key):
                continue
            drift = ColumnDrift(c[0], TYPE_MISMATCH, contract_type=c[1], target_type=t[1])
        elif c is not None:
            drift = ColumnDrift(c[0], MISSING_IN_TARGET, contract_type=c[1])
        elif t is not None:
            drift = ColumnDrift(t[0], MISSING_IN_CONTRACT, target_type=t[1])
        else:  # pragma: no cover - every key comes from one side or the other
            continue

        baseline = c
        if last is not None:
            applied = last.get(key)
            drift.in_last_applied = applied is not None
            drift.last_applied_type = applied[1] if applied else None
            contract_moved = (applied is None) != (c is None) or (
                applied is not None
                and c is not None
                and _declared_differ(c[1], applied[1], contract_key)
            )
            target_stayed = (applied is None) == (t is None) and not (
                applied is not None
                and t is not None
                and _types_differ(applied[1], t[1], contract_key, target_key)
            )
            if contract_moved and target_stayed:
                drift.classification = COLUMN_PENDING
                drifts.append(drift)
                continue
            baseline = applied

        drift.event, drift.action = _evolution_decision(
            drift.column,
            baseline,
            t,
            policy=effective_policy,
            overrides=overrides or {},
            contract_key=contract_key,
            target_key=target_key,
        )
        if drift.action != EvolutionAction.FAIL.value:
            drift.classification = COLUMN_ALLOWED
        drifts.append(drift)
    return sorted(drifts, key=lambda d: (d.column.lower(), d.reason))


def _outcome(columns: List[ColumnDrift]) -> str:
    kinds = {c.classification for c in columns}
    if COLUMN_DRIFT in kinds:
        return DRIFT
    if COLUMN_PENDING in kinds:
        return PENDING
    if COLUMN_ALLOWED in kinds:
        return EVOLVED
    return MATCH


def _local_family(value: str) -> Optional[str]:
    """Coarse type family of a DuckDB or contract type, or ``None`` if unknown.

    Used for local files, whose column types are whatever the build wrote:
    an ``integer`` column landing as BIGINT is not drift, text landing where a
    number was declared is. DuckDB spells a list ``VARCHAR[]`` and a fixed
    array ``INTEGER[3]``, and a contract may spell one ``array<string>``.
    """
    from ._verify_reconcile import normalize_type

    text = " ".join(str(value).split()).lower()
    if not text:
        return None
    if text.endswith("]"):
        return "STRUCTURED"
    base = re.split(r"[(<]", text, maxsplit=1)[0].strip()
    if base in _DUCKDB_FAMILIES:
        return _DUCKDB_FAMILIES[base]
    family = normalize_type(base)
    return None if family == "UNKNOWN" else family


def _untyped(_value: str) -> Optional[str]:
    """No type is judged: the file format stores none."""
    return None


def _compact_lower(value: str) -> Optional[str]:
    return _compact(value) or None


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


def _declared_columns(expose: Mapping[str, Any]) -> List[Column]:
    """``(name, type)`` for every column the expose declares."""
    contract_block = expose.get("contract") or {}
    raw = (contract_block.get("schema") if isinstance(contract_block, dict) else None) or (
        expose.get("schema") or []
    )
    if isinstance(raw, dict):
        raw = raw.get("fields") or []
    columns: List[Column] = []
    for col in raw if isinstance(raw, list) else []:
        if isinstance(col, dict) and col.get("name"):
            typ = col.get("type")
            columns.append((str(col["name"]), str(typ) if typ else None))
    return columns


def _schema_policy(expose: Mapping[str, Any]) -> Tuple[Any, Dict[str, str], str]:
    """``(policy, overrides, name)`` the build runs this expose under.

    Read the way ``enforce_schema_policy_or_raise`` reads it: the expose's
    ``contract.schemaPolicy``, ``evolve_safe`` when it names none. A value
    the engine does not know is classified as ``strict``: the build would
    skip its own check for it, and a gate must not read that as permission.
    """
    from fluid_build.api.schema import SchemaPolicy

    block = expose.get("contract") or {}
    block = block if isinstance(block, dict) else {}
    name = str(block.get("schemaPolicy") or DEFAULT_SCHEMA_POLICY)
    overrides = block.get("evolutionOverrides") or {}
    overrides = (
        {str(k): str(v) for k, v in overrides.items()} if isinstance(overrides, dict) else {}
    )
    try:
        return SchemaPolicy(name), overrides, name
    except ValueError:
        return SchemaPolicy.STRICT, overrides, f"{name} (unknown, read as strict)"


def _safe_detail(exc: BaseException) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    text = f"{type(exc).__name__}: {exc}"
    text = redact_secret_text(" ".join(text.split()))
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 3] + "..."
    return text


@dataclass
class _Baseline:
    """What an expose's target is measured against."""

    declared: List[Column]
    #: The last apply's columns for this expose, when that baseline was given
    #: and declares them.
    last_applied: Optional[List[Column]]
    #: The expose whose ``schemaPolicy`` a build-written target is judged by:
    #: the last apply's, which is the one the builds since then ran under.
    policy_expose: Mapping[str, Any]


def _compared(
    base: ExposeLiveResult,
    baseline: _Baseline,
    fields: List[Any],
    *,
    contract_key: TypeKey,
    target_key: TypeKey,
    build_written: bool,
) -> ExposeLiveResult:
    """Compare the target's ``fields`` with the baseline and set the outcome.

    ``build_written``: the build writes this target's columns, so its
    ``schemaPolicy`` decides what it may have changed. Otherwise the IaC
    declares them and every difference is drift.
    """
    policy = None
    overrides: Dict[str, str] = {}
    if build_written:
        policy, overrides, base.schema_policy = _schema_policy(baseline.policy_expose)
    actual = [(str(f.name), str(f.type or "")) for f in fields]
    base.columns = compare_columns(
        baseline.declared,
        actual,
        contract_key=contract_key,
        target_key=target_key,
        last_applied=baseline.last_applied,
        policy=policy,
        overrides=overrides,
    )
    base.status = _outcome(base.columns)
    return base


def _duckdb_table_exists(path: Path, schema_name: str, table: str) -> bool:
    """Does the DuckDB database file at ``path`` hold ``schema_name.table``?

    Asked before ``DESCRIBE``, which answers a missing table with a Catalog
    Error that would otherwise read as "could not look". The identifiers are
    checked by the provider's own allowlist first and are bound as
    parameters, never spliced into the SQL.
    """
    import duckdb

    from fluid_build.providers.local_validation import _build_duckdb_table_ref

    _build_duckdb_table_ref(schema_name, table)
    # Only the catalog is read, so the connection gets no file or network
    # access beyond the database file itself.
    con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})
    try:
        row = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE lower(table_schema) = lower(?) AND lower(table_name) = lower(?)",
            [schema_name, table],
        ).fetchone()
    finally:
        con.close()
    return bool(row and row[0])


def _inspect_local(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    baseline: _Baseline,
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

    suffix = path.suffix.lower()
    if suffix in _DUCKDB_SUFFIXES and loc.get("table"):
        schema_name = str(loc.get("schema") or "main")
        table = str(loc["table"])
        base.target = f"{path} ({schema_name}.{table})"
        if not _duckdb_table_exists(path, schema_name, table):
            base.status = ABSENT
            base.detail = "the database file holds no such table yet; apply will create it"
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
    key: TypeKey = _local_family
    if suffix in _UNTYPED_SUFFIXES:
        # DuckDB sniffs these, so their types say what the data looks like,
        # not what was written. Names only.
        key = _untyped
        base.detail = f"'{suffix}' files carry no column types; column names compared only"
    return _compared(
        base, baseline, schema.fields, contract_key=key, target_key=key, build_written=True
    )


def _aws_region(contract: Mapping[str, Any]) -> Optional[str]:
    """The region ``fluid apply`` puts the AWS resources in, or ``None``.

    Resolved by the emitter's own ``provider_block_for``, so the two cannot
    disagree: the region every AWS binding's ``location.region`` names. When
    they name none or several, the tofu provider takes the environment's, in
    the AWS SDK order (``AWS_REGION``, then ``AWS_DEFAULT_REGION``), and so
    does this. ``None`` leaves it to boto3's shared config. The global
    ``--region`` flag is not used, because it defaults to a GCP region and
    ``provider_block_for`` does not read it either.
    """
    from fluid_build.iac.providers.aws import AwsIacPlugin

    region = AwsIacPlugin().provider_block_for(contract).get("region")
    if region:
        return str(region)
    return next((os.environ[k] for k in _AWS_REGION_ENV if os.environ.get(k)), None)


def _inspect_glue(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    baseline: _Baseline,
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
        base,
        baseline,
        schema.fields,
        contract_key=_hive_key,
        target_key=_compact_lower,
        build_written=False,
    )


def _bigquery_table(
    expose: Mapping[str, Any], binding: Mapping[str, Any], default_project: Optional[str]
) -> Tuple[Optional[str], str, str]:
    """``(project, dataset, table)`` the emitter declares for this expose.

    Mirrors ``_emit_bigquery``: the table is ``_bq_table_name`` and the
    dataset defaults to ``default``. A binding that names no project is
    created in the tofu provider's project, which the load path resolves
    (``bigquery_load_target``: ``GOOGLE_PROJECT``, ``GOOGLE_CLOUD_PROJECT``,
    ``GCLOUD_PROJECT``, ``CLOUDSDK_CORE_PROJECT``); then ``default_project``
    (``--project`` / ``FLUID_PROJECT``, which the plan used); then ``None``,
    the client's own ADC default. A legacy ``properties.target`` of
    ``project.dataset.table`` wins, as it does in ``fluid verify``.
    """
    from fluid_build.build_runners._bigquery_load import _PROJECT_ENV, bigquery_load_target
    from fluid_build.iac.providers.gcp import _bq_table_name

    props = expose.get("properties") or {}
    target = props.get("target") if isinstance(props, dict) else None
    if isinstance(target, str) and len(target.split(".")) == 3:
        project, dataset, table = target.split(".")
        return project, dataset, table
    resolved = bigquery_load_target(binding, expose)
    if resolved is not None:
        project = resolved.get("project")
        dataset, table = str(resolved["dataset"]), str(resolved["table"])
    else:
        # ``format: bigquery_table`` on a binding the GCP resolver does not
        # claim: same fields, same environment order.
        loc = binding.get("location") or {}
        project = loc.get("project") or next(
            (os.environ[k] for k in _PROJECT_ENV if os.environ.get(k)), None
        )
        dataset = str(loc.get("dataset") or "default")
        table = str(_bq_table_name(expose, loc))
    project = project or default_project
    return (str(project) if project else None), dataset, table


def _inspect_bigquery(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    baseline: _Baseline,
    base: ExposeLiveResult,
    default_project: Optional[str],
) -> ExposeLiveResult:
    project, dataset, table = _bigquery_table(expose, binding, default_project)
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
        base,
        baseline,
        schema.fields,
        contract_key=_bq_contract_key,
        target_key=_bq_target_key,
        build_written=False,
    )


def _inspect_expose(
    expose: Mapping[str, Any],
    contract: Mapping[str, Any],
    contract_dir: Path,
    applied_expose: Optional[Mapping[str, Any]],
    default_project: Optional[str],
) -> ExposeLiveResult:
    from fluid_build.iac.provider_match import canonical_cloud

    from .verify import _GCP_BIGQUERY, _GCP_NO_VERIFIER, _gcp_provisioned_kind

    binding = expose.get("binding") or {}
    if not isinstance(binding, dict):
        binding = {}
    expose_id = _expose_id(expose)
    platform = canonical_cloud(binding.get("platform")) or str(binding.get("platform") or "")
    base = ExposeLiveResult(expose_id=expose_id, platform=platform, target="", status=ERROR)

    declared = _declared_columns(expose)
    if not declared:
        base.status = NOT_CHECKED
        base.detail = "the contract declares no schema for this expose"
        return base
    applied_columns = _declared_columns(applied_expose) if applied_expose else []
    if applied_columns:
        base.baseline = "last_applied"
    baseline = _Baseline(
        declared=declared,
        last_applied=applied_columns or None,
        policy_expose=applied_expose if applied_columns and applied_expose else expose,
    )

    try:
        # Same dispatch as ``fluid verify``: a GCP binding is classified by
        # what the emitter provisions for it, not by its format string.
        gcp_kind = _gcp_provisioned_kind(binding)
        fmt = str(binding.get("format") or "").lower()
        if gcp_kind == _GCP_BIGQUERY or fmt == "bigquery_table":
            return _inspect_bigquery(expose, binding, baseline, base, default_project)
        if gcp_kind == _GCP_NO_VERIFIER:
            base.status = NOT_CHECKED
            base.detail = f"no live inspector for this GCP binding (format: {fmt or 'unset'})"
            return base
        if platform == "aws":
            return _inspect_glue(expose, binding, baseline, base, contract)
        if platform in ("", "local"):
            return _inspect_local(expose, binding, baseline, base, contract_dir)
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


def _expose_id(expose: Mapping[str, Any]) -> str:
    return str(expose.get("exposeId") or expose.get("id") or "<unnamed>")


def _exposes(contract: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    exposes = contract.get("exposes") or []
    if isinstance(exposes, dict):
        exposes = [
            {"exposeId": key, **value} for key, value in exposes.items() if isinstance(value, dict)
        ]
    return [e for e in exposes if isinstance(e, dict)] if isinstance(exposes, list) else []


def last_applied_contract(document: Any) -> Optional[Mapping[str, Any]]:
    """The contract a last-applied baseline file holds, or ``None``.

    Accepts the plan ``fluid plan --out`` writes and ``fluid apply`` runs
    from (its ``contract`` key, the contract after the environment overlay),
    or a contract document itself.
    """
    if not isinstance(document, dict):
        return None
    inner = document.get("contract")
    if isinstance(inner, dict) and "exposes" in inner:
        return inner
    if "exposes" in document:
        return document
    return None


def compare_live(
    contract: Mapping[str, Any],
    contract_dir: Path,
    *,
    last_applied: Optional[Mapping[str, Any]] = None,
    default_project: Optional[str] = None,
) -> LiveDriftReport:
    """Compare every expose's live target with the schema the contract declares.

    ``last_applied``: the contract the last apply ran, for the three-way
    comparison. ``default_project``: the project the plan used
    (``--project`` / ``FLUID_PROJECT``), read for a BigQuery binding that
    names none and when the environment names none either.
    """
    applied = {_expose_id(e): e for e in _exposes(last_applied)} if last_applied else {}
    report = LiveDriftReport()
    for expose in _exposes(contract):
        report.exposes.append(
            _inspect_expose(
                expose, contract, contract_dir, applied.get(_expose_id(expose)), default_project
            )
        )
    return report
