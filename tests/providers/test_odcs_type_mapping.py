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

"""Regression tests for ``OdcsProvider._map_type_to_logical``.

The FLUID-type -> ODCS-logicalType map originally covered only a handful of
type names; a Snowflake ``NUMBER`` column (``"NUMBER".lower() == "number"``)
fell through to the ``string`` default, and the same was true for the whole
extended type family (``real``, ``bignumeric``, ``tinyint``, ``serial``,
``variant``, the Snowflake timestamp-tz variants, ...). Published ODCS
contracts then showed ``logicalType: "string"`` for numeric / integer /
temporal / structured columns, silently losing type fidelity. These tests
pin the full FLUID-type-family coverage.
"""

from __future__ import annotations

import pytest

from fluid_build.providers.odcs.odcs import OdcsProvider

pytestmark = pytest.mark.unit


class TestOdcsNumberTypeMapping:
    """``number`` and the core numeric family map to the ODCS ``number`` type."""

    def test_number_maps_to_number(self) -> None:
        assert OdcsProvider()._map_type_to_logical("number") == "number"

    def test_uppercase_number_maps_to_number(self) -> None:
        # Snowflake emits ``NUMBER``; the mapper lower-cases before lookup.
        assert OdcsProvider()._map_type_to_logical("NUMBER") == "number"

    @pytest.mark.parametrize(
        "fluid_type",
        ["NUMERIC", "FLOAT", "DOUBLE", "DECIMAL", "numeric", "float", "double", "decimal"],
    )
    def test_numeric_family_maps_to_number(self, fluid_type: str) -> None:
        assert OdcsProvider()._map_type_to_logical(fluid_type) == "number"


class TestOdcsTypeFamilyCoverage:
    """Every FLUID integer / number / string / boolean / temporal / structured
    type resolves to its correct ODCS logicalType — none falls through to the
    ``string`` default."""

    @pytest.mark.parametrize(
        ("fluid_type", "expected"),
        [
            # integer family — Postgres int2/4/8, width-suffixed, serials
            ("int2", "integer"),
            ("int4", "integer"),
            ("int8", "integer"),
            ("int16", "integer"),
            ("int32", "integer"),
            ("int64", "integer"),
            ("tinyint", "integer"),
            ("smallint", "integer"),
            ("mediumint", "integer"),
            ("longint", "integer"),
            ("serial", "integer"),
            ("bigserial", "integer"),
            # number family — real, dec, BigQuery bignumeric, money, width floats
            ("real", "number"),
            ("dec", "number"),
            ("bignumeric", "number"),
            ("money", "number"),
            ("float4", "number"),
            ("float8", "number"),
            ("float32", "number"),
            ("float64", "number"),
            # string family
            ("varchar2", "string"),
            ("nvarchar", "string"),
            ("nchar", "string"),
            ("character", "string"),
            ("clob", "string"),
            # boolean
            ("bit", "boolean"),
            # temporal — Snowflake tz variants + SQL Server datetime types
            ("timestamptz", "timestamp"),
            ("timestamp_tz", "timestamp"),
            ("timestamp_ntz", "timestamp"),
            ("timestamp_ltz", "timestamp"),
            ("datetime2", "timestamp"),
            ("smalldatetime", "timestamp"),
            # structured / semi-structured — Snowflake variant, struct/map/record
            ("variant", "object"),
            ("struct", "object"),
            ("map", "object"),
            ("record", "object"),
            ("row", "object"),
            ("jsonb", "object"),
        ],
    )
    def test_type_resolves_to_expected_logical(self, fluid_type: str, expected: str) -> None:
        assert OdcsProvider()._map_type_to_logical(fluid_type) == expected

    @pytest.mark.parametrize(
        ("fluid_type", "expected"),
        [
            ("REAL", "number"),
            ("TINYINT", "integer"),
            ("VARIANT", "object"),
            ("BIGNUMERIC", "number"),
            ("TIMESTAMPTZ", "timestamp"),
        ],
    )
    def test_uppercase_extended_types_resolve(self, fluid_type: str, expected: str) -> None:
        # Uppercase dialect spellings resolve too — the mapper lower-cases.
        assert OdcsProvider()._map_type_to_logical(fluid_type) == expected


class TestOdcsTypeMapDriftGuard:
    """The ODCS logical-type map must stay exhaustive against the FLUID schema's
    column-type enum — its single source of truth. This guard fails if the
    schema gains a column type the map does not cover, so a new type can never
    silently degrade to the ``string`` default and lose type fidelity."""

    @staticmethod
    def _fluid_column_type_enum() -> list[str]:
        """Return the canonical column-type enum from the bundled FLUID 0.7.3 schema."""
        import json
        from pathlib import Path

        import fluid_build

        schema = json.loads(
            (Path(fluid_build.__file__).parent / "schemas" / "fluid-schema-0.7.3.json").read_text(
                encoding="utf-8"
            )
        )
        type_def = schema["$defs"]["column"]["properties"]["type"]
        for branch in type_def["anyOf"]:
            if "enum" in branch:
                return list(branch["enum"])
        raise AssertionError("FLUID schema $defs.column.properties.type has no enum branch")

    def test_every_fluid_schema_type_is_mapped(self) -> None:
        from fluid_build.providers.odcs.odcs import _FLUID_TYPE_TO_ODCS_LOGICAL

        enum_types = self._fluid_column_type_enum()
        assert enum_types, "expected a non-empty FLUID column-type enum"
        missing = sorted(t for t in enum_types if t.lower() not in _FLUID_TYPE_TO_ODCS_LOGICAL)
        assert not missing, (
            "FLUID schema column types missing from _FLUID_TYPE_TO_ODCS_LOGICAL: "
            f"{missing} — add them in fluid_build/providers/odcs/odcs.py, otherwise "
            "ODCS export silently degrades them to logicalType 'string'."
        )

    def test_map_values_are_valid_odcs_logical_types(self) -> None:
        from fluid_build.providers.odcs.odcs import _FLUID_TYPE_TO_ODCS_LOGICAL

        # The nine ODCS v3.1.0 logicalTypes.
        valid = {
            "string",
            "date",
            "timestamp",
            "time",
            "number",
            "integer",
            "object",
            "array",
            "boolean",
        }
        bad = {k: v for k, v in _FLUID_TYPE_TO_ODCS_LOGICAL.items() if v not in valid}
        assert not bad, f"map values that are not valid ODCS logicalTypes: {bad}"

    def test_newly_mapped_types_resolve_correctly(self) -> None:
        # year/super previously fell through to the WRONG "string" default.
        provider = OdcsProvider()
        assert provider._map_type_to_logical("year") == "integer"
        assert provider._map_type_to_logical("super") == "object"
        # The genuinely-string exotic types (binary/geo/uuid/interval/sketch).
        for t in ("uuid", "geography", "blob", "interval", "hll", "varbinary"):
            assert provider._map_type_to_logical(t) == "string"
