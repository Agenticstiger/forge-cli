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

"""Tests for fluid_build.schema_manager — version parsing, constraints, cache, validation."""

from datetime import datetime, timedelta

import pytest

from fluid_build.schema_manager import (
    FluidSchemaManager,
    SchemaCache,
    SchemaVersion,
    ValidationResult,
    VersionConstraint,
)


class TestSchemaVersion:
    def test_parse_standard(self):
        v = SchemaVersion.parse("0.5.7")
        assert v.major == 0
        assert v.minor == 5
        assert v.patch == 7
        assert v.prerelease is None

    def test_parse_two_part(self):
        v = SchemaVersion.parse("1.0")
        assert v.major == 1
        assert v.minor == 0
        assert v.patch == 0

    def test_parse_prerelease(self):
        v = SchemaVersion.parse("1.0.0-beta.1")
        assert v.prerelease == "beta.1"

    def test_parse_invalid_raises(self):
        from fluid_build.errors import ValidationError

        with pytest.raises(ValidationError):
            SchemaVersion.parse("not-a-version")

    def test_comparisons(self):
        v1 = SchemaVersion.parse("0.4.0")
        v2 = SchemaVersion.parse("0.5.7")
        v3 = SchemaVersion.parse("0.5.7")
        assert v1 < v2
        assert v2 > v1
        assert v2 == v3
        assert v1 <= v2
        assert v2 >= v1

    def test_not_equal_to_non_version(self):
        v = SchemaVersion.parse("1.0.0")
        assert v != "1.0.0"

    def test_str(self):
        v = SchemaVersion.parse("0.7.1")
        assert str(v) == "0.7.1"

    def test_schema_url_generation(self):
        v = SchemaVersion.parse("0.5.7")
        assert "0.5.7" in v.schema_url


class TestVersionConstraint:
    def test_parse_gte(self):
        c = VersionConstraint.parse(">=0.4.0")
        assert c.operator == ">="
        v = SchemaVersion.parse("0.5.7")
        assert c.matches(v) is True

    def test_parse_gt(self):
        c = VersionConstraint.parse(">0.4.0")
        assert c.matches(SchemaVersion.parse("0.4.0")) is False
        assert c.matches(SchemaVersion.parse("0.5.0")) is True

    def test_parse_lte(self):
        c = VersionConstraint.parse("<=0.5.7")
        assert c.matches(SchemaVersion.parse("0.5.7")) is True
        assert c.matches(SchemaVersion.parse("0.6.0")) is False

    def test_parse_lt(self):
        c = VersionConstraint.parse("<1.0.0")
        assert c.matches(SchemaVersion.parse("0.9.9")) is True
        assert c.matches(SchemaVersion.parse("1.0.0")) is False

    def test_parse_tilde_compatible(self):
        c = VersionConstraint.parse("~0.5.0")
        assert c.matches(SchemaVersion.parse("0.5.0")) is True
        assert c.matches(SchemaVersion.parse("0.5.7")) is True
        assert c.matches(SchemaVersion.parse("0.6.0")) is False  # different minor

    def test_parse_exact(self):
        c = VersionConstraint.parse("=0.5.7")
        assert c.matches(SchemaVersion.parse("0.5.7")) is True
        assert c.matches(SchemaVersion.parse("0.5.8")) is False

    def test_parse_bare_version_exact(self):
        c = VersionConstraint.parse("0.5.7")
        assert c.matches(SchemaVersion.parse("0.5.7")) is True
        assert c.matches(SchemaVersion.parse("0.5.8")) is False

    def test_satisfies(self):
        v = SchemaVersion.parse("0.5.7")
        c = VersionConstraint.parse(">=0.4.0")
        assert v.satisfies(c) is True


class TestValidationResult:
    def test_valid_summary(self):
        vr = ValidationResult(is_valid=True, schema_version=SchemaVersion.parse("0.5.7"))
        summary = vr.get_summary()
        assert "✅" in summary

    def test_invalid_summary(self):
        vr = ValidationResult(is_valid=False)
        vr.add_error("Missing field: id")
        summary = vr.get_summary()
        assert "❌" in summary
        assert "1 error" in summary

    def test_add_error_toggles_validity(self):
        vr = ValidationResult(is_valid=True)
        vr.add_error("bad")
        assert vr.is_valid is False

    def test_add_warning(self):
        vr = ValidationResult(is_valid=True, schema_version=SchemaVersion.parse("1.0.0"))
        vr.add_warning("consider improving")
        assert vr.is_valid is True
        summary = vr.get_summary()
        assert "⚠️" in summary

    def test_validation_time_in_summary(self):
        vr = ValidationResult(
            is_valid=True, schema_version=SchemaVersion.parse("1.0.0"), validation_time=0.123
        )
        assert "0.123" in vr.get_summary()


class TestSchemaCache:
    def test_init_creates_dir(self, tmp_path):
        cache_dir = tmp_path / "cache"
        SchemaCache(cache_dir)
        assert cache_dir.exists()

    def test_cache_and_retrieve(self, tmp_path):
        cache = SchemaCache(tmp_path / "cache")
        v = SchemaVersion.parse("0.5.7")
        schema = {"type": "object", "properties": {"id": {"type": "string"}}}
        cache.cache_schema(v, schema)
        result = cache.get_cached_schema(v, max_age_hours=1)
        assert result == schema

    def test_cached_schema_expired(self, tmp_path):
        cache = SchemaCache(tmp_path / "cache")
        v = SchemaVersion.parse("0.5.7")
        cache.cache_schema(v, {"test": True})
        # Manually backdate the cache entry
        cache._cache_index["0.5.7"]["last_fetched"] = (
            datetime.now() - timedelta(hours=48)
        ).isoformat()
        cache._save_cache_index()
        result = cache.get_cached_schema(v, max_age_hours=24)
        assert result is None

    def test_missing_file_cleans_index(self, tmp_path):
        cache = SchemaCache(tmp_path / "cache")
        v = SchemaVersion.parse("0.5.7")
        cache.cache_schema(v, {"test": True})
        # Delete the actual file
        for f in (tmp_path / "cache").glob("*.schema.json"):
            f.unlink()
        result = cache.get_cached_schema(v)
        assert result is None
        assert "0.5.7" not in cache._cache_index

    def test_clear_cache(self, tmp_path):
        cache = SchemaCache(tmp_path / "cache")
        v = SchemaVersion.parse("0.5.7")
        cache.cache_schema(v, {"test": True})
        removed = cache.clear_cache()
        assert removed >= 1
        assert cache.list_cached_versions() == []

    def test_list_cached_versions(self, tmp_path):
        cache = SchemaCache(tmp_path / "cache")
        cache.cache_schema(SchemaVersion.parse("0.5.7"), {})
        cache.cache_schema(SchemaVersion.parse("0.7.1"), {})
        versions = cache.list_cached_versions()
        assert "0.5.7" in versions
        assert "0.7.1" in versions


class TestFluidSchemaManager:
    def test_bundled_versions(self):
        mgr = FluidSchemaManager()
        assert "0.5.7" in mgr.BUNDLED_VERSIONS
        assert "0.7.1" in mgr.BUNDLED_VERSIONS
        assert "0.7.2" in mgr.BUNDLED_VERSIONS

    def test_latest_bundled_version(self):
        assert FluidSchemaManager.latest_bundled_version() == "0.7.2"

    def test_detect_version_from_contract(self):
        mgr = FluidSchemaManager()
        v = mgr.detect_version({"fluidVersion": "0.5.7"})
        assert str(v) == "0.5.7"

    def test_detect_version_missing(self):
        mgr = FluidSchemaManager()
        v = mgr.detect_version({})
        assert v is None  # no fluidVersion key -> None

    def test_list_available_versions(self):
        mgr = FluidSchemaManager()
        versions = mgr.list_available_versions()
        assert len(versions) >= 1
        assert "0.7.2" in versions
