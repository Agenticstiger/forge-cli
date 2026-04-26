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

"""Coverage for the deterministic multi-dialect type mapper (D5).

The mapper is the post-processor that back-fills OSI
``expression.dialects[]`` lists when the LLM forgot a dialect or
produced a wrong physical type for it. Tests pin:

* **Round-trip for every built-in dialect** on the 20+ logical types
  shipped in the registry.
* **Placeholder substitution** — ``{length}`` / ``{precision}`` /
  ``{scale}`` / ``{element_type}`` get filled from qualifiers or
  registry defaults, never leaked into the output.
* **Pass-through behaviour** for unknown logical types in known
  dialects; **UNSUPPORTED** for unknown dialects (distinct enough that
  callers can distinguish "fix your input" from "add a dialect").
* **Aliases** — ``postgresql``/``pg``/``bq``/``databricks-sql`` all
  resolve to canonical labels.
* **Non-destructive back-fill** — ``fill_missing_dialects`` preserves
  LLM-authored entries unless ``override=True``.
* **ValidationReport aggregation** on a multi-column schema.
* **Pydantic round-trip** for ``MappingResult`` / ``ValidationReport``
  via the staged-store JSON contract.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.forge_datamodel.sql import (
    DEFAULT_DIALECTS,
    DialectMapper,
    MappingResult,
    ValidationReport,
)
from fluid_build.forge_datamodel.sql.registry import (
    DIALECTS,
    LOGICAL_TYPES,
    REGISTRY_VERSION,
)

# ----------------------------------------------------------------------
# Registry shape — keeps registry.py and dialect_mapper.py in lockstep
# ----------------------------------------------------------------------


class TestRegistryShape:
    def test_every_dialect_covers_every_shipped_logical_type(self):
        """The registry's contract: every dialect in ``DIALECTS`` has an
        entry for every logical type in ``LOGICAL_TYPES``. Breaking this
        would let the mapper silently pass-through for types we
        explicitly listed."""
        missing = {}
        for dialect, mapping in DIALECTS.items():
            gaps = sorted(set(LOGICAL_TYPES.keys()) - set(mapping.keys()))
            if gaps:
                missing[dialect] = gaps
        assert missing == {}, f"Missing per-dialect mappings: {missing}"

    def test_registry_version_is_semver_like(self):
        parts = REGISTRY_VERSION.split(".")
        assert len(parts) >= 2
        assert all(p.isdigit() for p in parts)

    def test_default_dialects_are_all_registered(self):
        mapper = DialectMapper()
        supported = set(mapper.supported_dialects())
        for d in DEFAULT_DIALECTS:
            assert d in supported


# ----------------------------------------------------------------------
# map_type — the single-cell primitive
# ----------------------------------------------------------------------


class TestMapType:
    @pytest.fixture
    def mapper(self) -> DialectMapper:
        return DialectMapper()

    @pytest.mark.parametrize(
        "logical,dialect,expected",
        [
            ("STRING", "SNOWFLAKE", "VARCHAR(255)"),
            ("STRING", "BIGQUERY", "STRING"),
            ("STRING", "POSTGRES", "VARCHAR(255)"),
            ("DECIMAL", "SNOWFLAKE", "NUMBER(18,4)"),
            ("DECIMAL", "BIGQUERY", "NUMERIC(18,4)"),
            ("DECIMAL", "POSTGRES", "NUMERIC(18,4)"),
            ("TIMESTAMP", "SNOWFLAKE", "TIMESTAMP_TZ"),
            ("TIMESTAMP", "POSTGRES", "TIMESTAMPTZ"),
            ("JSON", "SNOWFLAKE", "VARIANT"),
            ("JSON", "POSTGRES", "JSONB"),
            ("BOOLEAN", "BIGQUERY", "BOOL"),
            ("INTEGER", "BIGQUERY", "INT64"),
        ],
    )
    def test_canonical_mappings(
        self, mapper: DialectMapper, logical: str, dialect: str, expected: str
    ):
        result = mapper.map_type(logical, dialect)
        assert result.physical_type == expected
        assert result.supported
        assert result.target_dialect == dialect

    def test_placeholder_substitution_from_qualifiers(self):
        mapper = DialectMapper()
        result = mapper.map_type(
            "DECIMAL",
            "SNOWFLAKE",
            qualifiers={"precision": 38, "scale": 10},
        )
        assert result.physical_type == "NUMBER(38,10)"

    def test_placeholder_substitution_uses_registry_defaults(self):
        """Callers who pass no qualifiers still get a valid physical
        type — defaults come from the logical-type spec's ``defaults``
        block, falling back to global ``DEFAULTS``."""
        mapper = DialectMapper()
        string_result = mapper.map_type("STRING", "SNOWFLAKE")
        assert string_result.physical_type == "VARCHAR(255)"
        decimal_result = mapper.map_type("DECIMAL", "POSTGRES")
        assert decimal_result.physical_type == "NUMERIC(18,4)"

    def test_array_element_type_substitution(self):
        mapper = DialectMapper()
        result = mapper.map_type(
            "ARRAY",
            "BIGQUERY",
            qualifiers={"element_type": "INT64"},
        )
        assert result.physical_type == "ARRAY<INT64>"

    def test_map_key_and_value_substitution(self):
        mapper = DialectMapper()
        result = mapper.map_type(
            "MAP",
            "DATABRICKS",
            qualifiers={"key_type": "STRING", "value_type": "DOUBLE"},
        )
        assert result.physical_type == "MAP<STRING,DOUBLE>"

    def test_unknown_dialect_returns_unsupported(self):
        mapper = DialectMapper()
        result = mapper.map_type("STRING", "TERADATA")
        assert result.supported is False
        assert result.physical_type == "UNSUPPORTED"
        assert result.target_dialect == "TERADATA"
        assert any("Unknown target dialect" in w for w in result.warnings)

    def test_unknown_logical_type_passes_through(self):
        """A logical type the registry doesn't know about passes
        through in a known dialect — the caller gets warned but still
        has a usable physical type to emit."""
        mapper = DialectMapper()
        result = mapper.map_type("GEOHASH", "SNOWFLAKE")
        assert result.supported is True
        assert result.physical_type == "GEOHASH"
        assert result.rule_id == "pass-through"
        assert any("pass-through" in w for w in result.warnings)

    @pytest.mark.parametrize(
        "alias,canonical",
        [
            ("postgresql", "POSTGRES"),
            ("PG", "POSTGRES"),
            ("bq", "BIGQUERY"),
            ("BIGQUERY", "BIGQUERY"),
            ("databricks-sql", "DATABRICKS"),
            ("ansi", "ANSI_SQL"),
            ("STANDARD_SQL", "ANSI_SQL"),
        ],
    )
    def test_dialect_aliases_resolve(self, mapper: DialectMapper, alias: str, canonical: str):
        result = mapper.map_type("INTEGER", alias)
        assert result.supported
        assert result.target_dialect == canonical

    def test_lossy_flag_propagates(self):
        mapper = DialectMapper()
        result = mapper.map_type("MAP", "BIGQUERY")
        assert result.supported is True
        assert result.lossy is True
        assert result.note is not None

    def test_map_type_is_case_insensitive_on_logical_type(self):
        mapper = DialectMapper()
        lower = mapper.map_type("decimal", "SNOWFLAKE")
        upper = mapper.map_type("DECIMAL", "SNOWFLAKE")
        assert lower.physical_type == upper.physical_type == "NUMBER(18,4)"


# ----------------------------------------------------------------------
# fill_missing_dialects — the OSI post-processor
# ----------------------------------------------------------------------


class TestFillMissingDialects:
    def test_fills_in_all_default_dialects_for_empty_input(self):
        mapper = DialectMapper()
        out = mapper.fill_missing_dialects("DECIMAL", existing=None)
        seen = {e["dialect"] for e in out}
        assert seen == set(DEFAULT_DIALECTS)

    def test_preserves_llm_entries_by_default(self):
        """An LLM-authored Snowflake entry with a hand-tuned expression
        should survive back-fill even if the static table would emit
        something different. The mapper is advisory, not authoritative."""
        mapper = DialectMapper()
        existing = [{"dialect": "SNOWFLAKE", "expression": "NUMBER(38,18)"}]
        out = mapper.fill_missing_dialects(
            "DECIMAL", existing=existing, targets=["SNOWFLAKE", "POSTGRES"]
        )
        by_dialect = {e["dialect"]: e["expression"] for e in out}
        assert by_dialect["SNOWFLAKE"] == "NUMBER(38,18)"
        assert by_dialect["POSTGRES"] == "NUMERIC(18,4)"

    def test_override_replaces_existing_entries(self):
        mapper = DialectMapper()
        existing = [{"dialect": "SNOWFLAKE", "expression": "NUMBER(38,18)"}]
        out = mapper.fill_missing_dialects(
            "DECIMAL",
            existing=existing,
            targets=["SNOWFLAKE"],
            override=True,
            qualifiers={"precision": 10, "scale": 2},
        )
        assert len(out) == 1
        assert out[0]["expression"] == "NUMBER(10,2)"

    def test_unknown_target_dialect_is_silently_skipped(self):
        """Back-fill must never crash a forge run — an unknown target
        is dropped rather than raising. ``map_type`` has already
        recorded the warning on the first call."""
        mapper = DialectMapper()
        out = mapper.fill_missing_dialects(
            "STRING", existing=None, targets=["SNOWFLAKE", "TERADATA"]
        )
        dialects_emitted = {e["dialect"] for e in out}
        assert "SNOWFLAKE" in dialects_emitted
        assert "TERADATA" not in dialects_emitted

    def test_input_list_is_not_mutated(self):
        mapper = DialectMapper()
        existing = [{"dialect": "SNOWFLAKE", "expression": "NUMBER(38,18)"}]
        before = json.dumps(existing)
        mapper.fill_missing_dialects("DECIMAL", existing=existing)
        assert json.dumps(existing) == before

    def test_respects_canonical_dialect_case(self):
        """``existing`` entries use canonical uppercase OSI labels;
        targets may come in in any case via alias — the output entries
        must all be canonical so the OSI validator accepts them."""
        mapper = DialectMapper()
        out = mapper.fill_missing_dialects(
            "STRING",
            existing=None,
            targets=["snowflake", "postgresql", "BIGQUERY"],
        )
        for entry in out:
            assert entry["dialect"] == entry["dialect"].upper()


# ----------------------------------------------------------------------
# map_table_schema — whole-schema report
# ----------------------------------------------------------------------


class TestMapTableSchema:
    @pytest.fixture
    def schema(self):
        return [
            {"name": "customer_id", "logical_type": "IDENTIFIER"},
            {"name": "email", "logical_type": "EMAIL"},
            {
                "name": "amount",
                "logical_type": "DECIMAL",
                "qualifiers": {"precision": 18, "scale": 4},
            },
            {"name": "created_at", "logical_type": "TIMESTAMP"},
        ]

    def test_all_columns_map_successfully(self, schema):
        mapper = DialectMapper()
        results, report = mapper.map_table_schema(schema, "SNOWFLAKE")
        assert len(results) == 4
        assert report.total_mappings == 4
        assert report.successful_mappings == 4
        assert report.unsupported_mappings == 0
        assert report.errors == []

    def test_missing_logical_type_reports_error(self):
        mapper = DialectMapper()
        _, report = mapper.map_table_schema(
            [{"name": "foo"}, {"name": "bar", "type": "STRING"}], "SNOWFLAKE"
        )
        # First column errored; second (using fallback ``type`` key) worked.
        assert report.total_mappings == 1
        assert any("no logical type" in e for e in report.errors)

    def test_unknown_dialect_marks_everything_unsupported(self, schema):
        mapper = DialectMapper()
        _, report = mapper.map_table_schema(schema, "TERADATA")
        assert report.unsupported_mappings == len(schema)
        assert all("unsupported" in e.lower() for e in report.errors)

    def test_lossy_column_recorded_in_warnings(self):
        mapper = DialectMapper()
        _, report = mapper.map_table_schema([{"name": "attrs", "logical_type": "MAP"}], "BIGQUERY")
        assert report.lossy_mappings == 1
        assert any("lossy" in w.lower() for w in report.warnings)


# ----------------------------------------------------------------------
# Override hooks — user-supplied dialects
# ----------------------------------------------------------------------


class TestOverrides:
    def test_dialects_override_adds_new_dialect(self):
        custom_rules = {
            "DUCKDB": {
                "STRING": {
                    "physical": "VARCHAR",
                    "supported": True,
                    "lossy": False,
                    "rule_id": "duck_string",
                },
                "INTEGER": {
                    "physical": "INTEGER",
                    "supported": True,
                    "lossy": False,
                    "rule_id": "duck_integer",
                },
            }
        }
        mapper = DialectMapper(dialects_override=custom_rules)
        result = mapper.map_type("STRING", "DUCKDB")
        assert result.supported
        assert result.physical_type == "VARCHAR"
        assert result.rule_id == "duck_string"

    def test_dialects_override_can_patch_existing_dialect(self):
        """A partial override on an existing dialect merges with
        built-ins — built-in rules for types the override didn't touch
        keep firing."""
        patch = {
            "SNOWFLAKE": {
                "STRING": {
                    "physical": "TEXT",
                    "supported": True,
                    "lossy": False,
                    "rule_id": "sf_string_patched",
                },
            }
        }
        mapper = DialectMapper(dialects_override=patch)
        patched = mapper.map_type("STRING", "SNOWFLAKE")
        untouched = mapper.map_type("DECIMAL", "SNOWFLAKE")
        assert patched.physical_type == "TEXT"
        assert patched.rule_id == "sf_string_patched"
        assert untouched.physical_type == "NUMBER(18,4)"


# ----------------------------------------------------------------------
# Pydantic round-trip — staged-store contract
# ----------------------------------------------------------------------


class TestPydanticRoundTrip:
    def test_mapping_result_round_trip(self):
        mapper = DialectMapper()
        result = mapper.map_type("DECIMAL", "SNOWFLAKE", qualifiers={"precision": 38, "scale": 10})
        as_json = result.model_dump_json()
        loaded = MappingResult.model_validate_json(as_json)
        assert loaded == result

    def test_validation_report_round_trip(self):
        report = ValidationReport(
            total_mappings=3,
            successful_mappings=2,
            lossy_mappings=1,
            unsupported_mappings=0,
            warnings=["lossy thing"],
            errors=[],
        )
        as_json = report.model_dump_json()
        loaded = ValidationReport.model_validate_json(as_json)
        assert loaded == report


# ----------------------------------------------------------------------
# Cache key stability — D5 is meant to be cache-keyed by registry version
# ----------------------------------------------------------------------


class TestCacheKeyability:
    def test_registry_version_is_exposed_on_instance(self):
        mapper = DialectMapper()
        assert mapper.registry_version == REGISTRY_VERSION
