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

"""Unit tests for forge_copilot_contract_helpers — validation, normalization, and extraction."""

import pytest

from fluid_build.cli.forge_copilot_contract_helpers import (
    KNOWN_BUILD_ENGINES,
    PROVIDER_ENGINE_COMPATIBILITY,
    TEMPLATE_ALIASES,
    extract_json_object,
    normalize_provider_name,
    normalize_template_name,
    sanitize_additional_files,
    sanitize_name,
)


class TestNormalizeTemplateName:
    def test_none_returns_starter(self):
        assert normalize_template_name(None) == "starter"

    def test_exact_match(self):
        assert normalize_template_name("analytics") == "analytics"

    def test_alias(self):
        assert normalize_template_name("etl") == "etl_pipeline"
        assert normalize_template_name("ml") == "ml_pipeline"

    def test_dash_to_underscore(self):
        assert normalize_template_name("etl-pipeline") == "etl_pipeline"
        assert normalize_template_name("ml-pipeline") == "ml_pipeline"

    def test_case_insensitive(self):
        assert normalize_template_name("ANALYTICS") == "analytics"
        assert normalize_template_name("Streaming") == "streaming"

    def test_unknown_passes_through(self):
        assert normalize_template_name("custom_thing") == "custom_thing"


class TestNormalizeProviderName:
    def test_none_returns_local(self):
        assert normalize_provider_name(None) == "local"

    def test_standard_providers(self):
        assert normalize_provider_name("gcp") == "gcp"
        assert normalize_provider_name("aws") == "aws"
        assert normalize_provider_name("snowflake") == "snowflake"
        assert normalize_provider_name("local") == "local"

    def test_case_insensitive(self):
        assert normalize_provider_name("GCP") == "gcp"
        assert normalize_provider_name("AWS") == "aws"

    def test_dash_to_underscore(self):
        assert normalize_provider_name("my-provider") == "my_provider"


class TestSanitizeName:
    def test_basic(self):
        assert sanitize_name("Hello World") == "hello-world"

    def test_special_chars(self):
        assert sanitize_name("My Project!@#$%") == "my-project"

    def test_none_fallback(self):
        assert sanitize_name(None) == "copilot-data-product"

    def test_empty_fallback(self):
        assert sanitize_name("") == "copilot-data-product"

    def test_consecutive_dashes(self):
        assert sanitize_name("a---b") == "a-b"


class TestExtractJsonObject:
    def test_plain_json(self):
        result = extract_json_object('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_markdown_fences(self):
        result = extract_json_object('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_json_with_surrounding_text(self):
        result = extract_json_object('Here is the result: {"key": "value"} done.')
        assert result == {"key": "value"}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="valid JSON"):
            extract_json_object("not json at all")

    def test_array_raises(self):
        with pytest.raises(ValueError, match="valid JSON"):
            extract_json_object("[1, 2, 3]")

    def test_nested_json(self):
        result = extract_json_object('{"a": {"b": 1}, "c": [1,2]}')
        assert result["a"]["b"] == 1
        assert result["c"] == [1, 2]


class TestSanitizeAdditionalFiles:
    def test_valid_files(self):
        result = sanitize_additional_files(
            {
                "scripts/setup.sh": "#!/bin/bash",
                "src/main.py": "print('hello')",
                "README.md": "# Readme",
            }
        )
        assert "scripts/setup.sh" in result
        assert "src/main.py" in result
        assert "README.md" in result

    def test_rejects_absolute_paths(self):
        result = sanitize_additional_files({"/etc/passwd": "bad"})
        assert result == {}

    def test_rejects_parent_traversal(self):
        result = sanitize_additional_files({"../secret.txt": "bad"})
        assert result == {}

    def test_rejects_unsafe_extensions(self):
        result = sanitize_additional_files({"malware.exe": "bad"})
        assert result == {}

    def test_non_mapping_returns_empty(self):
        assert sanitize_additional_files(None) == {}
        assert sanitize_additional_files("not a dict") == {}
        assert sanitize_additional_files([]) == {}

    def test_non_string_values_skipped(self):
        result = sanitize_additional_files({"ok.py": 123})
        assert result == {}


class TestConstants:
    def test_known_build_engines_not_empty(self):
        assert len(KNOWN_BUILD_ENGINES) > 0
        assert "sql" in KNOWN_BUILD_ENGINES
        assert "python" in KNOWN_BUILD_ENGINES

    def test_provider_engine_compatibility_has_local(self):
        assert "local" in PROVIDER_ENGINE_COMPATIBILITY
        assert "sql" in PROVIDER_ENGINE_COMPATIBILITY["local"]

    def test_template_aliases_has_common_aliases(self):
        assert TEMPLATE_ALIASES["etl"] == "etl_pipeline"
        assert TEMPLATE_ALIASES["ml"] == "ml_pipeline"
