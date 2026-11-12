# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog metric views from ``exposes[].semantics``: compile, create, grant, drop.

Each Databricks expose that declares a ``semantics`` block gets one metric view, so Genie,
AI/BI dashboards and SQL read the contract's semantic model natively
(``SELECT country_code, MEASURE(site_count) FROM ... GROUP BY ALL``). The view is
``<catalog>.<schema>.<semantics.name>`` (``<table>_semantic_model`` when the model has no
name), beside its table in the product's own schema, so a schema dropped with its product
takes the view with it and nothing lands outside what the product owns.

The definition is metric view YAML version 1.1 (Databricks, "Metric view YAML syntax
reference"). The mapping follows the Apache Ossie Databricks converter
(``apache/ossie-converters`` ``databricks/``, Apache-2.0) where FLUID and OSI meet, and refuses
what the YAML cannot hold rather than approximating it:

* ``source`` is the expose's table. ``entities`` (the primary key, foreign keys) and
  ``dimensions`` are the view's ``dimensions``; a time dimension's ``timeGranularity`` is applied
  the way MetricFlow presents a dimension at its grain: a ``DATE`` column at ``day`` is used as it
  is, a coarser grain on a ``DATE`` is ``TRUNC(col, 'MONTH')`` (still a ``DATE``), anything else
  ``DATE_TRUNC('MONTH', col)``; ``hour`` or ``minute`` on a ``DATE`` is refused.
* A measure is its aggregate over its ``expr`` (the shared rendering of
  ``forge_datamodel/semantics_builder.py::aggregate_call``, the MCP query compiler's too). A
  ``simple`` metric is its measure's aggregate under the metric's name with its ``filter`` as
  ``FILTER (WHERE ...)``; a ``ratio`` metric is ``MEASURE(numerator) / NULLIF(MEASURE(denominator),
  0)``, NULL rather than a division-by-zero error. ``derived`` metrics, a ratio that carries a
  filter, and an unknown metric type are refused.
* ``nonAdditiveDimension`` is a window measure ordered by that dimension: ``range: current``
  and ``semiadditive: last`` for ``windowChoice: max`` (``first`` for ``min``, MetricFlow's
  default). One difference is kept, not hidden: Databricks takes the last value of the dimension
  per output group (a country whose sites stopped reporting earlier shows its own last day;
  measured live), where MetricFlow without ``window_groupings`` takes one latest value for the
  whole query. A dimension that is not a declared time dimension is refused.
* Every reference to a column of the table, in a dimension, a measure or a filter, is written
  ``source.<column>``. A bare name in a metric view expression reads a field or a measure of the
  view before a column (measured live: a dimension shadowing ``site_status`` made a filter on it
  read the dimension, and a window measure named like a column broke every window measure that
  read the column, ``METRIC_VIEW_WINDOW_MEASURE_REFERENCES_WINDOW_MEASURE``). A name that is no
  column of the table but names a field or measure of the model is refused for the same reason.
* Expressions pass the same allowlist as the governed query path
  (``providers/_sql_safety.validate_sql_expression_allowlist``: no statement keyword, no comment,
  no ``;`` or ``$``), balanced parentheses, no aggregate where none belongs, no ``MEASURE()``, and
  no call that would lend the view owner's rights to its readers (``secret()``,
  ``http_request()``, ``ai_*()``, ``read_*()``, reflection, the querying user's identity).
* Text: the view's ``comment`` is ``semantics.description`` (else the expose's), then the model's
  ``ai-synonyms`` label as ``Synonyms: ...`` and its ``ai-instructions`` label as
  ``Instructions: ...``, since the YAML has no model-level synonym field. A dimension's
  ``comment`` is its description, else its column's; its ``display_name`` is the column's
  ``businessName`` and its ``synonyms`` the column's ``labels.synonyms`` (``;``-separated), at most
  10 of at most 255 characters each. Text holding ``$$`` (which would end the YAML body) or ``${``
  (which Spark may substitute) is refused. Not carried: ``aggTimeDimension`` and
  ``defaultAggTimeDimension`` (MetricFlow's ``metric_time``; a metric view groups by a named time
  dimension), ``createMetric`` (every measure is queryable), a metric's ``owner``, the model's
  ``tags`` and its ``ai-example-questions`` label (a Genie space's sample questions).

The databricks/databricks provider has no metric view resource (terraform-provider-databricks
#4827), so the view is created on the SQL warehouse through the SQL Statement Execution API,
like the column masks (``databricks_enforce.py``), around ``tofu apply``:

* :func:`pre_apply` refuses, before anything is applied, a view whose name is taken by an object
  this module did not create: another kind of table, a view without this product's
  ``fluid.product_id`` property, or one the deploy identity does not own.
* :func:`post_apply` runs after the masks and row filters are bound. It creates a missing view
  with ``CREATE VIEW`` (never ``CREATE OR REPLACE``: replacing a view drops its grants, measured
  live, and makes whoever replaces it its owner) and updates a changed one with ``ALTER VIEW ...
  AS``, which keeps its grants. The view carries ``fluid.product_id``, ``fluid.expose_id`` and
  ``fluid.definition_sha256`` as properties; one whose digest and stored structure match is
  left alone, so a second apply runs nothing. ``SELECT`` on the view is exactly the principals
  that may read its table (``SELECT`` or ``ALL_PRIVILEGES`` there, from ``accessPolicy.grants``
  and ``policy.authz.readers``): granted to them, revoked from anyone else. Readers also need
  ``USE SCHEMA`` (the module grants it) and ``USE CATALOG`` (named by the plan notice). A view
  of this product that the contract no longer declares is dropped once the apply succeeds.
* A metric view reads its table with its owner's rights, and Unity Catalog applies the table's
  row filter and column masks to whoever queries the view (a row filter measured live). When
  binding them fails, :func:`lock_restricted` revokes ``SELECT`` on the views over restricted
  tables from everyone but the owner before the run fails, so a view cannot serve rows the
  fail-safe closed on the table.

``fluid apply --mode dry-run`` prints every statement (:func:`preview`); a module written by
``fluid generate iac`` holds no metric view (it is no OpenTofu resource), and ``fluid apply``
creates it.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import yaml

from ...forge_datamodel import semantics_builder as _semantics
from ...forge_datamodel.time_grains import normalize_time_grain
from ...providers._sql_safety import validate_ident, validate_sql_expression_allowlist
from ..base import UnsupportedBindingError
from . import databricks_enforce as _enforce
from .databricks_access import quote_principal, quote_table, revoke_all_statement, string_literal
from .databricks_enforce import (
    DATABRICKS_EXTRA_INSTALL,
    WAREHOUSE_ENV,
    Session,
    SqlRunner,
    StatementFailed,
    _list_named,
    _list_pages,
    _privileges,
    owner_matches,
    read_table,
    scrub,
)

#: The metric view YAML version this module writes: the one with ``display_name``, ``synonyms``
#: and window measures, measured on Databricks SQL 2026.45 (Free Edition).
YAML_VERSION = 1.1
#: The ``table_type`` Unity Catalog reports for a metric view.
METRIC_VIEW = "METRIC_VIEW"
#: The view name when ``semantics.name`` is not given: ``<table>`` + this.
DEFAULT_SUFFIX = "_semantic_model"
#: Properties every view this module creates carries: whose it is, and what it was created from.
PRODUCT_PROPERTY = "fluid.product_id"
EXPOSE_PROPERTY = "fluid.expose_id"
DIGEST_PROPERTY = "fluid.definition_sha256"
#: Metric view YAML 1.1 limits: at most 10 synonyms, and 255 characters in a synonym or a
#: ``display_name``.
MAX_SYNONYMS = 10
MAX_TEXT = 255
#: How a metric view expression names its source table.
SOURCE = "source"
#: Table privileges that read: who holds one on the table may read its metric view.
_READ = frozenset({"SELECT", "ALL_PRIVILEGES"})
#: Text keys Unity Catalog keeps as metadata and leaves out of the stored ``view_definition``
#: (measured live): the structural comparison ignores them, the digest does not.
_METADATA_KEYS = ("comment", "display_name", "synonyms")
#: Grains a ``DATE`` column does not have.
_SUB_DAY = frozenset({"hour", "minute"})

#: A quoted identifier, a string literal (either quote), or an identifier that is not the tail
#: of a number or of a longer word. Backslashes cannot occur: the allowlist refuses them.
_TOKEN_RE = re.compile(
    r"`(?:``|[^`])*`|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|(?<![A-Za-z0-9_$])[A-Za-z_][A-Za-z0-9_]*"
)
_LITERAL_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")
_MEASURE_CALL_RE = re.compile(r"(?i)\bmeasure\s*\(")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: Functions a contract expression may not call. A metric view evaluates its expressions with
#: its owner's rights (the deploy identity) for everyone who queries it, so a call that reads a
#: secret, reaches a network connection or a model endpoint, reads files or streams, or reflects
#: into the JVM would lend that identity to every reader; one that asks who is querying is
#: refused by Databricks in a metric view; ``IDENTIFIER()`` names a function from a string. The
#: row filter's predicates are held tighter still (``databricks_access.ALLOWED_FUNCTIONS``).
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "secret",
        "try_secret",
        "http_request",
        "vector_search",
        "java_method",
        "reflect",
        "try_reflect",
        "current_user",
        "session_user",
        "user",
        "is_member",
        "is_account_group_member",
        "current_recipient",
        "identifier",
    }
)
_FORBIDDEN_PREFIXES = ("ai_", "read_")
_NAME = r"(?:`(?:``|[^`])*`|[A-Za-z_][A-Za-z0-9_]*)"
#: Every call: a name, quoted or not, qualified or not, before ``(`` (string literals blanked).
_CALL_RE = re.compile(rf"(?<![A-Za-z0-9_$`])({_NAME}(?:\s*\.\s*{_NAME})*)\s*\(")
#: What a name may follow and still not be a column: ``CAST(x AS <type>)``.
_AFTER_AS_RE = re.compile(r"(?i)\bAS$")

Say = Callable[[str], None]
ClientFactory = Callable[[], Any]
Step = Tuple[str, str]


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-metric-view-{kind}", message, tuple(remediation))


class MetricViewError(UnsupportedBindingError):
    """A metric view could not be created, updated, granted or dropped on the warehouse."""

    def __init__(self, message: str, remediation: Sequence[str] = ()):
        super().__init__("databricks-metric-view-failed", message, tuple(remediation))


class _Dumper(yaml.SafeDumper):
    """Plain YAML for a reader: no ``&id`` anchors for a repeated value, multi-line text as a
    ``|`` block."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _represent_str)


def _dump(document: Mapping[str, Any]) -> str:
    return yaml.dump(
        dict(document), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1_000_000
    )


def _plain_ident(raw: Any) -> Optional[str]:
    """``raw`` stripped when it is a plain identifier, else ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        return validate_ident(raw.strip())
    except ValueError:
        return None


# ── the compiled views ─────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricView:
    """One metric view: where it goes, what it says and who reads it."""

    full_name: str
    #: The table it reads.
    source: str
    expose_id: str
    product_id: str
    #: The metric view YAML.
    definition: str
    #: The principals ``SELECT`` on the view goes to, as the table's grants name them.
    readers: Tuple[str, ...]
    #: The table it reads has column masks or a row filter.
    restricted: bool
    where: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.definition.encode("utf-8")).hexdigest()

    def _properties(self) -> str:
        pairs = (
            (PRODUCT_PROPERTY, self.product_id),
            (EXPOSE_PROPERTY, self.expose_id),
            (DIGEST_PROPERTY, self.digest),
        )
        return ", ".join(f"{string_literal(k)} = {string_literal(v)}" for k, v in pairs)

    @property
    def create(self) -> str:
        return (
            f"CREATE VIEW {quote_table(self.full_name)} WITH METRICS LANGUAGE YAML "
            f"TBLPROPERTIES ({self._properties()}) AS $${self.definition}$$"
        )

    @property
    def update(self) -> Tuple[str, str]:
        """``ALTER VIEW ... AS`` (it keeps the view's grants), then the new digest."""
        name = quote_table(self.full_name)
        return (
            f"ALTER VIEW {name} AS $${self.definition}$$",
            f"ALTER VIEW {name} SET TBLPROPERTIES ({self._properties()})",
        )


@dataclass
class MetricViews:
    """Every metric view a contract declares, and where a view it no longer declares may be."""

    product_id: str = ""
    views: List[MetricView] = field(default_factory=list)
    #: ``catalog.schema`` of each schema the product's tables are in.
    schemas: Tuple[str, ...] = ()

    def plan_actions(self) -> List[Dict[str, Any]]:
        """The views as the native planner's actions (``databricks.planned_objects``)."""
        return [
            {
                "op": "databricks.metric_view.ensure",
                # No provider resource: fluid apply creates it on the SQL warehouse.
                "resource_type": "databricks_metric_view",
                "resource_id": view.full_name,
                "source": view.source,
                "readers": list(view.readers),
            }
            for view in self.views
        ]


@dataclass(frozen=True)
class _Field:
    name: str
    #: The expression as the contract wrote it.
    raw: str
    is_time: bool


@dataclass(frozen=True)
class _Measure:
    """A base measure, for the simple metrics over it."""

    entry: Mapping[str, Any]
    #: Its ``expr``, columns qualified.
    expr: str
    window: Optional[List[Dict[str, str]]]


@dataclass(frozen=True)
class _Ratio:
    name: str
    numerator: str
    denominator: str
    what: str
    description: Any


class _ViewCompiler:
    """One expose's ``semantics`` block → metric view YAML, or refusals."""

    def __init__(self, table: Any, semantics: Mapping[str, Any], refusals: List[Any]):
        self.table = table
        self.semantics = semantics
        self.where = f"{table.where} semantics"
        self.refusals = refusals
        self.failed = False
        #: The table's columns by the lower-case name Unity Catalog stores.
        self.columns: Dict[str, Mapping[str, Any]] = {c["name"]: c for c in table.columns}
        self.schema_columns: Dict[str, Mapping[str, Any]] = {}
        for column in (table.exposure.get("contract") or {}).get("schema") or []:
            if isinstance(column, Mapping):
                self.schema_columns[str(column.get("name") or "").strip().lower()] = column
        #: Every name the model declares, lower-cased: a bare one in an expression would read it.
        self.declared: Set[str] = {
            entry["name"].strip().lower()
            for kind in ("entities", "dimensions", "measures", "metrics")
            for entry in semantics.get(kind) or []
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
        }
        #: Names taken in the view (fields and measures share one namespace, ignoring case).
        self.taken: Dict[str, str] = {}
        self.dimension_docs: List[Dict[str, Any]] = []
        self.fields: Dict[str, _Field] = {}
        self.measure_docs: List[Dict[str, Any]] = []
        self.base: Dict[str, _Measure] = {}
        self.ratios: List[_Ratio] = []

    # ── checks ──

    def refuse(self, kind: str, message: str, *remediation: str) -> None:
        self.failed = True
        self.refusals.append(_refusal(kind, message, *remediation))

    def name(self, raw: Any, what: str) -> Optional[str]:
        text = _plain_ident(raw)
        if text is None:
            self.refuse(
                "invalid",
                f"{what} is {raw!r}, which is not a plain identifier (letters, digits and "
                "underscores, not starting with a digit).",
                "Rename it: metric view dimensions and measures are queried by this name.",
            )
        return text

    def claim(self, name: str, what: str) -> bool:
        lower = name.lower()
        if lower == SOURCE:
            self.refuse(
                "invalid",
                f"{what} is named {name!r}, the name a metric view expression gives its source "
                "table: every source.<column> in the view would read it instead.",
                "Rename it.",
            )
            return False
        if lower in self.taken:
            self.refuse(
                "collision",
                f"{what} is named {name!r}, as is {self.taken[lower]}: a metric view's dimensions "
                "and measures share one namespace, ignoring case.",
                "Rename one of them.",
            )
            return False
        self.taken[lower] = what
        return True

    def text(self, raw: Any, what: str, *, limit: Optional[int] = None) -> Optional[str]:
        if raw is None or isinstance(raw, (Mapping, list)):
            return None
        text = str(raw).strip()
        if not text:
            return None
        if "$$" in text or "${" in text:
            self.refuse(
                "invalid",
                f"{what} holds {'$$' if '$$' in text else '${'}: the metric view's YAML is sent "
                "between $$ quotes, which $$ would end, and Spark may substitute ${...} with a "
                "variable, setting or environment value.",
                "Write it without '$$' and '${' (for example '$ {').",
            )
            return None
        if limit is not None and len(text) > limit:
            self.refuse(
                "invalid",
                f"{what} is {len(text)} characters; a metric view holds at most {limit}.",
                f"Shorten it to {limit} characters.",
            )
            return None
        return text

    def expression(self, raw: Any, what: str) -> Optional[str]:
        """``raw`` with every column of the table written ``source.<column>``; ``None`` if refused."""
        text = self._allowed(raw, what)
        if text is None:
            return None
        scan = _LITERAL_RE.sub(" ", text)
        refused_call = _refused_call(scan)
        if refused_call is not None:
            self.refuse(
                "invalid",
                f"{what} calls {refused_call}: a metric view evaluates its expressions with its "
                "owner's rights for everyone who queries it, so it may not read secrets, reach a "
                "connection, a model or a file, reflect, ask who is querying, name a function "
                "from a string, or call a function by a quoted or qualified name (another "
                "schema's function runs with the owner's rights too).",
                "Compute the value in the table's build and reference the column.",
            )
            return None
        nested = _semantics.first_aggregate_call(scan)
        if _MEASURE_CALL_RE.search(scan) or nested is not None:
            called = "MEASURE()" if _MEASURE_CALL_RE.search(scan) else f"{str(nested).upper()}(...)"
            self.refuse(
                "invalid",
                f"{what} is {raw!r}, which calls {called}: a contract expression reads the "
                "table's columns, and the metric view applies the measure's agg itself (a "
                "dimension or filter cannot hold an aggregate).",
                "Split it: 'expr: SUM(amount)' + 'agg: sum' becomes 'expr: amount' + 'agg: sum'; "
                "a ratio of two measures is a ratio metric.",
            )
            return None
        qualified, bare = _qualify(text, self.columns)
        shadowed = sorted({n for n in bare if n.lower() in self.declared})
        if shadowed:
            self.refuse(
                "invalid",
                f"{what} reads {', '.join(repr(n) for n in shadowed)}, which is not a column of "
                f"{self.table.full_name} but names a dimension or measure of this model: in a "
                "metric view the name would read that dimension or measure, not a column.",
                "Declare the column in contract.schema, or rename the dimension or measure.",
            )
            return None
        return qualified

    def _allowed(self, raw: Any, what: str) -> Optional[str]:
        """``raw`` if the allowlist takes it and its parentheses balance."""
        try:
            text = validate_sql_expression_allowlist(raw) if isinstance(raw, str) else ""
        except ValueError:
            text = ""
        if not text:
            self.refuse(
                "invalid",
                f"{what} is {raw!r}, which is not an expression this module writes into a metric "
                "view: only names, literals, operators and parentheses, with no statement "
                "keyword (SELECT, DROP, ...), no comment, and no ';' or '$'.",
                "Rewrite it as a plain SQL expression over the table's columns.",
            )
            return None
        if not _semantics.parens_balanced(
            _LITERAL_RE.sub(" ", text)
        ):  # a ')' in '...' closes nothing
            self.refuse(
                "invalid",
                f"{what} is {raw!r}, whose parentheses do not balance.",
                "Close every parenthesis it opens.",
            )
            return None
        return text

    def missing_column(self, entry: Mapping[str, Any], name: str, what: str) -> bool:
        """Refuse an entry with no ``expr`` whose name is no column: it would read nothing."""
        if entry.get("expr") is not None or name.lower() in self.columns:
            return False
        self.refuse(
            "invalid",
            f"{what} ({name}) has no expr, and {self.table.full_name} has no column {name!r} for "
            "it to read.",
            "Give it an expr over the table's columns, or declare the column in contract.schema.",
        )
        return True

    # ── dimensions ──

    def compile_fields(self) -> None:
        for kind in ("entities", "dimensions"):
            for position, entry in enumerate(self.semantics.get(kind) or []):
                if isinstance(entry, Mapping):
                    self._add_field(kind, f"{self.where}.{kind}[{position}]", entry)

    def _add_field(self, kind: str, what: str, entry: Mapping[str, Any]) -> None:
        name = self.name(entry.get("name"), f"{what}.name")
        if name is None:
            return
        raw = entry.get("expr") or name
        is_time = kind == "dimensions" and str(entry.get("type") or "") == "time"
        prior = self.fields.get(name.lower())
        if prior is not None and not is_time and str(raw).strip().lower() == prior.raw.lower():
            return  # an entity and a dimension over one column: one field
        if not self.claim(name, f"{what} ({name})") or self.missing_column(entry, name, what):
            return
        expr = self.expression(raw, f"{what}.expr")
        if expr is not None and is_time:
            expr = self._time_expr(entry, str(raw), expr, what)
        if expr is None:
            return
        document: Dict[str, Any] = {"name": name, "expr": expr}
        self._describe(document, entry, str(raw), what)
        self.dimension_docs.append(document)
        self.fields[name.lower()] = _Field(name=name, raw=str(raw).strip(), is_time=is_time)

    def _time_expr(self, entry: Mapping[str, Any], raw: str, expr: str, what: str) -> Optional[str]:
        params = entry.get("typeParams")
        declared = params.get("timeGranularity") if isinstance(params, Mapping) else None
        if declared is None:
            return expr
        grain = normalize_time_grain(str(declared))
        if grain is None:
            self.refuse(
                "unsupported",
                f"{what}.typeParams.timeGranularity is {declared!r}, which is no grain a metric "
                "view can truncate to.",
                "Use day, week, month, quarter, year, hour or minute.",
            )
            return None
        column = self.columns.get(raw.strip().lower())
        if column is None or str(column.get("type")) != "date":
            return f"DATE_TRUNC('{grain.upper()}', {expr})"
        if grain in _SUB_DAY:
            self.refuse(
                "unsupported",
                f"{what} is the DATE column {raw!r} with timeGranularity {grain!r}, which a "
                "date does not have.",
                "Use a day or coarser grain, or a TIMESTAMP column.",
            )
            return None
        return expr if grain == "day" else f"TRUNC({expr}, '{grain.upper()}')"

    def _describe(
        self, document: Dict[str, Any], entry: Mapping[str, Any], raw: str, what: str
    ) -> None:
        """The field's comment, and the display name and synonyms of the column it is."""
        bare = raw.strip().lower()
        column = self.schema_columns.get(bare) if bare in self.columns else None
        comment = self.text(entry.get("description"), f"{what}.description")
        if comment is None and column is not None:
            comment = self.text(column.get("description"), f"{self.table.where} column {raw!r}")
        if comment is not None:
            document["comment"] = comment
        if column is None:
            return
        display = self.text(
            column.get("businessName"),
            f"{self.table.where} column {raw!r} businessName",
            limit=MAX_TEXT,
        )
        if display is not None:
            document["display_name"] = display
        synonyms = self._synonyms(column, raw)
        if synonyms:
            document["synonyms"] = synonyms

    def _synonyms(self, column: Mapping[str, Any], raw: str) -> List[str]:
        labels = column.get("labels")
        value = labels.get("synonyms") if isinstance(labels, Mapping) else None
        if not isinstance(value, str):
            return []
        what = f"{self.table.where} column {raw!r} labels.synonyms"
        out: List[str] = []
        for part in value.split(";"):
            text = self.text(part, what, limit=MAX_TEXT)
            if text is not None and text.lower() not in {s.lower() for s in out}:
                out.append(text)
        if len(out) > MAX_SYNONYMS:
            self.refuse(
                "invalid",
                f"{what} lists {len(out)} synonyms; a metric view field holds at most "
                f"{MAX_SYNONYMS}.",
                f"Keep the {MAX_SYNONYMS} that matter most.",
            )
            return []
        return out

    # ── measures ──

    def compile_measures(self) -> None:
        for position, entry in enumerate(self.semantics.get("measures") or []):
            if isinstance(entry, Mapping):
                self._add_measure(f"{self.where}.measures[{position}]", entry)
        for position, entry in enumerate(self.semantics.get("metrics") or []):
            if isinstance(entry, Mapping):
                self._add_metric(f"{self.where}.metrics[{position}]", entry)
        self._place_ratios()

    def _add_measure(self, what: str, entry: Mapping[str, Any]) -> None:
        name = self.name(entry.get("name"), f"{what}.name")
        if name is None or not self.claim(name, f"{what} ({name})"):
            return
        if self.missing_column(entry, name, what):
            return
        expr = self.expression(entry.get("expr") or name, f"{what}.expr")
        window = self._window(entry, what)
        if expr is None or window is False:
            return
        try:
            aggregate = _semantics.aggregate_call(name, entry, expr)
        except ValueError as exc:
            self.refuse("unsupported", f"{what}: {exc}.", "Use an agg the contract schema lists.")
            return
        self.base[name.lower()] = _Measure(entry=entry, expr=expr, window=window or None)
        self.measure_docs.append(self._measure(name, aggregate, entry.get("description"), what))
        if window:
            self.measure_docs[-1]["window"] = copy.deepcopy(window)

    def _add_metric(self, what: str, entry: Mapping[str, Any]) -> None:
        name = self.name(entry.get("name"), f"{what}.name")
        if name is None:
            return
        kind = str(entry.get("type") or "")
        if kind == "simple":
            self._simple(name, entry, what)
        elif kind == "ratio":
            self._ratio(name, entry, what)
        elif kind == "derived":
            self.refuse(
                "unsupported",
                f"{what} ({name}) is a derived metric. Its expr names other metrics, which "
                "a metric view reads only through MEASURE(), and rewriting the expression is "
                "not done: it is refused rather than guessed.",
                "Express it as a ratio of two metrics, or remove it for Databricks.",
            )
        else:
            self.refuse(
                "unsupported",
                f"{what} ({name}) has type {kind!r}; a metric view takes simple and ratio metrics.",
                "Use type simple or ratio.",
            )

    def _measure(self, name: str, expr: str, description: Any, what: str) -> Dict[str, Any]:
        document: Dict[str, Any] = {"name": name, "expr": expr}
        comment = self.text(description, f"{what}.description")
        if comment is not None:
            document["comment"] = comment
        return document

    def _window(self, entry: Mapping[str, Any], what: str) -> Any:
        """The window of a non-additive measure; ``None`` for none, ``False`` when refused."""
        nad = entry.get("nonAdditiveDimension")
        if nad is None:
            return None
        name = nad.get("name") if isinstance(nad, Mapping) else None
        target = self.fields.get(name.strip().lower()) if isinstance(name, str) else None
        if target is None or not target.is_time:
            self.refuse(
                "unsupported",
                f"{what}.nonAdditiveDimension names {name!r}, which is not a time dimension of "
                "this model: a metric view orders the window of a non-additive measure by one of "
                "its own dimensions.",
                "Name a dimension of type time declared in semantics.dimensions.",
            )
            return False
        choice = nad.get("windowChoice", "min")
        if choice not in ("min", "max"):
            self.refuse(
                "unsupported",
                f"{what}.nonAdditiveDimension.windowChoice is {choice!r}; it is min or max.",
                "Use min (the first value) or max (the last).",
            )
            return False
        semiadditive = "last" if choice == "max" else "first"
        return [{"order": target.name, "range": "current", "semiadditive": semiadditive}]

    def _simple(self, name: str, entry: Mapping[str, Any], what: str) -> None:
        ref = entry.get("measure")
        base = self.base.get(ref.strip().lower()) if isinstance(ref, str) else None
        if base is None:
            self.refuse(
                "unsupported",
                f"{what} ({name}) is a simple metric over measure {ref!r}, which this model does "
                "not declare (or which was refused).",
                "Name a measure declared in semantics.measures.",
            )
            return
        aggregate = _semantics.aggregate_call(str(ref).strip(), base.entry, base.expr)
        condition = entry.get("filter")
        if condition is None and name.lower() == str(ref).strip().lower():
            return  # the metric is its measure, which the view already holds
        if condition is not None:
            qualified = self.expression(condition, f"{what}.filter")
            if qualified is None:
                return
            aggregate = f"{aggregate} FILTER (WHERE {qualified})"
        if not self.claim(name, f"{what} ({name})"):
            return
        self.measure_docs.append(self._measure(name, aggregate, entry.get("description"), what))
        if base.window:
            self.measure_docs[-1]["window"] = copy.deepcopy(base.window)

    def _ratio(self, name: str, entry: Mapping[str, Any], what: str) -> None:
        if entry.get("filter") is not None:
            self.refuse(
                "unsupported",
                f"{what} ({name}) is a ratio metric with a filter, which a metric view cannot "
                "apply to MEASURE(numerator) / MEASURE(denominator).",
                "Put the filter on its numerator and denominator metrics instead.",
            )
            return
        inputs = [m for m in entry.get("inputMetrics") or [] if isinstance(m, str)]
        numerator = self.name(
            entry.get("numerator") or (inputs[0] if inputs else None), f"{what}.numerator"
        )
        denominator = self.name(
            entry.get("denominator") or (inputs[1] if len(inputs) > 1 else None),
            f"{what}.denominator",
        )
        if numerator and denominator and self.claim(name, f"{what} ({name})"):
            self.ratios.append(_Ratio(name, numerator, denominator, what, entry.get("description")))

    def _place_ratios(self) -> None:
        """Ratios after the measures they read: a metric view resolves ``MEASURE()`` against
        the measures defined before it."""
        placed = {document["name"].lower() for document in self.measure_docs}
        pending = list(self.ratios)
        while True:
            ready = [r for r in pending if {r.numerator.lower(), r.denominator.lower()} <= placed]
            if not ready:
                break
            for ratio in ready:
                expr = f"MEASURE({ratio.numerator}) / NULLIF(MEASURE({ratio.denominator}), 0)"
                self.measure_docs.append(
                    self._measure(ratio.name, expr, ratio.description, ratio.what)
                )
                placed.add(ratio.name.lower())
                pending.remove(ratio)
        for ratio in pending:
            missing = [m for m in (ratio.numerator, ratio.denominator) if m.lower() not in placed]
            self.refuse(
                "unsupported",
                f"{ratio.what} ({ratio.name}) divides {ratio.numerator!r} by "
                f"{ratio.denominator!r}, but {', '.join(repr(m) for m in missing)} is no measure "
                "or metric this view holds (undeclared, refused, or a ratio that reads this one).",
                "Name metrics or measures this model declares.",
            )

    # ── the view ──

    def comment(self) -> Optional[str]:
        description = self.text(self.semantics.get("description"), f"{self.where}.description")
        if description is None:
            description = self.text(
                self.table.exposure.get("description"), f"{self.table.where} description"
            )
        parts = [description] if description else []
        labels = self.semantics.get("labels")
        labels = labels if isinstance(labels, Mapping) else {}
        synonyms = self.text(labels.get("ai-synonyms"), f"{self.where}.labels.ai-synonyms")
        if synonyms:
            parts.append(f"Synonyms: {synonyms.rstrip('.')}.")
        instructions = self.text(
            labels.get("ai-instructions"), f"{self.where}.labels.ai-instructions"
        )
        if instructions:
            parts.append(f"Instructions: {instructions}")
        return "\n\n".join(parts) or None

    def view_name(self) -> Optional[str]:
        raw = self.semantics.get("name")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return f"{self.table.name}{DEFAULT_SUFFIX}"
        name = _plain_ident(raw)
        if name is None:
            self.refuse(
                "invalid",
                f"{self.where}.name is {raw!r}: it names the metric view, so it must be a plain "
                "identifier (letters, digits and underscores, not starting with a digit).",
                f"Rename it, or leave it out to name the view {self.table.name}{DEFAULT_SUFFIX}.",
            )
            return None
        return name.lower()

    def property_values(self, product_id: str) -> None:
        """Refuse a property value the CREATE statement could not hold as itself."""
        for value, what in (
            (product_id, "the contract's id"),
            (str(self.table.expose_id), f"{self.table.where} exposeId"),
        ):
            if not value or _CONTROL_RE.search(value) or "${" in value or "$$" in value:
                self.refuse(
                    "invalid",
                    f"{what} is {value!r}; the metric view records it as a property, which takes "
                    "no control character, '${' or '$$', and no empty value.",
                    "Rename it.",
                )


def _refused_call(text: str) -> Optional[str]:
    """The first call in ``text`` (literals blanked) that may not run with the owner's rights,
    as written, or ``None``."""
    for match in _CALL_RE.finditer(text):
        call = match.group(1)
        name = call.lower()
        if "`" in call or "." in call:
            return f"{call}() (a quoted or qualified function name)"
        if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_PREFIXES):
            return f"{call}()"
    return None


def _qualify(text: str, columns: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """``text`` with each name of a column in ``columns`` written ``source.<name>``, and the
    other names it reads.

    A name is a column reference unless it is in a string literal, follows a ``.`` (a field of
    a struct, or a name already qualified), is called (``name(``), prefixes a typed literal
    (``DATE'2026-01-01'``) or names a type (``CAST(x AS DATE)``).
    """
    out: List[str] = []
    bare: List[str] = []
    last = 0
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if token[0] in "'\"":
            continue
        before = text[: match.start()].rstrip()
        after = text[match.end() :].lstrip()
        typed = after.startswith(("'", '"')) or _AFTER_AS_RE.search(before)  # DATE'..', AS DATE
        if before.endswith(".") or after.startswith("(") or typed:
            continue
        name = token[1:-1].replace("``", "`") if token[0] == "`" else token
        if name.lower() in columns:
            out += [text[last : match.start()], f"{SOURCE}.{token}"]
            last = match.end()
        else:
            bare.append(name)
    out.append(text[last:])
    return "".join(out), bare


def compile_metric_views(
    contract: Mapping[str, Any],
    tables: Sequence[Any],
    privileges: Mapping[str, Mapping[str, Set[str]]],
    refusals: List[UnsupportedBindingError],
    warnings: List[str],
) -> MetricViews:
    """The metric view of each table whose expose declares ``semantics``; refusals appended.

    ``tables`` are the module's resolved tables (``databricks._Table``: ``where``,
    ``expose_id``, ``catalog``, ``schema``, ``name``, ``full_name``, ``columns``, ``exposure``,
    ``enforcement``); ``privileges`` is ``{table full name: {principal: privileges}}``.
    """
    product_id = str(contract.get("id") or contract.get("name") or "").strip()
    plan = MetricViews(
        product_id=product_id,
        schemas=tuple(sorted({f"{t.catalog}.{t.schema}" for t in tables})),
    )
    names = {t.full_name.lower(): t.where for t in tables}
    for table in tables:
        if not isinstance(table.exposure.get("semantics"), Mapping):
            continue
        view = _compile_view(table, product_id, privileges, refusals, warnings)
        if view is None:
            continue
        clash = names.get(view.full_name.lower())
        if clash is not None:
            refusals.append(
                _refusal(
                    "collision",
                    f"{table.where} semantics would create the metric view {view.full_name}, a "
                    f"name {clash} already takes.",
                    "Give the semantic model another name (semantics.name).",
                )
            )
            continue
        names[view.full_name.lower()] = view.where
        plan.views.append(view)
    return plan


def _compile_view(
    table: Any,
    product_id: str,
    privileges: Mapping[str, Mapping[str, Set[str]]],
    refusals: List[UnsupportedBindingError],
    warnings: List[str],
) -> Optional[MetricView]:
    compiler = _ViewCompiler(table, table.exposure["semantics"], refusals)
    view_name = compiler.view_name()
    compiler.compile_fields()
    compiler.compile_measures()
    comment = compiler.comment()
    compiler.property_values(product_id)
    if compiler.failed or view_name is None:
        return None
    if not compiler.dimension_docs and not compiler.measure_docs:
        warnings.append(
            f"{table.where} semantics declares no entity, dimension, measure or metric, so no "
            "metric view is created for it."
        )
        return None
    document: Dict[str, Any] = {"version": YAML_VERSION}
    if comment:
        document["comment"] = comment
    document["source"] = table.full_name
    if compiler.dimension_docs:
        document["dimensions"] = compiler.dimension_docs
    if compiler.measure_docs:
        document["measures"] = compiler.measure_docs
    definition = _dump(document)
    if "$$" in definition or "${" in definition:  # every text is checked; this is the backstop
        refusals.append(
            _refusal(
                "invalid",
                f"{table.where} semantics renders a metric view definition holding '$$' or '${{'.",
                "Remove '$$' and '${' from the semantics block and the column texts it reads.",
            )
        )
        return None
    readers = sorted(
        principal
        for principal, granted in privileges.get(table.full_name, {}).items()
        if set(granted) & _READ
    )
    return MetricView(
        full_name=f"{table.catalog}.{table.schema}.{view_name}",
        source=table.full_name,
        expose_id=str(table.expose_id),
        product_id=product_id,
        definition=definition,
        readers=tuple(readers),
        restricted=table.enforcement is not None,
        where=compiler.where,
    )


# ── live state ─────────────────────────────────────────────────────────


def _structure(document: Any) -> Any:
    """A metric view document without the text Unity Catalog keeps outside it."""
    if not isinstance(document, Mapping):
        return document
    out = {k: v for k, v in copy.deepcopy(dict(document)).items() if k != "comment"}
    for key in ("dimensions", "fields", "measures"):
        items = out.get(key)
        if isinstance(items, list):
            out[key] = [
                (
                    {k: v for k, v in item.items() if k not in _METADATA_KEYS}
                    if isinstance(item, Mapping)
                    else item
                )
                for item in items
            ]
    return out


@dataclass(frozen=True)
class _Live:
    """What holds a view's name: ``absent``, ``ours`` (``current`` when it matches) or
    ``foreign``, with why."""

    state: str
    info: Optional[Mapping[str, Any]] = None
    current: bool = False
    why: str = ""


def _stored(info: Mapping[str, Any]) -> Any:
    try:
        return yaml.safe_load(str(info.get("view_definition") or ""))
    except yaml.YAMLError:
        return None


def _classify(session: Session, view: MetricView) -> _Live:
    info = read_table(session.client, view.full_name)
    if info is None:
        return _Live("absent")
    kind = str(info.get("table_type") or "").upper()
    found = (info.get("properties") or {}).get(PRODUCT_PROPERTY)
    owner = str(info.get("owner") or "")
    if kind != METRIC_VIEW:
        why = f"it is a {kind or 'table'}, not a metric view"
    elif found != view.product_id:
        why = (
            f"it is the metric view of product {found!r}"
            if found
            else "it is a metric view this module did not create (no fluid.product_id)"
        )
    elif not owner_matches(owner, {session.identity}):
        why = (
            f"it is owned by {owner or 'an unknown owner'}, not the deploy identity "
            f"{session.identity}"
        )
    else:
        current = (info.get("properties") or {}).get(DIGEST_PROPERTY) == view.digest and _stored(
            info
        ) == _structure(yaml.safe_load(view.definition))
        return _Live("ours", info, current=current)
    return _Live("foreign", info, why=why)


def _session(
    client_factory: Optional[ClientFactory], say: Say, *, required: bool
) -> Optional[Session]:
    """A client, the warehouse runner and the run's identity; ``None`` (or raised, when
    ``required``) without one."""
    try:
        # The enforcement module's, looked up per call: the one client a test patches.
        client = (client_factory or _enforce.default_client)()
    except UnsupportedBindingError:
        if required:
            raise
        return None
    except ImportError:
        if required:
            raise MetricViewError(
                "databricks-sdk is not installed, and the contract's metric views are created "
                "through it.",
                (f"Install the extra: {DATABRICKS_EXTRA_INSTALL}",),
            ) from None
        return None  # nothing declared now, and no SDK an earlier view could have come from
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        if required:
            raise MetricViewError(
                f"could not create the Databricks client for the metric views: "
                f"{type(exc).__name__}: {scrub(exc)}"
            ) from exc
        say(
            "  note: metric views an earlier apply created were not checked for removal (no "
            "Databricks client); any left in place are still readable by their grantees."
        )
        return None
    warehouse = str(os.environ.get(WAREHOUSE_ENV) or "").strip()
    if not warehouse:
        if not required:
            return None
        raise MetricViewError(
            f"{WAREHOUSE_ENV} is not set, and the metric views are created on that warehouse.",
            (f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace.",),
        )
    return Session(client, SqlRunner(client, warehouse))


def _permissions(session: Session, full_name: str) -> Dict[str, Set[str]]:
    """``{principal: privileges}`` granted directly on a table or view."""
    path = f"/api/2.1/unity-catalog/permissions/table/{quote(full_name, safe='.')}"
    return {
        str(entry["principal"]): set(_privileges(entry))
        for entry in _list_pages(session.client, path)
        if entry.get("principal")
    }


def _exempt(session: Session, info: Optional[Mapping[str, Any]]) -> Set[str]:
    """Who a grant change never touches: the view's owner and this run's identity."""
    return {p.lower() for p in (str((info or {}).get("owner") or ""), session.identity) if p}


def _grant_steps(
    session: Session, view: MetricView, live: Optional[Mapping[str, Any]]
) -> List[Step]:
    """``[(statement, what)]`` that make ``SELECT`` on the view exactly its readers."""
    name = quote_table(view.full_name)
    exempt = _exempt(session, live)
    held = _permissions(session, view.full_name) if live is not None else {}
    holding = {p.lower() for p, privileges in held.items() if "SELECT" in privileges}
    wanted = {p.lower(): p for p in view.readers}
    steps = [
        (f"GRANT SELECT ON TABLE {name} TO {quote_principal(p)}", f"grant SELECT to {p}")
        for lower, p in sorted(wanted.items())
        if lower not in holding and lower not in exempt
    ]
    steps += [
        (f"REVOKE SELECT ON TABLE {name} FROM {quote_principal(p)}", f"revoke SELECT from {p}")
        for p, privileges in sorted(held.items())
        if "SELECT" in privileges and p.lower() not in wanted and p.lower() not in exempt
    ]
    return steps


def _definition_steps(view: MetricView, live: _Live) -> List[Step]:
    if live.state == "absent":
        return [(view.create, f"create metric view {view.full_name}")]
    if live.current:
        return []
    return [
        (view.update[0], f"update metric view {view.full_name}"),
        (view.update[1], "record its definition digest"),
    ]


def _say_statement(say: Say, statement: str, indent: str = "      ") -> None:
    for line in statement.splitlines():
        say(f"{indent}{line}")


# ── the hooks ──────────────────────────────────────────────────────────


def preview(plan: MetricViews, say: Say, client_factory: Optional[ClientFactory] = None) -> None:
    """Print what an apply would run for the metric views, in order; run none."""
    if not plan.views and not plan.schemas:
        return
    session = _session(client_factory, say, required=False)
    if plan.views:
        say("\n  Unity Catalog metric views (run on the SQL warehouse after tofu apply):")
    for view in plan.views:
        _preview_view(session, view, say)
    if session is not None:
        _drop_stale(session, plan, say, dry_run=True)
    if plan.views:
        say("    dry-run: none of these statements ran.")


def _preview_view(session: Optional[Session], view: MetricView, say: Say) -> None:
    try:
        live = _classify(session, view) if session is not None else _Live("absent")
    except Exception as exc:  # noqa: BLE001 — a dry run reports, it does not stop
        say(f"  {view.full_name}: could not be read ({type(exc).__name__}: {scrub(exc)})")
        live = _Live("absent")
    if live.state == "foreign":
        say(f"  {view.full_name}: REFUSED before the apply: {live.why}.")
        return
    say(f"  {view.full_name}" + ("" if session else " (live state not read)"))
    if live.state == "absent":
        say(f"    after apply: create the metric view over {view.source}")
    elif live.current:
        say("    unchanged")
    else:
        say("    after apply: update the definition (ALTER VIEW keeps its grants)")
    for statement, _what in _definition_steps(view, live):
        _say_statement(say, statement)
    try:
        assert session is not None
        steps = _grant_steps(session, view, live.info if live.state == "ours" else None)
    except Exception:  # noqa: BLE001 — unread: say what the contract asks
        steps = [("", f"grant SELECT to {p}") for p in view.readers]
    for _statement, what in steps:
        say(f"    after apply: {what}")


def pre_apply(plan: MetricViews, say: Say, client_factory: Optional[ClientFactory] = None) -> None:
    """Refuse, before ``tofu apply``, a view whose name an object this module did not create
    holds: creating ours would fail, and replacing it would take it over."""
    if not plan.views:
        return
    session = _session(client_factory, say, required=True)
    assert session is not None
    conflicts = []
    for view in plan.views:
        try:
            live = _classify(session, view)
        except Exception as exc:  # noqa: BLE001 — every failure here is a typed refusal
            raise MetricViewError(
                f"could not read {view.full_name} from Unity Catalog: {type(exc).__name__}: "
                f"{scrub(exc)}",
                ("Check DATABRICKS_HOST and the credential, then re-run fluid apply.",),
            ) from exc
        if live.state == "foreign":
            conflicts.append(f"{view.full_name} ({view.where}): {live.why}")
    if conflicts:
        raise MetricViewError(
            "the metric view name is taken, so nothing was applied: " + "; ".join(conflicts) + ".",
            (
                "Give the semantic model another name (semantics.name), or drop or rename the "
                "existing object in Databricks. A view of this product that another identity "
                "owns can be handed back with ALTER VIEW <name> OWNER TO <the deploy identity>.",
            ),
        )


def post_apply(plan: MetricViews, say: Say, client_factory: Optional[ClientFactory] = None) -> None:
    """Create or update every view, make its grants the contract's, drop the views this
    product no longer declares; raise :class:`MetricViewError` naming what failed."""
    if not plan.views and not plan.schemas:
        return
    session = _session(client_factory, say, required=bool(plan.views))
    if session is None:
        return
    failed = [problem for view in plan.views if (problem := _converge(session, view, say))]
    _drop_stale(session, plan, say)
    if failed:
        raise MetricViewError(
            "the apply succeeded, but these metric views are not as the contract says: "
            + " | ".join(failed),
            (
                "Fix the cause (this identity needs CREATE TABLE and USE SCHEMA on the schema, "
                "SELECT on the table and CAN USE on the warehouse), then re-run fluid apply.",
            ),
        )


def _converge(session: Session, view: MetricView, say: Say) -> Optional[str]:
    """Make one view the contract's; what failed, or ``None``."""
    try:
        live = _classify(session, view)
        if live.state == "foreign":
            return f"{view.full_name}: {live.why}"
        steps = _definition_steps(view, live)
        steps += _grant_steps(session, view, live.info if live.state == "ours" else None)
    except Exception as exc:  # noqa: BLE001
        return f"{view.full_name}: it could not be read ({type(exc).__name__}: {scrub(exc)})"
    if steps:
        say(f"\n  {view.full_name}: metric view")
    for statement, what in steps:
        try:
            session.runner.run(statement)
        except StatementFailed as exc:
            say(f"    FAILED: {what}: {exc}")
            return f"{view.full_name}: {what} failed: {exc}"
        say(f"    done: {what}")
    return None


def _schema_tables(session: Session, where: str, say: Say) -> List[Mapping[str, Any]]:
    """The tables and views of one schema; none (with a note) when they cannot be listed."""
    catalog, schema = where.split(".", 1)
    try:
        query = {"catalog_name": catalog, "schema_name": schema}
        return _list_named(session.client, "/api/2.1/unity-catalog/tables", query, "tables")
    except Exception as exc:  # noqa: BLE001 — unknown contents: drop nothing
        if not _enforce.is_not_found(exc):  # a schema not created yet holds no view
            say(
                f"  note: metric views in {where} were not checked for removal: its tables "
                f"could not be listed ({type(exc).__name__}: {scrub(exc)})."
            )
        return []


def _stale_views(session: Session, plan: MetricViews, say: Say) -> Iterator[Mapping[str, Any]]:
    """This product's metric views in its schemas that the contract no longer declares."""
    expected = {view.full_name.lower() for view in plan.views}
    for where in plan.schemas:
        for row in _schema_tables(session, where, say):
            full = str(row.get("full_name") or f"{where}.{row.get('name')}")
            listed = str(row.get("table_type") or "").upper()
            if full.lower() in expected or listed not in ("", METRIC_VIEW):
                continue
            try:
                info = row if listed and "properties" in row else read_table(session.client, full)
            except Exception as exc:  # noqa: BLE001 — unread: leave it
                say(
                    f"  note: metric view {full} was not checked for removal ({type(exc).__name__})"
                )
                continue
            info = info or {}
            ours = (info.get("properties") or {}).get(PRODUCT_PROPERTY) == plan.product_id
            if ours and str(info.get("table_type") or "").upper() == METRIC_VIEW:
                yield {**info, "full_name": full}


def _drop_stale(session: Session, plan: MetricViews, say: Say, *, dry_run: bool = False) -> None:
    """Drop this product's metric views in its schemas that the contract no longer declares."""
    if not plan.product_id:
        return
    for info in _stale_views(session, plan, say):
        full, owner = str(info["full_name"]), str(info.get("owner") or "")
        try:
            mine = owner_matches(owner, {session.identity})
        except Exception:  # noqa: BLE001 — unknown identity: leave it
            mine = False
        if not mine:
            say(
                f"  left metric view {full}: owned by {owner or 'unknown'}, not the deploy identity"
            )
        elif dry_run:
            say(f"  would drop metric view {full}: the contract no longer declares it")
        else:
            _run_noted(
                session,
                f"DROP VIEW IF EXISTS {quote_table(full)}",
                say,
                done=f"  dropped metric view {full}: the contract no longer declares it",
                failed=f"  note: metric view {full} was not dropped",
            )


def _run_noted(session: Session, statement: str, say: Say, *, done: str, failed: str) -> None:
    try:
        session.runner.run(statement)
    except StatementFailed as exc:
        say(f"{failed}: {exc}")
    else:
        say(done)


def lock_restricted(
    plan: MetricViews, say: Say, client_factory: Optional[ClientFactory] = None
) -> None:
    """After column masks or a row filter failed to bind: revoke ``SELECT`` (and ``ALL
    PRIVILEGES``) on each existing metric view over a restricted table from every grantee but
    its owner and this run's identity. Best effort; the run fails with the binding's error."""
    views = [view for view in plan.views if view.restricted]
    if not views:
        return
    try:
        session = _session(client_factory, say, required=True)
    except UnsupportedBindingError as exc:
        say(f"    fail-safe: the metric views over restricted tables were not closed: {exc}")
        return
    assert session is not None
    for view in views:
        _close(session, view, say)


def _close(session: Session, view: MetricView, say: Say) -> None:
    """Revoke every read on one view but its owner's and this run's."""
    try:
        info = read_table(session.client, view.full_name)
        if info is None or str(info.get("table_type") or "").upper() != METRIC_VIEW:
            return
        exempt = _exempt(session, info)
        held = _permissions(session, view.full_name)
    except Exception as exc:  # noqa: BLE001
        say(
            f"    fail-safe: NOT CLOSED: the grants on metric view {view.full_name} could not "
            f"be read ({type(exc).__name__}: {scrub(exc)}); revoke SELECT on it by hand."
        )
        return
    name = quote_table(view.full_name)
    for principal, privileges in sorted(held.items()):
        if principal.lower() in exempt:
            continue
        statements = []
        if "SELECT" in privileges:
            statements.append(f"REVOKE SELECT ON TABLE {name} FROM {quote_principal(principal)}")
        if "ALL_PRIVILEGES" in privileges:
            statements.append(revoke_all_statement(view.full_name, principal))
        for statement in statements:
            _run_noted(
                session,
                statement,
                say,
                done=f"    fail-safe: revoked: {statement}",
                failed=f"    fail-safe: NOT REVOKED: {statement} failed",
            )


__all__ = [
    "DEFAULT_SUFFIX",
    "DIGEST_PROPERTY",
    "EXPOSE_PROPERTY",
    "METRIC_VIEW",
    "MetricView",
    "MetricViewError",
    "MetricViews",
    "PRODUCT_PROPERTY",
    "compile_metric_views",
    "lock_restricted",
    "post_apply",
    "pre_apply",
    "preview",
]
