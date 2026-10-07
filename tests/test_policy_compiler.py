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

"""Tests for fluid_build/policy/compiler.py — access-policy → IAM bindings."""

import pytest

from fluid_build.policy.compiler import (
    SAFE_BQ_PERMS,
    SAFE_S3_PERMS,
    SAFE_SNOWFLAKE_PERMS,
    _compile_aws_bindings,
    _compile_gcp_bindings,
    _compile_snowflake_bindings,
    compile_policy,
)


def _contract(platform, fmt, location, grants=None):
    """Build a minimal contract with one expose and one grant."""
    return {
        "accessPolicy": {
            "grants": grants or [{"principal": "user@example.com", "permissions": ["read"]}],
        },
        "exposes": [
            {"binding": {"platform": platform, "format": fmt, "location": location}},
        ],
    }


class TestCompilePolicy:
    def test_no_grants(self):
        bindings, warnings = compile_policy({"accessPolicy": {}})
        assert bindings == []
        assert any("No grants" in w for w in warnings)

    def test_no_access_policy(self):
        bindings, warnings = compile_policy({})
        assert bindings == []

    def test_missing_principal(self):
        contract = {
            "accessPolicy": {"grants": [{"permissions": ["read"]}]},
            "exposes": [
                {
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "ds"},
                    }
                }
            ],
        }
        bindings, warnings = compile_policy(contract)
        assert any("missing principal" in w for w in warnings)

    def test_gcp_bigquery(self):
        c = _contract("gcp", "bigquery_table", {"dataset": "my_ds", "project": "proj1"})
        bindings, warnings = compile_policy(c)
        assert len(bindings) == 1
        b = bindings[0]
        assert b["provider"] == "gcp"
        assert b["resource_type"] == "bigquery.dataset"
        assert b["dataset"] == "my_ds"
        assert b["project"] == "proj1"
        assert b["roles"] == SAFE_BQ_PERMS["readData"]

    def test_gcp_bigquery_write(self):
        c = _contract(
            "gcp",
            "bigquery_table",
            {"dataset": "ds", "project": "p"},
            grants=[{"principal": "sa@gcp", "permissions": ["write"]}],
        )
        bindings, _ = compile_policy(c)
        assert bindings[0]["roles"] == SAFE_BQ_PERMS["manage"]

    def test_gcp_gcs(self):
        c = _contract("gcp", "gcs_parquet_files", {"bucket": "my-bucket"})
        bindings, _ = compile_policy(c)
        assert len(bindings) == 1
        assert bindings[0]["resource_type"] == "gcs.bucket"
        assert bindings[0]["bucket"] == "my-bucket"

    def test_aws_s3(self):
        c = _contract("aws", "s3_file", {"bucket": "s3-bkt", "region": "us-east-1"})
        bindings, _ = compile_policy(c)
        assert len(bindings) == 1
        assert bindings[0]["provider"] == "aws"
        assert bindings[0]["resource_type"] == "s3.bucket"
        assert bindings[0]["actions"] == SAFE_S3_PERMS["readData"]

    def test_aws_glue(self):
        c = _contract(
            "aws",
            "iceberg",
            {"bucket": "bkt", "database": "mydb", "table": "tbl", "region": "eu-west-1"},
        )
        bindings, _ = compile_policy(c)
        # Should produce both S3 and Glue bindings
        assert len(bindings) == 2
        types = {b["resource_type"] for b in bindings}
        assert "s3.bucket" in types
        assert "glue.table" in types

    def test_aws_write_permissions(self):
        c = _contract(
            "aws",
            "s3_file",
            {"bucket": "bkt"},
            grants=[{"principal": "role/x", "permissions": ["insert"]}],
        )
        bindings, _ = compile_policy(c)
        assert bindings[0]["actions"] == SAFE_S3_PERMS["manage"]

    def test_snowflake(self):
        c = _contract(
            "snowflake", "snowflake_table", {"database": "DB", "schema": "SCH", "table": "T"}
        )
        bindings, _ = compile_policy(c)
        assert len(bindings) == 1
        b = bindings[0]
        assert b["provider"] == "snowflake"
        assert b["resource_type"] == "snowflake.table"
        assert b["resource_id"] == "DB.SCH.T"
        assert b["grants"] == SAFE_SNOWFLAKE_PERMS["readData"]

    def test_snowflake_schema_only(self):
        c = _contract("snowflake", "snowflake_table", {"database": "DB", "schema": "SCH"})
        bindings, _ = compile_policy(c)
        assert bindings[0]["resource_type"] == "snowflake.schema"
        assert bindings[0]["resource_id"] == "DB.SCH"

    def test_unsupported_platform(self):
        c = _contract("azure", "blob", {"container": "x"})
        bindings, warnings = compile_policy(c)
        assert bindings == []
        assert any("Unsupported" in w for w in warnings)

    def test_multiple_grants(self):
        contract = {
            "accessPolicy": {
                "grants": [
                    {"principal": "a@b.com", "permissions": ["read"]},
                    {"principal": "c@d.com", "permissions": ["write"]},
                ],
            },
            "exposes": [
                {
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "ds"},
                    }
                },
            ],
        }
        bindings, _ = compile_policy(contract)
        assert len(bindings) == 2
        principals = {b["principal"] for b in bindings}
        assert principals == {"a@b.com", "c@d.com"}

    def test_no_bindings_warning(self):
        contract = {
            "accessPolicy": {"grants": [{"principal": "x@y.com", "permissions": ["read"]}]},
            "exposes": [{"binding": {"platform": "unknown", "format": "unknown", "location": {}}}],
        }
        _, warnings = compile_policy(contract)
        assert any("No IAM bindings" in w for w in warnings)


class TestIcebergCatalogRouting:
    """An Iceberg expose is granted where its catalog lives.

    Every ``iceberg`` expose used to route to the AWS compiler on any
    platform, so a Lakekeeper table, a Snowflake-managed table and a
    ``platform: local`` table each got a ``glue.table`` grant on a Glue table
    that does not exist (and ``policy-apply`` took ``provider: aws`` from it).
    """

    _LOC = {"bucket": "bkt", "database": "mydb", "table": "tbl", "region": "eu-west-1"}

    @staticmethod
    def _types(bindings):
        return sorted(b["resource_type"] for b in bindings)

    @staticmethod
    def _catalog_warnings(warnings):
        return [w for w in warnings if "cataloged in" in w]

    @pytest.mark.parametrize("catalog", [None, "glue", "GLUE"])
    def test_glue_catalog_keeps_s3_and_glue_grants(self, catalog):
        loc = dict(self._LOC, **({"catalog": catalog} if catalog else {}))
        bindings, warnings = compile_policy(_contract("aws", "iceberg", loc))
        assert self._types(bindings) == ["glue.table", "s3.bucket"]
        assert self._catalog_warnings(warnings) == []

    @pytest.mark.parametrize("catalog", ["lakekeeper", "Lakekeeper", "iceberg_rest", "polaris"])
    def test_rest_catalog_on_aws_gets_no_glue_grant(self, catalog):
        loc = dict(self._LOC, catalog=catalog)
        bindings, warnings = compile_policy(_contract("aws", "iceberg", loc))
        # The bucket is still the table's storage; the Glue table does not exist.
        assert self._types(bindings) == ["s3.bucket"]
        (warning,) = self._catalog_warnings(warnings)
        assert "user@example.com" in warning and "['read']" in warning
        assert "not AWS Glue" in warning

    def test_rest_catalog_warning_names_the_canonical_kind(self):
        loc = dict(self._LOC, catalog="LakeKeeper")
        _, warnings = compile_policy(_contract("aws", "iceberg", loc))
        assert "'lakekeeper'" in self._catalog_warnings(warnings)[0]

    def test_rest_catalog_without_bucket_compiles_nothing_and_says_so(self):
        loc = {"database": "mydb", "table": "tbl", "catalog": "lakekeeper"}
        bindings, warnings = compile_policy(_contract("aws", "iceberg", loc))
        assert bindings == []
        assert len(self._catalog_warnings(warnings)) == 1
        assert any("No IAM bindings" in w for w in warnings)

    def test_snowflake_managed_iceberg_gets_snowflake_rbac(self):
        loc = {"database": "DB", "schema": "SCH", "table": "T"}
        bindings, warnings = compile_policy(_contract("snowflake", "iceberg", loc))
        assert self._types(bindings) == ["snowflake.table"]
        assert bindings[0]["grants"] == SAFE_SNOWFLAKE_PERMS["readData"]
        # Snowflake IS the catalog: its grant is the whole enforcement.
        assert self._catalog_warnings(warnings) == []

    def test_external_catalog_on_snowflake_warns_grant_covers_snowflake_only(self):
        loc = {"database": "DB", "schema": "SCH", "table": "T", "catalog": "lakekeeper"}
        bindings, warnings = compile_policy(_contract("snowflake", "iceberg", loc))
        assert self._types(bindings) == ["snowflake.table"]
        (warning,) = self._catalog_warnings(warnings)
        assert "Snowflake readers only" in warning

    @pytest.mark.parametrize("platform", ["local", "azure"])
    def test_non_aws_platform_gets_no_aws_grant(self, platform):
        bindings, warnings = compile_policy(_contract(platform, "iceberg", dict(self._LOC)))
        assert [b for b in bindings if b["provider"] == "aws"] == []
        assert len(self._catalog_warnings(warnings)) == 1

    def test_one_warning_per_grant_and_expose(self):
        grants = [
            {"principal": "a@b.com", "permissions": ["read"]},
            {"principal": "c@d.com", "permissions": ["write"]},
        ]
        loc = dict(self._LOC, catalog="lakekeeper")
        _, warnings = compile_policy(_contract("aws", "iceberg", loc, grants=grants))
        catalog_warnings = self._catalog_warnings(warnings)
        assert len(catalog_warnings) == 2
        assert "a@b.com" in catalog_warnings[0] and "c@d.com" in catalog_warnings[1]

    def test_non_iceberg_format_ignores_the_catalog_key(self):
        loc = dict(self._LOC, catalog="lakekeeper")
        bindings, warnings = compile_policy(_contract("aws", "parquet", loc))
        assert self._types(bindings) == ["glue.table", "s3.bucket"]
        assert self._catalog_warnings(warnings) == []


class TestGcpBindingsInternal:
    def test_bigquery_no_dataset(self):
        bindings = []
        _compile_gcp_bindings(bindings, "bigquery_table", {}, "user@a.com", ["read"])
        assert bindings == []  # no dataset → no binding

    def test_gcs_no_bucket(self):
        bindings = []
        _compile_gcp_bindings(bindings, "gcs_file", {}, "user@a.com", ["read"])
        assert bindings == []


class TestAwsBindingsInternal:
    def test_no_bucket(self):
        bindings = []
        _compile_aws_bindings(bindings, "s3_file", {}, "user@a.com", ["read"])
        assert bindings == []

    def test_database_alias(self):
        bindings = []
        _compile_aws_bindings(bindings, "iceberg", {"dataset": "myds"}, "user@a.com", ["read"])
        # dataset should alias to database
        glue = [b for b in bindings if b["resource_type"] == "glue.table"]
        assert len(glue) == 1
        assert glue[0]["database"] == "myds"

    def test_glue_false_emits_only_the_s3_statement(self):
        bindings = []
        loc = {"bucket": "bkt", "database": "db", "table": "t"}
        _compile_aws_bindings(bindings, "iceberg", loc, "user@a.com", ["read"], glue=False)
        assert [b["resource_type"] for b in bindings] == ["s3.bucket"]


class TestSnowflakeBindingsInternal:
    def test_no_database(self):
        bindings = []
        _compile_snowflake_bindings(bindings, "snowflake_table", {}, "user@a.com", ["read"])
        assert bindings == []

    def test_write_grants(self):
        bindings = []
        _compile_snowflake_bindings(
            bindings, "snowflake_table", {"database": "DB"}, "u@x.com", ["delete"]
        )
        assert bindings[0]["grants"] == SAFE_SNOWFLAKE_PERMS["manage"]
