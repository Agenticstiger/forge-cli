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

"""Single shared contract → dbt-test mapping.

Historically three parallel generators translated FLUID contract quality
intent into dbt tests and **disagreed**:

* ``engines/dbt/schema_yml.py`` — completeness/uniqueness/valid_values +
  an accuracy→range mapping; no relationships, no freshness.
* ``exporters/dbt_tests.py`` — the richest: + expression_is_true, recency,
  and ``fluid_*`` fail-loud sentinels for schema/anomaly/drift.
* ``copilot/tools/dbt_test_generator.py`` — the only emitter of
  ``relationships`` and the only ``dbt_expectations`` user.

That divergence meant range tests differed by surface (``dbt_utils`` vs
``dbt_expectations``), relationships never derived from the engine path,
and freshness only existed in the exporter. This module is the one place
the translation lives so the three paths *cannot* drift — the same design
`datacontract-cli` uses (a single pure ``dbt_test_mapping`` module shared by
both its export and sync paths) and `DataVow` (ODCS → dbt) follow.

Two directions are pinned symmetric:

* **Forward** — a canonical ``dqRule.type`` → dbt generic-test name table
  (:data:`FORWARD_RULE_TO_TEST`), plus richer emitters that also carry the
  test *arguments* (value lists, expressions, recency windows, range bounds,
  relationships).
* **Reverse** — dbt test name → ``dqRule.type`` (:data:`REVERSE_TEST_TO_RULE`),
  the hook the planned dbt-manifest importer consumes so it, too, reuses this
  table rather than becoming a fourth divergent mapping.

The reverse table is the hand-written inverse of the forward table over the
**mappable subset** (the round-trip is pinned in the tests). Constraint-derived
tests (``relationships``, numeric range) and the ``fluid_*`` sentinels are
intentionally *outside* that bijection — there is no faithful FLUID
``dqRule.type`` for referential integrity or a numeric range, and the sentinels
are deliberately non-standard so ``dbt test`` fails loud on an unmapped rule.

Range-test dialect decision
---------------------------
The one range/between dialect is **dbt_expectations**
(``dbt_expectations.expect_column_values_to_be_between``), *not*
``dbt_utils.accepted_range``. Rationale:

1. It is what ``datacontract-cli`` and ``DataVow`` (the surveyed prior art)
   both emit for numeric range/between — aligning the fluid artifact with the
   wider ecosystem convention.
2. The copilot generator already emitted this dialect, and its output is the
   one range path with a live downstream consumer (``copilot/enrichment.py``);
   unifying *on* it leaves that path byte-for-byte unchanged and only shifts
   the two YAML-emit-only paths (engine + exporter), neither of which is
   runtime-coupled to the dialect.
3. ``expect_column_values_to_be_between`` natively expresses min-only / max-only
   bounds and is inclusive by default (``strictly: false``), matching the
   inclusive intent the exporter previously spelled with an explicit
   ``inclusive: true`` on ``accepted_range``.

Test names emitted here are ``tests:`` entries (dbt convention): bare strings
for ``not_null`` / ``unique`` and single-key dicts for everything else.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# ── Package prefixes / canonical test names ───────────────────────────────
# The single numeric range/between dialect (see module docstring).
RANGE_TEST_NAME = "dbt_expectations.expect_column_values_to_be_between"
EXPRESSION_TEST_NAME = "dbt_utils.expression_is_true"
RECENCY_TEST_NAME = "dbt_utils.recency"
ROW_COUNT_TEST_NAME = "dbt_expectations.expect_table_row_count_to_be_between"

# Prefix for fail-loud sentinel test names — an unmapped rule surfaces as a
# ``dbt test`` "test not found" error rather than being silently dropped.
SENTINEL_PREFIX = "fluid_"

# Selector value that marks a table-wide (not column-scoped) dq rule.
TABLE_SELECTOR = "*"

# Rule types whose dbt test can only attach at *model* level, whatever the
# rule's selector says. dbt passes ``column_name`` to every generic test
# reached through ``columns[].data_tests``; ``dbt_utils.recency`` is declared
# ``(model, field, datepart, interval, ignore_time_component,
# group_by_columns)`` and takes no ``column_name``, so a column-attached
# freshness rule fails the whole project at parse time with
# "macro 'dbt_macro__test_recency' takes no keyword argument 'column_name'".
# The column the rule selected is preserved — it becomes the test's ``field``.
MODEL_SCOPED_RULE_TYPES = frozenset({"freshness"})

# FLUID ``dqRule.operator`` → SQL comparison operator. The enum is
# ``>= > <= < == !=``; only ``==`` needs translating for SQL.
_SQL_OPERATORS: dict[str, str] = {
    ">=": ">=",
    ">": ">",
    "<=": "<=",
    "<": "<",
    "==": "=",
    "!=": "!=",
}


# ── Canonical dbt-test emitters (pure) ────────────────────────────────────
# Every call site funnels through these so the emitted shape is identical
# everywhere. Reference them via module attribute access from the call sites
# (``import ... _test_mapping as _tm; _tm.numeric_range_test(...)``) so a
# ``patch("...engines.dbt._test_mapping.<fn>")`` flows through to all three.


def not_null_test() -> str:
    """dbt built-in ``not_null`` generic test."""
    return "not_null"


def unique_test() -> str:
    """dbt built-in ``unique`` generic test."""
    return "unique"


def accepted_values_test(values: Sequence[Any]) -> dict[str, Any]:
    """dbt built-in ``accepted_values`` with the declared value list."""
    return {"accepted_values": {"values": list(values)}}


def relationships_test(to: str, field: str) -> dict[str, Any]:
    """dbt built-in ``relationships`` test — referential integrity.

    ``to`` is wrapped in a dbt ``ref(...)`` so the test binds to a model in
    the same project (matches the copilot generator's long-standing shape).
    """
    return {"relationships": {"to": f"ref('{to}')", "field": field}}


def numeric_range_test(
    *, min_value: Any = None, max_value: Any = None, strictly: Any = None
) -> dict[str, Any] | None:
    """The single numeric range/between dialect (dbt_expectations).

    Inclusive by default (``expect_column_values_to_be_between`` treats
    ``strictly`` as ``false``), so the key is only emitted when the contract
    declares exclusive bounds. Returns ``None`` when neither bound is present.
    """
    body: dict[str, Any] = {}
    if min_value is not None:
        body["min_value"] = min_value
    if max_value is not None:
        body["max_value"] = max_value
    if not body:
        return None
    if strictly:
        body["strictly"] = True
    return {RANGE_TEST_NAME: body}


def expression_test(expression: str) -> dict[str, Any]:
    """``dbt_utils.expression_is_true`` — a predicate placeholder/check.

    Attached to a column, dbt_utils compiles this to
    ``where not(<column_name> <expression>)``, so ``expression`` is the
    *right-hand side* of the comparison (``">= 0"``). Attached to a model it
    compiles to ``where not(<expression>)`` and must be a complete row
    predicate. Either way the string is interpolated into executable SQL —
    never put a ``--`` comment in it.
    """
    return {EXPRESSION_TEST_NAME: {"expression": expression}}


def row_count_test(min_rows: Any) -> dict[str, Any]:
    """``dbt_expectations.expect_table_row_count_to_be_between`` — a volume floor.

    The model-level shape for ``anomaly_detection`` / ``drift_detection``
    thresholds. ``dbt_utils.expression_is_true`` cannot express this: at
    model level it compiles the expression into a ``WHERE`` clause, and a
    warehouse rejects an aggregate there ("count(*) > 100" →
    ``Invalid aggregate function in WHERE clause`` on Snowflake).
    """
    return {ROW_COUNT_TEST_NAME: {"min_value": min_rows}}


def recency_test(
    field: str,
    *,
    window: Any = None,
    datepart: str = "day",
    interval: int = 1,
) -> dict[str, Any]:
    """``dbt_utils.recency`` freshness test on a timestamp column.

    ``datepart``/``interval`` derive from the rule's ISO-8601 ``window``
    (``PT6H`` → ``hour``/6, ``P1D`` → ``day``/1) via the repo's canonical
    converter (:func:`fluid_build.util.freshness.iso_duration_to_freshness_unit`
    — its ``minute | hour | day`` vocabulary is a valid datepart subset); the
    explicit kwargs apply only when the window is absent or unparseable.

    Only kwargs the ``dbt_utils.test_recency`` macro accepts are emitted. The
    previous ``_fluid_window`` carry-through key made every freshness test
    fail at dbt compile time ("unexpected keyword argument"). Round-trip
    intent survives without it: the manifest importer reconstructs the ISO
    window from datepart/interval (``cli/import_workflow/dbt.py``).
    """
    if window is not None:
        from fluid_build.util.freshness import iso_duration_to_freshness_unit

        unit = iso_duration_to_freshness_unit(str(window))
        if unit is not None:
            datepart = unit["period"]
            interval = unit["count"]
    return {RECENCY_TEST_NAME: {"field": field, "datepart": datepart, "interval": interval}}


def sentinel_test(kind: str, column: str | None = None) -> str:
    """A fail-loud ``fluid_*`` sentinel test name for an unmapped rule.

    dbt surfaces a clean "test not found" error pointing at the gap instead of
    dropping a declared check. This is the deliberate divergence retained from
    the exporter (pinned in ``tests/test_dbt_tests_exporter.py``).
    """
    return f"{SENTINEL_PREFIX}{kind}_{column}" if column else f"{SENTINEL_PREFIX}{kind}"


# ── Field-level constraint recognition (best-effort, in-the-wild keys) ─────
# Mirrors the canonical FLUID column-constraint recognition already used across
# the codebase (the exporter + ``copilot.enrichment``) so every surface agrees
# on which keys mean what. FLUID columns formally carry ``required`` /
# ``semanticType`` / ``labels``; the ``unique`` / ``enum`` / ``minimum`` /
# ``maximum`` / FK spellings below are accepted best-effort because contracts
# express quality intent inline in practice.

_PK_KEYS = ("primary", "primaryKey", "primary_key", "pk", "isPrimary")
_ENUM_KEYS = ("enum", "acceptedValues", "accepted_values")
_FK_KEYS = ("foreign_key", "foreignKey", "references", "relationship_to")

# ``column.validationRules[]`` is the *schema-sanctioned* way to declare a
# range / enum / FK on a column: the ``column`` definition has
# ``additionalProperties: false`` in every version 0.7.1-0.7.6 and its eleven
# keys include ``validationRules`` but none of the inline ``minimum`` /
# ``maximum`` / ``enum`` / ``foreign_key`` spellings recognised above. Those
# inline forms are accepted best-effort (contracts express intent that way in
# practice) but a contract using them fails ``fluid validate``, so before this
# the richest mapping was unreachable from a valid contract. The rule shapes
# read here are exactly the ones ``cli/import_workflow/dbt.py`` writes
# (``{"type": "range", "constraint": ">= 0 and <= 100"}``,
# ``{"type": "custom", "constraint": "references model.field"}``), which also
# closes the dbt → contract → dbt round trip for those tests.
_RANGE_BOUND_RE = re.compile(r"(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)")
_BETWEEN_RE = re.compile(
    r"^\s*between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE
)
_REFERENCES_RE = re.compile(r"^\s*references\s+(\S+?)\.(\w+)\s*$", re.IGNORECASE)


def is_truthy(value: Any) -> bool:
    """Loose truthiness for YAML/JSON booleans-as-strings (``"true"``/``True``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def column_is_key(col: Mapping[str, Any]) -> bool:
    """True when the column is declared a uniqueness key / primary key."""
    if is_truthy(col.get("unique")):
        return True
    if any(is_truthy(col.get(k)) for k in _PK_KEYS):
        return True
    if str(col.get("semanticType") or "").strip().lower() in ("identifier", "primary_key"):
        return True
    labels = col.get("labels")
    if isinstance(labels, Mapping):
        if is_truthy(labels.get("unique")):
            return True
        if str(labels.get("constraint") or "").strip().lower() in ("primary_key", "unique"):
            return True
    return False


def validation_rules(col: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The column's ``validationRules[]`` entries (schema-canonical)."""
    raw = col.get("validationRules")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [r for r in raw if isinstance(r, Mapping)]


def _rules_of_type(col: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        rule
        for rule in validation_rules(col)
        if str(rule.get("type") or "").strip().lower() == kind
    ]


def column_enum_values(col: Mapping[str, Any]) -> list[Any]:
    """Return the accepted-value list declared on a column, if any.

    Reads the inline ``enum`` / ``acceptedValues`` keys and the
    schema-canonical ``validationRules[] {type: enum, constraint: "a,b,c"}``
    (the spelling the shipped examples and the dbt importer use).
    """
    for key in _ENUM_KEYS:
        raw = col.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            vals = [v for v in raw if v is not None]
            if vals:
                return vals
    for rule in _rules_of_type(col, "enum"):
        constraint = rule.get("constraint")
        if isinstance(constraint, str):
            vals = [v.strip() for v in constraint.split(",") if v.strip()]
            if vals:
                return vals
    return []


def column_range(col: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``{min_value, max_value[, strictly]}`` declared on a column.

    Accepts ``minimum``/``maximum`` (JSON-Schema spelling), the ``min``/``max``
    aliases the copilot mapper recognises, and the schema-canonical
    ``validationRules[] {type: range, constraint: ">= 0 and <= 100"}`` —
    including ``between X and Y``.

    ``strictly`` is set only when *every* declared bound is exclusive, because
    ``expect_column_values_to_be_between`` has a single flag for both bounds.
    A mix of inclusive and exclusive bounds cannot be expressed faithfully, so
    :func:`constraint_tests` emits a fail-loud sentinel for it rather than
    quietly moving a boundary.
    """
    out: dict[str, Any] = {}
    minimum = col.get("minimum", col.get("min"))
    maximum = col.get("maximum", col.get("max"))
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        out["min_value"] = minimum
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        out["max_value"] = maximum
    if out:
        return out

    for rule in _rules_of_type(col, "range"):
        constraint = rule.get("constraint")
        if not isinstance(constraint, str):
            continue
        between = _BETWEEN_RE.match(constraint)
        if between is not None:
            return {
                "min_value": _as_number(between.group(1)),
                "max_value": _as_number(between.group(2)),
            }
        bounds = _RANGE_BOUND_RE.findall(constraint)
        if not bounds:
            continue
        parsed: dict[str, Any] = {}
        strict_flags: list[bool] = []
        for operator, literal in bounds:
            key = "min_value" if operator.startswith(">") else "max_value"
            if key in parsed:
                continue
            parsed[key] = _as_number(literal)
            strict_flags.append(operator in (">", "<"))
        if not parsed:
            continue
        if all(strict_flags):
            parsed["strictly"] = True
        elif any(strict_flags):
            parsed["strictly"] = None  # mixed — caller fails loud
        return parsed
    return {}


def _as_number(literal: str) -> Any:
    """``"12"`` → ``12``; ``"1.5"`` → ``1.5``."""
    return float(literal) if "." in literal else int(literal)


def column_relationship(col: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the ``(to, field)`` foreign-key reference declared on a column.

    Surfaces ``relationships`` in the engine path (previously only the copilot
    generator emitted it). Recognises the copilot ``{to, field}`` dict shape,
    the ODCS-style ``"table.field"`` string (also used by ``datacontract-cli``
    → dbt relationships), and the schema-canonical
    ``validationRules[] {type: custom, constraint: "references model.field"}``
    — the exact shape ``fluid import dbt`` writes when it recovers a dbt
    ``relationships`` test, so that test survives a full round trip.
    Returns ``None`` when no complete FK is declared.
    """
    for key in _FK_KEYS:
        raw = col.get(key)
        if isinstance(raw, Mapping):
            to = raw.get("to") or raw.get("table") or raw.get("model")
            field = raw.get("field") or raw.get("column")
            if to and field:
                return str(to), str(field)
        elif isinstance(raw, str) and "." in raw:
            to, _, field = raw.rpartition(".")
            if to and field:
                return to, field
    for rule in _rules_of_type(col, "custom"):
        constraint = rule.get("constraint")
        if not isinstance(constraint, str):
            continue
        match = _REFERENCES_RE.match(constraint)
        if match is not None:
            return match.group(1), match.group(2)
    return None


def constraint_tests(col: Mapping[str, Any]) -> list[Any]:
    """Translate one schema column's field-level constraints to dbt tests.

    ``required`` → ``not_null``; a declared key → ``unique``; ``enum`` /
    ``acceptedValues`` → ``accepted_values``; ``minimum`` / ``maximum`` →
    the numeric range dialect; a foreign-key reference → ``relationships``.

    Each of those is read from the inline spelling *and* from the
    schema-canonical ``validationRules[]`` (see the note on
    :data:`_RANGE_BOUND_RE`), so the mapping is reachable from a contract that
    passes ``fluid validate``.
    """
    tests: list[Any] = []

    if is_truthy(col.get("required")):
        tests.append(not_null_test())

    if column_is_key(col):
        tests.append(unique_test())

    rel = column_relationship(col)
    if rel is not None:
        tests.append(relationships_test(*rel))

    values = column_enum_values(col)
    if values:
        tests.append(accepted_values_test(values))

    bounds = dict(column_range(col))
    if bounds:
        if "strictly" in bounds and bounds["strictly"] is None:
            # Mixed inclusive/exclusive bounds — a single ``strictly`` flag
            # cannot express them, and picking one would silently move a
            # boundary. Fail loud instead.
            bounds.pop("strictly")
            tests.append(sentinel_test("range_mixed_strictness", str(col.get("name") or "")))
        else:
            range_test = numeric_range_test(**bounds)
            if range_test is not None:
                tests.append(range_test)

    return tests


# ── dq.rules[] → dbt test (forward) ───────────────────────────────────────


def rule_type(rule: Mapping[str, Any]) -> str:
    """Normalised lowercase ``dqRule.type``."""
    kind = rule.get("type") or ""
    return kind.strip().lower() if isinstance(kind, str) else ""


def valid_values(rule: Mapping[str, Any]) -> list[Any]:
    """Extract the value list for a ``valid_values`` rule.

    The v0.7.x ``dqRule`` schema has no dedicated value-list field, so the value
    set is carried on an explicit ``validValues`` / ``values`` key (quality
    engine), a nested ``parameters.values`` (engine path), or parsed from a
    ``description`` of the form ``"<col> valid values: a, b, c."`` — mirrors
    ``providers/quality_engine.py`` so the artifact and the live checker agree.
    """
    for key in ("validValues", "values"):
        raw = rule.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            vals = [v for v in raw if v is not None]
            if vals:
                return vals

    params = rule.get("parameters")
    if isinstance(params, Mapping):
        raw = params.get("values")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            vals = [v for v in raw if v is not None]
            if vals:
                return vals

    description = rule.get("description")
    if isinstance(description, str) and " valid values:" in description.lower():
        import re

        m = re.search(r"valid values:\s*([^.]+)", description, re.IGNORECASE)
        if m:
            return [v.strip() for v in m.group(1).split(",") if v.strip()]

    return []


def forward_column_rule(rule: Mapping[str, Any], column: str) -> Any | None:
    """Map one column-scoped ``dqRule`` to a dbt test entry (string or dict)."""
    kind = rule_type(rule)

    if kind == "completeness":
        return not_null_test()

    if kind == "uniqueness":
        return unique_test()

    if kind == "valid_values":
        values = valid_values(rule)
        if values:
            return accepted_values_test(values)
        return sentinel_test("valid_values", column)

    if kind == "accuracy":
        threshold = rule.get("threshold")
        operator = _SQL_OPERATORS.get(str(rule.get("operator") or ">=").strip())
        if (
            operator is not None
            and isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
        ):
            # ``dbt_utils.expression_is_true`` attached to a column compiles to
            # ``where not(<column_name> <expression>)`` — the expression is the
            # right-hand side only. Emit exactly the comparison the live
            # quality engine runs for an accuracy rule
            # (``providers/quality_engine.py::_check_accuracy`` →
            # ``MIN(col) <operator> <threshold>``), so the artifact and the
            # checker agree. A SQL *comment* here (the previous placeholder)
            # swallowed the closing paren and made every `dbt test` fail with
            # an opaque Snowflake parser error.
            return expression_test(f"{operator} {threshold}")
        return sentinel_test("accuracy", column)

    if kind == "freshness":
        # Never reached through a column: freshness is model-scoped (see
        # MODEL_SCOPED_RULE_TYPES). Kept explicit so a caller that ignores
        # the partition still emits a fail-loud sentinel rather than the
        # unparseable column-attached recency test.
        return sentinel_test("freshness", column)

    if kind in ("schema", "anomaly_detection", "drift_detection"):
        return sentinel_test(kind, column)

    return sentinel_test(f"unmapped_{kind}", column)


def forward_model_rule(rule: Mapping[str, Any], *, field: str | None = None) -> Any | None:
    """Map one ``dqRule`` to a model-level dbt test.

    ``field`` is the timestamp column a freshness test should measure. It
    comes from the rule's own ``selector`` when the rule names a column, and
    otherwise from :func:`default_recency_field` (the contract's first
    temporal column). The previous hardcoded ``updated_at`` named a column
    that appears nowhere in the contract, so the emitted test failed with
    ``invalid identifier``.
    """
    kind = rule_type(rule)

    if kind == "freshness":
        if field:
            # A missing window keeps ``recency_test``'s documented day/1
            # default; only the *column* is non-negotiable.
            return recency_test(field, window=rule.get("window") or rule.get("threshold"))
        # No column to measure — fail loud rather than inventing an identifier.
        return f"{SENTINEL_PREFIX}freshness_check"

    if kind in ("anomaly_detection", "drift_detection"):
        threshold = rule.get("threshold")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            return row_count_test(threshold)
        return sentinel_test(kind)

    if kind in ("completeness", "uniqueness", "valid_values", "accuracy", "schema"):
        # These need a column to be meaningful; a "*" selector is a smell.
        return sentinel_test(f"{kind}_table_level")

    return sentinel_test(f"unmapped_{kind}")


def is_model_scoped(rule: Mapping[str, Any]) -> bool:
    """True when this rule's dbt test must attach at model level.

    Either the rule is table-wide (``selector: "*"`` or absent) or its type
    is one dbt can only express as a model-level test
    (:data:`MODEL_SCOPED_RULE_TYPES`).
    """
    selector = rule.get("selector")
    selector = selector.strip() if isinstance(selector, str) else ""
    if not selector or selector == TABLE_SELECTOR:
        return True
    return rule_type(rule) in MODEL_SCOPED_RULE_TYPES


def default_recency_field(schema_cols: Sequence[Any]) -> str | None:
    """The column a table-wide freshness rule should measure.

    The first column in ``contract.schema[]`` whose declared type is
    temporal. Returns ``None`` when the contract declares no temporal
    column, in which case the caller emits a fail-loud sentinel instead of
    a test bound to a column that does not exist.
    """
    for col in schema_cols or ():
        if not isinstance(col, Mapping):
            continue
        name = col.get("name")
        if not isinstance(name, str) or not name:
            continue
        declared = str(col.get("type", "")).strip().lower()
        base = declared.split("(", 1)[0].strip()
        if base.startswith(("timestamp", "datetime", "date")):
            return name
    return None


def partition_rules(
    rules: Sequence[Mapping[str, Any]],
    *,
    schema_cols: Sequence[Any] = (),
) -> tuple[dict[str, list[Any]], list[Any]]:
    """Split ``dq.rules[]`` into per-column tests and model-level tests.

    The **one** router every dbt-emitting surface uses, so the engine path
    (``engines/dbt/schema_yml.py``) and the exporter
    (``exporters/dbt_tests.py``) cannot disagree about where a rule lands —
    the divergence that made ``selector: "*"`` become a dbt column literally
    named ``*`` on the engine path while the exporter routed it correctly.

    Returns ``({column_name: [tests]}, [model_tests])``.
    """
    by_column: dict[str, list[Any]] = {}
    model_tests: list[Any] = []
    fallback_field = default_recency_field(schema_cols)

    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        selector = rule.get("selector")
        selector = selector.strip() if isinstance(selector, str) else ""
        column = "" if selector == TABLE_SELECTOR else selector

        if is_model_scoped(rule):
            dbt_test = forward_model_rule(rule, field=column or fallback_field)
            if dbt_test is not None:
                model_tests.append(dbt_test)
            continue

        dbt_test = forward_column_rule(rule, selector)
        if dbt_test is not None:
            by_column.setdefault(selector, []).append(dbt_test)

    return by_column, model_tests


# ── De-duplication (merge dq-rule tests with constraint-derived tests) ─────


def test_identity(test: Any) -> Any:
    """A hashable identity for a dbt test entry, for de-duplication.

    String tests (``not_null``) identify by name; single-key dict tests
    (``{accepted_values: ...}``) identify by their test-name key so two sources
    can't emit the same generic test twice for one column.
    """
    if isinstance(test, str):
        return test
    if isinstance(test, Mapping) and len(test) == 1:
        return next(iter(test))
    return repr(test)


def merge_tests(primary: list[Any], extra: list[Any]) -> list[Any]:
    """Concatenate two test lists, dropping duplicates by test identity.

    ``primary`` wins on collision so an explicitly-tuned dq rule isn't clobbered
    by the generic constraint-derived form.
    """
    merged: list[Any] = []
    taken: set[Any] = set()
    for test in (*primary, *extra):
        identity = test_identity(test)
        if identity in taken:
            continue
        taken.add(identity)
        merged.append(test)
    return merged


# ── Forward / reverse tables (bijective mappable subset) ──────────────────
# The dbt-manifest importer (planned) consumes REVERSE_TEST_TO_RULE so the
# reverse translation reuses this module instead of becoming a 4th mapping.
#
# The two tables are hand-written inverses over the mappable subset; the
# round-trip (``reverse(forward(t)) == t`` and vice-versa) is pinned in
# ``tests/test_dbt_tests_exporter.py``. Anything outside this subset —
# ``relationships`` (no FLUID dqRule.type for referential integrity), the
# numeric range test (no ``range`` dqRule.type), and the ``fluid_*`` sentinels
# (deliberately non-standard) — is intentionally not reversible.

FORWARD_RULE_TO_TEST: dict[str, str] = {
    "completeness": "not_null",
    "uniqueness": "unique",
    "valid_values": "accepted_values",
    "accuracy": EXPRESSION_TEST_NAME,
    "freshness": RECENCY_TEST_NAME,
}

REVERSE_TEST_TO_RULE: dict[str, str] = {
    "not_null": "completeness",
    "unique": "uniqueness",
    "accepted_values": "valid_values",
    EXPRESSION_TEST_NAME: "accuracy",
    RECENCY_TEST_NAME: "freshness",
}


def test_name(test: Any) -> str | None:
    """Return the dbt generic-test name for a test entry (string or 1-key dict)."""
    if isinstance(test, str):
        return test
    if isinstance(test, Mapping) and len(test) == 1:
        return next(iter(test))
    return None


def rule_type_to_test_name(dq_rule_type: str) -> str | None:
    """Forward: a canonical ``dqRule.type`` → its dbt generic-test name."""
    return FORWARD_RULE_TO_TEST.get((dq_rule_type or "").strip().lower())


def test_to_rule_type(test: Any) -> str | None:
    """Reverse: a dbt test entry → the FLUID ``dqRule.type`` it derives from.

    The hook the manifest importer consumes. Returns ``None`` for tests outside
    the mappable subset (relationships, range, ``fluid_*`` sentinels).
    """
    name = test_name(test)
    if name is None:
        return None
    return REVERSE_TEST_TO_RULE.get(name)
