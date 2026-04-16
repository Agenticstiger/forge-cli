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

"""Tests for fluid_build/schema.py — FLUID contract schema validation."""

import pytest

from fluid_build.schema import (
    _assert,
    _check_build,
    _check_column,
    _check_consumes,
    _check_email,
    _check_expose,
    _check_id,
    _check_location,
    _check_metadata,
    _check_version,
    _is_list,
    _is_obj,
    _is_str,
    _opt,
    _req,
    validate_contract,
)

# ── helpers ──────────────────────────────────────────────────────────────


class TestTypeHelpers:
    def test_is_str(self):
        assert _is_str("hello")
        assert not _is_str(123)
        assert not _is_str(None)
        assert not _is_str([])

    def test_is_obj(self):
        assert _is_obj({})
        assert _is_obj({"a": 1})
        assert not _is_obj([])
        assert not _is_obj("x")

    def test_is_list(self):
        assert _is_list([])
        assert _is_list([1, 2])
        assert not _is_list({})
        assert not _is_list("x")


class TestReqOpt:
    def test_req_present(self):
        errors = []
        val = _req({"key": "val"}, "key", errors, "$")
        assert val == "val"
        assert errors == []

    def test_req_missing(self):
        errors = []
        val = _req({}, "key", errors, "$")
        assert val is None
        assert len(errors) == 1
        assert "missing required" in errors[0]

    def test_opt_present(self):
        assert _opt({"k": 42}, "k") == 42

    def test_opt_missing_default(self):
        assert _opt({}, "k", "fallback") == "fallback"
        assert _opt({}, "k") is None


class TestAssert:
    def test_pass(self):
        errors = []
        _assert(True, "should not appear", errors)
        assert errors == []

    def test_fail(self):
        errors = []
        _assert(False, "boom", errors)
        assert errors == ["boom"]


# ── field validators ────────────────────────────────────────────────────


class TestCheckId:
    @pytest.mark.parametrize("val", ["abc", "a.b.c", "A-1_2.x"])
    def test_valid(self, val):
        errors = []
        _check_id(val, "$.id", errors)
        assert errors == []

    @pytest.mark.parametrize("val", ["", "ab cd", "a@b"])
    def test_invalid(self, val):
        errors = []
        _check_id(val, "$.id", errors)
        assert len(errors) >= 1

    def test_non_string(self):
        errors = []
        _check_id(123, "$.id", errors)
        assert any("must be string" in e for e in errors)


class TestCheckVersion:
    @pytest.mark.parametrize("val", ["0.4.0", "1.0", "12.3.456"])
    def test_valid(self, val):
        errors = []
        _check_version(val, "$.v", errors)
        assert errors == []

    @pytest.mark.parametrize("val", ["abc", "1", "1.2.3.4"])
    def test_invalid(self, val):
        errors = []
        _check_version(val, "$.v", errors)
        assert len(errors) >= 1


class TestCheckEmail:
    def test_valid(self):
        errors = []
        _check_email("a@b.com", "$.email", errors)
        assert errors == []

    def test_invalid(self):
        errors = []
        _check_email("notanemail", "$.email", errors)
        assert len(errors) >= 1


class TestCheckColumn:
    def test_valid(self):
        errors = []
        _check_column({"name": "col1", "type": "STRING"}, "$.col", errors)
        assert errors == []

    def test_missing_name(self):
        errors = []
        _check_column({"type": "STRING"}, "$.col", errors)
        assert any("name" in e for e in errors)

    def test_missing_type(self):
        errors = []
        _check_column({"name": "col1"}, "$.col", errors)
        assert any("type" in e for e in errors)

    def test_not_object(self):
        errors = []
        _check_column("bad", "$.col", errors)
        assert any("must be object" in e for e in errors)


class TestCheckLocation:
    def test_valid(self):
        errors = []
        _check_location({"format": "csv", "properties": {}}, "$.loc", errors)
        assert errors == []

    def test_missing_format(self):
        errors = []
        _check_location({"properties": {}}, "$.loc", errors)
        assert any("format" in e for e in errors)

    def test_not_object(self):
        errors = []
        _check_location("bad", "$.loc", errors)
        assert len(errors) >= 1


class TestCheckExpose:
    def _valid_expose(self):
        return {
            "id": "customers",
            "type": "table",
            "location": {"format": "csv", "properties": {}},
            "schema": [{"name": "id", "type": "INTEGER"}],
        }

    def test_valid(self):
        errors = []
        _check_expose(self._valid_expose(), 0, errors)
        assert errors == []

    def test_missing_id(self):
        e = self._valid_expose()
        del e["id"]
        errors = []
        _check_expose(e, 0, errors)
        assert any("id" in err for err in errors)

    def test_schema_not_array(self):
        e = self._valid_expose()
        e["schema"] = "bad"
        errors = []
        _check_expose(e, 0, errors)
        assert any("array" in err for err in errors)

    def test_not_object(self):
        errors = []
        _check_expose("bad", 0, errors)
        assert any("must be object" in e for e in errors)


class TestCheckConsumes:
    def test_valid(self):
        errors = []
        _check_consumes([{"id": "src1", "ref": "urn:fluid:src1"}], errors)
        assert errors == []

    def test_not_array(self):
        errors = []
        _check_consumes("bad", errors)
        assert any("array" in e for e in errors)

    def test_missing_ref(self):
        errors = []
        _check_consumes([{"id": "src1"}], errors)
        assert any("ref" in e for e in errors)


class TestCheckMetadata:
    def test_valid_email_owner(self):
        errors = []
        _check_metadata({"layer": "Gold", "owner": "a@b.com"}, errors)
        assert errors == []

    def test_valid_team_owner(self):
        errors = []
        _check_metadata({"layer": "Silver", "owner": {"team": "eng"}}, errors)
        assert errors == []

    def test_invalid_layer(self):
        errors = []
        _check_metadata({"layer": "Garbage", "owner": "a@b.com"}, errors)
        assert any("layer" in e for e in errors)

    def test_missing_owner(self):
        errors = []
        _check_metadata({"layer": "Gold"}, errors)
        assert any("owner" in e for e in errors)

    def test_owner_not_string_or_object(self):
        errors = []
        _check_metadata({"layer": "Gold", "owner": 123}, errors)
        assert len(errors) >= 1

    def test_team_owner_with_email(self):
        errors = []
        _check_metadata({"layer": "Gold", "owner": {"team": "eng", "email": "t@x.com"}}, errors)
        assert errors == []

    def test_not_object(self):
        errors = []
        _check_metadata("bad", errors)
        assert any("must be object" in e for e in errors)


class TestCheckBuild:
    def _valid_build(self, pattern="declarative"):
        return {
            "transformation": {
                "pattern": pattern,
                "engine": "dbt",
                "properties": {},
            }
        }

    def test_valid_declarative(self):
        errors = []
        _check_build(self._valid_build("declarative"), errors)
        assert errors == []

    def test_invalid_pattern(self):
        errors = []
        _check_build(self._valid_build("unknown"), errors)
        assert any("pattern" in e for e in errors)

    def test_hybrid_reference_with_model(self):
        build = self._valid_build("hybrid-reference")
        build["transformation"]["properties"] = {"model": "my_model"}
        errors = []
        _check_build(build, errors)
        assert errors == []

    def test_hybrid_reference_missing_model(self):
        build = self._valid_build("hybrid-reference")
        errors = []
        _check_build(build, errors)
        assert any("model" in e for e in errors)

    def test_embedded_logic_with_sql(self):
        build = self._valid_build("embedded-logic")
        build["transformation"]["properties"] = {"sql": "SELECT 1"}
        errors = []
        _check_build(build, errors)
        assert errors == []

    def test_embedded_logic_invalid_language(self):
        build = self._valid_build("embedded-logic")
        build["transformation"]["properties"] = {"sql": "SELECT 1", "language": "ruby"}
        errors = []
        _check_build(build, errors)
        assert any("language" in e for e in errors)

    def test_logical_mapping(self):
        build = self._valid_build("logical-mapping")
        build["transformation"]["properties"] = {"sources": ["s1"], "steps": ["step1"]}
        errors = []
        _check_build(build, errors)
        assert errors == []

    def test_not_object(self):
        errors = []
        _check_build("bad", errors)
        assert any("must be object" in e for e in errors)

    def test_missing_transformation(self):
        errors = []
        _check_build({}, errors)
        assert any("transformation" in e for e in errors)

    def test_declarative_from_joins_filters_select(self):
        build = self._valid_build("declarative")
        build["transformation"]["properties"] = {
            "from": "source_table",
            "joins": [{"table": "t2"}],
            "filters": ["x > 1"],
            "select": ["a", "b"],
        }
        errors = []
        _check_build(build, errors)
        assert errors == []

    def test_declarative_bad_types(self):
        build = self._valid_build("declarative")
        build["transformation"]["properties"] = {
            "from": 123,
            "joins": "bad",
            "filters": "bad",
            "select": "bad",
        }
        errors = []
        _check_build(build, errors)
        assert len(errors) == 4


# ── public API ──────────────────────────────────────────────────────────


class TestValidateContract:
    def _minimal_contract(self):
        return {
            "fluidVersion": "0.4.0",
            "kind": "DataProduct",
            "id": "my-product",
            "name": "My Product",
            "domain": "analytics",
            "metadata": {"layer": "Gold", "owner": "a@b.com"},
            "exposes": [
                {
                    "id": "customers",
                    "type": "table",
                    "location": {"format": "csv", "properties": {}},
                    "schema": [{"name": "id", "type": "INTEGER"}],
                }
            ],
        }

    def test_valid_minimal(self):
        ok, err = validate_contract(self._minimal_contract())
        assert ok is True
        assert err is None

    def test_missing_required_fields(self):
        ok, err = validate_contract({})
        assert ok is False
        assert "fluidVersion" in err
        assert "kind" in err

    def test_empty_exposes(self):
        c = self._minimal_contract()
        c["exposes"] = []
        ok, err = validate_contract(c)
        assert ok is False
        assert "at least one" in err

    def test_with_consumes(self):
        c = self._minimal_contract()
        c["consumes"] = [{"id": "source1", "ref": "urn:fluid:source1"}]
        ok, err = validate_contract(c)
        assert ok is True

    def test_with_build(self):
        c = self._minimal_contract()
        c["build"] = {
            "transformation": {
                "pattern": "declarative",
                "engine": "dbt",
                "properties": {},
            }
        }
        ok, err = validate_contract(c)
        assert ok is True

    def test_invalid_version(self):
        c = self._minimal_contract()
        c["fluidVersion"] = "bad"
        ok, err = validate_contract(c)
        assert ok is False
        assert "semver" in err

    def test_many_errors_capped(self):
        # Ensure error output doesn't flood
        ok, err = validate_contract({"exposes": "not-a-list"})
        assert ok is False
        assert err is not None

    def test_exposes_not_list(self):
        c = self._minimal_contract()
        c["exposes"] = "bad"
        ok, err = validate_contract(c)
        assert ok is False
        assert "array" in err
