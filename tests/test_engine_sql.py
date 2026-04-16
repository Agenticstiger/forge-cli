# Copyright 2024-2026 Agentics Transformation Ltd

"""Tests for the SQL transformation engine."""

import pytest

from fluid_build.engines import get_engine
from fluid_build.engines.base import Severity, TransformationIntent
from fluid_build.engines.sql.scripts import generate_scripts


class TestSqlEngineValidation:
    def test_valid_sql_engine(self):
        engine = get_engine("sql")
        build = {
            "id": "main",
            "engine": "sql",
            "pattern": "embedded-logic",
            "properties": {"sql": "SELECT 1"},
        }
        issues = engine.validate({}, build)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert len(errors) == 0

    def test_wrong_engine(self):
        engine = get_engine("sql")
        build = {"id": "main", "engine": "dbt", "pattern": "embedded-logic"}
        issues = engine.validate({}, build)
        assert any(i.severity == Severity.ERROR for i in issues)


class TestEmbeddedLogic:
    def test_basic_sql(self):
        contract = {"id": "test.product"}
        build = {
            "id": "transform",
            "pattern": "embedded-logic",
            "properties": {"sql": "SELECT 1 AS id, 'hello' AS name"},
        }
        files = generate_scripts(contract, build)
        assert "transform.sql" in files
        assert "SELECT 1 AS id" in files["transform.sql"]
        assert "Contract: test.product" in files["transform.sql"]

    def test_no_sql(self):
        contract = {"id": "test"}
        build = {"id": "empty", "pattern": "embedded-logic", "properties": {}}
        files = generate_scripts(contract, build)
        assert "empty.sql" in files
        assert "TODO" in files["empty.sql"]


class TestMultiStage:
    def test_ordered_scripts(self):
        contract = {"id": "test.pipeline"}
        build = {
            "id": "pipeline",
            "pattern": "multi-stage",
            "properties": {
                "stages": [
                    {
                        "name": "extract",
                        "properties": {"sql": "SELECT * FROM raw"},
                        "dependsOn": [],
                    },
                    {
                        "name": "transform",
                        "properties": {"sql": "SELECT id FROM extract"},
                        "dependsOn": ["extract"],
                    },
                    {
                        "name": "load",
                        "properties": {"sql": "INSERT INTO target SELECT * FROM transform"},
                        "dependsOn": ["transform"],
                    },
                ]
            },
        }
        files = generate_scripts(contract, build)
        assert "01_extract.sql" in files
        assert "02_transform.sql" in files
        assert "03_load.sql" in files
        # Check dependency comments
        assert "Depends on: extract" in files["02_transform.sql"]


class TestWithTransformationIntent:
    def test_intent_stages(self):
        intent = TransformationIntent(
            stages=[
                {"name": "stg_orders", "sql": "SELECT * FROM raw.orders", "depends_on": []},
                {
                    "name": "mart_summary",
                    "sql": "SELECT count(*) FROM stg_orders",
                    "depends_on": ["stg_orders"],
                },
            ]
        )
        contract = {"id": "test"}
        build = {"id": "main", "pattern": "embedded-logic", "properties": {}}
        files = generate_scripts(contract, build, transformation_intent=intent)
        assert "01_stg_orders.sql" in files
        assert "02_mart_summary.sql" in files


class TestPlatformFiltering:
    def test_sql_is_platform_agnostic(self):
        engine = get_engine("sql")
        assert engine.supported_platforms is None

    def test_glue_is_aws_only(self):
        engine = get_engine("glue")
        assert "aws" in engine.supported_platforms

    def test_dataform_is_gcp_only(self):
        engine = get_engine("dataform")
        assert "gcp" in engine.supported_platforms

    def test_dbt_is_platform_agnostic(self):
        engine = get_engine("dbt")
        assert engine.supported_platforms is None

    def test_list_engines_for_platform(self):
        from fluid_build.engines import list_engines_for_platform

        gcp = list_engines_for_platform("gcp")
        assert "dataform" in gcp
        assert "dataflow" in gcp
        assert "dbt" in gcp  # agnostic
        assert "glue" not in gcp  # aws only

        aws = list_engines_for_platform("aws")
        assert "glue" in aws
        assert "dbt" in aws
        assert "dataform" not in aws
