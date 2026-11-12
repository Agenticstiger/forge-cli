# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lakeflow pipelines for lakeflow builds: FLUID ``builds[]`` → ``databricks_pipeline``.

A build with ``engine: lakeflow`` (schema 0.7.7) builds its expose as a Lakeflow Declarative
Pipeline (formerly Delta Live Tables), so the contract's data-quality rules run as pipeline
expectations and the pipeline graph's Data quality tab shows each rule's passed and failed
records. One serverless pipeline per build, publishing into the expose's own catalog and
schema (``binding.location.database`` / ``schema``); the founder's decisions of 2026-11-12:

* ``pattern: embedded-logic`` and ``properties.sql``, the SELECT of the dataset. forge-cli
  writes the pipeline's source around it: ``CREATE OR REFRESH MATERIALIZED VIEW <table>`` with
  the expose's columns (typed as the table emitter types them, ``NOT NULL`` where
  ``required``, the contract's comments) and one ``CONSTRAINT <rule id> EXPECT (...)`` per
  row-level rule. The file is uploaded under the applying identity's home, beside a dbt
  build's project (``<home>/.fluid/<product>/<build>/pipeline``). A materialized view, not a
  streaming table: the query reads tables another build rewrites (a dbt ``CREATE OR
  REPLACE``), which a stream cannot follow. With expectations, Lakeflow recomputes it in full
  on every update.
* The rules are read once (``util/dq_rules.py``). Completeness and valid_values are
  expectations on the table. Uniqueness, freshness, and a valid_values rule whose column
  references another product's column (``labels.references``) need the whole table, so each
  is a private materialized view in the same pipeline (``<table>__<rule id>``, never
  published to the catalog) whose one expectation counts what fails: a key's rows, the
  newest timestamp, each value's match in the referenced table. ``schema`` is the declared
  column list itself: an update whose query does not give those columns and types fails.
* Severity: ``info`` and ``warn`` are ``EXPECT`` (the row is kept and counted), ``error`` is
  ``ON VIOLATION DROP ROW`` (the row never reaches the table, and the drop is counted) and
  ``critical`` is ``ON VIOLATION FAIL UPDATE`` (the update fails, and the table's write rolls
  back). There is no quarantine table: the dropped rows would sit in a table outside the
  contract's access policy. A whole-table check has no row of the product to drop, so
  ``error`` is counted there and nothing else, and a ``critical`` one fails the update after
  the table was written (Lakeflow runs a check view after the table it reads).
* The pipeline owns the table: no ``databricks_sql_table`` is created for it, its comments
  are the definition's (one set outside is reset by the next update), and its readers get
  ``SELECT`` on the product's schema, which exists before the first update creates the
  table. Its tags are set after each update ``fluid apply`` runs
  (``build_runners/_databricks_pipeline_run.py``); grants and tags on a pipeline's table
  survive later updates and full refreshes (measured on Databricks Free Edition). Masks,
  row filters and write privileges are refused here: Lakeflow takes masks and filters only
  in the definition, and only the pipeline writes its table.
* ``execution.trigger``: ``manual`` (or none) is a triggered pipeline, which ``fluid apply
  --mode amend-and-build`` starts and waits for; ``continuous`` keeps running. A cron is
  refused for now. A triggered pipeline does not retry a failed update
  (:data:`RETRY_SETTING` is 0): the same rows would fail the same rule.

Everything the pipeline would accept with exit 0 and not honour is refused, as for tables
and jobs. Prior art: DQX (databrickslabs/dqx) for the rule-to-check mapping, Lakeflow's own
expectation patterns (a validation view per primary-key and referential check) for the
whole-table checks, and Databricks Asset Bundles for the layout under the deploying
identity's home.
"""

from __future__ import annotations

import base64
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ...providers._sql_safety import validate_ident
from ...util.contract import builds_writing_expose, is_lakeflow_build
from ...util.dq_checks import DqCheck, read_rules
from ..base import UnsupportedBindingError
from ..naming import TofuExpr, digest_ident, safe_ident, tofu_ref, unique_ident
from ..provider_match import canonical_cloud
from ..source import current_contract_source
from .databricks_access import quote, quote_table, string_literal
from .databricks_jobs import (
    BUILD_TAG,
    CURRENT_USER,
    PRODUCT_TAG,
    _under_home,
    job_build_id,
    job_product,
)

_LOG = logging.getLogger(__name__)

#: The Lakeflow runtime channel: the current one, never the preview.
CHANNEL = "CURRENT"

#: Lakeflow retries a failed triggered update itself, five times by default
#: (``pipelines.numUpdateRetryAttempts``, cause ``RETRY_ON_FAILURE``; seen live 2026-11-12: a
#: FAIL UPDATE rule failed the update, and the retries kept running after ``fluid apply`` had
#: said so). The same rows fail the same rule again, so a triggered pipeline retries nothing;
#: a continuous one keeps Lakeflow's own restarts.
RETRY_SETTING = "pipelines.numUpdateRetryAttempts"

#: What a failing row does, by the rule's severity, on the table itself ...
ON_VIOLATION = {
    "info": "",
    "warn": "",
    "error": " ON VIOLATION DROP ROW",
    "critical": " ON VIOLATION FAIL UPDATE",
}
#: ... and on a whole-table check, which has no row of the product to drop.
ON_TABLE_VIOLATION = {"info": "", "warn": "", "error": "", "critical": " ON VIOLATION FAIL UPDATE"}

#: Table properties the definition sets: the tag keys ``fluid apply`` set on the table and
#: the columns it tagged, so the next run can remove the ones the contract no longer has.
TAGS_PROPERTY = "fluid.tags"
COLUMN_TAGS_PROPERTY = "fluid.column_tags"

#: Unity Catalog's identifier length limit.
_MAX_NAME = 255

#: ``execution`` and ``properties`` keys a pipeline honours; any other one set is refused.
_BUILD_KEYS = frozenset(
    {
        "id",
        "description",
        "pattern",
        "engine",
        "properties",
        "execution",
        "outputs",
        "tags",
        "labels",
    }
)
_EXECUTION_KEYS = frozenset({"trigger", "runtime"})
_RUNTIME_KEYS = frozenset({"platform"})
_PROPERTY_KEYS = frozenset({"sql", "language", "tags", "labels"})
_LEADING_COMMENT_RE = re.compile(r"\A\s*(?:--[^\n]*(?:\n|\Z)|/\*.*?\*/)", re.S)
_QUERY_START_RE = re.compile(r"\A\s*(?:select|with|from|values|table)\b|\A\s*\(", re.I)
_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9_.:-]")


@dataclass(frozen=True)
class LakeflowPipeline:
    """One Lakeflow pipeline, as the planner lists it and ``fluid apply`` runs it."""

    build_id: str
    name: str
    address: str
    #: The full name of the table it publishes.
    table: str
    continuous: bool
    #: ``(dataset, rule id, severity)`` per expectation, the table's first.
    expectations: Tuple[Tuple[str, str, str], ...]
    #: The generated source file, below the applying identity's home.
    path: str
    #: Tags ``fluid apply`` sets after an update: ``(key, value)`` on the table and
    #: ``(column, key, value)`` on its columns.
    table_tags: Tuple[Tuple[str, str], ...] = ()
    column_tags: Tuple[Tuple[str, str, str], ...] = ()
    #: The table's columns, as the definition declares them.
    columns: Tuple[str, ...] = ()


@dataclass
class CompiledPipelines:
    """The pipelines' resources (``(type, key, owner, body)``), data sources and refusals."""

    resources: List[Tuple[str, str, str, Dict[str, Any]]] = field(default_factory=list)
    data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    refusals: List[UnsupportedBindingError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pipelines: List[LakeflowPipeline] = field(default_factory=list)


#: ``(product id, expose id or None, column) -> (catalog.schema.table, None) | (None, why)``.
ReferenceResolver = Callable[[str, Optional[str], str], Tuple[Optional[str], Optional[str]]]


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-lakeflow-{kind}", message, remediation)


def lakeflow_written_exposes(contract: Mapping[str, Any], tables: Sequence[Any]) -> Set[str]:
    """The expose ids of ``tables`` a lakeflow build writes."""
    builds = contract.get("builds") or []
    return {
        table.expose_id
        for table in tables
        if any(is_lakeflow_build(b) for b in builds_writing_expose(table.expose_id, builds))
    }


def _set(value: Any) -> bool:
    return value not in (None, "", [], {})


def _refuse_unhonoured(
    build: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]
) -> None:
    """Every build setting a pipeline would not honour, refused."""
    unhonoured = [f"{key}" for key in build if key not in _BUILD_KEYS and _set(build[key])]
    execution = build.get("execution")
    if isinstance(execution, Mapping):
        unhonoured += [
            f"execution.{key}"
            for key in execution
            if key not in _EXECUTION_KEYS and _set(execution[key])
        ]
        runtime = execution.get("runtime")
        if isinstance(runtime, Mapping):
            unhonoured += [
                f"execution.runtime.{key}"
                for key in runtime
                if key not in _RUNTIME_KEYS and _set(runtime[key])
            ]
            platform = runtime.get("platform")
            if _set(platform) and canonical_cloud(platform) != "databricks":
                refusals.append(
                    _refusal(
                        "platform",
                        f"{where} is a lakeflow build with execution.runtime.platform "
                        f"{platform!r}; a Lakeflow pipeline runs in Databricks.",
                        "Set execution.runtime.platform: databricks, or leave it out.",
                    )
                )
    properties = build.get("properties")
    if isinstance(properties, Mapping):
        unhonoured += [
            f"properties.{key}"
            for key in properties
            if key not in _PROPERTY_KEYS and _set(properties[key])
        ]
    if unhonoured:
        refusals.append(
            _refusal(
                "setting-unsupported",
                f"{where} sets {', '.join(unhonoured)}, which the Lakeflow pipeline would "
                "not honour.",
                "Remove them. A lakeflow build takes pattern, engine, outputs, "
                "properties.sql, execution.trigger (manual or continuous) and "
                "execution.runtime.platform.",
            )
        )


def _continuous(
    build: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]
) -> bool:
    execution = build.get("execution")
    trigger = execution.get("trigger") if isinstance(execution, Mapping) else None
    kind = trigger.get("type") if isinstance(trigger, Mapping) else None
    kind = str(kind or "manual").strip().lower()
    if kind == "continuous":
        return True
    if kind != "manual":
        refusals.append(
            _refusal(
                "trigger-unsupported",
                f"{where} has execution.trigger.type {kind!r}; a lakeflow build is triggered "
                "(manual: fluid apply --mode amend-and-build starts an update, and so does "
                "the pipeline's page) or continuous.",
                "Use trigger type manual or continuous, and schedule a triggered pipeline in "
                "Databricks (a job with a pipeline task) until forge-cli does.",
            )
        )
    return False


def _left_open(sql: str) -> Optional[str]:
    """What ``sql`` leaves open at its end, as Spark SQL lexes it, or ``None``: a ``'`` or
    ``"`` string (backslash escapes), a backtick identifier (a doubled backtick is one
    backtick) or a bracketed comment (Spark nests them). A ``--`` comment ends at its line.

    The query is the last thing in the pipeline's source, so an open comment or string could
    only run into the end of the file; refused here all the same, at validate time, since
    an earlier version that wrote check views after it let an unclosed ``/*`` swallow
    them up to a ``*/`` in a rule's description (security review, 2026-11-12)."""
    i, n, depth = 0, len(sql), 0
    while i < n:
        two = sql[i : i + 2]
        if depth:
            depth += 1 if two == "/*" else -1 if two == "*/" else 0
            i += 2 if two in ("/*", "*/") else 1
            continue
        char = sql[i]
        if two == "/*":
            depth, i = 1, i + 2
        elif two == "--":
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif char in ("'", '"'):
            i += 1
            while i < n and sql[i] != char:
                i += 2 if sql[i] == "\\" else 1
            if i >= n:
                return f"a {char} string"
            i += 1
        elif char == "`":
            end = sql.find("`", i + 1)
            while end >= 0 and sql[end + 1 : end + 2] == "`":
                end = sql.find("`", end + 2)
            if end < 0:
                return "a ` identifier"
            i = end + 1
        else:
            i += 1
    return "a /* comment" if depth else None


def _query(build: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]) -> str:
    """``properties.sql``: one query, without its trailing ``;``; ``""`` and a refusal if not."""
    pattern = str(build.get("pattern") or "hybrid-reference").strip().lower()
    properties = build.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    language = str(properties.get("language") or "sql").strip().lower()
    if pattern != "embedded-logic" or language != "sql":
        refusals.append(
            _refusal(
                "pattern",
                f"{where} is a lakeflow build with pattern {pattern!r} and language "
                f"{language!r}; forge-cli builds the pipeline from pattern embedded-logic, "
                "whose properties.sql is the SELECT of the dataset.",
                "Set pattern: embedded-logic and properties.sql to the query.",
            )
        )
        return ""
    raw = properties.get("sql")
    text = raw.strip() if isinstance(raw, str) else ""
    if text.endswith(";"):
        text = text[:-1].rstrip()
    body = text
    while True:
        stripped = _LEADING_COMMENT_RE.sub("", body, count=1)
        if stripped == body:
            break
        body = stripped
    left_open = _left_open(text) if text else None
    if left_open:
        refusals.append(
            _refusal(
                "query",
                f"{where} properties.sql leaves {left_open} open at its end.",
                "Close it, so the query is one complete SELECT.",
            )
        )
        return ""
    if not text or ";" in text or not _QUERY_START_RE.match(body):
        refusals.append(
            _refusal(
                "query",
                f"{where} properties.sql must be one query (SELECT, WITH ...): the pipeline "
                "publishes its result as the table, so a second statement, or a ';' inside "
                "it, has no place.",
                "Set properties.sql to the one SELECT whose result is the expose's table.",
            )
        )
        return ""
    return text


def _output_table(
    build: Mapping[str, Any],
    where: str,
    contract: Mapping[str, Any],
    by_expose: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
) -> Optional[Any]:
    """The one Databricks table the build writes, or ``None`` and a refusal."""
    outputs = build.get("outputs")
    if isinstance(outputs, list):
        ids = [str(o) for o in outputs]
    else:
        ids = [
            str(e.get("exposeId"))
            for e in contract.get("exposes") or []
            if isinstance(e, Mapping) and e.get("exposeId")
        ]
    tables = [by_expose[i] for i in ids if i in by_expose]
    if len(ids) == 1 and len(tables) == 1:
        return tables[0]
    refusals.append(
        _refusal(
            "outputs",
            f"{where} writes {', '.join(ids) or 'no expose'}; a lakeflow build publishes "
            "exactly one expose bound to a Databricks table (binding.platform databricks, "
            "format delta_table).",
            "Name that one expose in builds[].outputs.",
        )
    )
    return None


def _literal(value: str, what: str, where: str, refusals: List[UnsupportedBindingError]) -> str:
    """``value`` as a Databricks SQL string literal, or ``''`` and a refusal: a Lakeflow source
    file substitutes ``${name}`` from the pipeline's settings, so one would not be literal."""
    try:
        if "${" in value:
            raise ValueError("it contains '${'")
        return string_literal(value)
    except ValueError as exc:
        refusals.append(
            _refusal(
                "literal-unsafe",
                f"{where}: {what} {value!r} cannot be written into the pipeline's SQL ({exc}).",
                "Remove the '${', control characters or line breaks from it.",
            )
        )
        return "''"


def _number(value: Any, where: str, refusals: List[UnsupportedBindingError]) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        refusals.append(
            _refusal(
                "range",
                f"{where}: range bound {value!r} is not a finite number.",
                "Write the column's range validationRule with numbers, such as >= 0.",
            )
        )
        return "0"
    return str(value)


#: Check kinds (``util/dq_checks.py``) one row decides: the table's own expectations.
ROW_KINDS = frozenset({"not_null", "accepted_values", "regex", "bounds"})
#: Kinds only the whole table decides: one private check view each.
VIEW_KINDS = frozenset({"unique", "freshness", "reference"})
_SEVERITIES = ("info", "warn", "error", "critical")


@dataclass(frozen=True)
class _Rule:
    """One dq rule's checks on the table, with the severity the rule is written with
    (``dq_checks`` folds critical into error, and Lakeflow keeps them apart)."""

    rule_id: str
    severity: str
    checks: Tuple[DqCheck, ...]
    description: str = ""


def _row_condition(check: DqCheck, where: str, refusals: List[UnsupportedBindingError]) -> str:
    """The SQL a row passes ``check`` on. A NULL passes a value check: completeness says
    whether one is allowed."""
    column = quote(str(check.column).lower())
    if check.kind == "not_null":
        return f"{column} IS NOT NULL"
    if check.kind == "accepted_values":
        values = ", ".join(_literal(v, "allowed value", where, refusals) for v in check.values)
        return f"({column} IS NULL OR {column} IN ({values}))"
    if check.kind == "regex":
        pattern = _literal(check.pattern or "", "pattern", where, refusals)
        return f"({column} IS NULL OR {column} RLIKE {pattern})"
    bounds = " AND ".join(
        f"{column} {'=' if op == '==' else op} {_number(bound, where, refusals)}"
        for op, bound in check.bounds
    )
    return f"({column} IS NULL OR {bounds})"


def _description(rule: _Rule) -> str:
    """The rule's description as a comment: one line, nothing Lakeflow would substitute."""
    text = " ".join(rule.description.split())
    return "" if "${" in text else text


def _view_name(table: Any, check: DqCheck) -> str:
    return f"{table.name}__{check.name}"


def _check_view(
    table: Any,
    rule: _Rule,
    check: DqCheck,
    referenced: Optional[str],
    where: str,
    refusals: List[UnsupportedBindingError],
) -> str:
    """The private materialized view that checks one whole-table check of ``rule``. Its one
    expectation is named after the rule, so the Data quality tab lists the rule's id."""
    source = quote(table.name)
    column = quote(str(check.column).lower())
    if check.kind == "unique":
        # NULLs are left out, as fluid test and dbt's unique test leave them out.
        expect = "(_fluid_rows = 1)"
        query = (
            f"SELECT {column}, count(*) AS _fluid_rows FROM {source}\n"
            f" WHERE {column} IS NOT NULL GROUP BY {column}"
        )
    elif check.kind == "freshness":
        newer = ">" if check.strict else ">="
        expect = (
            f"(_fluid_newest IS NOT NULL AND _fluid_newest {newer} current_timestamp() - "
            f"INTERVAL {int(check.max_age_seconds or 0)} SECONDS)"
        )
        query = f"SELECT max({column}) AS _fluid_newest FROM {source}"
    else:  # reference
        assert check.reference is not None and referenced is not None
        target = quote(check.reference.column.lower())
        expect = "(_fluid_found)"
        query = (
            f"SELECT c.{column} AS _fluid_value, r._fluid_key IS NOT NULL AS _fluid_found\n"
            f"  FROM {source} c\n"
            f"  LEFT JOIN (SELECT DISTINCT {target} AS _fluid_key FROM {quote_table(referenced)}) r"
            f"\n    ON c.{column} = r._fluid_key\n"
            f" WHERE c.{column} IS NOT NULL"
        )
    description = _description(rule)
    comment = (
        f"\nCOMMENT {_literal(description, 'rule description', where, refusals)}"
        if description
        else ""
    )
    return (
        f"CREATE OR REFRESH PRIVATE MATERIALIZED VIEW {quote(_view_name(table, check))} (\n"
        f"  CONSTRAINT {quote(rule.rule_id)} EXPECT {expect}{ON_TABLE_VIOLATION[rule.severity]}"
        f"\n){comment}\nAS {query};\n"
    )


def _check_problem(
    check: DqCheck, severity: str, known: Set[str], table: Any
) -> Optional[Tuple[str, str, str]]:
    """``(kind, message, fix)`` when the pipeline cannot run ``check`` as its rule says."""
    if check.kind not in ROW_KINDS | VIEW_KINDS | {"schema"}:
        return (
            "rule-unexpressible",
            f"rule {check.rule_id!r} is a {check.kind} check, which a pipeline expectation "
            "cannot express.",
            "Check it another way, or remove the rule.",
        )
    try:
        validate_ident(check.rule_id)
        validate_ident(check.name)
    except ValueError:
        return (
            "rule-id",
            f"rule id {check.rule_id!r} names an expectation and its check view, so it must "
            "be a plain identifier (letters, digits and underscores, not starting with a "
            "digit).",
            "Rename the rule.",
        )
    if check.kind != "schema" and str(check.column or "").lower() not in known:
        return (
            "rule-column",
            f"rule {check.rule_id!r} checks {check.column}, which the table does not have (its "
            "name was refused above).",
            "Rename the column to a plain identifier, then the rule checks it.",
        )
    if check.share is not None and severity in ("error", "critical"):
        operator, threshold = check.share
        action = "DROP ROW" if severity == "error" else "FAIL UPDATE"
        return (
            "rule-threshold",
            f"rule {check.rule_id!r} ({severity}) passes at {operator} {threshold:g} of the "
            f"rows, while ON VIOLATION {action} acts on every failing row.",
            "Make the rule all-rows (threshold 1, operator >=), or make it warn, whose "
            "expectation only counts.",
        )
    return None


def _written_rules(table: Any) -> Dict[str, Mapping[str, Any]]:
    """The expose's ``contract.dq.rules[]`` as written, by id."""
    spec = table.exposure.get("contract") or {}
    dq = spec.get("dq") if isinstance(spec, Mapping) else None
    return {
        str(rule.get("id")): rule
        for rule in ((dq or {}).get("rules") or [])
        if isinstance(rule, Mapping)
    }


def _severity(rule: Mapping[str, Any], check: DqCheck) -> str:
    """The rule's severity as written; ``dq_checks``' level when it is not one of four."""
    severity = str(rule.get("severity") or "").strip().lower()
    if severity in _SEVERITIES:
        return severity
    return "error" if check.severity == "error" else "warn"


def _referenced_table(
    check: DqCheck, resolve: ReferenceResolver
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]]]:
    """``(the referenced table, None)`` for a reference check, or ``(None, problem)``."""
    assert check.reference is not None
    ref = check.reference
    try:
        validate_ident(ref.column.lower())
        full_name, why_not = resolve(ref.product_id, ref.expose_id, ref.column)
    except ValueError:
        full_name, why_not = None, "that column is not a plain identifier"
    if full_name is not None:
        return full_name, None
    target = f"{ref.product_id} {ref.expose_id}" if ref.expose_id else ref.product_id
    return None, (
        "reference-unresolved",
        f"rule {check.rule_id!r} checks {check.column} against {target}.{ref.column}, and "
        f"{why_not}.",
        "Keep the referenced product's contract in this workspace (the directory holding "
        "fluid.workspace.yaml, or a root in FLUID_UPSTREAM_CONTRACTS), bound to a Databricks "
        "table with that column; or remove the rule.",
    )


def _view_problem(table: Any, check: DqCheck) -> Optional[Tuple[str, str, str]]:
    name = _view_name(table, check)
    if check.kind not in VIEW_KINDS or len(name) <= _MAX_NAME:
        return None
    return (
        "rule-id",
        f"check view {name} would be longer than the {_MAX_NAME} characters a Unity Catalog "
        "name holds.",
        "Shorten the rule id.",
    )


def _rules(
    table: Any,
    contract: Mapping[str, Any],
    refusals: List[UnsupportedBindingError],
    resolve: ReferenceResolver,
) -> Tuple[List[_Rule], Dict[str, str]]:
    """The expose's rules as ``util/dq_checks.py`` reads them, grouped by rule, and the
    referenced table of each reference check (by check name). A rule with a check the
    pipeline cannot run is refused whole."""
    read = read_rules(contract, [table.exposure])
    for refusal in read.refusals:
        refusals.append(
            _refusal("rule-unexpressible", f"{table.where}: {refusal.message}", refusal.fix)
        )
    written = _written_rules(table)
    known = {column["name"] for column in table.columns}
    grouped: Dict[str, List[DqCheck]] = {}
    referenced: Dict[str, str] = {}
    refused: Set[str] = set()
    for check in read.checks:
        if check.rule_id in refused:
            continue
        severity = _severity(written.get(check.rule_id) or {}, check)
        problem = _check_problem(check, severity, known, table) or _view_problem(table, check)
        if problem is None and check.kind == "reference":
            full_name, problem = _referenced_table(check, resolve)
            if full_name is not None:
                referenced[check.name] = full_name
        if problem is not None:
            kind, message, fix = problem
            refusals.append(_refusal(kind, f"{table.where}: {message}", fix))
            refused.add(check.rule_id)
            grouped.pop(check.rule_id, None)
            continue
        grouped.setdefault(check.rule_id, []).append(check)
    return [
        _Rule(
            rule_id,
            _severity(written.get(rule_id) or {}, checks[0]),
            tuple(checks),
            str((written.get(rule_id) or {}).get("description") or ""),
        )
        for rule_id, checks in grouped.items()
    ], referenced


def render_pipeline_sql(
    table: Any,
    query: str,
    rules: Sequence[_Rule],
    referenced: Mapping[str, str],
    *,
    product: str,
    build_id: str,
    table_tags: Sequence[Tuple[str, str]] = (),
    column_tags: Sequence[Tuple[str, str, str]] = (),
    refusals: Optional[List[UnsupportedBindingError]] = None,
) -> str:
    """The pipeline's source: one check view per whole-table check, then the table with one
    expectation per row-level rule and the contract's query, last. ``table`` is
    ``databricks.py::_Table``."""
    refusals = refusals if refusals is not None else []
    where = table.where
    described = dict(table.column_comments)
    lines = []
    for column in table.columns:
        parts = [quote(column["name"]), column["type"]]
        if not column.get("nullable", True):
            parts.append("NOT NULL")
        if column["name"] in described:
            parts.append(
                "COMMENT " + _literal(described[column["name"]], "column comment", where, refusals)
            )
        lines.append(" ".join(parts))
    for rule in rules:
        conditions = [
            _row_condition(check, where, refusals)
            for check in rule.checks
            if check.kind in ROW_KINDS
        ]
        if conditions:
            lines.append(
                f"CONSTRAINT {quote(rule.rule_id)} EXPECT ({' AND '.join(conditions)})"
                f"{ON_VIOLATION[rule.severity]}"
            )
    clauses = []
    if table.comment is not None:
        clauses.append("COMMENT " + _literal(table.comment, "table comment", where, refusals))
    properties = [
        ("fluid.product_id", product),
        ("fluid.build_id", build_id),
        (TAGS_PROPERTY, ",".join(key for key, _ in table_tags)),
        (COLUMN_TAGS_PROPERTY, ",".join(sorted({column for column, _, _ in column_tags}))),
    ]
    clauses.append(
        "TBLPROPERTIES (\n"
        + ",\n".join(
            f"  {_literal(key, 'table property', where, refusals)} = "
            f"{_literal(value, 'table property', where, refusals)}"
            for key, value in properties
        )
        + "\n)"
    )
    header = (
        f"-- Generated by forge-cli from {_SAFE_TEXT_RE.sub('_', product)}, build "
        f"{_SAFE_TEXT_RE.sub('_', build_id)}. Do not edit: change the contract and run fluid "
        "apply.\n"
    )
    # The check views first and the table last: the contract's query is the last text in
    # the file, so nothing forge-cli writes can follow it (a dataset's place in the file does
    # not order the pipeline; its references do).
    statements = [
        _check_view(table, rule, check, referenced.get(check.name), where, refusals)
        for rule in rules
        for check in rule.checks
        if check.kind in VIEW_KINDS
    ]
    statements.append(
        f"CREATE OR REFRESH MATERIALIZED VIEW {quote(table.name)} (\n  "
        + ",\n  ".join(lines)
        + "\n)\n"
        + "\n".join(clauses)
        + f"\nAS\n{query}\n;\n"
    )
    return header + "\n" + "\n".join(statements)


def _databricks_table_with(expose: Mapping[str, Any], column: str) -> Optional[str]:
    """``catalog.schema.table`` of an expose bound to a Databricks table with ``column``."""
    binding = expose.get("binding") or {}
    if canonical_cloud(binding.get("platform")) != "databricks":
        return None
    schema = (expose.get("contract") or {}).get("schema") or []
    if not any(
        isinstance(c, Mapping) and str(c.get("name") or "").lower() == column.lower()
        for c in schema
    ):
        return None
    location = binding.get("location") or {}
    parts = [str(location.get(k) or "").strip().lower() for k in ("database", "schema", "table")]
    try:
        return ".".join(validate_ident(part) for part in parts)
    except ValueError:
        return None


def workspace_reference_resolver(contract: Mapping[str, Any]) -> ReferenceResolver:
    """Resolve a reference check's product the way an embedded-SQL build resolves
    ``consumes[]`` (``build_runners/_embedded_sql_io.py``): the contract that declares the
    product id, found from the nearest ``fluid.workspace.yaml`` and
    ``FLUID_UPSTREAM_CONTRACTS``, refusing an id two contracts declare; then its expose, which
    must be bound to a Databricks table that has the column. The upstream is read without an
    ``--env`` overlay: an overlay that moves its table is not followed."""
    own_id = contract.get("id")
    cache: Dict[str, Tuple[Optional[Mapping[str, Any]], str]] = {}

    def load(product_id: str) -> Tuple[Optional[Mapping[str, Any]], str]:
        if product_id == own_id:
            return contract, ""
        if product_id in cache:
            return cache[product_id]
        from ...util.upstream_discovery import collect_search_roots, index_contract_paths
        from ...util.workspace_root import find_workspace_root

        source = current_contract_source()
        root = find_workspace_root(Path(source).parent) if source else None
        roots = collect_search_roots(root)
        if not roots:
            found: Tuple[Optional[Mapping[str, Any]], str] = (
                None,
                "no fluid.workspace.yaml was found above the contract and "
                "FLUID_UPSTREAM_CONTRACTS is not set, so no contract declares it here",
            )
        else:
            index, _skipped = index_contract_paths(roots)
            paths = index.get(product_id) or []
            if len(paths) != 1:
                found = (
                    None,
                    (
                        f"{len(paths)} contracts in the workspace declare {product_id}"
                        if paths
                        else f"no contract in the workspace declares {product_id}"
                    ),
                )
            else:
                from ..._contract_loader import load_contract_with_overlay

                try:
                    found = (load_contract_with_overlay(str(paths[0]), None, _LOG), "")
                except Exception as exc:  # noqa: BLE001 - reported as the refusal's reason
                    found = (None, f"{paths[0]} could not be read ({type(exc).__name__})")
        cache[product_id] = found
        return found

    def resolve(
        product_id: str, expose_id: Optional[str], column: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """The expose ``expose_id`` names (``consumes[]`` lists the product by it), or, with
        none, the product's one Databricks expose that has ``column``."""
        upstream, why = load(product_id)
        if upstream is None:
            return None, why
        names: List[str] = []
        for expose in upstream.get("exposes") or []:
            if not isinstance(expose, Mapping):
                continue
            if expose_id is not None and str(expose.get("exposeId")) != expose_id:
                continue
            name = _databricks_table_with(expose, column)
            if name:
                names.append(name)
        which = f"{product_id} {expose_id}" if expose_id is not None else product_id
        if len(names) != 1:
            return None, (
                f"{which} has {len(names)} Databricks tables with a column {column} "
                "(catalog.schema.table names forge-cli can read)"
            )
        return names[0], None

    return resolve


def compile_lakeflow_pipelines(
    contract: Mapping[str, Any],
    tables: Sequence[Any],
    *,
    schemas: Mapping[str, TofuExpr],
    tags: Mapping[str, Tuple[Mapping[str, str], Sequence[Tuple[str, str, str]]]],
    resolve: Optional[ReferenceResolver] = None,
) -> CompiledPipelines:
    """Every lakeflow build of ``contract``: its pipeline, its source file and refusals.

    ``tables`` are the resolved Databricks tables (``databricks.py::_Table``); ``schemas``
    maps a table's full name to the reference of the schema resource that owns it, so the
    pipeline is created after its schema; ``tags`` maps it to the ``(table tags, (column,
    key, value) column tags)`` ``fluid apply`` sets after an update.
    """
    compiled = CompiledPipelines()
    resolve = resolve or workspace_reference_resolver(contract)
    product = job_product(contract)
    by_expose = {table.expose_id: table for table in tables}
    published: Dict[str, str] = {}
    for index, build in enumerate(contract.get("builds") or []):
        if not isinstance(build, Mapping) or not is_lakeflow_build(build):
            continue
        build_id = job_build_id(contract, build)
        where = f"builds[{index}] ({build_id})"
        before = len(compiled.refusals)
        _refuse_unhonoured(build, where, compiled.refusals)
        continuous = _continuous(build, where, compiled.refusals)
        query = _query(build, where, compiled.refusals)
        table = _output_table(build, where, contract, by_expose, compiled.refusals)
        if table is None or not query:
            continue
        if table.full_name in published:
            compiled.refusals.append(
                _refusal(
                    "table-shared",
                    f"{published[table.full_name]} and {where} both publish {table.full_name}; "
                    "a pipeline owns its table, and a second one cannot write it.",
                    "Write the expose with one lakeflow build.",
                )
            )
            continue
        published[table.full_name] = where
        schema = schemas.get(table.full_name)
        if schema is None:
            compiled.refusals.append(
                _refusal(
                    "writers",
                    f"{where} would publish {table.full_name}, and another build names that "
                    "expose in its builds[].outputs: a pipeline owns its table, so no other "
                    "build can write it.",
                    "Name the expose in this lakeflow build's outputs only.",
                )
            )
            continue
        rules, referenced = _rules(table, contract, compiled.refusals, resolve)
        table_tags, on_columns = tags.get(table.full_name, ({}, ()))
        table_tag_pairs = tuple(sorted(table_tags.items()))
        column_tag_pairs = tuple(sorted(on_columns))
        source = render_pipeline_sql(
            table,
            query,
            rules,
            referenced,
            product=product,
            build_id=build_id,
            table_tags=table_tag_pairs,
            column_tags=column_tag_pairs,
            refusals=compiled.refusals,
        )
        if len(compiled.refusals) > before:
            continue
        _emit(
            compiled,
            product=product,
            build_id=build_id,
            where=where,
            table=table,
            schema=schema,
            source=source,
            continuous=continuous,
            rules=rules,
            table_tags=table_tag_pairs,
            column_tags=column_tag_pairs,
        )
    return compiled


def _emit(
    compiled: CompiledPipelines,
    *,
    product: str,
    build_id: str,
    where: str,
    table: Any,
    schema: TofuExpr,
    source: str,
    continuous: bool,
    rules: Sequence[_Rule],
    table_tags: Tuple[Tuple[str, str], ...],
    column_tags: Tuple[Tuple[str, str, str], ...],
) -> None:
    """The folder, the source file and the pipeline. Each id carries its digest in the
    folder name (``databricks_jobs.py::_Build.root``): one identity applies many
    contracts into one home, and two ids that fold alike must not share a folder."""
    key = unique_ident(f"{safe_ident(product)}_{build_id}")
    root = f"/.fluid/{digest_ident(product)}/{digest_ident(build_id)}/pipeline"
    path = f"{root}/{table.name}.sql"
    dir_key, file_key = f"{key}_pipeline_dir", f"{key}_pipeline_sql"
    compiled.data.setdefault(CURRENT_USER, {})["me"] = {}
    compiled.resources.append(
        (
            "databricks_directory",
            dir_key,
            f"{where} pipeline folder",
            {"path": _under_home("", root)},
        )
    )
    compiled.resources.append(
        (
            "databricks_workspace_file",
            file_key,
            f"{where} pipeline source",
            {
                "path": _under_home("", path),
                "content_base64": base64.b64encode(source.encode("utf-8")).decode("ascii"),
                "depends_on": [f"databricks_directory.{dir_key}"],
            },
        )
    )
    name = f"fluid {product} {build_id}"
    settings: Dict[str, Any] = {} if continuous else {"configuration": {RETRY_SETTING: "0"}}
    compiled.resources.append(
        (
            "databricks_pipeline",
            key,
            f"{where} pipeline",
            {
                **settings,
                "name": name,
                "catalog": table.catalog,
                "schema": schema,
                "serverless": True,
                "continuous": continuous,
                "development": False,
                "channel": CHANNEL,
                "tags": {PRODUCT_TAG: product, BUILD_TAG: build_id},
                "library": [
                    {"file": [{"path": tofu_ref(f"databricks_workspace_file.{file_key}.path")}]}
                ],
            },
        )
    )
    expectations = [
        (table.full_name, rule.rule_id, rule.severity)
        for rule in rules
        if any(check.kind in ROW_KINDS for check in rule.checks)
    ] + [
        (f"{table.catalog}.{table.schema}.{_view_name(table, check)}", rule.rule_id, rule.severity)
        for rule in rules
        for check in rule.checks
        if check.kind in VIEW_KINDS
    ]
    compiled.pipelines.append(
        LakeflowPipeline(
            build_id=build_id,
            name=name,
            address=f"databricks_pipeline.{key}",
            table=table.full_name,
            continuous=continuous,
            expectations=tuple(expectations),
            path=path,
            table_tags=table_tags,
            column_tags=column_tags,
            columns=tuple(column["name"] for column in table.columns),
        )
    )


__all__ = [
    "CHANNEL",
    "COLUMN_TAGS_PROPERTY",
    "RETRY_SETTING",
    "CompiledPipelines",
    "LakeflowPipeline",
    "ON_TABLE_VIOLATION",
    "ON_VIOLATION",
    "TAGS_PROPERTY",
    "compile_lakeflow_pipelines",
    "lakeflow_written_exposes",
    "render_pipeline_sql",
    "workspace_reference_resolver",
]
