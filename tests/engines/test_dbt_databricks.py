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

"""dbt on Databricks: the generated profile and column types (Trello qonTQaoc).

``fluid generate`` wrote a DuckDB ``profiles.yml`` and DuckDB column types for a
dbt build on Databricks, and a contract that named Databricks only on its
binding got DuckDB too. Pinned: the profile, the types, the binding fallback,
and that the generated profile and the dbt runner's own connect the same way.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import pytest
import yaml

from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile
from fluid_build.engines.dbt import _types
from fluid_build.engines.dbt.profiles import _profile_for_platform, generate_profiles

pytestmark = [pytest.mark.unit, pytest.mark.databricks]


def _contract(runtime_platform: str = "") -> Dict[str, Any]:
    build: Dict[str, Any] = {"id": "dbt_daily", "engine": "dbt", "execution": {"runtime": {}}}
    if runtime_platform:
        build["execution"]["runtime"]["platform"] = runtime_platform
    return {
        "id": "sales.orders",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "contract": {"schema": [{"name": "amount", "type": "number"}]},
            }
        ],
        "builds": [build],
    }


def _dev(content: str) -> Dict[str, Any]:
    return list(yaml.safe_load(content).values())[0]["outputs"]["dev"]


class TestProfile:
    def test_databricks_profile_not_duckdb(self):
        dev = _profile_for_platform("databricks", "p")["p"]["outputs"]["dev"]
        assert dev["type"] == "databricks"

    def test_catalog_and_schema_come_from_the_binding(self):
        contract = _contract("databricks")
        dev = _dev(generate_profiles(contract, contract["builds"][0]))
        assert dev["catalog"] == "main" and dev["schema"] == "sales"
        assert dev["host"] == "{{ env_var('DATABRICKS_HOST') }}"
        assert dev["token"] == "{{ env_var('DATABRICKS_TOKEN') }}"

    def test_a_binding_only_contract_gets_databricks(self):
        contract = _contract()  # no builds[].execution.runtime.platform
        assert _dev(generate_profiles(contract, contract["builds"][0]))["type"] == "databricks"
        assert _types.adapter_for_build(contract["builds"][0], contract) == "databricks"

    def test_runtime_platform_still_wins(self):
        contract = _contract("snowflake")
        assert _dev(generate_profiles(contract, contract["builds"][0]))["type"] == "snowflake"

    def test_generated_and_runner_profiles_agree(self, monkeypatch):
        """Same keys and the same environment variables as the dbt runner's profile."""
        names = {
            "DATABRICKS_HOST": "h",
            "DATABRICKS_HTTP_PATH": "p",
            "DATABRICKS_TOKEN": "t",  # pragma: allowlist secret
            "DATABRICKS_CATALOG": "c",
            "DATABRICKS_SCHEMA": "s",
        }
        for name, value in names.items():
            monkeypatch.setenv(name, value)
        runner = _build_generated_dbt_profile(
            {"execution": {"runtime": {"platform": "databricks"}}}, {}
        )
        runner_dev = list(runner.values())[0]["outputs"]
        runner_dev = list(runner_dev.values())[0]
        template = _profile_for_platform("databricks", "p")["p"]["outputs"]["dev"]
        assert set(template) == set(runner_dev)
        referenced = set(re.findall(r"env_var\('([A-Z_]+)'", " ".join(map(str, template.values()))))
        assert referenced == set(names)


class TestCredentialPlaceholders:
    @pytest.mark.parametrize(
        "value",
        [
            "{{ env.DATABRICKS_TOKEN }}",
            "${DATABRICKS_TOKEN}",
            "{{ env_var('DB_PASSWORD') }}",
            # Jinja beyond the allowlist is refused whatever it reads.
            '{{ env_var("DATABRICKS_" ~ "TOKEN") }}',
            '{{ env_var ("DATABRICKS_TOKEN") }}',
            '{% set n = "DATABRICKS_TOKEN" %}{{ env_var(n) }}',
            "{# a comment #}main",
            # A placeholder in the default would become a second, unscanned env_var.
            "{{ env_var('DBX_CATALOG', '$DATABRICKS_TOKEN') }}",
        ],
    )
    def test_a_credential_never_becomes_the_catalog_or_schema(self, value):
        contract = _contract("databricks")
        contract["exposes"][0]["binding"]["location"].update(database=value, schema=value)
        dev = _dev(generate_profiles(contract, contract["builds"][0]))
        assert dev["catalog"] == "{{ env_var('DATABRICKS_CATALOG') }}"
        assert dev["schema"] == "{{ env_var('DATABRICKS_SCHEMA', 'default') }}"

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("{{ env_var('DBX_CATALOG') }}", "{{ env_var('DBX_CATALOG') }}"),
            ("{{ env_var('DBX_CATALOG', 'main') }}", "{{ env_var('DBX_CATALOG', 'main') }}"),
            ("lake_{{ env.ENV_NAME }}", "lake_{{ env_var('ENV_NAME') }}"),
        ],
    )
    def test_the_allowed_forms_pass(self, value, expected):
        contract = _contract("databricks")
        contract["exposes"][0]["binding"]["location"]["database"] = value
        assert _dev(generate_profiles(contract, contract["builds"][0]))["catalog"] == expected

    def test_an_ordinary_placeholder_still_resolves(self):
        contract = _contract("databricks")
        contract["exposes"][0]["binding"]["location"]["database"] = "{{ env.DBX_CATALOG }}"
        dev = _dev(generate_profiles(contract, contract["builds"][0]))
        assert dev["catalog"] == "{{ env_var('DBX_CATALOG') }}"


class TestTypes:
    @pytest.mark.parametrize(
        "fluid,databricks",
        [
            ("string", "string"),
            ("integer", "int"),
            ("int8", "bigint"),
            ("bigint", "bigint"),
            ("float", "double"),
            ("double", "double"),
            ("number", "decimal"),
            ("decimal(12, 2)", "decimal(12,2)"),
            ("boolean", "boolean"),
            ("timestamp", "timestamp"),
            ("datetime", "timestamp_ntz"),
            ("time", "string"),
            ("array", "array<string>"),
            ("object", "variant"),
            ("json", "variant"),
            ("bytes", "binary"),
            ("varchar(25)", "string"),
            ("geography", "string"),
        ],
    )
    def test_sql_type(self, fluid, databricks):
        assert _types.sql_type(fluid, "databricks") == databricks

    @pytest.mark.parametrize(
        "fluid,known",
        [("number", True), ("varchar(25)", True), ("json", True), ("geography", False)],
    )
    def test_known_type_tells_a_mapping_from_the_fallback(self, fluid, known):
        assert _types.known_type(fluid, "databricks") is known
        assert _types.known_type("array<int>", "databricks") is False

    def test_a_bare_number_warns_about_scale_zero(self):
        contract = _contract("databricks")
        warnings = _types.precision_warnings(contract, contract["builds"][0])
        assert warnings and "DECIMAL(10,0)" in warnings[0]
