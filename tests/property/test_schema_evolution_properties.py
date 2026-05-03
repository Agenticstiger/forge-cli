# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Hypothesis properties on the schema-evolution decision engine.

Invariants checked across a randomized space of (baseline, current, policy):

  - same baseline == current: no fail, no decisions
  - strict policy: any change → must_fail True
  - evolve_all: never fails on add/remove (always include / drop / cast)
  - column-order independence: shuffling baseline or current preserves decisions
  - stricter override always overrides default unless default is already stricter
"""

from __future__ import annotations

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from fluid_build.api.schema import EvolutionAction, SchemaColumn, SchemaPolicy
from fluid_build.build_runners._schema_evolution import resolve

_col_name = st.text(
    alphabet=st.characters(whitelist_categories=("L",)),
    min_size=1,
    max_size=15,
).map(str.lower)
_col_type = st.sampled_from(["int", "bigint", "smallint", "string", "varchar", "double", "boolean"])
_column = st.builds(SchemaColumn, name=_col_name, type=_col_type, nullable=st.booleans())
_columns = st.lists(_column, min_size=0, max_size=8, unique_by=lambda c: c.name)


@given(cols=_columns)
@settings(max_examples=80)
def test_no_change_yields_no_decisions(cols):
    """If baseline == current, no decisions, never fail."""
    plan = resolve(cols, cols, SchemaPolicy.STRICT)
    assert not plan.has_failure
    assert plan.decisions == []


@given(baseline=_columns, current=_columns)
@settings(max_examples=80)
def test_strict_fails_on_any_change(baseline, current):
    """Strict: any difference → must fail."""
    plan = resolve(baseline, current, SchemaPolicy.STRICT)
    differs = {c.name for c in current} != {c.name for c in baseline} or any(
        b.type.lower() != c.type.lower() for b in baseline for c in current if b.name == c.name
    )
    assert plan.has_failure == differs


@given(cols_only_added=st.lists(_column, min_size=1, max_size=3, unique_by=lambda c: c.name))
@settings(max_examples=50)
def test_evolve_all_never_fails_on_add(cols_only_added):
    """Adding columns is always safe under evolve_all."""
    plan = resolve([], cols_only_added, SchemaPolicy.EVOLVE_ALL)
    assert not plan.has_failure
    for d in plan.decisions:
        assert d.action in (EvolutionAction.INCLUDE, EvolutionAction.OK)


@given(baseline=_columns, current=_columns)
@settings(max_examples=80)
def test_column_order_invariance(baseline, current):
    """Shuffling either side must produce the same set of decisions."""
    plan_a = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
    shuffled_baseline = list(baseline)
    shuffled_current = list(current)
    random.Random(42).shuffle(shuffled_baseline)
    random.Random(42).shuffle(shuffled_current)
    plan_b = resolve(shuffled_baseline, shuffled_current, SchemaPolicy.EVOLVE_SAFE)
    set_a = {(d.column, d.event, d.action) for d in plan_a.decisions}
    set_b = {(d.column, d.event, d.action) for d in plan_b.decisions}
    assert set_a == set_b


@given(
    base=_column,
)
@settings(max_examples=50)
def test_override_fail_overrides_default_include(base):
    """When baseline is empty and current has a column (added event),
    evolve_all default = INCLUDE; overriding onAddedColumn=fail should win.
    """
    plan = resolve([], [base], SchemaPolicy.EVOLVE_ALL, overrides={"onAddedColumn": "fail"})
    assert plan.has_failure
