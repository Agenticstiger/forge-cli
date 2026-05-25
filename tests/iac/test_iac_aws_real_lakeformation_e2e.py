# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 3 — Lake Formation real-AWS round-trips.

Verifies the AWS plugin's ``governance.lakeFormation`` emit actually
lands the matching LF resources in the account when ``tofu apply``
runs against real AWS. Three tests, one per LF feature pillar:

* ``test_real_lf_location_register_and_grant`` — registers an S3
  location with LF and grants SELECT on the underlying Glue table to a
  principal. Asserts (a) ``DescribeResource`` returns the registered
  location, (b) ``ListPermissions`` shows the grant.
* ``test_real_lf_tag_definitions_and_associations`` — defines LF-tags
  at the contract level and associates them to a Glue table.
  Asserts (a) ``GetLFTag`` returns each tag with its values, (b)
  ``GetResourceLFTags`` returns the right tag/value on the table.
* ``test_real_lf_row_and_column_filter`` — applies a
  ``aws_lakeformation_data_cells_filter`` with both a row predicate
  and a column projection. Asserts ``GetDataCellsFilter`` returns the
  filter with the expected expression.

These are IaC-side verifications: they confirm forge-cli emits the
right LF resources and tofu applies them successfully. Enforcement-side
verification (a non-admin principal tries to SELECT and is
allowed/denied per grants) is a separate harder layer that needs
explicit STS assume-role plumbing; it lives in its own follow-up file.

Prerequisite: the bootstrap module must have applied the
``aws_lakeformation_data_lake_settings`` block making the test
principal an LF admin (otherwise GrantPermissions / RegisterResource
return 403). See ``tests/iac/_aws_stage3_bootstrap/README.md``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from .conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
    aws_iceberg_contract,
    aws_real_boto,
    aws_real_role_arn,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
    pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON),
]


def _wait_for(predicate, *, timeout: float = 30.0, interval: float = 1.0):
    """Poll ``predicate`` until it returns a truthy value or timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


# ---------------------------------------------------------------------------
# Test 1 — location registration + principal grant
# ---------------------------------------------------------------------------


def test_real_lf_location_register_and_grant(aws_real_project, aws_account):
    """LF registers the S3 path AND a SELECT grant lands on the Glue table.

    The contract turns ``registerLocation: true`` on and grants SELECT
    on the events table to the bootstrap's redshift-spectrum role
    (chosen because it's a non-admin principal — proves the grant
    targets the right ARN, not just the LF admins implicit access).
    """
    bucket = aws_real_project.name("lf-loc-b")
    glue_db = aws_real_project.name("lf_loc_db").replace("-", "_")
    grantee = aws_real_role_arn("spectrum")
    region = aws_account["region"]

    contract = aws_iceberg_contract(bucket, database=glue_db, table="events", cid="iac.aws.lf.loc")
    # Inject the LF block onto the single exposure.
    contract["exposes"][0]["binding"]["governance"] = {
        "lakeFormation": {
            "registerLocation": True,
            "grants": [{"principal": grantee, "permissions": ["SELECT", "DESCRIBE"]}],
        }
    }

    aws_real_project.apply_ok(contract)

    lf = aws_real_boto("lakeformation")
    s3_arn = f"arn:aws:s3:::{bucket}/silver/events/"

    # (a) The S3 location is registered. ``DescribeResource`` returns
    # ``ResourceInfo`` (with RoleArn / VerificationStatus / ...) when
    # the location is registered; otherwise the boto wrapper raises
    # ``EntityNotFoundException`` (caught + treated as ``None`` here).
    desc = _wait_for(lambda: _safe_describe_resource(lf, s3_arn))
    assert desc and desc.get("ResourceInfo"), desc
    info = desc["ResourceInfo"]
    # The service-linked role for LF data access is the canonical
    # ``RoleArn`` returned for an SLR-registered location — confirms the
    # registration used ``use_service_linked_role=true`` as emitted.
    assert "AWSServiceRoleForLakeFormation" in info.get("RoleArn", ""), info

    # (b) The SELECT grant on the underlying Glue table is in
    # list-permissions for the grantee principal.
    grants = _wait_for(
        lambda: _list_lf_permissions_for_table(
            lf, principal=grantee, database=glue_db, table="events"
        )
    )
    assert grants, "no LF permissions found for the grantee on the table"
    perms = {p for g in grants for p in (g.get("Permissions") or [])}
    assert "SELECT" in perms and "DESCRIBE" in perms, perms


# ---------------------------------------------------------------------------
# Test 2 — LF-TBAC: tag definitions + table tag associations
# ---------------------------------------------------------------------------


def test_real_lf_tag_definitions_and_associations(aws_real_project, aws_account):
    """Contract-level LF-tag definitions land as ``aws_lakeformation_lf_tag``
    AND the per-exposure association attaches them to the Glue table."""
    bucket = aws_real_project.name("lf-tbac-b")
    glue_db = aws_real_project.name("lf_tbac_db").replace("-", "_")
    uid = aws_real_project.uid
    # Tag keys must be lowercase + unique-per-account; suffix with uid to
    # avoid colliding with concurrent test runs in the same account.
    classification_key = f"fluid_class_{uid}"
    domain_key = f"fluid_dom_{uid}"

    contract = aws_iceberg_contract(bucket, database=glue_db, table="events", cid="iac.aws.lf.tbac")
    contract["governance"] = {
        "lakeFormation": {
            "tagDefinitions": {
                classification_key: ["public", "pii_low", "pii_high"],
                domain_key: ["sales", "marketing"],
            }
        }
    }
    contract["exposes"][0]["binding"]["governance"] = {
        "lakeFormation": {
            "tags": {
                classification_key: "pii_low",
                domain_key: "sales",
            }
        }
    }

    aws_real_project.apply_ok(contract)

    lf = aws_real_boto("lakeformation")

    # (a) Both LF-tags exist with the right allowed-values list.
    classification = lf.get_lf_tag(TagKey=classification_key)
    assert set(classification["TagValues"]) >= {"public", "pii_low", "pii_high"}
    domain = lf.get_lf_tag(TagKey=domain_key)
    assert set(domain["TagValues"]) >= {"sales", "marketing"}

    # (b) The table carries both associations.
    assoc = lf.get_resource_lf_tags(
        Resource={
            "Table": {
                "CatalogId": aws_account["account_id"],
                "DatabaseName": glue_db,
                "Name": "events",
            }
        }
    )
    found = {t["TagKey"]: t["TagValues"] for t in assoc.get("LFTagOnDatabase", [])}
    # The TBAC association lives under ``LFTagsOnTable`` for table-level tags.
    for entry in assoc.get("LFTagsOnTable", []):
        found[entry["TagKey"]] = entry["TagValues"]
    assert classification_key in found, found
    assert domain_key in found, found
    assert "pii_low" in found[classification_key], found
    assert "sales" in found[domain_key], found


# ---------------------------------------------------------------------------
# Test 3 — row-level + column-level data filter
# ---------------------------------------------------------------------------


def test_real_lf_row_and_column_filter(aws_real_project, aws_account):
    """``aws_lakeformation_data_cells_filter`` is applied with both a row
    predicate and a column projection. ``GetDataCellsFilter`` returns it."""
    bucket = aws_real_project.name("lf-filter-b")
    glue_db = aws_real_project.name("lf_filter_db").replace("-", "_")
    filter_name = f"only_eu_{aws_real_project.uid}"

    contract = aws_iceberg_contract(
        bucket,
        database=glue_db,
        table="events",
        cid="iac.aws.lf.filter",
        schema_cols=[
            {"name": "event_id", "type": "string", "required": True},
            {"name": "occurred_at", "type": "timestamp"},
            {"name": "amount", "type": "decimal(12,2)"},
            {"name": "region", "type": "string"},
        ],
    )
    contract["exposes"][0]["binding"]["governance"] = {
        "lakeFormation": {
            "rowFilter": {
                "name": filter_name,
                "rowExpression": "region = 'EU'",
                "columnNames": ["event_id", "occurred_at", "amount"],
            }
        }
    }

    aws_real_project.apply_ok(contract)

    lf = aws_real_boto("lakeformation")
    f = lf.get_data_cells_filter(
        TableCatalogId=aws_account["account_id"],
        DatabaseName=glue_db,
        TableName="events",
        Name=filter_name,
    )
    body = f["DataCellsFilter"]
    assert body["Name"] == filter_name
    assert body["RowFilter"]["FilterExpression"] == "region = 'EU'"
    # Column projection — three columns visible; 'region' must be hidden.
    visible = set(body["ColumnNames"])
    assert visible == {"event_id", "occurred_at", "amount"}, body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_describe_resource(lf, s3_arn: str):
    try:
        return lf.describe_resource(ResourceArn=s3_arn)
    except lf.exceptions.EntityNotFoundException:
        return None


def _list_lf_permissions_for_table(lf, *, principal: str, database: str, table: str):
    """Return permissions on database.table for the given principal."""
    try:
        resp = lf.list_permissions(
            Principal={"DataLakePrincipalIdentifier": principal},
            Resource={"Table": {"DatabaseName": database, "Name": table}},
        )
    except lf.exceptions.AccessDeniedException:
        return None
    return resp.get("PrincipalResourcePermissions") or []
