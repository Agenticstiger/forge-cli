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

"""Render a contract's data-quality block as a dbt ``schema.yml`` document.

Reads the FLUID v0.7.x contract shape:

* ``exposes[].contract.schema[]``  — tabular columns (``{name, type, ...}``),
  including any *field-level constraints* declared inline on a column
  (``required`` / ``unique`` / ``enum`` / ``minimum`` / ``maximum``)
* ``exposes[].contract.dq.rules[]`` — data-quality rules (``dqRule``)
* ``exposes[].binding.location.table`` — the physical table the dbt model
  binds to (falls back to ``exposeId``)

Two test sources feed each column and are merged (deduped): the ``dq.rules[]``
block AND the column's own inline constraints. The inline-constraint mapping is:

==================== =================================================
column constraint    dbt test
==================== =================================================
``required: true``   ``not_null``
``unique`` / key     ``unique`` (``primaryKey`` / ``pk`` / ``identifier``
                     / ``labels.constraint: primary_key`` also count)
``enum`` /           ``accepted_values`` with the declared value list
``acceptedValues``
``minimum`` /        ``dbt_utils.accepted_range`` (inclusive bounds)
``maximum``
==================== =================================================

Each ``dqRule`` carries a ``type`` from the v0.7.3 enum
(``completeness | uniqueness | valid_values | accuracy | freshness |
schema | anomaly_detection | drift_detection``) and a ``selector`` —
the column the rule targets, or ``"*"`` for a table-wide rule.

Mapping to dbt's built-in generic tests (the four dbt ships:
``not_null``, ``unique``, ``accepted_values``, ``relationships``):

================= ==================================================
``dqRule.type``   dbt test
================= ==================================================
completeness      ``not_null`` (column) — the canonical "no NULLs" check
uniqueness        ``unique`` (column)
valid_values      ``accepted_values`` with the rule's value list
accuracy          ``dbt_utils.expression_is_true`` placeholder when a
                  threshold is given, else a ``fluid_accuracy_*``
                  sentinel test name
freshness         ``dbt_utils.recency`` when a column + window is
                  present, else a ``fluid_freshness_*`` sentinel
schema /          model-level sentinel test names — dbt surfaces a
anomaly_detection clean "test not found" error pointing at the gap
drift_detection   rather than silently dropping the contract intent
================= ==================================================

Output schema follows dbt's tests semantics. ``not_null`` / ``unique``
are emitted as bare string test names (dbt convention); everything
else is a single-key dict. The output is one YAML doc covering every
expose; column-targeting rules attach under the matching
``columns[].tests`` entry, ``"*"``-targeting rules attach at the
model level.

The sentinel ``# managed-by: fluid`` header lets re-runs detect and
overwrite without clobbering hand-edits outside that block (the runner
enforces the boundary).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# Block sentinel — the runner uses this to detect a managed file and refuse
# to clobber a hand-edited one.
MANAGED_BY_SENTINEL = "# managed-by: fluid"

# Selector value that marks a table-wide (not column-scoped) dq rule.
_TABLE_SELECTOR = "*"


def render_dbt_tests(contract: Mapping[str, Any]) -> str:
    """Render a parsed FLUID contract as a dbt schema.yml string.

    The returned text is a single multi-doc YAML stream. Callers should
    write it to ``<dbt_project>/models/<schema>/schema.yml`` or similar.

    Parameters
    ----------
    contract:
        Parsed contract dict (FLUID v0.7.x — as produced by
        ``loader.load_with_overlay``).

    Returns
    -------
    str
        UTF-8 YAML text ready to write to disk.
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover — pyyaml is a hard dep
        raise RuntimeError("PyYAML is required to render dbt tests") from e

    if not isinstance(contract, Mapping):
        raise TypeError(f"contract must be a Mapping, got {type(contract).__name__}")

    models: list[dict[str, Any]] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        model_block = _expose_to_model(expose)
        if model_block is not None:
            models.append(model_block)

    doc: dict[str, Any] = {"version": 2, "models": models}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return (
        f"{MANAGED_BY_SENTINEL}\n"
        "# Generated from fluid contract — do not edit between the sentinels.\n"
        f"{body}"
    )


def _expose_to_model(expose: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert one ``exposes[i]`` entry to a dbt model dict."""
    name = _model_name(expose)
    if not name:
        return None

    rules = _dq_rules(expose)
    tests_by_column, model_tests = _group_rules(rules)

    columns = _columns_with_tests(expose, tests_by_column)
    description = expose.get("description") or expose.get("title") or ""

    model: dict[str, Any] = {
        "name": name,
        "description": description,
    }

    # Table-wide (``selector: "*"``) rules become dbt model-level tests.
    if model_tests:
        model["tests"] = model_tests

    if columns:
        model["columns"] = columns
    return model


def _model_name(expose: Mapping[str, Any]) -> str:
    """Best-effort name lookup for the dbt model.

    Prefers the binding location's table identifier (FLUID v0.7.x
    ``binding.location.table``), falls back to the exposeId. dbt models
    are named after their source table so this is the right hook for
    downstream ``dbt test`` runs to bind to.
    """
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        location = binding.get("location")
        if isinstance(location, Mapping):
            # v0.7.x: binding.location.table (also accept dataset/topic-style
            # identifiers as a fallback for non-table bindings).
            table = (
                location.get("table")
                or location.get("name")
                or location.get("topic")
                or location.get("object")
            )
            if isinstance(table, str) and table:
                return table
    eid = expose.get("exposeId") or expose.get("id")
    return eid if isinstance(eid, str) else ""


def _dq_rules(expose: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the ``contract.dq.rules[]`` list for an expose (v0.7.x shape)."""
    contract = expose.get("contract")
    if not isinstance(contract, Mapping):
        return []
    dq = contract.get("dq")
    if not isinstance(dq, Mapping):
        return []
    rules = dq.get("rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    return [r for r in rules if isinstance(r, Mapping)]


def _group_rules(
    rules: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[Any]], list[Any]]:
    """Split dq rules into per-column tests and model-level tests.

    A rule's ``selector`` is the column it targets; ``"*"`` (or an empty
    selector) marks a table-wide rule that maps to a dbt model-level test.
    """
    by_column: dict[str, list[Any]] = {}
    model_tests: list[Any] = []

    for rule in rules:
        selector = rule.get("selector")
        selector = selector.strip() if isinstance(selector, str) else ""

        if selector and selector != _TABLE_SELECTOR:
            dbt_test = _convert_column_rule(rule, selector)
            if dbt_test is not None:
                by_column.setdefault(selector, []).append(dbt_test)
        else:
            dbt_test = _convert_model_rule(rule)
            if dbt_test is not None:
                model_tests.append(dbt_test)

    return by_column, model_tests


def _columns_with_tests(
    expose: Mapping[str, Any], tests_by_column: Mapping[str, list[Any]]
) -> list[dict[str, Any]]:
    """Build the dbt ``columns:`` block, attaching per-column tests.

    Reads ``contract.schema[]`` (v0.7.x) — an array of column objects
    (``{name, type, ...}``). Two test sources are merged per column:

    1. ``tests_by_column`` — tests derived from ``contract.dq.rules[]``
       (the data-quality block).
    2. The column's own *field-level constraints* — ``required`` → ``not_null``,
       a declared key (``unique`` / ``primaryKey`` / ``pk`` / ``identifier``) →
       ``unique``, ``enum`` / ``acceptedValues`` → ``accepted_values``, and
       ``minimum`` / ``maximum`` → ``dbt_utils.accepted_range``.

    Without (2) a contract that expressed its quality intent inline on the
    schema (the common case) produced a dbt model with column names but **zero
    executable tests**. Both sources are deduped so a column declared
    ``required: true`` *and* covered by a ``completeness`` dq rule gets a single
    ``not_null``.
    """
    contract = expose.get("contract")
    cols_in: Sequence[Any] = []
    if isinstance(contract, Mapping):
        schema = contract.get("schema")
        if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
            cols_in = schema

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for col in cols_in:
        if not isinstance(col, Mapping):
            continue
        name = col.get("name")
        if not isinstance(name, str) or not name:
            continue
        seen.add(name)
        col_block: dict[str, Any] = {"name": name}
        desc = col.get("description") or col.get("businessName")
        if desc:
            col_block["description"] = desc
        col_tests = _merge_tests(
            list(tests_by_column.get(name) or []),
            _constraint_tests_for_column(col),
        )
        if col_tests:
            col_block["tests"] = col_tests
        out.append(col_block)

    # A dq rule may target a column not declared in contract.schema[]
    # (author error, or schema discovered at runtime). Don't silently
    # drop the test — emit a column entry for it so dbt still runs it.
    for col_name, col_tests in tests_by_column.items():
        if col_name in seen:
            continue
        out.append({"name": col_name, "tests": col_tests})

    return out


# ── Field-level constraint → dbt test mapping ─────────────────────────
#
# Mirrors the canonical FLUID column-constraint key recognition already used
# by ``copilot.enrichment._map_fluid_column_to_wave2`` so the dbt artifact and
# the rest of the codebase agree on which keys mean what. The dbt test names
# follow the dbt ecosystem conventions confirmed against the docs:
#   - not_null / unique / accepted_values — dbt built-in generic tests
#     (https://docs.getdbt.com/reference/resource-properties/data-tests)
#   - dbt_utils.accepted_range — numeric range test
#     (https://github.com/dbt-labs/dbt-utils)
# Tests are emitted under the ``tests:`` key (not ``data_tests:``) to stay
# consistent with the dq-rules path above and ``engines/dbt`` — ``tests`` is
# still valid in current dbt (the 1.8+ rename concerns test *arguments*, not
# the property name).

# Truthy markers a column may carry to declare it a uniqueness key. ``labels``
# is a free-form string map in v0.7.x; ``labels.unique: "true"`` and
# ``labels.constraint: "primary_key"`` are an in-the-wild convention (see
# examples/bitcoin-price-api-declarative-part-c).
_PK_KEYS = ("primary", "primaryKey", "primary_key", "pk", "isPrimary")
_ENUM_KEYS = ("enum", "acceptedValues", "accepted_values")


def _is_truthy(value: Any) -> bool:
    """Loose truthiness for YAML/JSON booleans-as-strings (``"true"``/``True``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _column_is_key(col: Mapping[str, Any]) -> bool:
    """True when the column is declared a uniqueness key / primary key."""
    if _is_truthy(col.get("unique")):
        return True
    if any(_is_truthy(col.get(k)) for k in _PK_KEYS):
        return True
    if str(col.get("semanticType") or "").strip().lower() in ("identifier", "primary_key"):
        return True
    labels = col.get("labels")
    if isinstance(labels, Mapping):
        if _is_truthy(labels.get("unique")):
            return True
        if str(labels.get("constraint") or "").strip().lower() in ("primary_key", "unique"):
            return True
    return False


def _column_enum_values(col: Mapping[str, Any]) -> list[Any]:
    """Return the accepted-value list declared on a column, if any."""
    for key in _ENUM_KEYS:
        raw = col.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            vals = [v for v in raw if v is not None]
            if vals:
                return vals
    return []


def _column_range(col: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``{min_value, max_value}`` declared on a column, if any.

    Accepts both ``minimum``/``maximum`` (JSON-Schema spelling) and the
    ``min``/``max`` aliases the copilot mapper recognises.
    """
    out: dict[str, Any] = {}
    minimum = col.get("minimum", col.get("min"))
    maximum = col.get("maximum", col.get("max"))
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        out["min_value"] = minimum
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        out["max_value"] = maximum
    return out


def _constraint_tests_for_column(col: Mapping[str, Any]) -> list[Any]:
    """Translate one schema column's field-level constraints to dbt tests."""
    tests: list[Any] = []

    # required → not_null
    if _is_truthy(col.get("required")):
        tests.append("not_null")

    # declared key → unique
    if _column_is_key(col):
        tests.append("unique")

    # enum / acceptedValues → accepted_values
    values = _column_enum_values(col)
    if values:
        tests.append({"accepted_values": {"values": values}})

    # minimum / maximum → dbt_utils.accepted_range (inclusive bounds)
    bounds = _column_range(col)
    if bounds:
        bounds["inclusive"] = True
        tests.append({"dbt_utils.accepted_range": bounds})

    return tests


def _test_identity(test: Any) -> Any:
    """A hashable identity for a dbt test entry, for de-duplication.

    String tests (``not_null``) identify by their name; dict tests
    (``{accepted_values: ...}``) identify by their single test-name key so two
    sources can't emit the same generic test twice for one column.
    """
    if isinstance(test, str):
        return test
    if isinstance(test, Mapping) and len(test) == 1:
        return next(iter(test))
    return repr(test)


def _merge_tests(primary: list[Any], extra: list[Any]) -> list[Any]:
    """Concatenate two test lists, dropping duplicates by test identity.

    ``primary`` (dq-rule-derived) wins on collision so an explicitly-tuned dq
    rule isn't clobbered by the generic constraint-derived form.
    """
    merged: list[Any] = []
    taken: set[Any] = set()
    for test in (*primary, *extra):
        identity = _test_identity(test)
        if identity in taken:
            continue
        taken.add(identity)
        merged.append(test)
    return merged


def _convert_column_rule(rule: Mapping[str, Any], column: str) -> Any | None:
    """Map one column-scoped ``dqRule`` to a dbt test entry (string or dict)."""
    kind = _rule_type(rule)

    if kind == "completeness":
        # Completeness == "no NULLs" in the column → dbt's not_null.
        return "not_null"

    if kind == "uniqueness":
        return "unique"

    if kind == "valid_values":
        values = _valid_values(rule)
        if values:
            return {"accepted_values": {"values": values}}
        # No value list on the rule — emit a sentinel so the operator
        # sees the gap rather than dropping a declared check silently.
        return f"fluid_valid_values_{column}"

    if kind == "accuracy":
        threshold = rule.get("threshold")
        operator = rule.get("operator") or ">="
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            # Accuracy is contract-specific; surface it as a dbt_utils
            # expression placeholder the operator can tune to their own
            # accuracy predicate. The threshold + operator are preserved
            # in the expression so the intent isn't lost.
            expr = f"-- accuracy({column}) {operator} {threshold}: replace with predicate"
            return {"dbt_utils.expression_is_true": {"expression": expr}}
        return f"fluid_accuracy_{column}"

    if kind == "freshness":
        # Column-scoped freshness → dbt_utils.recency on that column.
        window = rule.get("window") or rule.get("threshold")
        rec: dict[str, Any] = {
            "field": column,
            "datepart": "day",
            "interval": 1,
        }
        if window is not None:
            rec["_fluid_window"] = str(window)
        return {"dbt_utils.recency": rec}

    if kind in ("schema", "anomaly_detection", "drift_detection"):
        # No dbt built-in maps cleanly — emit a sentinel test name so dbt
        # surfaces a clean "test not found" error pointing at the gap.
        return f"fluid_{kind}_{column}"

    # Unknown / unmapped type — leave the operator a breadcrumb.
    return f"fluid_unmapped_{kind}_{column}"


def _convert_model_rule(rule: Mapping[str, Any]) -> Any | None:
    """Map one table-wide (``selector: "*"``) ``dqRule`` to a model-level test."""
    kind = _rule_type(rule)

    if kind == "freshness":
        window = rule.get("window") or rule.get("threshold")
        if window is not None:
            return {
                "dbt_utils.recency": {
                    "field": "updated_at",
                    "datepart": "day",
                    "interval": 1,
                    "_fluid_window": str(window),
                }
            }
        return "fluid_freshness_check"

    if kind in ("anomaly_detection", "drift_detection"):
        threshold = rule.get("threshold")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            return {"dbt_utils.expression_is_true": {"expression": f"count(*) > {threshold}"}}
        return f"fluid_{kind}"

    if kind in ("completeness", "uniqueness", "valid_values", "accuracy", "schema"):
        # These need a column to be meaningful; a "*" selector for them is
        # an authoring smell. Emit a sentinel so the operator sees it.
        return f"fluid_{kind}_table_level"

    return f"fluid_unmapped_{kind}"


def _rule_type(rule: Mapping[str, Any]) -> str:
    """Normalised lowercase ``dqRule.type``."""
    kind = rule.get("type") or ""
    return kind.strip().lower() if isinstance(kind, str) else ""


def _valid_values(rule: Mapping[str, Any]) -> list[Any]:
    """Extract the value list for a ``valid_values`` rule.

    The v0.7.3 ``dqRule`` schema has no dedicated value-list field, so the
    value set is carried either on an explicit ``validValues`` / ``values``
    key (used by the quality engine) or parsed from a ``description`` of
    the form ``"<col> valid values: a, b, c."`` — mirrors
    ``providers/quality_engine.py``'s parsing so the dbt artifact and the
    live checker agree on the value set.
    """
    for key in ("validValues", "values"):
        raw = rule.get(key)
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
