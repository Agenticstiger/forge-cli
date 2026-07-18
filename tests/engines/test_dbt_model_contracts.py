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

"""Tests for dbt model-contract emission (``--model-contracts``).

Covers the opt-in dbt model contract path (dbt-core >= 1.5):

1. ``config: {contract: {enforced: true}}`` + per-column ``data_type``
   + ``constraints`` on every expose model, derived from
   ``exposes[].contract.schema[]``.
2. The adapter-aware FLUID → SQL type mapper (``engines/dbt/_types.py``)
   keyed off ``builds[].execution.runtime.platform`` — BigQuery rejects
   ``varchar``, so generic types alone fail there.
3. Opt-in semantics: without the flag the emitted schema.yml is
   unchanged (no default behavior change).
4. The live enforcement proof: a real ``dbt build`` (duckdb) fails when
   the model SQL diverges from the contract schema (integration test,
   self-skipped when dbt is unavailable).

The constraint/enforcement rules adapt datacontract-cli's
``dbt_exporter.py`` (MIT): contracts only on constraint-supporting
materializations (table/incremental); ``not_null`` splits between
``constraints:`` (when supported) and the data-test fallback.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import yaml

from fluid_build.engines.dbt import DbtEngine, _types
from fluid_build.engines.dbt.models import _sql_type
from fluid_build.engines.dbt.schema_yml import generate_schema_yml

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _contract(
    *,
    schema: Optional[List[Dict[str, Any]]] = None,
    dq_rules: Optional[List[Dict[str, Any]]] = None,
    platform: str = "local",
) -> Dict[str, Any]:
    """Minimal contract with one dbt build + one expose."""
    expose_contract: Dict[str, Any] = {
        "schema": (
            schema
            if schema is not None
            else [
                {"name": "customer_id", "type": "STRING", "required": True},
                {"name": "total_orders", "type": "INTEGER"},
                {"name": "total_amount", "type": "NUMBER"},
            ]
        ),
    }
    if dq_rules is not None:
        expose_contract["dq"] = {"rules": dq_rules}
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "gold.analytics.contracted_v1",
        "name": "Contracted Product",
        "builds": [
            {
                "id": "main_transform",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "execution": {"runtime": {"platform": platform}},
            }
        ],
        "exposes": [
            {
                "exposeId": "customer_orders",
                "kind": "table",
                "contract": expose_contract,
            }
        ],
    }


def _parsed_model(out: Dict[str, str]) -> Dict[str, Any]:
    content = out.get("models/marts/schema.yml", "")
    assert content, "expected models/marts/schema.yml in output"
    return yaml.safe_load(content)["models"][0]


def _column(model: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(c for c in model["columns"] if c["name"] == name)


# -----------------------------------------------------------------------------
# Adapter-aware type mapping (_types.py)
# -----------------------------------------------------------------------------


class TestAdapterTypeMapping:
    # (fluid_type, bigquery, snowflake, redshift, duckdb)
    MATRIX = [
        ("string", "string", "varchar", "varchar", "varchar"),
        ("integer", "int64", "number", "integer", "integer"),
        ("number", "numeric", "number", "numeric", "numeric"),
        ("float", "float64", "float", "double precision", "numeric"),
        ("boolean", "bool", "boolean", "boolean", "boolean"),
        ("date", "date", "date", "date", "date"),
        ("timestamp", "timestamp", "timestamp_ntz", "timestamp", "timestamp"),
        ("datetime", "datetime", "timestamp_ntz", "timestamp", "timestamp"),
        ("array", "json", "array", "super", "varchar"),
        ("object", "json", "object", "super", "varchar"),
    ]

    @pytest.mark.parametrize("fluid_type,bq,sf,rs,duck", MATRIX)
    def test_adapter_matrix(self, fluid_type, bq, sf, rs, duck):
        assert _types.sql_type(fluid_type, "bigquery") == bq
        assert _types.sql_type(fluid_type, "snowflake") == sf
        assert _types.sql_type(fluid_type, "redshift") == rs
        assert _types.sql_type(fluid_type, "duckdb") == duck

    def test_bigquery_never_emits_varchar(self):
        """BigQuery rejects ``varchar`` — the whole reason the mapper is
        adapter-aware. No FLUID type may map to it, unknowns included."""
        for fluid_type, *_ in self.MATRIX:
            assert _types.sql_type(fluid_type, "bigquery") != "varchar"
        assert _types.sql_type("mystery_type", "bigquery") == "string"

    def test_adapter_lookup_is_case_insensitive(self):
        assert _types.sql_type("STRING", "bigquery") == "string"
        assert _types.sql_type("String", "snowflake") == "varchar"

    def test_generic_mapping_preserved_for_skeleton_casts(self):
        """``adapter=None`` reproduces the historical ``models._sql_type``
        table byte-for-byte — including its case-sensitive keys and the
        ``varchar`` fallback for unknown/mixed-case types."""
        assert _types.sql_type("string") == "varchar"
        assert _types.sql_type("STRING") == "varchar"
        assert _types.sql_type("float") == "numeric"
        assert _types.sql_type("Datetime") == "varchar"  # mixed case → fallback
        assert _types.sql_type("mystery_type") == "varchar"

    def test_unknown_adapter_falls_back_to_generic(self):
        assert _types.sql_type("string", "postgres") == "varchar"
        assert _types.sql_type("float", "postgres") == "numeric"

    def test_models_sql_type_delegates_and_patches_flow_through(self):
        """``models._sql_type`` is a thin seam over ``_types.sql_type``;
        a patch on the extracted module flows through (the repo's
        module-attribute-indirection extraction pattern)."""
        assert _sql_type("string") == "varchar"
        with patch("fluid_build.engines.dbt._types.sql_type", return_value="patched"):
            assert _sql_type("string") == "patched"

    def test_adapter_for_build_platform_dispatch(self):
        """Mirrors profiles.py::_profile_for_platform platform dispatch."""
        cases = {
            "gcp": "bigquery",
            "bigquery": "bigquery",
            "Snowflake": "snowflake",
            "aws": "redshift",
            "redshift": "redshift",
            "local": "duckdb",
            "anything-else": "duckdb",
        }
        for platform, adapter in cases.items():
            build = {"execution": {"runtime": {"platform": platform}}}
            assert _types.adapter_for_build(build) == adapter, platform
        assert _types.adapter_for_build({}) == "duckdb"


# -----------------------------------------------------------------------------
# Opt-in default: no flag → byte-identical schema.yml
# -----------------------------------------------------------------------------


class TestOptInDefault:
    def test_no_flag_emits_no_contract_keys(self):
        out = generate_schema_yml(_contract())
        content = out["models/marts/schema.yml"]
        assert "contract" not in content
        assert "data_type" not in content
        assert "constraints" not in content
        assert "config" not in content

    def test_no_flag_is_byte_identical_to_explicit_false(self):
        contract = _contract(
            dq_rules=[{"id": "c", "type": "completeness", "selector": "customer_id"}]
        )
        implicit = generate_schema_yml(contract)
        explicit = generate_schema_yml(contract, model_contracts=False, adapter="snowflake")
        assert implicit == explicit

    def test_engine_default_emits_no_contract_keys(self):
        contract = _contract()
        files = DbtEngine().generate(contract, contract["builds"][0])
        assert "contract:" not in files["models/marts/schema.yml"]
        assert "data_type" not in files["models/marts/schema.yml"]


# -----------------------------------------------------------------------------
# Contract emission (flag on)
# -----------------------------------------------------------------------------


class TestContractEmission:
    def test_contract_enforced_on_expose_model(self):
        out = generate_schema_yml(_contract(), model_contracts=True, adapter="duckdb")
        model = _parsed_model(out)
        assert model["config"] == {"contract": {"enforced": True}}
        # Mesh annotations coexist untouched.
        assert model["access"] == "public"

    def test_every_schema_column_gets_adapter_correct_data_type(self):
        out = generate_schema_yml(_contract(), model_contracts=True, adapter="bigquery")
        model = _parsed_model(out)
        assert _column(model, "customer_id")["data_type"] == "string"
        assert _column(model, "total_orders")["data_type"] == "int64"
        assert _column(model, "total_amount")["data_type"] == "numeric"

    def test_required_column_gets_not_null_constraint(self):
        out = generate_schema_yml(_contract(), model_contracts=True, adapter="duckdb")
        col = _column(_parsed_model(out), "customer_id")
        assert col["constraints"] == [{"type": "not_null"}]

    def test_optional_column_has_no_constraints(self):
        out = generate_schema_yml(_contract(), model_contracts=True, adapter="duckdb")
        col = _column(_parsed_model(out), "total_orders")
        assert "constraints" not in col

    def test_key_column_gets_primary_key_and_not_null(self):
        schema = [
            {"name": "id", "type": "STRING", "primaryKey": True},
            {"name": "value", "type": "NUMBER"},
        ]
        out = generate_schema_yml(_contract(schema=schema), model_contracts=True, adapter="duckdb")
        col = _column(_parsed_model(out), "id")
        assert col["constraints"] == [{"type": "not_null"}, {"type": "primary_key"}]
        # The unique *test* stays: unique constraints are metadata-only on
        # most warehouses, so the post-build test still adds real checking.
        assert "unique" in col.get("tests", [])

    def test_not_null_splits_from_tests_into_constraints(self):
        """Borrowed datacontract-cli rule: when constraints are supported,
        not_null intent lives in ``constraints:`` and the redundant
        post-build ``not_null`` data test is dropped — including one
        derived from a dq completeness rule on the same column."""
        contract = _contract(
            dq_rules=[{"id": "c", "type": "completeness", "selector": "customer_id"}]
        )
        # Without the flag the not_null test is present (pre-existing path).
        col_off = _column(_parsed_model(generate_schema_yml(contract)), "customer_id")
        assert "not_null" in col_off["tests"]
        # With the flag it moves into constraints.
        out = generate_schema_yml(contract, model_contracts=True, adapter="duckdb")
        col_on = _column(_parsed_model(out), "customer_id")
        assert col_on["constraints"] == [{"type": "not_null"}]
        assert "not_null" not in col_on.get("tests", [])

    def test_non_not_null_tests_survive(self):
        contract = _contract(
            dq_rules=[
                {
                    "id": "v",
                    "type": "valid_values",
                    "selector": "customer_id",
                    "values": ["a", "b"],
                }
            ]
        )
        out = generate_schema_yml(contract, model_contracts=True, adapter="duckdb")
        col = _column(_parsed_model(out), "customer_id")
        assert any(isinstance(t, dict) and "accepted_values" in t for t in col["tests"])


# -----------------------------------------------------------------------------
# Skip rules — cases where enforcement must NOT be emitted
# -----------------------------------------------------------------------------


class TestContractSkipRules:
    def test_orphan_dq_column_skips_contract_for_model(self):
        """dbt hard-errors on a contracted model column without data_type
        ("Contracted models require data_type to be defined for each
        column", verified live on dbt-core 1.11). A dq rule can reference
        a column absent from schema[] whose type is unknowable — such a
        model skips enforcement entirely rather than emitting YAML that
        fails on our output instead of on user drift."""
        contract = _contract(
            dq_rules=[{"id": "x", "type": "completeness", "selector": "not_in_schema"}]
        )
        out = generate_schema_yml(contract, model_contracts=True, adapter="duckdb")
        model = _parsed_model(out)
        assert "config" not in model
        assert all("data_type" not in c for c in model["columns"])
        assert all("constraints" not in c for c in model["columns"])
        # The orphan's declared check is still emitted as a test.
        orphan = _column(model, "not_in_schema")
        assert "not_null" in orphan["tests"]

    def test_empty_schema_skips_contract(self):
        contract = _contract(
            schema=[],
            dq_rules=[{"id": "c", "type": "completeness", "selector": "some_col"}],
        )
        out = generate_schema_yml(contract, model_contracts=True, adapter="duckdb")
        model = _parsed_model(out)
        assert "config" not in model

    def test_unsupported_materialization_skips_contract(self):
        """Contracts are only enforced on constraint-supporting
        materializations (table/incremental — datacontract-cli's
        ``_supports_constraints``). If the marts layer ever became a
        view, enforcement must be skipped, and the module-indirection
        seam on ``models._layer_materialization`` carries the patch."""
        with patch("fluid_build.engines.dbt.models._layer_materialization", return_value="view"):
            out = generate_schema_yml(_contract(), model_contracts=True, adapter="duckdb")
        model = _parsed_model(out)
        assert "config" not in model
        assert all("constraints" not in c for c in model["columns"])
        # Fallback path: not_null intent stays as a data test.
        assert "not_null" in _column(model, "customer_id")["tests"]


# -----------------------------------------------------------------------------
# Engine plumbing — DbtEngine.generate resolves the adapter from the build
# -----------------------------------------------------------------------------


class TestEnginePlumbing:
    def test_generate_passes_adapter_from_build_platform(self):
        contract = _contract(platform="snowflake")
        files = DbtEngine().generate(contract, contract["builds"][0], model_contracts=True)
        model = yaml.safe_load(files["models/marts/schema.yml"])["models"][0]
        assert model["config"] == {"contract": {"enforced": True}}
        assert _column(model, "total_orders")["data_type"] == "number"  # snowflake

    def test_generate_bigquery_platform_yields_bigquery_types(self):
        contract = _contract(platform="gcp")
        files = DbtEngine().generate(contract, contract["builds"][0], model_contracts=True)
        model = yaml.safe_load(files["models/marts/schema.yml"])["models"][0]
        assert _column(model, "customer_id")["data_type"] == "string"
        assert _column(model, "total_orders")["data_type"] == "int64"


# -----------------------------------------------------------------------------
# CLI wiring — --model-contracts flag
# -----------------------------------------------------------------------------


class TestGenerateSpeedTransformationArgparse:
    def _parse(self, argv):
        from fluid_build.cli import generate_speed_transformation

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="generate_sub")
        generate_speed_transformation.register_subcommand(sp)
        return parser.parse_args(argv)

    def test_model_contracts_flag_registered(self):
        ns = self._parse(["speed-transformation", "--model-contracts"])
        assert ns.model_contracts is True

    def test_model_contracts_default_false(self):
        """Opt-in: enforcement fails builds for already-drifted user SQL,
        so the right first ship is default-off."""
        ns = self._parse(["speed-transformation"])
        assert ns.model_contracts is False


# -----------------------------------------------------------------------------
# Live enforcement proof — real dbt build on duckdb
# -----------------------------------------------------------------------------


def _find_dbt() -> Optional[str]:
    """Locate the dbt CLI: prefer the running venv's bin, then PATH."""
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.exists():
        return str(candidate)
    return shutil.which("dbt")


_CONFORMANT_SQL = """{{ config(materialized='table') }}
select
    cast('c1' as varchar) as customer_id,
    cast(3 as integer) as total_orders,
    cast(12.5 as numeric) as total_amount
"""

_DRIFTED_SQL_DROPPED_COLUMN = """{{ config(materialized='table') }}
select
    cast('c1' as varchar) as customer_id,
    cast(3 as integer) as total_orders
"""

_DRIFTED_SQL_RETYPED_COLUMN = """{{ config(materialized='table') }}
select
    cast('c1' as varchar) as customer_id,
    cast('three' as varchar) as total_orders,
    cast(12.5 as numeric) as total_amount
"""


@pytest.mark.integration
@pytest.mark.slow
class TestLiveDbtBuildEnforcement:
    """ACCEPTANCE: with --model-contracts, a REAL ``dbt build`` fails when
    the model SQL diverges from exposes[].contract.schema[] (duckdb)."""

    @pytest.fixture()
    def dbt_project(self, tmp_path: Path):
        dbt = _find_dbt()
        if dbt is None:
            pytest.skip("dbt CLI not available")
        contract = _contract()  # local platform → duckdb profile
        build = contract["builds"][0]
        out_dir = tmp_path / "dbt_project"
        files = DbtEngine().generate(contract, build, output_dir=out_dir, model_contracts=True)
        out_dir.mkdir(parents=True)
        for rel_path, content in files.items():
            target = out_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return dbt, out_dir

    def _dbt_build(self, dbt: str, project_dir: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [dbt, "build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)],
            capture_output=True,
            text=True,
            check=False,
            cwd=project_dir,
            timeout=300,
        )

    def test_conformant_model_builds_then_drift_fails(self, dbt_project):
        dbt, project_dir = dbt_project
        model_sql = project_dir / "models" / "marts" / "customer_orders.sql"

        # 1. Conformant SQL → dbt build passes with the enforced contract.
        model_sql.write_text(_CONFORMANT_SQL, encoding="utf-8")
        ok = self._dbt_build(dbt, project_dir)
        assert ok.returncode == 0, f"conformant build failed:\n{ok.stdout}\n{ok.stderr}"

        # 2. Drop a contracted column → contract failure.
        model_sql.write_text(_DRIFTED_SQL_DROPPED_COLUMN, encoding="utf-8")
        dropped = self._dbt_build(dbt, project_dir)
        assert dropped.returncode != 0
        assert "enforced contract that failed" in dropped.stdout
        assert "missing in definition" in dropped.stdout

        # 3. Retype a contracted column → contract failure.
        model_sql.write_text(_DRIFTED_SQL_RETYPED_COLUMN, encoding="utf-8")
        retyped = self._dbt_build(dbt, project_dir)
        assert retyped.returncode != 0
        assert "enforced contract that failed" in retyped.stdout
        assert "data type mismatch" in retyped.stdout

    def test_without_flag_drifted_model_ships_silently(self, tmp_path: Path):
        """The pre-flag status quo the card exists to fix: schema drift in
        user-edited SQL builds fine without enforcement."""
        dbt = _find_dbt()
        if dbt is None:
            pytest.skip("dbt CLI not available")
        contract = _contract()
        build = contract["builds"][0]
        out_dir = tmp_path / "dbt_project_noflag"
        files = DbtEngine().generate(contract, build, output_dir=out_dir)
        out_dir.mkdir(parents=True)
        for rel_path, content in files.items():
            target = out_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (out_dir / "models" / "marts" / "customer_orders.sql").write_text(
            _DRIFTED_SQL_DROPPED_COLUMN, encoding="utf-8"
        )
        # dbt run (not build) — skip the not_null data tests; the point is
        # that the *schema shape* is unenforced without the flag.
        result = subprocess.run(
            [dbt, "run", "--project-dir", str(out_dir), "--profiles-dir", str(out_dir)],
            capture_output=True,
            text=True,
            check=False,
            cwd=out_dir,
            timeout=300,
        )
        assert result.returncode == 0, f"unexpected failure:\n{result.stdout}\n{result.stderr}"
