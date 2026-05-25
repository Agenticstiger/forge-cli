# Upstream issue draft — snowflakedb/terraform-provider-snowflake

`snowflake_tag_masking_policy_association` was present in the legacy
`Snowflake-Labs/snowflake` provider (v0.85.0 docs:
<https://registry.terraform.io/providers/snowflakedb/snowflake/0.85.0/docs/resources/tag_masking_policy_association>)
but does **not** exist in the current `snowflakedb/snowflake` v2
provider — `tofu validate` fails with:

```
Error: Invalid resource type
The provider snowflakedb/snowflake does not support resource type
"snowflake_tag_masking_policy_association".
```

The MIGRATION_GUIDE.md does not mention the removal. There is no
documented replacement resource — the v2 docs cover `snowflake_tag` +
`snowflake_tag_association` + `snowflake_masking_policy`, but no
declarative way to bind a masking policy to a tag (which is the
canonical Snowflake Horizon tag-based-masking pattern).

The workaround today is to emit the binding via SQL post-apply:

```sql
ALTER TAG "DB"."SCH"."TAG_NAME"
  SET MASKING POLICY "DB"."SCH"."POLICY_NAME"
  USING (val);
```

This breaks declarative state management — the binding can't be
modeled as a tofu resource, so drift detection / `tofu plan` doesn't
catch it.

## Impact

Tag-based masking is Snowflake's recommended Horizon governance
pattern for "auto-apply masking by column classification". Without a
declarative resource the policy attachment has to live outside the
Terraform / OpenTofu module — splits the source-of-truth, breaks
brownfield import, and forces an out-of-band apply step.

forge-cli's `IacProviderPlugin` for Snowflake (this branch) ACCEPTS
the `governance.snowflake.tagMaskingPolicies[]` contract field for
forward-compatibility but emits no resource for it until the
provider re-adds (or documents) the declarative path. A WARNING is
logged at emit time pointing to this file.

## To file

```bash
gh issue create \
  --repo snowflakedb/terraform-provider-snowflake \
  --title "Restore (or document replacement for) snowflake_tag_masking_policy_association in v2" \
  --body-file docs/upstream-issues/snowflake-tag-masking-policy-v2.body.md
```

The body file (see `.body.md`) reproduces the failure + cites the
canonical Horizon tag-based-masking pattern as the use case.
