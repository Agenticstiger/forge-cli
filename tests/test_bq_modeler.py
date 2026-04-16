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

"""Tests for providers/gcp/plan/bq_modeler.py — transformation planning and validation."""

import pytest

from fluid_build.providers.gcp.plan.bq_modeler import (
    _validate_dataform_config,
    _validate_dbt_config,
    _validate_sql_config,
    plan_transformation_actions,
    validate_transformation_config,
)


# ── plan_transformation_actions dispatch ─────────────────────────────
class TestPlanTransformationActions:
    def test_unknown_engine_returns_empty(self):
        assert plan_transformation_actions({"engine": "spark"}, {}, "proj", "us") == []

    def test_no_engine_returns_empty(self):
        assert plan_transformation_actions({}, {}, "proj", "us") == []

    # ── dbt-bigquery ─────────────────────────────────────────────────
    def test_dbt_default_actions(self):
        actions = plan_transformation_actions(
            {"engine": "dbt-bigquery"},
            {"id": "c1"},
            "proj",
            "us-central1",
        )
        ops = [a["op"] for a in actions]
        assert "dbt.prepare_profile" in ops
        assert "dbt.run" in ops
        assert "dbt.test" in ops
        # install_deps defaults to True
        assert "dbt.install_deps" in ops

    def test_dbt_no_deps_no_tests(self):
        actions = plan_transformation_actions(
            {
                "engine": "dbt-bigquery",
                "properties": {"install_deps": False, "run_tests": False},
            },
            {"id": "c1"},
            "proj",
            "us",
        )
        ops = [a["op"] for a in actions]
        assert "dbt.install_deps" not in ops
        assert "dbt.test" not in ops

    def test_dbt_with_seed(self):
        actions = plan_transformation_actions(
            {"engine": "dbt-bigquery", "properties": {"run_seed": True}},
            {"id": "c1"},
            "proj",
            "us",
        )
        ops = [a["op"] for a in actions]
        assert "dbt.seed" in ops

    def test_dbt_with_docs(self):
        actions = plan_transformation_actions(
            {"engine": "dbt-bigquery", "properties": {"generate_docs": True}},
            {"id": "c1"},
            "proj",
            "us",
        )
        ops = [a["op"] for a in actions]
        assert "dbt.docs_generate" in ops

    def test_dbt_profile_uses_properties(self):
        actions = plan_transformation_actions(
            {
                "engine": "dbt-bigquery",
                "properties": {
                    "project": "custom",
                    "dataset": "ds",
                    "target": "dev",
                    "threads": 8,
                },
            },
            {"id": "c1"},
            "proj",
            "us",
        )
        profile = next(a for a in actions if a["op"] == "dbt.prepare_profile")
        assert profile["project"] == "custom"
        assert profile["dataset"] == "ds"
        assert profile["target"] == "dev"
        assert profile["threads"] == 8

    # ── dataform ─────────────────────────────────────────────────────
    def test_dataform_basic(self):
        actions = plan_transformation_actions(
            {
                "engine": "dataform",
                "properties": {"repository_id": "repo1"},
            },
            {"id": "c1"},
            "proj",
            "us-central1",
        )
        ops = [a["op"] for a in actions]
        assert "dataform.compile" in ops
        assert "dataform.run" in ops

    def test_dataform_with_workspace(self):
        actions = plan_transformation_actions(
            {
                "engine": "dataform",
                "properties": {"repository_id": "repo1", "workspace_id": "ws1"},
            },
            {},
            "proj",
            "us",
        )
        ops = [a["op"] for a in actions]
        assert "dataform.ensure_workspace" in ops

    def test_dataform_no_repo_raises(self):
        with pytest.raises(ValueError, match="repository_id"):
            plan_transformation_actions(
                {"engine": "dataform", "properties": {}},
                {},
                "proj",
                "us",
            )

    # ── sql ──────────────────────────────────────────────────────────
    def test_sql_statements(self):
        actions = plan_transformation_actions(
            {
                "engine": "sql",
                "properties": {"sql_statements": ["SELECT 1", "SELECT 2"]},
            },
            {},
            "proj",
            "us",
        )
        assert len(actions) == 2
        assert all(a["op"] == "bq.execute_sql" for a in actions)

    def test_sql_files(self):
        actions = plan_transformation_actions(
            {
                "engine": "sql",
                "properties": {"sql_files": ["a.sql"]},
            },
            {},
            "proj",
            "us",
        )
        assert len(actions) == 1
        assert actions[0]["op"] == "bq.execute_sql_file"

    def test_sql_empty(self):
        actions = plan_transformation_actions(
            {"engine": "sql", "properties": {}},
            {},
            "proj",
            "us",
        )
        assert actions == []


# ── validate_transformation_config ──────────────────────────────────
class TestValidateTransformationConfig:
    def test_no_engine(self):
        errs = validate_transformation_config({}, {})
        assert any("engine is required" in e for e in errs)

    def test_unknown_engine(self):
        errs = validate_transformation_config({"engine": "spark"}, {})
        assert any("Unknown" in e for e in errs)

    def test_valid_dbt(self):
        errs = validate_transformation_config(
            {"engine": "dbt-bigquery", "properties": {"project": "p"}}, {}
        )
        assert errs == []

    def test_valid_dataform(self):
        errs = validate_transformation_config(
            {"engine": "dataform", "properties": {"repository_id": "r"}}, {}
        )
        assert errs == []

    def test_valid_sql(self):
        errs = validate_transformation_config(
            {"engine": "sql", "properties": {"sql_statements": ["SELECT 1"]}}, {}
        )
        assert errs == []


# ── _validate_dbt_config ────────────────────────────────────────────
class TestValidateDbtConfig:
    def test_missing_project_and_key(self):
        errs = _validate_dbt_config({})
        assert any("project" in e or "keyfile" in e for e in errs)

    def test_has_project(self):
        errs = _validate_dbt_config({"project": "p"})
        assert errs == []

    def test_has_keyfile(self):
        errs = _validate_dbt_config({"keyfile_path": "/k"})
        assert errs == []

    def test_bad_threads(self):
        errs = _validate_dbt_config({"project": "p", "threads": -1})
        assert any("threads" in e for e in errs)

    def test_bad_timeout(self):
        errs = _validate_dbt_config({"project": "p", "run_timeout": "abc"})
        assert any("run_timeout" in e for e in errs)


# ── _validate_dataform_config ───────────────────────────────────────
class TestValidateDataformConfig:
    def test_missing_repo(self):
        errs = _validate_dataform_config({})
        assert any("repository_id" in e for e in errs)

    def test_ok(self):
        errs = _validate_dataform_config({"repository_id": "r"})
        assert errs == []

    def test_bad_timeout(self):
        errs = _validate_dataform_config({"repository_id": "r", "compile_timeout": -5})
        assert any("compile_timeout" in e for e in errs)


# ── _validate_sql_config ────────────────────────────────────────────
class TestValidateSqlConfig:
    def test_empty(self):
        errs = _validate_sql_config({})
        assert any("sql_statements" in e or "sql_files" in e for e in errs)

    def test_has_statements(self):
        errs = _validate_sql_config({"sql_statements": ["SELECT 1"]})
        assert errs == []

    def test_has_files(self):
        errs = _validate_sql_config({"sql_files": ["a.sql"]})
        assert errs == []

    def test_statements_not_list(self):
        errs = _validate_sql_config({"sql_statements": "SELECT 1"})
        assert any("must be a list" in e for e in errs)

    def test_files_not_list(self):
        errs = _validate_sql_config({"sql_files": "a.sql"})
        assert any("must be a list" in e for e in errs)
