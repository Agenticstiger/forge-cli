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

"""Tests for providers/aws/util/agent_policy.py — tag sanitization, ARN parsing, policy extraction."""

from fluid_build.providers.aws.util.agent_policy import (
    AgentPolicyExtractor,
    extract_agent_policy_tags,
    generate_lake_formation_policy,
    sanitize_tag_key,
    sanitize_tag_value,
)


# ── sanitize_tag_value ───────────────────────────────────────────────
class TestSanitizeTagValue:
    def test_clean_value(self):
        assert sanitize_tag_value("hello") == "hello"

    def test_empty(self):
        assert sanitize_tag_value("") == ""

    def test_truncates_at_256(self):
        assert len(sanitize_tag_value("x" * 300)) == 256

    def test_special_chars_replaced(self):
        assert sanitize_tag_value("a!b#c") == "a_b_c"

    def test_valid_special_chars_kept(self):
        assert sanitize_tag_value("a+b=c.d:e/f@g") == "a+b=c.d:e/f@g"

    def test_spaces_kept(self):
        assert sanitize_tag_value("hello world") == "hello world"


# ── sanitize_tag_key ────────────────────────────────────────────────
class TestSanitizeTagKey:
    def test_clean_key(self):
        assert sanitize_tag_key("my_key") == "my_key"

    def test_empty(self):
        assert sanitize_tag_key("") == ""

    def test_truncates_at_128(self):
        assert len(sanitize_tag_key("x" * 200)) == 128

    def test_strips_aws_prefix(self):
        assert sanitize_tag_key("aws:my_key") == "my_key"

    def test_strips_aws_prefix_case_insensitive(self):
        assert sanitize_tag_key("AWS:my_key") == "my_key"

    def test_special_chars(self):
        assert sanitize_tag_key("key!val") == "key_val"


# ── AgentPolicyExtractor ────────────────────────────────────────────
class TestAgentPolicyExtractorTags:
    def test_no_agent_policy(self):
        ext = AgentPolicyExtractor()
        assert ext.extract_tags({}) == {}

    def test_allowed_models(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"allowedModels": ["gpt-4", "claude-3"]}}
        tags = ext.extract_tags(contract)
        assert "fluid:agent_allowed_models" in tags
        assert "gpt-4" in tags["fluid:agent_allowed_models"]

    def test_denied_models(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"deniedModels": ["public-llm"]}}
        tags = ext.extract_tags(contract)
        assert "fluid:agent_denied_models" in tags

    def test_prohibited_purposes(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"usageConstraints": {"prohibitedPurposes": ["advertising"]}}}
        tags = ext.extract_tags(contract)
        assert "fluid:agent_prohibited_purposes" in tags

    def test_allowed_purposes(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"usageConstraints": {"allowedPurposes": ["analytics"]}}}
        tags = ext.extract_tags(contract)
        assert "fluid:agent_allowed_purposes" in tags

    def test_requires_approval(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"requiresHumanApproval": True}}
        tags = ext.extract_tags(contract)
        assert tags["fluid:agent_requires_approval"] == "true"

    def test_audit_level(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"auditLevel": "full"}}
        tags = ext.extract_tags(contract)
        assert tags["fluid:agent_audit_level"] == "full"

    def test_rate_limits(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"rateLimits": {"requestsPerDay": 1000, "requestsPerHour": 100}}}
        tags = ext.extract_tags(contract)
        assert tags["fluid:agent_rate_limit_daily"] == "1000"
        assert tags["fluid:agent_rate_limit_hourly"] == "100"

    def test_custom_tags(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"tags": ["custom_tag"]}}
        tags = ext.extract_tags(contract)
        assert "fluid:agent_custom_tag" in tags

    def test_exposure_overrides_contract(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"auditLevel": "basic"}}
        exposure = {"contract": {"agentPolicy": {"auditLevel": "full"}}}
        tags = ext.extract_tags(contract, exposure)
        assert tags["fluid:agent_audit_level"] == "full"


class TestARNParsing:
    def test_extract_account(self):
        ext = AgentPolicyExtractor()
        assert (
            ext._extract_account_from_arn("arn:aws:glue:us-east-1:123456789:table/db/tbl")
            == "123456789"
        )

    def test_extract_account_short(self):
        ext = AgentPolicyExtractor()
        assert ext._extract_account_from_arn("short") == ""

    def test_extract_database(self):
        ext = AgentPolicyExtractor()
        assert (
            ext._extract_database_from_arn("arn:aws:glue:us-east-1:123:table/mydb/mytbl") == "mydb"
        )

    def test_extract_table(self):
        ext = AgentPolicyExtractor()
        assert ext._extract_table_from_arn("arn:aws:glue:us-east-1:123:table/mydb/mytbl") == "mytbl"


class TestLakeFormationPolicy:
    def test_no_policy(self):
        ext = AgentPolicyExtractor()
        assert ext.generate_lake_formation_policy({}, "arn") is None

    def test_not_enforced(self):
        ext = AgentPolicyExtractor()
        contract = {"agentPolicy": {"auditLevel": "full"}}
        assert ext.generate_lake_formation_policy(contract, "arn") is None

    def test_enforced_basic(self):
        ext = AgentPolicyExtractor()
        contract = {
            "id": "test",
            "agentPolicy": {"enforceThroughLakeFormation": True},
        }
        arn = "arn:aws:glue:us-east-1:123:table/mydb/mytbl"
        result = ext.generate_lake_formation_policy(contract, arn)
        assert result is not None
        assert result["Name"] == "AgentPolicy_test"
        assert result["DatabaseName"] == "mydb"
        assert result["TableName"] == "mytbl"

    def test_enforced_with_restricted_columns(self):
        ext = AgentPolicyExtractor()
        contract = {
            "id": "test",
            "agentPolicy": {
                "enforceThroughLakeFormation": True,
                "restrictedColumns": ["ssn", "dob"],
            },
        }
        arn = "arn:aws:glue:us-east-1:123:table/db/tbl"
        result = ext.generate_lake_formation_policy(contract, arn)
        assert result["ColumnWildcard"]["ExcludedColumnNames"] == ["ssn", "dob"]


class TestConvenienceFunctions:
    def test_extract_agent_policy_tags(self):
        tags = extract_agent_policy_tags({"agentPolicy": {"auditLevel": "full"}})
        assert "fluid:agent_audit_level" in tags

    def test_generate_lake_formation_policy_none(self):
        assert generate_lake_formation_policy({}, "arn") is None
