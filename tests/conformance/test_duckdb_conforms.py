# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Conformance: DuckDB runner satisfies the public Runner Protocol contract.

This is the minimum viable exercise of fluid_build.api.conformance.RunnerConformance —
third-party runner authors copy this file pattern to validate their own runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fluid_build.build_runners._acquisition_common import generate_run_id
from fluid_build.build_runners._cost import InMemoryCostTracker
from fluid_build.build_runners._lineage import NullLineageEmitter
from fluid_build.build_runners._state import FileStateStore
from fluid_build.build_runners.duckdb.runner import DuckdbRunner

from fluid_build.api.conformance.fixtures.minimal import minimal_acquisition_contract
from fluid_build.api.conformance.runner import RunnerConformance
from fluid_build.api.cost import ChargebackTag
from fluid_build.api.hooks import HookChain
from fluid_build.api.runner import RunContext
from fluid_build.api.source import SinkSpec, SourceSpec


@pytest.fixture
def conformance_ctx(tmp_path: Path) -> RunContext:
    """Build a RunContext that exercises a filesystem CSV ingestion path."""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "minimal.csv").write_text("id,name\n1,Alice\n2,Bob\n", encoding="utf-8")
    contract = minimal_acquisition_contract(str(tmp_path))
    build = contract["builds"][0]
    source = SourceSpec.from_dict(build["properties"]["source"])
    sink = SinkSpec.from_dict(build["properties"].get("sink"))
    state_store = FileStateStore(tmp_path / ".fluid")
    return RunContext(
        run_id=generate_run_id(),
        product_id=contract["id"],
        build_id=build["id"],
        contract=contract,
        source=source,
        sink=sink,
        state_store=state_store,
        hook_chain=HookChain(hooks=[]),
        lineage=NullLineageEmitter(),
        cost_tracker=InMemoryCostTracker(chargeback=ChargebackTag(team="conformance")),
        workdir=str(tmp_path),
    )


class TestDuckdbConformance(RunnerConformance):
    """DuckDB runner must satisfy the public Runner Protocol contract."""

    runner = DuckdbRunner()
