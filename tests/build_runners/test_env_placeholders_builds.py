# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A build never runs against a name the contract wrote as ``{{ env.NAME }}`` and nobody set.

The build runners rendered an unset variable as the empty string, so a path lost its first
half, a connection lost its user, and the build still ran. The dispatchers now refuse, naming
every variable, before the runner or the SQL starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from fluid_build.build_runners.base import (
    _execute_acquisition_build,
    _execute_embedded_sql_build,
    _resolve_env_placeholders,
)
from fluid_build.util.env_templates import EnvTemplateError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_variables(monkeypatch):
    for name in ("OUT_DIR", "PGUSER", "PGHOST", "RUN_DATE", "DBT_TARGET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", raising=False)


@pytest.fixture
def printed() -> Iterator[List[str]]:
    lines: List[str] = []
    with patch(
        "fluid_build.build_runners.base.cprint",
        side_effect=lambda *a, **_k: lines.append(" ".join(str(x) for x in a)),
    ):
        yield lines


def _contract(build: Dict[str, Any], path: str = "out/x.parquet") -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "silver.demo",
        "name": "Demo",
        "builds": [build],
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": path},
                },
            }
        ],
    }


def _sql_build() -> Dict[str, Any]:
    return {
        "id": "t",
        "pattern": "embedded-logic",
        "engine": "duckdb",
        "properties": {"sql": "SELECT 1 AS id"},
    }


def _acquisition_build(**connection: str) -> Dict[str, Any]:
    return {
        "id": "ingest",
        "pattern": "acquisition",
        "engine": "duckdb",
        "properties": {
            "source": {
                "kind": "postgres",
                "connection": {"database": "x", **connection},
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            "sink": {"format": "parquet"},
        },
    }


def test_an_embedded_sql_build_refuses_an_unset_variable_before_it_runs(tmp_path: Path, printed):
    contract = _contract(_sql_build(), path="{{ env.OUT_DIR }}/x.parquet")
    rc = _execute_embedded_sql_build(contract["builds"][0], contract, tmp_path, dry_run=False)

    assert rc == 1
    shown = "\n".join(printed)
    assert "OUT_DIR" in shown and "exposes[0].binding.location.path" in shown
    assert not (tmp_path / "x.parquet").exists()


def test_an_embedded_sql_build_runs_when_the_variable_is_set(tmp_path: Path, monkeypatch, printed):
    pytest.importorskip("duckdb")
    monkeypatch.setenv("OUT_DIR", str(tmp_path / "data"))
    contract = _contract(_sql_build(), path="{{ env.OUT_DIR }}/x.parquet")
    assert _execute_embedded_sql_build(contract["builds"][0], contract, tmp_path, dry_run=True) == 0


def test_an_acquisition_build_refuses_unset_variables_and_names_every_one(tmp_path: Path, printed):
    build = _acquisition_build(user="{{ env.PGUSER }}", host="db.internal")
    contract = _contract(build)
    contract["builds"][0]["properties"]["sink"]["path"] = "{{ env.OUT_DIR }}/raw"

    rc = _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=True)

    assert rc == 1
    shown = "\n".join(printed)
    assert "OUT_DIR" in shown and "PGUSER" in shown, "one error lists every missing name"


def test_the_build_resolver_raises_a_typed_error_not_an_empty_string():
    with pytest.raises(EnvTemplateError) as info:
        _resolve_env_placeholders({"vars": {"run_date": "{{ env.RUN_DATE }}"}})
    assert info.value.extras["missing"] == ["RUN_DATE"]


def test_the_build_resolver_still_renders_a_credential_shaped_name(monkeypatch):
    """A connection's ``password: "{{ env.PGPASSWORD }}"`` reaches the engine that connects."""
    monkeypatch.setenv("PGPASSWORD", "pw")
    rendered = _resolve_env_placeholders({"password": "{{ env.PGPASSWORD }}"})
    assert rendered == {"password": "pw"}  # pragma: allowlist secret


def test_a_dbt_target_variable_that_is_unset_is_refused(tmp_path: Path):
    from fluid_build.build_runners.dbt.runner import _resolve_env_placeholders as dbt_resolve

    with pytest.raises(EnvTemplateError):
        dbt_resolve({"target": "{{ env.DBT_TARGET }}"})
