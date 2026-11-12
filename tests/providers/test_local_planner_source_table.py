# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The local planner lands the last build that produced a table.

It took the output of the contract's last build. A last build the local
planner does not run (a dbt model, for one) produced no table, so it fell back
to the first input it had loaded and landed that, raw, in the expose: columns
the expose does not declare, PII among them, included.

An expose an acquisition build lands needs no table: the build's runner lands
it, and the plan says so instead of failing validation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

import pytest

from fluid_build.build_runners import base as runners
from fluid_build.providers.local import planner
from fluid_build.providers.local.planner import plan_actions, validate_plan


def test_a_trailing_build_without_a_table_does_not_land_the_raw_input() -> None:
    contract = {
        "fluidVersion": "0.7.5",
        "id": "demo.customers",
        "builds": [
            {
                "id": "clean",
                "engine": "sql",
                "pattern": "embedded-logic",
                "properties": {
                    "sql": "SELECT id FROM customers",
                    "parameters": {"inputs": [{"name": "customers", "path": "data/customers.csv"}]},
                },
            },
            {"id": "docs", "engine": "dbt", "pattern": "hybrid-reference"},
        ],
        "exposes": [
            {
                "exposeId": "customers_clean",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "out/clean.csv"},
                },
            }
        ],
    }
    actions = plan_actions(contract, logger=logging.getLogger("t"))
    (materialize,) = [a for a in actions if a.get("op") == "materialize"]
    assert materialize["payload"]["source_table"] == "result_clean"


# ── An expose an acquisition build lands ─────────────────────────────────
#
# The local planner leaves an acquisition build to its runner under
# ``build_runners/<engine>/``, so the expose that build writes has no table in
# the plan. ``validate_plan`` reported "materialize missing source_table" for
# it, and ``fluid diff`` printed that failure before the expose's ``match``.

_INGEST: Dict[str, Any] = {
    "id": "ingest",
    "pattern": "acquisition",
    "engine": "duckdb",
    "properties": {
        "source": {
            "kind": "filesystem",
            "mode": "full_refresh",
            "connection": {"uri": "src/*.csv"},
            "reader": {"format": "csv"},
            "streams": ["rows"],
        },
        "sink": {"format": "parquet"},
    },
    "outputs": ["rows"],
}

# The same build declaring no ``outputs``: it may write every expose.
_INGEST_ANY = {key: value for key, value in _INGEST.items() if key != "outputs"}

_DBT_MODEL: Dict[str, Any] = {"id": "model", "engine": "dbt", "pattern": "hybrid-reference"}


def _landing_contract(builds: Sequence[Dict[str, Any]], exposes: Sequence[str]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "id": "bronze.rows",
        "builds": list(builds),
        "exposes": [
            {
                "exposeId": expose_id,
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": f"out/{expose_id}.parquet"},
                },
            }
            for expose_id in exposes
        ],
    }


def test_an_expose_its_acquisition_build_lands_needs_no_local_table(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = logging.getLogger("t")
    with caplog.at_level(logging.DEBUG, logger="t"):
        actions = plan_actions(_landing_contract([_INGEST], ["rows"]), logger=log)
        verdict = validate_plan(actions, log)

    (materialize,) = [a for a in actions if a.get("op") == "materialize"]
    assert materialize["payload"]["source_table"] is None
    assert materialize["payload"]["landed_by"] == ["ingest"]
    assert verdict == (True, [])
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "expose 'rows' is landed by build(s) 'ingest'" in caplog.text


@pytest.mark.parametrize(
    ("builds", "exposes", "unlanded"),
    [
        pytest.param([_INGEST], ["rows", "extra"], "extra", id="no-build-writes-it"),
        pytest.param([_INGEST_ANY, _DBT_MODEL], ["rows"], "rows", id="a-dbt-model-may-write-it"),
    ],
)
def test_an_expose_no_runner_alone_lands_still_reports_its_missing_source(
    builds: List[Dict[str, Any]],
    exposes: List[str],
    unlanded: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = logging.getLogger("t")
    with caplog.at_level(logging.WARNING, logger="t"):
        actions = plan_actions(_landing_contract(builds, exposes), logger=log)
        verdict = validate_plan(actions, log)

    (materialize,) = [
        a for a in actions if a.get("op") == "materialize" and a["resource_id"] == unlanded
    ]
    assert "landed_by" not in materialize["payload"]
    assert verdict == (False, [f"{unlanded}: materialize missing source_table"])
    assert f"Could not determine source table for expose {unlanded}" in caplog.text


def test_the_planner_knows_every_acquisition_runner() -> None:
    """Which exposes a runner lands follows the planner's own engine list,
    kept apart from the runners' registry to keep the dbt runner out of it."""
    assert planner._ACQUISITION_ENGINES == runners.ACQUISITION_ENGINES
