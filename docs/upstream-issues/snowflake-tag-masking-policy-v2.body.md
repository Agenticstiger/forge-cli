## Title

Restore (or document replacement for) `snowflake_tag_masking_policy_association` in the v2 provider

## What I'm doing

Building tag-based masking on Snowflake via Terraform / OpenTofu. The
canonical Snowflake Horizon pattern is:

1. Define a tag (`snowflake_tag`).
2. Bind a masking policy to that tag (canonically:
   `snowflake_tag_masking_policy_association` in the v0.85-era provider).
3. Attach the tag to a column via `snowflake_tag_association`.

Snowflake then auto-applies the policy on any column carrying the tag.

## Expected

The v0.85.0 docs document
`snowflake_tag_masking_policy_association`:
<https://registry.terraform.io/providers/snowflakedb/snowflake/0.85.0/docs/resources/tag_masking_policy_association>.

I expected the resource to either:
- still exist in v2 under the same name, OR
- be renamed with the rename called out in `MIGRATION_GUIDE.md`.

## Actual

`tofu validate` against the v2 provider fails:

```
Error: Invalid resource type
The provider snowflakedb/snowflake does not support resource type
"snowflake_tag_masking_policy_association".
```

Searching the v2 docs (registry + the `latest/docs/resources` list)
shows no equivalent resource for "bind masking policy to tag". The
MIGRATION_GUIDE.md does NOT mention the removal — searching for
"tag_masking_policy_association" returns no hits.

## Asks

1. Was the resource intentionally removed? If so, please add a note
   to `MIGRATION_GUIDE.md` so consumers know to migrate.
2. Is there a v2 replacement resource? The `snowflake_table` ⨯
   `column.masking_policy` block applies a policy DIRECTLY to a
   specific column — that's a different shape than tag-based masking
   (where the policy follows the tag wherever it lands).
3. If neither, would the project accept a PR to add
   `snowflake_tag_masking_policy_association` back? The underlying
   SQL is stable (`ALTER TAG ... SET MASKING POLICY ...`) and the
   tag-based-masking workflow is documented Snowflake guidance.

## Workaround today

In forge-cli we deliberately **do not emit any tag-based masking**
until this gap closes — per our "discuss & file upstream, don't
silently work around" policy. We emit the policy + the tag + the
column-direct masking_policy attachment for the cases that don't
need tag indirection. Tag taxonomy (the "policy follows the tag"
pattern) stays a manual Snowsight workflow until either the v2
provider re-adds the resource or documents a replacement.

## Reproducer

```hcl
resource "snowflake_tag" "pii_type" {
  name     = "PII_TYPE"
  database = "GOVERNANCE"
  schema   = "TAGS"
  ordered_allowed_values = ["email", "phone"]
}

resource "snowflake_masking_policy" "mask_email" {
  name       = "MASK_EMAIL"
  database   = "GOVERNANCE"
  schema     = "POLICIES"
  argument   { name = "val" type = "VARCHAR" }
  body       = "CASE WHEN CURRENT_ROLE() = 'ADMIN' THEN val ELSE '***' END"
  return_data_type = "VARCHAR"
}

# THIS FAILS — resource type not supported:
resource "snowflake_tag_masking_policy_association" "pii_email" {
  tag_id            = "\"GOVERNANCE\".\"TAGS\".\"PII_TYPE\""
  masking_policy_id = "\"GOVERNANCE\".\"POLICIES\".\"MASK_EMAIL\""
}
```
