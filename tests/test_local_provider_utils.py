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

"""Tests for fluid_build.providers.local.local — utility functions and provider basics."""

from pathlib import Path

import pytest

from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.local.local import (
    RESERVED_LOG_KEYS,
    _ext,
    _guess_table_name_from_path,
    _has_glob,
    _mkdir,
    _safe_extra,
)


class TestSafeExtra:
    def test_none_input(self):
        assert _safe_extra(None) == {"ctx": {}}

    def test_empty_dict(self):
        assert _safe_extra({}) == {"ctx": {}}

    def test_safe_keys_pass_through(self):
        r = _safe_extra({"my_key": "val"})
        assert r == {"ctx": {"my_key": "val"}}

    def test_reserved_keys_nested(self):
        r = _safe_extra({"name": "bad", "custom": "ok"})
        # Should nest under ctx since 'name' is reserved
        assert "ctx" in r
        assert r["ctx"]["name"] == "bad"


class TestValidateIdent:
    def test_valid_identifiers(self):
        assert validate_ident("my_table") == "my_table"
        assert validate_ident("_private") == "_private"
        assert validate_ident("Table123") == "Table123"

    def test_invalid_identifiers(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_ident("123start")
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_ident("has space")
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_ident("drop;table")
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_ident("")


class TestExt:
    def test_common_extensions(self):
        assert _ext(Path("data.csv")) == "csv"
        assert _ext(Path("data.JSON")) == "json"
        assert _ext(Path("data.PARQUET")) == "parquet"

    def test_no_extension(self):
        assert _ext(Path("noext")) == ""

    def test_double_extension(self):
        assert _ext(Path("data.tar.gz")) == "gz"


class TestHasGlob:
    def test_no_glob(self):
        assert _has_glob(Path("/data/file.csv")) is False

    def test_star(self):
        assert _has_glob(Path("/data/*.csv")) is True

    def test_question(self):
        assert _has_glob(Path("/data/file?.csv")) is True

    def test_bracket(self):
        assert _has_glob(Path("/data/file[0-9].csv")) is True


class TestGuessTableName:
    def test_simple_name(self):
        assert _guess_table_name_from_path(Path("sales_data.csv")) == "sales_data"

    def test_special_chars(self):
        result = _guess_table_name_from_path(Path("my-data (2023).csv"))
        assert "_" in result  # special chars replaced
        assert "-" not in result

    def test_empty_stem(self):
        # Edge case: hidden file like .csv
        result = _guess_table_name_from_path(Path(".csv"))
        # Should return "t" as fallback or the cleaned stem
        assert isinstance(result, str)
        assert len(result) > 0


class TestMkdir:
    def test_creates_nested(self, tmp_path):
        p = tmp_path / "a" / "b" / "c"
        result = _mkdir(p)
        assert result.is_dir()
        assert result == p


class TestReservedLogKeys:
    def test_contains_standard_keys(self):
        assert "name" in RESERVED_LOG_KEYS
        assert "message" in RESERVED_LOG_KEYS
        assert "levelname" in RESERVED_LOG_KEYS
        assert "lineno" in RESERVED_LOG_KEYS
