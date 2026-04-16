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

"""Tests for fluid_build.providers.validation_provider — data classes + ABC helpers."""

from fluid_build.providers.validation_provider import (
    FieldSchema,
    ResourceSchema,
    ResourceType,
    ValidationIssue,
    ValidationProvider,
    ValidationResult,
)

# ── Enum ──


class TestResourceType:
    def test_values(self):
        assert ResourceType.TABLE.value == "table"
        assert ResourceType.VIEW.value == "view"
        assert ResourceType.DATASET.value == "dataset"


# ── FieldSchema ──


class TestFieldSchema:
    def test_default_mode(self):
        f = FieldSchema(name="col", type="STRING")
        assert f.mode == "NULLABLE"
        assert f.description is None

    def test_eq_case_insensitive(self):
        a = FieldSchema(name="Name", type="string", mode="nullable")
        b = FieldSchema(name="name", type="STRING", mode="NULLABLE")
        assert a == b

    def test_neq_different_type(self):
        a = FieldSchema(name="id", type="INTEGER")
        b = FieldSchema(name="id", type="STRING")
        assert a != b

    def test_neq_different_name(self):
        a = FieldSchema(name="a", type="STRING")
        b = FieldSchema(name="b", type="STRING")
        assert a != b

    def test_neq_non_field_schema(self):
        f = FieldSchema(name="x", type="INT")
        assert f != "not a field"


# ── ResourceSchema ──


class TestResourceSchema:
    def test_post_init_metadata_none(self):
        rs = ResourceSchema(
            resource_type=ResourceType.TABLE,
            fully_qualified_name="db.tbl",
            fields=[],
        )
        assert rs.metadata == {}

    def test_explicit_metadata(self):
        rs = ResourceSchema(
            resource_type=ResourceType.TABLE,
            fully_qualified_name="db.tbl",
            fields=[],
            metadata={"owner": "me"},
        )
        assert rs.metadata["owner"] == "me"


# ── ValidationResult ──


class TestValidationResult:
    def test_has_errors(self):
        vr = ValidationResult(
            resource_name="tbl",
            success=False,
            issues=[
                ValidationIssue("error", "missing", "bad", "x"),
                ValidationIssue("warning", "mismatch", "ok", "y"),
            ],
        )
        assert vr.has_errors is True
        assert vr.has_warnings is True

    def test_no_errors(self):
        vr = ValidationResult(
            resource_name="tbl",
            success=True,
            issues=[ValidationIssue("info", "extra", "note", "z")],
        )
        assert vr.has_errors is False
        assert vr.has_warnings is False

    def test_empty_issues(self):
        vr = ValidationResult(resource_name="tbl", success=True, issues=[])
        assert vr.has_errors is False
        assert vr.has_warnings is False


# ── ValidationProvider base methods ──
# Create a minimal concrete subclass to test ABC methods


class _DummyProvider(ValidationProvider):
    @property
    def provider_name(self):
        return "dummy"

    def validate_connection(self):
        return True

    def get_resource_schema(self, resource_spec):
        return None

    def validate_resource(self, contract_spec, actual_schema):
        return ValidationResult("x", True, [])


class TestNormalizeType:
    def test_common_aliases(self):
        p = _DummyProvider({})
        assert p.normalize_type("VARCHAR") == "STRING"
        assert p.normalize_type("TEXT") == "STRING"
        assert p.normalize_type("INT") == "INTEGER"
        assert p.normalize_type("BIGINT") == "INTEGER"
        assert p.normalize_type("BOOL") == "BOOLEAN"
        assert p.normalize_type("DOUBLE") == "FLOAT"

    def test_case_insensitive(self):
        p = _DummyProvider({})
        assert p.normalize_type("varchar") == "STRING"

    def test_unknown_passes_through(self):
        p = _DummyProvider({})
        assert p.normalize_type("STRUCT") == "STRUCT"
        assert p.normalize_type("TIMESTAMP") == "TIMESTAMP"


class TestCompareSchemas:
    def _provider(self):
        return _DummyProvider({})

    def test_identical_schemas_no_issues(self):
        p = self._provider()
        fields = [FieldSchema("id", "INTEGER"), FieldSchema("name", "STRING")]
        issues = p.compare_schemas(fields, list(fields))
        assert len(issues) == 0

    def test_missing_field(self):
        p = self._provider()
        expected = [FieldSchema("id", "INTEGER"), FieldSchema("name", "STRING")]
        actual = [FieldSchema("id", "INTEGER")]
        issues = p.compare_schemas(expected, actual)
        cats = [i.category for i in issues]
        assert "missing_field" in cats

    def test_extra_field_info(self):
        p = self._provider()
        expected = [FieldSchema("id", "INTEGER")]
        actual = [FieldSchema("id", "INTEGER"), FieldSchema("extra", "STRING")]
        issues = p.compare_schemas(expected, actual)
        extra = [i for i in issues if i.category == "extra_field"]
        assert len(extra) == 1
        assert extra[0].severity == "info"

    def test_type_mismatch(self):
        p = self._provider()
        expected = [FieldSchema("id", "STRING")]
        actual = [FieldSchema("id", "INTEGER")]
        issues = p.compare_schemas(expected, actual)
        types = [i for i in issues if i.category == "type_mismatch"]
        assert len(types) == 1
        assert types[0].severity == "error"

    def test_mode_mismatch_warning(self):
        p = self._provider()
        expected = [FieldSchema("id", "INTEGER", mode="REQUIRED")]
        actual = [FieldSchema("id", "INTEGER", mode="NULLABLE")]
        issues = p.compare_schemas(expected, actual)
        mode = [i for i in issues if i.category == "mode_mismatch"]
        assert len(mode) == 1
        assert mode[0].severity == "warning"

    def test_type_aliases_dont_mismatch(self):
        p = self._provider()
        expected = [FieldSchema("x", "VARCHAR")]
        actual = [FieldSchema("x", "STRING")]
        issues = p.compare_schemas(expected, actual)
        types = [i for i in issues if i.category == "type_mismatch"]
        assert len(types) == 0


class TestCreateResourceIdentifier:
    def _provider(self):
        return _DummyProvider({})

    def test_string_resource(self):
        p = self._provider()
        spec = {"binding": {"resource": "my_project.my_dataset.my_table"}}
        assert p.create_resource_identifier(spec) == "my_project.my_dataset.my_table"

    def test_dict_resource_dataset_table(self):
        p = self._provider()
        spec = {"binding": {"resource": {"dataset": "ds", "table": "tbl"}}}
        assert p.create_resource_identifier(spec) == "ds.tbl"

    def test_dict_resource_name_fallback(self):
        p = self._provider()
        spec = {"binding": {"resource": {"name": "my_resource"}}}
        assert p.create_resource_identifier(spec) == "my_resource"

    def test_empty_binding(self):
        p = self._provider()
        assert p.create_resource_identifier({}) == "unknown"


class TestRunQualityChecks:
    def test_default_returns_info(self):
        p = _DummyProvider({})
        issues = p.run_quality_checks({}, [])
        assert len(issues) == 1
        assert issues[0].severity == "info"
