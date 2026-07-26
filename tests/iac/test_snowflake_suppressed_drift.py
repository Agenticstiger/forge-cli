# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``lifecycle.ignore_changes = ["column"]`` must be reported, not silent.

The Snowflake emitter deliberately tells ``tofu`` to ignore column drift —
the build engine owns the materialized types and Snowflake rejects most
in-place scale changes. The defect was that the apply then said *nothing*:
a contract whose declared column type no longer matched the live table
planned ``+0 ~0 -0``, applied, and exited 0 — including under ``--mode
replace``, the destructive reconcile mode, where "apply complete" reads as
"the table now matches the contract".

``SnowflakeIacPlugin.suppressed_drift`` turns the plan's refreshed prior
state into an explicit report. Pure-function tests: no credentials, no
network, no ``tofu``.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.naming import safe_ident

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_CID = "silver.demo"


def _contract(schema):
    return {
        "id": _CID,
        "name": "Demo",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "DB", "schema": "SC", "table": "T"},
                },
                "contract": {"schema": schema},
            }
        ],
    }


def _prior(columns, *, name=None):
    return [
        {
            "type": "snowflake_table",
            "name": name or safe_ident(f"{safe_ident(_CID)}_DB_SC_T"),
            "values": {"name": "T", "column": columns},
        }
    ]


_LIVE = [
    {"name": "ID", "type": "NUMBER(38,0)"},
    {"name": "MESSAGE", "type": "VARCHAR(16777216)"},
    {"name": "CREATED_AT", "type": "TIMESTAMP_NTZ(9)"},
]
_DECLARED = [
    {"name": "ID", "type": "INTEGER", "required": True},
    {"name": "MESSAGE", "type": "STRING"},
    {"name": "CREATED_AT", "type": "TIMESTAMP"},
]


class TestSuppressedDrift:
    def test_matching_shape_reports_nothing(self):
        """Precision widening (``VARCHAR`` → ``VARCHAR(16777216)``) is not
        drift — comparison folds to type families, like ``fluid verify``."""
        assert (
            get_iac_plugin("snowflake").suppressed_drift(_contract(_DECLARED), _prior(_LIVE)) == []
        )

    def test_type_change_the_emitter_ignores_is_reported(self):
        declared = [
            {"name": "ID", "type": "INTEGER", "required": True},
            {"name": "MESSAGE", "type": "STRING"},
            # was TIMESTAMP; the live table still has TIMESTAMP_NTZ(9)
            {"name": "CREATED_AT", "type": "STRING"},
        ]
        drift = get_iac_plugin("snowflake").suppressed_drift(_contract(declared), _prior(_LIVE))
        assert len(drift) == 1
        assert drift[0]["table"] == "DB.SC.T"
        assert drift[0]["type_mismatches"] == [
            {"column": "CREATED_AT", "declared": "STRING", "live": "TIMESTAMP"}
        ]

    def test_declared_but_absent_column_is_reported(self):
        declared = _DECLARED + [{"name": "EXTRA", "type": "STRING"}]
        drift = get_iac_plugin("snowflake").suppressed_drift(_contract(declared), _prior(_LIVE))
        assert drift[0]["missing"] == ["EXTRA"]

    def test_live_only_column_is_reported(self):
        live = _LIVE + [{"name": "SURPRISE", "type": "VARCHAR(16777216)"}]
        drift = get_iac_plugin("snowflake").suppressed_drift(_contract(_DECLARED), _prior(live))
        assert drift[0]["extra"] == ["SURPRISE"]

    def test_a_table_not_yet_in_state_is_not_drift(self):
        """A first apply creates the table — there is nothing to diverge from."""
        assert get_iac_plugin("snowflake").suppressed_drift(_contract(_DECLARED), []) == []

    def test_a_state_resource_for_another_contract_is_ignored(self):
        prior = _prior(_LIVE, name="some_other_contract_DB_SC_T")
        assert get_iac_plugin("snowflake").suppressed_drift(_contract(_DECLARED), prior) == []

    def test_views_are_out_of_scope(self):
        """Views carry no ``column`` block — only tables pin ignore_changes."""
        contract = _contract(_DECLARED)
        contract["exposes"][0]["binding"]["format"] = "snowflake_view"
        assert get_iac_plugin("snowflake").suppressed_drift(contract, _prior(_LIVE)) == []

    def test_non_snowflake_state_entries_are_skipped(self):
        prior = [{"type": "snowflake_schema", "name": "x", "values": {}}]
        assert get_iac_plugin("snowflake").suppressed_drift(_contract(_DECLARED), prior) == []
