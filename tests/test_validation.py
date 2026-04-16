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

"""Tests for fluid_build/validation.py — input validation framework."""

import pytest

from fluid_build.errors import ValidationError
from fluid_build.validation import (
    validate_contract_path,
    validate_directory_exists,
    validate_enum,
    validate_environment_name,
    validate_file_exists,
    validate_gcp_project_id,
    validate_identifier,
    validate_int_range,
    validate_non_empty,
    validate_positive_int,
    validate_url,
    validate_writable_path,
)

# ── Path validation ─────────────────────────────────────────────────────


class TestValidateFileExists:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = validate_file_exists(str(f))
        assert result == f.resolve()

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValidationError, match="not found"):
            validate_file_exists(str(tmp_path / "nope.txt"))

    def test_directory_instead_of_file(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(ValidationError, match="not a file"):
            validate_file_exists(str(d))

    def test_custom_description(self, tmp_path):
        with pytest.raises(ValidationError, match="Contract"):
            validate_file_exists(str(tmp_path / "nope.txt"), description="Contract")


class TestValidateDirectoryExists:
    def test_existing_dir(self, tmp_path):
        result = validate_directory_exists(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_missing_dir(self, tmp_path):
        with pytest.raises(ValidationError, match="not found"):
            validate_directory_exists(str(tmp_path / "nope"))

    def test_file_instead_of_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(ValidationError, match="not a directory"):
            validate_directory_exists(str(f))


class TestValidateWritablePath:
    def test_writable(self, tmp_path):
        result = validate_writable_path(str(tmp_path / "out.txt"))
        assert result.parent == tmp_path.resolve()

    def test_parent_not_exists(self, tmp_path):
        with pytest.raises(ValidationError, match="Parent directory"):
            validate_writable_path(str(tmp_path / "no" / "such" / "dir" / "out.txt"))


class TestValidateContractPath:
    def test_yaml(self, tmp_path):
        f = tmp_path / "contract.yaml"
        f.write_text("id: test")
        result = validate_contract_path(str(f))
        assert result == f.resolve()

    def test_yml(self, tmp_path):
        f = tmp_path / "contract.yml"
        f.write_text("id: test")
        result = validate_contract_path(str(f))
        assert result == f.resolve()

    def test_json(self, tmp_path):
        f = tmp_path / "contract.json"
        f.write_text("{}")
        result = validate_contract_path(str(f))
        assert result == f.resolve()

    def test_bad_extension(self, tmp_path):
        f = tmp_path / "contract.txt"
        f.write_text("data")
        with pytest.raises(ValidationError, match="extension"):
            validate_contract_path(str(f))


# ── String validation ───────────────────────────────────────────────────


class TestValidateNonEmpty:
    def test_valid(self):
        assert validate_non_empty("hello", field_name="name") == "hello"

    def test_strips_whitespace(self):
        assert validate_non_empty("  hi  ", field_name="name") == "hi"

    def test_empty(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_non_empty("", field_name="name")

    def test_whitespace_only(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_non_empty("   ", field_name="name")


class TestValidateIdentifier:
    @pytest.mark.parametrize("val", ["abc", "my-thing", "X_1", "aBC123"])
    def test_valid(self, val):
        assert validate_identifier(val, field_name="id") == val

    def test_no_hyphens(self):
        with pytest.raises(ValidationError):
            validate_identifier("a-b", field_name="id", allow_hyphens=False)

    def test_start_with_number(self):
        with pytest.raises(ValidationError):
            validate_identifier("1abc", field_name="id")

    def test_empty(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_identifier("", field_name="id")


class TestValidateEnum:
    def test_valid_case_insensitive(self):
        assert (
            validate_enum("Gold", field_name="layer", allowed_values=["gold", "silver"]) == "gold"
        )

    def test_valid_case_sensitive(self):
        assert (
            validate_enum(
                "Gold", field_name="layer", allowed_values=["Gold", "Silver"], case_sensitive=True
            )
            == "Gold"
        )

    def test_invalid(self):
        with pytest.raises(ValidationError, match="Invalid"):
            validate_enum("Platinum", field_name="layer", allowed_values=["Gold", "Silver"])

    def test_case_sensitive_mismatch(self):
        with pytest.raises(ValidationError):
            validate_enum("gold", field_name="layer", allowed_values=["Gold"], case_sensitive=True)


class TestValidateUrl:
    def test_valid_http(self):
        assert validate_url("http://example.com", field_name="url") == "http://example.com"

    def test_valid_https(self):
        assert (
            validate_url("https://example.com/path", field_name="url") == "https://example.com/path"
        )

    def test_require_https(self):
        with pytest.raises(ValidationError, match="HTTPS"):
            validate_url("http://example.com", field_name="url", require_https=True)

    def test_missing_protocol(self):
        with pytest.raises(ValidationError, match="protocol"):
            validate_url("example.com", field_name="url")

    def test_unsupported_protocol(self):
        with pytest.raises(ValidationError, match="unsupported"):
            validate_url("ftp://example.com", field_name="url")

    def test_missing_host(self):
        with pytest.raises(ValidationError, match="host"):
            validate_url("http://", field_name="url")


# ── Numeric validation ──────────────────────────────────────────────────


class TestValidatePositiveInt:
    def test_valid(self):
        assert validate_positive_int(5, field_name="count") == 5

    def test_zero(self):
        with pytest.raises(ValidationError, match="positive"):
            validate_positive_int(0, field_name="count")

    def test_negative(self):
        with pytest.raises(ValidationError):
            validate_positive_int(-1, field_name="count")


class TestValidateIntRange:
    def test_in_range(self):
        assert validate_int_range(5, field_name="x", min_value=1, max_value=10) == 5

    def test_below_min(self):
        with pytest.raises(ValidationError, match="too small"):
            validate_int_range(0, field_name="x", min_value=1)

    def test_above_max(self):
        with pytest.raises(ValidationError, match="too large"):
            validate_int_range(20, field_name="x", max_value=10)

    def test_no_bounds(self):
        assert validate_int_range(999, field_name="x") == 999

    def test_exact_min(self):
        assert validate_int_range(1, field_name="x", min_value=1) == 1

    def test_exact_max(self):
        assert validate_int_range(10, field_name="x", max_value=10) == 10


# ── Environment validation ──────────────────────────────────────────────


class TestValidateEnvironmentName:
    @pytest.mark.parametrize("env", ["dev", "test", "staging", "prod", "my-env"])
    def test_valid(self, env):
        assert validate_environment_name(env) == env

    def test_invalid(self):
        with pytest.raises(ValidationError):
            validate_environment_name("1bad")


class TestValidateGcpProjectId:
    def test_valid(self):
        assert validate_gcp_project_id("my-data-project-123") == "my-data-project-123"

    def test_too_short(self):
        with pytest.raises(ValidationError, match="project ID"):
            validate_gcp_project_id("ab")

    def test_uppercase(self):
        with pytest.raises(ValidationError):
            validate_gcp_project_id("My-Project")

    def test_start_with_number(self):
        with pytest.raises(ValidationError):
            validate_gcp_project_id("1project")
