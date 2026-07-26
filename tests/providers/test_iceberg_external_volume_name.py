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

"""Deterministic Snowflake EXTERNAL VOLUME naming.

``iceberg_external_volume_name`` is the contract between the dbt
``catalogs.yml`` emitter (which references the name) and the Snowflake IaC
emitter (which will create it). These tests pin the properties that contract
depends on: purity, identifier legality, the override path, and length capping.
"""

from __future__ import annotations

import pytest

from fluid_build.providers._iceberg_catalog import iceberg_external_volume_name
from fluid_build.providers._sql_safety import validate_ident

pytestmark = pytest.mark.unit


class TestDerivation:
    def test_simple_contract_id(self):
        assert (
            iceberg_external_volume_name({"id": "bronze.orders"}, {}) == "FLUID_BRONZE_ORDERS_VOL"
        )

    def test_deterministic(self):
        contract = {"id": "gold.hr.employee_360_v1"}
        assert iceberg_external_volume_name(contract, {}) == iceberg_external_volume_name(
            contract, {}
        )

    def test_dots_and_hyphens_fold_to_underscores(self):
        name = iceberg_external_volume_name({"id": "gold.hr-analytics.employee-360"}, {})
        assert name == "FLUID_GOLD_HR_ANALYTICS_EMPLOYEE_360_VOL"

    def test_result_is_always_a_legal_snowflake_identifier(self):
        for contract_id in ("bronze.orders", "9starts.with.digit", "a--b..c__d", "UPPER.lower"):
            name = iceberg_external_volume_name({"id": contract_id}, {})
            assert validate_ident(name) == name

    def test_digit_leading_id_is_saved_by_the_prefix(self):
        name = iceberg_external_volume_name({"id": "9lives.cat"}, {})
        assert name.startswith("FLUID_")
        assert validate_ident(name) == name

    def test_empty_contract_falls_back(self):
        assert iceberg_external_volume_name({}, {}) == "FLUID_PRODUCT_VOL"
        assert iceberg_external_volume_name(None, None) == "FLUID_PRODUCT_VOL"

    def test_long_ids_are_capped_with_a_stable_digest(self):
        long_id = "domain." + ".".join(f"segment{i}" for i in range(60))
        first = iceberg_external_volume_name({"id": long_id}, {})
        second = iceberg_external_volume_name({"id": long_id}, {})
        assert first == second
        # Snowflake identifiers cap at 255; the derivation stays well inside.
        assert len(first) <= 255
        assert validate_ident(first) == first

    def test_distinct_long_ids_do_not_collide(self):
        base = "domain." + ".".join(f"segment{i}" for i in range(60))
        a = iceberg_external_volume_name({"id": base + ".alpha"}, {})
        b = iceberg_external_volume_name({"id": base + ".beta"}, {})
        assert a != b


class TestOverride:
    def test_explicit_override_wins(self):
        binding = {"icebergConfig": {"properties": {"external_volume": "MY_VOLUME"}}}
        assert iceberg_external_volume_name({"id": "bronze.orders"}, binding) == "MY_VOLUME"

    def test_camel_case_override_also_accepted(self):
        binding = {"icebergConfig": {"properties": {"externalVolume": "MY_VOLUME"}}}
        assert iceberg_external_volume_name({"id": "bronze.orders"}, binding) == "MY_VOLUME"

    def test_illegal_override_raises_instead_of_passing_through(self):
        """The result is interpolated into CREATE EXTERNAL VOLUME DDL, so an
        override that fails identifier validation must raise, never flow on."""
        binding = {"icebergConfig": {"properties": {"external_volume": "bad-name; DROP TABLE x"}}}
        # ValueError specifically: that is what validate_ident raises. A bare
        # Exception here would stay green even if a refactor crashed before
        # ever reaching the identifier gate.
        with pytest.raises(ValueError):
            iceberg_external_volume_name({"id": "bronze.orders"}, binding)

    def test_non_mapping_properties_are_ignored(self):
        binding = {"icebergConfig": {"properties": "not-a-dict"}}
        assert (
            iceberg_external_volume_name({"id": "bronze.orders"}, binding)
            == "FLUID_BRONZE_ORDERS_VOL"
        )
