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

"""Quality mapper.

Three levels of quality survive round-trip:

1. **Property-level** — emitted under ``schemaObject.properties[i].quality[]``.
   Generated from FLUID ``field.required``, primary-key tags,
   ``field.validationRules``, and any user-provided ``field.quality``. When the
   FLUID field was itself imported from ODCS, ``field.quality`` carries the
   verbatim list and is replayed as-is — no auto-checks added — so the
   round-trip stays lossless.

2. **Object-level** — ``schemaObject.quality[]``. Pass-through via
   ``expose.odcs_passthrough.object_quality`` (handled in :mod:`.schema`), plus
   the rules derived here from ``exposes[].contract.dq.rules`` and
   ``exposes[].contract.quality``.

3. **Contract-level** — ``customProperties[].property ==
   "fluidContractQuality"``. FLUID has a whole-contract quality block
   (a SodaCL/Great-Expectations spec, say) and ODCS v3.1.0 has no field for
   one, so it rides in the spec's own escape hatch. It used to be written to a
   top-level ``odcs["quality"]`` instead, which the ODCS root — a closed
   object, ``additionalProperties: false``, with no ``quality`` property —
   rejects outright: every contract that reached that branch exported a
   document ``fluid generate standard --format odcs --validate`` refuses. See
   :data:`FLUID_CONTRACT_QUALITY_PROPERTY`.

Every rule this module *mints* carries an ``id``. Three defects drove the
shape of the code below; all three were found by running forge's ODCS exports
through a real contract resolver and a real DQ engine.

* **Rules forge synthesises used to have no id.** ODCS v3.1.0 gives every
  quality rule an optional ``id`` (``$defs.StableId``), and the engines that
  label results by rule id refuse a contract whose rules have none — the one
  we tested rejects the whole document, so a single derived rule made the
  export unusable. The ids are derived, never counted, so two renders of one
  contract produce the same ids and an export is not a spurious diff. DataHub's
  ODCS ingestion source is the prior art for why that matters: it seeds its
  assertion URNs from ``quality.id``, so a stable id is what keeps an
  assertion's identity across a rename or a reorder.

* **An author's ``dq.rules`` and ``quality`` never reached the ODCS.** They
  were parked in ``customProperties.fluidExtras`` and nowhere else, so a
  consumer running the published contract's quality rules saw only the checks
  forge had synthesised from ``required`` and the primary key — never the rules
  the author actually wrote. They are now exported as ODCS quality as well, and
  a rule with no ODCS form is *reported* rather than silently dropped: ODCS has
  five library metrics and nothing else executable, so a type outside
  :data:`_DQ_LIBRARY_METRICS` and ``freshness`` is logged by name and left to
  ride along in ``fluidExtras``. Silence was the bug.

* **The exporter read a key the FLUID schema does not define.** It read
  ``field.validations``; the schema defines ``field.validationRules``, so rules
  written the documented way were never exported at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)

# ODCS v3.1.0 ``$defs.StableId``: "^[A-Za-z0-9_-]+$" — "Stable technical
# identifier for references. Must be unique within its containing array."
# A FLUID column name or rule id may hold a dot (``order.id``) or a space, so
# every run of characters outside that class collapses to a single underscore.
_STABLE_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

# ``dqRule.operator`` (fluid-schema ``$defs.dqRule``) → the ODCS
# ``$defs.DataQualityOperators`` key with the same meaning. The ODCS definition
# is a ``oneOf``, so a rule carries exactly one of these and no more.
_DQ_OPERATORS = {
    ">=": "mustBeGreaterOrEqualTo",
    ">": "mustBeGreaterThan",
    "<=": "mustBeLessOrEqualTo",
    "<": "mustBeLessThan",
    "==": "mustBe",
    "!=": "mustNotBe",
}

# ``dqRule.type`` → (ODCS library metric, ODCS quality dimension). The metric
# names are the five in ODCS v3.1.0 ``$defs.DataQualityLibrary.metric``
# (nullValues, missingValues, invalidValues, duplicateValues, rowCount) and the
# dimensions are from ``$defs.DataQuality.dimension``; nothing here is
# invented. The completeness→nullValues and uniqueness→duplicateValues pairings
# are the ecosystem convention, not our reading of the spec: DataHub's ODCS
# ingestion source maps ``nullValues``/``mustBe: 0`` to a NOT_NULL assertion and
# ``duplicateValues``/``mustBe: 0`` to "fully unique", and maps
# ``invalidValues`` with ``arguments.validValues`` to an IN check and with
# ``arguments.pattern`` to a regex check.
_DQ_LIBRARY_METRICS = {
    "completeness": ("nullValues", "completeness"),
    "uniqueness": ("duplicateValues", "uniqueness"),
    "valid_values": ("invalidValues", "conformity"),
}

# ``dqRule.severity`` → ODCS ``severity``. ODCS types the field as a free
# string and only *documents* info/warning/error by example, so the one FLUID
# spelling that differs from that vocabulary is normalised and the rest ride
# through verbatim — including "critical", which ODCS has no word for and which
# downgrading to "error" would quietly erase.
_DQ_SEVERITY = {"warn": "warning"}

# ``customProperties`` key carrying the contract-level quality block.
#
# This used to be a top-level ``odcs["quality"]``, which is not a thing: the
# ODCS v3.1.0 root object is ``additionalProperties: false`` (and
# ``unevaluatedProperties: false``) and declares no ``quality`` property, so
# the key produced two validation errors and failed the whole document. The
# branch is hard to reach — ``fluid.quality`` is not in any FLUID schema
# version, so ``fluid validate`` rejects a contract that declares it — but
# ``OdcsProvider.render`` is handed a plain mapping with no schema gate, and
# ``import_contract``'s own validation is deliberately non-fatal, so a
# hand-written ODCS carrying a root ``quality`` imports and then re-exports
# through here. Either way the output was invalid.
#
# ``customProperties`` is the spec's designated escape hatch and the name
# follows the two properties this provider already writes (``fluidExtras`` in
# :mod:`.extras`, ``fluidProductName`` in :mod:`.metadata`): ``fluid``-prefixed
# camelCase, per ODCS's own rule for the key.
FLUID_CONTRACT_QUALITY_PROPERTY = "fluidContractQuality"

_CONTRACT_QUALITY_DESCRIPTION = (
    "Contract-level data quality specification carried over from the FLUID "
    "contract's 'quality' block. ODCS v3.1.0 has no root field for a "
    "whole-contract quality spec, so it is published here; safe for other "
    "consumers to ignore."
)


def _stable_id(*parts: Any) -> str:
    """A ``$defs.StableId``-shaped id built from ``parts``."""
    slug = "-".join(_STABLE_ID_UNSAFE.sub("_", str(p)) for p in parts if p not in (None, ""))
    return slug or "quality"


def _claim(base: str, taken: Set[str]) -> str:
    """``base``, or ``base-2``, ``base-3``… when it is already in ``taken``.

    Deterministic: the suffix comes from how many ids ahead of this one in
    document order already claimed the name, never from a counter that depends
    on anything outside the document.
    """
    candidate = base
    attempt = 1
    while candidate in taken:
        attempt += 1
        candidate = f"{base}-{attempt}"
    taken.add(candidate)
    return candidate


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    """Contract-level quality only — property/object level are handled by schema.

    Two sources, in precedence order. The namespaced ``customProperties`` entry
    is where a FLUID export puts the block (see
    :data:`FLUID_CONTRACT_QUALITY_PROPERTY`), so it comes first. A root
    ``quality`` key is still read behind it: no valid ODCS can carry one, but
    import validation here is non-fatal by design, so a document that does
    carry it is better read than dropped.
    """
    contract_quality = _contract_quality_from_custom_properties(ctx.odcs)
    if contract_quality is None:
        contract_quality = ctx.odcs.get("quality")
    if contract_quality is None:
        return
    if isinstance(contract_quality, Mapping):
        ctx.fluid["quality"] = dict(contract_quality)
    else:
        metadata_passthrough(ctx.fluid)["contract_quality_raw"] = contract_quality


def _contract_quality_from_custom_properties(odcs: Mapping[str, Any]) -> Any:
    custom_properties = odcs.get("customProperties")
    if not isinstance(custom_properties, list):
        return None
    for prop in custom_properties:
        if isinstance(prop, Mapping) and prop.get("property") == FLUID_CONTRACT_QUALITY_PROPERTY:
            return prop.get("value")
    return None


# ----- FLUID → ODCS (contract level) --------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    if not ctx.options.get("include_quality_checks", True):
        return
    _contract_level_to_odcs(ctx)
    _declared_quality_to_odcs(ctx)


def _contract_level_to_odcs(ctx: ExportCtx) -> None:
    pt = get_metadata_passthrough(ctx.fluid)
    if "contract_quality_raw" in pt:
        _stamp_contract_quality(ctx.odcs, pt["contract_quality_raw"])
        return

    spec = ctx.fluid.get("quality")
    if not spec:
        return
    if isinstance(spec, Mapping):
        _stamp_contract_quality(
            ctx.odcs,
            {
                "type": spec.get("type", "custom"),
                "specification": spec.get("specification", ""),
            },
        )


def _stamp_contract_quality(odcs: MutableMapping[str, Any], value: Any) -> None:
    """Record the contract-level quality block in ``customProperties``, once.

    Runs after the metadata mapper, which replays an imported document's own
    ``customProperties`` wholesale, so an entry already in the list is the
    source document's own and is left alone — that is what keeps export a fixed
    point instead of a list that grows one duplicate per render. Same shape and
    same precedence as :func:`..metadata._stamp_product_name`.
    """
    existing = odcs.get("customProperties")
    properties: List[Any] = list(existing) if isinstance(existing, list) else []
    if any(
        isinstance(prop, Mapping) and prop.get("property") == FLUID_CONTRACT_QUALITY_PROPERTY
        for prop in properties
    ):
        return
    properties.append(
        {
            "property": FLUID_CONTRACT_QUALITY_PROPERTY,
            "value": value,
            "description": _CONTRACT_QUALITY_DESCRIPTION,
        }
    )
    odcs["customProperties"] = properties


# ----- FLUID → ODCS (the author's own dq.rules / quality expressions) ------


def _declared_quality_to_odcs(ctx: ExportCtx) -> None:
    """Export ``exposes[].contract.{dq.rules,quality}`` as ODCS quality.

    Runs after the schema mapper (see the pipeline order in
    :mod:`..mappers`), so the SchemaObjects are already in ``ctx.odcs`` and
    each rule can be attached to the property its ``selector`` names — or to
    the object itself when it names no column.
    """
    schema_objects = ctx.odcs.get("schema")
    if not isinstance(schema_objects, list):
        return

    exposes: Dict[Any, Mapping[str, Any]] = {}
    for expose in ctx.fluid.get("exposes") or []:
        if isinstance(expose, Mapping):
            key = expose.get("exposeId") or expose.get("id")
            if key:
                exposes[key] = expose

    for obj in schema_objects:
        if not isinstance(obj, dict):
            continue
        # The schema mapper names each SchemaObject after its exposeId.
        expose = exposes.get(obj.get("name"))
        if not isinstance(expose, Mapping):
            continue
        contract = expose.get("contract")
        if not isinstance(contract, Mapping):
            continue
        _attach_declared_quality(ctx, obj, contract, str(obj.get("name")))


def _attach_declared_quality(
    ctx: ExportCtx,
    obj: Dict[str, Any],
    contract: Mapping[str, Any],
    expose_id: str,
) -> None:
    taken = _existing_ids(obj)
    properties = {
        p["name"]: p
        for p in obj.get("properties") or []
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    }

    dq = contract.get("dq")
    rules = dq.get("rules") if isinstance(dq, Mapping) else None
    for rule in rules or []:
        if not isinstance(rule, Mapping):
            continue
        check, reason = _dq_rule_to_quality(rule)
        if check is None:
            ctx.logger.warning(
                "dq rule %r (type %r) on expose %r has no ODCS v3.1.0 quality form: %s. "
                "It is NOT exported as ODCS quality; it rides along verbatim under "
                "customProperties.fluidExtras so nothing is lost. Restate it as a dq rule "
                "of type completeness, uniqueness, valid_values or freshness to have it "
                "exported, or keep running it with your own engine.",
                rule.get("id"),
                rule.get("type"),
                expose_id,
                reason,
            )
            continue
        rule_id = _stable_id(rule.get("id") or rule.get("type"))
        if rule_id in taken:
            continue
        taken.add(rule_id)
        target = properties.get(rule.get("selector")) or obj
        _append_quality(target, {"id": rule_id, **check})

    for expr in contract.get("quality") or []:
        if not isinstance(expr, Mapping) or not expr.get("expression"):
            continue
        rule_id = _stable_id(expr.get("rule") or "expression")
        if rule_id in taken:
            continue
        taken.add(rule_id)
        _append_quality(obj, {"id": rule_id, **_expression_to_quality(expr)})


def _existing_ids(obj: Mapping[str, Any]) -> Set[str]:
    """Every quality-rule id already in this SchemaObject, object and columns.

    ODCS only requires an id to be unique within its own array, but an engine
    that labels results by rule id needs them distinct across the whole object
    to tell two results apart, so a declared rule whose id is already in here is
    not exported twice. That is also what keeps FLUID → ODCS → FLUID → ODCS a
    fixed point: the first export writes the rule onto the column, the import
    reads it back into ``field.quality``, and the second export replays that list
    verbatim — so re-deriving the rule would double it on every round trip. A
    ``dq.rules`` id is unique within its contract, so an id match *is* the same
    rule, not a collision.

    Rules that arrive *without* an id are left alone rather than given one: a
    verbatim-imported list is replayed byte-for-byte (the round-trip guarantee
    this module is built around), and minting an id for a rule forge did not
    write would break it.
    """
    ids: Set[str] = set()
    arrays: List[Any] = [obj.get("quality")]
    arrays.extend(p.get("quality") for p in obj.get("properties") or [] if isinstance(p, Mapping))
    for array in arrays:
        for rule in array or []:
            if isinstance(rule, Mapping) and isinstance(rule.get("id"), str):
                ids.add(rule["id"])
    return ids


def _append_quality(target: Dict[str, Any], check: Dict[str, Any]) -> None:
    """Append to ``target["quality"]`` without mutating a pass-through list.

    The schema mapper assigns ``schema_object["quality"] = pt["object_quality"]``
    by reference, so appending in place would write into the FLUID contract's
    own pass-through bucket and the rule would come back doubled on the next
    export.
    """
    target["quality"] = list(target.get("quality") or []) + [check]


def _dq_rule_to_quality(rule: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """One FLUID ``dqRule`` as an ODCS quality rule, or ``(None, reason)``."""
    rule_type = rule.get("type")
    description = rule.get("description")
    selector = rule.get("selector")

    library = _DQ_LIBRARY_METRICS.get(rule_type)
    if library:
        metric, dimension = library
        check: Dict[str, Any] = {"type": "library", "metric": metric, "dimension": dimension}
        check.update(_dq_threshold(rule))
        check["description"] = description or (f"{rule_type} check on {selector or 'the dataset'}")
        _dq_severity(check, rule)
        return check, ""

    if rule_type == "freshness":
        # ODCS has no library metric for freshness and we have no query to put
        # in a ``sql`` rule, so it goes out as the spec's own escape hatch for
        # engine-specific checks (``$defs.DataQualityCustom``: "quality
        # attributes that are vendor-specific"). The implementation object
        # carries the rule verbatim, which is what DataHub's ODCS source does
        # with a custom rule — engine becomes the assertion type, implementation
        # its logic.
        implementation = {
            k: rule[k] for k in ("selector", "window", "threshold", "operator") if k in rule
        }
        check = {
            "type": "custom",
            "engine": "fluid",
            "implementation": implementation,
            "dimension": "timeliness",
            "description": description or (f"freshness check on {selector or 'the dataset'}"),
        }
        _dq_severity(check, rule)
        return check, ""

    return None, (
        "ODCS v3.1.0 executes only the five library metrics (nullValues, missingValues, "
        "invalidValues, duplicateValues, rowCount), a sql query or a named custom engine, "
        f"and a {rule_type!r} rule supplies none of them"
    )


def _dq_threshold(rule: Mapping[str, Any]) -> Dict[str, Any]:
    """The rule's ODCS comparison operator — exactly one key, as ODCS requires.

    ``mustBe: 0`` is the default because that is what a bare
    completeness/uniqueness rule means: no failing rows tolerated. An operator
    with no threshold to compare against falls back to it rather than emitting
    an operator with no value, which no ODCS consumer could evaluate.
    """
    threshold = rule.get("threshold")
    operator = _DQ_OPERATORS.get(rule.get("operator") or "")
    if operator and threshold is not None:
        return {operator: threshold}
    return {"mustBe": 0}


def _dq_severity(check: Dict[str, Any], rule: Mapping[str, Any]) -> None:
    severity = rule.get("severity")
    if severity:
        check["severity"] = _DQ_SEVERITY.get(severity, severity)


# ``\b`` so only a whole first token counts: a column named ``selected_at`` or
# ``with_tax`` begins with these letters and is a predicate, not a query.
_QUERY_FIRST_TOKEN_RE = re.compile(r"\s*(?:select|with)\b", re.IGNORECASE)


def _expression_to_quality(expr: Mapping[str, Any]) -> Dict[str, Any]:
    """One ``exposes[].contract.quality`` entry as an ODCS ``sql`` rule.

    The FLUID field is "SQL or boolean expression that must evaluate to true" —
    a ROW PREDICATE, such as ``preferred_customer_flag IN ('Y', 'N')``. ODCS is
    specific about what ``sql`` means: ``$defs.DataQuality.type`` says "'sql' is
    an individual SQL query that returns a value that can be compared", and
    ``$defs.DataQualitySql.query`` carries the example ``SELECT COUNT(*) FROM
    ${table} WHERE ${column} IS NOT NULL``. Emitting the bare predicate as
    ``query`` therefore handed an engine something it cannot execute: it was a
    fragment, not a statement, and the ``mustBe: true`` beside it compared
    against a value no query produced.

    A predicate is now exported as the count of rows that fail it, compared to
    zero — the shape ODCS's own example takes, and one its
    ``$defs.DataQualityOperators`` ``oneOf`` accepts. ``${table}`` is the
    placeholder from that example.

    ``IS NOT TRUE`` rather than ``NOT (...)`` on purpose. Under SQL's
    three-valued logic ``NOT (flag IN ('Y','N'))`` is NULL when ``flag`` is
    NULL, so a naive negation counts no failure for the row — yet a row whose
    predicate did not evaluate to TRUE has not satisfied "must evaluate to
    true". ``IS NOT TRUE`` catches NULL and FALSE alike, and is supported by
    DuckDB, PostgreSQL, MySQL, Snowflake and BigQuery.

    An expression that is already a query (its first token is ``SELECT`` or
    ``WITH``) is passed through as written with the previous comparison: the
    author wrote a statement and chose what it returns. The boundary matters —
    ``startswith`` alone sent ``selected_at IS NOT NULL`` and ``with_tax > 0``
    down that branch and exported them unchanged, reinstating the very fragment
    this is here to remove.

    A predicate still containing a ``;`` of its own is left as the author wrote
    it: the field is specified as one predicate, and a second statement in it is
    not something to guess at.
    """
    expression = str(expr["expression"]).strip()
    # A trailing ``;`` would end up inside the wrapper's parentheses below.
    expression = expression.rstrip(";").rstrip()
    is_query = bool(_QUERY_FIRST_TOKEN_RE.match(expression))

    check: Dict[str, Any] = {"type": "sql"}
    if is_query:
        check["query"] = expression
        check["mustBe"] = True
        check["description"] = f"Expression must hold for every row: {expression}"
    else:
        # The predicate gets its own line: a trailing ``-- comment`` on one line
        # with the wrapper swallowed ``) IS NOT TRUE`` and left a statement the
        # engine could not parse.
        check["query"] = f"SELECT COUNT(*) FROM ${{table}} WHERE (\n{expression}\n) IS NOT TRUE"
        check["mustBe"] = 0
        check["description"] = f"Rows failing the expression must be 0: {expression}"

    severity = expr.get("severity")
    if severity:
        check["severity"] = _DQ_SEVERITY.get(severity, severity)
    return check


# ----- FLUID → ODCS (property level, called from schema mapper) -----------


def to_odcs_property(fld: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Return the ``quality[]`` array for an ODCS SchemaProperty.

    If ``field.quality`` is set (a list), it is replayed **verbatim** — this is
    the round-trip pass-through path. Otherwise auto-checks are synthesised
    from ``required``, primary-key tags and ``field.validationRules``, each one
    carrying a derived ``id``.

    The id is ``<column>-<metric>`` (or ``<column>-<validation type>``), which
    makes it unique within the whole SchemaObject for free, since column names
    already are, and which depends on nothing but the contract — so two renders
    of one contract produce identical ids and re-exporting is not a diff.
    """
    # Pass-through wins
    custom_quality = fld.get("quality")
    if isinstance(custom_quality, list):
        return list(custom_quality)

    quality_checks: List[Dict[str, Any]] = []
    name = fld.get("name", "unknown")
    taken: Set[str] = set()
    tags = fld.get("tags") or []
    is_primary_key = "primary-key" in tags or "primaryKey" in tags

    def add(check: Dict[str, Any], *id_parts: Any) -> None:
        quality_checks.append({"id": _claim(_stable_id(name, *id_parts), taken), **check})

    if fld.get("required"):
        add(
            {
                "type": "library",
                "metric": "nullValues",
                "mustBe": 0,
                "dimension": "completeness",
                "description": f"Field '{name}' must not contain null values",
            },
            "nullValues",
        )

    if is_primary_key:
        add(
            {
                "type": "library",
                "metric": "duplicateValues",
                "mustBe": 0,
                "dimension": "uniqueness",
                "description": f"Primary key field '{name}' must contain only unique values",
            },
            "duplicateValues",
        )
        if not fld.get("required"):
            add(
                {
                    "type": "library",
                    "metric": "nullValues",
                    "mustBe": 0,
                    "dimension": "completeness",
                    "description": (f"Primary key field '{name}' must not contain null values"),
                },
                "nullValues",
            )

    for validation in _validation_rules(fld):
        check = _validation_rule_to_quality(validation, name)
        if check is not None:
            add(check, validation.get("type") or "validationRule")

    for validation in _legacy_validations(fld):
        check = _validation_to_quality(validation, name, is_primary_key, fld)
        if check is not None:
            add(check, validation.get("type") or "validation")

    return quality_checks or None


def _validation_rules(fld: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """``field.validationRules`` — the key the FLUID schema actually defines.

    The exporter used to read ``field.validations`` only, a key no FLUID schema
    version has ever defined (``$defs.column`` is ``additionalProperties:
    false``), so a column written the documented way — ``validationRules``, with
    ``{type, constraint, message}`` items — exported no quality rule at all.
    """
    rules = fld.get("validationRules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, Mapping)]


def _legacy_validations(fld: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """``field.validations`` — tolerated alias, kept deliberately.

    It is not in the FLUID schema and never was, so ``fluid validate`` rejects
    a contract that writes it on a column. It survives because the exporter is
    also fed dicts that never went through the validator (importers and the
    provider's own public ``_extract_field_quality`` entry point, which the
    suite exercises with this shape), and because dropping it would silently
    stop exporting rules for any such caller rather than telling them.
    """
    validations = fld.get("validations") or []
    if isinstance(validations, Mapping):
        validations = [{"type": k, "value": v} for k, v in validations.items()]
    if not isinstance(validations, list):
        return []
    return [v for v in validations if isinstance(v, Mapping)]


def _validation_rule_to_quality(
    validation: Mapping[str, Any], name: str
) -> Optional[Dict[str, Any]]:
    """One ``field.validationRules`` entry as an ODCS quality rule.

    ``regex`` and ``enum`` become real, executable ODCS *library* rules rather
    than prose: ODCS ``invalidValues`` takes free-form ``arguments``, and the
    ecosystem convention for what to put there is DataHub's ODCS ingestion
    source, which reads ``arguments.pattern`` as a regex check and
    ``arguments.validValues`` as an IN check. ``range`` and ``custom`` have no
    library metric and no query to run, so they go out as ODCS ``text`` — the
    spec's "human-readable text that describes the quality of the data" — which
    is what the rest of this module already does for a constraint it cannot
    execute.
    """
    val_type = validation.get("type")
    constraint = validation.get("constraint")
    message = validation.get("message")
    if not constraint:
        return None

    if val_type == "regex":
        return {
            "type": "library",
            "metric": "invalidValues",
            "mustBe": 0,
            "arguments": {"pattern": constraint},
            "dimension": "conformity",
            "description": message or f"Field '{name}' must match pattern: {constraint}",
        }
    if val_type == "enum":
        # The constraint is a single string, so a comma-separated list is the
        # only reading available; a value with no comma is one allowed value.
        values = [v.strip().strip("'\"") for v in constraint.split(",")]
        values = [v for v in values if v]
        return {
            "type": "library",
            "metric": "invalidValues",
            "mustBe": 0,
            "arguments": {"validValues": values},
            "dimension": "conformity",
            "description": message or f"Field '{name}' must be one of: {', '.join(values)}",
        }
    if val_type == "range":
        return {
            "type": "text",
            "dimension": "conformity",
            "description": message or f"Field '{name}' must be within range: {constraint}",
        }
    return {
        "type": "text",
        "description": message or f"Field '{name}' must satisfy: {constraint}",
    }


def _validation_to_quality(
    validation: Mapping[str, Any],
    name: str,
    is_primary_key: bool,
    fld: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    val_type = validation.get("type", "")
    val_value = validation.get("value")
    val_values = validation.get("values")

    if val_type in ("pattern", "regex") and val_value:
        return {
            "type": "text",
            "description": f"Field '{name}' must match pattern: {val_value}",
        }
    if val_type == "min_length" and val_value is not None:
        return {
            "type": "text",
            "description": f"Field '{name}' must have minimum length of {val_value}",
        }
    if val_type == "max_length" and val_value is not None:
        return {
            "type": "text",
            "description": f"Field '{name}' must have maximum length of {val_value}",
        }
    if val_type == "min_value" and val_value is not None:
        return {
            "type": "text",
            "description": (f"Field '{name}' must be greater than or equal to {val_value}"),
        }
    if val_type == "max_value" and val_value is not None:
        return {
            "type": "text",
            "description": (f"Field '{name}' must be less than or equal to {val_value}"),
        }
    if val_type in ("allowed_values", "enum") and val_values:
        values_str = ", ".join(str(v) for v in val_values[:5])
        if len(val_values) > 5:
            values_str += f", ... ({len(val_values)} total)"
        return {
            "type": "text",
            "description": f"Field '{name}' must be one of: {values_str}",
        }
    if val_type == "not_null" and val_value and not fld.get("required"):
        return {
            "type": "library",
            "metric": "nullValues",
            "mustBe": 0,
            "dimension": "completeness",
            "description": f"Field '{name}' must not contain null values",
        }
    if val_type == "unique" and val_value and not is_primary_key:
        return {
            "type": "library",
            "metric": "duplicateValues",
            "mustBe": 0,
            "dimension": "uniqueness",
            "description": f"Field '{name}' must contain only unique values",
        }
    return None
