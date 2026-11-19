# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
``minimum`` /        ``dbt_expectations.expect_column_values_to_be_between``
``maximum``          (inclusive bounds — the one range dialect, see
                     ``engines/dbt/_test_mapping.py``)
==================== =================================================

The actual translation is delegated to the single shared mapping module
:mod:`fluid_build.engines.dbt._test_mapping` so this exporter, the
``engines/dbt`` engine, and the copilot generator cannot drift.

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

On a 0.7.7 contract each declared invariant, identifier format, bounded placeholder
declaration and shared attribute also becomes one model-level ``fluid_rule`` test
(:mod:`fluid_build.data_rules._dbt`), and a column's placeholders are exempt from its own
``accepted_values`` and range tests through a ``where`` config.

The sentinel ``# managed-by: fluid`` header lets re-runs detect and
overwrite without clobbering hand-edits outside that block (the runner
enforces the boundary).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Sequence

# The single shared contract → dbt-test mapping. Reached via module attribute
# access (``_tm.<fn>``) so a ``patch("...engines.dbt._test_mapping.<fn>")``
# flows through to every call site. This is the richest historical surface
# (expression_is_true / recency / fluid_* sentinels); its logic now lives in
# the shared module so the engine + copilot paths cannot drift from it.
from ..engines.dbt import _test_mapping as _tm

# Shared tests:/data_tests: dialect handling (dbt-core 1.8 renamed the key;
# Fusion requires the modern spelling). Single source of truth so this
# exporter and the engine emitters cannot drift.
from ..engines.dbt.schema_yml import TESTS_KEY_LEGACY, normalize_tests_key

# Block sentinel — the runner uses this to detect a managed file and refuse
# to clobber a hand-edited one.
MANAGED_BY_SENTINEL = "# managed-by: fluid"


@dataclass(frozen=True)
class DbtTests:
    """What :func:`render_project_tests` writes: the schema.yml text, whether it calls the
    ``fluid_rule`` macro (which the project must then carry), and the declared rules it
    could not render, each with its reason."""

    yaml: str
    uses_fluid_rule: bool
    skipped: List[Any] = field(default_factory=list)


def render_dbt_tests(contract: Mapping[str, Any], *, tests_key: str | None = None) -> str:
    """:func:`render_project_tests`'s schema.yml text."""
    return render_project_tests(contract, tests_key=tests_key).yaml


def render_project_tests(contract: Mapping[str, Any], *, tests_key: str | None = None) -> DbtTests:
    """Render a parsed FLUID contract as a dbt schema.yml string.

    The returned text is a single multi-doc YAML stream. Callers should
    write it to ``<dbt_project>/models/<schema>/schema.yml`` or similar.

    Parameters
    ----------
    contract:
        Parsed contract dict (FLUID v0.7.x — as produced by
        ``loader.load_with_overlay``).
    tests_key:
        YAML key data tests attach under — ``"tests"`` (legacy, default;
        the only spelling dbt-core <1.8 understands) or ``"data_tests"``
        (dbt-core >=1.8; required by the strict-parsing Fusion engine).

    Returns
    -------
    str
        UTF-8 YAML text ready to write to disk.
    """
    resolved_tests_key = normalize_tests_key(tests_key)
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
        model_block = _expose_to_model(expose, tests_key=resolved_tests_key)
        if model_block is not None:
            models.append(model_block)

    # Declared invariants (0.7.7): one ``fluid_rule`` test each, at model level.
    rule_tests, skipped = _rule_tests(contract)
    for model in models:
        extra = rule_tests.get(model["name"])
        if extra:
            model[resolved_tests_key] = list(model.get(resolved_tests_key) or []) + extra
            # dbt reads a model's keys in any order; keep columns last, as before.
            if "columns" in model:
                model["columns"] = model.pop("columns")

    doc: dict[str, Any] = {"version": 2, "models": _inert_jinja(models)}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    text = (
        f"{MANAGED_BY_SENTINEL}\n"
        "# Generated from fluid contract — do not edit between the sentinels.\n"
        f"{body}"
    )
    return DbtTests(text, any(rule_tests.values()), skipped)


def _rule_tests(contract: Mapping[str, Any]) -> tuple[dict[str, list[Any]], list[Any]]:
    """The ``fluid_rule`` tests by model name, and the rules skipped (``data_rules._dbt``)."""
    from ..data_rules import FIRST_VERSION, _version

    version = _version(contract)
    if not version or version < FIRST_VERSION:
        return {}, []
    from ..data_rules._dbt import rule_tests

    names = {
        e.get("exposeId"): _model_name(e)
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping) and isinstance(e.get("exposeId"), str)
    }
    return rule_tests(contract, names.get)


def _expose_to_model(
    expose: Mapping[str, Any], *, tests_key: str = TESTS_KEY_LEGACY
) -> dict[str, Any] | None:
    """Convert one ``exposes[i]`` entry to a dbt model dict."""
    name = _model_name(expose)
    if not name:
        return None

    rules = _dq_rules(expose)
    tests_by_column, model_tests = _group_rules(rules, _schema_cols(expose))

    columns = _columns_with_tests(expose, tests_by_column, tests_key=tests_key)
    description = expose.get("description") or expose.get("title") or ""

    model: dict[str, Any] = {
        "name": name,
        "description": description,
    }

    # Table-wide (``selector: "*"``) rules become dbt model-level tests.
    if model_tests:
        model[tests_key] = model_tests

    if columns:
        model["columns"] = columns
    return model


def _model_name(expose: Mapping[str, Any]) -> str:
    """Name lookup for the dbt model these tests attach to.

    The **exposeId**, because that is the dbt *node* name: the engine path
    writes the model to ``models/marts/<exposeId>.sql`` and declares
    ``- name: <exposeId>`` in its own schema.yml. This exporter's own help
    tells the user to drop the output into that project and run
    ``dbt test``, so any other spelling produces tests bound to a node dbt
    cannot find — which dbt downgrades to a ``[WARNING]`` and then reports
    ``PASS=3 ... Completed successfully`` with zero data-quality coverage.

    ``binding.location.table`` is only the fallback, for an expose that
    declares no exposeId at all.
    """
    eid = expose.get("exposeId") or expose.get("id")
    if isinstance(eid, str) and eid:
        return eid

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
    return ""


def _schema_cols(expose: Mapping[str, Any]) -> Sequence[Any]:
    """Return the ``contract.schema[]`` column list for an expose."""
    contract = expose.get("contract")
    if not isinstance(contract, Mapping):
        return []
    schema = contract.get("schema")
    if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        return schema
    return []


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
    schema_cols: Sequence[Any] = (),
) -> tuple[dict[str, list[Any]], list[Any]]:
    """Split dq rules into per-column tests and model-level tests.

    Delegates to :func:`._test_mapping.partition_rules` — the single router
    the engine path uses too, so both surfaces put a rule in the same place.
    ``schema_cols`` supplies the timestamp column a table-wide freshness rule
    measures (previously hardcoded to ``updated_at``, a column that appears
    in no contract).
    """
    return _tm.partition_rules(rules, schema_cols=schema_cols)


def _columns_with_tests(
    expose: Mapping[str, Any],
    tests_by_column: Mapping[str, list[Any]],
    *,
    tests_key: str = TESTS_KEY_LEGACY,
) -> list[dict[str, Any]]:
    """Build the dbt ``columns:`` block, attaching per-column tests.

    Reads ``contract.schema[]`` (v0.7.x) — an array of column objects
    (``{name, type, ...}``). Two test sources are merged per column:

    1. ``tests_by_column`` — tests derived from ``contract.dq.rules[]``
       (the data-quality block).
    2. The column's own *field-level constraints* — ``required`` → ``not_null``,
       a declared key (``unique`` / ``primaryKey`` / ``pk`` / ``identifier``) →
       ``unique``, ``enum`` / ``acceptedValues`` → ``accepted_values``, and
       ``minimum`` / ``maximum`` →
       ``dbt_expectations.expect_column_values_to_be_between``.

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
        col_tests = _exempt_placeholders(
            col,
            _tm.merge_tests(list(tests_by_column.get(name) or []), _tm.constraint_tests(col)),
        )
        if col_tests:
            col_block[tests_key] = col_tests
        out.append(col_block)

    # A dq rule may target a column not declared in contract.schema[]
    # (author error, or schema discovered at runtime). Don't silently
    # drop the test — emit a column entry for it so dbt still runs it.
    for col_name, col_tests in tests_by_column.items():
        if col_name in seen:
            continue
        out.append({"name": col_name, tests_key: col_tests})

    return out


def _exempt_placeholders(col: Mapping[str, Any], tests: list[Any]) -> list[Any]:
    """The column's ``accepted_values`` and range tests leave its placeholder rows out
    (:func:`fluid_build.data_rules._dbt.exempt_placeholders`)."""
    if not isinstance(col.get("placeholders"), Mapping):
        return tests
    from ..data_rules._dbt import exempt_placeholders

    return exempt_placeholders(col, tests, _GeneratedJinja)


_JINJA_DELIMITERS = ("{{", "{%", "{#")
#: dbt also evaluates a test argument that looks like one of these calls, braces or not
#: (``dbt.clients.jinja.add_rendered_test_kwargs``): an enum value ``env_var('SECRET')`` would
#: read the variable.
_LOOKS_LIKE_CALL = re.compile(r"^\s*(env_var|ref|var|source|doc)\s*\(.+\)\s*$", re.S)
#: The ``relationships`` test's own ``ref('<model>')``, kept when the model is a plain name.
_OWN_REF = re.compile(r"ref\('[A-Za-z0-9_]+'\)")


class _GeneratedJinja(str):
    """Jinja this module built from literals (a placeholder ``where``): left as written."""


def _inert_jinja(value: Any) -> Any:
    """``value`` with every string that holds a Jinja delimiter written as a Jinja string
    literal of itself (:func:`fluid_build.data_rules._dbt.jinja_text`).

    dbt renders a schema.yml's names, descriptions and test arguments as Jinja, so a column
    named ``x{{ env_var('SECRET') }}`` or an enum value holding ``{%`` would run during
    ``dbt parse``, and so would a test argument that is a bare ``env_var(...)``-style call.
    Every other string is left as written, so the file for every other contract is
    unchanged; so is the ``relationships`` test's own ``ref('<model>')`` when the model is a
    plain name (any other model name is a literal, and the test then names no model).
    The ``fluid_rule`` test bodies are already Jinja built from literals; they are known by
    their type (``GeneratedTest``), never by a key name, which a contract could also write.
    """
    if isinstance(value, _GeneratedJinja):
        return str(value)
    from ..data_rules._dbt import GeneratedTest

    if isinstance(value, GeneratedTest):
        return _plain(value)
    if isinstance(value, str):
        unsafe = any(d in value for d in _JINJA_DELIMITERS) or _LOOKS_LIKE_CALL.match(value)
        if unsafe and not _OWN_REF.fullmatch(value):
            from ..data_rules._dbt import jinja_text

            return jinja_text(value)
        return value
    if isinstance(value, Mapping):
        return {key: _inert_jinja(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        # YAML's !!pairs and !!omap load as tuples, and dbt renders the strings inside
        # whatever list safe_dump makes of them.
        return [_inert_jinja(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, datetime.date)):
        return value
    return _inert_jinja(str(value))  # any other type: its text, escaped like any text


def _plain(value: Any) -> Any:
    """``value`` as plain dicts, lists and strings (yaml.safe_dump takes no subclass)."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return str(value) if isinstance(value, str) else value
