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

"""Tests for ODCS provider type mapping, status mapping, and conversion functions."""

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers.odcs.odcs import OdcsProvider


@pytest.fixture
def provider():
    """Create an OdcsProvider instance with mocked dependencies."""
    with patch.object(OdcsProvider, "__init__", lambda self: None):
        p = OdcsProvider.__new__(OdcsProvider)
        p.logger = MagicMock()
        p.include_quality_checks = True
        return p


class TestMapStatusToODCS:
    def test_draft(self, provider):
        assert provider._map_status_to_odcs("draft") == "draft"

    def test_active(self, provider):
        assert provider._map_status_to_odcs("active") == "active"

    def test_deprecated(self, provider):
        assert provider._map_status_to_odcs("deprecated") == "deprecated"

    def test_retired(self, provider):
        assert provider._map_status_to_odcs("retired") == "retired"

    def test_development_maps_to_draft(self, provider):
        assert provider._map_status_to_odcs("development") == "draft"

    def test_unknown_defaults_to_active(self, provider):
        assert provider._map_status_to_odcs("whatever") == "active"


class TestMapStatusFromODCS:
    def test_draft(self, provider):
        assert provider._map_status_from_odcs("draft") == "draft"

    def test_active(self, provider):
        assert provider._map_status_from_odcs("active") == "active"

    def test_unknown_defaults_to_active(self, provider):
        assert provider._map_status_from_odcs("unknown_status") == "active"


class TestMapTypeToLogical:
    @pytest.mark.parametrize(
        "fluid_type,expected",
        [
            ("string", "string"),
            ("text", "string"),
            ("varchar", "string"),
            ("char", "string"),
            ("int", "integer"),
            ("integer", "integer"),
            ("bigint", "integer"),
            ("long", "integer"),
            ("float", "number"),
            ("double", "number"),
            ("decimal", "number"),
            ("numeric", "number"),
            ("bool", "boolean"),
            ("boolean", "boolean"),
            ("date", "date"),
            ("datetime", "timestamp"),
            ("timestamp", "timestamp"),
            ("time", "time"),
            ("json", "object"),
            ("object", "object"),
            ("array", "array"),
            ("binary", "string"),
            ("bytes", "string"),
        ],
    )
    def test_type_mapping(self, provider, fluid_type, expected):
        assert provider._map_type_to_logical(fluid_type) == expected

    def test_case_insensitive(self, provider):
        assert provider._map_type_to_logical("STRING") == "string"
        assert provider._map_type_to_logical("Integer") == "integer"

    def test_unknown_defaults_to_string(self, provider):
        assert provider._map_type_to_logical("custom_type") == "string"


class TestMapTypeToPhysical:
    def test_no_provider_falls_back_to_logical(self, provider):
        result = provider._map_type_to_physical("string", None)
        assert result == "string"

    @pytest.mark.parametrize(
        "fluid_type,expected",
        [
            ("string", "STRING"),
            ("int", "INT64"),
            ("float", "FLOAT64"),
            ("bool", "BOOL"),
            ("date", "DATE"),
            ("timestamp", "TIMESTAMP"),
            ("json", "JSON"),
            ("object", "STRUCT"),
            ("array", "ARRAY"),
            ("binary", "BYTES"),
        ],
    )
    def test_bigquery_types(self, provider, fluid_type, expected):
        assert provider._map_type_to_physical(fluid_type, "gcp") == expected

    def test_bigquery_alias(self, provider):
        assert provider._map_type_to_physical("string", "bigquery") == "STRING"

    @pytest.mark.parametrize(
        "fluid_type,expected",
        [
            ("string", "VARCHAR"),
            ("text", "TEXT"),
            ("int", "NUMBER"),
            ("float", "FLOAT"),
            ("double", "DOUBLE"),
            ("decimal", "DECIMAL"),
            ("bool", "BOOLEAN"),
            ("date", "DATE"),
            ("datetime", "TIMESTAMP_NTZ"),
            ("json", "VARIANT"),
            ("object", "OBJECT"),
            ("array", "ARRAY"),
            ("binary", "BINARY"),
        ],
    )
    def test_snowflake_types(self, provider, fluid_type, expected):
        assert provider._map_type_to_physical(fluid_type, "snowflake") == expected

    def test_unknown_provider_falls_back_to_logical(self, provider):
        result = provider._map_type_to_physical("string", "unknown_provider")
        assert result == "string"

    def test_case_insensitive_provider(self, provider):
        assert provider._map_type_to_physical("string", "GCP") == "STRING"


class TestExtractTeam:
    def test_no_owner(self, provider):
        assert provider._extract_team({}) is None

    def test_team_name_only(self, provider):
        fluid = {"owner": {"team": "data-team"}}
        result = provider._extract_team(fluid)
        assert result["name"] == "data-team"

    def test_owner_name_as_member(self, provider):
        fluid = {"owner": {"team": "data-team", "name": "John", "email": "john@example.com"}}
        result = provider._extract_team(fluid)
        assert result["name"] == "data-team"
        assert len(result["members"]) == 1
        assert result["members"][0]["name"] == "John"
        assert result["members"][0]["username"] == "john@example.com"

    def test_contacts_as_members(self, provider):
        fluid = {
            "owner": {
                "team": "data-team",
                "contacts": [
                    {"name": "Alice", "email": "alice@example.com", "role": "lead"},
                    {"name": "Bob", "email": "bob@example.com"},
                ],
            }
        }
        result = provider._extract_team(fluid)
        assert len(result["members"]) == 2
        assert result["members"][0]["role"] == "lead"

    def test_no_team_name_returns_none(self, provider):
        fluid = {"owner": {"email": "someone@example.com"}}
        assert provider._extract_team(fluid) is None


class TestExtractFieldQuality:
    def test_required_field(self, provider):
        field = {"name": "id", "required": True}
        quality = provider._extract_field_quality(field)
        assert quality is not None
        assert quality[0]["metric"] == "nullValues"
        assert quality[0]["mustBe"] == 0

    def test_primary_key_uniqueness(self, provider):
        field = {"name": "id", "tags": ["primary-key"]}
        quality = provider._extract_field_quality(field)
        metrics = [q["metric"] for q in quality]
        assert "duplicateValues" in metrics
        assert "nullValues" in metrics  # PK also gets not-null

    def test_primary_key_no_duplicate_null_check(self, provider):
        field = {"name": "id", "required": True, "tags": ["primary-key"]}
        quality = provider._extract_field_quality(field)
        null_checks = [q for q in quality if q.get("metric") == "nullValues"]
        # required already adds null check, PK should not duplicate
        assert len(null_checks) == 1

    def test_pattern_validation(self, provider):
        field = {"name": "email", "validations": [{"type": "pattern", "value": "^.+@.+$"}]}
        quality = provider._extract_field_quality(field)
        assert quality[0]["type"] == "text"
        assert "pattern" in quality[0]["description"]

    def test_min_max_length(self, provider):
        field = {
            "name": "code",
            "validations": [
                {"type": "min_length", "value": 3},
                {"type": "max_length", "value": 10},
            ],
        }
        quality = provider._extract_field_quality(field)
        assert len(quality) == 2

    def test_min_max_value(self, provider):
        field = {
            "name": "price",
            "validations": [
                {"type": "min_value", "value": 0},
                {"type": "max_value", "value": 1000},
            ],
        }
        quality = provider._extract_field_quality(field)
        assert len(quality) == 2

    def test_allowed_values(self, provider):
        field = {"name": "status", "validations": [{"type": "enum", "values": ["a", "b", "c"]}]}
        quality = provider._extract_field_quality(field)
        assert "one of" in quality[0]["description"]

    def test_allowed_values_truncated(self, provider):
        field = {
            "name": "code",
            "validations": [{"type": "allowed_values", "values": list(range(10))}],
        }
        quality = provider._extract_field_quality(field)
        assert "10 total" in quality[0]["description"]

    def test_dict_validations_format(self, provider):
        field = {"name": "x", "validations": {"pattern": "^[A-Z]+$", "min_length": 1}}
        quality = provider._extract_field_quality(field)
        assert len(quality) == 2

    def test_no_quality_returns_none(self, provider):
        field = {"name": "x"}
        assert provider._extract_field_quality(field) is None

    def test_custom_quality_odcs_format(self, provider):
        field = {
            "name": "x",
            "quality": [{"type": "library", "metric": "rowCount", "mustBeGreaterThan": 0}],
        }
        quality = provider._extract_field_quality(field)
        assert quality[0]["metric"] == "rowCount"

    def test_custom_quality_text_type(self, provider):
        field = {"name": "x", "quality": [{"type": "text", "description": "Must be valid"}]}
        quality = provider._extract_field_quality(field)
        assert quality[0]["type"] == "text"

    def test_unique_validation(self, provider):
        field = {"name": "email", "validations": [{"type": "unique", "value": True}]}
        quality = provider._extract_field_quality(field)
        assert quality[0]["metric"] == "duplicateValues"


class TestFluidFieldToODCSProperty:
    def test_basic_field(self, provider):
        field = {"name": "user_id", "type": "integer", "description": "User ID"}
        expose = {}
        result = provider._fluid_field_to_odcs_property(field, expose)
        assert result["name"] == "user_id"
        assert result["logicalType"] == "integer"
        assert result["description"] == "User ID"
        assert result["required"] is False

    def test_required_field(self, provider):
        field = {"name": "id", "type": "string", "required": True}
        result = provider._fluid_field_to_odcs_property(field, {})
        assert result["required"] is True

    def test_physical_type_from_binding(self, provider):
        field = {"name": "x", "type": "string"}
        expose = {"binding": {"platform": "snowflake"}}
        result = provider._fluid_field_to_odcs_property(field, expose)
        assert result["physicalType"] == "VARCHAR"

    def test_classification_and_tags(self, provider):
        field = {"name": "x", "type": "string", "classification": "PII", "tags": ["sensitive"]}
        result = provider._fluid_field_to_odcs_property(field, {})
        assert result["classification"] == "PII"
        assert result["tags"] == ["sensitive"]

    def test_quality_checks_included(self, provider):
        field = {"name": "id", "type": "int", "required": True}
        result = provider._fluid_field_to_odcs_property(field, {})
        assert "quality" in result


class TestMapProviderToServerType:
    @pytest.mark.parametrize(
        "provider_name,expected",
        [
            ("gcp", "bigquery"),
            ("bigquery", "bigquery"),
            ("snowflake", "snowflake"),
            ("aws", "s3"),
            ("s3", "s3"),
            ("redshift", "redshift"),
            ("kafka", "kafka"),
            ("local", "local"),
        ],
    )
    def test_known_providers(self, provider, provider_name, expected):
        assert provider._map_provider_to_server_type(provider_name) == expected

    def test_unknown_provider(self, provider):
        assert provider._map_provider_to_server_type("unknown") == "custom"

    def test_case_insensitive(self, provider):
        assert provider._map_provider_to_server_type("GCP") == "bigquery"


class TestMapServerTypeToProvider:
    @pytest.mark.parametrize(
        "server_type,expected",
        [
            ("bigquery", "gcp"),
            ("snowflake", "snowflake"),
            ("s3", "aws"),
            ("redshift", "aws"),
            ("postgres", "postgres"),
            ("kafka", "kafka"),
        ],
    )
    def test_known_types(self, provider, server_type, expected):
        assert provider._map_server_type_to_provider(server_type) == expected

    def test_unknown_type(self, provider):
        assert provider._map_server_type_to_provider("unknown") == "custom"


class TestMapLogicalTypeToFluid:
    @pytest.mark.parametrize(
        "logical_type,expected",
        [
            ("string", "string"),
            ("integer", "int"),
            ("long", "bigint"),
            ("float", "float"),
            ("double", "double"),
            ("decimal", "decimal"),
            ("boolean", "bool"),
            ("date", "date"),
            ("timestamp", "timestamp"),
            ("object", "object"),
            ("array", "array"),
            ("binary", "binary"),
        ],
    )
    def test_type_mapping(self, provider, logical_type, expected):
        assert provider._map_logical_type_to_fluid(logical_type) == expected

    def test_unknown_defaults_to_string(self, provider):
        assert provider._map_logical_type_to_fluid("custom") == "string"


class TestOdcsTeamToFluidOwner:
    def test_basic(self, provider):
        result = provider._odcs_team_to_fluid_owner("data-team")
        assert result["team"] == "data-team"
        assert result["name"] == "data-team"


class TestOdcsSchemaToField:
    def test_basic(self, provider):
        entry = {"name": "user_id", "logicalType": "integer"}
        result = provider._odcs_schema_to_field(entry)
        assert result["name"] == "user_id"
        assert result["type"] == "int"
        assert result["required"] is False

    def test_not_nullable(self, provider):
        entry = {"name": "id", "logicalType": "integer", "isNullable": False}
        result = provider._odcs_schema_to_field(entry)
        assert result["required"] is True

    def test_with_description_and_tags(self, provider):
        entry = {
            "name": "email",
            "logicalType": "string",
            "description": "User email",
            "classification": "PII",
            "tags": ["sensitive"],
        }
        result = provider._odcs_schema_to_field(entry)
        assert result["description"] == "User email"
        assert result["classification"] == "PII"
        assert result["tags"] == ["sensitive"]


class TestOdcsSchemaToExpose:
    def test_basic(self, provider):
        odcs = {
            "id": "test-contract",
            "version": "2.0.0",
            "description": "Test",
            "schema": [
                {"name": "id", "logicalType": "integer"},
                {"name": "name", "logicalType": "string"},
            ],
        }
        result = provider._odcs_schema_to_expose(odcs)
        assert result["id"] == "test-contract"
        assert len(result["schema"]["fields"]) == 2

    def test_no_schema(self, provider):
        result = provider._odcs_schema_to_expose({})
        assert result is None


class TestExtractLocationFromServer:
    def test_extracts_fields(self, provider):
        server = {
            "project": "my-project",
            "dataset": "my_dataset",
            "table": "users",
            "region": "us-east-1",
            "extra": "ignored",
        }
        result = provider._extract_location_from_server(server)
        assert result["project"] == "my-project"
        assert result["dataset"] == "my_dataset"
        assert "extra" not in result

    def test_empty_server(self, provider):
        result = provider._extract_location_from_server({})
        assert result == {}


class TestExtractQuality:
    def test_with_quality(self, provider):
        fluid = {"quality": {"type": "SodaCL", "specification": "checks for ..."}}
        result = provider._extract_quality(fluid)
        assert result["type"] == "SodaCL"

    def test_no_quality(self, provider):
        result = provider._extract_quality({})
        assert result is None


class TestOdcsServerToExpect:
    def test_basic(self, provider):
        server = {"type": "bigquery", "name": "upstream", "project": "p1"}
        result = provider._odcs_server_to_expect(server)
        assert result["id"] == "upstream"
        assert result["provider"] == "gcp"
        assert result["location"]["project"] == "p1"

    def test_no_type(self, provider):
        result = provider._odcs_server_to_expect({})
        assert result is None
