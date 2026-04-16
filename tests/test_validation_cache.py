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

"""Tests for fluid_build.providers.validation_cache"""

import time

from fluid_build.providers.validation_cache import ValidationCache
from fluid_build.providers.validation_provider import (
    FieldSchema,
    ResourceSchema,
    ResourceType,
)


class TestCacheKey:
    def test_deterministic(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        k1 = cache._get_cache_key("db.table", "gcp")
        k2 = cache._get_cache_key("db.table", "gcp")
        assert k1 == k2

    def test_different_for_different_inputs(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        k1 = cache._get_cache_key("db.a", "gcp")
        k2 = cache._get_cache_key("db.b", "gcp")
        assert k1 != k2

    def test_different_providers(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        k1 = cache._get_cache_key("db.a", "gcp")
        k2 = cache._get_cache_key("db.a", "snowflake")
        assert k1 != k2


class TestCachePath:
    def test_returns_json_in_cache_dir(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        p = cache._get_cache_path("abc123")
        assert p == tmp_path / "abc123.json"


class TestSetGetSchema:
    def _make_schema(self):
        return ResourceSchema(
            resource_type=ResourceType.TABLE,
            fully_qualified_name="project.dataset.table",
            fields=[
                FieldSchema("id", "INTEGER", "REQUIRED"),
                FieldSchema("name", "STRING", "NULLABLE", "User name"),
            ],
            row_count=100,
            size_bytes=4096,
            last_modified="2024-01-01T00:00:00Z",
            metadata={"owner": "team-a"},
        )

    def test_roundtrip(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        schema = self._make_schema()
        cache.set_schema("project.dataset.table", "gcp", schema)
        retrieved = cache.get_schema("project.dataset.table", "gcp")
        assert retrieved is not None
        assert retrieved.fully_qualified_name == "project.dataset.table"
        assert len(retrieved.fields) == 2
        assert retrieved.fields[0].name == "id"
        assert retrieved.fields[0].type == "INTEGER"
        assert retrieved.row_count == 100
        assert retrieved.metadata == {"owner": "team-a"}

    def test_get_nonexistent_returns_none(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        assert cache.get_schema("no.such.table", "gcp") is None

    def test_expired_returns_none(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=1)
        schema = self._make_schema()
        cache.set_schema("t", "gcp", schema)
        # Manually set mtime to past
        key = cache._get_cache_key("t", "gcp")
        path = cache._get_cache_path(key)
        import os

        os.utime(path, (time.time() - 100, time.time() - 100))
        assert cache.get_schema("t", "gcp") is None

    def test_corrupted_returns_none(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        key = cache._get_cache_key("t", "gcp")
        path = cache._get_cache_path(key)
        path.write_text("not json at all {{{")
        assert cache.get_schema("t", "gcp") is None


class TestInvalidate:
    def test_invalidate_removes_entry(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        schema = ResourceSchema(ResourceType.TABLE, "db.t", [])
        cache.set_schema("db.t", "gcp", schema)
        assert cache.get_schema("db.t", "gcp") is not None
        cache.invalidate("db.t", "gcp")
        assert cache.get_schema("db.t", "gcp") is None

    def test_invalidate_nonexistent_no_error(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        cache.invalidate("no.table", "gcp")  # should not raise


class TestClear:
    def test_clears_all(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        for i in range(5):
            schema = ResourceSchema(ResourceType.TABLE, f"db.t{i}", [])
            cache.set_schema(f"db.t{i}", "gcp", schema)
        assert len(list(tmp_path.glob("*.json"))) == 5
        cache.clear()
        assert len(list(tmp_path.glob("*.json"))) == 0


class TestCacheStats:
    def test_empty_cache(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        stats = cache.get_cache_stats()
        assert stats["total_entries"] == 0
        assert stats["total_size_bytes"] == 0

    def test_with_entries(self, tmp_path):
        cache = ValidationCache(cache_dir=tmp_path, ttl=3600)
        schema = ResourceSchema(ResourceType.TABLE, "db.t", [FieldSchema("id", "INT")])
        cache.set_schema("db.t", "gcp", schema)
        stats = cache.get_cache_stats()
        assert stats["total_entries"] == 1
        assert stats["total_size_bytes"] > 0
        assert stats["fresh_entries"] == 1
