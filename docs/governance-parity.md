# Governance parity: one contract, AWS and GCP

A contract declares its governance once, in fields that name no cloud. Each
environment's overlay patches only `exposes[].binding`: the platform, the location,
and which real identities the contract's logical principals are on that cloud.
`fluid apply` then emits that cloud's own OpenTofu resources, and `fluid verify`
checks the live platform against the same derivation apply emitted from.

The fields are in fluid-schema **0.7.6** (the preview). 0.7.5 GA is unchanged, so a
contract needs `fluidVersion: "0.7.6"` to validate with them.

## The parity table

| Policy (contract field) | AWS: what `fluid apply` emits | GCP: what `fluid apply` emits | What `fluid verify` checks |
|---|---|---|---|
| **Retention**: `exposes[].lifecycle {retention, expire: true}` | One S3 lifecycle rule per expose, filtered to the binding's prefix, expiring objects `retention` after they are written (`aws_s3_bucket_lifecycle_configuration`). | Daily partitions that expire `retention` after their day ends: `google_bigquery_table.time_partitioning {type: DAY, expiration_ms}`, by ingestion time, or on `binding.location.partitionBy` when it names one date or timestamp column (see "Retention counts from the partition's date" below). Never a table expiration, which would delete the product. | AWS: the enabled prefix rule's `Expiration.Days`, and no rule that expires sooner. GCP: the table's partition type, field and `expirationMs`, and that the table itself has no `expirationTime`. |
| **Encryption at rest**: `binding.encryption.kms` | `product`: a KMS key per bucket (`aws_kms_key`, rotation on, alias `alias/fluid/<id>/<bucket>`) as the bucket's default SSE-KMS. `alias/...` or an ARN: that key. `none`: SSE-S3. | `product`: a key ring and key per dataset (`google_kms_key_ring` `fluid-<id>-<dataset>`, `google_kms_crypto_key` `bigquery`, 90-day rotation) in the dataset's location, `roles/cloudkms.cryptoKeyEncrypterDecrypter` for the BigQuery service agent (`google_kms_crypto_key_iam_member`), used as the dataset's `default_encryption_configuration` and the table's `encryption_configuration`. `projects/.../cryptoKeys/...`: that key. `none`: Google-managed keys. Every table of one dataset must declare the same key (see "One dataset, one key"). | AWS: the key is `Enabled`, and the objects under the prefix are SSE-KMS with it. GCP: `kmsKeyName` of the table, and of the dataset's default when the product owns the dataset. |
| **Column restrictions**: `exposes[].policy.authz.columnRestrictions` | Each Lake Formation grant with `SELECT` excludes the restricted columns its principal may not read (`aws_lakeformation_permissions.table_with_columns.excluded_column_names`, with `wildcard`). | A Data Catalog taxonomy per product and dataset with fine-grained access control (`google_data_catalog_taxonomy`), a policy tag per set of restricted columns that share their readers (`google_data_catalog_policy_tag`), attached through the table schema's `policyTags`, and `roles/datacatalog.categoryFineGrainedReader` for exactly the allowed readers (`google_data_catalog_policy_tag_iam_member`). The restrictions' `tags` and `labels` go into the policy tag's description. | AWS: `lakeformation:ListPermissions` on tables; no `SELECT` of a denied principal, of a read grantee, or of `IAM_ALLOWED_PRINCIPALS` reaches a column it may not read, including grants made outside the contract. GCP: every restricted column carries a tag, the tag's fine-grained readers are exactly the derived set (a denied principal, or one granted outside the contract, fails), and the taxonomy enforces fine-grained access control. |
| **Column masking**: `columnRestrictions[] {access: mask, mask: last_four \| first_four \| nullify}` | Lake Formation cannot mask. The masked principal's grant excludes the masked columns from the table, as a deny does, and a protected multi-dialect Glue Data Catalog view returns them masked (`aws_glue_catalog_table`, `table_type = VIRTUAL_VIEW`, `view_definition {is_protected, definer, representations {dialect = ATHENA, validation_connection}}`, named `<table>_masked`): exactly the columns the principal's own grant reads raw (its `columns` list, or every column less its exclusions), the masked ones through the same rule in Athena SQL, and the principal's row filter as the view's `WHERE`. The view reads the table as its definer, so it selects nothing the grant does not. The definer gets `SELECT` with grant option on the table and `CREATE_TABLE` and `DESCRIBE` on the database (Lake Formation's view prerequisites), the principal `SELECT` and `DESCRIBE` on the view. Needs `governance.lakeFormation.maskedViews {definer, validationConnection}`. | The masked columns get a policy tag of their own with a BigQuery data policy on it (`google_bigquery_datapolicy_data_policy`, `DATA_MASKING_POLICY`, predefined `LAST_FOUR_CHARACTERS`, `FIRST_FOUR_CHARACTERS` or `ALWAYS_NULL`) and `roles/bigquerydatapolicy.maskedReader` on the data policy for the masked principals (`google_bigquery_datapolicy_data_policy_iam_member`). The tag's fine-grained readers read the clear value. | AWS: the masked principal's table grant excludes the masked columns (the column check above). GCP: the masked tag's fine-grained readers are exactly the derived set. Not checked yet: the data policy and its masked readers, and the view's SQL. |
| **Row filters**: `exposes[].policy.authz.rowFilters[] {principal, where, name?}` | A Lake Formation data cells filter per filter (`aws_lakeformation_data_cells_filter`, `row_filter.filter_expression` the predicate, `column_wildcard.excluded_column_names` the principal's excluded columns), and the principal's `SELECT` granted on the filter instead of the table. Only on a Glue-catalog table (format `iceberg`, `parquet`, `csv`, `json`, `avro`, `orc` or `delta`, with `location.database` and `location.table`): on a Redshift or Kinesis binding the filter is refused. | A row access policy per filter (`google_bigquery_row_access_policy`, `filter_predicate` the predicate, `grantees` every identity of the principal, whatever it holds: a writer reads rows too), and one more, `fluid_all_rows` (`TRUE`), for every other principal the expose grants anything to, never for a filtered one, since BigQuery shows a principal no policy names no row at all and the union of those that do. | AWS: the column check reads a grant made on a data cells filter, so a filtered principal's excluded columns are still checked. Not checked yet: the filter expression on AWS, and the row access policies on GCP. |
| **Governance labels**: `labels`, `exposes[].labels`, `dataClassification`, `sovereignty {jurisdiction, regulatoryFramework, dataResidency}` | The same keys as AWS tags on the bucket and the key, and as parameters on the Glue table. | GCP labels on the dataset, the table and the key: the contract's and the expose's labels, and `fluid_classification`, `fluid_jurisdiction`, `fluid_regulation` (the frameworks joined), `fluid_residency`. | Not checked yet. |
| **Access grants**: `accessPolicy.grants[]` | Not emitted: on AWS, access is the binding's `governance.lakeFormation.grants`. `fluid validate` warns only for an aws binding with no Lake Formation grants, where the contract's access intent is unenforced. | One non-authoritative `google_bigquery_dataset_iam_member` per role and member (and `google_storage_bucket_iam_member` for GCS), with the logical principal mapped to its GCP identity. | Not checked yet on either cloud. |

A policy that a binding cannot apply is refused, never dropped: at `fluid validate`
(stage 2), and again by the emitter at `fluid generate iac`, `fluid apply` and the
state check of `fluid diff`, before any module is written. `fluid plan` does not run
the emitter, so it does not refuse them. Refused: retention, a key or a column
restriction on a GCP binding that is not a BigQuery table (GCS, Pub/Sub, Iceberg
storage), a column restriction (a deny or a mask) on an AWS binding with no Lake
Formation grants or on a non-Glue format (`column-restriction-unenforceable`), an AWS
key reference on GCP and a Cloud KMS key name on AWS, a mask on an AWS binding with no
`maskedViews` (`column-mask-unenforceable`), a text mask on a column that is not a
string (`column-mask-type`), a row filter on an AWS binding that names no Glue-catalog
table, such as a Redshift or Kinesis format, or one whose principal holds no Lake
Formation read grant (`row-filter-unenforceable`), and a row filter on a BigQuery view
(`row-filter-view`) or on a GCP binding that is not a BigQuery table.

## Logical principals and `binding.principals`

The base contract names principals as the business knows them:

```yaml
accessPolicy:
  grants:
    - principal: group:data-platform@northwind.example
      permissions: [read, select, query]
exposes:
  - exposeId: candidates
    policy:
      authz:
        columnRestrictions:
          - principal: group:analysts@northwind.example
            columns: [customer_id, msisdn]
            access: deny
```

Each environment's overlay maps them, in the binding, to the identities they are on
that cloud:

```yaml
# overlays/gcp.yaml
exposes:
  - binding:
      platform: gcp
      principals:
        group:data-platform@northwind.example: group:data-platform@northwind.com
        group:analysts@northwind.example: group:analysts@northwind.com
        serviceAccount:fluid-pipeline@northwind.example: >-
          serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com

# overlays/aws.yaml
exposes:
  - binding:
      platform: aws
      principals:
        group:analysts@northwind.example: arn:aws:iam::123456789012:role/fluid-demo-lab-analyst
```

* A value is one identity, a list of them, or `[]` for "no identity on this cloud"
  (nothing is granted to it there).
* With `binding.principals` present, every principal the contract names for the
  expose must be mapped; an unmapped one is refused (`principal-unmapped`).
* Without it, principals are used as written, as before, except that on GCP a
  placeholder is refused (`principal-placeholder`): a principal in a reserved
  top-level domain (`.example`, `.test`, `.invalid`, `.localhost`), and one that is
  not an IAM member at all (`group:data-platform` with no domain, a bare `analysts`,
  an unknown prefix such as `role:analyst`, or an unfilled `<<YOUR_PROJECT_HERE>>`).
  BigQuery and Cloud Storage refuse either at apply, so none was ever a working grant.
* On AWS an unmapped restriction principal must already be an IAM ARN.

The pattern follows ODCS v3, which declares `roles[]` once and binds them per server
(`servers[].roles`), and dbt `grants`, which resolve the grantee per target.

## Column restriction semantics

* A column named in any restriction is restricted.
* `deny`: the principal may not read the columns.
* `allow`: the columns are readable only by the principals an `allow` names.
* `mask` (with `mask: last_four`, `first_four` or `nullify`): the principal reads the
  columns masked, never clear. A masked principal is denied the raw column, so on
  every check that counts readers it counts as denied. A rule needs `access: mask`, a
  mask needs its rule, and a column has one rule.
* A deny beats an allow and a mask. A restriction never grants access: the readers are the
  expose's readers. On GCP they are the `accessPolicy` read grantees and the
  expose's own `policy.authz.readers` (whose table access is managed elsewhere, so
  only the fine-grained reader role on the tag is granted to them); on AWS, the Lake
  Formation `SELECT` grantees. An allowed principal that is not a reader is logged,
  not added.
* A restriction on an expose with no reader is refused on both clouds: on AWS when
  the binding has no Lake Formation grant (`column-restriction-unenforceable`), on
  GCP when there is no read grant and no `policy.authz.readers`
  (`column-restriction-no-readers`). The policy tag would otherwise lock the columns
  for everyone, not only for the principals the restrictions name.
* A restriction's `tags` and `labels` are descriptive. On GCP they are written into
  the policy tag's description; Lake Formation permissions have no field for them.
* On AWS a grant's hand-written `excludedColumns` keeps working. With a restriction
  on the same expose the two must agree, or the emit is refused
  (`column-restriction-conflict`).

On GCP a denied principal gets an access error on the restricted columns;
`SELECT * EXCEPT (customer_id, msisdn)` still works for it.

## Masking: the platform's, and the landing's

`access: mask` is the platform masking a column at read time, for its principal only;
the stored value is clear and its other readers read it. `policy.privacy.masking` is
the build hashing (or tokenizing) the value before it lands, so nobody reads it clear.
A contract can carry both on two columns of one value, as a column kept clear for its
stewards and masked for analysts, beside a salted hash every reader may join on.
Masking a column that `privacy.masking` already hashes would mask the hash.

* **GCP**: BigQuery's predefined rules. `last_four` is `LAST_FOUR_CHARACTERS`
  (`XXXXX` and the last four characters), `first_four` is `FIRST_FOUR_CHARACTERS`,
  `nullify` is `ALWAYS_NULL`. The data policy is attached to the policy tag of the
  masked columns, so the columns' fine-grained readers read the clear value and the
  masked readers read the masked one. A principal with neither is refused, as for a
  deny. BigQuery applies a new masked reader about a minute or two after the grant.
  A data policy id longer than BigQuery takes (it refuses 200 characters or more) keeps
  its first characters and ends in a hash of the whole.
* **AWS**: Lake Formation has no masking, so the masked value comes from a protected
  Glue Data Catalog view (Lake Formation's multi-dialect views, run as their
  definer). The view returns the rule's value in Athena SQL:
  `concat('XXXXX', substr(c, -4))` for `last_four`, the first four and `XXXXX` for
  `first_four`, `NULL` for `nullify`, and NULL stays NULL. A value of four characters
  or fewer is returned as its SHA-256, since its last four characters would be all of
  it. The principal keeps its grant on the table, without the masked columns.
* The view needs, per account: an Athena workgroup, a Glue connection of type
  `VIEW_VALIDATION_ATHENA` naming it (`validationConnection`), and a definer role
  that Glue and Lake Formation may assume (`sts:AssumeRole` and `sts:SetContext` for
  `glue.amazonaws.com` and `lakeformation.amazonaws.com`). The identity running
  `fluid apply` needs `iam:PassRole` on the definer and `glue:PassConnection` on the
  connection. A validated view carries no storage descriptor.
* The view is created by terraform-provider-aws 6 (`aws_glue_catalog_table`
  `view_definition`, which 5.x does not have), which forge-cli pins as `~> 6.0`.

## Row filters

`exposes[].policy.authz.rowFilters[]` names a principal and one SQL boolean
expression over the expose's columns; that principal reads only the rows it
selects. A reader no filter names reads every row, and a principal has one filter
per expose. The predicate is parsed (sqlglot) before it is written into a policy: a
statement separator, a comment, a subquery or a column the schema does not declare is
refused. Write it NULL-aware: `is_suppressed = false` drops the rows where the column
is NULL, which `(is_suppressed IS NULL OR is_suppressed = false)` keeps.

```yaml
policy:
  authz:
    rowFilters:
      - principal: group:analysts@northwind.example
        name: analysts_consented
        where: consent_data_analytics = true AND (is_suppressed IS NULL OR is_suppressed = false)
```

* **GCP**: a row access policy per filter, naming every identity of its principal,
  whatever the principal holds (a row access policy never grants the table, and a
  writer's `roles/bigquery.dataEditor` reads rows), and `fluid_all_rows` (`FILTER USING
  (TRUE)`) for every other principal the expose grants anything to, the pipeline's
  identity included, because BigQuery shows a principal that no row access policy
  names no row at all. A filtered identity is never in `fluid_all_rows`: BigQuery
  shows a principal the union of the policies naming it.
* **AWS**: a data cells filter per filter; the principal's `SELECT` is granted on the
  filter, with the principal's excluded columns as its column wildcard's exclusions,
  instead of on the table. The masked view of a filtered principal applies the same
  predicate, since the view reads the table as its definer.
* **A build keeps them.** BigQuery removes every row access policy of a table that a
  `WRITE_TRUNCATE` load writes, so a full-refresh build loads with
  `WRITE_TRUNCATE_DATA`, which replaces the rows and keeps the table's policies, tags
  and partitioning (measured on BigQuery, 5 October 2026, on an unpartitioned and on a
  DAY-partitioned table with a partition expiration). It is the disposition dlt's
  BigQuery destination uses for the same reason.

## Dataset grants are no longer authoritative

The grants were an authoritative `access` list on the dataset: it replaced every
entry the dataset had, including the ones BigQuery gives a new dataset (the project's
owners, writers and readers, and its creator), and anything granted elsewhere. They
are `google_bigquery_dataset_iam_member` resources now, which add their own binding
and leave the rest. The consequences:

* A principal holding a basic role on the project (Viewer, Editor, Owner) keeps the
  dataset access BigQuery's default entries give it. Restricted columns stay
  protected by their policy tags whatever the dataset grants; keep basic roles off
  projects that hold restricted data.
* A grant made outside the contract is no longer removed by the next apply.
  `fluid verify` reports a fine-grained reader granted outside the contract; dataset
  grants are not verified yet.
* The provider rewrites the dataset's access list without authorized-view entries
  when it adds a member; forge-cli emits none.
* Each member resource is named from its role and member plus a hash of both, so
  principals that differ only in `.`, `-` or `_` keep one grant each.

### The first apply after upgrading revokes what the old list held

Once the module stops setting `access`, the provider keeps it as Computed: an entry
the old authoritative list held and no member resource covers (a principal removed
in the same change, or a logical principal `binding.principals` now maps to another
identity) would stay on the dataset, unmanaged, and no plan would show it
(terraform-provider-google issue 8165). So the first `fluid apply` on a dataset whose
state holds an access list and no member resource sets `access`, for that one apply,
to the list less those entries. The provider revokes them and the member resources
are created after it; the apply prints what it revoked. For that one apply the list
is authoritative, as it was on every apply before: it is the list state recorded at
the last apply, so an entry added to the dataset by hand since then is removed, as
the old module removed it. The next apply finds the
member resources in state and leaves `access` unset. Only entries of the old
emitter's shape (a role and one user, group or domain) are revoked; special groups,
views and routines are kept. `fluid diff` makes the same change, so its plan shows
the revocation. A dataset whose every entry would be revoked cannot be narrowed that
way (an empty `access` plans nothing), and the apply is refused with the entries to
revoke by hand.

### Revoking a grant is not data loss

The data-loss gate refuses a plan that destroys a resource unless `--allow-data-loss`
is set. It counts only resources that hold data or policy: removing a
`google_bigquery_dataset_iam_member`, `google_bigquery_table_iam_member`,
`google_storage_bucket_iam_member`, `google_data_catalog_policy_tag_iam_member`,
`google_data_catalog_policy_tag`, `google_data_catalog_taxonomy` or an
`aws_lakeformation_permissions` revokes access and deletes nothing, so a revoked
reader or a lifted restriction applies without the flag. The apply lists them. A
key's IAM grant stays gated: without it BigQuery cannot decrypt the table. When the
plan's per-resource events do not account for every removal, every removal counts.

## One dataset, one key

BigQuery gives a table created without a key the dataset's default key. A table
declared unkeyed in a dataset another expose keys therefore gets the key anyway, the
provider plans removing it, and because `encryption_configuration` is ForceNew every
later plan replaces the table (terraform-provider-google issue 26193). Tables of one
dataset that declare different encryption are refused
(`encryption-kms-mixed-dataset`). A view stores no rows and carries no key; a keyed
view sets the dataset's default and must agree too. The dataset's default key no
longer depends on which expose comes first.

## Retention counts from the partition's date

BigQuery deletes a partition `retention` after the partition's own date, not after
its rows were written. Partitioned by ingestion time (no `partitionBy`, the default),
the date is the day the rows landed, so no row is deleted sooner than `retention`
after it was written, as with the S3 rule. With `binding.location.partitionBy`, the
date is the column's value: retention is the age of the event, and a backfill of rows
whose date is already older than `retention` lands in expired partitions and is
deleted at once. Naming the column is the opt-in to that; `fluid apply` logs it
(`bigquery_retention_event_time`).

## Changes a live table cannot take in place

BigQuery cannot partition an existing table, and the provider replaces a table whose
key changes (`encryption_configuration` is ForceNew). So the first apply that adds
`expire: true`, or a key, to a table that exists plans the table's **replacement**,
and `fluid apply` refuses it without `--allow-data-loss`. The next build lands the
data again.

For partitioning, the replacement is made explicit: the provider plans adding
ingestion-time partitioning as an in-place update, which BigQuery then refuses, so
the table's `lifecycle.replace_triggered_by` names a `terraform_data` holding the
partitioning's shape. Changing `retention` later is an in-place update of
`expiration_ms`. Removing `expire` removes that trigger, which the data-loss gate
also refuses without `--allow-data-loss`; BigQuery cannot un-partition a table, so to
keep data longer, set a longer `retention` instead.

## Prerequisites

* The Cloud KMS API (`cloudkms.googleapis.com`) and the Data Catalog API
  (`datacatalog.googleapis.com`) enabled on the project, for keys and policy tags.
* The identity running `fluid apply` needs, beyond BigQuery: `roles/cloudkms.admin`
  (create key rings and keys, set their IAM), `roles/datacatalog.categoryAdmin`
  (taxonomies, tags and their IAM), and `bigquery.datasets.update` on the datasets
  (dataset IAM members).
* For masking and row filters on GCP: the BigQuery Data Policy API
  (`bigquerydatapolicy.googleapis.com`) enabled, and `bigquery.dataPolicies.*` and
  `bigquery.rowAccessPolicies.*` for the identity running `fluid apply`.
  `roles/bigquery.dataOwner` on the project carries both.
* A key ring and a crypto key cannot be deleted on GCP. `tofu destroy` removes them
  from state and schedules the key's versions for destruction (30 days by default);
  the next apply adopts the same names (`discover_imports`).
* `fluid verify`'s column check calls the Data Catalog API with Application Default
  Credentials, and needs `datacatalog.taxonomies.get` and
  `datacatalog.taxonomies.getIamPolicy`.
* On AWS, `fluid verify`'s Lake Formation check lists the table permissions the
  caller can see, so it must run as a Lake Formation administrator. When the
  contract's own grants are not in the listing, it reports an error rather than a
  pass.

## What is proven, and what is not

* `tofu validate` accepts every governed module shape (`tests/iac/test_iac_gcp_governance.py`).
* A real `tofu plan` and `apply` with terraform-provider-google, against an
  in-process stand-in for the BigQuery REST API, shows adding retention or a key to a
  live table plans its replacement (the data-loss gate refuses it), a new retention is
  in place, the governed table then plans clean, and the dataset's own access entries
  survive the grants (`tests/iac/test_iac_gcp_governance_plan.py`).
* A real `tofu plan` against moto accepts the Lake Formation grants with excluded
  columns, and `fluid verify`'s Lake Formation check runs against moto's stored grants
  (`tests/iac/test_iac_aws_column_restrictions.py`).
* The same stand-in shows a revoked reader plans one member destroy that the gate
  lets through, and that moving from the authoritative access list of 0.16.6 and earlier to member
  resources revokes a grant removed in the same change and then plans clean.
* **Measured against real Google Cloud**, 4 October 2026, on 0.18.0. In a demo lab, two
  lineage chains of eleven products were applied with `fluid apply --env gcp` from
  generated Jenkins pipelines, as a deploy service account reached by Workload Identity
  Federation:
  * Every apply created its dataset, key ring and key, its table with daily partitions
    that expire after the retention, its dataset IAM members, and a policy tag on each
    restricted column.
  * Every load into a policy-tagged column succeeded for the pipeline's identity.
  * Consumers read their upstreams bound to BigQuery from BigQuery.
  * The BigQuery service agent used each product's key: every load and query ran
    against a CMEK table.
  * Every `fluid verify` passed its retention, encryption and columnRestrictions
    dimensions against the live platform.
  * Querying as each principal: a denied principal's query of a restricted column is
    refused by that column's tag, a reader the restriction leaves reads it, and a
    principal with no dataset grant is refused the table. BigQuery's refusal reads
    "User has neither fine-grained reader nor masked get permission to get data
    protected by policy tag … on column …", not the documented "does not have
    permission to access policy tag".
* **Masking and row filters, measured by hand**, 5 October 2026, on the same lab's
  customer profile table, with the resources this release emits made by hand:
  * BigQuery: a `LAST_FOUR_CHARACTERS` data policy on a policy tag, the analyst a
    masked reader, read `XXXXX` and the last four digits, and the steward, a
    fine-grained reader, read the number.
  * AWS: a protected Glue view with the Athena expression above returned every row
    masked to the analyst, who was still refused the column on the table.
  * AWS: a data cells filter with the NULL-aware consent predicate returned 752 of
    10,172 rows to the analyst.
  * BigQuery: a `WRITE_TRUNCATE` load removed a table's row access policy and the
    table then showed every row; a `WRITE_TRUNCATE_DATA` load kept it.
* `tofu plan` with terraform-provider-aws 6.67 against the live state of the lab's
  eleven AWS products planned no change for any of them, with only the provider pin
  moved from `~> 5.0`.
* **Not proven yet**: an apply of the masking, row filter and label resources exactly
  as forge-cli emits them, on either cloud.
* **Not proven**: the Lake Formation half against a real account as 0.17.0 derives it
  from `columnRestrictions`. The grant shape it emits (excluded columns beside
  `wildcard`) was applied and enforced on a real account from 0.16.6, written by hand
  in the overlay.
