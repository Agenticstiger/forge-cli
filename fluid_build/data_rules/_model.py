# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract's exposes as FLUID SQL/1 sees them: one :class:`Expose` per expose.

Each holds the expose's raw blocks (for paths and pointers) and its
:class:`~fluid_build.fluid_sql.Model`: the top-level columns of
``contract.schema`` with their FLUID SQL/1 types, enums and placeholders, and
the relationships of ``semantics.relationships`` resolved to their targets.
Reads a contract of any shape without raising: an entry the schema refuses is
left out, so the lint still reports on the rest.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fluid_build import fluid_sql as fs


def nfc(value: Any) -> Any:
    """``value`` NFC-normalized when it is a string: the contract's own texts (names,
    enum values, placeholders) are compared in NFC, as FLUID SQL/1 texts are (RFC 2.3)."""
    return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass
class Relationship:
    index: int
    name: str
    to: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]
    shared_columns: Tuple[Any, ...]
    raw: Mapping[str, Any]


@dataclass
class Expose:
    index: int
    expose_id: str
    raw: Mapping[str, Any]
    model: fs.Model
    columns: Dict[str, Tuple[int, Mapping[str, Any]]] = field(default_factory=dict)
    relationships: Dict[str, Relationship] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"exposes[{self.expose_id}]" if self.expose_id else f"exposes[{self.index}]"

    @property
    def contract(self) -> Mapping[str, Any]:
        return as_dict(self.raw.get("contract"))

    def column_label(self, name: str) -> str:
        return f"{self.label}.contract.schema[{name}]"

    def column_path(self, name: str) -> Tuple[Any, ...]:
        index = self.columns[name][0] if name in self.columns else 0
        return ("exposes", self.index, "contract", "schema", index)

    def relationship_label(self, name: str) -> str:
        return f"{self.label}.semantics.relationships[{name}]"


def enum_values(column: Mapping[str, Any]) -> Tuple[str, ...]:
    """The values a column's ``validationRules`` enum lists, read as every forge reader reads
    them: split at commas (the dbt bridge) and at ``|`` too (``fluid test``), so a literal is
    refused only when no reader would accept it."""
    values: List[str] = []
    for rule in as_list(column.get("validationRules")):
        if not isinstance(rule, Mapping) or str(rule.get("type") or "").strip().lower() != "enum":
            continue
        constraint = rule.get("constraint")
        if not isinstance(constraint, str):
            continue
        for part in constraint.split(",") + re.split(r"[|,]", constraint):
            part = nfc(part.strip())
            if part and part not in values:
                values.append(part)
    return tuple(values)


def placeholder_lists(column: Mapping[str, Any]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    block = as_dict(column.get("placeholders"))
    values = tuple(nfc(v) for v in as_list(block.get("values")) if isinstance(v, str) and v)
    prefixes = tuple(nfc(p) for p in as_list(block.get("prefixes")) if isinstance(p, str) and p)
    return values, prefixes


def declares_validity_window(expose: Mapping[str, Any]) -> bool:
    """Whether the expose's semantics declare a validity window (a slowly changing
    dimension): a dimension with ``typeParams.validityParams`` ``isStart`` or ``isEnd``."""
    semantics = as_dict(expose.get("semantics"))
    for dimension in as_list(semantics.get("dimensions")):
        params = as_dict(as_dict(as_dict(dimension).get("typeParams")).get("validityParams"))
        if params.get("isStart") is True or params.get("isEnd") is True:
            return True
    return False


def build(contract: Mapping[str, Any]) -> List[Expose]:
    """Every expose of ``contract`` with an ``exposeId``, its model's links resolved."""
    exposes: List[Expose] = []
    for index, raw in enumerate(as_list(contract.get("exposes"))):
        if not isinstance(raw, Mapping):
            continue
        expose_id = raw.get("exposeId") if isinstance(raw.get("exposeId"), str) else ""
        model = fs.Model(name=f"exposes[{expose_id or index}]")
        model.validity_window = declares_validity_window(raw)
        expose = Expose(index, expose_id, raw, model)
        for position, column in enumerate(as_list(as_dict(raw.get("contract")).get("schema"))):
            if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
                continue
            name = nfc(column["name"])
            if name in expose.columns:
                continue
            declared = column.get("type") if isinstance(column.get("type"), str) else ""
            values, prefixes = placeholder_lists(column)
            expose.columns[name] = (position, column)
            model.columns[name] = fs.Column(
                name=name,
                declared=declared,
                type=fs.column_type(declared) if not column.get("fields") else None,
                enum=enum_values(column),
                placeholder_values=values,
                placeholder_prefixes=prefixes,
            )
        semantics = as_dict(raw.get("semantics"))
        for position, rel in enumerate(as_list(semantics.get("relationships"))):
            if not isinstance(rel, Mapping) or not isinstance(rel.get("name"), str):
                continue
            name = nfc(rel["name"])
            if name in expose.relationships:
                continue
            expose.relationships[name] = Relationship(
                index=position,
                name=name,
                to=rel.get("to") if isinstance(rel.get("to"), str) else "",
                from_columns=tuple(
                    nfc(c) for c in as_list(rel.get("fromColumns")) if isinstance(c, str)
                ),
                to_columns=tuple(
                    nfc(c) for c in as_list(rel.get("toColumns")) if isinstance(c, str)
                ),
                shared_columns=tuple(nfc(c) for c in as_list(rel.get("sharedColumns"))),
                raw=rel,
            )
        exposes.append(expose)
    by_id = {e.expose_id: e for e in exposes if e.expose_id}
    for expose in exposes:
        for rel in expose.relationships.values():
            target = by_id.get(rel.to)
            expose.model.links[rel.name] = fs.Link(
                rel.name, target.model if target else None, target_name=rel.to
            )
    return exposes


def by_id(exposes: List[Expose]) -> Dict[str, Expose]:
    return {e.expose_id: e for e in exposes if e.expose_id}


def target_of(expose: Expose, relationship: str, exposes: Dict[str, Expose]) -> Optional[Expose]:
    rel = expose.relationships.get(relationship)
    return exposes.get(rel.to) if rel else None
