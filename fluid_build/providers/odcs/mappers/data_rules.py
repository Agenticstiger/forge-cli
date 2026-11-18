# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Declared invariants, identifier formats, placeholder values and shared attributes as
ODCS quality rules (``RFC-data-rules.md``, Consumer impact matrix).

:func:`~fluid_build.data_rules._compile.compile_rules` renders each declaration in its
expose's platform dialect; this module places the result in the ODCS document:

- an invariant, a ``sharedColumns`` entry and a shared attribute: a ``sql`` rule on the
  schema object, whose ``query`` returns the violating rows (``unit: rows``) or violating
  rows times 100 divided by covered rows (``unit: percent``), with the invariant's own
  operator and unit; ``${table}`` names the rule's own object, and a linked expose is
  named by its quoted binding location;
- an identifier format: ``logicalTypeOptions.pattern`` and a library ``invalidValues``
  rule with ``arguments.pattern`` on the property, both accepting the placeholders;
- placeholders: a ``sql`` rule on the property counting placeholder rows when an
  operator bounds them, and a ``text`` rule when none does;
- a derived column's ``transformLogic`` (its canonical FLUID SQL/1 value) and
  ``transformSourceObjects``, where the property does not carry its own already.

A declaration with no exact SQL (no dialect for the platform, a link to another
platform, a declaration the validator refuses) becomes a ``text`` rule saying why, and
a logged warning: it is reported, never dropped and never approximated. The
declaration itself rides in ``fluidExtras`` either way.

Every generated rule carries ``customProperties`` ``{property: fluidGenerated, value:
true}``. An import that restores the declarations from ``fluidExtras`` drops the marked
rules (:func:`strip_generated`), so a generated rule never comes back as a hand-written
one; with ``ODCS_FLUID_EXTRAS=false`` there is nothing to restore, and they stay
ordinary rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Set

from .base import ExportCtx, get_expose_passthrough, get_field_passthrough

#: The ``customProperties`` key that marks a rule this module wrote.
FLUID_GENERATED_PROPERTY = "fluidGenerated"
_MARK = {"property": FLUID_GENERATED_PROPERTY, "value": True}


def is_generated(rule: Any) -> bool:
    """Whether ``rule`` is an ODCS quality rule this module wrote."""
    if not isinstance(rule, Mapping):
        return False
    return any(
        isinstance(p, Mapping)
        and p.get("property") == FLUID_GENERATED_PROPERTY
        and p.get("value") is True
        for p in rule.get("customProperties") or []
    )


def strip_generated(rules: Any) -> Any:
    """``rules`` without the rules this module wrote (anything else unchanged)."""
    if not isinstance(rules, list):
        return rules
    return [rule for rule in rules if not is_generated(rule)]


def _location_name(dialect: str, expose: Mapping[str, Any]) -> Optional[str]:
    """A linked expose's table, quoted for ``dialect``: its binding's catalog or project or
    database, schema or dataset, and table, as far as the binding names them."""
    from fluid_build import fluid_sql as fs

    binding = expose.get("binding")
    location = binding.get("location") if isinstance(binding, Mapping) else None
    if not isinstance(location, Mapping) or not isinstance(location.get("table"), str):
        return None
    parts: List[str] = []
    for keys in (("project", "catalog", "database"), ("dataset", "schema")):
        for key in keys:
            if isinstance(location.get(key), str) and location[key]:
                parts.append(location[key])
                break
    parts.append(location["table"])
    return ".".join(fs.sql_name(dialect, part) for part in parts)


def export(ctx: ExportCtx, schema_objects: List[Any]) -> None:
    """Append the generated rules of every expose to its schema object."""
    from fluid_build.data_rules._compile import compile_rules

    # A per-expose export scopes the contract to one expose; a rule may read its siblings.
    whole = ctx.options.get("whole_contract")
    contract = whole if isinstance(whole, Mapping) else ctx.fluid
    rules = compile_rules(contract)
    if not rules:
        return
    exposes: Dict[str, Mapping[str, Any]] = {}
    for expose in contract.get("exposes") or []:
        if isinstance(expose, Mapping) and isinstance(expose.get("exposeId"), str):
            exposes[expose["exposeId"]] = expose
    for obj in schema_objects:
        if not isinstance(obj, dict) or obj.get("name") not in exposes:
            continue
        own = [rule for rule in rules if rule.expose == obj["name"]]
        if own:
            _attach(ctx, obj, exposes[obj["name"]], own, exposes, rules)


def _attach(
    ctx: ExportCtx,
    obj: Dict[str, Any],
    expose: Mapping[str, Any],
    rules: List[Any],
    exposes: Mapping[str, Mapping[str, Any]],
    every: List[Any],
) -> None:
    from fluid_build import fluid_sql as fs

    from .quality import _append_quality, _claim, _existing_ids, _stable_id

    taken: Set[str] = _existing_ids(obj)
    properties = {
        p["name"]: p
        for p in obj.get("properties") or []
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    }
    fields = {
        f["name"]: f
        for f in (expose.get("contract") or {}).get("schema") or []
        if isinstance(f, Mapping) and isinstance(f.get("name"), str)
    }

    for rule in rules:
        check: Dict[str, Any] = {
            "id": _claim(_stable_id(rule.id), taken),
            "name": rule.id,
            "dimension": rule.dimension,
            "severity": rule.severity,
        }
        if rule.status == "executable" and rule.pattern is not None:
            check.update(
                type="library",
                metric="invalidValues",
                mustBe=0,
                arguments={"pattern": rule.pattern},
                description=rule.description,
            )
            _add_pattern(properties.get(rule.column), fields.get(rule.column), rule.pattern)
        elif rule.status == "executable":
            check.update(type="sql", query=rule.measure(_table_sql(rule, exposes)))
            check[rule.band.operator] = rule.band.value
            check["unit"] = rule.band.unit
            check["description"] = rule.description
        elif rule.status == "informational":
            check.update(
                type="text",
                description=f"{rule.description}; declared, and no operator bounds it",
            )
        else:
            ctx.logger.warning(
                "%s %r on expose %r is not exported as an executable ODCS quality rule: %s. "
                "It is written as a text rule, and the declaration rides along under "
                "customProperties.fluidExtras.",
                rule.kind,
                rule.id,
                rule.expose,
                rule.reason,
            )
            check.update(
                type="text",
                description=f"{rule.description}. Not executable here: {rule.reason}",
            )
        check["customProperties"] = [dict(_MARK)]
        target = properties.get(rule.column) if rule.column else None
        _append_quality(target if target is not None else obj, check)

    _transform_logic(expose, properties, fields, every, exposes)


def _table_sql(rule: Any, exposes: Mapping[str, Mapping[str, Any]]) -> Any:
    """How ``rule``'s query names a table: ``${table}`` for its own object, the quoted
    binding location (else the quoted exposeId) for a linked one."""
    from fluid_build import fluid_sql as fs

    def table_sql(expose_id: str) -> str:
        if expose_id == rule.expose:
            return "${table}"
        located = _location_name(rule.dialect, exposes.get(expose_id) or {})
        return located or fs.sql_name(rule.dialect, expose_id)

    return table_sql


def _add_pattern(
    prop: Optional[Dict[str, Any]], fld: Optional[Mapping[str, Any]], pattern: str
) -> None:
    """``logicalTypeOptions.pattern`` on a string property, unless the property carries its
    own options verbatim (an imported column) or a pattern already."""
    if prop is None or fld is None or prop.get("logicalType") != "string":
        return
    if "logical_type_options" in get_field_passthrough(fld):
        return
    options = dict(prop.get("logicalTypeOptions") or {})
    if "pattern" in options:
        return
    options["pattern"] = pattern
    prop["logicalTypeOptions"] = options


def _transform_logic(
    expose: Mapping[str, Any],
    properties: Mapping[str, Dict[str, Any]],
    fields: Mapping[str, Mapping[str, Any]],
    rules: List[Any],
    exposes: Mapping[str, Mapping[str, Any]],
) -> None:
    """A derived column's value as ``transformLogic``: its one ``expr``, or the first-match
    ``CASE`` of several, in canonical FLUID SQL/1; and the objects it reads."""
    from fluid_build import fluid_sql as fs

    derived: Dict[str, List[Mapping[str, Any]]] = {}
    for inv in (expose.get("contract") or {}).get("invariants") or []:
        if isinstance(inv, Mapping) and isinstance(inv.get("derive"), str):
            derived.setdefault(inv["derive"], []).append(inv)
    ok = {r.id for r in rules if r.expose == expose.get("exposeId") and r.status == "executable"}
    for column, invs in derived.items():
        prop, fld = properties.get(column), fields.get(column)
        if prop is None or fld is None:
            continue
        pt = get_field_passthrough(fld)
        if any(not isinstance(i.get("expr"), str) or i.get("id") not in ok for i in invs):
            continue  # a summary, or a declaration with no exact SQL: no expression to show
        try:
            values = [(i.get("when"), fs.canonical(fs.parse(i["expr"]))) for i in invs]
            whens = [None if w is None else fs.canonical(fs.parse(w)) for w, _ in values]
        except fs.FluidSqlError:
            continue
        if len(values) == 1 and whens[0] is None:
            logic = values[0][1]
        else:
            arms = " ".join(
                f"WHEN {when or 'TRUE'} THEN {value}"
                for when, (_, value) in zip(whens, values, strict=True)
            )
            logic = f"CASE {arms} ELSE {fs.quote_name(column)} END"
        if "transformLogic" not in prop and "transform_logic" not in pt:
            prop["transformLogic"] = logic
        if "transformSourceObjects" not in prop and "transform_source_objects" not in pt:
            sources = [expose.get("exposeId")]
            for rule in rules:
                if rule.id in {i.get("id") for i in invs}:
                    sources += [r for r in rule.reads if r in exposes]
            prop["transformSourceObjects"] = [str(s) for s in dict.fromkeys(sources)]


def strip_imported(fluid: Mapping[str, Any]) -> None:
    """Drop the generated rules from an import whose declarations ``fluidExtras`` restored:
    each field's ``quality`` and each expose's pass-through object quality."""
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        pt = get_expose_passthrough(expose)
        if isinstance(pt.get("object_quality"), list):
            pt["object_quality"] = strip_generated(pt["object_quality"])
        contract = expose.get("contract")
        if not isinstance(contract, Mapping):
            continue
        for fld in contract.get("schema") or []:
            if isinstance(fld, dict) and isinstance(fld.get("quality"), list):
                fld["quality"] = strip_generated(fld["quality"])
