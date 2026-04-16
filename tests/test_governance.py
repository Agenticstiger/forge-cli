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

"""Tests for providers/snowflake/governance.py — masking templates, errors, validator, applicator."""

from unittest.mock import MagicMock

import pytest

from fluid_build.providers.snowflake.governance import (
    GovernanceValidator,
    MaskingPolicyTemplates,
    SnowflakeGovernanceError,
    UnifiedGovernanceApplicator,
    _parse_qualified_name,
)


# ── MaskingPolicyTemplates ───────────────────────────────────────────
class TestMaskingPolicyTemplates:
    def test_hash_string(self):
        tpl = MaskingPolicyTemplates.hash_template("VARCHAR")
        assert "SHA256" in tpl
        assert "SYSADMIN" in tpl

    def test_hash_custom_algorithm(self):
        tpl = MaskingPolicyTemplates.hash_template("VARCHAR", "MD5")
        assert "MD5" in tpl

    def test_hash_algorithm_case_insensitive(self):
        tpl = MaskingPolicyTemplates.hash_template("VARCHAR", "sha1")
        assert "SHA1(val)" in tpl

    def test_hash_algorithm_rejects_unknown(self):
        with pytest.raises(ValueError, match="Invalid hash algorithm"):
            MaskingPolicyTemplates.hash_template("VARCHAR", "BLAKE3")

    def test_hash_algorithm_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid hash algorithm"):
            MaskingPolicyTemplates.hash_template("VARCHAR", "SHA256; DROP TABLE users; --")

    def test_hash_algorithm_rejects_non_string(self):
        with pytest.raises(ValueError, match="Invalid hash algorithm"):
            MaskingPolicyTemplates.hash_template("VARCHAR", None)

    def test_hash_timestamp(self):
        tpl = MaskingPolicyTemplates.hash_template("TIMESTAMP_NTZ")
        assert "1970-01-01" in tpl
        assert "SHA256" not in tpl  # timestamp branch doesn't hash

    def test_hash_timestamp_ltz(self):
        tpl = MaskingPolicyTemplates.hash_template("TIMESTAMP_LTZ")
        assert "1970-01-01" in tpl

    def test_hash_date(self):
        tpl = MaskingPolicyTemplates.hash_template("DATE")
        assert "1970-01-01" in tpl

    def test_email_mask(self):
        tpl = MaskingPolicyTemplates.email_mask_template()
        assert "SPLIT_PART" in tpl
        assert "***@" in tpl

    def test_redact(self):
        tpl = MaskingPolicyTemplates.redact_template()
        assert "********" in tpl

    def test_partial_mask_default(self):
        tpl = MaskingPolicyTemplates.partial_mask_template()
        assert "4" in tpl  # default visible_chars
        assert "RIGHT" in tpl

    def test_partial_mask_custom(self):
        tpl = MaskingPolicyTemplates.partial_mask_template(visible_chars=2)
        assert "2" in tpl

    def test_partial_mask_rejects_float_string(self):
        with pytest.raises(ValueError, match="Invalid visible_chars"):
            MaskingPolicyTemplates.partial_mask_template(visible_chars="4.5")

    def test_partial_mask_rejects_negative(self):
        with pytest.raises(ValueError, match="Invalid visible_chars"):
            MaskingPolicyTemplates.partial_mask_template(visible_chars=-1)

    def test_partial_mask_rejects_bool(self):
        with pytest.raises(ValueError, match="Invalid visible_chars"):
            MaskingPolicyTemplates.partial_mask_template(visible_chars=True)

    def test_partial_mask_rejects_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid visible_chars"):
            MaskingPolicyTemplates.partial_mask_template(visible_chars=10_000)

    def test_get_template_hash(self):
        t = MaskingPolicyTemplates.get_template("hash", "VARCHAR")
        assert t is not None
        assert "SHA256" in t

    def test_get_template_hash_with_algorithm(self):
        t = MaskingPolicyTemplates.get_template("hash", "VARCHAR", {"algorithm": "MD5"})
        assert "MD5" in t

    def test_get_template_email_mask(self):
        t = MaskingPolicyTemplates.get_template("email_mask")
        assert t is not None

    def test_get_template_redact(self):
        t = MaskingPolicyTemplates.get_template("redact")
        assert "********" in t

    def test_get_template_partial_mask(self):
        t = MaskingPolicyTemplates.get_template("partial_mask", params={"visible_chars": 3})
        assert "3" in t

    def test_get_template_unknown(self):
        assert MaskingPolicyTemplates.get_template("unknown_strategy") is None


# ── SnowflakeGovernanceError ─────────────────────────────────────────
class TestSnowflakeGovernanceError:
    def test_basic(self):
        e = SnowflakeGovernanceError("fail")
        assert "fail" in str(e)
        assert e.suggestion is None

    def test_with_suggestion(self):
        e = SnowflakeGovernanceError("fail", suggestion="try X")
        msg = e.format_message()
        assert "try X" in msg
        assert "Suggestion" in msg

    def test_with_details(self):
        e = SnowflakeGovernanceError("fail", details={"code": 42})
        msg = e.format_message()
        assert "42" in msg
        assert "Details" in msg

    def test_attributes(self):
        e = SnowflakeGovernanceError("m", "s", {"k": "v"})
        assert e.message == "m"
        assert e.suggestion == "s"
        assert e.details == {"k": "v"}


# ── _parse_qualified_name ────────────────────────────────────────────
class TestParseQualifiedName:
    def test_valid(self):
        assert _parse_qualified_name("DB.SCH.TBL") == ("DB", "SCH", "TBL")

    def test_too_few_parts(self):
        with pytest.raises(ValueError, match="Expected fully-qualified name"):
            _parse_qualified_name("DB.SCH")

    def test_too_many_parts(self):
        with pytest.raises(ValueError, match="Expected fully-qualified name"):
            _parse_qualified_name("DB.SCH.TBL.EXTRA")

    def test_invalid_identifier_in_part(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _parse_qualified_name("DB.SCH;DROP.TBL")


# ── GovernanceValidator ──────────────────────────────────────────────
class TestGovernanceValidator:
    def _make_validator(self, cursor=None):
        if cursor is None:
            cursor = MagicMock()
        return GovernanceValidator(cursor, "DB", "SCH", "TBL")

    def test_validate_table_exists_true(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        v = self._make_validator(cursor)
        assert v.validate_table_exists() is True
        cursor.execute.assert_called_once()

    def test_validate_table_exists_false(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (0,)
        v = self._make_validator(cursor)
        assert v.validate_table_exists() is False

    def test_validate_table_exists_none(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        v = self._make_validator(cursor)
        assert not v.validate_table_exists()

    def test_validate_table_exists_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("DB error")
        v = self._make_validator(cursor)
        assert v.validate_table_exists() is False

    def test_validate_column_descriptions(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (10, 7)
        v = self._make_validator(cursor)
        applied, total = v.validate_column_descriptions()
        assert applied == 7
        assert total == 10

    def test_validate_column_descriptions_none_result(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        v = self._make_validator(cursor)
        assert v.validate_column_descriptions() == (0, 0)

    def test_validate_column_descriptions_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("err")
        v = self._make_validator(cursor)
        assert v.validate_column_descriptions() == (0, 0)

    def test_validate_table_tags(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("TAG1", "val1"), ("TAG2", "val2")]
        v = self._make_validator(cursor)
        tags = v.validate_table_tags()
        assert len(tags) == 2
        assert tags[0] == {"name": "TAG1", "value": "val1"}

    def test_validate_table_tags_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("err")
        v = self._make_validator(cursor)
        assert v.validate_table_tags() == []

    def test_validate_column_tags(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("COL1", 3), ("COL2", 1)]
        v = self._make_validator(cursor)
        result = v.validate_column_tags()
        assert result == {"COL1": 3, "COL2": 1}

    def test_validate_column_tags_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("err")
        v = self._make_validator(cursor)
        assert v.validate_column_tags() == {}

    def test_validate_masking_policies(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("EMAIL", "EMAIL_MASK")]
        v = self._make_validator(cursor)
        result = v.validate_masking_policies()
        assert result == [{"column": "EMAIL", "policy": "EMAIL_MASK"}]

    def test_validate_masking_policies_fallback(self):
        cursor = MagicMock()
        cursor.execute.side_effect = [Exception("first"), None]
        cursor.fetchall.return_value = []
        v = self._make_validator(cursor)
        result = v.validate_masking_policies()
        assert result == []

    def test_get_table_properties_found(self):
        cursor = MagicMock()
        row = [None] * 12
        row[7] = "90"
        row[11] = "LINEAR(id)"
        cursor.fetchone.return_value = row
        v = self._make_validator(cursor)
        props = v.get_table_properties()
        assert props["exists"] is True
        assert props["clustering_key"] == "LINEAR(id)"
        assert props["retention_time"] == "90"

    def test_get_table_properties_not_found(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        v = self._make_validator(cursor)
        props = v.get_table_properties()
        assert props["exists"] is False

    def test_get_table_properties_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("err")
        v = self._make_validator(cursor)
        props = v.get_table_properties()
        assert props["exists"] is False

    @pytest.mark.parametrize(
        ("database", "schema", "table"),
        [
            ("DB", "SCH;DROP", "TBL"),
            ("DB", "SCH", "TBL bad"),
            ("DB-1", "SCH", "TBL"),
        ],
    )
    def test_rejects_invalid_identifiers(self, database, schema, table):
        cursor = MagicMock()
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            GovernanceValidator(cursor, database, schema, table)


# ── UnifiedGovernanceApplicator ──────────────────────────────────────
class TestUnifiedGovernanceApplicator:
    def _minimal_contract(self):
        return {
            "tags": ["pii", "real-time"],
            "labels": {"env": "prod"},
            "exposes": [
                {
                    "binding": {
                        "location": {"database": "DB", "schema": "SCH", "table": "TBL"},
                        "properties": {},
                    },
                    "contract": {
                        "schema": [
                            {
                                "name": "id",
                                "type": "INTEGER",
                                "required": True,
                                "description": "PK",
                            },
                            {"name": "name", "type": "STRING"},
                        ]
                    },
                }
            ],
        }

    def test_init_stats(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {"exposes": []}, dry_run=True)
        assert app.stats["tables_created"] == 0
        assert app.stats["errors"] == []

    def test_map_type_known(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._map_type("STRING") == "VARCHAR"
        assert app._map_type("INTEGER") == "NUMBER"
        assert app._map_type("FLOAT") == "FLOAT"
        assert app._map_type("BOOLEAN") == "BOOLEAN"
        assert app._map_type("TIMESTAMP") == "TIMESTAMP_NTZ"
        assert app._map_type("DATE") == "DATE"

    def test_map_type_unknown_passthrough(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._map_type("VARIANT") == "VARIANT"

    def test_map_type_case_insensitive(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._map_type("string") == "VARCHAR"

    def test_generate_create_table_ddl(self):
        contract = self._minimal_contract()
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, contract)
        expose = contract["exposes"][0]
        ddl = app._generate_create_table_ddl("DB", "SCH", "TBL", expose)
        assert "CREATE TABLE IF NOT EXISTS DB.SCH.TBL" in ddl
        assert "ID NUMBER" in ddl
        assert "NAME VARCHAR" in ddl
        assert "COMMENT 'PK'" in ddl

    def test_generate_create_table_with_clustering(self):
        contract = self._minimal_contract()
        contract["exposes"][0]["binding"]["properties"]["cluster_by"] = ["id", "name"]
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, contract)
        ddl = app._generate_create_table_ddl("DB", "SCH", "TBL", contract["exposes"][0])
        assert "CLUSTER BY (id, name)" in ddl

    def test_generate_create_table_with_comment(self):
        contract = self._minimal_contract()
        contract["exposes"][0]["binding"]["properties"]["comment"] = "Bob's table"
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, contract)
        ddl = app._generate_create_table_ddl("DB", "SCH", "TBL", contract["exposes"][0])
        assert "COMMENT = 'Bob''s table'" in ddl

    def test_generate_create_table_rejects_invalid_column_identifier(self):
        contract = self._minimal_contract()
        contract["exposes"][0]["contract"]["schema"][0]["name"] = "bad-name"
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, contract)
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            app._generate_create_table_ddl("DB", "SCH", "TBL", contract["exposes"][0])

    def test_apply_all_missing_location(self):
        cursor = MagicMock()
        contract = {"exposes": [{"binding": {"location": {}}}]}
        app = UnifiedGovernanceApplicator(cursor, contract)
        result = app.apply_all()
        assert result["status"] == "error"

    def test_apply_all_missing_exposes(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {"exposes": []})
        result = app.apply_all()
        assert result["status"] == "error"
        assert "Missing expose configuration" in result["error"]
        cursor.execute.assert_not_called()

    def test_apply_all_rejects_invalid_schema_identifier_without_execute(self):
        cursor = MagicMock()
        contract = self._minimal_contract()
        contract["exposes"][0]["binding"]["location"]["schema"] = "SCH;DROP"
        app = UnifiedGovernanceApplicator(cursor, contract)
        result = app.apply_all()
        assert result["status"] == "error"
        assert "Invalid SQL identifier" in result["error"]
        cursor.execute.assert_not_called()

    def test_apply_all_dry_run(self):
        cursor = MagicMock()
        # Validator still runs even in dry_run — set up cursor for validation calls
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = []
        contract = self._minimal_contract()
        app = UnifiedGovernanceApplicator(cursor, contract, dry_run=True)
        result = app.apply_all()
        assert result["status"] == "success"
        assert result["dry_run"] is True

    def test_apply_all_success(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)  # table exists check
        cursor.fetchall.return_value = []
        contract = self._minimal_contract()
        app = UnifiedGovernanceApplicator(cursor, contract, dry_run=False)
        result = app.apply_all()
        assert result["status"] == "success"
        assert result["table"] == "DB.SCH.TBL"
        assert cursor.execute.call_count > 0

    def test_create_tag_dry_run(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=True)
        app._create_tag("SCH", "MY_TAG")
        cursor.execute.assert_not_called()

    def test_create_tag_real(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._create_tag("SCH", "MY_TAG")
        cursor.execute.assert_called_once()
        assert "CREATE TAG" in cursor.execute.call_args[0][0]

    def test_create_tag_error_logged(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("perm denied")
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        # Should not raise
        app._create_tag("SCH", "BAD_TAG")

    def test_create_tag_rejects_invalid_identifier(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            app._create_tag("SCH", "BAD-TAG")

    def test_apply_tags_to_table_dry_run(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=True)
        app._apply_tags_to_table("DB.SCH.TBL", {"T1": "v1"})
        cursor.execute.assert_not_called()

    def test_apply_tags_to_table_real(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_tags_to_table("DB.SCH.TBL", {"T1": "v1"})
        assert app.stats["table_tags_applied"] == 1

    def test_apply_clustering_dry_run(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=True)
        app._apply_clustering("DB.SCH.TBL", ["id"])
        cursor.execute.assert_not_called()

    def test_apply_clustering_list(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_clustering("DB.SCH.TBL", ["id", "ts"])
        assert "CLUSTER BY" in cursor.execute.call_args[0][0]

    def test_apply_clustering_string(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_clustering("DB.SCH.TBL", "id")
        assert "CLUSTER BY" in cursor.execute.call_args[0][0]

    def test_apply_retention_dry_run(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=True)
        app._apply_retention("DB.SCH.TBL", 90)
        cursor.execute.assert_not_called()

    def test_apply_retention_real(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_retention("DB.SCH.TBL", 90)
        assert "DATA_RETENTION_TIME_IN_DAYS = 90" in cursor.execute.call_args[0][0]

    def test_apply_change_tracking_true(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_change_tracking("DB.SCH.TBL", True)
        assert "CHANGE_TRACKING = TRUE" in cursor.execute.call_args[0][0]

    def test_apply_change_tracking_false(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        app._apply_change_tracking("DB.SCH.TBL", False)
        assert "CHANGE_TRACKING = FALSE" in cursor.execute.call_args[0][0]

    def test_get_column_type_found(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("NUMBER",)
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._get_column_type("DB.SCH.TBL", "ID") == "NUMBER"

    def test_get_column_type_not_found(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._get_column_type("DB.SCH.TBL", "ID") == "VARCHAR"

    def test_get_column_type_exception(self):
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("err")
        app = UnifiedGovernanceApplicator(cursor, {})
        assert app._get_column_type("DB.SCH.TBL", "ID") == "VARCHAR"

    def test_apply_security_policies_rejects_invalid_column_identifier(self):
        cursor = MagicMock()
        app = UnifiedGovernanceApplicator(cursor, {}, dry_run=False)
        expose = {
            "policy": {
                "privacy": {
                    "masking": [
                        {"column": "email;DROP", "strategy": "hash"},
                    ]
                }
            }
        }
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            app._apply_security_policies("DB", "SCH", "DB.SCH.TBL", expose)
        cursor.execute.assert_not_called()
