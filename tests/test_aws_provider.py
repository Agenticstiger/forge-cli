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

"""Tests for AWS provider spec converters, planning, and SQL generation."""

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers.aws.types import (
    APISpec,
    AuthenticationMethod,
    AWSRegion,
    ModelSpec,
    StorageClass,
    StreamSpec,
    TableSpec,
)


class TestAWSTypes:
    """Test AWS type enums and dataclasses."""

    def test_aws_regions(self):
        assert AWSRegion.US_EAST_1.value == "us-east-1"
        assert AWSRegion.EU_WEST_1.value == "eu-west-1"
        assert AWSRegion.AP_NORTHEAST_1.value == "ap-northeast-1"

    def test_auth_methods(self):
        assert AuthenticationMethod.IAM_ROLE.value == "iam_role"
        assert AuthenticationMethod.SSO.value == "sso"
        assert AuthenticationMethod.ASSUME_ROLE.value == "assume_role"

    def test_storage_classes(self):
        assert StorageClass.STANDARD.value == "STANDARD"
        assert StorageClass.GLACIER.value == "GLACIER"
        assert StorageClass.DEEP_ARCHIVE.value == "DEEP_ARCHIVE"

    def test_table_spec_defaults(self):
        spec = TableSpec(name="test", service="redshift")
        assert spec.encryption_enabled is True
        assert spec.backup_enabled is True
        assert spec.columns == []
        assert spec.primary_keys == []

    def test_api_spec_defaults(self):
        spec = APISpec(name="test-api")
        assert spec.protocol_type == "REST"
        assert spec.stages == []

    def test_stream_spec_defaults(self):
        spec = StreamSpec(name="test-stream", service="kinesis")
        assert spec.retention_period == 24
        assert spec.encryption_enabled is True

    def test_model_spec_defaults(self):
        spec = ModelSpec(name="test-model")
        assert spec.instance_type == "ml.t2.medium"
        assert spec.instance_count == 1


def _make_provider():
    """Create a mock AWSProvider for testing pure methods."""
    # Import the class to get a proper instance
    from fluid_build.providers.aws.aws import AWSProvider

    with patch.object(AWSProvider, "__init__", lambda self, config: None):
        p = AWSProvider.__new__(AWSProvider)
        p.logger = MagicMock()

        # Mock options with nested attributes
        p.options = MagicMock()
        p.options.region = AWSRegion.US_EAST_1
        p.options.monitoring.cloudwatch_enabled = True
        p.options.services.redshift = MagicMock()
        p.options.services.redshift.cluster_identifier = "test-cluster"
        p.options.services.redshift.database_name = "testdb"
        return p


class TestContractToTableSpec:
    def test_basic_expose(self):
        p = _make_provider()
        expose = {
            "id": "users",
            "location": {
                "properties": {
                    "service": "redshift",
                    "columns": [{"name": "id", "type": "INTEGER"}],
                    "primary_keys": ["id"],
                }
            },
        }
        spec = p._contract_to_table_spec(expose)
        assert spec.name == "users"
        assert spec.service == "redshift"
        assert len(spec.columns) == 1
        assert spec.primary_keys == ["id"]

    def test_defaults(self):
        p = _make_provider()
        expose = {"name": "fallback"}
        spec = p._contract_to_table_spec(expose)
        assert spec.name == "fallback"
        assert spec.service == "redshift"
        assert spec.encryption_enabled is True

    def test_with_distribution_and_sort(self):
        p = _make_provider()
        expose = {
            "id": "events",
            "location": {
                "properties": {
                    "distribution_key": "event_date",
                    "sort_keys": ["event_date", "event_type"],
                    "partition_keys": ["region"],
                }
            },
        }
        spec = p._contract_to_table_spec(expose)
        assert spec.distribution_key == "event_date"
        assert spec.sort_keys == ["event_date", "event_type"]
        assert spec.partition_keys == ["region"]


class TestContractToAPISpec:
    def test_basic(self):
        p = _make_provider()
        expose = {
            "id": "my-api",
            "description": "Test API",
            "location": {
                "properties": {
                    "protocol_type": "HTTP",
                    "stages": ["dev", "prod"],
                }
            },
        }
        spec = p._contract_to_api_spec(expose)
        assert spec.name == "my-api"
        assert spec.description == "Test API"
        assert spec.protocol_type == "HTTP"
        assert len(spec.stages) == 2
        assert spec.stages[0]["stage_name"] == "dev"

    def test_default_stage(self):
        p = _make_provider()
        expose = {"id": "api", "location": {"properties": {}}}
        spec = p._contract_to_api_spec(expose)
        assert len(spec.stages) == 1
        assert spec.stages[0]["stage_name"] == "prod"


class TestContractToStreamSpec:
    def test_basic(self):
        p = _make_provider()
        expose = {
            "id": "events-stream",
            "location": {
                "properties": {
                    "service": "kinesis",
                    "shard_count": 4,
                    "retention_period": 48,
                }
            },
        }
        spec = p._contract_to_stream_spec(expose)
        assert spec.name == "events-stream"
        assert spec.service == "kinesis"
        assert spec.shard_count == 4
        assert spec.retention_period == 48


class TestContractToModelSpec:
    def test_basic(self):
        p = _make_provider()
        expose = {
            "id": "fraud-model",
            "description": "Fraud detection",
            "location": {
                "properties": {
                    "model_artifacts": "s3://bucket/model.tar.gz",
                    "inference_image": "123456.ecr.amazonaws.com/fraud:latest",
                    "instance_type": "ml.m5.xlarge",
                    "instance_count": 2,
                }
            },
        }
        spec = p._contract_to_model_spec(expose)
        assert spec.name == "fraud-model"
        assert spec.instance_type == "ml.m5.xlarge"
        assert spec.instance_count == 2


class TestPlanTable:
    def test_redshift(self):
        p = _make_provider()
        table = TableSpec(name="test", service="redshift")
        plan = p._plan_table(table)
        assert plan["type"] == "table"
        assert plan["service"] == "redshift"
        assert any(a["type"] == "create_table" for a in plan["actions"])

    def test_athena(self):
        p = _make_provider()
        table = TableSpec(name="test", service="athena")
        plan = p._plan_table(table)
        assert any(a["type"] == "create_athena_table" for a in plan["actions"])

    def test_dynamodb(self):
        p = _make_provider()
        table = TableSpec(name="test", service="dynamodb")
        plan = p._plan_table(table)
        assert any(a["type"] == "create_dynamodb_table" for a in plan["actions"])

    def test_unsupported_service(self):
        p = _make_provider()
        table = TableSpec(name="test", service="unknown")
        with pytest.raises(ValueError, match="Unsupported table service"):
            p._plan_table(table)


class TestPlanRedshiftTable:
    def test_actions(self):
        p = _make_provider()
        table = TableSpec(name="test", service="redshift", encryption_enabled=True)
        result = p._plan_redshift_table(table)
        action_types = [a["type"] for a in result["actions"]]
        assert "check_cluster" in action_types
        assert "create_database" in action_types
        assert "create_table" in action_types
        assert "setup_monitoring" in action_types
        assert result["cost_estimate"] == 50.0

    def test_security_requirements_with_encryption(self):
        p = _make_provider()
        table = TableSpec(name="test", service="redshift", encryption_enabled=True)
        result = p._plan_redshift_table(table)
        assert "vpc_access" in result["security_requirements"]

    def test_security_requirements_without_encryption(self):
        p = _make_provider()
        table = TableSpec(name="test", service="redshift", encryption_enabled=False)
        result = p._plan_redshift_table(table)
        assert "vpc_access" not in result["security_requirements"]


class TestPlanAthenaTable:
    def test_basic(self):
        p = _make_provider()
        table = TableSpec(name="test", service="athena")
        result = p._plan_athena_table(table)
        assert result["cost_estimate"] == 5.0
        assert "s3_access" in result["security_requirements"]

    def test_with_partitioning(self):
        p = _make_provider()
        table = TableSpec(name="test", service="athena", partition_keys=["dt"])
        result = p._plan_athena_table(table)
        action_types = [a["type"] for a in result["actions"]]
        assert "setup_partitioning" in action_types


class TestPlanDynamoDBTable:
    def test_with_backup(self):
        p = _make_provider()
        table = TableSpec(name="test", service="dynamodb", backup_enabled=True)
        result = p._plan_dynamodb_table(table)
        action_types = [a["type"] for a in result["actions"]]
        assert "enable_backup" in action_types

    def test_without_backup(self):
        p = _make_provider()
        table = TableSpec(name="test", service="dynamodb", backup_enabled=False)
        result = p._plan_dynamodb_table(table)
        action_types = [a["type"] for a in result["actions"]]
        assert "enable_backup" not in action_types


class TestGenerateRedshiftSQL:
    def test_basic(self):
        p = _make_provider()
        table = TableSpec(
            name="users",
            service="redshift",
            columns=[
                {"name": "id", "type": "INTEGER", "nullable": False},
                {"name": "name", "type": "VARCHAR(100)"},
            ],
        )
        sql = p._generate_redshift_sql(table)
        assert "CREATE TABLE IF NOT EXISTS users" in sql
        assert "id INTEGER NOT NULL" in sql
        assert "name VARCHAR(100)" in sql

    def test_with_distribution_key(self):
        p = _make_provider()
        table = TableSpec(
            name="events",
            service="redshift",
            columns=[{"name": "id", "type": "INT"}],
            distribution_key="id",
        )
        sql = p._generate_redshift_sql(table)
        assert "DISTKEY(id)" in sql

    def test_with_sort_keys(self):
        p = _make_provider()
        table = TableSpec(
            name="events",
            service="redshift",
            columns=[{"name": "id", "type": "INT"}],
            sort_keys=["created_at", "region"],
        )
        sql = p._generate_redshift_sql(table)
        assert "SORTKEY(created_at, region)" in sql

    def test_with_default(self):
        p = _make_provider()
        table = TableSpec(
            name="t",
            service="redshift",
            columns=[{"name": "status", "type": "VARCHAR", "default": "'active'"}],
        )
        sql = p._generate_redshift_sql(table)
        assert "DEFAULT 'active'" in sql


class TestPlanResource:
    def test_routes_table(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift")
        plan = p._plan_resource(table)
        assert "actions" in plan

    def test_routes_api(self):
        p = _make_provider()
        api = APISpec(name="a")
        plan = p._plan_resource(api)
        assert plan["type"] == "api"

    def test_routes_stream(self):
        p = _make_provider()
        stream = StreamSpec(name="s", service="kinesis")
        plan = p._plan_resource(stream)
        assert plan["type"] == "stream"

    def test_routes_model(self):
        p = _make_provider()
        model = ModelSpec(name="m")
        plan = p._plan_resource(model)
        assert plan["type"] == "model"

    def test_unknown_type_raises(self):
        p = _make_provider()
        with pytest.raises(ValueError, match="Unsupported resource type"):
            p._plan_resource("not_a_spec")


class TestPlanAPI:
    def test_actions(self):
        p = _make_provider()
        api = APISpec(name="test-api")
        result = p._plan_api(api)
        action_types = [a["type"] for a in result["actions"]]
        assert "create_api_gateway" in action_types
        assert "configure_cors" in action_types
        assert "setup_authentication" in action_types
        assert "deploy_api" in action_types
        assert result["cost_estimate"] == 15.0


class TestPlanStream:
    def test_kinesis(self):
        p = _make_provider()
        stream = StreamSpec(name="events", service="kinesis")
        result = p._plan_stream(stream)
        action_types = [a["type"] for a in result["actions"]]
        assert "create_kinesis_stream" in action_types

    def test_msk(self):
        p = _make_provider()
        stream = StreamSpec(name="events", service="msk")
        result = p._plan_stream(stream)
        action_types = [a["type"] for a in result["actions"]]
        assert "create_msk_cluster" in action_types
        assert "create_kafka_topic" in action_types


class TestPlanModel:
    def test_actions(self):
        p = _make_provider()
        model = ModelSpec(name="fraud")
        result = p._plan_model(model)
        action_types = [a["type"] for a in result["actions"]]
        assert "create_sagemaker_model" in action_types
        assert "create_endpoint_config" in action_types
        assert "deploy_endpoint" in action_types
        assert "setup_monitoring" in action_types
        assert result["cost_estimate"] == 100.0


class TestAssessSecurity:
    def test_empty_resources(self):
        p = _make_provider()
        result = p._assess_security([])
        assert result["encryption_required"] is False
        assert result["security_score"] == 50.0

    def test_encrypted_resource(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift", encryption_enabled=True)
        result = p._assess_security([table])
        assert result["encryption_required"] is True
        assert result["security_score"] == 100.0  # 50 + 30 + 20

    def test_redshift_iam_policies(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift")
        result = p._assess_security([table])
        assert "redshift:DescribeClusters" in result["iam_policies_needed"]
        assert "redshift:GetClusterCredentials" in result["iam_policies_needed"]

    def test_dynamodb_iam_policies(self):
        p = _make_provider()
        table = TableSpec(name="t", service="dynamodb")
        result = p._assess_security([table])
        assert "dynamodb:GetItem" in result["iam_policies_needed"]

    def test_no_encryption_lower_score(self):
        p = _make_provider()
        api = APISpec(name="a")
        result = p._assess_security([api])
        assert result["encryption_required"] is False
        assert result["security_score"] == 50.0


class TestCheckCompliance:
    def test_compliant_resources(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift")
        result = p._check_compliance([table])
        assert result["gdpr_compliant"] is True
        assert result["hipaa_compliant"] is True
        assert result["sox_compliant"] is True
        assert result["issues"] == []

    def test_no_encryption_breaks_compliance(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift", encryption_enabled=False)
        result = p._check_compliance([table])
        assert result["gdpr_compliant"] is False
        assert result["hipaa_compliant"] is False
        assert len(result["issues"]) > 0

    def test_no_backup_breaks_sox(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift", backup_enabled=False)
        result = p._check_compliance([table])
        assert result["sox_compliant"] is False


class TestAnalyzeCosts:
    def test_empty(self):
        p = _make_provider()
        result = p._analyze_costs([])
        assert result["monthly_estimate"] == 0.0
        assert result["annual_estimate"] == 0.0

    def test_redshift_cost(self):
        p = _make_provider()
        table = TableSpec(name="t", service="redshift")
        result = p._analyze_costs([table])
        assert result["monthly_estimate"] == 50.0
        assert result["annual_estimate"] == 600.0
        assert result["cost_breakdown"]["t"] == 50.0

    def test_mixed_resources(self):
        p = _make_provider()
        resources = [
            TableSpec(name="t1", service="redshift"),
            APISpec(name="a1"),
            StreamSpec(name="s1", service="kinesis"),
            ModelSpec(name="m1"),
        ]
        result = p._analyze_costs(resources)
        assert result["monthly_estimate"] == 195.0  # 50+15+30+100
        assert "Consider Reserved Instances" in result["optimization_opportunities"][0]
        assert "Kinesis shard sizing" in result["optimization_opportunities"][1]

    def test_dynamodb_cost(self):
        p = _make_provider()
        table = TableSpec(name="t", service="dynamodb")
        result = p._analyze_costs([table])
        assert result["cost_breakdown"]["t"] == 25.0

    def test_athena_cost(self):
        p = _make_provider()
        table = TableSpec(name="t", service="athena")
        result = p._analyze_costs([table])
        assert result["cost_breakdown"]["t"] == 5.0
