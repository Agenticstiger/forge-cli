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

"""The native AWS planner provisions Glue only for a Glue-cataloged table.

``catalog: lakekeeper`` on an AWS Iceberg expose used to stream over the
Iceberg REST protocol while this planner still emitted
``glue.ensure_database`` + ``glue.ensure_iceberg_table`` for the same name: a
second, metadata-less claim on a table the REST catalog owns. The planner now
reads the shared ``is_glue_cataloged`` classification. An absent catalog on
AWS is Glue, so those contracts must plan exactly as before.
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.providers.aws.plan.planner import plan_actions

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCT = "123456789012"
REGION = "us-east-1"
STAGING = f"{ACCT}-fluid-staging"

_LAKEKEEPER = {
    "catalog": "lakekeeper",
    "uri": "http://lakekeeper:8181/catalog",
    "warehouse": "demo",
}


def _contract(location, *, fmt="iceberg", platform="aws", policies=None, extra_exposes=()):
    contract = {
        "id": "analytics.lake",
        "name": "Lake",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": platform, "format": fmt, "location": location},
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            },
            *extra_exposes,
        ],
    }
    if policies:
        contract["metadata"] = {"policies": policies}
    return contract


def _ops(contract):
    return [
        (a["op"], a.get("database") or a.get("bucket"))
        for a in plan_actions(contract, ACCT, REGION, logging.getLogger("t"))
    ]


def _glue_ops(contract):
    return [op for op, _ in _ops(contract) if op.startswith(("glue.", "iam.bind_glue"))]


class TestGlueCatalogUnchanged:
    def test_absent_catalog_plans_the_glue_database_and_iceberg_table(self):
        loc = {"database": "sales", "table": "orders", "bucket": "lake"}
        assert _ops(_contract(loc)) == [
            ("glue.ensure_database", "sales"),
            ("s3.ensure_bucket", "lake"),
            ("s3.ensure_bucket", STAGING),
            ("glue.ensure_iceberg_table", "sales"),
        ]

    @pytest.mark.parametrize("spelling", ["glue", "GLUE"])
    def test_explicit_glue_plans_identically_to_absent(self, spelling):
        loc = {"database": "sales", "table": "orders", "bucket": "lake"}
        absent = plan_actions(_contract(dict(loc)), ACCT, REGION)
        explicit = plan_actions(_contract({**loc, "catalog": spelling}), ACCT, REGION)
        assert explicit == absent

    def test_non_iceberg_format_ignores_the_catalog(self):
        # ``catalog`` is an Iceberg concept: a parquet table is Glue-cataloged
        # whatever the key says, so its plan must not change.
        loc = {"database": "sales", "table": "orders", "bucket": "lake", "catalog": "lakekeeper"}
        assert ("glue.ensure_table", "sales") in _ops(_contract(loc, fmt="parquet"))


class TestNonGlueIcebergCatalog:
    @pytest.mark.parametrize(
        "catalog", ["lakekeeper", "LakeKeeper", "rest", "iceberg_rest", "polaris", "nessie"]
    )
    def test_no_glue_database_or_table(self, catalog):
        loc = {"database": "sales", "table": "orders", "bucket": "lake", "catalog": catalog}
        assert _glue_ops(_contract(loc)) == []

    def test_declared_bucket_is_still_provisioned(self):
        loc = {"database": "sales", "table": "orders", "bucket": "lake", **_LAKEKEEPER}
        assert _ops(_contract(loc)) == [
            ("s3.ensure_bucket", "lake"),
            ("s3.ensure_bucket", STAGING),
        ]

    def test_no_fallback_data_bucket_without_a_declared_one(self):
        # ``{account}-fluid-data`` only ever backed the Glue database's
        # location, and there is no Glue database here.
        loc = {"database": "sales", "table": "orders", **_LAKEKEEPER}
        assert _ops(_contract(loc)) == [("s3.ensure_bucket", STAGING)]

    @pytest.mark.parametrize("platform", ["glue", "athena", "AWS"])
    def test_every_aws_platform_spelling(self, platform):
        loc = {"database": "sales", "table": "orders", **_LAKEKEEPER}
        assert _glue_ops(_contract(loc, platform=platform)) == []

    def test_iceberg_table_alias_format(self):
        loc = {"database": "sales", "table": "orders", **_LAKEKEEPER}
        assert _glue_ops(_contract(loc, fmt="iceberg-table")) == []

    def test_glue_expose_sharing_the_database_still_gets_it(self):
        # The skipped Lakekeeper expose must not mark ``sales`` as created.
        parquet = {
            "exposeId": "raw",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"database": "sales", "table": "raw", "bucket": "lake"},
            },
        }
        loc = {"database": "sales", "table": "orders", "bucket": "lake", **_LAKEKEEPER}
        ops = _ops(_contract(loc, extra_exposes=[parquet]))
        assert ops.count(("glue.ensure_database", "sales")) == 1
        assert ops.count(("s3.ensure_bucket", "lake")) == 1
        assert ("glue.ensure_table", "sales") in ops
        assert ("glue.ensure_iceberg_table", "sales") not in ops


class TestIamPolicies:
    _POLICIES = {"read": ["analyst"]}

    def test_glue_binding_still_binds_the_database(self):
        contract = _contract({"table": "orders"}, policies=self._POLICIES)
        contract["exposes"][0]["binding"]["database"] = "sales"
        assert ("iam.bind_glue_database", "sales") in _ops(contract)

    def test_lakekeeper_binding_skips_the_glue_bind_and_says_so(self, caplog):
        contract = _contract({"table": "orders", **_LAKEKEEPER}, policies=self._POLICIES)
        contract["exposes"][0]["binding"]["database"] = "sales"
        with caplog.at_level(logging.WARNING, logger="t"):
            ops = _ops(contract)
        assert not [op for op, _ in ops if op.startswith("iam.")]
        assert any(
            "glue_iam_binding_skipped" in r.getMessage() and "lakekeeper" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
