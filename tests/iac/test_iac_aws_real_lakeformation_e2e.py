# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
* ``test_real_lf_row_filter_holds_through_athena`` — the enforcement side of
  ``policy.authz.rowFilters``: the filtered role, assumed, reads only its rows
  through Athena.
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

import json
import time
from typing import Any, Dict, List

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
    # This contract owns the tag vocabulary it defines (packaging, schema 0.7.7).
    contract["packaging"] = {"containers": {"lfTagVocabulary": "isolated"}}
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
# Test 4 — a row filter holds through Athena
# ---------------------------------------------------------------------------


def test_real_lf_row_filter_holds_through_athena(aws_real_project, aws_account):
    """A role whose read grant carries ``policy.authz.rowFilters`` reads only its rows.

    ``binding.principals`` maps the filtered principal to the analyst role, and the
    contract grants the role SELECT. The emitter puts that grant on the data cells
    filter, never on the table; assumed, the role reads only the EU rows through
    Athena. Before 2026-11-16 a filter that excluded no column did not apply at all
    (hashicorp/aws read back ``excluded_column_names = []`` for the omitted list).

    Grants that spell the role another way (eDKBar8G) are matched to the filter by
    ``aws_principal_key``, and pinned offline in test_iac_mask_rowfilters_labels.py:
    on a live account Lake Formation itself refuses such a grant, "Invalid path for
    user" for the ARN without the role's path and "Invalid principal" for its name
    in another case (both seen 2026-11-16), so neither spelling reaches a table here.

    The table's ``IAM_ALLOWED_PRINCIPALS`` grant (this account's default for a new
    table) is revoked once the rows are in, or IAM alone would let any role read
    every row and the filter would prove nothing.
    """
    import boto3

    region = aws_account["region"]
    iam, lf, s3 = aws_real_boto("iam"), aws_real_boto("lakeformation"), aws_real_boto("s3")
    bucket = aws_real_project.name("lfsec-b")
    results = aws_real_project.name("lfsec-res")
    glue_db = aws_real_project.name("lfsec_db").replace("-", "_")
    table = "profile"
    role_name = aws_real_project.name("lfsec-analyst")
    analysts = "group:analysts@northwind.example"

    role = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": aws_account["user_arn"]},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        ),
        Tags=[{"Key": "purpose", "Value": "fluid-iactest"}],
    )["Role"]
    _create_bucket(s3, results, region)
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="athena-reader",
            PolicyDocument=json.dumps(_analyst_policy(results)),
        )
        role_arn = role["Arn"]

        contract = aws_iceberg_contract(
            bucket,
            database=glue_db,
            table=table,
            cid="iac.aws.lf.rows",
            schema_cols=[
                {"name": "customer_id", "type": "string", "required": True},
                {"name": "region", "type": "string"},
            ],
        )
        expose = contract["exposes"][0]
        # Parquet, not Iceberg: the Iceberg table the emitter registers has no
        # metadata file until an engine writes one, so Athena cannot read it yet.
        expose["binding"]["format"] = "parquet"
        expose["binding"]["principals"] = {analysts: role_arn}
        expose["binding"]["governance"] = {
            "lakeFormation": {
                "registerLocation": True,
                "grants": [
                    {"principal": role_arn, "permissions": ["SELECT", "DESCRIBE"]},
                ],
            }
        }
        expose["policy"] = {
            "authz": {
                "rowFilters": [{"principal": analysts, "name": "eu_only", "where": "region = 'EU'"}]
            }
        }

        module = json.loads(aws_real_project.emit(contract))
        grants = module["resource"]["aws_lakeformation_permissions"].values()
        assert all("data_cells_filter" in g for g in grants), grants

        aws_real_project.apply_ok(contract)

        # The rows go straight to S3 under the tester's IAM: Athena's INSERT would
        # write through Lake Formation's vended credentials, which carry no
        # s3:PutObject for a location registered with the service-linked role.
        pq = pytest.importorskip("pyarrow.parquet")
        import io

        import pyarrow as pa

        buf = io.BytesIO()
        pq.write_table(
            pa.table({"customer_id": ["c-eu", "c-us-1", "c-us-2"], "region": ["EU", "US", "US"]}),
            buf,
        )
        s3.put_object(Bucket=bucket, Key=f"silver/{table}/rows.parquet", Body=buf.getvalue())
        lf.revoke_permissions(
            Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            Resource={"Table": {"DatabaseName": glue_db, "Name": table}},
            Permissions=["ALL"],
        )
        time.sleep(10)  # IAM role propagation, LF convergence

        creds = _assume(role_arn)
        analyst = boto3.Session(**creds, region_name=region).client("athena")
        rows = _athena_ok(
            analyst,
            f'SELECT customer_id, region FROM "{glue_db}"."{table}" ORDER BY customer_id',
            results,
        )
        assert rows == [["c-eu", "EU"]], f"the analyst read past its row filter: {rows}"
    finally:
        with _suppress():
            iam.delete_role_policy(RoleName=role_name, PolicyName="athena-reader")
        with _suppress():
            iam.delete_role(RoleName=role_name)
        with _suppress():
            for obj in s3.list_objects_v2(Bucket=results).get("Contents", []):
                s3.delete_object(Bucket=results, Key=obj["Key"])
            s3.delete_bucket(Bucket=results)


def _suppress():
    import contextlib

    return contextlib.suppress(Exception)


def _create_bucket(s3, name: str, region: str) -> None:
    if region == "us-east-1":
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})


def _analyst_policy(results_bucket: str) -> Dict[str, Any]:
    """Athena and the catalog, Lake Formation data access, and the results bucket only:
    the table's own objects are reachable only through Lake Formation."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetWorkGroup",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                    "lakeformation:GetDataAccess",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:GetObject", "s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{results_bucket}",
                    f"arn:aws:s3:::{results_bucket}/*",
                ],
            },
        ],
    }


def _assume(role_arn: str) -> Dict[str, str]:
    """The role's credentials; retried while a new role propagates through IAM."""
    sts = aws_real_boto("sts")
    for attempt in range(10):
        try:
            c = sts.assume_role(RoleArn=role_arn, RoleSessionName="fluid-iactest-lfsec")[
                "Credentials"
            ]
            return {
                "aws_access_key_id": c["AccessKeyId"],
                "aws_secret_access_key": c["SecretAccessKey"],
                "aws_session_token": c["SessionToken"],
            }
        except Exception:  # noqa: BLE001
            if attempt == 9:
                raise
            time.sleep(3)
    raise AssertionError("unreachable")


def _athena_ok(athena, query: str, results_bucket: str) -> List[List[str]]:
    """Run ``query`` and return its data rows; a failed query fails the test."""
    exec_id = athena.start_query_execution(
        QueryString=query,
        ResultConfiguration={"OutputLocation": f"s3://{results_bucket}/athena/"},
    )["QueryExecutionId"]
    deadline = time.monotonic() + 120
    while True:
        q = athena.get_query_execution(QueryExecutionId=exec_id)["QueryExecution"]
        state = q["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED") or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert state == "SUCCEEDED", f"{query!r}: {state} {q['Status'].get('StateChangeReason')}"
    rows = athena.get_query_results(QueryExecutionId=exec_id)["ResultSet"]["Rows"]
    return [[d.get("VarCharValue") for d in r["Data"]] for r in rows[1:]]


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
