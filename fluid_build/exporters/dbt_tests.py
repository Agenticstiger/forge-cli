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

The sentinel ``# managed-by: fluid`` header lets re-runs detect and
overwrite without clobbering hand-edits outside that block (the runner
enforces the boundary).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

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


def render_dbt_tests(contract: Mapping[str, Any], *, tests_key: str | None = None) -> str:
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

    doc: dict[str, Any] = {"version": 2, "models": models}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return (
        f"{MANAGED_BY_SENTINEL}\n"
        "# Generated from fluid contract — do not edit between the sentinels.\n"
        f"{body}"
    )


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
        col_tests = _tm.merge_tests(
            list(tests_by_column.get(name) or []),
            _tm.constraint_tests(col),
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
