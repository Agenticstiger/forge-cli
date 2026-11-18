# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Declared invariants as dbt ``fluid_rule`` tests (``RFC-data-rules.md``, dbt bridge).

:func:`rule_tests` compiles every rule about valid data
(:func:`~fluid_build.data_rules._compile.compile_rules`) once per engine forge writes SQL
for, and gives each rule one model-level ``fluid_rule`` generic test. The test carries, per
dbt adapter type, the query that counts covered and violating rows and the predicate that
holds when the counts meet the rule's band. :data:`MACRO` picks both by ``target.type``
when the test runs, so one project runs on DuckDB locally and on Athena live. An adapter
with no rendering fails the test with a message naming the rule and the adapter, never a
silent pass. A rule no engine can render is skipped with its reason, never approximated.

The band is compared exactly: ``unit: rows`` compares the violating rows, and ``unit:
percent`` compares ``100 * violating`` with ``value * covered`` (a share of no covered row
is 0), so no division rounds a boundary row. ``severity: error`` fails the test, and
``warning`` and ``info`` warn (dbt has no third level).

**Jinja.** dbt renders test arguments as Jinja, so a contract name or literal holding
``{{``, ``{%`` or ``{#`` would otherwise run during ``dbt test``. Every piece of SQL text is
written as one Jinja string literal (:func:`jinja_text`), which Jinja reads back exactly,
and each table is a ``ref()`` whose argument is such a literal too. The caller writes the
YAML with a YAML emitter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from fluid_build import fluid_sql as fs

from ._compile import EXECUTABLE, Band, Rule, _literal, _Refused, compile_rules

#: dbt adapter type (``target.type``) -> the FLUID SQL/1 dialect its SQL is written in.
ADAPTERS: Dict[str, str] = {
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "spark": "databricks",
    "postgres": "postgres",
    "athena": "athena",
    "trino": "athena",
}

#: The generic test every generated rule calls.
TEST_NAME = "fluid_rule"
#: First line of the macro file: a re-run may overwrite a file carrying it, never another.
MACRO_SENTINEL = "-- managed-by: fluid"
#: The file the macro is written to, below the project's first macro path.
MACRO_FILE = "fluid/fluid_rule.sql"
MACRO = (
    MACRO_SENTINEL
    + """
-- Written by `fluid generate dbt-tests`. One generic test for every rule about valid data a
-- FLUID contract declares (RFC-data-rules.md): each test carries, per adapter type, the query
-- that counts covered and violating rows, and the predicate that holds when the counts meet
-- the rule's band. A row returned is a failure.
{% test fluid_rule(model, rule, sql, holds) %}
{%- set counts = sql.get(target.type) -%}
{%- set band = holds.get(target.type) -%}
{%- if counts is none or band is none -%}
{#- dbt renders a test once at parse time (execute is false then): fail only the run of
    this test, never the parse of the whole project. -#}
{%- if execute -%}
{{ exceptions.raise_compiler_error("fluid_rule " ~ rule ~ ": forge wrote no SQL for the "
   ~ target.type ~ " adapter (it writes " ~ (sql.keys() | list | join(", ")) ~ "). "
   ~ "`fluid generate dbt-tests` warned why; run this rule on one of those adapters, "
   ~ "or restate it in FLUID SQL/1 that " ~ target.type ~ " can run.") }}
{%- endif -%}
select 1 as fluid_no_sql where false
{%- else %}
select fluid_counts.*
from ({{ counts }}) as fluid_counts
where not ({{ band }})
{%- endif %}
{% endtest %}
"""
)

_OPERATORS = {
    "mustBe": "=",
    "mustBeLessThan": "<",
    "mustBeLessOrEqualTo": "<=",
    "mustBeGreaterThan": ">",
    "mustBeGreaterOrEqualTo": ">=",
}
_SEVERITY = {"error": "error", "warning": "warn", "info": "warn"}
_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
_NUL = chr(0)
_SENTINEL = re.compile(_NUL + "([0-9]+)" + _NUL)


@dataclass(frozen=True)
class Skipped:
    """A rule no engine renders (``adapters`` empty), or one some adapters cannot run
    (``adapters`` names them), and why."""

    id: str
    expose: str
    reason: str
    adapters: Tuple[str, ...] = ()


def jinja_text(text: str) -> str:
    r"""``text`` as one Jinja expression that renders exactly ``text``: a double-quoted
    string literal with ``\`` and ``"`` escaped and every character outside printable ASCII
    written as a ``\u`` or ``\U`` escape (Jinja decodes a literal with ``unicode-escape``),
    so no ``{{``, ``{%`` or ``{#`` from a contract reaches Jinja as syntax."""
    return "{{ " + _jinja_literal(text) + " }}"


def _jinja_literal(text: str) -> str:
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif " " <= char <= "~":
            out.append(char)
        elif ord(char) < 0x10000:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(f"\\U{ord(char):08x}")
    return '"' + "".join(out) + '"'


def holds_sql(dialect: str, band: Band) -> str:
    """The predicate over ``covered`` and ``violating`` that holds when they meet ``band``,
    compared exactly (RFC 1.4)."""
    q = lambda name: fs.sql_name(dialect, name)  # noqa: E731
    covered, violating = q("covered"), q("violating")

    def number(value: Any) -> str:
        return fs.number_literal(dialect, _literal(value))

    def compare(left: str, value: Any, scaled: bool) -> List[str]:
        def side(v: Any) -> str:
            return f"{number(v)} * {covered}" if scaled else number(v)

        if band.operator == "mustBeBetween":
            if not isinstance(value, list) or len(value) != 2:
                raise _Refused("mustBeBetween takes [lo, hi]")
            return [f"{left} >= {side(value[0])}", f"{left} <= {side(value[1])}"]
        op = _OPERATORS.get(band.operator)
        if op is None:
            raise _Refused(f"{band.operator} is not a band operator")
        return [f"{left} {op} {side(value)}"]

    if band.unit != "percent":
        return " AND ".join(compare(violating, band.value, False))
    empty = " AND ".join(compare("0", band.value, False))
    shared = " AND ".join(compare(f"100 * {violating}", band.value, True))
    return f"(({covered} = 0 AND {empty}) OR ({covered} > 0 AND {shared}))"


def _jinja_sql(sql: str, refs: List[str]) -> str:
    """The SQL as Jinja: text pieces as literals, each sentinel a ``ref()``."""
    parts, last = [], 0
    for match in _SENTINEL.finditer(sql):
        if match.start() > last:
            parts.append(jinja_text(sql[last : match.start()]))
        parts.append("{{ ref(" + _jinja_literal(refs[int(match.group(1))]) + ") }}")
        last = match.end()
    if last < len(sql):
        parts.append(jinja_text(sql[last:]))
    return "".join(parts)


def test_name(rule_id: str, taken: Set[str]) -> str:
    """A dbt test name for ``rule_id``: ``fluid_<id>`` in ``[A-Za-z0-9_]``, unique (case
    folded, as dbt compares node names on case-insensitive file systems)."""
    base = "fluid_" + (_UNSAFE.sub("_", rule_id).strip("_") or "rule")
    name, attempt = base, 1
    while name.casefold() in taken:
        attempt += 1
        name = f"{base}_{attempt}"
    taken.add(name.casefold())
    return name


def _rendered(
    rule: Rule, dialect: str, model_of: Callable[[str], Optional[str]]
) -> Tuple[str, str]:
    refs: List[str] = []

    def table(expose_id: str) -> str:
        name = model_of(expose_id)
        if not name:
            raise _Refused(f"{expose_id} has no dbt model")
        refs.append(name)
        return f"{_NUL}{len(refs) - 1}{_NUL}"

    assert rule.counts is not None
    counts = _jinja_sql(rule.counts(table), refs)
    return counts, jinja_text(holds_sql(dialect, rule.band or Band("mustBe", 0)))


def rule_tests(
    contract: Mapping[str, Any], model_of: Callable[[str], Optional[str]]
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Skipped]]:
    """The ``fluid_rule`` tests of every expose, keyed by its dbt model name, and the rules
    skipped. ``model_of(exposeId)`` names the dbt model of a rule's own or linked expose."""
    # Keyed by expose too: a column rule's id (``code.identifierFormat``) repeats per expose.
    by_dialect: Dict[str, Dict[Tuple[str, str], Rule]] = {}
    order: List[Rule] = []
    for dialect in fs.DIALECTS:
        rules = compile_rules(contract, dialect=dialect)
        by_dialect[dialect] = {(rule.expose, rule.id): rule for rule in rules}
        order = order or rules
    tests: Dict[str, List[Dict[str, Any]]] = {}
    skipped: List[Skipped] = []
    taken: Set[str] = set()
    for first in order:
        if first.band is None:
            continue  # declared, and no operator bounds it: nothing to test (RFC 4)
        model = model_of(first.expose)
        sql: Dict[str, str] = {}
        holds: Dict[str, str] = {}
        reasons: List[str] = []
        missing: List[str] = []
        for adapter, dialect in ADAPTERS.items():
            rule = by_dialect[dialect].get((first.expose, first.id))
            if rule is None or rule.status != EXECUTABLE or rule.counts is None:
                reasons.append(rule.reason if rule is not None else "")
                missing.append(adapter)
                continue
            try:
                sql[adapter], holds[adapter] = _rendered(rule, dialect, model_of)
            except (_Refused, ValueError) as why:
                reasons.append(str(why))
                missing.append(adapter)
        reason = next((r for r in reasons if r), "") or "its expose has no dbt model"
        if not sql or not model:
            skipped.append(Skipped(first.id, first.expose, reason))
            continue
        if missing:
            skipped.append(Skipped(first.id, first.expose, reason, tuple(missing)))
        tests.setdefault(model, []).append(
            {
                TEST_NAME: {
                    "name": test_name(first.id, taken),
                    # dbt renders every string test argument as Jinja (and a bare
                    # env_var(...) too): the id is contract text, so it is a literal.
                    "rule": jinja_text(first.id),
                    "sql": sql,
                    "holds": holds,
                    "config": {"severity": _SEVERITY.get(first.severity, "error")},
                }
            }
        )
    return tests, skipped


def placeholder_where(column: str, values: List[str], prefixes: List[str]) -> Optional[str]:
    """A test ``where`` config that leaves a column's placeholder rows out (RFC 4), per
    adapter type; ``None`` when the column declares none. dbt renders a ``where`` at parse
    time and takes no macro there, so the branches are plain Jinja on ``target.type``, and an
    adapter forge writes no SQL for keeps every row (no exemption, never a looser test)."""
    if not values and not prefixes:
        return None
    from ._compile import placeholder_sql

    groups: Dict[str, List[str]] = {}
    for adapter, dialect in ADAPTERS.items():
        groups.setdefault(dialect, []).append(adapter)
    branches = []
    for dialect, adapters in groups.items():
        name = fs.sql_name(dialect, column)
        sql = f"NOT {placeholder_sql(dialect, name, values, prefixes)}"
        listed = ", ".join(_jinja_literal(a) for a in adapters)
        branches.append((listed, jinja_text(sql)))
    text = ""
    for i, (listed, sql) in enumerate(branches):
        text += ("{% if" if i == 0 else "{% elif") + f" target.type in [{listed}] %}}{sql}"
    return text + "{% else %}TRUE{% endif %}"


#: The tests a column's own ``validationRules``, enum and dq value rules produce, from which a
#: placeholder is exempt (RFC 4): dbt's ``accepted_values`` and dbt_expectations' range test.
PLACEHOLDER_EXEMPT_TESTS = frozenset(
    {"accepted_values", "dbt_expectations.expect_column_values_to_be_between"}
)


def exempt_placeholders(
    col: Mapping[str, Any], tests: List[Any], mark: Callable[[str], str] = str
) -> List[Any]:
    """``tests`` with a ``where`` config that leaves the column's placeholder rows out of its
    ``accepted_values`` and range tests; unchanged when it declares none. ``mark`` wraps the
    ``where`` text (a caller that escapes contract text marks it as already Jinja)."""
    block = col.get("placeholders")
    if not isinstance(block, Mapping):
        return tests

    def texts(key: str) -> List[str]:
        raw = block.get(key)
        return [v for v in raw if isinstance(v, str) and v] if isinstance(raw, list) else []

    try:
        where = placeholder_where(str(col.get("name")), texts("values"), texts("prefixes"))
    except ValueError:  # a column name no dialect can quote: leave the tests as they were
        return tests
    if where is None:
        return tests
    out: List[Any] = []
    for test in tests:
        if isinstance(test, Mapping) and len(test) == 1:
            ((name, body),) = test.items()
            if name in PLACEHOLDER_EXEMPT_TESTS and isinstance(body, Mapping):
                config = dict(body.get("config") or {})
                config["where"] = mark(where)
                test = {name: {**body, "config": config}}
        out.append(test)
    return out
