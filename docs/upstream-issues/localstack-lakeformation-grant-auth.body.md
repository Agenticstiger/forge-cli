**LocalStack version**: Pro Platinum, current (May 2026)
**Service**: Lake Formation

### Summary

A principal added to `DataLakeAdmins` via `put-data-lake-settings`
cannot subsequently call `grant-permissions` — LocalStack rejects the
call with `AccessDeniedException`. Real AWS authorises this sequence
cleanly: a registered DataLakeAdmin has implicit permission to grant
any LF permission on any LF resource.

### Repro

```bash
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1

aws --endpoint-url=http://localhost:4566 lakeformation put-data-lake-settings \
  --data-lake-settings '{
    "DataLakeAdmins": [
      {"DataLakePrincipalIdentifier": "arn:aws:iam::000000000000:user/test"}
    ]
  }'

# Create a Glue DB so we have something to grant on.
aws --endpoint-url=http://localhost:4566 glue create-database \
  --database-input Name=test_db

# Grant DESCRIBE — fails.
aws --endpoint-url=http://localhost:4566 lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::000000000000:user/test \
  --resource '{"Database":{"Name":"test_db"}}' \
  --permissions DESCRIBE
```

Expected: `{}` (success, no error body).
Observed: `AccessDeniedException: Insufficient Lake Formation permissions`.

### Discussion

LocalStack appears to gate `GrantPermissions` behind a check that is
not satisfied by an entry in the DataLakeAdmins list. Real AWS treats
DataLakeAdmin membership as the implicit-grant principal; LocalStack's
LF emulation diverges here.

### Why this matters

Lake Formation tooling (forge-cli, Terraform modules, Atlan
governance, the AWS console wizard) all use this exact pattern: a
deployer principal is the LF admin, and bootstrap scripts grant
downstream service-role permissions to read specific tables. Without
DataLakeAdmin → GrantPermissions auth, none of those tools work
against LocalStack — they have to switch to real AWS for LF testing.

### Scope refinement (re-verified on Pro 2026.6.0)

The failure is specific to **IAM enforcement being on**:

* With `ENFORCE_IAM=1`, `GrantPermissions` returns
  `AccessDeniedException` — including when the caller is registered
  via `aws_lakeformation_data_lake_settings` in the same apply, so
  ordering is not the cause.
* With IAM enforcement **off** (the default), the same
  `GrantPermissions` call succeeds and the grant reads back correctly
  through `ListPermissions`.

So the LF grant *can* be created on the emulator, just never on an
instance that is also evaluating authorization — which is exactly the
combination needed to test whether an LF grant actually authorises
anything.

### Workaround

Partial. LF grants can be **created and inspected** on a
non-enforcing LocalStack, which is enough to verify that a tool emits
and applies the right LF configuration. Verifying that a grant
*authorises* a read still requires a real AWS account. forge-cli does
both: emulator tier 2 for emit + apply + read-back
(`tests/integration/test_iac_cross_account_localstack.py`), real-AWS
tier 3 for LF authorization.
