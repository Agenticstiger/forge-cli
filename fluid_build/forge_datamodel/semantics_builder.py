# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared primitives for building ``exposes[].semantics`` blocks.

The contract semantics block has two independent producers — the
data-model pipeline (``forge_datamodel/emit/fluid_contract.py``) and the
copilot interview path (``cli/forge_copilot_contract_helpers.py``) —
which historically shared no code and drifted (different agg inference,
different grain handling, neither populating
``defaultAggTimeDimension``). The assembly idioms that must agree across
producers live here; per-path naming conventions (pinned by each path's
tests) stay with the paths.

The headline fix carried by this module: OSI metric expressions are
whole aggregate calls (``SUM(amount)``), and the previous translation
copied them verbatim into ``measures[].expr`` next to an inferred
``agg`` — so both consumers double-aggregated (the MCP query compiler
rendered ``SUM(SUM(amount))``, invalid SQL on every engine, and the dbt
MetricFlow bridge exported the same double wrap).
:func:`measure_from_aggregate_expression` strips the outer aggregate
when the expression is a single aggregate call, and
:func:`infer_measure_agg` classifies ``COUNT(DISTINCT …)`` as
``count_distinct`` (previously misfiled as plain ``count``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build.forge_datamodel import time_grains as _time_grains

# Single top-level aggregate call, e.g. ``SUM(amount)`` / ``count( * )``.
# The inner expression is validated for balanced parens separately so
# ``SUM(a) / COUNT(b)`` (which this regex also matches end-to-end with
# inner ``a) / COUNT(b``) is rejected as "not a single aggregate".
_AGGREGATE_CALL_RE = re.compile(
    r"(?is)^\s*(sum|avg|min|max|median|count)\s*\((.*)\)\s*$",
)
_DISTINCT_PREFIX_RE = re.compile(r"(?is)^\s*distinct\s+(.+)$")

# ANY aggregate function call anywhere in an expression — broader than
# :data:`_AGGREGATE_CALL_RE`, which only matches an expression that is
# EXACTLY one aggregate call. Used by the contract validator to reject
# the double-aggregation shape, including the compound forms the
# single-call parser deliberately skips (``SUM(a) / COUNT(b)``). The
# trailing ``\s*\(`` is what keeps a column named ``count_of_orders``
# from matching.
_ANY_AGGREGATE_CALL_RE = re.compile(
    r"(?i)\b("
    r"sum|avg|mean|min|max|median|count|"
    r"stddev|stddev_pop|stddev_samp|variance|var_pop|var_samp|"
    r"percentile_cont|percentile_disc|approx_percentile|approx_count_distinct|"
    r"array_agg|string_agg|listagg|group_concat|bool_and|bool_or|any_value"
    r")\s*\(",
)


def first_aggregate_call(expr: str) -> Optional[str]:
    """Return the name of the first aggregate function ``expr`` calls, or
    ``None`` when it calls none.

    A ``measures[].expr`` is the PRE-aggregation input — the declared
    ``agg`` is applied to it — so an aggregate call inside it means the
    contract double-aggregates (``{agg: sum, expr: SUM(amount)}`` →
    ``SUM(SUM(amount))``). A ``dimensions[].expr`` lands in the GROUP BY,
    where an aggregate is equally invalid. Both are rejected by
    ``cli/contract_validation.ContractValidator._validate_semantics``.
    """
    match = _ANY_AGGREGATE_CALL_RE.search(expr or "")
    return match.group(1).lower() if match is not None else None


def parse_aggregate_expression(expr: str) -> Optional[Tuple[str, str]]:
    """Split a single-aggregate SQL expression into (agg, inner expr).

    Returns ``None`` when ``expr`` is not exactly one aggregate call
    (complex expressions like ``SUM(a) / COUNT(b)`` belong in derived /
    ratio metrics, not measures). ``COUNT(DISTINCT x)`` maps to
    ``("count_distinct", "x")``; ``COUNT(*)`` maps to ``("count", "1")``
    — the dbt convention for a row count, and valid SQL either way.
    """
    match = _AGGREGATE_CALL_RE.match(expr or "")
    if match is None:
        return None
    func = match.group(1).lower()
    inner = match.group(2).strip()
    if not _parens_balanced(inner):
        return None
    if func == "count":
        distinct = _DISTINCT_PREFIX_RE.match(inner)
        if distinct is not None:
            return "count_distinct", distinct.group(1).strip()
        if inner == "*":
            return "count", "1"
        return "count", inner
    return func, inner


def infer_measure_agg(expr: str) -> str:
    """Best-effort agg classification for an aggregate expression.

    Falls back to ``sum`` for anything unrecognized — the historical
    default, kept so complex expressions degrade the same way they
    always have."""
    parsed = parse_aggregate_expression(expr)
    return parsed[0] if parsed is not None else "sum"


def measure_from_aggregate_expression(
    name: str,
    expr: str,
    *,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``measures[]`` entry from an aggregate expression.

    Single aggregate calls are split into ``agg`` + inner ``expr`` so
    consumers apply the aggregation exactly once. Expressions that are
    not a single aggregate keep the legacy verbatim shape (``agg: sum``
    + full expr) — still wrong for consumers, but that class needs
    derived/ratio metric emission, not a measure, and silently mangling
    it here would hide the modeling gap.
    """
    parsed = parse_aggregate_expression(expr)
    if parsed is not None:
        agg, inner = parsed
        measure: Dict[str, Any] = {"name": name, "agg": agg, "expr": inner}
    else:
        measure = {"name": name, "agg": "sum", "expr": expr}
    if description:
        measure["description"] = description
    return measure


def validate_semantics_block(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate-time gate for ``exposes[].semantics`` (anti-no-op, mirrors
    ``output_ports.vector.validate_vector_binding``). Returns
    ``(errors, warnings)``.

    Catches the DOUBLE-AGGREGATION shape. A ``measures[]`` entry declares
    its aggregation in ``agg`` and its PRE-aggregation input in ``expr``,
    so an ``expr`` that is itself an aggregate call double-wraps:
    ``{agg: sum, expr: SUM(TOTAL_PRICE)}`` compiles to
    ``SUM(SUM(TOTAL_PRICE))`` on the governed MCP ``query`` path and
    exports the same double wrap through the dbt MetricFlow bridge —
    invalid SQL on every engine. ``dimensions[].expr`` has the same
    problem: it lands in the GROUP BY.

    The forge emitters stopped producing that shape (see
    :func:`measure_from_aggregate_expression`), but nothing caught it in
    a HAND-AUTHORED or third-party contract: ``fluid validate`` reported
    "✅ Valid" with zero warnings and ``fluid policy-check --strict``
    scored 100/100, and the only feedback was an opaque engine error on
    the first query. Hard errors, because the shape cannot produce a
    working query on any engine.

    Also WARNS on a NAME COLLISION between a dimension and a metric or
    measure in the same expose. Every mainstream semantic layer treats
    those as one namespace and rejects the duplicate at model-build time
    (dbt MetricFlow registers entities / dimensions / measures / metrics
    in a single global namespace; Cube requires ``name`` to be unique
    among all dimensions, measures and segments —
    https://cube.dev/docs/dimensions/). Snowflake Semantic Views tolerate
    it and then emit two output columns with the same name, which the
    docs tell you to fix by renaming through a table alias
    (https://docs.snowflake.com/en/user-guide/views-semantic/querying).
    Our governed ``query`` path does that rename automatically where it
    can, and rejects the request where it can't — either way the contract
    is ambiguous and the author should know. A WARNING, not an error:
    contracts carrying the collision still answer queries correctly, so
    failing them outright would break working models.

    Checks the references of ``relationships`` (schema 0.7.7,
    RFC-semantics-tier1 §4) that a JSON Schema cannot: ``to`` is an expose
    of this contract, ``fromColumns`` and ``toColumns`` have the same length
    (they pair up by position, as Apache Ossie's ``from_columns`` and
    ``to_columns`` do), and every column is one its own expose's
    ``contract.schema`` declares. Errors: a consumer that follows the
    relationship, a join or a generator writing keys, would read columns
    that do not exist. That the arrays are not empty is the schema's rule
    (``minItems: 1``). It WARNS when a one-column relationship has no
    matching entity pair, because the dbt MetricFlow export joins by entity
    name and would not follow the relationship: entity/relationship drift.

    And the validity window of a type II slowly changing dimension (§5),
    with the rules dbt-semantic-interfaces' ``SemanticModelValidityWindowRule``
    applies: exactly two time dimensions carry ``typeParams.validityParams``,
    one ``isStart`` and the other ``isEnd``, and the model declares one
    ``natural`` entity, the key whose versions the window separates. Errors:
    without them no consumer can tell which version applies at a time.
    """
    errors: List[str] = []
    warnings: List[str] = []
    exposes = contract.get("exposes")
    if not isinstance(exposes, list):
        return errors, warnings
    for position, expose in enumerate(exposes):
        if not isinstance(expose, Mapping):
            continue
        semantics = expose.get("semantics")
        if not isinstance(semantics, Mapping):
            continue
        expose_id = str(expose.get("exposeId") or expose.get("id") or "?")
        for entry in semantics.get("measures") or []:
            if not isinstance(entry, Mapping):
                continue
            agg = entry.get("agg")
            expr = entry.get("expr")
            if not agg or not isinstance(expr, str):
                continue
            nested = first_aggregate_call(expr)
            if nested is None:
                continue
            errors.append(
                f"expose '{expose_id}': measure "
                f"'{entry.get('name') or '?'}' declares agg '{agg}' over an expr "
                f"that is itself an aggregate ({expr!r}). Consumers apply 'agg' "
                f"to 'expr', so this compiles to "
                f"{str(agg).upper()}({nested.upper()}(...)) — invalid SQL on "
                f"every engine. Split them: 'expr: SUM(amount)' + 'agg: sum' "
                f"becomes 'expr: amount' + 'agg: sum'; "
                f"'expr: COUNT(DISTINCT id)' becomes 'expr: id' + "
                f"'agg: count_distinct'."
            )
        for entry in semantics.get("dimensions") or []:
            if not isinstance(entry, Mapping):
                continue
            expr = entry.get("expr")
            if not isinstance(expr, str):
                continue
            nested = first_aggregate_call(expr)
            if nested is None:
                continue
            errors.append(
                f"expose '{expose_id}': dimension "
                f"'{entry.get('name') or '?'}' has an aggregate expr ({expr!r}). "
                f"Dimensions land in the GROUP BY, where {nested.upper()}(...) "
                f"is invalid on every engine. Model it as a measure, or group "
                f"by the underlying column."
            )
        warnings.extend(_name_collision_warnings(semantics, expose_id))
        errors.extend(_validity_window_errors(semantics, _expose_path(expose, position)))
    relationship_errors, relationship_warnings = _relationship_findings(exposes)
    errors.extend(relationship_errors)
    warnings.extend(relationship_warnings)
    return errors, warnings


#: The entity types that can hold a relationship's target key. dbt MetricFlow joins
#: an entity to a primary or unique entity of the same name
#: (https://docs.getdbt.com/docs/build/join-logic), and to the natural entity of a
#: model with a validity window.
_TARGET_KEY_ENTITY_TYPES = frozenset({"primary", "unique", "natural"})


def _exported_entity_types(semantics: Any) -> FrozenSet[str]:
    """The entity types the dbt MetricFlow export keeps for a model.

    Beside a validity window it keeps the natural entity and foreign ones; otherwise every
    type but natural (``engines/dbt/semantic_models._entity_fits_window``, after the
    dbt-semantic-interfaces window rules), so the drift check counts only what dbt will see.
    """
    if isinstance(semantics, Mapping) and validity_window(semantics) is not None:
        return frozenset({"natural", "foreign"})
    return frozenset({"primary", "unique", "foreign"})


def _as_list(value: Any) -> List[Any]:
    """``value`` when it is a list, else ``[]``: these checks run on contracts the schema refused."""
    return value if isinstance(value, list) else []


def _expose_path(expose: Mapping[str, Any], position: int) -> str:
    """``exposes[<exposeId>]``, or the position: the prefix ``fluid validate`` points at."""
    expose_id = expose.get("exposeId")
    return f"exposes[{expose_id if isinstance(expose_id, str) and expose_id else position}]"


def declared_columns(expose: Mapping[str, Any]) -> List[str]:
    """The column names ``expose``'s ``contract.schema`` declares, in order."""
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if not isinstance(schema, list):
        return []
    return [
        column["name"]
        for column in schema
        if isinstance(column, Mapping) and isinstance(column.get("name"), str)
    ]


def _column_list(value: Any) -> List[str]:
    """A ``fromColumns`` / ``toColumns`` value as a list, or ``[]`` when the schema refuses it."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return list(value)


def _column_errors(
    where: str, columns: Sequence[str], expose_path: str, declared: Sequence[str]
) -> List[str]:
    """One error per column of ``columns`` that ``declared`` (its expose's schema) lacks."""
    if not declared:
        named = ", ".join(repr(column) for column in columns)
        return [
            f"{where}: {expose_path} declares no contract.schema columns, so {named} "
            f"cannot be checked. Declare the expose's columns."
        ]
    known = set(declared)
    folded = {column.casefold(): column for column in declared}
    errors: List[str] = []
    for column in columns:
        if column in known:
            continue
        near = folded.get(column.casefold())
        hint = f" (did you mean {near!r}?)" if near else ""
        errors.append(
            f"{where}: {column!r} is not a column of {expose_path}{hint}. "
            f"Name a column its contract.schema declares."
        )
    return errors


def entity_column(entity: Mapping[str, Any]) -> Optional[str]:
    """The column an entity keys on: its ``expr``, else its name (the schema's default)."""
    value = entity.get("expr") or entity.get("name")
    return value if isinstance(value, str) else None


def _entity_names(
    expose: Mapping[str, Any], column: str, types: Optional[FrozenSet[str]] = None
) -> Set[str]:
    """Case-folded names of the entities of ``expose`` keyed on ``column`` (of ``types``)."""
    semantics = expose.get("semantics")
    entities = semantics.get("entities") if isinstance(semantics, Mapping) else None
    names: Set[str] = set()
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, Mapping) or not isinstance(entity.get("name"), str):
            continue
        if types is not None and str(entity.get("type") or "").lower() not in types:
            continue
        if entity_column(entity) == column:
            names.add(entity["name"].casefold())
    return names


def _relationship_findings(exposes: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """Referential errors and entity-drift warnings for every ``semantics.relationships``."""
    errors: List[str] = []
    warnings: List[str] = []
    by_id: Dict[str, Tuple[Mapping[str, Any], str]] = {}
    for position, expose in enumerate(exposes):
        if isinstance(expose, Mapping) and isinstance(expose.get("exposeId"), str):
            by_id.setdefault(expose["exposeId"], (expose, _expose_path(expose, position)))
    for position, expose in enumerate(exposes):
        if not isinstance(expose, Mapping):
            continue
        semantics = expose.get("semantics")
        relationships = semantics.get("relationships") if isinstance(semantics, Mapping) else None
        if not isinstance(relationships, list):
            continue
        path = _expose_path(expose, position)
        first_with_name: Dict[str, int] = {}
        for index, relationship in enumerate(relationships):
            if not isinstance(relationship, Mapping):
                continue
            where = f"{path}.semantics.relationships[{index}]"
            name = relationship.get("name")
            if isinstance(name, str):
                if name in first_with_name:
                    errors.append(
                        f"{where}: the name {name!r} is relationships[{first_with_name[name]}]'s "
                        f"too. A relationship is referred to by its name, so the names in one "
                        f"expose must differ."
                    )
                else:
                    first_with_name[name] = index
            found: List[str] = []
            target_id = relationship.get("to")
            target = by_id.get(target_id) if isinstance(target_id, str) else None
            if isinstance(target_id, str) and target is None:
                found.append(
                    f"{where}.to: {target_id!r} is not an exposeId of this contract "
                    f"({', '.join(sorted(by_id)) or 'none'}). A relationship joins two exposes "
                    f"of one contract."
                )
            from_columns = _column_list(relationship.get("fromColumns"))
            to_columns = _column_list(relationship.get("toColumns"))
            if from_columns and to_columns and len(from_columns) != len(to_columns):
                found.append(
                    f"{where}: fromColumns has {len(from_columns)} columns and toColumns "
                    f"{len(to_columns)}. They pair up by position, so they must be the same "
                    f"length."
                )
            if from_columns:
                found.extend(
                    _column_errors(
                        f"{where}.fromColumns", from_columns, path, declared_columns(expose)
                    )
                )
            if to_columns and target is not None:
                target_expose, target_path = target
                found.extend(
                    _column_errors(
                        f"{where}.toColumns",
                        to_columns,
                        target_path,
                        declared_columns(target_expose),
                    )
                )
            errors.extend(found)
            if found or target is None or len(from_columns) != 1 or len(to_columns) != 1:
                continue
            target_expose, target_path = target
            if target_expose is expose:
                # A self-join: one model cannot hold two entities of one name, so
                # MetricFlow has no entity pair to declare. Nothing to drift from.
                continue
            from_names = _entity_names(expose, from_columns[0], _exported_entity_types(semantics))
            target_types = (
                _exported_entity_types(target_expose.get("semantics")) & _TARGET_KEY_ENTITY_TYPES
            )
            target_names = _entity_names(target_expose, to_columns[0], target_types)
            if from_names & target_names:
                continue
            warnings.append(
                f"{where}: no entity pair matches this relationship, and the dbt MetricFlow "
                f"export joins by entity name, so it will not follow the relationship. Declare "
                f"an entity with expr {from_columns[0]!r} on {path} (type foreign) and one of "
                f"the same name with expr {to_columns[0]!r} on {target_path} (type "
                f"{' or '.join(sorted(target_types))})."
            )
    return errors, warnings


def _validity_params(dimension: Any) -> Optional[Mapping[str, Any]]:
    """A dimension's ``typeParams.validityParams``, or ``None``."""
    if not isinstance(dimension, Mapping):
        return None
    type_params = dimension.get("typeParams")
    validity = type_params.get("validityParams") if isinstance(type_params, Mapping) else None
    return validity if isinstance(validity, Mapping) else None


def declares_validity_params(semantics: Mapping[str, Any]) -> bool:
    """True when any dimension of ``semantics`` carries ``typeParams.validityParams``."""
    return any(_validity_params(d) is not None for d in _as_list(semantics.get("dimensions")))


def validity_window(semantics: Mapping[str, Any]) -> Optional[Tuple[str, str, str]]:
    """``(start dimension, end dimension, natural entity)`` of the model's validity window.

    ``None`` when the model declares none, and when its window breaks a rule
    :func:`validate_semantics_block` reports: a consumer acts on a well-formed
    window only, so the dbt bridge and the validator share one definition of it.
    """
    if not declares_validity_params(semantics) or _validity_window_errors(semantics, ""):
        return None
    start = end = None
    for dimension in _as_list(semantics.get("dimensions")):
        validity = _validity_params(dimension)
        if validity is None:
            continue
        if validity.get("isStart") is True:
            start = str(dimension.get("name"))
        elif validity.get("isEnd") is True:
            end = str(dimension.get("name"))
    natural = next(
        str(entity.get("name"))
        for entity in _as_list(semantics.get("entities"))
        if isinstance(entity, Mapping) and str(entity.get("type") or "").lower() == "natural"
    )
    if start is None or end is None:
        return None
    return start, end, natural


def _bound_flags(validity: Mapping[str, Any]) -> str:
    """Which ends of the window a ``validityParams`` marks, for a message."""
    flags = [key for key in ("isStart", "isEnd") if validity.get(key) is True]
    return " and ".join(flags) if flags else "neither isStart nor isEnd"


def _validity_window_errors(semantics: Mapping[str, Any], expose_path: str) -> List[str]:
    """The validity window rules (dbt-semantic-interfaces ``SemanticModelValidityWindowRule``)."""
    errors: List[str] = []
    bounds: List[Tuple[str, Mapping[str, Any]]] = []
    for index, dimension in enumerate(_as_list(semantics.get("dimensions"))):
        validity = _validity_params(dimension)
        if validity is None:
            continue
        name = str(dimension.get("name") or index)
        bounds.append((name, validity))
        if str(dimension.get("type") or "").lower() != "time":
            errors.append(
                f"{expose_path}.semantics.dimensions[{index}].typeParams.validityParams: "
                f"dimension {name!r} is {dimension.get('type')!r}, but a validity window is "
                f"bounded by times. Make it type: time, or remove validityParams."
            )
    if not bounds:
        return errors
    starts = [name for name, validity in bounds if validity.get("isStart") is True]
    ends = [name for name, validity in bounds if validity.get("isEnd") is True]
    both = [n for n, v in bounds if v.get("isStart") is True and v.get("isEnd") is True]
    if len(bounds) != 2 or len(starts) != 1 or len(ends) != 1 or both:
        found = ", ".join(f"{name} ({_bound_flags(validity)})" for name, validity in bounds)
        errors.append(
            f"{expose_path}.semantics: a validity window is exactly two time dimensions, one "
            f"with validityParams.isStart and the other with validityParams.isEnd; this model "
            f"has {found}."
        )
    naturals = [
        str(entity.get("name"))
        for entity in _as_list(semantics.get("entities"))
        if isinstance(entity, Mapping) and str(entity.get("type") or "").lower() == "natural"
    ]
    window = ", ".join(name for name, _ in bounds)
    if not naturals:
        errors.append(
            f"{expose_path}.semantics: {window} declare a validity window, but the model "
            f"declares no natural entity. The window separates the versions of one natural "
            f"key: declare it, e.g. {{name: customer, type: natural, expr: customer_id}}."
        )
    elif len(naturals) > 1:
        errors.append(
            f"{expose_path}.semantics: a validity window separates the versions of one natural "
            f"key, but the model declares {len(naturals)} natural entities "
            f"({', '.join(naturals)}). Keep one."
        )
    return errors


def _semantic_names(semantics: Mapping[str, Any], kind: str) -> Dict[str, str]:
    """Case-folded name → as-authored name for one semantics collection.

    Case-folded because the collision that matters is the one an ENGINE
    sees: unquoted identifiers fold, so ``Order_Status`` and
    ``order_status`` are the same output column name.
    """
    found: Dict[str, str] = {}
    for entry in semantics.get(kind) or []:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            found.setdefault(name.casefold(), name)
    return found


def _name_collision_warnings(semantics: Mapping[str, Any], expose_id: str) -> List[str]:
    """Warn when a metric or measure shares a dimension's name.

    A ``query`` that selects the metric/measure AND groups by the
    identically-named dimension would project two columns with one name;
    the drivers key rows with ``dict(zip(columns, values))``, so one of
    the two values would be silently dropped. ``compile_semantic_query``
    now renames or rejects instead — this warning names the underlying
    modelling problem at validate time rather than leaving it to be
    discovered on the first query.
    """
    messages: List[str] = []
    dimensions = _semantic_names(semantics, "dimensions")
    if not dimensions:
        return messages
    measures = _semantic_names(semantics, "measures")
    metric_measure: Dict[str, Any] = {}
    for entry in semantics.get("metrics") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            metric_measure.setdefault(str(entry["name"]).casefold(), entry.get("measure"))
    for folded, metric_name in _semantic_names(semantics, "metrics").items():
        if folded not in dimensions:
            continue
        measure_ref = metric_measure.get(folded)
        fallback_taken = isinstance(measure_ref, str) and measure_ref.casefold() in dimensions
        messages.append(
            f"expose '{expose_id}': metric '{metric_name}' has the same name as "
            f"dimension '{dimensions[folded]}'. Metric, measure and dimension "
            f"names share one namespace (dbt MetricFlow, Cube), so a query for "
            f"this metric grouped by that dimension would project two columns "
            f"with one name. The governed query path "
            + (
                f"REJECTS that request — measure '{measure_ref}' collides too, so "
                f"there is no distinct name left to fall back to."
                if fallback_taken
                else f"falls back to the measure name ('{measure_ref}') for the "
                f"aggregate column."
            )
            + " Rename one of them."
        )
    for folded, measure_name in measures.items():
        if folded not in dimensions:
            continue
        messages.append(
            f"expose '{expose_id}': measure '{measure_name}' has the same name as "
            f"dimension '{dimensions[folded]}'. Metric, measure and dimension "
            f"names share one namespace (dbt MetricFlow, Cube); a query for this "
            f"measure grouped by that dimension is rejected because both columns "
            f"would be projected under one name and one value would be silently "
            f"dropped from every row. Rename one of them."
        )
    return messages


#: The first schema version with ``semantics.relationships`` and
#: ``dimensions[].typeParams.validityParams`` (opened as the 0.7.7 preview).
RELATIONSHIPS_VERSION = "0.7.7"


def _version_key(version: Any) -> Tuple[int, ...]:
    """``"0.7.6"`` as ``(0, 7, 6)``; ``()`` for a value that is not a dotted version."""
    parts = str(version or "").split(".")
    return tuple(int(part) for part in parts) if all(p.isdigit() for p in parts) else ()


def carries_relationship_keys(contract: Mapping[str, Any]) -> bool:
    """True when an expose of ``contract`` declares a relationship or a validity window bound."""
    for expose in contract.get("exposes") or []:
        semantics = expose.get("semantics") if isinstance(expose, Mapping) else None
        if not isinstance(semantics, Mapping):
            continue
        if semantics.get("relationships") or declares_validity_params(semantics):
            return True
    return False


def version_for_relationship_keys(contract: Mapping[str, Any]) -> Optional[str]:
    """The ``fluidVersion`` a generated ``contract`` needs for its relationships and validity
    params, when its own predates them; ``None`` when it carries neither or already has it.

    For a producer (the dbt importer, the data-model emitter) that writes the newest stable
    version and only sometimes one of these fields: the contract moves to the version that
    holds them only when it uses them, so every other contract keeps its version and digest.
    """
    if not carries_relationship_keys(contract):
        return None
    if _version_key(contract.get("fluidVersion")) >= _version_key(RELATIONSHIPS_VERSION):
        return None
    return RELATIONSHIPS_VERSION


def simple_metric(name: str, measure: str, *, description: Optional[str] = None) -> Dict[str, Any]:
    metric: Dict[str, Any] = {"name": name, "type": "simple", "measure": measure}
    if description:
        metric["description"] = description
    return metric


def normalized_time_type_params(grain: Optional[str]) -> Optional[Dict[str, str]]:
    """``typeParams`` for a time dimension, or ``None`` when the grain
    doesn't normalize — emitters must omit rather than write an
    enum-invalid contract."""
    canonical = _time_grains.normalize_time_grain(grain)
    if canonical is None:
        return None
    return {"timeGranularity": canonical}


#: ``measures[].agg`` (the schema's enum) → the SQL aggregate it compiles to. Shared by the
#: consumers that render a measure as SQL (the MCP query compiler and the Databricks metric
#: view), so one contract aggregates the same way on every path. ``count_distinct`` renders
#: ``COUNT(DISTINCT …)`` and ``percentile`` the ordered-set ``PERCENTILE_CONT`` (see
#: :func:`aggregate_call`).
AGGREGATE_FUNCTIONS: Dict[str, str] = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "count_distinct": "COUNT",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "percentile": "PERCENTILE_CONT",
}

#: ``aggParams.percentile`` when an ``agg: percentile`` measure gives none: 0.5, the median.
#: The dbt MetricFlow bridge keeps its own copy, pinned equal by tests, so every consumer
#: answers the same number for one contract.
DEFAULT_PERCENTILE = 0.5


def percentile_params(measure_name: str, measure: Mapping[str, Any]) -> Tuple[float, bool]:
    """``(percentile, use_discrete)`` from ``measure.aggParams``; :class:`ValueError` if bad.

    The percentile is interpolated into SQL as a literal (an aggregate's argument must be a
    constant), so this check is the safety boundary: a real number in [0, 1], never a bool.
    """
    params = measure.get("aggParams") or {}
    if not isinstance(params, Mapping):
        raise ValueError(f"Measure {measure_name!r} has non-object aggParams")
    raw = params.get("percentile", DEFAULT_PERCENTILE)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 1:
        raise ValueError(
            f"Measure {measure_name!r} has invalid aggParams.percentile "
            f"{raw!r}; expected a number in [0, 1]"
        )
    return float(raw), bool(params.get("useDiscretePercentile"))


def aggregate_call(measure_name: str, measure: Mapping[str, Any], expr: str) -> str:
    """``AGG(expr)`` for one ``measures[]`` entry; :class:`ValueError` on an unknown ``agg``.

    ``expr`` is the caller's: already validated, and rewritten where its target needs it.
    ``count_distinct`` is ``COUNT(DISTINCT expr)``; ``percentile`` is the ANSI ordered-set
    ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)`` (``p`` from ``aggParams.percentile``,
    default :data:`DEFAULT_PERCENTILE`; ``useDiscretePercentile`` selects ``PERCENTILE_DISC``).
    A target without that aggregate refuses ``percentile`` before calling this.
    """
    agg = measure.get("agg")
    if not isinstance(agg, str) or agg not in AGGREGATE_FUNCTIONS:
        raise ValueError(
            f"Measure {measure_name!r} has unsupported agg "
            f"{agg!r}; supported: {sorted(AGGREGATE_FUNCTIONS)}"
        )
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    if agg == "percentile":
        percentile, use_discrete = percentile_params(measure_name, measure)
        fn = "PERCENTILE_DISC" if use_discrete else "PERCENTILE_CONT"
        return f"{fn}({percentile:g}) WITHIN GROUP (ORDER BY {expr})"
    return f"{AGGREGATE_FUNCTIONS[agg]}({expr})"


def parens_balanced(expr: str) -> bool:
    """Every ``(`` closes in order and none closes early: a contract expression is spliced
    into a larger statement, and an unbalanced one would escape its place in it."""
    return _parens_balanced(expr)


def default_agg_time_dimension(dimensions: List[Mapping[str, Any]]) -> Optional[str]:
    """The model-level default aggregation time dimension: the first
    declared time dimension. Populating it (instead of leaving the
    schema field write-never) lets consumers skip their fallback
    heuristics and makes the choice visible in the contract."""
    for dimension in dimensions:
        if dimension.get("type") == "time" and dimension.get("name"):
            return str(dimension["name"])
    return None


def _parens_balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
