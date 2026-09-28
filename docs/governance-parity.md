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
| **Retention**: `exposes[].lifecycle {retention, expire: true}` | One S3 lifecycle rule per expose, filtered to the binding's prefix, expiring objects `retention` after they are written (`aws_s3_bucket_lifecycle_configuration`). | Daily partitions that expire `retention` after their day ends: `google_bigquery_table.time_partitioning {type: DAY, expiration_ms}`, on `binding.location.partitionBy` when it names one date or timestamp column, else by ingestion time. Never a table expiration, which would delete the product. | AWS: the enabled prefix rule's `Expiration.Days`, and no rule that expires sooner. GCP: the table's partition type, field and `expirationMs`, and that the table itself has no `expirationTime`. |
| **Encryption at rest**: `binding.encryption.kms` | `product`: a KMS key per bucket (`aws_kms_key`, rotation on, alias `alias/fluid/<id>/<bucket>`) as the bucket's default SSE-KMS. `alias/...` or an ARN: that key. `none`: SSE-S3. | `product`: a key ring and key per dataset (`google_kms_key_ring` `fluid-<id>-<dataset>`, `google_kms_crypto_key` `bigquery`, 90-day rotation) in the dataset's location, `roles/cloudkms.cryptoKeyEncrypterDecrypter` for the BigQuery service agent (`google_kms_crypto_key_iam_member`), used as the dataset's `default_encryption_configuration` and the table's `encryption_configuration`. `projects/.../cryptoKeys/...`: that key. `none`: Google-managed keys. | AWS: the key is `Enabled`, and the objects under the prefix are SSE-KMS with it. GCP: `kmsKeyName` of the table, and of the dataset's default when the product owns the dataset. |
| **Column restrictions**: `exposes[].policy.authz.columnRestrictions` | Each Lake Formation grant with `SELECT` excludes the restricted columns its principal may not read (`aws_lakeformation_permissions.table_with_columns.excluded_column_names`, with `wildcard`). | A Data Catalog taxonomy per product and dataset with fine-grained access control (`google_data_catalog_taxonomy`), a policy tag per set of restricted columns that share their readers (`google_data_catalog_policy_tag`), attached through the table schema's `policyTags`, and `roles/datacatalog.categoryFineGrainedReader` for exactly the allowed readers (`google_data_catalog_policy_tag_iam_member`). | AWS: `lakeformation:ListPermissions` on tables; no `SELECT` of a denied principal, of a read grantee, or of `IAM_ALLOWED_PRINCIPALS` reaches a column it may not read, including grants made outside the contract. GCP: every restricted column carries a tag, the tag's fine-grained readers are exactly the derived set (a denied principal, or one granted outside the contract, fails), and the taxonomy enforces fine-grained access control. |
| **Access grants**: `accessPolicy.grants[]` | Not emitted: on AWS, access is the binding's `governance.lakeFormation.grants` (`fluid validate` warns). | One non-authoritative `google_bigquery_dataset_iam_member` per role and member (and `google_storage_bucket_iam_member` for GCS), with the logical principal mapped to its GCP identity. | Not checked yet on either cloud. |

A policy that a binding cannot apply is refused at `fluid validate`, `fluid plan` and
`fluid apply`, never dropped: retention, a key or a column restriction on a GCP
binding that is not a BigQuery table (GCS, Pub/Sub, Iceberg storage), a column
restriction on an AWS binding with no Lake Formation grants or on a non-Glue format,
an AWS key reference on GCP and a Cloud KMS key name on AWS.

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
  principal in a reserved top-level domain (`.example`, `.test`, `.invalid`,
  `.localhost`) is refused as a placeholder (`principal-placeholder`).
* On AWS an unmapped restriction principal must already be an IAM ARN.

The pattern follows ODCS v3, which declares `roles[]` once and binds them per server
(`servers[].roles`), and dbt `grants`, which resolve the grantee per target.

## Column restriction semantics

* A column named in any restriction is restricted.
* `deny`: the principal may not read the columns.
* `allow`: the columns are readable only by the principals an `allow` names.
* A deny beats an allow. A restriction never grants access: the readers are the
  expose's readers (the `accessPolicy` read grantees on GCP, the Lake Formation
  `SELECT` grantees on AWS). An allowed principal that is not a reader is logged,
  not added.
* On AWS a grant's hand-written `excludedColumns` keeps working. With a restriction
  on the same expose the two must agree, or the emit is refused
  (`column-restriction-conflict`).

On GCP a denied principal gets an access error on the restricted columns;
`SELECT * EXCEPT (customer_id, msisdn)` still works for it. BigQuery dynamic data
masking (`google_bigquery_datapolicy_data_policy`, SHA-256 or nullify, for principals
holding `roles/bigquerydatapolicy.maskedReader`) is not emitted yet. It needs a third
principal set (who sees masked values rather than an error) that the contract has no
field for, and a column masked at landing (`policy.privacy.masking`) would be hashed
twice.

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
* **Not proven**: anything against real BigQuery, Cloud KMS, Data Catalog or Lake
  Formation. No emulator enforces IAM, policy tags or keys: that a denied principal's
  query is refused, that the BigQuery service agent can use the key, and that a load
  into a policy-tagged column succeeds for the pipeline's identity rest on the
  providers' documentation.
