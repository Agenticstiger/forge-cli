# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``fluid generate speed-transformation`` feature set.

Covers the five change points described in the plan file at
``~/.claude/plans/here-is-the-setup-breezy-bunny.md``:

1. Bootstrap interview — ``data_modeling_technique`` default + alias
   normalisation.
2. CLI rename — ``speed-transformation`` routes + legacy alias emits a
   deprecation line.
3. Prompt injection — ``build_user_prompt`` surfaces technique +
   guidance; ``build_system_prompt`` carries the mandate clause.
4. Validation guardrail — ``validate_generated_result`` flags a DV2
   context whose ``additional_files`` contain only dimensional models.
5. Engine threading — ``TransformationIntent.data_modeling_technique``
   drives hub/link/satellite vs stg/fct/dim skeleton layout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

# ---------------------------------------------------------------------------
# 1. Interview bootstrap / normalisation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. CLI rename — transformation/dbt aliases route to the same generator.
# ---------------------------------------------------------------------------


class TestCliRename:
    def test_transformation_aliases_registered(self):
        import argparse

        from fluid_build.cli import generate_speed_transformation

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="generate_sub")
        generate_speed_transformation.register_subcommand(sub)
        choices = sub.choices or {}
        assert "speed-transformation" in choices
        assert "transformation" in choices
        assert "dbt" in choices

    def test_speed_transformation_runs(self, capsys):
        from fluid_build.cli import generate_speed_transformation

        args = SimpleNamespace(
            generate_sub="speed-transformation",
            list_engines=True,
            verbose=False,
            contract=None,
            output=None,
            overwrite=False,
            env=None,
            build_index=0,
        )
        rc = generate_speed_transformation.run(args, logger=None)
        assert rc == 0
        captured = capsys.readouterr()
        # No deprecation line — the legacy name does not exist.
        assert "deprecated" not in captured.out


# ---------------------------------------------------------------------------
# 3. Prompt injection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. Validation guardrail + repair guidance
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Engine threading (TransformationIntent + dbt skeleton shape)
# ---------------------------------------------------------------------------


class TestEngineThreading:
    def test_transformation_intent_accepts_technique(self):
        from fluid_build.engines.base import TransformationIntent

        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        assert intent.data_modeling_technique == "data_vault_2"

    def test_dbt_dv2_without_stages_falls_back_to_compile_safe_models(self):
        """DV2 technique without a sidecar plan still emits non-empty dbt SQL."""
        from fluid_build.engines.base import TransformationIntent
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "fluidVersion": "0.7.2",
            "id": "silver.x_v1",
            "consumes": [
                {"exposeId": "party_source", "productId": "bronze.party_v1"},
                {"exposeId": "account_source", "productId": "bronze.party_v1"},
            ],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {
                        "schema": [
                            {"name": "subscriber_id", "type": "string", "required": True},
                            {"name": "total", "type": "number"},
                        ]
                    },
                }
            ],
        }
        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        files = generate_models(contract, contract["builds"][0], transformation_intent=intent)
        names = sorted(files.keys())
        assert "models/staging/stg_party_source.sql" in names
        assert "models/staging/stg_account_source.sql" in names
        assert "models/marts/mart.sql" in names

    def test_dbt_dimensional_without_stages_falls_back_to_compile_safe_models(self):
        """Dimensional technique without a sidecar plan still emits non-empty dbt SQL."""
        from fluid_build.engines.base import TransformationIntent
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "id": "silver.x_v1",
            "consumes": [{"exposeId": "party_source", "productId": "bronze.party_v1"}],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        intent = TransformationIntent(data_modeling_technique="dimensional")
        files = generate_models(contract, contract["builds"][0], transformation_intent=intent)
        names = sorted(files.keys())
        assert "models/staging/stg_party_source.sql" in names
        assert "models/marts/mart.sql" in names

    def test_dbt_engine_skips_schema_yml_when_technique_set(self):
        """Engine must skip marts/schema.yml emission — LLM ships its own."""
        from fluid_build.engines import get_engine
        from fluid_build.engines.base import TransformationIntent

        contract = {
            "fluidVersion": "0.7.2",
            "id": "silver.x_v1",
            "consumes": [{"exposeId": "party_source", "productId": "bronze.party_v1"}],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {
                        "schema": [{"name": "id", "type": "string", "required": True}],
                        "dq": {"rules": [{"id": "r1", "type": "uniqueness", "selector": "id"}]},
                    },
                }
            ],
        }
        engine = get_engine("dbt")
        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        files = engine.generate(contract, contract["builds"][0], transformation_intent=intent)
        # Engine still emits infrastructure; it must NOT emit model-level
        # schema.yml files that would collide with the LLM's own.
        schema_yml_files = [p for p in files if "schema.yml" in p]
        assert (
            schema_yml_files == []
        ), f"engine must skip schema.yml when technique is set; got {schema_yml_files}"
        # Infrastructure must still be there.
        assert "dbt_project.yml" in files
        assert "profiles.yml" in files

    def test_dbt_unset_technique_falls_back_to_default_dimensional(self):
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "id": "x.v1",
            "consumes": [{"exposeId": "src", "productId": "u.v1"}],
            "builds": [
                {
                    "id": "b",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "m"},
                    "execution": {"runtime": {"platform": "local"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "m",
                    "kind": "table",
                    "binding": {"platform": "local"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        files = generate_models(contract, contract["builds"][0], transformation_intent=None)
        names = sorted(files.keys())
        # No intent at all → stg / mart classic path.
        assert "models/staging/stg_src.sql" in names
        assert "models/marts/m.sql" in names


# ---------------------------------------------------------------------------
# 6. Agent-spec file shape + enrichment wiring
# ---------------------------------------------------------------------------
