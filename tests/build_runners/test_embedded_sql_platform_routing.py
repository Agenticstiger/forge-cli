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

"""An embedded-SQL build runs on the platform it declares.

``_execute_embedded_sql_build`` used to hardcode
``LocalProvider(project="local", region="local")``, so a build declaring

.. code-block:: yaml

    execution:
      runtime:
        platform: snowflake
        resources: {warehouse: ..., database: ..., schema: ..., role: ...}

had its whole runtime block accepted and silently discarded: the SQL ran
against an in-process DuckDB, the rows landed in a local CSV under
``runtime/out/``, and the declared warehouse was never touched — while the
run reported success.

These tests pin the routing (snowflake → warehouse, local/unset → DuckDB,
anything else → hard error rather than a silent downgrade) and the
``{{ env.X }}`` resolution that used to differ between the two documented
apply inputs (``.fluid.yaml`` left placeholders literal; ``plan.json``
resolved them).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.build_runners.base import (
    LOCAL_SQL_PLATFORMS,
    _execute_embedded_sql_build,
    embedded_sql_platform,
)

_SQL = 'CREATE OR REPLACE TABLE "{{ env.FX_DB }}"."S"."T" AS SELECT 1'


def _build(platform=None, sql=_SQL, resources=None):
    build = {
        "id": "seed_t",
        "pattern": "embedded-logic",
        "engine": "sql",
        "properties": {"sql": sql},
    }
    if platform is not None:
        runtime = {"platform": platform}
        if resources is not None:
            runtime["resources"] = resources
        build["execution"] = {"trigger": {"type": "manual"}, "runtime": runtime}
    return build


def _contract():
    return {"id": "silver.community.smoke_v1", "exposes": [], "consumes": []}


class TestEmbeddedSqlPlatform:
    def test_reads_the_declared_runtime_platform(self):
        assert embedded_sql_platform(_build("Snowflake")) == "snowflake"

    def test_absent_runtime_is_the_empty_platform(self):
        assert embedded_sql_platform(_build()) == ""
        assert "" in LOCAL_SQL_PLATFORMS


class TestSnowflakeRouting:
    def test_snowflake_platform_executes_on_snowflake_not_duckdb(self, tmp_path):
        conn = MagicMock()
        with (
            patch(
                "fluid_build.providers.snowflake.util.config.get_connection_params",
                return_value={"account": "acct", "user": "u", "warehouse": "WH"},
            ),
            patch("fluid_build.providers.snowflake.connection.SnowflakeConnection") as sf_conn_cls,
            patch("fluid_build.providers.local.local.LocalProvider") as local_provider,
        ):
            sf_conn_cls.return_value.__enter__.return_value = conn
            rc = _execute_embedded_sql_build(
                _build(
                    "snowflake",
                    resources={
                        "warehouse": "COMPUTE_WH",
                        "database": "FLUID_TEST",
                        "schema": "FIX_APPLY",
                        "role": "ACCOUNTADMIN",
                    },
                ),
                _contract(),
                Path(tmp_path),
            )

        assert rc == 0
        local_provider.assert_not_called()
        conn.executescript.assert_called_once()

    def test_declared_resources_win_over_the_ambient_environment(self, tmp_path):
        """The build's own ``execution.runtime.resources`` select the session
        context — an operator's ambient ``SNOWFLAKE_SCHEMA`` must not silently
        redirect a build that named its target."""
        with (
            patch(
                "fluid_build.providers.snowflake.util.config.get_connection_params",
                return_value={
                    "account": "acct",
                    "user": "u",
                    "warehouse": "AMBIENT_WH",
                    "database": "AMBIENT_DB",
                    "schema": "AMBIENT_SC",
                },
            ),
            patch("fluid_build.providers.snowflake.connection.SnowflakeConnection") as sf_conn_cls,
        ):
            _execute_embedded_sql_build(
                _build(
                    "snowflake",
                    resources={
                        "warehouse": "COMPUTE_WH",
                        "database": "FLUID_TEST",
                        "schema": "FIX_APPLY",
                        "role": "ACCOUNTADMIN",
                    },
                ),
                _contract(),
                Path(tmp_path),
            )

        kwargs = sf_conn_cls.call_args.kwargs
        assert kwargs["warehouse"] == "COMPUTE_WH"
        assert kwargs["database"] == "FLUID_TEST"
        assert kwargs["schema"] == "FIX_APPLY"
        assert kwargs["role"] == "ACCOUNTADMIN"

    def test_env_placeholders_are_resolved_before_the_engine_sees_the_sql(
        self, tmp_path, monkeypatch
    ):
        """The YAML apply input used to ship a literal ``{{ env.X }}`` into the
        executor while ``plan.json`` resolved it. Both must resolve."""
        monkeypatch.setenv("FX_DB", "FLUID_TEST")
        conn = MagicMock()
        with (
            patch(
                "fluid_build.providers.snowflake.util.config.get_connection_params",
                return_value={"account": "acct", "user": "u"},
            ),
            patch("fluid_build.providers.snowflake.connection.SnowflakeConnection") as sf_conn_cls,
        ):
            sf_conn_cls.return_value.__enter__.return_value = conn
            _execute_embedded_sql_build(_build("snowflake"), _contract(), Path(tmp_path))

        executed = conn.executescript.call_args.args[0]
        assert "{{ env.FX_DB }}" not in executed
        assert '"FLUID_TEST"' in executed

    def test_a_snowflake_build_without_sql_fails_rather_than_no_ops(self, tmp_path):
        build = _build("snowflake", sql="")
        with patch("fluid_build.providers.snowflake.connection.SnowflakeConnection") as sf_conn_cls:
            rc = _execute_embedded_sql_build(build, _contract(), Path(tmp_path))
        assert rc == 1
        sf_conn_cls.assert_not_called()


class TestUnsupportedPlatformRefusesToDowngrade:
    def test_unknown_platform_errors_and_never_reaches_duckdb(self, tmp_path):
        with patch("fluid_build.providers.local.local.LocalProvider") as local_provider:
            rc = _execute_embedded_sql_build(_build("databricks"), _contract(), Path(tmp_path))
        assert rc == 1
        local_provider.assert_not_called()


class TestLocalPlatformIsUnchanged:
    def test_unset_platform_still_uses_the_local_duckdb_engine(self, tmp_path):
        provider = MagicMock()
        provider._derive_actions_from_contract.return_value = [{"op": "noop"}]
        provider.apply.return_value = {"applied": 1, "failed": 0, "results": []}
        with patch("fluid_build.providers.local.local.LocalProvider", return_value=provider):
            rc = _execute_embedded_sql_build(_build(), _contract(), Path(tmp_path))
        assert rc == 0
        provider.apply.assert_called_once()

    def test_explicit_local_platform_uses_the_local_duckdb_engine(self, tmp_path):
        provider = MagicMock()
        provider._derive_actions_from_contract.return_value = [{"op": "noop"}]
        provider.apply.return_value = {"applied": 1, "failed": 0, "results": []}
        with patch("fluid_build.providers.local.local.LocalProvider", return_value=provider):
            rc = _execute_embedded_sql_build(_build("local"), _contract(), Path(tmp_path))
        assert rc == 0
        provider.apply.assert_called_once()

    def test_dry_run_never_connects(self, tmp_path):
        with patch("fluid_build.providers.snowflake.connection.SnowflakeConnection") as sf_conn_cls:
            rc = _execute_embedded_sql_build(
                _build("snowflake"), _contract(), Path(tmp_path), dry_run=True
            )
        assert rc == 0
        sf_conn_cls.assert_not_called()
