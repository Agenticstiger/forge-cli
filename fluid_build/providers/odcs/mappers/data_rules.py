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
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _exposes(contract: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {
        e["exposeId"]: e
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping) and isinstance(e.get("exposeId"), str)
    }


def export(ctx: ExportCtx, schema_objects: List[Any]) -> None:
    """Append the generated rules of every expose to its schema object."""
    from fluid_build.data_rules._compile import compile_rules

    # A per-expose export scopes the contract to one expose; a rule may read its siblings.
    whole = ctx.options.get("whole_contract")
    contract = whole if isinstance(whole, Mapping) else ctx.fluid
    rules = compile_rules(contract)
    if not rules:
        return
    exposes = _exposes(contract)
    unsupported: Dict[Tuple[str, str], List[str]] = {}
    for obj in schema_objects:
        if not isinstance(obj, dict) or obj.get("name") not in exposes:
            continue
        own = [rule for rule in rules if rule.expose == obj["name"]]
        if own:
            _attach(obj, exposes[obj["name"]], own, exposes, rules, unsupported)
    from .extras import _enabled

    kept = (
        "The declarations ride along under customProperties.fluidExtras."
        if _enabled()
        else "ODCS_FLUID_EXTRAS=false, so only the text rules record them."
    )
    for (expose_id, reason), ids in unsupported.items():
        ctx.logger.warning(
            "%d declared rule(s) on expose %r are exported as ODCS text rules, not executable "
            "quality rules (%s): %s. %s Run `fluid validate` on the contract, or bind the "
            "expose to a platform forge writes SQL for, to get sql rules.",
            len(ids),
            expose_id,
            ", ".join(ids),
            reason,
            kept,
        )


def _linked_without_table(rule: Any, exposes: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """The first expose ``rule`` reads whose binding names no table, if any: its query would
    have to guess the name."""
    for expose_id in rule.reads:
        if (
            expose_id != rule.expose
            and _location_name(rule.dialect, exposes.get(expose_id) or {}) is None
        ):
            return expose_id
    return None


def _attach(
    obj: Dict[str, Any],
    expose: Mapping[str, Any],
    rules: List[Any],
    exposes: Mapping[str, Mapping[str, Any]],
    every: List[Any],
    unsupported: Dict[Tuple[str, str], List[str]],
) -> None:
    from .quality import _append_quality, _claim, _existing_ids, _stable_id

    taken: Set[str] = _existing_ids(obj)
    properties = {
        p["name"]: p
        for p in obj.get("properties") or []
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    }
    fields = _fields(expose)

    for rule in rules:
        check: Dict[str, Any] = {
            "id": _claim(_stable_id(rule.id), taken),
            "name": rule.id,
            "dimension": rule.dimension,
            "severity": rule.severity,
        }
        status, reason = rule.status, rule.reason
        if status == "executable" and rule.pattern is None:
            unnamed = _linked_without_table(rule, exposes)
            if unnamed is not None:
                status = "unsupported"
                reason = (
                    f"it reads {unnamed}, whose binding.location names no table for the query "
                    "to read"
                )
        if status == "executable" and rule.pattern is not None:
            check.update(
                type="library",
                metric="invalidValues",
                mustBe=0,
                arguments={"pattern": rule.pattern},
                description=rule.description,
            )
            _add_pattern(properties.get(rule.column), rule.pattern)
        elif status == "executable":
            check.update(type="sql", query=rule.measure(_table_sql(rule, exposes)))
            check[rule.band.operator] = rule.band.value
            check["unit"] = rule.band.unit
            check["description"] = rule.description
        elif status == "informational":
            check.update(
                type="text",
                description=f"{rule.description}; declared, and no operator bounds it",
            )
        else:
            unsupported.setdefault((rule.expose, reason), []).append(rule.id)
            check.update(
                type="text",
                description=f"{rule.description}. Not executable here: {reason}",
            )
        check["customProperties"] = [dict(_MARK)]
        target = properties.get(rule.column) if rule.column else None
        _append_quality(target if target is not None else obj, check)

    for column, (logic, sources) in derived_logic(expose, every, exposes).items():
        prop, fld = properties.get(column), fields.get(column)
        if prop is None or fld is None:
            continue
        pt = get_field_passthrough(fld)
        if "transformLogic" not in prop and "transform_logic" not in pt:
            prop["transformLogic"] = logic
        if "transformSourceObjects" not in prop and "transform_source_objects" not in pt:
            prop["transformSourceObjects"] = sources


def _fields(expose: Mapping[str, Any]) -> Dict[str, Any]:
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    return {
        f["name"]: f
        for f in schema or []
        if isinstance(f, Mapping) and isinstance(f.get("name"), str)
    }


def _table_sql(rule: Any, exposes: Mapping[str, Mapping[str, Any]]) -> Any:
    """How ``rule``'s query names a table: ``${table}`` for its own object, the quoted
    binding location for a linked one (:func:`_linked_without_table` refuses the rest)."""

    def table_sql(expose_id: str) -> str:
        if expose_id == rule.expose:
            return "${table}"
        located = _location_name(rule.dialect, exposes.get(expose_id) or {})
        if located is None:  # pragma: no cover - refused before the query is built
            raise ValueError(f"{expose_id} names no table")
        return located

    return table_sql


def _add_pattern(prop: Optional[Dict[str, Any]], pattern: str) -> None:
    """``logicalTypeOptions.pattern`` on a string property that has no pattern yet. The
    options are copied: an imported property's options are its pass-through bucket."""
    if prop is None or prop.get("logicalType") != "string":
        return
    options = dict(prop.get("logicalTypeOptions") or {})
    if "pattern" in options:
        return
    options["pattern"] = pattern
    prop["logicalTypeOptions"] = options


def derived_logic(
    expose: Mapping[str, Any], rules: List[Any], exposes: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Tuple[str, List[str]]]:
    """Each derived column's ``transformLogic`` and ``transformSourceObjects``: its one
    ``expr``, or the first-match ``CASE`` of several, in canonical FLUID SQL/1, and the
    objects it reads. A column with a summary, or a derive with no exact SQL, has none."""
    from fluid_build import fluid_sql as fs

    contract = expose.get("contract")
    derived: Dict[str, List[Mapping[str, Any]]] = {}
    for inv in (contract.get("invariants") if isinstance(contract, Mapping) else None) or []:
        if isinstance(inv, Mapping) and isinstance(inv.get("derive"), str):
            derived.setdefault(inv["derive"], []).append(inv)
    own = expose.get("exposeId")
    ok = {r.id: r for r in rules if r.expose == own and r.status == "executable"}
    out: Dict[str, Tuple[str, List[str]]] = {}
    for column, invs in derived.items():
        if any(not isinstance(i.get("expr"), str) or i.get("id") not in ok for i in invs):
            continue
        try:
            values = [fs.canonical(fs.parse(i["expr"])) for i in invs]
            whens = [
                fs.canonical(fs.parse(i["when"])) if isinstance(i.get("when"), str) else None
                for i in invs
            ]
        except fs.FluidSqlError:
            continue
        if len(values) == 1 and whens[0] is None:
            logic = values[0]
        else:
            arms = " ".join(
                f"WHEN {when or 'TRUE'} THEN {value}"
                for when, value in zip(whens, values, strict=True)
            )
            logic = f"CASE {arms} ELSE {fs.quote_name(column)} END"
        sources = [own]
        for inv in invs:
            sources += [r for r in ok[inv["id"]].reads if r in exposes]
        out[column] = (logic, [str(s) for s in dict.fromkeys(sources)])
    return out


def strip_imported(fluid: Mapping[str, Any]) -> None:
    """Drop what the export generated from an import whose declarations ``fluidExtras``
    restored, so a re-export generates it again from the declarations as they are then:
    the marked rules (each field's ``quality``, each expose's object quality), and a
    ``logicalTypeOptions.pattern``, ``transformLogic`` or ``transformSourceObjects`` the
    restored declarations generate exactly."""
    from fluid_build.data_rules._compile import compile_rules

    rules = compile_rules(fluid)
    exposes = _exposes(fluid)
    patterns = {(r.expose, r.column): r.pattern for r in rules if r.pattern is not None}
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        pt = get_expose_passthrough(expose)
        if isinstance(pt.get("object_quality"), list):
            pt["object_quality"] = strip_generated(pt["object_quality"])
        logic = derived_logic(expose, rules, exposes) if rules else {}
        for name, fld in _fields(expose).items():
            if isinstance(fld, dict) and isinstance(fld.get("quality"), list):
                fld["quality"] = strip_generated(fld["quality"])
            fpt = get_field_passthrough(fld)
            if not isinstance(fpt, dict):
                continue
            options = fpt.get("logical_type_options")
            pattern = patterns.get((expose.get("exposeId"), name))
            if isinstance(options, dict) and pattern and options.get("pattern") == pattern:
                options = {k: v for k, v in options.items() if k != "pattern"}
                if options:
                    fpt["logical_type_options"] = options
                else:
                    fpt.pop("logical_type_options")
            if name in logic:
                text, sources = logic[name]
                if fpt.get("transform_logic") == text:
                    fpt.pop("transform_logic")
                if fpt.get("transform_source_objects") == sources:
                    fpt.pop("transform_source_objects")
