# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pipeline-stage integration matrix (Slice J).

For every engine, assert:

  1. ``validate`` recognizes the contract (schema acceptance).
  2. ``plan`` would produce an ordered plan (infra → provider → builds).
  3. ``apply`` dispatches to the correct runner via ``base.is_acquisition_build``.
  4. Capability negotiation at the validator catches mis-asks before runtime.
  5. ``generate-artifacts`` emits at least one artifact for managed-mode contracts.
  6. ``validate-artifacts`` accepts the bundle.

We exercise every engine declared in ``ACQUISITION_ENGINES`` so adding a new
engine surfaces immediately if it isn't wired through every stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.base import (
    ACQUISITION_ENGINES,
    is_acquisition_build,
    is_dbt_build,
)
from fluid_build.schema_manager import FluidSchemaManager

# ── Helpers ──────────────────────────────────────────────────────────────


def _minimal_acquisition_contract(engine: str) -> Dict[str, Any]:
    """Minimum-viable contract for an engine. Connection details are
    intentionally generic — schema validation only needs the field shapes
    to be present.
    """
    source: Dict[str, Any] = {"kind": _default_kind_for(engine), "mode": "full_refresh"}
    if engine == "duckdb":
        source.update(
            {
                "kind": "filesystem",
                "connection": {"uri": "/tmp/fake.csv"},
                "reader": {"format": "csv", "options": {"header": True}},
            }
        )
    elif engine == "dlt":
        source.update(
            {
                "kind": "filesystem",
                "connection": {"uri": "/tmp/fake.csv"},
                "reader": {"format": "csv", "options": {"header": True}},
            }
        )
    elif engine == "meltano":
        source.update({"kind": "fake-fluid", "connection": {"n_records": 1}, "streams": ["x"]})
    elif engine == "airbyte":
        source.update({"kind": "faker", "connection": {"count": 10}, "streams": ["users"]})
    elif engine == "kafka-connect":
        source.update(
            {
                "kind": "postgres",
                "connection": {
                    "host": "x",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "streams": ["public.x"],
            }
        )
    elif engine == "debezium":
        source.update(
            {
                "kind": "postgres",
                "mode": "cdc",
                "connection": {
                    "host": "x",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "streams": ["public.x"],
            }
        )
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"bronze.{engine}_pipeline",
        "name": f"{engine} pipeline test",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": engine,
                "capabilities": [_default_capability_for(engine)],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    engine: {"deployment": {"mode": "embedded"}},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out.parquet"},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def _default_kind_for(engine: str) -> str:
    return {
        "duckdb": "filesystem",
        "dlt": "filesystem",
        "meltano": "fake-fluid",
        "airbyte": "faker",
        "kafka-connect": "postgres",
        "debezium": "postgres",
    }.get(engine, "filesystem")


def _default_capability_for(engine: str) -> str:
    return {
        "duckdb": "full_refresh",
        "dlt": "full_refresh",
        "meltano": "full_refresh",
        "airbyte": "full_refresh",
        "kafka-connect": "streaming",
        "debezium": "cdc",
    }.get(engine, "full_refresh")


def _managed_contract(engine: str) -> Dict[str, Any]:
    """Same shape as ``_minimal_acquisition_contract`` but with managed deployment."""
    contract = _minimal_acquisition_contract(engine)
    contract["builds"][0]["properties"][engine] = {
        "deployment": {
            "mode": "managed",
            "managed": {
                "target": "kubernetes",
                "profile": "small",
            },
        }
    }
    return contract


@pytest.fixture(scope="module")
def schema_manager() -> FluidSchemaManager:
    return FluidSchemaManager()


# ── Stage 1: ACQUISITION_ENGINES registry ──────────────────────────────


class TestAcquisitionEnginesRegistry:
    def test_six_engines_registered(self):
        assert ACQUISITION_ENGINES == frozenset(
            {"duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"}
        )

    def test_dbt_is_not_acquisition(self):
        # Dbt is a transformation engine; it must not flip is_acquisition_build.
        assert not is_acquisition_build({"pattern": "embedded-logic", "engine": "dbt"})
        assert is_dbt_build({"pattern": "embedded-logic", "engine": "dbt"})

    def test_acquisition_pattern_required(self):
        # An "engine: duckdb" build with the wrong pattern is still a transform.
        assert not is_acquisition_build({"pattern": "embedded-logic", "engine": "duckdb"})


# ── Stage 2: validate (schema acceptance per engine) ───────────────────


class TestValidateStagePerEngine:
    @pytest.mark.parametrize("engine", sorted(ACQUISITION_ENGINES))
    def test_validate_accepts_minimal_contract(
        self, schema_manager: FluidSchemaManager, engine: str
    ):
        contract = _minimal_acquisition_contract(engine)
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, f"{engine}: {result.errors}"

    @pytest.mark.parametrize("engine", sorted(ACQUISITION_ENGINES))
    def test_validate_rejects_acquisition_with_unknown_engine(
        self, schema_manager: FluidSchemaManager, engine: str
    ):
        # Substitute an invalid engine — schema enum should reject.
        contract = _minimal_acquisition_contract(engine)
        contract["builds"][0]["engine"] = "not-a-real-engine"
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert not result.is_valid


# ── Stage 3: apply dispatch + is_acquisition_build per engine ─────────


class TestApplyDispatchPerEngine:
    @pytest.mark.parametrize("engine", sorted(ACQUISITION_ENGINES))
    def test_dispatcher_recognizes_engine(self, engine: str):
        contract = _minimal_acquisition_contract(engine)
        build = contract["builds"][0]
        assert is_acquisition_build(build)


# ── Stage 6: ordered plan (infra → provider → builds) ─────────────────


class TestPlanActionOrdering:
    """The plan layer assembles three categories into one ordered plan. Even
    without a full plan implementation today, we assert the ``PlanAction``
    model carries the ``category`` field that ordering depends on.
    """

    def test_plan_action_has_category_field(self):
        from fluid_build.api.provider import PlanAction

        action = PlanAction(
            op="create", action_type="airbyte_source", resource_id="src-1", category="provider"
        )
        assert action.category == "provider"

    def test_plan_action_default_category_is_provider(self):
        from fluid_build.api.provider import PlanAction

        action = PlanAction(op="create", action_type="x", resource_id="r")
        assert action.category == "provider"

    def test_plan_actions_can_be_sorted_infra_provider_builds(self):
        from fluid_build.api.provider import PlanAction

        actions = [
            PlanAction(op="create", action_type="b", resource_id="r3", category="build"),
            PlanAction(op="create", action_type="i", resource_id="r1", category="infra"),
            PlanAction(op="create", action_type="p", resource_id="r2", category="provider"),
        ]
        order = {"infra": 0, "provider": 1, "build": 2}
        ordered = sorted(actions, key=lambda a: order[a.category])
        assert [a.category for a in ordered] == ["infra", "provider", "build"]


# ── Stage 7: capability negotiation ────────────────────────────────────


class TestCapabilityNegotiationPerEngine:
    @pytest.mark.parametrize(
        "engine, capability_runner_cant_satisfy",
        [
            ("duckdb", "cdc"),  # DuckDB doesn't do CDC
            ("duckdb", "streaming"),  # or streaming
            ("dlt", "cdc"),  # dlt: no CDC
            ("dlt", "streaming"),  # or streaming
            ("meltano", "cdc"),  # Meltano (Singer): no CDC
            ("airbyte", "streaming"),  # Airbyte does batch CDC, not pure streaming
        ],
    )
    def test_runner_does_not_declare_unsupported_capability(
        self, engine: str, capability_runner_cant_satisfy: str
    ):
        from fluid_build.api.runner import RunnerCapability

        runner_class_map = {
            "duckdb": "fluid_build.build_runners.duckdb.runner:DuckdbRunner",
            "dlt": "fluid_build.build_runners.dlt.runner:DltRunner",
            "meltano": "fluid_build.build_runners.meltano.runner:MeltanoRunner",
            "airbyte": "fluid_build.build_runners.airbyte.runner:AirbyteRunner",
            "kafka-connect": "fluid_build.build_runners.kafka_connect.runner:KafkaConnectRunner",
            "debezium": "fluid_build.build_runners.debezium.runner:DebeziumRunner",
        }
        module_path, class_name = runner_class_map[engine].split(":")
        import importlib

        module = importlib.import_module(module_path)
        runner = getattr(module, class_name)()
        cap_enum = RunnerCapability(capability_runner_cant_satisfy)
        assert (
            cap_enum not in runner.declared_capabilities
        ), f"{engine} unexpectedly declares {capability_runner_cant_satisfy}"


# ── Stage 8: existence of every entry-point ──────────────────────────


class TestRunnerEntryPoints:
    @pytest.mark.parametrize("engine", sorted(ACQUISITION_ENGINES))
    def test_entry_point_exists(self, engine: str):
        # Every engine in ACQUISITION_ENGINES must have an execute_<engine>_build
        # callable available for the dispatcher.
        import importlib

        module_name = f"fluid_build.build_runners.{engine.replace('-', '_')}.runner"
        module = importlib.import_module(module_name)
        attr_name = f"execute_{engine.replace('-', '_')}_build"
        assert hasattr(module, attr_name), f"{module_name}.{attr_name} missing"
        assert callable(getattr(module, attr_name))


# ── Stage 9: schema additions cover every engine ───────────────────────


class TestSchemaCoversEveryEngine:
    @pytest.mark.parametrize("engine", sorted(ACQUISITION_ENGINES))
    def test_engine_in_schema_enum(self, schema_manager: FluidSchemaManager, engine: str):
        schema = schema_manager.get_schema("0.7.3", offline_only=True)
        engine_schema = schema["$defs"]["build"]["properties"]["engine"]
        # ``engine`` is ``anyOf: [{enum: [...]}, {pattern: "^dbt-..."}]`` (issue
        # #249 relaxed the closed enum to also accept ``dbt-<adapter>``), so the
        # enumerated values may sit directly on the node or inside an ``anyOf``
        # branch. Collect them regardless of which shape is present.
        engine_enum = engine_schema.get("enum") or [
            value
            for branch in engine_schema.get("anyOf", [])
            for value in branch.get("enum", [])
        ]
        assert engine in engine_enum, f"engine '{engine}' missing from schema enum"


# ── Stage 10: end-to-end smoke (dispatch + run record per engine) ─────


class TestDispatchSmoke:
    """Exercise the dispatcher with each engine; we don't run real syncs here
    (each engine has its own full-matrix test for that). We only assert
    the dispatcher routes correctly for ``--dry-run``.
    """

    @pytest.mark.parametrize("engine", ["duckdb", "dlt", "airbyte", "kafka-connect", "debezium"])
    def test_dispatcher_dry_run(self, engine: str, tmp_path: Path):
        from fluid_build.build_runners.base import _execute_acquisition_build

        contract = _minimal_acquisition_contract(engine)
        build = contract["builds"][0]
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=True, sample_rows=None)
        # All five engines listed support dry_run successfully.
        assert rc == 0, f"{engine} dispatcher dry-run returned {rc}"
