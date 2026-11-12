# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A data product's Databricks AI/BI Genie space: compiled from its contract, kept in step by
``fluid apply``.

A contract opts in with ``extensions.databricksGenie`` (``{}``, or ``{title: ...}``). The product
gets one space holding its Databricks tables and any metric view over them, where people ask
the product's own example questions in natural language.

What the space holds (``serialized_space`` version 2, the body ``POST /api/2.0/genie/spaces``
takes; field rules from databricks/databricks-agent-skills ``databricks-genie-agents``
``references/create-genie-agent.md`` and the Genie API reference, checked against a live
workspace on 2026-11-12):

* ``config.sample_questions``: each expose's ``semantics.labels.ai-example-questions``, split on
  ``|``.
* ``data_sources.tables``: one per Databricks expose, with its description and, per column, the
  description, business definition, allowed values (an ``enum`` validation rule), range, example
  and synonyms (``businessName``, ``labels.synonyms``). Genie learns values from the contract,
  not by sampling the data.
* every metric view whose ``source`` is one of those tables, read from Unity Catalog at apply
  time, so one is used as soon as it exists, whoever made it. It goes among
  ``data_sources.tables``: the service keeps it there, and moves one sent under
  ``data_sources.metric_views`` there.
* ``instructions.text_instructions`` (Genie allows one): ``ai-instructions``, ``ai-synonyms``, the
  grain, the key, the time dimension and snapshot rule, each metric as the product defines it,
  the products it is built from (``consumes``), the expose's ``agentPolicy`` (allowed and denied
  use cases, purpose limitation, ``canStore``, ``canReason``), its classification, and how access
  works.
* ``instructions.sql_snippets.measures``: each simple metric as SQL, in a one-table space (an
  expression over two tables' columns would need qualifying, which this module does not do). The
  SQL is rendered as the governed MCP query path renders it
  (``output_ports/mcp/query_compiler.py``), so both answer the same number for one contract.

A space never widens access. Genie runs each query as the person asking, under that person's own
Unity Catalog grants, so column masks and row filters apply ("Genie agents: data access",
Databricks docs). What could still show data past those grants is not done: prompt matching
(format assistance and entity matching) samples column values with the author's permissions and
shows them to everyone in the space, so it is never turned on (a space the API creates has it
off, and ``agentPolicy.canStore: false`` forbids the cache besides); and the space is shared,
with ``CAN_RUN``, only with the principals that hold ``SELECT`` on one of its tables. A space
for a product whose ``agentPolicy`` rules out question answering is refused.

Identity, without state. The databricks/databricks provider has no Genie space resource (v1.142.0
ships only the Private Preview ``databricks_genie_agent``, a typed definition of another shape),
so the space lives outside OpenTofu. Its description ends with ``[fluid-genie:<product id>]``:
each apply lists the spaces, adopts the one carrying the marker, updates it (``PATCH`` with the
space's ``etag``) only when what the contract asks differs from what it holds, and trashes it
once the contract drops the block. No local record, so a fresh CI runner finds the same space;
two spaces carrying one product's marker stop the apply. The service keeps the space as sent,
except that it leaves out a ``false`` flag, so the module never sends one and a second apply
compares equal.

The client is the ``databricks.sdk.WorkspaceClient`` the masks and row filters use
(``databricks_enforce.default_client``), through ``api_client.do``. The paths are the SDK's own
(databricks-sdk-py ``service/dashboards.py`` and ``service/iam.py``). No message carries a
credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import quote

from ...providers._sql_safety import validate_ident, validate_sql_expression_allowlist
from ..base import UnsupportedBindingError
from . import databricks_enforce as _enforce

#: The contract's opt-in: ``extensions.databricksGenie``.
EXTENSION_KEY = "databricksGenie"
_BLOCK_KEYS = frozenset({"title"})

SPACES_PATH = "/api/2.0/genie/spaces"
PERMISSIONS_PATH = "/api/2.0/permissions/genie/{}"
TABLES_PATH = "/api/2.1/unity-catalog/tables"
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
DATABRICKS_EXTRA_INSTALL = "pip install 'data-product-forge[databricks]'"

#: "Up to 50 tables, views or metric views" per space ("Set up a Genie agent", Databricks docs).
MAX_TABLES = 50
#: Each string in ``serialized_space`` holds at most 25,000 characters (Genie API reference).
MAX_STRING = 25_000
#: What a reader holds on the space: ask questions. Genie's levels (``GET
#: /api/2.0/permissions/genie/{id}/permissionLevels``) are CAN_READ, CAN_RUN, CAN_EDIT, CAN_MANAGE.
READER_LEVEL = "CAN_RUN"
#: Direct levels an apply never takes away: whoever edits or manages the space keeps doing so.
_KEPT_LEVELS = frozenset({"CAN_EDIT", "CAN_MANAGE"})
_LEVEL_RANK = {"CAN_READ": 1, "CAN_RUN": 2, "CAN_EDIT": 3, "CAN_MANAGE": 4}
_PAGE_SIZE = 100
#: The use case a Genie space is: answering questions about the data.
_GENIE_USE_CASE = "qa"
_NOT_SENSITIVE = frozenset({"", "none", "public", "internal"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

Say = Callable[[str], None]
ClientFactory = Callable[[], Any]


class GenieError(UnsupportedBindingError):
    """The Genie space could not be put in step with the contract (``databricks-genie-failed``)."""

    def __init__(self, message: str, remediation: Sequence[str] = ()):
        super().__init__("databricks-genie-failed", message, tuple(remediation))


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-genie-{kind}", message, tuple(remediation))


def marker(product_id: str) -> str:
    """The token a space's description carries to say which product it belongs to."""
    return f"[fluid-genie:{product_id}]"


@dataclass(frozen=True)
class Space:
    """What the contract asks the product's Genie space to be."""

    product_id: str
    title: str
    description: str
    warehouse_id: str
    #: The full names of its tables, sorted.
    tables: Tuple[str, ...]
    #: The principals given ``CAN_RUN``: whoever holds ``SELECT`` on one of its tables.
    readers: Tuple[str, ...]
    #: ``serialized_space``, without the metric views (those are read at apply time).
    serialized: Dict[str, Any]

    @property
    def marker(self) -> str:
        return marker(self.product_id)


@dataclass
class Compiled:
    """The space a contract asks for (``None`` without the block), and every refusal."""

    space: Optional[Space] = None
    refusals: List[UnsupportedBindingError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: The contract's id: what a space it asked for earlier is found by, block or no block.
    product_id: str = ""


# ---------------------------------------------------------------------- compile (pure)


def requested(contract: Mapping[str, Any]) -> bool:
    """Whether the contract carries ``extensions.databricksGenie`` at all."""
    extensions = contract.get("extensions")
    return isinstance(extensions, Mapping) and EXTENSION_KEY in extensions


_WHERE = f"extensions.{EXTENSION_KEY}"


def compile_space(
    contract: Mapping[str, Any],
    tables: Sequence[Any],
    grants: Iterable[Tuple[str, str, str, Sequence[str]]],
    env: Mapping[str, str],
) -> Compiled:
    """The space for ``contract``, from the Databricks tables and grants the emitter resolved.

    ``tables`` are the emitter's resolved exposes (``full_name``, ``name``, ``columns``,
    ``exposure``, ``where``); ``grants`` its ``(securable type, full name, principal,
    privileges)``. Credential-free and offline: ``fluid validate`` runs it.
    """
    out = Compiled(product_id=_text(contract.get("id")))
    if not requested(contract):
        return out
    title = _block_title(contract, out)
    out.refusals += _table_refusals(tables)
    if out.refusals:
        return out
    ordered = sorted(tables, key=lambda t: t.full_name)
    serialized = _serialized_space(contract, out.product_id, ordered, out.warnings)
    too_long = next((text for text in _strings_in(serialized) if len(text) > MAX_STRING), None)
    if too_long is not None:
        out.refusals.append(
            _refusal(
                "limit",
                f"{_WHERE}: the space would hold a text of {len(too_long)} characters, and Genie "
                f"holds at most {MAX_STRING:,} in one: {too_long[:80]!r}...",
                "Shorten that text in the contract.",
            )
        )
        return out
    out.space = Space(
        product_id=out.product_id,
        title=title,
        description=_description(contract, out.product_id),
        warehouse_id=str(env.get(WAREHOUSE_ENV) or "").strip(),
        tables=tuple(t.full_name for t in ordered),
        readers=_readers(tables, grants),
        serialized=serialized,
    )
    return out


def _block_title(contract: Mapping[str, Any], out: Compiled) -> str:
    """The space's title from the block (or the product's name); what is wrong with the block
    goes into ``out.refusals``."""
    block = contract["extensions"][EXTENSION_KEY]
    if block is None:  # ``databricksGenie:`` with nothing under it
        block = {}
    if not isinstance(block, Mapping):
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} is {type(block).__name__}, and it is a mapping: {{}} for a space with "
                "the product's name, or {title: ...}.",
                f"Write {_WHERE}: {{}} to create the space, or remove {_WHERE} for none.",
            )
        )
        return ""
    unknown = sorted(str(key) for key in block if key not in _BLOCK_KEYS)
    if unknown:
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} has {', '.join(repr(k) for k in unknown)}, which it does not read "
                f"(it reads: {', '.join(sorted(_BLOCK_KEYS))}).",
                f"Remove {', '.join(unknown)} from {_WHERE}.",
            )
        )
    if not out.product_id:
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} needs the contract's id: the space's description carries it, which is "
                "how the next apply finds the space again.",
                "Give the contract an id.",
            )
        )
    title = block.get("title", contract.get("name") or out.product_id)
    if isinstance(title, str) and title.strip():
        return title.strip()
    out.refusals.append(
        _refusal(
            "invalid",
            f"{_WHERE}.title is {title!r}, and the space needs a title.",
            f"Set {_WHERE}.title to a name, or leave it out to use the product's name.",
        )
    )
    return ""


def _table_refusals(tables: Sequence[Any]) -> List[UnsupportedBindingError]:
    """A space needs one to :data:`MAX_TABLES` tables, each one an ``agentPolicy`` allows."""
    if not tables:
        return [
            _refusal(
                "no-table",
                f"{_WHERE} asks for a Genie space, and the contract has no Databricks table "
                "expose for it to hold.",
                "Bind an expose to databricks (binding.platform: databricks), or remove "
                f"{_WHERE}.",
            )
        ]
    out: List[UnsupportedBindingError] = []
    if len(tables) > MAX_TABLES:
        out.append(
            _refusal(
                "limit",
                f"{_WHERE}: a Genie space holds at most {MAX_TABLES} tables, and the contract "
                f"has {len(tables)} Databricks exposes.",
                "Split the product, or remove the block.",
            )
        )
    for table in tables:
        out += _policy_refusals(table)
    return out


def _readers(
    tables: Sequence[Any], grants: Iterable[Tuple[str, str, str, Sequence[str]]]
) -> Tuple[str, ...]:
    """Every principal that holds ``SELECT`` on one of the space's tables, sorted."""
    names = {t.full_name for t in tables}
    return tuple(
        sorted(
            {
                principal
                for kind, full_name, principal, privileges in grants
                if kind == "table" and full_name in names and "SELECT" in privileges
            }
        )
    )


def _policy_refusals(table: Any) -> List[UnsupportedBindingError]:
    """What in the expose's ``agentPolicy`` a question-answering space would break."""
    policy = _agent_policy(table.exposure)
    if not policy:
        return []
    out: List[UnsupportedBindingError] = []
    where = f"{table.where} policy.agentPolicy"
    allowed = _string_list(policy.get("allowedUseCases"))
    denied = _string_list(policy.get("deniedUseCases"))
    if isinstance(policy.get("allowedModels"), list) and not policy["allowedModels"]:
        out.append(
            _refusal(
                "policy",
                f"{where}.allowedModels is [], which means no AI access, and a Genie space answers "
                "questions with a model.",
                f"Remove extensions.{EXTENSION_KEY}, or allow AI access in agentPolicy.",
            )
        )
    if _GENIE_USE_CASE in denied or (allowed and _GENIE_USE_CASE not in allowed):
        out.append(
            _refusal(
                "policy",
                f"{where} does not allow the use case {_GENIE_USE_CASE!r} (question answering), "
                "which is what a Genie space is.",
                f"Remove extensions.{EXTENSION_KEY}, or allow {_GENIE_USE_CASE!r} in "
                "agentPolicy.allowedUseCases.",
            )
        )
    return out


def _description(contract: Mapping[str, Any], product_id: str) -> str:
    about = _text(contract.get("description"))
    footer = (
        f"Managed by fluid apply from the data product contract {product_id}: changes made "
        f"here are replaced on the next apply. {marker(product_id)}"
    )
    return f"{about}\n\n{footer}" if about else footer


def _serialized_space(
    contract: Mapping[str, Any], product_id: str, tables: Sequence[Any], warnings: List[str]
) -> Dict[str, Any]:
    questions: Dict[str, str] = {}
    for table in tables:
        for question in _split(
            _labels(_semantics(table.exposure).get("labels")).get("ai-example-questions"), "|"
        ):
            questions.setdefault(question.casefold(), question)
    if not questions:
        warnings.append(
            f"extensions.{EXTENSION_KEY}: no expose has semantics.labels.ai-example-questions, "
            "so the Genie space shows no sample questions."
        )
    space: Dict[str, Any] = {"version": 2}
    if questions:
        space["config"] = {
            "sample_questions": _by_id(
                {"id": _id(product_id, "question", q), "question": [q]} for q in questions.values()
            )
        }
    space["data_sources"] = {"tables": [_table_source(t) for t in tables]}
    instructions: Dict[str, Any] = {
        "text_instructions": [
            {
                "id": _id(product_id, "instructions"),
                "content": [line + "\n" for line in _instruction_lines(contract, tables, warnings)],
            }
        ]
    }
    if len(tables) == 1:
        measures = _measure_snippets(product_id, tables[0])
        if measures:
            instructions["sql_snippets"] = {"measures": measures}
    space["instructions"] = instructions
    return space


def _table_source(table: Any) -> Dict[str, Any]:
    exposure = table.exposure
    source: Dict[str, Any] = {"identifier": table.full_name}
    about = _sentence(
        _text(exposure.get("description")) or _text(_semantics(exposure).get("description"))
    )
    if about:
        source["description"] = [about]
    configs = _column_configs(table)
    if configs:
        source["column_configs"] = configs
    return source


def _column_configs(table: Any) -> List[Dict[str, Any]]:
    """What the contract says of each column the table has, sorted by name (Genie's rule)."""
    by_name = {
        _text(column.get("name")).lower(): column
        for column in (table.exposure.get("contract") or {}).get("schema") or ()
        if isinstance(column, Mapping)
    }
    configs = []
    for emitted in table.columns:
        name = str(emitted.get("name") or "")
        column = by_name.get(name)
        if not name or column is None:
            continue
        config: Dict[str, Any] = {"column_name": name}
        about = _column_description(column)
        if about:
            config["description"] = [about]
        synonyms = _column_synonyms(column, name)
        if synonyms:
            config["synonyms"] = synonyms
        if len(config) > 1:
            configs.append(config)
    return sorted(configs, key=lambda c: c["column_name"])


def _column_description(column: Mapping[str, Any]) -> str:
    parts = [_sentence(_text(column.get("description")))]
    definition = _text(column.get("businessDefinition"))
    if definition:
        parts.append(_sentence(f"Business definition: {definition}"))
    parts += _rule_parts(column.get("validationRules"))
    sensitivity = _text(column.get("sensitivity"))
    if sensitivity.lower() in _NOT_SENSITIVE:
        example = _text(_labels(column.get("labels")).get("example"))
        if example:
            parts.append(f"Example: {example}.")
    else:
        parts.append(f"Sensitivity: {sensitivity}.")
    return " ".join(part for part in parts if part)


def _rule_parts(rules: Any) -> List[str]:
    """The values an ``enum`` rule allows and the bounds of a ``range`` one, as sentences."""
    parts = []
    for rule in rules or ():
        if not isinstance(rule, Mapping):
            continue
        kind = _text(rule.get("type")).lower()
        constraint = rule.get("constraint")
        values = _split(constraint, "|", ",") if kind == "enum" else []
        if values:
            parts.append(f"Allowed values: {', '.join(values)}.")
        elif kind == "range" and _text(constraint):
            parts.append(f"Range: {_text(constraint)}.")
    return parts


def _column_synonyms(column: Mapping[str, Any], name: str) -> List[str]:
    candidates = [_text(column.get("businessName"))]
    candidates += _split(_labels(column.get("labels")).get("synonyms"), ";", ",", "|")
    seen = {_folded(name)}
    out: List[str] = []
    for candidate in candidates:
        if candidate and _folded(candidate) not in seen:
            seen.add(_folded(candidate))
            out.append(candidate)
    return out


def _instruction_lines(
    contract: Mapping[str, Any], tables: Sequence[Any], warnings: List[str]
) -> List[str]:
    lines: List[str] = []
    for table in tables:
        section = _table_section(contract, table, warnings)
        if section and len(tables) > 1:
            title = _text(table.exposure.get("title")) or _text(table.exposure.get("exposeId"))
            lines.append(f"About {table.full_name}" + (f" ({title}):" if title else ":"))
            lines += [f"  {line}" for line in section]
        else:
            lines += section
    lines += _lineage_lines(contract)
    lines.append(
        "How access works: Genie runs each query as the person asking, under that person's own "
        "Unity Catalog grants, so column masks and row filters apply. This space grants no "
        "access to data: a table the person cannot SELECT returns no rows."
    )
    return lines


def _table_section(contract: Mapping[str, Any], table: Any, warnings: List[str]) -> List[str]:
    """What the space is told about one table: its semantics, metrics and ``agentPolicy``."""
    exposure = table.exposure
    semantics = _semantics(exposure)
    labels = _labels(semantics.get("labels"))
    section: List[str] = []
    if labels.get("ai-instructions"):
        section.append(_text(labels["ai-instructions"]))
    synonyms = _split(labels.get("ai-synonyms"), ";", "|")
    if synonyms:
        section.append(f"Also called: {'; '.join(synonyms)}.")
    grain = (
        labels.get("grain")
        or _labels(exposure.get("labels")).get("grain")
        or _labels(contract.get("labels")).get("grain")
    )
    if grain:
        section.append(_sentence(f"Grain: {_text(grain)}"))
    keys = [
        _text(entity.get("name"))
        for entity in semantics.get("entities") or ()
        if isinstance(entity, Mapping) and _text(entity.get("type")).lower() == "primary"
    ]
    if any(keys):
        section.append(f"Primary key: {', '.join(k for k in keys if k)}.")
    time = _text(semantics.get("defaultAggTimeDimension"))
    if time:
        section.append(f"Default time dimension: {time}.")
    return (
        section
        + _snapshot_lines(semantics)
        + _metric_lines(table, semantics, warnings)
        + _policy_lines(exposure)
    )


def _snapshot_lines(semantics: Mapping[str, Any]) -> List[str]:
    """The non-additive rule of the measures (``nonAdditiveDimension``), once per dimension."""
    rules: Dict[Tuple[str, str], List[str]] = {}
    for measure in semantics.get("measures") or ():
        if not isinstance(measure, Mapping):
            continue
        rule = measure.get("nonAdditiveDimension")
        if not isinstance(rule, Mapping) or not _text(rule.get("name")):
            continue
        choice = _text(rule.get("windowChoice")).lower() or "max"
        rules.setdefault((_text(rule["name"]), choice), []).append(_text(measure.get("name")))
    out = []
    for (dimension, choice), measures in sorted(rules.items()):
        which = "earliest" if choice == "min" else "latest"
        out.append(
            f"{', '.join(m for m in measures if m)} hold a state as of {dimension}: aggregate "
            f"them at the {which} {dimension} ({choice}) unless the question asks about history."
        )
    return out


def _exposed_metrics(semantics: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """``metrics[]``, then each measure with ``createMetric: true`` (a simple metric of itself,
    as MetricFlow reads it)."""
    metrics = [m for m in semantics.get("metrics") or () if isinstance(m, Mapping)]
    named = {_text(m.get("name")) for m in metrics}
    for measure in semantics.get("measures") or ():
        if (
            isinstance(measure, Mapping)
            and measure.get("createMetric") is True
            and _text(measure.get("name")) not in named
        ):
            metrics.append(
                {
                    "name": measure.get("name"),
                    "description": measure.get("description"),
                    "type": "simple",
                    "measure": measure.get("name"),
                }
            )
    return metrics


def _metric_lines(table: Any, semantics: Mapping[str, Any], warnings: List[str]) -> List[str]:
    out = []
    for metric in _exposed_metrics(semantics):
        name = _text(metric.get("name"))
        if not name:
            continue
        line = f"- {name}"
        about = _text(metric.get("description"))
        if about:
            line += f": {_sentence(about)}"
        sql, problem = metric_sql(metric, semantics)
        if sql:
            line += f" SQL: {sql}"
        elif problem:
            warnings.append(f"{table.where} metric {name!r}: {problem}")
        out.append(line)
    return ["Metrics, as the product defines them:"] + out if out else []


def _policy_lines(exposure: Mapping[str, Any]) -> List[str]:
    policy = _agent_policy(exposure)
    out = []
    allowed = _string_list(policy.get("allowedUseCases"))
    denied = _string_list(policy.get("deniedUseCases"))
    if allowed:
        out.append(f"Allowed uses of this data: {', '.join(allowed)}.")
    if denied:
        out.append(
            f"Not allowed: {', '.join(denied)}. Decline a request for a use that is not allowed."
        )
    purpose = _text(policy.get("purposeLimitation"))
    if purpose:
        out.append(_sentence(f"Purpose limitation: {purpose}"))
    if policy.get("canStore") is False:
        out.append("AI systems may not cache or store this data (agentPolicy canStore: false).")
    if policy.get("canReason") is False:
        out.append(
            "Answer by direct retrieval, without multi-step reasoning over this data "
            "(agentPolicy canReason: false)."
        )
    classification = _text((exposure.get("policy") or {}).get("classification"))
    if classification:
        out.append(f"Classification: {classification}.")
    return out


def _lineage_lines(contract: Mapping[str, Any]) -> List[str]:
    out = []
    for ref in contract.get("consumes") or ():
        if not isinstance(ref, Mapping):
            continue
        product = _text(ref.get("productId") or ref.get("contractId"))
        if not product:
            continue
        expose = _text(ref.get("exposeId"))
        line = f"- {product}" + (f" ({expose})" if expose else "")
        purpose = _text(ref.get("purpose"))
        out.append(line + (f": {_sentence(purpose)}" if purpose else ""))
    if not out:
        return []
    return ["Built from these data products, where a value's detail is checked or disputed:"] + out


# ---------------------------------------------------------------------- metric SQL

#: The aggregates of ``semantics.measures[].agg``, rendered exactly as the governed MCP query path
#: renders them (``output_ports/mcp/query_compiler.py::_render_measure_expression``; a test pins the
#: two together). Not imported: that package loads the whole MCP output port.
_AGGREGATES = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "count_distinct": "COUNT",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "percentile": "PERCENTILE_CONT",
}
DEFAULT_PERCENTILE = 0.5


def aggregate_sql(name: str, measure: Mapping[str, Any]) -> str:
    """``AGG(expr)`` for one measure; raises ``ValueError`` for one it cannot render safely."""
    agg = measure.get("agg")
    if not isinstance(agg, str) or agg not in _AGGREGATES:
        raise ValueError(f"its measure {name!r} has the aggregate {agg!r}")
    expr = measure.get("expr") or name
    if not isinstance(expr, str):
        raise ValueError(f"its measure {name!r} has a non-string expr")
    expr = validate_sql_expression_allowlist(expr)
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    if agg == "percentile":
        params = measure.get("aggParams") or {}
        raw = params.get("percentile", DEFAULT_PERCENTILE) if isinstance(params, Mapping) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 1:
            raise ValueError(f"its measure {name!r} has aggParams.percentile {raw!r}")
        fn = "PERCENTILE_DISC" if params.get("useDiscretePercentile") else "PERCENTILE_CONT"
        return f"{fn}({float(raw):g}) WITHIN GROUP (ORDER BY {expr})"
    return f"{_AGGREGATES[agg]}({expr})"


def metric_sql(
    metric: Mapping[str, Any], semantics: Mapping[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """``(SQL, None)`` for a simple metric over a measure, its filter as ``FILTER (WHERE ...)``;
    ``(None, why)`` for one that is not rendered (``None`` for a derived or ratio metric, which
    the text describes in words)."""
    if (_text(metric.get("type")) or "simple") != "simple":
        return None, None
    measure_name = _text(metric.get("measure"))
    measure = next(
        (
            m
            for m in semantics.get("measures") or ()
            if isinstance(m, Mapping) and _text(m.get("name")) == measure_name
        ),
        None,
    )
    if measure is None:
        return None, f"its measure {measure_name!r} is not in semantics.measures"
    try:
        sql = aggregate_sql(measure_name, measure)
        condition = metric.get("filter")
        if condition is not None:
            condition = validate_sql_expression_allowlist(condition)
            if not _balanced(condition):
                raise ValueError("its filter has unbalanced parentheses")
            sql += f" FILTER (WHERE {condition})"
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("its ") else "its SQL is not a plain expression"
        return None, f"{reason}, so the Genie space gets its description without SQL."
    return sql, None


def _measure_snippets(product_id: str, table: Any) -> List[Dict[str, Any]]:
    semantics = _semantics(table.exposure)
    snippets = []
    for metric in _exposed_metrics(semantics):
        name = _text(metric.get("name"))
        sql, _problem = metric_sql(metric, semantics)
        if not sql or not name:
            continue
        try:
            alias = validate_ident(name)
        except ValueError:
            continue
        snippets.append(
            {
                "id": _id(product_id, "measure", table.full_name, alias),
                "alias": alias,
                "display_name": alias.replace("_", " "),
                "sql": [sql],
            }
        )
    return _by_id(snippets)


# ---------------------------------------------------------------------- converge (live)


def converge(
    compiled: Compiled,
    say: Say,
    *,
    client_factory: Optional[ClientFactory] = None,
    dry_run: bool = False,
) -> None:
    """Create, update or trash the product's space so it is what the contract asks.

    Runs after ``tofu apply`` and the masks and row filters, so the tables exist and are
    protected first. With ``dry_run`` it reads and prints, and writes nothing. Raises
    :class:`GenieError` (or a refusal) for what it cannot do.
    """
    if compiled.refusals:
        raise compiled.refusals[0]
    space = compiled.space
    if space is None:
        _retire(compiled.product_id, say, client_factory, dry_run)
        return
    if not space.warehouse_id:
        raise GenieError(
            f"{WAREHOUSE_ENV} is not set, and a Genie space runs its queries on that warehouse.",
            (f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace.",),
        )
    client = _client(client_factory)
    say("\n  Databricks Genie space (Genie API, not OpenTofu):")
    found = _call(client, "list the Genie spaces", lambda: _find(client, space.marker))
    if len(found) > 1:
        raise GenieError(
            f"{len(found)} Genie spaces carry {space.marker} in their description: "
            + ", ".join(str(s.get("space_id")) for s in found)
            + ". fluid apply keeps one space per product and cannot tell which is the product's.",
            ("Trash the extra spaces (or remove the marker from their description), then re-run.",),
        )
    serialized = _with_metric_views(client, space)
    if found:
        space_id = str(found[0]["space_id"])
        _update_found(client, space_id, space, serialized, say, dry_run)
    elif dry_run:
        say(f"    would create {space.title!r}: {_summary(serialized, space.tables)}")
        if space.readers:
            say(f"    would share it ({READER_LEVEL}) with {', '.join(space.readers)}")
        say("    dry-run: no Genie space was created, changed or shared.")
        return
    else:
        space_id = _create_new(client, space, serialized, say)
    _share(client, space_id, space.readers, say, dry_run)
    if dry_run:
        say("    dry-run: no Genie space was created, changed or shared.")


def _with_metric_views(client: Any, space: Space) -> Dict[str, Any]:
    """``serialized_space`` with every metric view over the space's tables among the tables:
    the service keeps one there, and moves one sent under ``data_sources.metric_views`` there
    too (measured on a live workspace, 2026-11-12), so sent anywhere else it would never
    compare equal and every apply would update."""
    serialized = dict(space.serialized)
    views = _call(
        client, "read the metric views in Unity Catalog", lambda: metric_views(client, space.tables)
    )
    if views:
        tables = sorted(
            [*serialized["data_sources"]["tables"], *views], key=lambda t: t["identifier"]
        )
        serialized["data_sources"] = {**serialized["data_sources"], "tables": tables}
    return serialized


def _update_found(
    client: Any,
    space_id: str,
    space: Space,
    serialized: Mapping[str, Any],
    say: Say,
    dry_run: bool,
) -> None:
    live = _call(client, "read the Genie space", lambda: _get(client, space_id))
    changes = differences(live, space, serialized)
    if not changes:
        say(f"    {space.title!r}: no change ({_url(space_id)})")
    elif dry_run:
        say(f"    would update {space.title!r}: {', '.join(changes)} ({_url(space_id)})")
    else:
        _call(
            client,
            "update the Genie space",
            lambda: _update(client, space_id, space, serialized, live.get("etag")),
        )
        say(f"    updated {space.title!r}: {', '.join(changes)} ({_url(space_id)})")


def _create_new(client: Any, space: Space, serialized: Mapping[str, Any], say: Say) -> str:
    created = _call(client, "create the Genie space", lambda: _create(client, space, serialized))
    space_id = str(created.get("space_id") or "")
    if not space_id:
        raise GenieError("the Genie API created a space and returned no space_id.")
    say(f"    created {space.title!r}: {_summary(serialized, space.tables)} ({_url(space_id)})")
    return space_id


def _retire(product_id: str, say: Say, factory: Optional[ClientFactory], dry_run: bool) -> None:
    """Trash the space an earlier apply made, now the contract asks for none.

    Without a client, or where the workspace has no Genie API (403 or 404), there is nothing to
    say: no space can have been made there. Any other failure to look is a note, since the
    space stays as it was and grants nothing.
    """
    if not product_id:
        return
    try:
        client = (factory or _default_client)()
    except ImportError:
        return
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        say(f"  note: not checked for a Genie space an earlier apply made: {_scrub(exc)}")
        return
    try:
        found = _find(client, marker(product_id))
    except Exception as exc:  # noqa: BLE001
        if _status(exc) not in (403, 404):
            say(f"  note: not checked for a Genie space an earlier apply made: {_scrub(exc)}")
        return
    for space in found:
        space_id = str(space.get("space_id"))
        title = space.get("title")
        if dry_run:
            say(
                f"\n  Databricks Genie space: would trash {title!r} ({space_id}), which "
                f"extensions.{EXTENSION_KEY} no longer asks for."
            )
            continue
        _call(
            client,
            "trash the Genie space",
            lambda sid=space_id: client.api_client.do("DELETE", f"{SPACES_PATH}/{quote(sid)}"),
        )
        say(
            f"\n  Databricks Genie space: trashed {title!r} ({space_id}), which "
            f"extensions.{EXTENSION_KEY} no longer asks for."
        )


def _find(client: Any, token: str) -> List[Dict[str, Any]]:
    """Every space whose description carries ``token``, across the list's pages."""
    found: List[Dict[str, Any]] = []
    query: Dict[str, Any] = {"page_size": _PAGE_SIZE}
    seen: set = set()
    while True:
        page = client.api_client.do("GET", SPACES_PATH, query=query) or {}
        for space in page.get("spaces") or ():
            if isinstance(space, Mapping) and token in str(space.get("description") or ""):
                found.append(dict(space))
        token_next = page.get("next_page_token")
        if not token_next or token_next in seen:
            return found
        seen.add(token_next)
        query = {"page_size": _PAGE_SIZE, "page_token": token_next}


def _get(client: Any, space_id: str) -> Dict[str, Any]:
    return client.api_client.do(
        "GET",
        f"{SPACES_PATH}/{quote(space_id)}",
        query={"include_serialized_space": "true"},
    )


def _body(space: Space, serialized: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "title": space.title,
        "description": space.description,
        "warehouse_id": space.warehouse_id,
        "serialized_space": json.dumps(serialized, sort_keys=True),
    }


def _create(client: Any, space: Space, serialized: Mapping[str, Any]) -> Dict[str, Any]:
    return client.api_client.do("POST", SPACES_PATH, body=_body(space, serialized)) or {}


def _update(
    client: Any, space_id: str, space: Space, serialized: Mapping[str, Any], etag: Any
) -> None:
    body = _body(space, serialized)
    if etag:
        body["etag"] = etag
    try:
        client.api_client.do("PATCH", f"{SPACES_PATH}/{quote(space_id)}", body=body)
    except Exception as exc:  # noqa: BLE001 — a 409 is someone else's edit, said as such
        if _status(exc) == 409:
            raise GenieError(
                "the Genie space changed while this apply was updating it (its etag no longer "
                "matched), so nothing was overwritten.",
                ("Re-run fluid apply.",),
            ) from exc
        raise


def differences(live: Mapping[str, Any], space: Space, serialized: Mapping[str, Any]) -> List[str]:
    """What differs between the live space and the one the contract asks for, by name."""
    changes = []
    if live.get("title") != space.title:
        changes.append("title")
    if (live.get("description") or "") != space.description:
        changes.append("description")
    if live.get("warehouse_id") != space.warehouse_id:
        changes.append("warehouse")
    try:
        held = json.loads(live.get("serialized_space") or "{}")
    except (TypeError, ValueError):
        held = {}
    if held != serialized:
        named = []
        if _dig(held, ("config", "sample_questions")) != _dig(
            serialized, ("config", "sample_questions")
        ):
            named.append("sample questions")
        named += _source_changes(held, serialized, set(space.tables))
        if _dig(held, ("instructions", "text_instructions")) != _dig(
            serialized, ("instructions", "text_instructions")
        ):
            named.append("instructions")
        if _dig(held, ("instructions", "sql_snippets")) != _dig(
            serialized, ("instructions", "sql_snippets")
        ):
            named.append("SQL snippets")
        changes += named or ["other settings"]
    return changes


def _source_changes(held: Any, wanted: Any, own: set) -> List[str]:
    """``tables`` and/or ``metric views``: which of the space's data sources differ. Its own
    tables are the product's; any other entry in the list is a metric view over them."""
    path = ("data_sources", "tables")
    before = {str(t.get("identifier")): t for t in _dig(held, path) or () if isinstance(t, Mapping)}
    after = {
        str(t.get("identifier")): t for t in _dig(wanted, path) or () if isinstance(t, Mapping)
    }
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    out = []
    if any(name in own for name in changed) or (
        not changed and _dig(held, path) != _dig(wanted, path)
    ):
        out.append("tables")
    if any(name not in own for name in changed):
        out.append("metric views")
    return list(dict.fromkeys(out))


def metric_views(client: Any, tables: Sequence[str]) -> List[Dict[str, Any]]:
    """The metric views whose ``source`` is one of ``tables``, as space data sources, sorted.

    Read from each table's schema (``GET /api/2.1/unity-catalog/tables``, ``table_type
    METRIC_VIEW``); the definition is the metric view's YAML, whose ``source`` names the table.
    """
    wanted = {t.lower() for t in tables}
    out: Dict[str, Dict[str, Any]] = {}
    for catalog, schema in sorted({tuple(t.split(".")[:2]) for t in tables}):
        for info in _list_tables(client, catalog, schema):
            view = _metric_view_over(client, info, wanted)
            if view is not None:
                out[view["identifier"]] = view
    return [out[name] for name in sorted(out)]


def _metric_view_over(
    client: Any, info: Mapping[str, Any], wanted: Collection[str]
) -> Optional[Dict[str, Any]]:
    """``info`` as a space data source when it is a metric view over one of ``wanted``."""
    if str(info.get("table_type") or "").upper() != "METRIC_VIEW":
        return None
    full_name = str(info.get("full_name") or "").lower()
    if not full_name:
        return None
    definition = info.get("view_definition")
    if definition is None:  # a list that leaves the definition out: read the view itself
        definition = (_read_table(client, full_name) or {}).get("view_definition")
    if _metric_view_source(definition) not in wanted:
        return None
    view: Dict[str, Any] = {"identifier": full_name}
    comment = _sentence(_text(info.get("comment")))
    if comment:
        view["description"] = [comment]
    return view


def _list_tables(client: Any, catalog: str, schema: str) -> List[Mapping[str, Any]]:
    query: Dict[str, Any] = {
        "catalog_name": catalog,
        "schema_name": schema,
        "omit_columns": "true",
    }
    out: List[Mapping[str, Any]] = []
    seen: set = set()
    while True:
        try:
            page = client.api_client.do("GET", TABLES_PATH, query=query) or {}
        except Exception as exc:  # noqa: BLE001 — a schema not created yet holds no view
            if _status(exc) == 404:
                return out
            raise
        out += [t for t in page.get("tables") or () if isinstance(t, Mapping)]
        token = page.get("next_page_token")
        if not token or token in seen:
            return out
        seen.add(token)
        query = {**query, "page_token": token}


def _read_table(client: Any, full_name: str) -> Optional[Dict[str, Any]]:
    try:
        return client.api_client.do("GET", f"{TABLES_PATH}/{quote(full_name, safe='.')}")
    except Exception as exc:  # noqa: BLE001 — a view this identity cannot read is not used
        if _status(exc) in (403, 404):
            return None
        raise


def _metric_view_source(definition: Any) -> str:
    """The table a metric view's YAML names in ``source``, lower-cased; ``""`` for a query."""
    if not isinstance(definition, str) or not definition.strip():
        return ""
    import yaml

    try:
        doc = yaml.safe_load(definition)
    except yaml.YAMLError:
        return ""
    source = doc.get("source") if isinstance(doc, Mapping) else None
    if not isinstance(source, str):
        return ""
    name = source.strip().replace("`", "").lower()
    return "" if any(ch.isspace() for ch in name) else name


def _share(client: Any, space_id: str, readers: Sequence[str], say: Say, dry_run: bool) -> None:
    """Give ``readers`` ``CAN_RUN`` on the space and take it from anyone else who holds it
    directly. Direct ``CAN_EDIT`` and ``CAN_MANAGE`` stay, and so does every inherited level
    (the creator's ``CAN_MANAGE`` comes from the folder the space is in)."""
    path = PERMISSIONS_PATH.format(quote(space_id))
    doc = _call(
        client, "read the Genie space's permissions", lambda: client.api_client.do("GET", path)
    )
    held = _direct_levels(doc)
    wanted = {key: level for key, level in held.items() if level in _KEPT_LEVELS}
    for principal in readers:
        key = acl_key(principal)
        wanted.setdefault(key, READER_LEVEL)
    if wanted == held:
        return
    granted = sorted(key[1] for key, level in wanted.items() if held.get(key) != level)
    removed = sorted(key[1] for key in held if key not in wanted)
    what = []
    if granted:
        what.append(f"{READER_LEVEL} for {', '.join(granted)}")
    if removed:
        what.append(f"removed from {', '.join(removed)}")
    if dry_run:
        say(f"    would share: {'; '.join(what)}")
        return
    body = {
        "access_control_list": [
            {kind: name, "permission_level": level}
            for (kind, name), level in sorted(wanted.items())
        ]
    }
    _call(client, "share the Genie space", lambda: client.api_client.do("PUT", path, body=body))
    say(f"    shared: {'; '.join(what)}")


def acl_key(principal: str) -> Tuple[str, str]:
    """The Permissions API key for a Unity Catalog principal: a user's email, a service
    principal's application ID (a UUID) or a group's name, the three forms Unity Catalog
    grants to (``iac/principals.py::_databricks_identity``)."""
    if "@" in principal:
        return ("user_name", principal)
    if _UUID.match(principal):
        return ("service_principal_name", principal)
    return ("group_name", principal)


def _direct_levels(doc: Any) -> Dict[Tuple[str, str], str]:
    """``{(key, principal): level}`` of the permissions set on the space itself."""
    out: Dict[Tuple[str, str], str] = {}
    for entry in (doc or {}).get("access_control_list") or ():
        key = _acl_entry_key(entry)
        level = _direct_level(entry) if key is not None else ""
        if level:
            out[key] = level
    return out


def _direct_level(entry: Mapping[str, Any]) -> str:
    """The highest Genie level set on the space itself in one ACL entry, ``""`` for none
    (an inherited level is the folder's, which a share never touches)."""
    levels = [
        str(permission.get("permission_level") or "")
        for permission in entry.get("all_permissions") or ()
        if isinstance(permission, Mapping) and not permission.get("inherited")
    ]
    known = [level for level in levels if level in _LEVEL_RANK]
    return max(known, key=_LEVEL_RANK.__getitem__, default="")


def _acl_entry_key(entry: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(entry, Mapping):
        return None
    for kind in ("user_name", "service_principal_name", "group_name"):
        if entry.get(kind):
            return (kind, str(entry[kind]))
    return None


# ---------------------------------------------------------------------- helpers


def _client(factory: Optional[ClientFactory]) -> Any:
    try:
        return (factory or _default_client)()
    except UnsupportedBindingError:
        raise
    except ImportError:
        raise GenieError(
            "databricks-sdk is not installed, and the Genie space is created through it.",
            (f"Install the extra: {DATABRICKS_EXTRA_INSTALL}",),
        ) from None
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        raise GenieError(
            f"could not create the Databricks client for the Genie space: "
            f"{type(exc).__name__}: {_scrub(exc)}"
        ) from exc


def _default_client() -> Any:
    # Through the module, so a test that patches ``databricks_enforce.default_client`` (the
    # in-process stand-in) reaches this client too.
    return _enforce.default_client()


def _call(client: Any, what: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except UnsupportedBindingError:
        raise
    except Exception as exc:  # noqa: BLE001 — every API failure is said, with what to do
        status = _status(exc)
        remediation = ["Check DATABRICKS_HOST and the credential, then re-run fluid apply."]
        if status in (403, 404) and "Genie" in what:
            remediation = [
                "Check that Genie (AI/BI) is enabled in the workspace and that this identity "
                "may create Genie spaces, or remove extensions.databricksGenie.",
            ]
        raise GenieError(
            f"could not {what}: {type(exc).__name__}: {_scrub(exc)}", remediation
        ) from exc


#: The HTTP status each databricks-sdk error class stands for (``databricks/sdk/errors/
#: platform.py`` ``STATUS_CODE_MAPPING``): its errors carry the status as their class, not as an
#: attribute, and a subclass (``Aborted``, ``ResourceDoesNotExist``) as its parent's.
_SDK_STATUS = {
    "BadRequest": 400,
    "Unauthenticated": 401,
    "PermissionDenied": 403,
    "NotFound": 404,
    "ResourceConflict": 409,
    "TooManyRequests": 429,
}


def _status(exc: BaseException) -> Optional[int]:
    """The HTTP status of a failed call: the attribute a plain HTTP error carries, else the
    databricks-sdk error class (matched by name, so the SDK is not imported here)."""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(value, int):
        return value
    for cls in type(exc).__mro__:
        if cls.__name__ in _SDK_STATUS:
            return _SDK_STATUS[cls.__name__]
    return None


def _scrub(text: Any) -> str:
    """One short line with every credential value in the environment removed."""
    return _enforce.scrub(text)


def _url(space_id: str) -> str:
    host = str(os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
    return f"{host}/genie/rooms/{space_id}" if host.startswith("https://") else space_id


def _summary(serialized: Mapping[str, Any], own: Sequence[str]) -> str:
    sources = [t.get("identifier") for t in _dig(serialized, ("data_sources", "tables")) or ()]
    tables = sum(1 for name in sources if name in own)
    views = len(sources) - tables
    questions = len(_dig(serialized, ("config", "sample_questions")) or ())
    measures = len(_dig(serialized, ("instructions", "sql_snippets", "measures")) or ())
    parts = [f"{tables} table(s)"]
    if views:
        parts.append(f"{views} metric view(s)")
    parts.append(f"{questions} sample question(s)")
    if measures:
        parts.append(f"{measures} measure snippet(s)")
    return ", ".join(parts)


def _id(*parts: str) -> str:
    """A stable id of 32 lowercase hex characters, which Genie requires of every item."""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def _by_id(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Genie refuses an id-keyed list that is not sorted by id."""
    return sorted(items, key=lambda item: item["id"])


def _dig(doc: Any, path: Sequence[str]) -> Any:
    for key in path:
        doc = doc.get(key) if isinstance(doc, Mapping) else None
    return doc


def _strings_in(doc: Any) -> Iterable[str]:
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, Mapping):
        for value in doc.values():
            yield from _strings_in(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _strings_in(value)


def _semantics(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    semantics = exposure.get("semantics")
    return semantics if isinstance(semantics, Mapping) else {}


def _agent_policy(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = exposure.get("policy")
    agent = policy.get("agentPolicy") if isinstance(policy, Mapping) else None
    return agent if isinstance(agent, Mapping) else {}


def _labels(raw: Any) -> Dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _text(raw: Any) -> str:
    """A contract string, stripped and on one line; ``""`` for none. A structured description
    (``$defs.descriptionObject``) gives its purpose."""
    if isinstance(raw, Mapping):
        raw = raw.get("purpose")
    if raw is None or isinstance(raw, (Mapping, list, bool)):
        return ""
    return " ".join(str(raw).split())


def _sentence(text: str) -> str:
    return text if not text or text[-1] in ".!?" else text + "."


def _split(raw: Any, *separators: str) -> List[str]:
    """``raw`` split on any of ``separators`` (or a list as is), stripped, empties dropped."""
    if isinstance(raw, list):
        items = [_text(item) for item in raw]
    else:
        text = _text(raw)
        for separator in separators[1:]:
            text = text.replace(separator, separators[0])
        items = [part.strip() for part in text.split(separators[0])] if text else []
    return [item for item in items if item]


def _string_list(raw: Any) -> List[str]:
    return [_text(item) for item in raw if _text(item)] if isinstance(raw, list) else []


def _folded(text: str) -> str:
    return " ".join(text.replace("_", " ").casefold().split())


def _balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth < 0:
            return False
    return depth == 0
