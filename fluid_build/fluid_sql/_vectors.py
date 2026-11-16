# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Run ``fluid-sql-vectors.json``, the conformance vectors of FLUID SQL/1.

The vectors define the language: forge-cli's validator and flux-expr/2 each run
every vector through their own parser, checker and evaluator, and must reach
every verdict, tree, canonical text, type, scale and value it gives. The file's
own ``description`` is the format's reference; :func:`run_vector` is this
implementation's reading of it, used by the test suite and by
``scripts/fill_fluid_sql_vectors.py``.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from fractions import Fraction
from importlib import resources
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import _nodes as n
from ._canonical import canonical
from ._checker import PREDICATE, VALUE, Checked, check
from ._errors import FluidSqlError
from ._evaluate import evaluate
from ._parser import parse
from ._types import (
    BOOL,
    DATE,
    NUMBER,
    STRING,
    TIMESTAMP,
    TIMESTAMPTZ,
    Column,
    Link,
    Model,
    SqlType,
    column_type,
)

VECTORS_FILE = "fluid-sql-vectors.json"


def load_vectors() -> Dict[str, Any]:
    """The vectors document shipped with this package."""
    text = (
        resources.files("fluid_build.fluid_sql").joinpath(VECTORS_FILE).read_text(encoding="utf-8")
    )
    return json.loads(text)


def build_models(document: Mapping[str, Any]) -> Dict[str, Model]:
    """The models a vectors document declares, with their links resolved."""
    models: Dict[str, Model] = {}
    for name, spec in document.get("models", {}).items():
        columns = {}
        for column_name, column in spec.get("columns", {}).items():
            placeholders = column.get("placeholders") or {}
            columns[column_name] = Column(
                name=column_name,
                declared=column["type"],
                type=column_type(column["type"]),
                enum=tuple(column.get("enum") or ()),
                placeholder_values=tuple(placeholders.get("values") or ()),
                placeholder_prefixes=tuple(placeholders.get("prefixes") or ()),
            )
        models[name] = Model(
            name=name, columns=columns, validity_window=bool(spec.get("validityWindow"))
        )
    for name, spec in document.get("models", {}).items():
        for link_name, link in (spec.get("relationships") or {}).items():
            models[name].links[link_name] = Link(
                link_name, models.get(link["to"]), target_name=link["to"]
            )
    return models


def decode(value: Any, kind: str) -> Any:
    """A row value or a result, from its JSON form, as the evaluator holds it."""
    if value is None:
        return None
    if kind == NUMBER:
        if not isinstance(value, str):
            raise ValueError(f"a number is a string in the vectors file, not {value!r}")
        if "/" in value:
            numerator, _, denominator = value.partition("/")
            return Fraction(int(numerator), int(denominator))
        return Fraction(Decimal(value))
    if kind == BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"a bool is true or false, not {value!r}")
        return value
    if kind == STRING:
        return str(value)
    if kind == DATE:
        return datetime.date.fromisoformat(value)
    if kind in (TIMESTAMP, TIMESTAMPTZ):
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if (moment.tzinfo is None) != (kind == TIMESTAMP):
            raise ValueError(f"{value!r} is not a {kind}")
        return moment
    raise ValueError(f"unknown kind {kind!r}")


def encode(value: Any, sql_type: SqlType) -> Any:
    """A value in its JSON form: a number of static scale ``s`` with exactly ``s``
    digits after the point; one of unbounded scale as its shortest decimal, or as
    ``p/q`` in lowest terms when no decimal is exact."""
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    exact = Fraction(value)
    if sql_type.scale is not None:
        return _decimal_text(exact, sql_type.scale)
    denominator = exact.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator != 1:
        return f"{exact.numerator}/{exact.denominator}"
    places = 0
    while (exact * 10**places).denominator != 1:
        places += 1
    return _decimal_text(exact, places)


def _decimal_text(value: Fraction, places: int) -> str:
    scaled = value * 10**places
    if scaled.denominator != 1:
        raise ValueError(f"{value} has more than {places} decimal places")
    digits = str(abs(scaled.numerator)).rjust(places + 1, "0")
    sign = "-" if scaled.numerator < 0 else ""
    if places == 0:
        return sign + digits
    return f"{sign}{digits[:-places]}.{digits[-places:]}"


def _rows(case: Mapping[str, Any], model: Model) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    row = {
        name: decode(value, _kind(model, name)) for name, value in (case.get("row") or {}).items()
    }
    links: Dict[str, Optional[Dict[str, Any]]] = {}
    for link_name, reached in (case.get("links") or {}).items():
        link = model.links[link_name]
        assert link.target is not None
        links[link_name] = (
            None
            if reached is None
            else {name: decode(value, _kind(link.target, name)) for name, value in reached.items()}
        )
    return row, links


def _kind(model: Model, name: str) -> str:
    column = model.columns[name]
    if column.type is None:
        raise ValueError(f"{model.name}.{name} has no FLUID SQL/1 type and cannot hold a value")
    return column.type.type.kind


def run_vector(vector: Mapping[str, Any], models: Mapping[str, Model]) -> List[str]:
    """Every way this implementation disagrees with ``vector`` (``[]`` when it agrees)."""
    expect = vector["expect"]
    model = models[vector["model"]]
    site = vector.get("site", PREDICATE)
    derive = model.columns[vector["derive"]] if vector.get("derive") else None
    try:
        tree = parse(vector["text"])
        checked = check(tree, model, site, derive)
    except FluidSqlError as refused:
        if expect["verdict"] != "refused":
            return [f"refused ({refused.reason}: {refused}) but the vector accepts it"]
        if refused.reason != expect["reason"]:
            return [f"refused as {refused.reason} ({refused}), the vector says {expect['reason']}"]
        return []
    if expect["verdict"] != "accepted":
        return [f"accepted, but the vector refuses it ({expect.get('reason')})"]
    return _compare_accepted(vector, tree, checked, model, site)


def _compare_accepted(
    vector: Mapping[str, Any], tree: n.Node, checked: Checked, model: Model, site: str
) -> List[str]:
    expect = vector["expect"]
    problems: List[str] = []
    if n.to_json(tree) != expect["tree"]:
        problems.append(f"tree {json.dumps(n.to_json(tree))} differs from the vector's")
    if canonical(tree) != expect["canonical"]:
        problems.append(
            f"canonical text {canonical(tree)!r}, the vector says {expect['canonical']!r}"
        )
    if checked.type.kind != expect["type"]:
        problems.append(f"type {checked.type.kind}, the vector says {expect['type']}")
    if checked.type.kind == NUMBER:
        scale = "unbounded" if checked.type.scale is None else str(checked.type.scale)
        if scale != expect.get("scale"):
            problems.append(f"scale {scale}, the vector says {expect.get('scale')}")
    for index, case in enumerate(expect.get("eval", [])):
        row, links = _rows(case, model)
        actual = evaluate(tree, row, links, predicate=site == PREDICATE)
        expected = case["result"]
        if site == VALUE and expected is not None:
            want = decode(expected, checked.type.kind)
        else:
            want = expected
        if actual != want or (actual is None) != (want is None):
            problems.append(
                f"eval case {index}: got {encode(actual, checked.type)!r}, want {expected!r}"
            )
        elif site == VALUE and encode(actual, checked.type) != expected:
            problems.append(
                f"eval case {index}: the result is written {expected!r}, its canonical form is "
                f"{encode(actual, checked.type)!r}"
            )
    return problems
