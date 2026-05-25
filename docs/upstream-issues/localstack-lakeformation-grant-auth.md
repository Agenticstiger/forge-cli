# Upstream issue draft — localstack/localstack-pro

`aws lakeformation grant-permissions` against LocalStack Pro returns
`AccessDeniedException` even when the caller has been added to the LF
admin list via `put-data-lake-settings` — a path that works against
real AWS. The forge-cli IaC test
`test_iac_aws_localstack_e2e.py::test_localstack_lake_formation_grants_e2e`
trips this and is currently skipped.

## Repro

```bash
localstack start -d --image localstack/localstack-pro:latest \
  --license-auth-token "$LOCALSTACK_AUTH_TOKEN"

# Bootstrap: make the test runner an LF admin.
aws --endpoint-url=http://localhost:4566 lakeformation put-data-lake-settings \
  --data-lake-settings DataLakeAdmins=[{DataLakePrincipalIdentifier=arn:aws:iam::000000000000:user/test}]

# Grant DESCRIBE on a database — succeeds against real AWS, fails on LocalStack.
aws --endpoint-url=http://localhost:4566 lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::000000000000:user/test \
  --resource '{"Database":{"Name":"test_db"}}' \
  --permissions DESCRIBE
# → AccessDeniedException: Insufficient Lake Formation permissions
```

## Current behaviour

LocalStack Pro's LF authorization seems to reject the grant even
though `put-data-lake-settings` listed the same principal as an admin.
Real AWS auths the same sequence cleanly: a registered DataLakeAdmin
can grant any permission to any principal.

## Expected behaviour

A principal in the `DataLakeAdmins` list should be authorised to call
`GrantPermissions` on any LF resource, matching the real AWS IAM
semantics.

## Workaround in forge-cli

The test is marked `pytest.mark.skipif(LOCALSTACK_LF_AUTH_QUIRK, reason=…)`
and the LF code path is covered against real AWS instead — see
`tests/iac/test_iac_aws_real_lakeformation_e2e.py` (9 tests, all green).
The LocalStack tier 2 coverage for LF is therefore intentionally
deferred; tier 3 (real AWS) is the canonical pin.

## To file

LocalStack's public GitHub repo (`localstack/localstack`) was archived
on 2026-03-23 (still resolvable but read-only); the Pro repo
(`localstack/localstack-pro`) is private and only visible to org
members. File this through LocalStack's support portal at
<https://app.localstack.cloud/community> (free-tier accounts can file
community issues) or via the LocalStack Slack
(<https://slack.localstack.cloud/>) — paste the body below into the
"Issue title" + "Description" fields. The text in `.body.md` is
formatted to fit either channel verbatim.
