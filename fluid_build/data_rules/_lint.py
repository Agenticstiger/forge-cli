# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The v1 validator lint of ``RFC-data-rules.md`` ("Validator lint (v1)").

Errors, each failing ``fluid validate``:

1. a new field below 0.7.7 (the schema, with ``schema_manager``'s preview hint);
2. an invariant ``id`` used twice in the contract, counting ``dq.rules[].id`` and
   ``contract.quality[].rule``;
3. an entry with no form, or two (the schema's ``oneOf``, with
   ``schema_manager``'s form message);
4. to 7. a ``check``, ``expr``, ``when`` or ``aggregate.filter`` outside FLUID
   SQL/1, with an unknown name, a type error, or a value that does not fit its
   ``derive`` target (:mod:`fluid_build.fluid_sql`);
8. a cycle among derived columns, within an expose or across them;
9. a column with two sources;
10. an ``aggregate`` whose ``from``, ``via``, ``function``, ``column``, ``filter``
    or ``ifNone`` does not fit;
11. a ``dependency`` with an unknown column, a column on both sides, or
    ``bothWays`` with more than one column a side;
12. more than one share operator, a percentage above 100, bounds that do not
    ascend, or a number of more than 15 significant digits;
13. a bad mask or prefix, a format on a column that is not a string, overlapping
    alternatives, or a foreign key whose format differs from its target's;
14. placeholders on a column that is not a string, or with no value or prefix;
15. a ``sharedColumns`` entry naming a key column, a column missing on either
    end, or columns of different types;
16. a ``sharedAttribute`` group with no owner, two candidates, two paths, one
    expose twice, or mixed types.

And notes, never failing: same-named columns on the two ends of a relationship
that nothing relates; a ``/`` or float arithmetic (exact here, approximate in
engines); a column with both a format and a regex rule; and a
``contract.quality[]`` expression that is FLUID SQL/1 and could move to
``invariants``.
"""

from __future__ import annotations

import datetime
import difflib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from fluid_build import fluid_sql as fs
from fluid_build._validation_issues import json_pointer
from fluid_build.fluid_sql._types import NUMBER, ORDERABLE, STRING

from . import _masks
from ._model import Expose, Relationship, as_dict, as_list, build, by_id

OPERATORS = (
    "mustBe",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeBetween",
)
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_GRAMMAR_REASONS = frozenset(
    {"syntax", "comment-or-semicolon", "too-long", "too-deep", "bad-date", "multi-link-path"}
)
_FIXES = {
    "unknown-column": "name a column of the expose, or rel.col for a relationship's target, spelled "
    "as contract.schema[].name spells it",
    "unknown-relationship": "name one of the expose's semantics.relationships[].name",
    "validity-window": "FLUID SQL/1 v1 reads no slowly changing dimension through a relationship",
    "untyped-column": "name a column of a FLUID SQL/1 type",
    "type-mismatch": "compare and combine values of one type",
    "null-literal": "write x IS NULL, or give the NULL literal a typed operand beside it",
    "enum-literal": "use one of the column's enum values",
    "derive-target": "declare the derived column decimal(p,s), or another exact type",
    "derive-type": "derive a value of the column's own type",
    "derive-scale": "wrap the value in ROUND(x, s) with the column's scale",
}
_GRAMMAR_FIX = (
    "write the rule in FLUID SQL/1 (RFC-data-rules.md section 2), or keep it in "
    "contract.quality[], where SQL outside the subset belongs"
)

Node = Tuple[str, str]  # (exposeId, column)


@dataclass(frozen=True)
class Finding:
    """One lint finding: ``severity`` is ``error`` or ``note``; ``path`` locates it."""

    severity: str
    label: str
    path: Tuple[Any, ...]
    message: str
    fix: str = ""
    #: Whether the printed text repeats ``fix``: a refused text's message already says it.
    show_fix: bool = True

    @property
    def text(self) -> str:
        return f"{self.label}: {self.message}"

    @property
    def display(self) -> str:
        """The line ``fluid validate`` prints."""
        return f"{self.text}; {self.fix}" if self.fix and self.show_fix else self.text

    @property
    def pointer(self) -> str:
        return json_pointer(list(self.path))


def lint(contract: Mapping[str, Any]) -> List[Finding]:
    """Every data-rules finding for ``contract``, in contract order."""
    return _Lint(contract).run()


def _hint(name: Any, choices: Iterable[str]) -> str:
    if not isinstance(name, str):
        return ""
    close = difflib.get_close_matches(name, list(choices), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _significant_digits(value: float) -> int:
    digits = Decimal(repr(value)).normalize().as_tuple().digits
    return len(digits)


def _literal_scale(value: Any) -> int:
    exponent = Decimal(repr(value)).normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_date_text(value: Any) -> bool:
    """A calendar date written YYYY-MM-DD (a DATE literal's text)."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


class _Lint:
    def __init__(self, contract: Mapping[str, Any]) -> None:
        self.contract = contract
        self.exposes = build(contract)
        self.by_id = by_id(self.exposes)
        self.findings: List[Finding] = []
        self.derives: DefaultDict[Node, List[str]] = defaultdict(list)
        self.sources: DefaultDict[Node, List[str]] = defaultdict(list)
        self.edges: DefaultDict[Node, Set[Node]] = defaultdict(set)
        # (exposeId, relationship, column): an invariant reads the column on both ends.
        self.related: Set[Tuple[str, str, str]] = set()
        self.attribute_of: Dict[Node, str] = {}

    def run(self) -> List[Finding]:
        self.ids()
        for expose in self.exposes:
            self.invariants(expose)
            self.columns(expose)
            self.relationships(expose)
        self.shared_attributes()
        self.one_source()
        self.cycles()
        self.same_named_columns()
        self.quality_notes()
        return self.findings

    # -- reporting -------------------------------------------------------------

    def error(
        self, label: str, path: Tuple[Any, ...], message: str, fix: str = "", show_fix: bool = True
    ) -> None:
        self.findings.append(Finding("error", label, path, message, fix, show_fix))

    def note(self, label: str, path: Tuple[Any, ...], message: str, fix: str = "") -> None:
        self.findings.append(Finding("note", label, path, message, fix))

    def node(self, expose: Expose, relationship: Optional[str], column: str) -> Optional[Node]:
        if relationship is None:
            return (expose.expose_id, column)
        rel = expose.relationships.get(relationship)
        return (rel.to, column) if rel else None

    # -- 2. ids ----------------------------------------------------------------

    def ids(self) -> None:
        """Every invariant id that another rule of the contract also uses.

        All ids are collected first, in contract order, so a clash is found wherever the
        other rule sits. Each clash is reported once, on an invariant: the later of two
        invariants, or the invariant itself when the other rule is a dq or quality rule. A
        dq rule or quality rule that only repeats another of its kind is not reported here.
        """
        for rule_id, entries in self.rule_ids().items():
            invariants = [entry for entry in entries if entry[2]]
            if len(entries) < 2 or not invariants:
                continue
            first = entries[0]
            for entry in invariants:
                if entry is first and len(invariants) > 1:
                    continue  # the later invariant names this one
                other = first if entry is not first else entries[1]
                self.error(
                    entry[0],
                    entry[1],
                    f"the id {rule_id!r} is also {other[0]}'s: ids are unique within the "
                    "contract, across exposes, dq.rules and contract.quality, because reports, "
                    "tests and ODCS rules are keyed by them",
                    "give the invariant an id of its own",
                )

    def rule_ids(self) -> Dict[str, List[Tuple[str, Tuple[Any, ...], bool]]]:
        """Every rule id of the contract, in contract order: ``id -> [(label, path,
        is_invariant)]`` over ``dq.rules[].id``, ``contract.quality[].rule`` and the
        invariants' ids."""
        groups: DefaultDict[str, List[Tuple[str, Tuple[Any, ...], bool]]] = defaultdict(list)
        for expose in self.exposes:
            contract = expose.contract
            for rule in as_list(as_dict(contract.get("dq")).get("rules")):
                if isinstance(as_dict(rule).get("id"), str):
                    label = f"{expose.label}.contract.dq.rules[{rule['id']}]"
                    groups[rule["id"]].append((label, (), False))
            for rule in as_list(contract.get("quality")):
                if isinstance(as_dict(rule).get("rule"), str):
                    label = f"{expose.label}.contract.quality[{rule['rule']}]"
                    groups[rule["rule"]].append((label, (), False))
            for j, inv in enumerate(as_list(contract.get("invariants"))):
                inv_id = as_dict(inv).get("id")
                if isinstance(inv_id, str) and _ID.match(inv_id):
                    path = ("exposes", expose.index, "contract", "invariants", j, "id")
                    label = f"{expose.label}.contract.invariants[{inv_id}]"
                    groups[inv_id].append((label, path, True))
        return groups

    # -- invariants --------------------------------------------------------------

    def invariants(self, expose: Expose) -> None:
        for j, inv in enumerate(as_list(expose.contract.get("invariants"))):
            if not isinstance(inv, Mapping):
                continue
            raw_id = inv.get("id")
            inv_id = raw_id if isinstance(raw_id, str) and _ID.match(raw_id) else ""
            label = f"{expose.label}.contract.invariants[{inv_id or j}]"
            path = ("exposes", expose.index, "contract", "invariants", j)
            forms = [key for key in ("check", "derive", "dependency") if key in inv]
            if len(forms) != 1:
                continue  # the schema's oneOf reports it
            form = forms[0]
            reads: Set[Node] = set()
            if "when" in inv and form != "dependency":
                checked = self.text(
                    expose, expose.model, inv["when"], fs.PREDICATE, label, path, "when"
                )
                reads |= self.reads_of(expose, checked)
            if form == "check":
                checked = self.text(
                    expose, expose.model, inv["check"], fs.PREDICATE, label, path, "check"
                )
                self.note_related(expose, checked)
            elif form == "derive":
                self.derive(expose, inv, label, path, inv_id or label, reads)
            else:
                self.dependency(expose, inv, label, path, inv_id or label)
            self.band(inv, label, path)

    def text(
        self,
        expose: Expose,
        model: fs.Model,
        text: Any,
        site: str,
        label: str,
        path: Tuple[Any, ...],
        key: str,
        derive: Optional[fs.Column] = None,
    ) -> Optional[fs.Checked]:
        """Check one FLUID SQL/1 text, reporting a refusal or an approximation."""
        if not isinstance(text, str) or not text:
            return None  # the schema reports it
        where, at = f"{label}.{key}", path + (key,)
        try:
            checked = fs.check(fs.parse(text), model, site, derive)
        except fs.FluidSqlError as refused:
            at_char = "" if refused.position is None else f" at character {refused.position + 1}"
            if refused.reason in _GRAMMAR_REASONS:
                self.error(
                    where, at, f"not FLUID SQL/1{at_char}: {refused.detail}", _GRAMMAR_FIX, False
                )
            elif refused.reason == "derive-target":
                self.error(
                    f"{label}.derive",
                    path + ("derive",),
                    refused.detail,
                    _FIXES[refused.reason],
                    False,
                )
            else:
                suffix = f" ({at_char.strip()})" if at_char else ""
                self.error(
                    where, at, refused.detail + suffix, _FIXES.get(refused.reason, ""), False
                )
            return None
        if checked.division:
            self.note(
                where,
                at,
                "a / outside ROUND is exact in FLUID SQL/1 and approximate in SQL engines, which "
                "divide at their own precision (DuckDB in DOUBLE): a consumer can disagree on a "
                "row whose quotient lies within that precision of the boundary",
            )
        if checked.float_arithmetic:
            self.note(
                where,
                at,
                "arithmetic on a float column is exact in FLUID SQL/1 (it reads the shortest "
                "decimal of each double) and binary in SQL engines",
            )
        return checked

    def reads_of(self, expose: Expose, checked: Optional[fs.Checked]) -> Set[Node]:
        nodes: Set[Node] = set()
        for relationship, column in checked.reads if checked else ():
            node = self.node(expose, relationship, column)
            if node is not None:
                nodes.add(node)
        return nodes

    def note_related(self, expose: Expose, checked: Optional[fs.Checked]) -> None:
        """Remember a same-named pair an invariant relates (for the same-names note)."""
        if checked is None:
            return
        own = {column for relationship, column in checked.reads if relationship is None}
        for relationship, column in checked.reads:
            if relationship is not None and column in own:
                self.related.add((expose.expose_id, relationship, column))

    def derive(
        self,
        expose: Expose,
        inv: Mapping[str, Any],
        label: str,
        path: Tuple[Any, ...],
        name: str,
        reads: Set[Node],
    ) -> None:
        target_name = inv.get("derive")
        target = expose.model.columns.get(target_name) if isinstance(target_name, str) else None
        if target is None:
            self.error(
                f"{label}.derive",
                path + ("derive",),
                f"{target_name!r} is not a column of {expose.label}"
                + _hint(target_name, expose.model.columns),
                "name the column of contract.schema that the invariant determines",
            )
        if "expr" in inv:
            checked = self.text(
                expose, expose.model, inv["expr"], fs.VALUE, label, path, "expr", derive=target
            )
            reads |= self.reads_of(expose, checked)
            if target is not None and checked is not None:
                for relationship, column in checked.reads:
                    if relationship is not None and column == target.name:
                        self.related.add((expose.expose_id, relationship, column))
        else:
            summary = self.aggregate(expose, as_dict(inv.get("aggregate")), label, path, reads)
            if summary is not None and target is not None:
                try:
                    fs.check_derive_fit(target, summary)
                except fs.FluidSqlError as refused:
                    self.error(
                        f"{label}.aggregate",
                        path + ("aggregate",),
                        refused.detail,
                        _FIXES[refused.reason],
                        False,
                    )
        if target is not None:
            node = (expose.expose_id, target.name)
            self.derives[node].append(name)
            self.edges[node] |= reads

    def aggregate(
        self,
        parent: Expose,
        agg: Mapping[str, Any],
        label: str,
        path: Tuple[Any, ...],
        reads: Set[Node],
    ) -> Optional[fs.SqlType]:
        label, path = f"{label}.aggregate", path + ("aggregate",)
        child = self.by_id.get(agg.get("from")) if isinstance(agg.get("from"), str) else None
        if child is None:
            self.error(
                f"{label}.from",
                path + ("from",),
                f"{agg.get('from')!r} is not an expose of the contract"
                + _hint(agg.get("from"), self.by_id),
                "name the child expose by its exposeId",
            )
            return None
        via = agg.get("via")
        rel = child.relationships.get(via) if isinstance(via, str) else None
        if rel is None:
            self.error(
                f"{label}.via",
                path + ("via",),
                f"{via!r} is not a relationship of {child.label}" + _hint(via, child.relationships),
                "name the child's relationship (semantics.relationships[].name) that points here",
            )
            return None
        if rel.to != parent.expose_id:
            self.error(
                f"{label}.via",
                path + ("via",),
                f"{child.label}'s relationship {rel.name} goes to {rel.to!r}, not to {parent.label}: "
                "a summary counts the children whose relationship points to the parent",
                "name a relationship of the child whose to is this expose",
            )
            return None
        if parent.model.validity_window:
            self.error(
                f"{label}.via",
                path + ("via",),
                f"{parent.label} declares a validity window: FLUID SQL/1 v1 summarizes no slowly "
                "changing dimension through a relationship",
            )
            return None
        function = agg.get("function")
        column_name = agg.get("column")
        summary: Optional[fs.SqlType]
        if function == "count":
            if column_name is not None:
                self.error(
                    f"{label}.column",
                    path + ("column",),
                    "count takes no column: it counts the children for which filter is TRUE",
                    "leave column out, or summarize it with sum, min or max",
                )
                return None
            summary = fs.SqlType(NUMBER, 0)
        elif function in ("sum", "min", "max"):
            column = child.model.columns.get(column_name) if isinstance(column_name, str) else None
            if column is None:
                self.error(
                    f"{label}.column",
                    path + ("column",),
                    f"{function} needs a column of {child.label} to summarize; "
                    f"{column_name!r} is none" + _hint(column_name, child.model.columns),
                )
                return None
            if column.type is None:
                self.error(
                    f"{label}.column",
                    path + ("column",),
                    f"{column.name} ({column.declared}) has no FLUID SQL/1 type, so nothing can "
                    "summarize it",
                )
                return None
            kind = column.type.type.kind
            if function == "sum" and (kind != NUMBER or column.type.type.scale is None):
                self.error(
                    f"{label}.column",
                    path + ("column",),
                    f"sum takes a number of bounded scale; {column.name} is {column.declared}",
                    "sum a decimal(p,s) or integer column",
                )
                return None
            if function in ("min", "max") and kind not in ORDERABLE:
                self.error(
                    f"{label}.column",
                    path + ("column",),
                    f"{function} takes a number, string, date or timestamp; {column.name} is a {kind}",
                )
                return None
            summary = column.type.type
            reads.add((child.expose_id, column.name))
        else:
            return None  # the schema's enum reports it
        if "filter" in agg:
            checked = self.text(
                child, child.model, agg["filter"], fs.PREDICATE, label, path, "filter"
            )
            reads |= self.reads_of(child, checked)
        if "ifNone" in agg:
            summary = self.if_none(agg["ifNone"], summary, label, path)
        return summary

    def if_none(
        self, value: Any, summary: fs.SqlType, label: str, path: Tuple[Any, ...]
    ) -> Optional[fs.SqlType]:
        where, at = f"{label}.ifNone", path + ("ifNone",)
        if summary.kind == NUMBER:
            if not _is_number(value):
                self.error(
                    where, at, f"the summary is a number, so ifNone is a number, not {value!r}"
                )
                return None
            scale = _literal_scale(value)
            if summary.scale is None:
                return summary
            return fs.SqlType(NUMBER, max(summary.scale, scale))
        if summary.kind == STRING:
            if not isinstance(value, str):
                self.error(
                    where, at, f"the summary is a string, so ifNone is a string, not {value!r}"
                )
                return None
            return summary
        if summary.kind == "date":
            if not _is_date_text(value):
                self.error(
                    where, at, f"the summary is a date, so ifNone is 'YYYY-MM-DD', not {value!r}"
                )
                return None
            return summary
        self.error(
            where,
            at,
            f"the summary is a {summary.kind}, and FLUID SQL/1 has no {summary.kind} literal",
            "leave ifNone out",
        )
        return None

    def dependency(
        self, expose: Expose, inv: Mapping[str, Any], label: str, path: Tuple[Any, ...], name: str
    ) -> None:
        dep = as_dict(inv.get("dependency"))
        sides = {
            side: [c for c in as_list(dep.get(side)) if isinstance(c, str)]
            for side in ("from", "to")
        }
        known = True
        for side, columns in sides.items():
            for column in columns:
                if column not in expose.model.columns:
                    known = False
                    self.error(
                        f"{label}.dependency.{side}",
                        path + ("dependency", side),
                        f"{column!r} is not a column of {expose.label}"
                        + _hint(column, expose.model.columns),
                    )
        both = [c for c in sides["from"] if c in sides["to"]]
        if both:
            self.error(
                f"{label}.dependency",
                path + ("dependency",),
                f"{', '.join(both)} on both sides: a column always determines itself",
                "list each column on one side",
            )
        if dep.get("bothWays") is True and (len(sides["from"]) != 1 or len(sides["to"]) != 1):
            self.error(
                f"{label}.dependency.bothWays",
                path + ("dependency", "bothWays"),
                "bothWays is a one-to-one map, so it takes one column on each side",
            )
        if not known or both:
            return
        for column in sides["to"]:
            node = (expose.expose_id, column)
            self.sources[node].append(f"the to side of dependency {name}")
            self.edges[node] |= {(expose.expose_id, c) for c in sides["from"]}

    # -- 12. shares --------------------------------------------------------------

    def band(self, entry: Mapping[str, Any], label: str, path: Tuple[Any, ...]) -> None:
        present = [op for op in OPERATORS if op in entry]
        if len(present) > 1:
            self.error(
                label,
                path,
                f"{' and '.join(present)}: at most one share operator, so the band is one range",
                "keep one operator (mustBeBetween for a range)",
            )
            return
        if not present:
            return
        op = present[0]
        raw = entry[op]
        values = raw if op == "mustBeBetween" else [raw]
        if not isinstance(values, list) or not all(_is_number(v) for v in values):
            return  # the schema reports it
        percent = entry.get("unit") == "percent"
        for value in values:
            if value < 0:
                return  # the schema's minimum reports it
            if percent and value > 100:
                self.error(
                    f"{label}.{op}", path + (op,), f"{value} percent: a percentage is at most 100"
                )
            if _significant_digits(value) > 15:
                self.error(
                    f"{label}.{op}",
                    path + (op,),
                    f"{value!r} has more than 15 significant digits, so its text would not survive "
                    "a round trip through a double",
                )
        if op == "mustBeBetween" and len(values) == 2 and not values[0] < values[1]:
            self.error(
                f"{label}.mustBeBetween",
                path + ("mustBeBetween",),
                f"[{values[0]}, {values[1]}] does not ascend: mustBeBetween is [lo, hi] with lo below hi",
            )

    # -- 13, 14. columns -----------------------------------------------------------

    def columns(self, expose: Expose) -> None:
        for name, (position, column) in expose.columns.items():
            label = expose.column_label(name)
            path = ("exposes", expose.index, "contract", "schema", position)
            self.column_rules(column, label, path, nested=False)
            self.nested(column, label, path)

    def nested(self, column: Mapping[str, Any], label: str, path: Tuple[Any, ...]) -> None:
        for position, field in enumerate(as_list(column.get("fields"))):
            if not isinstance(field, Mapping):
                continue
            name = field.get("name") if isinstance(field.get("name"), str) else str(position)
            where = f"{label}.fields[{name}]"
            at = path + ("fields", position)
            self.column_rules(field, where, at, nested=True)
            self.nested(field, where, at)

    def column_rules(
        self, column: Mapping[str, Any], label: str, path: Tuple[Any, ...], nested: bool
    ) -> None:
        declared = column.get("type") if isinstance(column.get("type"), str) else "?"
        column_type = None if column.get("fields") else fs.column_type(declared)
        is_string = column_type is not None and column_type.type.kind == STRING
        if isinstance(column.get("identifierFormat"), list):
            self.formats(column, declared, is_string, label, path)
        if isinstance(column.get("placeholders"), Mapping):
            self.placeholders(column["placeholders"], declared, is_string, label, path)
        if nested and "sharedAttribute" in column:
            self.error(
                f"{label}.sharedAttribute",
                path + ("sharedAttribute",),
                "a nested field carries no shared attribute: no expression or relationship can "
                "reach it",
            )

    def formats(
        self,
        column: Mapping[str, Any],
        declared: str,
        is_string: bool,
        label: str,
        path: Tuple[Any, ...],
    ) -> None:
        """Item 13 on one column: a string column, masks in the grammar, no overlap."""
        where, at = f"{label}.identifierFormat", path + ("identifierFormat",)
        if not is_string:
            self.error(
                where,
                at,
                f"only a string column takes an identifier format; this one is {declared}",
            )
        formats = column["identifierFormat"]
        alternatives = []
        for k, entry in enumerate(formats):
            try:
                alternatives.append(_masks.alternative(entry))
            except _masks.MaskError as bad:
                self.error(f"{where}[{k}]", at + (k,), str(bad))
        if len(alternatives) == len(formats):
            clash = _masks.overlap(alternatives)
            if clash is not None:
                i, j, example = clash
                self.error(
                    where,
                    at,
                    f"alternatives {i} and {j} overlap: {example!r} has both formats, so a value "
                    "would not say which alternative made it",
                    "make the alternatives differ in a literal, or in length",
                )
        if any(
            str(as_dict(r).get("type") or "").strip().lower() == "regex"
            for r in as_list(column.get("validationRules"))
        ):
            self.note(
                where,
                at,
                "the column has both an identifier format and a regex rule: both apply, and only "
                "the format can be enumerated by a generator",
            )

    def placeholders(
        self,
        block: Mapping[str, Any],
        declared: str,
        is_string: bool,
        label: str,
        path: Tuple[Any, ...],
    ) -> None:
        """Item 14 on one column: a string column, something declared, and its band."""
        where, at = f"{label}.placeholders", path + ("placeholders",)
        if not is_string:
            self.error(
                where, at, f"only a string column takes placeholders; this one is {declared}"
            )
        if not as_list(block.get("values")) and not as_list(block.get("prefixes")):
            self.error(
                where,
                at,
                "declares neither values nor prefixes, so nothing is a placeholder",
                "list placeholder values, prefixes or both",
            )
        self.band(block, where, at)

    # -- 13, 15. relationships ---------------------------------------------------------

    def relationships(self, expose: Expose) -> None:
        for rel in expose.relationships.values():
            target = self.by_id.get(rel.to)
            if target is None:
                continue  # the relationships lint reports the missing target
            label = expose.relationship_label(rel.name)
            path = ("exposes", expose.index, "semantics", "relationships", rel.index)
            self.foreign_key_formats(expose, target, rel)
            self.shared_columns(expose, target, rel, label, path)

    def foreign_key_formats(self, expose: Expose, target: Expose, rel: Relationship) -> None:
        for from_name, to_name in zip(rel.from_columns, rel.to_columns, strict=False):
            mine, theirs = expose.columns.get(from_name), target.columns.get(to_name)
            if not mine or not theirs:
                continue
            own, other = mine[1].get("identifierFormat"), theirs[1].get("identifierFormat")
            if not isinstance(own, list) or not isinstance(other, list):
                continue
            if _normalized(own) != _normalized(other):
                self.error(
                    f"{expose.column_label(from_name)}.identifierFormat",
                    expose.column_path(from_name) + ("identifierFormat",),
                    f"the foreign key {from_name} carries the values of {target.column_label(to_name)}, "
                    "whose format differs",
                    "repeat the target column's format identically, or leave it out",
                )

    def shared_columns(
        self, expose: Expose, target: Expose, rel: Relationship, label: str, path: Tuple[Any, ...]
    ) -> None:
        if not rel.shared_columns:
            return
        where, at = f"{label}.sharedColumns", path + ("sharedColumns",)
        if target.model.validity_window:
            self.error(
                where,
                at,
                f"{rel.name} goes to {target.label}, which declares a validity window: FLUID SQL/1 v1 "
                "copies nothing from a slowly changing dimension through a relationship",
            )
            return
        keys = set(rel.from_columns) | set(rel.to_columns)
        for name in rel.shared_columns:
            if not isinstance(name, str):
                continue
            if name in keys:
                self.error(
                    where,
                    at,
                    f"{name} is a key column of {rel.name}: its own key columns agree by definition, "
                    "so they are never listed",
                )
                continue
            missing = [e.label for e in (expose, target) if name not in e.model.columns]
            if missing:
                self.error(
                    where,
                    at,
                    f"{name} is not a column of {' and '.join(missing)}: a shared column is in both "
                    "exposes' schemas under one name",
                )
                continue
            mine, theirs = expose.model.columns[name], target.model.columns[name]
            if mine.type is None or theirs.type is None:
                self.error(
                    where,
                    at,
                    f"{name} has no FLUID SQL/1 type on both ends, so no consumer can compare it",
                )
                continue
            if mine.type.type != theirs.type.type:
                self.error(
                    where,
                    at,
                    f"{name} is a {mine.type.type.describe()} in {expose.label} and a "
                    f"{theirs.type.type.describe()} in {target.label}: a shared column has one type",
                )
                continue
            node = (expose.expose_id, name)
            self.sources[node].append(f"sharedColumns of relationship {rel.name}")
            self.edges[node].add((target.expose_id, name))

    # -- 16. shared attributes ------------------------------------------------------------

    def shared_attributes(self) -> None:
        groups: DefaultDict[str, List[Tuple[Expose, str]]] = defaultdict(list)
        for expose in self.exposes:
            for name, (_, column) in expose.columns.items():
                if isinstance(column.get("sharedAttribute"), str):
                    groups[column["sharedAttribute"]].append((expose, name))
        for attribute, members in groups.items():
            self.attribute_group(attribute, members)

    def attribute_group(self, attribute: str, members: List[Tuple[Expose, str]]) -> None:
        first_expose, first_name = members[0]
        label = first_expose.column_label(first_name) + ".sharedAttribute"
        path = first_expose.column_path(first_name) + ("sharedAttribute",)
        counts = Counter(expose.expose_id for expose, _ in members)
        twice = [expose_id for expose_id, count in counts.items() if count > 1]
        if twice:
            expose = self.by_id[twice[0]]
            names = [name for e, name in members if e.expose_id == twice[0]]
            self.error(
                expose.column_label(names[1]) + ".sharedAttribute",
                expose.column_path(names[1]) + ("sharedAttribute",),
                f"{expose.label} carries the attribute {attribute!r} twice ({', '.join(names)}): "
                "an expose holds one column of an attribute",
            )
            return
        types = []
        for expose, name in members:
            column = expose.model.columns[name]
            if column.type is None:
                self.error(
                    expose.column_label(name) + ".sharedAttribute",
                    expose.column_path(name) + ("sharedAttribute",),
                    f"{name} ({column.declared}) has no FLUID SQL/1 type, so no consumer can compare "
                    "the attribute",
                )
                return
            types.append(column.type.type)
        if len(set(types)) > 1:
            described = ", ".join(
                f"{e.label}.{n} is a {t.describe()}"
                for (e, n), t in zip(members, types, strict=True)
            )
            self.error(
                label,
                path,
                f"the members of {attribute!r} have different types ({described}): an attribute "
                "has one type",
            )
            return
        if len(members) < 2:
            return
        ids = [expose.expose_id for expose, _ in members]
        candidates = []
        for owner in ids:
            found = {m: self.paths(m, owner) for m in ids if m != owner}
            if all(found.values()):
                candidates.append((owner, found))
        if not candidates:
            self.error(
                label,
                path,
                f"{attribute!r} has no owner: no member is reached by every other member through "
                "relationships, following to",
                "relate the members to one owner, or give the attribute another name where it differs",
            )
            return
        if len(candidates) > 1:
            names = " and ".join(self.by_id[c].label for c, _ in candidates)
            self.error(
                label,
                path,
                f"{attribute!r} has two candidates for its owner ({names}): each reaches the other, "
                "so no one member is the source",
            )
            return
        owner, found = candidates[0]
        for member, routes in found.items():
            if len(routes) > 1:
                self.error(
                    label,
                    path,
                    f"{self.by_id[member].label} reaches the owner {self.by_id[owner].label} of "
                    f"{attribute!r} on two paths ({' and '.join('.'.join(r) for r in routes[:2])}), "
                    "so the copy would not say which one it follows",
                )
                return
            hops = self.hops(member, routes[0])
            for hop_target in hops:
                if self.by_id[hop_target].model.validity_window:
                    self.error(
                        label,
                        path,
                        f"the path from {self.by_id[member].label} to the owner of {attribute!r} "
                        f"passes {self.by_id[hop_target].label}, which declares a validity window",
                    )
                    return
        owner_name = next(name for e, name in members if e.expose_id == owner)
        for expose, name in members:
            self.attribute_of[(expose.expose_id, name)] = attribute
            if expose.expose_id != owner:
                node = (expose.expose_id, name)
                self.sources[node].append(
                    f"sharedAttribute {attribute} (owned by {owner}.{owner_name})"
                )
                self.edges[node].add((owner, owner_name))

    def paths(self, start: str, goal: str) -> List[Tuple[str, ...]]:
        """Up to two simple paths of relationship names from ``start`` to ``goal``."""
        found: List[Tuple[str, ...]] = []

        def walk(here: str, visited: Set[str], route: Tuple[str, ...]) -> None:
            if len(found) >= 2:
                return
            expose = self.by_id.get(here)
            for rel in expose.relationships.values() if expose else ():
                if rel.to not in self.by_id or rel.to in visited:
                    continue
                if rel.to == goal:
                    found.append(route + (rel.name,))
                    if len(found) >= 2:
                        return
                    continue
                walk(rel.to, visited | {rel.to}, route + (rel.name,))

        walk(start, {start}, ())
        return found

    def hops(self, start: str, route: Tuple[str, ...]) -> List[str]:
        """The exposes a route of relationship names reaches, in order."""
        here, reached = start, []
        for name in route:
            here = self.by_id[here].relationships[name].to
            reached.append(here)
        return reached

    # -- 9. one source, 8. cycles -------------------------------------------------------

    def one_source(self) -> None:
        nodes = set(self.derives) | set(self.sources)
        for node in sorted(nodes):
            sources = list(self.sources.get(node, ()))
            derives = self.derives.get(node)
            if derives:
                word = "invariants" if len(derives) > 1 else "invariant"
                sources.insert(0, f"derive {word} {', '.join(derives)}")
            if len(sources) < 2:
                continue
            expose = self.by_id.get(node[0])
            if expose is None:
                continue
            self.error(
                expose.column_label(node[1]),
                expose.column_path(node[1]),
                f"{len(sources)} sources: {'; '.join(sources)}. A column has one source among its "
                "derives, the to side of a dependency, sharedColumns and a shared attribute it does "
                "not own",
                "keep one source and turn the others into checks",
            )

    def cycles(self) -> None:
        for component in _strongly_connected(self.edges):
            nodes = sorted(component)
            if len(nodes) == 1 and nodes[0] not in self.edges.get(nodes[0], set()):
                continue
            expose = self.by_id.get(nodes[0][0])
            if expose is None:
                continue
            members = ", ".join(f"{e}.{c}" for e, c in nodes)
            self.error(
                expose.column_label(nodes[0][1]),
                expose.column_path(nodes[0][1]),
                f"derived columns form a cycle ({members}): each needs another's value first",
                "derive one of them from columns outside the cycle",
            )

    # -- notes ------------------------------------------------------------------------

    def unrelated_pairs(self, expose: Expose, rel: Relationship, target: Expose) -> List[str]:
        """The column names both ends of ``rel`` carry that nothing relates: not a key of
        the relationship, not in its sharedColumns, not one shared attribute, and not read on
        both ends by one invariant."""
        shared = {c for c in rel.shared_columns if isinstance(c, str)}
        keys = set(rel.from_columns) | set(rel.to_columns)
        pairs = []
        for name in expose.model.columns:
            if name not in target.model.columns or name in keys or name in shared:
                continue
            mine = self.attribute_of.get((expose.expose_id, name))
            if mine is not None and mine == self.attribute_of.get((target.expose_id, name)):
                continue
            if (expose.expose_id, rel.name, name) not in self.related:
                pairs.append(name)
        return pairs

    def same_named_columns(self) -> None:
        for expose in self.exposes:
            for rel in expose.relationships.values():
                target = self.by_id.get(rel.to)
                if target is None or target is expose:
                    continue
                pairs = self.unrelated_pairs(expose, rel, target)
                if pairs:
                    self.note(
                        expose.relationship_label(rel.name),
                        ("exposes", expose.index, "semantics", "relationships", rel.index),
                        f"{expose.label} and {target.label} both have {', '.join(pairs)}, and "
                        "nothing says whether they are one attribute (names never decide it)",
                        f"list {'them' if len(pairs) > 1 else 'it'} in sharedColumns if the values "
                        "agree through the link, or relate the two with an invariant",
                    )

    def quality_notes(self) -> None:
        for expose in self.exposes:
            for k, rule in enumerate(as_list(expose.contract.get("quality"))):
                expression = as_dict(rule).get("expression")
                if not isinstance(expression, str):
                    continue
                try:
                    fs.check(fs.parse(expression), expose.model, fs.PREDICATE)
                except fs.FluidSqlError:
                    continue
                name = as_dict(rule).get("rule")
                self.note(
                    f"{expose.label}.contract.quality[{name if isinstance(name, str) else k}]",
                    ("exposes", expose.index, "contract", "quality", k, "expression"),
                    "this expression is FLUID SQL/1: as an invariant (contract.invariants[], with "
                    "check) FLUX and every consumer would read it alike",
                )


def _normalized(formats: List[Any]) -> List[Tuple[Tuple[str, ...], Any]]:
    out = []
    for entry in formats:
        entry = as_dict(entry)
        prefixes = entry.get("prefixes")
        out.append(
            (tuple(sorted(prefixes)) if isinstance(prefixes, list) else ("",), entry.get("mask"))
        )
    return sorted(out, key=repr)


def _strongly_connected(edges: Mapping[Node, Set[Node]]) -> List[Set[Node]]:
    """Tarjan's strongly connected components, iterative (no recursion limit)."""
    index: Dict[Node, int] = {}
    low: Dict[Node, int] = {}
    on_stack: Set[Node] = set()
    stack: List[Node] = []
    components: List[Set[Node]] = []
    counter = 0
    nodes = set(edges) | {n for targets in edges.values() for n in targets}
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                components.append(component)
    return components
