# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 1 unit tests — cross-account (AWS) + cross-project (GCP) emit.

The full ladder is exercised in:

  * Stage 1 (this file)   — pure-function emit, schema validation.
  * Stage 2               — LocalStack apply round-trip
                            (``test_iac_aws_localstack_e2e.py``).
  * Stage 3 same-principal proxy — real cloud, assume-role / impersonate
                            (``test_iac_aws_real_cross_account_e2e.py``,
                             ``test_iac_gcp_real_cross_project_e2e.py``).

**Zero new schema fields** — the cross-account / cross-project
capabilities reuse the contract surface that already exists:

  * AWS: ``binding.governance.lakeFormation.grants[].principal``
    already accepts arbitrary IAM ARNs (the v0.7.3 schema's principal
    pattern is ``^arn:aws[a-z0-9-]*:iam::`` which matches any account
    number). Any LF grant on a Glue-catalog-backed S3 binding ALSO
    triggers an ``aws_s3_bucket_policy`` for the same principal — LF
    alone does not authorise object reads (see the AWS LF
    cross-account FAQ + Komminar's Terraform article). The bucket
    policy is benign for in-account principals (additive on top of
    their IAM read).
  * GCP: cross-project SAs ride the existing ``metadata.policies``
    surface. ``_bq_access_entries`` already maps the policy entries
    to ``user_by_email`` on the dataset's ``access[]`` block, and BQ's
    ``user_by_email`` accepts service-account emails from other
    projects verbatim.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit, pytest.mark.provider]


# ── helpers ──────────────────────────────────────────────────────────────


def _aws_contract(*, principal: str, bucket: str = "fluid-iactest-xacc-demo"):
    """Contract with a single LF grant. The bucket policy is emitted
    automatically — no opt-in flag exists in the schema."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.aws.xacc.demo",
        "name": "X-acc demo",
        "domain": "ledger",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng", "email": "x@x.co"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": bucket,
                        "path": "orders/",
                        "database": "fluid_iactest_db",
                        "table": "orders",
                        "region": "eu-west-1",
                    },
                    "governance": {
                        "lakeFormation": {
                            "grants": [
                                {
                                    "principal": principal,
                                    "permissions": ["SELECT", "DESCRIBE"],
                                }
                            ]
                        }
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _gcp_contract(policies):
    """Contract using the existing ``metadata.policies`` surface to
    grant access — including to service accounts in other projects."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.gcp.xproj.demo",
        "name": "X-proj demo",
        "domain": "ledger",
        "metadata": {
            "layer": "Silver",
            "owner": {"team": "data-eng", "email": "x@x.co"},
            "policies": policies,
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": "fluid_iactest_ds",
                        "table": "orders",
                        "region": "EU",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


# ── AWS Stage 1 ──────────────────────────────────────────────────────────


class TestAwsCrossAccountEmit:
    """Any IAM-principal LF grant on a Glue-S3 binding automatically
    emits a matching ``aws_s3_bucket_policy``. Zero new schema fields
    — uses the existing ``governance.lakeFormation.grants[]`` block."""

    def test_lf_grant_lands_in_principal(self):
        contract = _aws_contract(principal="arn:aws:iam::222222222222:role/consumer")
        res = get_iac_plugin("aws").emit(contract, [])
        grants = res.get("aws_lakeformation_permissions", {})
        assert len(grants) == 1
        body = next(iter(grants.values()))
        assert body["principal"] == "arn:aws:iam::222222222222:role/consumer"

    def test_lf_grant_emits_companion_bucket_policy(self):
        contract = _aws_contract(principal="arn:aws:iam::222222222222:role/consumer")
        res = get_iac_plugin("aws").emit(contract, [])
        policies = res.get("aws_s3_bucket_policy", {})
        assert (
            len(policies) == 1
        ), "every IAM-principal LF grant pairs with one bucket-policy resource"
        body = next(iter(policies.values()))
        assert body["bucket"].startswith(
            "${aws_s3_bucket."
        ), f"bucket should be a tofu ref; got {body['bucket']!r}"
        doc = json.loads(body["policy"])
        assert doc["Version"] == "2012-10-17"
        stmts = {s["Sid"]: s for s in doc["Statement"]}
        assert "FluidLfBucketList0" in stmts
        assert "FluidLfBucketGet0" in stmts
        # List on the bucket ARN; Get on the per-object ARN.
        assert stmts["FluidLfBucketList0"]["Resource"] == "arn:aws:s3:::fluid-iactest-xacc-demo"
        assert (
            stmts["FluidLfBucketGet0"]["Resource"]
            == "arn:aws:s3:::fluid-iactest-xacc-demo/orders/*"
        )
        # Both statements target the same principal.
        assert stmts["FluidLfBucketList0"]["Principal"]["AWS"] == (
            "arn:aws:iam::222222222222:role/consumer"
        )
        assert stmts["FluidLfBucketGet0"]["Principal"]["AWS"] == (
            "arn:aws:iam::222222222222:role/consumer"
        )

    def test_multiple_principals_share_one_bucket_policy(self):
        contract = _aws_contract(principal="arn:aws:iam::222222222222:role/consumer-a")
        contract["exposes"][0]["binding"]["governance"]["lakeFormation"]["grants"].append(
            {
                "principal": "arn:aws:iam::333333333333:role/consumer-b",
                "permissions": ["SELECT"],
            }
        )
        res = get_iac_plugin("aws").emit(contract, [])
        policies = res.get("aws_s3_bucket_policy", {})
        # Single policy resource — one per bucket — with statements for both principals.
        assert len(policies) == 1
        body = next(iter(policies.values()))
        doc = json.loads(body["policy"])
        sids = sorted(s["Sid"] for s in doc["Statement"])
        assert sids == [
            "FluidLfBucketGet0",
            "FluidLfBucketGet1",
            "FluidLfBucketList0",
            "FluidLfBucketList1",
        ]
        principals = {s["Principal"]["AWS"] for s in doc["Statement"]}
        assert principals == {
            "arn:aws:iam::222222222222:role/consumer-a",
            "arn:aws:iam::333333333333:role/consumer-b",
        }

    def test_no_lf_grants_no_bucket_policy(self):
        # A contract with NO LF grants emits no bucket policy. Existing
        # contracts without a governance block stay unaffected.
        contract = _aws_contract(principal="arn:aws:iam::222222222222:role/c")
        del contract["exposes"][0]["binding"]["governance"]
        res = get_iac_plugin("aws").emit(contract, [])
        assert "aws_s3_bucket_policy" not in res
        assert "aws_lakeformation_permissions" not in res

    def test_schema_validates_lf_grant_without_extra_fields(self):
        # The contract used by these tests has ZERO new fields — the LF
        # grant block was already in the 0.7.3 schema. Confirm the
        # contract validates cleanly so the schema delta really is
        # nothing more than the existing LF surface.
        contract = _aws_contract(principal="arn:aws:iam::222222222222:role/consumer")
        result = FluidSchemaManager().validate_contract(contract)
        assert result.is_valid, (
            "schema rejected an unchanged LF-grant contract — "
            "regression in the LF schema. errors:\n" + "\n".join(result.errors)
        )


# ── GCP Stage 1 ──────────────────────────────────────────────────────────


class TestGcpCrossProjectEmit:
    """Cross-project SA access uses the existing ``metadata.policies``
    surface — no new schema fields. The plugin's ``_bq_access_entries``
    helper maps each policy entry to a ``user_by_email`` row on the
    dataset's ``access[]`` block, and BQ accepts cross-project SA
    emails via the ``user_by_email`` field."""

    def test_cross_project_sa_lands_in_dataset_access(self):
        contract = _gcp_contract(
            {
                "consumers": {
                    "principals": [
                        "consumer@other-project.iam.gserviceaccount.com",
                    ],
                    "permissions": ["read"],
                }
            }
        )
        res = get_iac_plugin("gcp").emit(contract, [])
        ds = next(iter(res["google_bigquery_dataset"].values()))
        access = ds.get("access") or []
        emails = {e.get("user_by_email") for e in access if "user_by_email" in e}
        assert (
            "consumer@other-project.iam.gserviceaccount.com" in emails
        ), f"cross-project SA not in dataset.access[] — got {access}"

    def test_multiple_principals_emit_multiple_access_entries(self):
        contract = _gcp_contract(
            {
                "consumers": {
                    "principals": [
                        "consumer-a@p1.iam.gserviceaccount.com",
                        "consumer-b@p2.iam.gserviceaccount.com",
                    ],
                    "permissions": ["read"],
                }
            }
        )
        res = get_iac_plugin("gcp").emit(contract, [])
        ds = next(iter(res["google_bigquery_dataset"].values()))
        emails = {e.get("user_by_email") for e in ds.get("access", []) if "user_by_email" in e}
        assert emails == {
            "consumer-a@p1.iam.gserviceaccount.com",
            "consumer-b@p2.iam.gserviceaccount.com",
        }

    def test_no_policies_no_access_block(self):
        contract = _gcp_contract({})
        res = get_iac_plugin("gcp").emit(contract, [])
        ds = next(iter(res["google_bigquery_dataset"].values()))
        # No access[] when no policies — existing behaviour preserved.
        assert "access" not in ds

    # Note: ``metadata.policies`` is read by the existing GCP plugin
    # (``_bq_access_entries`` → dataset.access[] block) but is NOT in
    # the v0.7.3 schema's ``metadata`` definition. This is a
    # pre-existing inconsistency between the plugin and the schema
    # that predates this branch — beyond the scope of "minimize schema
    # changes". A future PR should either add ``policies`` to the
    # schema's ``metadata`` block or migrate the plugin to read from
    # an existing schema-validated location like ``exposePolicy.authz``.
