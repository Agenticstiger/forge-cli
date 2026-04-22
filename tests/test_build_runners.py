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

"""Tests for fluid_build.build_runners (migrated from tests/test_execute.py).

Module-path mapping from the legacy ``cli/execute.py``:

- ``fluid_build.cli.execute.is_dbt_build`` / ``_resolve_env_placeholders``
  / ``run_builds_from_args`` (was ``run``) → ``fluid_build.build_runners.base``
- Python script runner (``execute_build``, ``resolve_script_path``) →
  ``fluid_build.build_runners.python.runner``
- dbt runner (``execute_dbt_build``, ``build_dbt_command``,
  ``resolve_dbt_project_path``, all ``_*dbt*`` container + env helpers) →
  ``fluid_build.build_runners.dbt.runner``
- dbt profile builders (``_build_generated_dbt_profile``,
  ``_create_temp_dbt_profiles_dir``, ``_load_dbt_project_config``) →
  ``fluid_build.build_runners.dbt.profiles``

``_from_apply=True`` was renamed to ``force_run=True`` (no deprecation banner
to suppress anymore — ``fluid execute`` was removed).
"""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from fluid_build.build_runners.base import (
    _resolve_env_placeholders,
    is_dbt_build,
)
from fluid_build.build_runners.base import (
    run_builds_from_args as run,
)
from fluid_build.build_runners.dbt.profiles import (
    _build_generated_dbt_profile,
    _create_temp_dbt_profiles_dir,
)
from fluid_build.build_runners.dbt.runner import (
    _build_containerized_dbt_command,
    _collect_dbt_container_env,
    _dbt_command_supports_adapter,
    _render_command_for_log,
    build_dbt_command,
    execute_dbt_build,
    resolve_dbt_project_path,
)
from fluid_build.build_runners.python.runner import (
    execute_build,
    resolve_script_path,
)
from fluid_build.cli._common import CLIError

# ── resolve_script_path ───────────────────────────────────────────────


class TestResolveScriptPath:
    def test_returns_py_file_when_exists(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script = repo_dir / "ingest.py"
        script.touch()

        build = {"repository": "repo", "properties": {"model": "ingest"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script

    def test_returns_file_without_extension(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script = repo_dir / "ingest"
        script.touch()

        build = {"repository": "repo", "properties": {"model": "ingest"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script

    def test_returns_none_when_script_missing(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        build = {"repository": "repo", "properties": {"model": "missing_model"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result is None

    def test_uses_default_repository_and_model(self, tmp_path):
        # defaults: repository="./" model="ingest"
        ingest_py = tmp_path / "ingest.py"
        ingest_py.touch()

        build = {}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == ingest_py

    def test_prefers_py_extension_over_bare_file(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        script_py = repo_dir / "transform.py"
        script_py.touch()
        script_bare = repo_dir / "transform"
        script_bare.touch()

        build = {"repository": "repo", "properties": {"model": "transform"}}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_script_path(contract_path, build)
        assert result == script_py


class TestResolveDbtProjectPath:
    def test_returns_project_dir_when_dbt_project_exists(self, tmp_path):
        repo_dir = tmp_path / "dbt_repo"
        repo_dir.mkdir()
        (repo_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")

        build = {"engine": "dbt", "repository": "dbt_repo"}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_dbt_project_path(contract_path, build)
        assert result == repo_dir.resolve()

    def test_returns_none_when_dbt_project_missing(self, tmp_path):
        repo_dir = tmp_path / "dbt_repo"
        repo_dir.mkdir()

        build = {"engine": "dbt", "repository": "dbt_repo"}
        contract_path = tmp_path / "contract.yaml"
        contract_path.touch()

        result = resolve_dbt_project_path(contract_path, build)
        assert result is None


class TestIsDbtBuild:
    def test_plain_dbt_engine_is_dbt(self):
        assert is_dbt_build({"engine": "dbt"}) is True

    def test_known_adapter_variants_are_dbt(self):
        for engine in ("dbt-bigquery", "dbt-duckdb"):
            assert is_dbt_build({"engine": engine}) is True

    def test_other_warehouse_adapters_are_dbt(self):
        # Warehouse-agnostic: any dbt-<adapter> should route to the dbt path.
        for engine in ("dbt-snowflake", "dbt-redshift", "dbt-postgres", "dbt-spark"):
            assert is_dbt_build({"engine": engine}) is True

    def test_engine_is_case_insensitive_and_trimmed(self):
        assert is_dbt_build({"engine": "  DBT-Snowflake  "}) is True

    def test_non_dbt_engines_are_not_dbt(self):
        for engine in ("python", "sql", "spark", "custom", "", None):
            assert is_dbt_build({"engine": engine}) is False

    def test_missing_engine_is_not_dbt(self):
        assert is_dbt_build({}) is False

    def test_prefix_only_match_without_dash_is_not_dbt(self):
        # Guard against "dbtx" false positives.
        assert is_dbt_build({"engine": "dbtx"}) is False


class TestResolveEnvPlaceholders:
    def test_replaces_placeholder_in_plain_string(self, monkeypatch):
        monkeypatch.setenv("DB_NAME", "TELCO")
        assert _resolve_env_placeholders("db={{ env.DB_NAME }}") == "db=TELCO"

    def test_missing_env_resolves_to_empty_string(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _resolve_env_placeholders("x={{ env.MISSING_VAR }}") == "x="

    def test_recurses_into_nested_dicts_and_lists(self, monkeypatch):
        monkeypatch.setenv("A", "alpha")
        monkeypatch.setenv("B", "beta")
        value = {
            "top": "{{ env.A }}",
            "nested": [{"inner": "{{ env.B }}"}, "plain"],
        }
        assert _resolve_env_placeholders(value) == {
            "top": "alpha",
            "nested": [{"inner": "beta"}, "plain"],
        }

    def test_non_string_scalars_passthrough(self):
        assert _resolve_env_placeholders(42) == 42
        assert _resolve_env_placeholders(True) is True
        assert _resolve_env_placeholders(None) is None


class TestBuildDbtCommand:
    def _make_project(self, tmp_path):
        project_dir = tmp_path / "dbt_project"
        project_dir.mkdir()
        (project_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")
        return project_dir

    def test_builds_full_project_command_for_multi_output_build(self, tmp_path):
        project_dir = self._make_project(tmp_path)

        build = {
            "id": "b1",
            "engine": "dbt",
            "outputs": ["one", "two"],
            "properties": {"model": "mart_orders"},
        }

        with patch(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            return_value="/opt/homebrew/bin/dbt",
        ):
            cmd = build_dbt_command(build, project_dir, profiles_dir=Path("/tmp/dbt-profiles"))

        assert cmd[:4] == ["/opt/homebrew/bin/dbt", "build", "--project-dir", str(project_dir)]
        assert "--no-partial-parse" not in cmd
        assert "--profiles-dir" in cmd
        assert "/tmp/dbt-profiles" in cmd
        assert "--profile" in cmd
        assert "telco" in cmd
        assert "--select" not in cmd

    def test_uses_model_selector_for_single_output_build(self, tmp_path):
        project_dir = self._make_project(tmp_path)
        build = {
            "id": "b1",
            "engine": "dbt",
            "outputs": ["one"],
            "properties": {"model": "mart_orders"},
        }

        with patch(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            return_value="/opt/homebrew/bin/dbt",
        ):
            cmd = build_dbt_command(build, project_dir)

        assert "--no-partial-parse" not in cmd
        assert "--select" in cmd
        assert "+mart_orders+" in cmd

    def test_resolves_env_placeholders_in_vars(self, tmp_path, monkeypatch):
        project_dir = self._make_project(tmp_path)
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "TELCO_LAB")

        build = {
            "id": "b1",
            "engine": "dbt",
            "properties": {"vars": {"database": "{{ env.SNOWFLAKE_DATABASE }}"}},
        }

        with patch(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            return_value="/opt/homebrew/bin/dbt",
        ):
            cmd = build_dbt_command(build, project_dir)

        vars_index = cmd.index("--vars")
        assert json.loads(cmd[vars_index + 1]) == {"database": "TELCO_LAB"}


class TestGeneratedDbtProfile:
    def test_runtime_resources_resolve_env_templates(self, monkeypatch):
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "TELCO_LAB")
        monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
        monkeypatch.setenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
        monkeypatch.setenv("SNOWFLAKE_FLUID_SCHEMA", "TELCO_FLUID_DEMO")
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")

        build = {
            "execution": {
                "runtime": {
                    "platform": "snowflake",
                    "resources": {
                        "database": "{{ env.SNOWFLAKE_DATABASE }}",
                        "warehouse": "{{ env.SNOWFLAKE_WAREHOUSE }}",
                        "schema": "{{ env.SNOWFLAKE_FLUID_SCHEMA }}",
                        "role": "{{ env.SNOWFLAKE_ROLE }}",
                    },
                }
            },
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "telco"})

        output = profile["telco"]["outputs"]["dev"]
        assert output["database"] == "TELCO_LAB"
        assert output["warehouse"] == "COMPUTE_WH"
        assert output["schema"] == "TELCO_FLUID_DEMO"
        assert output["role"] == "ACCOUNTADMIN"

    def test_bigquery_defaults_to_oauth_when_no_credentials_set(self, monkeypatch):
        for var in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GCP_KEYFILE",
            "GCP_SERVICE_ACCOUNT_JSON",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GCP_IMPERSONATE_SERVICE_ACCOUNT",
            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GCP_PROJECT", "my-proj")

        build = {
            "execution": {"runtime": {"platform": "bigquery", "resources": {"dataset": "lab"}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "bq_demo"})
        output = profile["bq_demo"]["outputs"]["dev"]

        assert output["method"] == "oauth"
        assert output["project"] == "my-proj"
        assert output["dataset"] == "lab"

    def test_bigquery_uses_service_account_when_keyfile_set(self, monkeypatch, tmp_path):
        keyfile = tmp_path / "sa.json"
        keyfile.write_text("{}")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(keyfile))
        monkeypatch.delenv("GCP_SERVICE_ACCOUNT_JSON", raising=False)
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
        monkeypatch.setenv("GCP_PROJECT", "my-proj")

        build = {
            "execution": {"runtime": {"platform": "bigquery", "resources": {"dataset": "lab"}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "bq_demo"})
        output = profile["bq_demo"]["outputs"]["dev"]

        assert output["method"] == "service-account"
        assert output["keyfile"] == str(keyfile)

    def test_bigquery_uses_service_account_json_when_inline_creds_set(self, monkeypatch):
        monkeypatch.setenv(
            "GCP_SERVICE_ACCOUNT_JSON",
            '{"type": "service_account", "project_id": "inline-proj"}',
        )
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setenv("GCP_PROJECT", "my-proj")

        build = {
            "execution": {"runtime": {"platform": "bigquery", "resources": {"dataset": "lab"}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "bq_demo"})
        output = profile["bq_demo"]["outputs"]["dev"]

        assert output["method"] == "service-account-json"
        assert output["keyfile_json"] == {
            "type": "service_account",
            "project_id": "inline-proj",
        }

    def test_bigquery_malformed_inline_json_falls_back_to_oauth(self, monkeypatch):
        monkeypatch.setenv("GCP_SERVICE_ACCOUNT_JSON", "{not valid json")
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

        build = {
            "execution": {"runtime": {"platform": "bigquery", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "bq_demo"})
        output = profile["bq_demo"]["outputs"]["dev"]

        assert output["method"] == "oauth"
        assert "keyfile_json" not in output

    def test_bigquery_includes_impersonation_when_set(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.delenv("GCP_SERVICE_ACCOUNT_JSON", raising=False)
        monkeypatch.setenv("GCP_IMPERSONATE_SERVICE_ACCOUNT", "runner@proj.iam.gserviceaccount.com")

        build = {
            "execution": {"runtime": {"platform": "bigquery", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "bq_demo"})
        output = profile["bq_demo"]["outputs"]["dev"]

        assert output["impersonate_service_account"] == "runner@proj.iam.gserviceaccount.com"

    def test_redshift_defaults_to_password_auth(self, monkeypatch):
        for var in ("REDSHIFT_CLUSTER_ID", "REDSHIFT_IAM_PROFILE", "REDSHIFT_USE_IAM"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("REDSHIFT_HOST", "cluster.eu-west-1.redshift.amazonaws.com")
        monkeypatch.setenv("REDSHIFT_USER", "admin")
        monkeypatch.setenv("REDSHIFT_PASSWORD", "hunter2")
        monkeypatch.setenv("REDSHIFT_DATABASE", "analytics")

        build = {
            "execution": {"runtime": {"platform": "redshift", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "rs"})
        output = profile["rs"]["outputs"]["dev"]

        assert output["password"] == "hunter2"
        assert "method" not in output

    def test_redshift_uses_iam_when_cluster_and_profile_set(self, monkeypatch):
        monkeypatch.setenv("REDSHIFT_CLUSTER_ID", "prod-cluster")
        monkeypatch.setenv("REDSHIFT_IAM_PROFILE", "dbt-runner")
        monkeypatch.setenv("REDSHIFT_HOST", "cluster.eu-west-1.redshift.amazonaws.com")
        monkeypatch.setenv("REDSHIFT_USER", "dbt_user")
        monkeypatch.setenv("REDSHIFT_DATABASE", "analytics")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")

        build = {
            "execution": {"runtime": {"platform": "redshift", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "rs"})
        output = profile["rs"]["outputs"]["dev"]

        assert output["method"] == "iam"
        assert output["cluster_id"] == "prod-cluster"
        assert output["iam_profile"] == "dbt-runner"
        assert output["region"] == "eu-west-1"
        assert "password" not in output

    def test_postgres_profile(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "db.internal")
        monkeypatch.setenv("PGPORT", "5433")
        monkeypatch.setenv("PGUSER", "analytics")
        monkeypatch.setenv("PGPASSWORD", "hunter2")
        monkeypatch.setenv("PGDATABASE", "lab")
        monkeypatch.setenv("PGSSLMODE", "require")

        build = {
            "execution": {"runtime": {"platform": "postgres", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "pg"})
        output = profile["pg"]["outputs"]["dev"]

        assert output["type"] == "postgres"
        assert output["host"] == "db.internal"
        assert output["port"] == 5433
        assert output["user"] == "analytics"
        assert output["password"] == "hunter2"
        assert output["dbname"] == "lab"
        assert output["sslmode"] == "require"

    def test_postgresql_alias_platform(self, monkeypatch):
        # dbt historically spells the type `postgres`, but teams commonly use
        # `postgresql` for the platform. Both should resolve to the same branch.
        monkeypatch.setenv("PGHOST", "db.internal")
        monkeypatch.setenv("PGUSER", "u")
        monkeypatch.setenv("PGPASSWORD", "p")
        monkeypatch.setenv("PGDATABASE", "lab")

        build = {
            "execution": {"runtime": {"platform": "postgresql", "resources": {}}},
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "pg"})
        assert profile["pg"]["outputs"]["dev"]["type"] == "postgres"

    def test_databricks_profile(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-abc.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-hunter2")
        monkeypatch.setenv("DATABRICKS_CATALOG", "main")

        build = {
            "execution": {
                "runtime": {"platform": "databricks", "resources": {"schema": "analytics"}}
            },
            "properties": {},
        }

        profile = _build_generated_dbt_profile(build, {"profile": "db"})
        output = profile["db"]["outputs"]["dev"]

        assert output["type"] == "databricks"
        assert output["host"] == "https://dbc-abc.cloud.databricks.com"
        assert output["http_path"] == "/sql/1.0/warehouses/abc123"
        assert output["token"] == "dapi-hunter2"
        assert output["catalog"] == "main"
        assert output["schema"] == "analytics"


class TestCollectDbtContainerEnv:
    """Env forwarding into the dbt container must cover each adapter's conventions."""

    def _only_sensitive_keys(self, env: dict, *known_keys: str) -> dict:
        """Filter out whatever the inherited shell env already had — we only
        want to assert on keys this test set."""
        return {k: v for k, v in env.items() if k in known_keys}

    def test_forwards_postgres_bare_env_keys(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "db.internal")
        monkeypatch.setenv("PGUSER", "analytics")
        monkeypatch.setenv("PGPASSWORD", "hunter2")

        env = _collect_dbt_container_env()
        forwarded = self._only_sensitive_keys(env, "PGHOST", "PGUSER", "PGPASSWORD")

        assert forwarded == {
            "PGHOST": "db.internal",
            "PGUSER": "analytics",
            "PGPASSWORD": "hunter2",
        }

    def test_forwards_clickhouse_and_trino_prefixes(self, monkeypatch):
        monkeypatch.setenv("CLICKHOUSE_HOST", "clickhouse.internal")
        monkeypatch.setenv("TRINO_USER", "dbt")
        monkeypatch.setenv("STARBURST_PASSWORD", "hunter2")

        env = _collect_dbt_container_env()
        forwarded = self._only_sensitive_keys(
            env, "CLICKHOUSE_HOST", "TRINO_USER", "STARBURST_PASSWORD"
        )

        assert forwarded == {
            "CLICKHOUSE_HOST": "clickhouse.internal",
            "TRINO_USER": "dbt",
            "STARBURST_PASSWORD": "hunter2",
        }

    def test_respects_fluid_dbt_forward_env_exact_key(self, monkeypatch):
        monkeypatch.setenv("FLUID_DBT_FORWARD_ENV", "MY_CUSTOM_TOKEN")
        monkeypatch.setenv("MY_CUSTOM_TOKEN", "hunter2")
        monkeypatch.setenv("MY_UNRELATED", "nope")

        env = _collect_dbt_container_env()

        assert env.get("MY_CUSTOM_TOKEN") == "hunter2"
        assert "MY_UNRELATED" not in env

    def test_respects_fluid_dbt_forward_env_prefix(self, monkeypatch):
        monkeypatch.setenv("FLUID_DBT_FORWARD_ENV", "MYAPP_")
        monkeypatch.setenv("MYAPP_HOST", "host")
        monkeypatch.setenv("MYAPP_TOKEN", "hunter2")
        monkeypatch.setenv("OTHERAPP_HOST", "nope")

        env = _collect_dbt_container_env()

        assert env.get("MYAPP_HOST") == "host"
        assert env.get("MYAPP_TOKEN") == "hunter2"
        assert "OTHERAPP_HOST" not in env

    def test_skips_empty_values(self, monkeypatch):
        # Empty values break `docker run -e KEY` (no inheritance to pick up).
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")

        env = _collect_dbt_container_env()

        assert "SNOWFLAKE_ACCOUNT" not in env
        assert env.get("SNOWFLAKE_USER") == "user"


class TestContainerizedDbtCommand:
    def test_builds_bootstrap_container_command(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "super-secret-never-echoed")

        cmd = _build_containerized_dbt_command(
            "snowflake",
            [
                "build",
                "--project-dir",
                str(project_dir),
                "--profiles-dir",
                str(profiles_dir),
                "--profile",
                "telco",
            ],
            project_dir,
            profiles_dir,
        )

        assert cmd[:3] == ["docker", "run", "--rm"]
        assert any("/workspace/project" in part for part in cmd)
        assert any("/workspace/profiles" in part for part in cmd)
        assert "python:3.12-slim" in cmd
        assert any("dbt-snowflake" in part for part in cmd)

    def test_env_forwarding_uses_key_only_form(self, tmp_path, monkeypatch):
        # `-e KEY` (no `=value`) keeps secrets out of argv (visible via `ps`).
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "super-secret-never-echoed")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")

        cmd = _build_containerized_dbt_command(
            "snowflake",
            ["build", "--project-dir", str(project_dir)],
            project_dir,
            None,
        )

        assert "super-secret-never-echoed" not in " ".join(cmd)
        env_indices = [i for i, part in enumerate(cmd) if part == "-e"]
        assert env_indices, "expected at least one -e flag"
        for idx in env_indices:
            assert "=" not in cmd[idx + 1], f"-e {cmd[idx + 1]} must not inline a value"

    def test_uses_configured_docker_image_when_set(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.setenv("DBT_DOCKER_IMAGE", "example/dbt-snowflake:custom")

        cmd = _build_containerized_dbt_command(
            "snowflake",
            ["build", "--project-dir", str(project_dir)],
            project_dir,
            None,
        )

        assert "example/dbt-snowflake:custom" in cmd
        assert "python:3.12-slim" not in cmd


class TestBuildDbtCommandExecutionSelection:
    def _make_project(self, tmp_path):
        project_dir = tmp_path / "dbt_project"
        project_dir.mkdir()
        (project_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")
        return project_dir

    def test_uses_container_when_local_adapter_missing(self, tmp_path):
        project_dir = self._make_project(tmp_path)
        build = {
            "engine": "dbt",
            "execution": {"runtime": {"platform": "snowflake"}},
            "properties": {},
        }

        with (
            patch(
                "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
                return_value="/opt/homebrew/bin/dbt",
            ),
            patch(
                "fluid_build.build_runners.dbt.runner._dbt_command_supports_adapter",
                return_value=False,
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            patch(
                "fluid_build.build_runners.dbt.runner._build_containerized_dbt_command",
                return_value=["docker", "run", "dbt"],
            ) as mock_container,
        ):
            cmd = build_dbt_command(build, project_dir)

        assert cmd == ["docker", "run", "dbt"]
        mock_container.assert_called_once()

    def test_uses_configured_dbt_command_prefix_verbatim(self, tmp_path, monkeypatch):
        project_dir = self._make_project(tmp_path)
        build = {"engine": "dbt", "properties": {}}
        monkeypatch.setenv("DBT_EXECUTABLE", "docker compose exec -T dbt-runner dbt")

        cmd = build_dbt_command(build, project_dir)

        assert cmd[:6] == ["docker", "compose", "exec", "-T", "dbt-runner", "dbt"]

    def test_container_path_includes_no_partial_parse(self, tmp_path):
        project_dir = self._make_project(tmp_path)
        build = {
            "engine": "dbt",
            "execution": {"runtime": {"platform": "snowflake"}},
            "properties": {},
        }

        with (
            patch(
                "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
                return_value="/opt/homebrew/bin/dbt",
            ),
            patch(
                "fluid_build.build_runners.dbt.runner._dbt_command_supports_adapter",
                return_value=False,
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            patch(
                "fluid_build.build_runners.dbt.runner._build_containerized_dbt_command",
                side_effect=lambda adapter, args, pd, pfd: ["docker", *args],
            ),
        ):
            cmd = build_dbt_command(build, project_dir)

        assert "--no-partial-parse" in cmd


class TestRenderCommandForLog:
    def test_redacts_vars_payload(self):
        cmd = ["dbt", "build", "--vars", '{"password": "hunter2"}']
        rendered = _render_command_for_log(cmd)
        assert "hunter2" not in rendered
        assert "<redacted>" in rendered

    def test_redacts_sensitive_e_flag_value(self):
        cmd = ["docker", "run", "-e", "SNOWFLAKE_PASSWORD=hunter2", "image"]
        rendered = _render_command_for_log(cmd)
        assert "hunter2" not in rendered
        assert "SNOWFLAKE_PASSWORD=<redacted>" in rendered

    def test_preserves_non_sensitive_e_flag(self):
        cmd = ["docker", "run", "-e", "SNOWFLAKE_ACCOUNT=acct", "image"]
        rendered = _render_command_for_log(cmd)
        assert "SNOWFLAKE_ACCOUNT=acct" in rendered

    def test_preserves_key_only_e_flag(self):
        # `-e KEY` (no value) — safe by construction, nothing to redact.
        cmd = ["docker", "run", "-e", "SNOWFLAKE_PASSWORD", "image"]
        rendered = _render_command_for_log(cmd)
        assert rendered == "docker run -e SNOWFLAKE_PASSWORD image"

    def test_redacts_long_form_env_flag(self):
        cmd = ["docker", "run", "--env", "API_TOKEN=abc123", "image"]
        rendered = _render_command_for_log(cmd)
        assert "abc123" not in rendered


class TestCreateTempDbtProfilesDir:
    def test_profiles_yml_written_with_600_perms(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")

        build = {
            "execution": {
                "runtime": {
                    "platform": "snowflake",
                    "resources": {"database": "DB", "warehouse": "WH"},
                }
            },
            "properties": {},
        }

        profiles_dir, temp_dir = _create_temp_dbt_profiles_dir(build, {"profile": "telco"})
        try:
            assert profiles_dir is not None
            profiles_path = profiles_dir / "profiles.yml"
            assert profiles_path.exists()
            import stat as _stat

            mode = _stat.S_IMODE(profiles_path.stat().st_mode)
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()


class TestDbtCommandSupportsAdapter:
    def test_result_is_cached_across_calls(self, monkeypatch):
        # Confirm @lru_cache avoids re-invoking `dbt --version`.
        _dbt_command_supports_adapter.cache_clear()
        call_count = {"n": 0}

        def fake_run(*args, **kwargs):
            call_count["n"] += 1
            mock = Mock()
            mock.stdout = "Plugins:\n  - snowflake: 1.11.4\n"
            mock.stderr = ""
            return mock

        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.subprocess.run", fake_run)

        assert _dbt_command_supports_adapter("/opt/dbt", "snowflake") is True
        assert _dbt_command_supports_adapter("/opt/dbt", "snowflake") is True
        assert call_count["n"] == 1
        _dbt_command_supports_adapter.cache_clear()


# ── execute_build ─────────────────────────────────────────────────────


class TestExecuteBuild:
    def _make_script(self, tmp_path, name="ingest.py"):
        s = tmp_path / name
        s.touch()
        return s

    def test_dry_run_manual_returns_zero(self, tmp_path):
        build = {
            "id": "test-build",
            "execution": {"trigger": {"type": "manual", "iterations": 3}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.build_runners.python.runner.cprint"):
            result = execute_build(build, script, tmp_path, dry_run=True)
        assert result == 0

    def test_schedule_trigger_returns_zero(self, tmp_path):
        build = {
            "id": "sched-build",
            "execution": {"trigger": {"type": "schedule", "cron": "0 0 * * *"}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.build_runners.python.runner.cprint"):
            result = execute_build(build, script, tmp_path)
        assert result == 0

    def test_unknown_trigger_returns_one(self, tmp_path):
        build = {
            "id": "bad-build",
            "execution": {"trigger": {"type": "unknown_type"}},
        }
        script = self._make_script(tmp_path)
        with patch("fluid_build.build_runners.python.runner.cprint"):
            result = execute_build(build, script, tmp_path)
        assert result == 1

    def test_successful_run_returns_zero(self, tmp_path):
        build = {
            "id": "ok-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.success"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
        ):
            result = execute_build(build, script, tmp_path, delay=0)
        assert result == 0

    def test_failed_run_returns_one(self, tmp_path):
        build = {
            "id": "fail-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="error!")

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.console_error"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
        ):
            result = execute_build(build, script, tmp_path, delay=0)
        assert result == 1

    def test_fail_fast_stops_on_first_failure(self, tmp_path):
        build = {
            "id": "fail-fast-build",
            "execution": {"trigger": {"type": "manual", "iterations": 3}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="")

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.console_error"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
        ):
            result = execute_build(build, script, tmp_path, delay=0, fail_fast=True)
        assert result == 1

    def test_exception_during_run_with_fail_fast(self, tmp_path):
        build = {
            "id": "exc-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.console_error"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = execute_build(build, script, tmp_path, delay=0, fail_fast=True)
        assert result == 1

    def test_delay_between_iterations(self, tmp_path):
        build = {
            "id": "multi-build",
            "execution": {"trigger": {"type": "manual", "iterations": 2}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.success"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
            patch("fluid_build.build_runners.python.runner.time.sleep") as mock_sleep,
        ):
            execute_build(build, script, tmp_path, delay=5)
        mock_sleep.assert_called_once_with(5)

    def test_contract_delay_overrides_arg(self, tmp_path):
        """delaySeconds in trigger overrides the delay arg."""
        build = {
            "id": "delay-contract-build",
            "execution": {"trigger": {"type": "manual", "iterations": 2, "delaySeconds": 10}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.success"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
            patch("fluid_build.build_runners.python.runner.time.sleep") as mock_sleep,
        ):
            execute_build(build, script, tmp_path, delay=2)
        mock_sleep.assert_called_once_with(10)

    def test_venv_python_used_when_available(self, tmp_path):
        build = {
            "id": "venv-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=0, stderr="")
        venv_python = tmp_path / "bin" / "python3"
        venv_python.parent.mkdir()
        venv_python.touch()

        captured_calls = []

        def fake_run(cmd, **kwargs):
            captured_calls.append(cmd)
            return mock_result

        with (
            patch("fluid_build.build_runners.python.runner.cprint"),
            patch("fluid_build.build_runners.python.runner.success"),
            patch("fluid_build.build_runners.python.runner.subprocess.run", side_effect=fake_run),
            patch.dict("os.environ", {"VIRTUAL_ENV": str(tmp_path)}),
        ):
            execute_build(build, script, tmp_path, delay=0)

        assert captured_calls[0][0] == str(venv_python)

    def test_no_output_stderr_shown_on_failure(self, tmp_path):
        build = {
            "id": "noout-build",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        script = self._make_script(tmp_path)
        mock_result = Mock(returncode=1, stderr="some error text")
        printed = []

        with (
            patch(
                "fluid_build.build_runners.python.runner.cprint",
                side_effect=lambda m: printed.append(m),
            ),
            patch("fluid_build.build_runners.python.runner.console_error"),
            patch(
                "fluid_build.build_runners.python.runner.subprocess.run", return_value=mock_result
            ),
        ):
            execute_build(build, script, tmp_path, delay=0, no_output=True)

        assert any("some error text" in str(p) for p in printed)


class TestExecuteDbtBuild:
    def _make_project(self, tmp_path):
        project_dir = tmp_path / "dbt_project"
        project_dir.mkdir()
        (project_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")
        return project_dir

    def test_dry_run_returns_zero(self, tmp_path):
        build = {"id": "dbt-build", "engine": "dbt", "execution": {"trigger": {"type": "manual"}}}
        project_dir = self._make_project(tmp_path)

        with (
            patch("fluid_build.build_runners.dbt.runner.cprint"),
            patch(
                "fluid_build.build_runners.dbt.runner.build_dbt_command",
                return_value=["dbt", "build", "--project-dir", str(project_dir)],
            ),
        ):
            result = execute_dbt_build(build, project_dir, tmp_path, dry_run=True)

        assert result == 0

    def test_successful_run_returns_zero(self, tmp_path):
        build = {"id": "dbt-build", "engine": "dbt", "execution": {"trigger": {"type": "manual"}}}
        project_dir = self._make_project(tmp_path)
        mock_result = Mock(returncode=0, stdout="", stderr="")

        with (
            patch("fluid_build.build_runners.dbt.runner.cprint"),
            patch("fluid_build.build_runners.dbt.runner.success"),
            patch(
                "fluid_build.build_runners.dbt.runner.build_dbt_command",
                return_value=["dbt", "build", "--project-dir", str(project_dir)],
            ),
            patch("fluid_build.build_runners.dbt.runner.subprocess.run", return_value=mock_result),
        ):
            result = execute_dbt_build(build, project_dir, tmp_path, delay=0)

        assert result == 0

    def test_schedule_force_run_executes_once(self, tmp_path):
        build = {
            "id": "dbt-build",
            "engine": "dbt",
            "execution": {"trigger": {"type": "schedule", "cron": "0 6 * * *"}},
        }
        project_dir = self._make_project(tmp_path)
        mock_result = Mock(returncode=0, stdout="", stderr="")

        with (
            patch("fluid_build.build_runners.dbt.runner.cprint"),
            patch("fluid_build.build_runners.dbt.runner.success"),
            patch(
                "fluid_build.build_runners.dbt.runner.build_dbt_command",
                return_value=["dbt", "build", "--project-dir", str(project_dir)],
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.subprocess.run", return_value=mock_result
            ) as mock_run,
        ):
            result = execute_dbt_build(build, project_dir, tmp_path, delay=0, force_run=True)

        assert result == 0
        mock_run.assert_called_once()


# ── run ───────────────────────────────────────────────────────────────


class TestRun:
    def _args(self, **kwargs):
        defaults = {
            "contract": "/nonexistent/contract.yaml",
            "build_id": None,
            "dry_run": False,
            "delay": 2,
            "no_output": False,
            "fail_fast": False,
            "env": None,
        }
        defaults.update(kwargs)
        ns = argparse.Namespace(**defaults)
        return ns

    def test_missing_contract_raises_cli_error(self, tmp_path):
        args = self._args(contract=str(tmp_path / "missing.yaml"))
        logger = logging.getLogger("test")
        with pytest.raises(CLIError) as exc_info:
            run(args, logger)
        assert exc_info.value.event == "contract_not_found"

    def test_empty_builds_returns_zero(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": []},
            ),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_build_id_filter_not_found_returns_one(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file), build_id="nonexistent")
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [{"id": "other-build"}]},
            ),
            patch("fluid_build.build_runners.base.cprint"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_contract_load_exception_raises_cli_error(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with patch(
            "fluid_build.build_runners.base.load_contract_with_overlay",
            side_effect=ValueError("bad yaml"),
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value.event == "contract_load_failed"

    def test_script_not_found_skipped(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "repository": "repo", "properties": {"model": "missing"}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_all_builds_succeed_returns_zero(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "execution": {"trigger": {"type": "manual"}}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch(
                "fluid_build.build_runners.python.runner.resolve_script_path",
                return_value=script_path,
            ),
            patch("fluid_build.build_runners.python.runner.execute_build", return_value=0),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_run_passes_force_run_to_dbt_builds_from_apply(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {
            "id": "dbt-build",
            "engine": "dbt",
            "repository": "dbt_repo",
            "execution": {"trigger": {"type": "schedule"}},
        }
        project_dir = tmp_path / "dbt_repo"
        project_dir.mkdir()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.resolve_dbt_project_path",
                return_value=project_dir,
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.execute_dbt_build", return_value=0
            ) as mock_execute_dbt,
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger, force_run=True)

        assert result == 0
        assert mock_execute_dbt.call_args.kwargs["force_run"] is True

    def test_run_passes_force_run_to_python_builds_from_apply(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {
            "id": "py-build",
            "repository": "./",
            "execution": {"trigger": {"type": "schedule"}},
        }
        script_path = tmp_path / "ingest.py"
        script_path.touch()
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch(
                "fluid_build.build_runners.python.runner.resolve_script_path",
                return_value=script_path,
            ),
            patch(
                "fluid_build.build_runners.python.runner.execute_build", return_value=0
            ) as mock_execute,
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger, force_run=True)

        assert result == 0
        assert mock_execute.call_args.kwargs["force_run"] is True

    def test_failed_build_returns_one(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        build = {"id": "b1", "execution": {"trigger": {"type": "manual"}}}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch(
                "fluid_build.build_runners.python.runner.resolve_script_path",
                return_value=script_path,
            ),
            patch("fluid_build.build_runners.python.runner.execute_build", return_value=1),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_fail_fast_stops_after_first_failed_build(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        builds = [
            {"id": "b1", "execution": {"trigger": {"type": "manual"}}},
            {"id": "b2", "execution": {"trigger": {"type": "manual"}}},
        ]
        args = self._args(contract=str(contract_file), fail_fast=True)
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        execute_build_calls = []

        def fake_execute_build(*a, **kw):
            execute_build_calls.append(a[0].get("id"))
            return 1

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": builds},
            ),
            patch(
                "fluid_build.build_runners.python.runner.resolve_script_path",
                return_value=script_path,
            ),
            patch(
                "fluid_build.build_runners.python.runner.execute_build",
                side_effect=fake_execute_build,
            ),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)

        assert result == 1
        assert len(execute_build_calls) == 1

    def test_build_id_filter_matches(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        builds = [
            {"id": "b1", "execution": {"trigger": {"type": "manual"}}},
            {"id": "b2", "execution": {"trigger": {"type": "manual"}}},
        ]
        args = self._args(contract=str(contract_file), build_id="b2")
        logger = logging.getLogger("test")
        script_path = tmp_path / "ingest.py"
        script_path.touch()

        executed_ids = []

        def fake_execute_build(build, *a, **kw):
            executed_ids.append(build.get("id"))
            return 0

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": builds},
            ),
            patch(
                "fluid_build.build_runners.python.runner.resolve_script_path",
                return_value=script_path,
            ),
            patch(
                "fluid_build.build_runners.python.runner.execute_build",
                side_effect=fake_execute_build,
            ),
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)

        assert result == 0
        assert executed_ids == ["b2"]

    def test_dbt_build_dispatches_to_native_executor(self, tmp_path):
        contract_file = tmp_path / "contract.yaml"
        contract_file.touch()
        project_dir = tmp_path / "dbt_project"
        project_dir.mkdir()
        (project_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")
        build = {"id": "b1", "engine": "dbt", "repository": "dbt_project"}
        args = self._args(contract=str(contract_file))
        logger = logging.getLogger("test")

        with (
            patch(
                "fluid_build.build_runners.base.load_contract_with_overlay",
                return_value={"builds": [build]},
            ),
            patch(
                "fluid_build.build_runners.dbt.runner.execute_dbt_build", return_value=0
            ) as mock_execute_dbt,
            patch("fluid_build.build_runners.base.cprint"),
            patch("fluid_build.build_runners.base.success"),
            patch("fluid_build.build_runners.base.console_error"),
        ):
            result = run(args, logger)

        assert result == 0
        assert mock_execute_dbt.call_count == 1
