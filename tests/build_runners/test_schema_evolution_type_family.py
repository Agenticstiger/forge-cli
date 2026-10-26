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

"""Upgrading DuckDB is not schema drift.

DuckDB 2.0 infers HUGEINT where 1.5 infers DOUBLE for the same integer literal,
and TIMESTAMP WITH TIME ZONE where 1.5 infers TIMESTAMP for the same JSON
timestamp. The drift gate compared type NAMES, so a DuckDB upgrade alone -- no
contract edit, no new data -- raised ``SchemaDriftError`` on a working pipeline.

:func:`test_the_family_check_does_not_swallow_the_ladder` pins the invariant
that no ordered ``_NUMERIC_ORDER`` pair is ever reported as a non-event. Worth
being precise about what that does and does not prove: with the narrow, explicit
family table, only ``double`` of those eight names belongs to a family at all,
so no pair is same-family and the check's POSITION after the ladder is defensive
rather than load-bearing -- moving it first changes nothing today, and this test
does not currently discriminate on ordering. It is here because the obvious
future refactor is to reuse the broad ``normalize_type`` helper, under which all
eight collapse into one numeric family; that change would make all 56 pairs
same-family, and then ordering decides whether ``type_widened`` and
``type_narrowed`` ever fire again. This test fails loudly at that point.
"""

from __future__ import annotations

import itertools

import pytest

from fluid_build.api.schema import SchemaColumn
from fluid_build.build_runners._schema_evolution import (
    _NUMERIC_ORDER,
    diff_columns,
    family_rename_note,
)


def _event(old: str, new: str) -> str:
    events = diff_columns(
        [SchemaColumn(name="c", type=old)],
        [SchemaColumn(name="c", type=new)],
    )
    return events[0][0] if events else "none"


def test_the_duckdb_version_renames_are_not_drift() -> None:
    for old, new in (
        ("double", "hugeint"),
        ("hugeint", "double"),
        ("double", "bignum"),
        ("timestamp", "timestamptz"),
        ("timestamptz", "timestamp"),
        ("timestamp", "timestamp with time zone"),
    ):
        assert _event(old, new) == "none", f"{old} -> {new} should not be drift"


def test_the_family_check_does_not_swallow_the_ladder() -> None:
    """Every ordered numeric pair must still be classified, not silently dropped.

    All 8 names in ``_NUMERIC_ORDER`` normalise into one numeric family, so a
    family check placed before the ladder would report ``none`` for all 56
    ordered pairs. If this fails, the family comparison has been moved ahead of
    ``_is_widening`` / ``_is_narrowing``.
    """
    pairs = list(itertools.permutations(_NUMERIC_ORDER, 2))
    assert len(pairs) == 56
    swallowed = [(a, b) for a, b in pairs if _event(a, b) == "none"]
    assert swallowed == [], f"the widen/narrow ladder was bypassed for {swallowed}"


def test_widening_and_narrowing_still_report_themselves() -> None:
    assert _event("int", "bigint") == "type_widened"
    assert _event("tinyint", "double") == "type_widened"
    assert _event("bigint", "int") == "type_narrowed"
    assert _event("decimal", "smallint") == "type_narrowed"


def test_a_real_type_change_still_fails() -> None:
    for old, new in (
        ("varchar", "integer"),
        ("integer", "varchar"),
        ("timestamp", "date"),
        ("double", "boolean"),
    ):
        assert _event(old, new) == "type_changed", f"{old} -> {new} is real drift"


def test_an_identical_type_is_still_no_event() -> None:
    assert _event("double", "double") == "none"
    assert _event("DOUBLE", "double") == "none"


def test_the_rename_is_reported_rather_than_hidden() -> None:
    """DOUBLE and HUGEINT are not value-identical, so the note says so.

    For the same CSV literal 1.5 landed a lossy 17-significant-digit float where
    2.0 lands the exact 128-bit integer. Collapsing the pair keeps the pipeline
    running; the note is what keeps that honest.
    """
    note = family_rename_note("double", "hugeint")
    assert note is not None
    assert "not schema drift" in note
    assert "precision" in note
    assert family_rename_note("varchar", "integer") is None


def test_the_family_check_is_case_and_space_insensitive() -> None:
    assert _event("DOUBLE", "HugeInt") == "none"
    assert _event("timestamp", "  TIMESTAMPTZ  ") == "none"


# ── the collapse is allowed, but it is not silent ────────────────────────


def test_the_acquisition_gate_logs_a_family_rename(caplog: pytest.LogCaptureFixture) -> None:
    """Suppressing the FAILURE is intended; suppressing the NOTICE is not.

    ``diff_columns`` emits no event for a family rename, so the drift gate
    returns early and a contract that declared ``schemaPolicy: strict`` would
    otherwise hear nothing at all — even though DOUBLE and HUGEINT are not
    value-identical. Without this wiring ``family_rename_note`` had no
    production caller, which made the module comment's claim that the change is
    "reported rather than hidden" untrue.
    """
    import logging

    from fluid_build.build_runners._acquisition_common import _log_family_renames

    with caplog.at_level(logging.WARNING):
        _log_family_renames(
            [SchemaColumn(name="big", type="double"), SchemaColumn(name="other", type="varchar")],
            [SchemaColumn(name="big", type="hugeint"), SchemaColumn(name="other", type="varchar")],
        )
    messages = [r.message for r in caplog.records]
    assert "acquisition_schema_family_rename" in messages, messages
    assert (
        len([m for m in messages if m == "acquisition_schema_family_rename"]) == 1
    ), "only the renamed column should be reported, not the unchanged one"


def test_the_gate_logs_nothing_when_no_type_changed(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from fluid_build.build_runners._acquisition_common import _log_family_renames

    with caplog.at_level(logging.WARNING):
        _log_family_renames(
            [SchemaColumn(name="a", type="double")],
            [SchemaColumn(name="a", type="double")],
        )
    assert [r.message for r in caplog.records] == []


def test_the_rename_reaches_the_operator_through_the_real_gate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through ``enforce_schema_policy_or_raise``, not the helper directly.

    This is the test that fails if the call is ever removed from the gate: a
    direct call to ``_log_family_renames`` passes either way. ``schemaPolicy:
    strict`` is used deliberately -- that is the contract that would otherwise
    hear nothing, since the family collapse means the gate returns before it
    raises.
    """
    import logging

    from fluid_build.api.schema import SchemaColumn, SchemaFingerprint
    from fluid_build.build_runners._acquisition_common import (
        enforce_schema_policy_or_raise,
    )

    contract = {
        "exposes": [
            {
                "contract": {
                    "schema": [{"name": "big", "type": "double"}],
                    "schemaPolicy": "strict",
                }
            }
        ]
    }

    class _Ctx:
        def __init__(self) -> None:
            self.contract = contract

    class _Runner:
        def fingerprint(self, _ctx: object) -> SchemaFingerprint:
            # What a DuckDB 2.0 install now infers for the same source bytes.
            return SchemaFingerprint.of([SchemaColumn(name="big", type="hugeint")])

    with caplog.at_level(logging.WARNING):
        enforce_schema_policy_or_raise(_Ctx(), _Runner())

    assert "acquisition_schema_family_rename" in [
        r.message for r in caplog.records
    ], "the gate allowed the family rename without telling anyone"
