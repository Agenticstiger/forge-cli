# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""dlt, airbyte and meltano land in the expose their build's ``outputs`` names.

They took ``exposes[0]`` whatever the build, so with two exposes a build with
``outputs: [Y]`` was gated against Y's schema yet loaded into X's destination.
``landed_expose`` is the one resolver (as the DuckDB runner's); an ``outputs`` entry
that is no exposeId, or no outputs beside several exposes, refuses before anything
runs, with the typed ``BuildOutputsError``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners._acquisition_common import (
    enforce_schema_policy_or_raise,
    landed_expose,
)
from fluid_build.util.contract import BuildOutputsError
from tests._infrastructure.singer_fixtures import fake_singer_tap  # noqa: F401


def _expose(expose_id: str, path: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {"platform": "local", "format": "parquet", "location": {"path": path}},
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }


def _contract(
    engine: str, source: Dict[str, Any], props: Dict[str, Any], x: Path, y: Path
) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"bronze.{engine}_outputs",
        "name": "Outputs",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@co.example"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": engine,
                "capabilities": ["full_refresh"],
                "properties": {"source": source, "sink": {"format": "parquet"}, engine: props},
                "outputs": ["y"],
            }
        ],
        "exposes": [_expose("x", str(x)), _expose("y", str(y))],
    }


def _refuse(contract: Dict[str, Any], mode: str) -> Dict[str, Any]:
    c = copy.deepcopy(contract)
    if mode == "unknown":
        c["builds"][0]["outputs"] = ["nope"]
    else:
        del c["builds"][0]["outputs"]
    return c


_CODES = {"unknown": "build_output_unknown_expose", "undeclared": "build_outputs_undeclared"}


class _Ctx:
    def __init__(self, contract: Dict[str, Any]) -> None:
        self.contract = contract
        self.build_id = "ingest"


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


# ── the shared resolver and the gate ─────────────────────────────────────


def test_landed_expose_is_the_one_outputs_names(tmp_path: Path):
    contract = _contract("dlt", {}, {}, tmp_path / "x", tmp_path / "y")
    assert landed_expose(_Ctx(contract))["exposeId"] == "y"


def test_landed_expose_falls_back_to_the_first_expose():
    contract = {"exposes": [{"exposeId": "a"}, {"exposeId": "b"}], "builds": []}
    assert landed_expose(_Ctx(contract))["exposeId"] == "a"
    assert landed_expose(_Ctx({"builds": []})) == {}


@pytest.mark.parametrize("engine", ["duckdb", "dlt", "airbyte", "meltano"])
@pytest.mark.parametrize("mode", ["unknown", "undeclared"])
def test_the_gate_refuses_for_runners_that_land_one_expose(tmp_path: Path, engine: str, mode: str):
    contract = _refuse(_contract(engine, {}, {}, tmp_path / "x", tmp_path / "y"), mode)
    with pytest.raises(BuildOutputsError) as caught:
        enforce_schema_policy_or_raise(_Ctx(contract), _Named(engine))
    assert caught.value.code == _CODES[mode]


@pytest.mark.parametrize("engine", ["debezium", "kafka_connect"])
def test_the_gate_tolerates_undeclared_outputs_for_sinks_covering_every_expose(
    tmp_path: Path, engine: str
):
    contract = _refuse(_contract(engine, {}, {}, tmp_path / "x", tmp_path / "y"), "undeclared")
    enforce_schema_policy_or_raise(_Ctx(contract), _Named(engine))
    unknown = _refuse(contract, "unknown")
    with pytest.raises(BuildOutputsError):
        enforce_schema_policy_or_raise(_Ctx(unknown), _Named(engine))


# ── dlt ──────────────────────────────────────────────────────────────────


def _dlt_contract(tmp_path: Path) -> Dict[str, Any]:
    src = tmp_path / "in" / "orders.csv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    return _contract(
        "dlt",
        {
            "kind": "filesystem",
            "connection": {"uri": str(src)},
            "mode": "full_refresh",
            "reader": {"format": "csv", "options": {"header": True}},
        },
        {},
        tmp_path / "out" / "x.duckdb",
        tmp_path / "out" / "y.duckdb",
    )


def test_dlt_lands_in_the_expose_outputs_names(tmp_path: Path):
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import execute_dlt_build

    contract = _dlt_contract(tmp_path)
    assert execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 0
    assert (tmp_path / "out" / "y.duckdb").exists()
    assert not (tmp_path / "out" / "x.duckdb").exists()


@pytest.mark.parametrize("mode", ["unknown", "undeclared"])
def test_dlt_refuses_unresolved_outputs_before_anything_runs(tmp_path: Path, mode: str):
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import execute_dlt_build

    contract = _refuse(_dlt_contract(tmp_path), mode)
    with pytest.raises(BuildOutputsError) as caught:
        execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert caught.value.code == _CODES[mode]
    assert not (tmp_path / "out").exists()


# ── meltano ──────────────────────────────────────────────────────────────


def _meltano_contract(tmp_path: Path) -> Dict[str, Any]:
    return _contract(
        "meltano",
        {
            "kind": "fluid-fake",
            "connection": {"n_records": 3},
            "mode": "full_refresh",
            "streams": ["orders"],
        },
        {"tap": "tap-fluid-fake", "dataset_name": "bronze"},
        tmp_path / "out" / "x.duckdb",
        tmp_path / "out" / "y.duckdb",
    )


def test_meltano_lands_in_the_expose_outputs_names(fake_singer_tap, tmp_path: Path):
    from fluid_build.build_runners.meltano.runner import execute_meltano_build

    contract = _meltano_contract(tmp_path)
    assert execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 0
    assert (tmp_path / "out" / "y.duckdb").exists()
    assert not (tmp_path / "out" / "x.duckdb").exists()


@pytest.mark.parametrize("mode", ["unknown", "undeclared"])
def test_meltano_refuses_unresolved_outputs_before_anything_runs(
    fake_singer_tap, tmp_path: Path, mode: str
):
    from fluid_build.build_runners.meltano.runner import execute_meltano_build

    contract = _refuse(_meltano_contract(tmp_path), mode)
    with pytest.raises(BuildOutputsError) as caught:
        execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert caught.value.code == _CODES[mode]
    assert not (tmp_path / "out").exists()


# ── airbyte (REST mode against the respx mock) ───────────────────────────


def _airbyte_contract(tmp_path: Path) -> Dict[str, Any]:
    return _contract(
        "airbyte",
        {
            "kind": "faker",
            "connection": {"count": 10},
            "mode": "full_refresh",
            "streams": ["users"],
        },
        {
            "deployment": {"mode": "bring-your-own", "server_url": "https://airbyte.test"},
            "workspace_id": "ws-1",
        },
        tmp_path / "out" / "x.jsonl",
        tmp_path / "out" / "y.jsonl",
    )


def test_airbyte_destination_is_the_expose_outputs_names(
    airbyte_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fluid_build.build_runners.airbyte import runner as airbyte_runner

    bodies = []
    original = airbyte_runner.AirbyteRestClient.create_destination

    def spy(self, body):
        bodies.append(json.dumps(body))
        return original(self, body)

    monkeypatch.setattr(airbyte_runner.AirbyteRestClient, "create_destination", spy)
    contract = _airbyte_contract(tmp_path)
    rc = airbyte_runner.execute_airbyte_build(
        contract["builds"][0], contract, tmp_path, dry_run=False
    )
    assert rc == 0
    assert len(bodies) == 1
    assert "y.jsonl" in bodies[0] and "x.jsonl" not in bodies[0]


@pytest.mark.parametrize("mode", ["unknown", "undeclared"])
def test_airbyte_refuses_unresolved_outputs_before_anything_runs(
    airbyte_mock, tmp_path: Path, mode: str
):
    from fluid_build.build_runners.airbyte.runner import execute_airbyte_build

    contract = _refuse(_airbyte_contract(tmp_path), mode)
    with pytest.raises(BuildOutputsError) as caught:
        execute_airbyte_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert caught.value.code == _CODES[mode]
    assert airbyte_mock.calls == []
