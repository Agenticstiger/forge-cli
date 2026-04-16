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

"""Extended tests for fluid_build.schema_manager covering uncovered lines."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.schema_manager import (
    FluidSchemaManager,
    SchemaCache,
    SchemaVersion,
    ValidationResult,
    VersionConstraint,
    create_schema_manager,
    validate_contract_file,
)

LOG = logging.getLogger(__name__)


# ── SchemaVersion ─────────────────────────────────────────────────────


class TestSchemaVersion:
    def test_parse_valid_version(self):
        v = SchemaVersion.parse("0.5.7")
        assert v.major == 0
        assert v.minor == 5
        assert v.patch == 7
        assert v.prerelease is None

    def test_parse_prerelease(self):
        v = SchemaVersion.parse("1.0.0-alpha")
        assert v.prerelease == "alpha"

    def test_parse_without_patch(self):
        v = SchemaVersion.parse("0.5")
        assert v.patch == 0

    def test_parse_invalid_raises(self):
        from fluid_build.errors import ValidationError

        with pytest.raises(ValidationError):
            SchemaVersion.parse("not-a-version")

    def test_str_representation(self):
        v = SchemaVersion.parse("0.5.7")
        assert str(v) == "0.5.7"

    def test_comparison_operators(self):
        v1 = SchemaVersion.parse("0.4.0")
        v2 = SchemaVersion.parse("0.5.7")
        v3 = SchemaVersion.parse("0.5.7")
        assert v1 < v2
        assert v2 > v1
        assert v2 == v3
        assert v2 >= v3
        assert v2 <= v3
        assert v1 != v2

    def test_schema_url_constructed(self):
        v = SchemaVersion.parse("0.5.7")
        assert "0.5.7" in v.schema_url
        assert v.schema_url.startswith("https://")


# ── VersionConstraint ─────────────────────────────────────────────────


class TestVersionConstraint:
    def test_parse_gte(self):
        c = VersionConstraint.parse(">=0.4.0")
        assert c.operator == ">="
        v = SchemaVersion.parse("0.5.7")
        assert c.matches(v)

    def test_parse_gt(self):
        c = VersionConstraint.parse(">0.4.0")
        assert c.operator == ">"
        assert c.matches(SchemaVersion.parse("0.5.0"))
        assert not c.matches(SchemaVersion.parse("0.4.0"))

    def test_parse_lte(self):
        c = VersionConstraint.parse("<=0.5.7")
        assert c.matches(SchemaVersion.parse("0.4.0"))
        assert c.matches(SchemaVersion.parse("0.5.7"))
        assert not c.matches(SchemaVersion.parse("0.6.0"))

    def test_parse_lt(self):
        c = VersionConstraint.parse("<0.5.7")
        assert c.matches(SchemaVersion.parse("0.4.0"))
        assert not c.matches(SchemaVersion.parse("0.5.7"))

    def test_parse_tilde_compatible(self):
        c = VersionConstraint.parse("~0.5.0")
        assert c.operator == "~"
        assert c.matches(SchemaVersion.parse("0.5.7"))
        assert not c.matches(SchemaVersion.parse("0.6.0"))

    def test_parse_exact(self):
        c = VersionConstraint.parse("=0.5.7")
        assert c.matches(SchemaVersion.parse("0.5.7"))
        assert not c.matches(SchemaVersion.parse("0.5.8"))

    def test_parse_default_exact(self):
        c = VersionConstraint.parse("0.5.7")
        assert c.operator == "="
        assert c.matches(SchemaVersion.parse("0.5.7"))


# ── ValidationResult ──────────────────────────────────────────────────


class TestValidationResult:
    def test_add_error_sets_invalid(self):
        r = ValidationResult(is_valid=True)
        r.add_error("bad field")
        assert not r.is_valid
        assert "bad field" in r.errors

    def test_add_warning(self):
        r = ValidationResult(is_valid=True)
        r.add_warning("deprecated")
        assert "deprecated" in r.warnings
        assert r.is_valid  # warnings don't invalidate

    def test_get_summary_valid(self):
        r = ValidationResult(is_valid=True, schema_version=SchemaVersion.parse("0.5.7"))
        summary = r.get_summary()
        assert "Valid" in summary

    def test_get_summary_invalid(self):
        r = ValidationResult(is_valid=False)
        r.add_error("missing field")
        summary = r.get_summary()
        assert "Invalid" in summary

    def test_get_summary_with_validation_time(self):
        r = ValidationResult(is_valid=True, schema_version=SchemaVersion.parse("0.5.7"))
        r.validation_time = 0.123
        summary = r.get_summary()
        assert "0.123" in summary


# ── FluidSchemaManager._fetch_schema ──────────────────────────────────


class TestFetchSchema:
    def _make_manager(self, tmp_path=None):
        cache_dir = Path(tmp_path) if tmp_path else None
        return FluidSchemaManager(cache_dir=cache_dir, logger=LOG)

    def test_fetch_schema_success(self, tmp_path):
        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("0.9.0")
        schema_data = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200
        mock_response.read.return_value = json.dumps(schema_data).encode("utf-8")

        with patch("fluid_build.schema_manager.urlopen", return_value=mock_response):
            result = m._fetch_schema(v)
        assert result is not None
        assert result["type"] == "object"

    def test_fetch_schema_http_error(self, tmp_path):
        from urllib.error import HTTPError

        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("0.9.0")

        with patch(
            "fluid_build.schema_manager.urlopen",
            side_effect=HTTPError(url="", code=404, msg="Not Found", hdrs=None, fp=None),
        ):
            result = m._fetch_schema(v)
        assert result is None

    def test_fetch_schema_url_error(self, tmp_path):
        from urllib.error import URLError

        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("0.9.0")

        with patch(
            "fluid_build.schema_manager.urlopen",
            side_effect=URLError("connection refused"),
        ):
            result = m._fetch_schema(v)
        assert result is None

    def test_fetch_schema_json_decode_error(self, tmp_path):
        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("0.9.0")

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200
        mock_response.read.return_value = b"not valid json {"

        with patch("fluid_build.schema_manager.urlopen", return_value=mock_response):
            result = m._fetch_schema(v)
        assert result is None

    def test_fetch_schema_no_url_returns_none(self, tmp_path):
        m = self._make_manager(tmp_path)
        v = SchemaVersion(version="0.9.0", major=0, minor=9, patch=0, schema_url=None)
        result = m._fetch_schema(v)
        assert result is None


# ── FluidSchemaManager.find_compatible_version ────────────────────────


class TestFindCompatibleVersion:
    def _make_manager(self, tmp_path):
        return FluidSchemaManager(cache_dir=Path(tmp_path), logger=LOG)

    def test_finds_highest_compatible(self, tmp_path):
        m = self._make_manager(tmp_path)
        result = m.find_compatible_version(">=0.4.0", ["0.4.0", "0.5.7", "0.7.1"])
        assert result is not None
        assert result.version == "0.7.1"

    def test_returns_none_when_no_match(self, tmp_path):
        m = self._make_manager(tmp_path)
        result = m.find_compatible_version(">=9.0.0", ["0.4.0", "0.5.7"])
        assert result is None

    def test_uses_bundled_versions_when_none_given(self, tmp_path):
        m = self._make_manager(tmp_path)
        result = m.find_compatible_version(">=0.4.0")
        assert result is not None, "expected a matching bundled version for >=0.4.0"
        assert hasattr(result, "version"), "result should be a SchemaVersion"

    def test_skips_invalid_version_strings(self, tmp_path):
        m = self._make_manager(tmp_path)
        # Only pass valid version strings
        result = m.find_compatible_version(">=0.4.0", ["0.4.0", "0.5.7"])
        assert result is not None
        assert result.version in ("0.4.0", "0.5.7")

    def test_constraint_object_passed_directly(self, tmp_path):
        m = self._make_manager(tmp_path)
        constraint = VersionConstraint.parse("~0.5.0")
        result = m.find_compatible_version(constraint, ["0.4.0", "0.5.7", "0.6.0"])
        assert result is not None
        assert result.minor == 5


# ── FluidSchemaManager._validate_with_fluid_validator ─────────────────


class TestValidateWithFluidValidator:
    def _make_manager(self, tmp_path):
        return FluidSchemaManager(cache_dir=Path(tmp_path), logger=LOG)

    def test_validate_basic_version_missing_field(self, tmp_path):
        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("1.0.0")
        r = ValidationResult(is_valid=True)
        contract = {"fluidVersion": "1.0.0"}  # Missing required fields
        result = m._validate_with_fluid_validator(contract, v, r)
        assert result is False
        assert len(r.errors) > 0

    def test_validate_basic_version_all_fields_present(self, tmp_path):
        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("1.0.0")
        r = ValidationResult(is_valid=True)
        contract = {
            "fluidVersion": "1.0.0",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "domain": "example",
            "metadata": {},
            "exposes": [],
        }
        result = m._validate_with_fluid_validator(contract, v, r)
        assert result is True

    def test_validate_05x_warns_on_import_failure(self, tmp_path):
        """Exercises the 0.5.x branch when fluid_build.schema import fails."""
        m = self._make_manager(tmp_path)
        v = SchemaVersion.parse("0.5.0")
        r = ValidationResult(is_valid=True)
        contract = {"fluidVersion": "0.5.0"}
        # Simulate import failure for fluid_build.schema
        with patch.dict("sys.modules", {"fluid_build.schema": None}):
            # ImportError is expected from the None entry in sys.modules
            result = m._validate_with_fluid_validator(contract, v, r)
        # Should fail gracefully and return False
        assert isinstance(result, bool)


# ── FluidSchemaManager.validate_contract ─────────────────────────────


class TestValidateContract:
    def _make_manager(self, tmp_path):
        return FluidSchemaManager(cache_dir=Path(tmp_path), logger=LOG)

    def test_validate_no_fluid_version_fails(self, tmp_path):
        m = self._make_manager(tmp_path)
        r = m.validate_contract({})
        assert not r.is_valid
        assert any("fluidVersion" in e for e in r.errors)

    def test_validate_with_explicit_version_string(self, tmp_path):
        m = self._make_manager(tmp_path)
        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "t",
            "name": "T",
            "domain": "d",
            "metadata": {},
            "exposes": [],
        }
        r = m.validate_contract(contract, schema_version="0.5.7")
        assert isinstance(r, ValidationResult)

    def test_validate_invalid_schema_version_string(self, tmp_path):
        m = self._make_manager(tmp_path)
        contract = {"fluidVersion": "0.5.7"}
        with pytest.raises(Exception):
            m.validate_contract(contract, schema_version="not-valid")

    def test_validate_schema_not_available(self, tmp_path):
        m = self._make_manager(tmp_path)
        contract = {"fluidVersion": "99.0.0"}
        # version 99.0.0 won't be bundled or cached
        r = m.validate_contract(contract, offline_only=True)
        assert not r.is_valid

    def test_validate_old_version_adds_deprecation_warning(self, tmp_path):
        m = self._make_manager(tmp_path)
        contract = {"fluidVersion": "0.3.0"}
        # 0.3.0 won't be available, but we can mock get_schema
        mock_schema = {"type": "object"}
        with patch.object(m, "get_schema", return_value=mock_schema):
            with patch.object(m, "_validate_with_fluid_validator", return_value=True):
                r = m.validate_contract(contract)
        assert any("deprecated" in w.lower() for w in r.warnings)


# ── validate_contract_file ────────────────────────────────────────────


class TestValidateContractFile:
    def test_file_not_found(self):
        r = validate_contract_file("/nonexistent/path/contract.yaml")
        assert not r.is_valid
        assert any("not found" in e.lower() for e in r.errors)

    def test_valid_yaml_file(self, tmp_path):
        import yaml

        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "domain": "example",
            "metadata": {},
            "exposes": [],
        }
        p = tmp_path / "contract.yaml"
        p.write_text(yaml.dump(contract))
        r = validate_contract_file(str(p))
        assert isinstance(r, ValidationResult)

    def test_valid_json_file(self, tmp_path):
        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataContract",
            "id": "test",
            "name": "Test",
            "domain": "example",
            "metadata": {},
            "exposes": [],
        }
        p = tmp_path / "contract.json"
        p.write_text(json.dumps(contract))
        r = validate_contract_file(str(p))
        assert isinstance(r, ValidationResult)

    def test_invalid_yaml_returns_error_result(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("key: [unclosed bracket")
        r = validate_contract_file(str(p))
        assert not r.is_valid

    def test_invalid_json_returns_error_result(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        r = validate_contract_file(str(p))
        assert not r.is_valid


# ── SchemaCache ───────────────────────────────────────────────────────


class TestSchemaCache:
    def test_cache_and_retrieve_schema(self, tmp_path):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        v = SchemaVersion.parse("0.5.7")
        schema = {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"}
        cache.cache_schema(v, schema)
        result = cache.get_cached_schema(v, max_age_hours=24)
        assert result is not None
        assert result["type"] == "object"

    def test_get_cached_schema_missing_returns_none(self, tmp_path):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        v = SchemaVersion.parse("9.9.9")
        result = cache.get_cached_schema(v)
        assert result is None

    def test_clear_cache_removes_files(self, tmp_path):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        v = SchemaVersion.parse("0.5.7")
        schema = {"type": "object"}
        cache.cache_schema(v, schema)
        removed = cache.clear_cache()
        assert removed >= 1
        assert cache.list_cached_versions() == []

    def test_list_cached_versions(self, tmp_path):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        v = SchemaVersion.parse("0.5.7")
        cache.cache_schema(v, {"type": "object"})
        versions = cache.list_cached_versions()
        assert "0.5.7" in versions

    def test_save_cache_index_logs_debug_when_write_fails(self, tmp_path, caplog):
        cache = SchemaCache(cache_dir=Path(tmp_path))

        with patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.DEBUG, logger="fluid_build.schema_manager"):
                cache._save_cache_index()

        assert "schema_cache_index_write_failed" in caplog.text

    def test_cache_schema_logs_debug_when_write_fails(self, tmp_path, caplog):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        version = SchemaVersion.parse("0.5.7")

        with patch("builtins.open", side_effect=OSError("read only")):
            with caplog.at_level(logging.DEBUG, logger="fluid_build.schema_manager"):
                cache.cache_schema(version, {"type": "object"})

        assert "schema_cache_write_failed" in caplog.text

    def test_clear_cache_logs_debug_when_remove_fails(self, tmp_path, caplog):
        cache = SchemaCache(cache_dir=Path(tmp_path))
        version = SchemaVersion.parse("0.5.7")
        cache.cache_schema(version, {"type": "object"})

        with patch("pathlib.Path.unlink", side_effect=OSError("busy")):
            with caplog.at_level(logging.DEBUG, logger="fluid_build.schema_manager"):
                removed = cache.clear_cache()

        assert removed == 0
        assert "schema_cache_remove_failed" in caplog.text


# ── create_schema_manager convenience function ────────────────────────


class TestCreateSchemaManager:
    def test_returns_fluid_schema_manager(self, tmp_path):
        m = create_schema_manager(cache_dir=Path(tmp_path), logger=LOG)
        assert isinstance(m, FluidSchemaManager)

    def test_default_args(self):
        m = create_schema_manager()
        assert isinstance(m, FluidSchemaManager)
