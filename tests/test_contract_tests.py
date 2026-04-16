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

"""Tests for fluid_build.contract_tests — local DuckDB provider helpers."""

from fluid_build.contract_tests import (
    LocalProviderError,
    _as_list,
    _glob_all,
    json_log,
)


class TestLocalProviderError:
    def test_is_runtime_error(self):
        err = LocalProviderError("oops")
        assert isinstance(err, RuntimeError)
        assert str(err) == "oops"


class TestAsList:
    def test_string_input(self):
        assert _as_list("hello") == ["hello"]

    def test_list_input(self):
        assert _as_list(["a", "b"]) == ["a", "b"]

    def test_single_element_list(self):
        assert _as_list(["x"]) == ["x"]


class TestGlobAll:
    def test_no_matches(self, tmp_path):
        result = _glob_all([str(tmp_path / "nonexistent_*.csv")])
        assert result == []

    def test_finds_files(self, tmp_path):
        (tmp_path / "a.csv").write_text("1")
        (tmp_path / "b.csv").write_text("2")
        result = _glob_all([str(tmp_path / "*.csv")])
        assert len(result) == 2

    def test_deduplicates(self, tmp_path):
        (tmp_path / "a.csv").write_text("1")
        pattern = str(tmp_path / "a.csv")
        result = _glob_all([pattern, pattern])
        assert len(result) == 1

    def test_preserves_order(self, tmp_path):
        (tmp_path / "a.csv").write_text("1")
        (tmp_path / "b.csv").write_text("2")
        result = _glob_all([str(tmp_path / "a.csv"), str(tmp_path / "b.csv")])
        assert result[0].endswith("a.csv")
        assert result[1].endswith("b.csv")

    def test_expands_user(self, tmp_path):
        # Should not crash on tilde paths (even if they don't match)
        result = _glob_all(["~/nonexistent_pattern_12345/*.csv"])
        assert result == []


class TestJsonLog:
    def test_basic(self):
        result = json_log("test_event")
        assert "test_event" in result
        assert "message" in result

    def test_with_kwargs(self):
        result = json_log("apply_ok", idx=0, resource="my_sql")
        assert "apply_ok" in result
        assert "idx" in result
        assert "my_sql" in result
