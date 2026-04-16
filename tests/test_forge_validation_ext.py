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

"""Tests for fluid_build.cli.forge_validation — all validation functions."""

import pytest

from fluid_build.cli.forge_validation import (
    sanitize_project_name,
    suggest_fixes,
    validate_context_dict,
    validate_directory_path,
    validate_project_name,
    validate_provider,
    validate_template_name,
)

# ── validate_project_name ──


class TestValidateProjectName:
    def test_valid(self):
        ok, err = validate_project_name("my-project")
        assert ok is True and err is None

    def test_empty(self):
        ok, err = validate_project_name("")
        assert ok is False

    def test_too_short(self):
        ok, err = validate_project_name("ab")
        assert ok is False

    def test_too_long(self):
        ok, err = validate_project_name("a" * 64)
        assert ok is False

    def test_starts_with_number(self):
        ok, err = validate_project_name("1bad")
        assert ok is False

    def test_consecutive_hyphens(self):
        ok, err = validate_project_name("my--project")
        assert ok is False

    def test_reserved_name(self):
        ok, err = validate_project_name("test")
        assert ok is False
        assert "reserved" in err

    def test_uppercase(self):
        ok, err = validate_project_name("My-Project")
        assert ok is False

    def test_ends_with_hyphen(self):
        ok, err = validate_project_name("my-project-")
        assert ok is False


# ── sanitize_project_name ──


class TestSanitizeProjectName:
    def test_empty_nonstrict(self):
        assert sanitize_project_name("") == "my-data-product"

    def test_empty_strict(self):
        with pytest.raises(ValueError):
            sanitize_project_name("", strict=True)

    def test_basic_sanitize(self):
        result = sanitize_project_name("My Cool Project!")
        assert result.islower()
        assert "!" not in result
        assert " " not in result

    def test_leading_number(self):
        result = sanitize_project_name("123-thing")
        assert result[0].isalpha()

    def test_special_chars_removed(self):
        result = sanitize_project_name("hello@world#test")
        assert "@" not in result
        assert "#" not in result

    def test_truncation(self):
        result = sanitize_project_name("a" * 100)
        assert len(result) <= 63

    def test_underscores_to_hyphens(self):
        result = sanitize_project_name("my_cool_project")
        assert "_" not in result
        assert "-" in result


# ── validate_provider ──


class TestValidateProvider:
    def test_valid_providers(self):
        for p in ["local", "gcp", "aws", "azure", "snowflake", "databricks"]:
            ok, err = validate_provider(p)
            assert ok is True, f"Failed for {p}"

    def test_empty(self):
        ok, err = validate_provider("")
        assert ok is False

    def test_invalid(self):
        ok, err = validate_provider("oracle")
        assert ok is False


# ── validate_template_name ──


class TestValidateTemplateName:
    def test_valid(self):
        ok, err = validate_template_name("analytics")
        assert ok is True

    def test_with_hyphens(self):
        ok, err = validate_template_name("ml-pipeline")
        assert ok is True

    def test_empty(self):
        ok, err = validate_template_name("")
        assert ok is False

    def test_too_short(self):
        ok, err = validate_template_name("a")
        assert ok is False

    def test_starts_with_number(self):
        ok, err = validate_template_name("1analytics")
        assert ok is False

    def test_uppercase(self):
        ok, err = validate_template_name("Analytics")
        assert ok is False


# ── validate_directory_path ──


class TestValidateDirectoryPath:
    def test_relative(self):
        ok, err = validate_directory_path("./output")
        assert ok is True

    def test_absolute(self, tmp_path):
        ok, err = validate_directory_path(str(tmp_path / "new-project"))
        assert ok is True

    def test_empty(self):
        ok, err = validate_directory_path("")
        assert ok is False


# ── validate_context_dict ──


class TestValidateContextDict:
    def test_valid(self):
        ok, err = validate_context_dict({"project_goal": "Build a dashboard"})
        assert ok is True

    def test_missing_required(self):
        ok, err = validate_context_dict({})
        assert ok is False
        assert "project_goal" in err

    def test_not_a_dict(self):
        ok, err = validate_context_dict("not a dict")
        assert ok is False

    def test_short_goal(self):
        ok, err = validate_context_dict({"project_goal": "hi"})
        assert ok is False

    def test_invalid_use_case(self):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build something",
                "use_case": "invalid_case",
            }
        )
        assert ok is False

    @pytest.mark.parametrize(
        "use_case",
        [
            "analytics",
            "etl_pipeline",
            "streaming",
            "ml_pipeline",
            "data_platform",
            "other",
        ],
    )
    def test_valid_canonical_use_cases(self, use_case):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build something",
                "use_case": use_case,
            }
        )
        assert ok is True

    @pytest.mark.parametrize(
        "use_case",
        [
            "reporting",
            "real_time",
            "data_lake",
            "etl",
            "machine learning",
            "Analytics & BI",
            "Data Platform / Lakehouse",
        ],
    )
    def test_valid_legacy_or_friendly_use_case_aliases(self, use_case):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build something",
                "use_case": use_case,
            }
        )
        assert ok is True

    def test_invalid_complexity(self):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build something",
                "complexity": "extreme",
            }
        )
        assert ok is False

    def test_invalid_use_case_other_type(self):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build something",
                "use_case": "other",
                "use_case_other": 123,
            }
        )
        assert ok is False

    def test_valid_all_fields(self):
        ok, err = validate_context_dict(
            {
                "project_goal": "Build a data lake",
                "use_case": "analytics",
                "complexity": "intermediate",
                "data_sources": "BigQuery",
                "use_case_other": "customer 360",
                "canonical_model": "omop_cdm",
                "supporting_standards": ["gs1_gdm"],
            }
        )
        assert ok is True


# ── suggest_fixes ──


class TestSuggestFixes:
    def test_starts_with_letter(self):
        s = suggest_fixes("123abc", "must start with a letter")
        assert "abc" in s or "Start with" in s

    def test_consecutive_hyphens(self):
        s = suggest_fixes("my--project", "consecutive hyphens")
        assert "my-project" in s

    def test_too_short(self):
        s = suggest_fixes("ab", "must be at least")
        assert "longer" in s.lower()

    def test_too_long(self):
        name = "a" * 70
        s = suggest_fixes(name, "must be 63 characters or less")
        assert "..." in s or "Try" in s
