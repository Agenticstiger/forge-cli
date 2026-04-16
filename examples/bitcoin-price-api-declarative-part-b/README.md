# Bitcoin Declarative Pipeline - Part B: Governance

**Building on Part A with Production-Grade Governance Policies**

This example extends [Part A](../bitcoin-price-api-declarative/) by adding comprehensive **data governance, compliance controls, and access policies** while maintaining the same GCP free tier infrastructure.

## 🎯 What's New in Part B?

### Governance Features Added

Part B demonstrates **schema-compliant governance** using FLUID v0.5.7's `policy` structure:

| Governance Feature | Part A | Part B | Description |
|-------------------|---------|---------|-------------|
| **Data Classification** | ❌ None | ✅ `classification: Internal` | Classify data sensitivity levels |
| **Authentication** | ❌ Default | ✅ `authn: iam` | GCP IAM-based authentication |
| **Authorization** | ❌ Open | ✅ `authz.readers/writers` | Role-based access control (RBAC) |
| **Column Restrictions** | ❌ None | ✅ `columnRestrictions` | Column-level access control |
| **Privacy Controls** | ❌ None | ✅ `privacy.rowLevelPolicy` | Row-level security (example) |
| **Policy Tags** | ❌ Basic | ✅ `policy.tags/labels` | Governance automation tags |
| **Field-level Tags** | ❌ None | ✅ Schema field tags | Business context metadata |
| **Compliance Metadata** | ❌ None | ✅ `labels` (GDPR, SOC2) | Compliance framework tracking |

### Comparison Table

| Aspect | Part A | Part B |
|--------|--------|--------|
| **Infrastructure** | BigQuery table | ✅ Same |
| **Data Pipeline** | API → BigQuery | ✅ Same |
| **Execution** | Manual trigger (5 iterations) | ✅ Same |
| **Region** | europe-west3 (GDPR) | ✅ Same |
| **Cost** | GCP Free Tier | ✅ Same |
| **Governance** | ❌ None | ✅ Full policies |
| **Policy Validation** | ❌ None | ✅ policy-check |
| **Access Control** | ❌ Open | ✅ RBAC (readers/writers) |
| **Privacy Controls** | ❌ None | ✅ Row-level policy |
| **Compliance** | ❌ None | ✅ GDPR/SOC2 metadata |

---

## 📋 Schema v0.5.7 Compliance

This contract validates against **FLUID Schema v0.5.7** with governance policies at `exposes[].policy`:

```yaml
exposes:
  - exposeId: bitcoin_prices_table
    
    # 🛡️ Governance policies (schema-compliant location)
    policy:
      # Data classification level
      classification: Internal  # Options: Public|Internal|Confidential|Restricted
      
      # Authentication method
      authn: iam  # Options: oidc|oauth2|api_key|none|custom|iam|jwt
      
      # Authorization: Who can read/write
      authz:
        readers:
          - group:data-analytics@company.com
          - group:finance-team@company.com
          - serviceAccount:looker@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com
        writers:
          - serviceAccount:data-pipeline@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com
          - group:data-engineering@company.com
        
        # Column-level access control
        columnRestrictions:
          - principal: "group:interns@company.com"
            columns: ["market_cap_usd", "volume_24h_usd"]
            access: deny
      
      # Privacy controls
      privacy:
        # Row-level security (example)
        rowLevelPolicy:
          expression: "ingestion_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
      
      # Policy metadata for automation
      tags:
        - gdpr-compliant
        - soc2-compliant
      
      labels:
        compliance_framework: "GDPR,SOC2,ISO27001"
        audit_required: "true"
```

### ✅ What v0.5.7 Schema Supports

| Feature | Supported | Location |
|---------|-----------|----------|
| **Classification** | ✅ Yes | `exposes[].policy.classification` |
| **Authentication** | ✅ Yes | `exposes[].policy.authn` |
| **Authorization (RBAC)** | ✅ Yes | `exposes[].policy.authz.readers/writers` |
| **Column Restrictions** | ✅ Yes | `exposes[].policy.authz.columnRestrictions` |
| **Privacy/Masking** | ✅ Yes | `exposes[].policy.privacy.masking` |
| **Row-Level Security** | ✅ Yes | `exposes[].policy.privacy.rowLevelPolicy` |
| **Policy Tags/Labels** | ✅ Yes | `exposes[].policy.tags/labels` |
| **Field-level Tags** | ✅ Yes | `exposes[].contract.schema[].tags` |
| **Sensitivity** | ✅ Yes | `exposes[].contract.schema[].sensitivity` |
| **Top-level Tags/Labels** | ✅ Yes | Root `tags`, `labels`, `metadata` |

### ❌ What v0.5.7 Schema Does NOT Support

| Feature | Status | Workaround |
|---------|--------|------------|
| **Root-level `policy`** | ❌ Not allowed | Use `exposes[].policy` instead |
| **Build-level `quality_checks`** | ❌ Not allowed | Document separately or use CI/CD |
| **Custom metadata fields** | ❌ Limited | Use standard `labels` and `tags` |
| **Field `pii`/`sensitive`** | ❌ Not allowed | Use `sensitivity` and `tags` |

### 📄 Reference: Comprehensive Governance Vision

See **`contract.fluid.yaml.full`** for a comprehensive governance contract with extended features:
- Root-level `policy` with 6 governance pillars
- Build-level `quality_checks` for data validation
- Field-level governance metadata (pii, sensitive, business_critical)
- Extended compliance metadata (x-governance section)

⚠️ **Important**: The `.full` version does **NOT validate** against schema v0.5.7 but demonstrates FLUID's governance vision for future schema versions. It **DOES pass** `policy-check` validation (100/100 score).

```bash
# Schema validation
python3 -m fluid_build validate contract.fluid.yaml.full
# ❌ FAILS - 13 errors (unsupported fields)

# Policy validation
python3 -m fluid_build policy-check contract.fluid.yaml.full
# ✅ PASSES - 100/100 score (governance logic is correct)
```

---

## 🚀 Quick Start

### Prerequisites

Same as Part A:
- GCP project with free tier enabled (`<<YOUR_PROJECT_HERE>>`)
- Python 3.9+
- `fluid_build` CLI installed
- GCP authentication configured

### 1. Validate Contract Structure

```bash
cd examples/bitcoin-price-api-declarative-part-b

# Validate schema compliance
python3 -m fluid_build validate contract.fluid.yaml
```

**Expected output:**
```
✅ Valid FLUID contract (schema v0.5.7)
Validation completed in 0.003s
```

### 2. Check Governance Policies

```bash
python3 -m fluid_build policy-check contract.fluid.yaml
```

**Expected output:**
```
🛡️ FLUID Policy Governance Report

Contract: crypto.bitcoin_prices_gcp_governed
Policy Score: 🏆 100/100 (EXCEPTIONAL)

✅ Checks Passed: 13
❌ Checks Failed: 0

✅ 🔒 Data Sensitivity & Privacy (2 checks passed)
✅ 🛡️ Access Control & Authorization (2 checks passed)
✅ 📊 Data Quality Standards (2 checks passed)
✅ 🔄 Lifecycle Management (2 checks passed)
✅ ⚡ Schema Evolution Control (2 checks passed)

✅ PASSED - Contract meets all governance requirements
```

### 3. Preview IAM Policies

Generate GCP IAM policies from governance declarations:

```bash
python3 -m fluid_build policy-compile contract.fluid.yaml --platform gcp --output-format terraform
```

**Example output:**
```hcl
# BigQuery Dataset IAM policies
resource "google_bigquery_dataset_iam_member" "crypto_data_readers" {
  dataset_id = "crypto_data"
  role       = "roles/bigquery.dataViewer"
  
  # From policy.authz.readers
  member     = "group:data-analytics@company.com"
}

resource "google_bigquery_dataset_iam_member" "crypto_data_writers" {
  dataset_id = "crypto_data"
  role       = "roles/bigquery.dataEditor"
  
  # From policy.authz.writers
  member     = "serviceAccount:data-pipeline@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com"
}

# Column-level security (BigQuery Policy Tags)
resource "google_bigquery_datapolicy_data_policy" "restrict_interns" {
  data_policy_id = "restrict_sensitive_columns"
  policy_tag     = "projects/<<YOUR_PROJECT_HERE>>/locations/europe-west3/taxonomies/finance/policyTags/sensitive"
  
  data_masking_policy {
    predefined_expression = "DEFAULT_MASKING_VALUE"
  }
}
```

⚠️ **Note**: `policy-compile` generates IaC but does **not apply** it. Use Terraform or `gcloud` to deploy IAM policies (requires paid tier features for advanced controls).

### 4. Apply Infrastructure (with Governance Metadata)

```bash
python3 -m fluid_build apply contract.fluid.yaml
```

**What gets created:**
- BigQuery dataset `crypto_data` in `europe-west3` (GDPR-compliant region)
- Table `bitcoin_prices` with schema
- **✅ 28+ governance labels deployed declaratively** including:
  - Contract labels: `cost_center`, `business_criticality`, `compliance_gdpr`, `compliance_soc2`
  - Contract tags: `tag_cryptocurrency`, `tag_real-time`, `tag_governed`, `tag_gdpr-compliant`
  - Expose labels: `sensitivity`, `data_domain`, `update_frequency`, `retention_years`, `region`
  - Expose tags: `tag_financial-data`, `tag_internal-use`, `tag_api-sourced`
  - Policy governance: `data_classification: internal`, `authn_method: iam`
  - Policy labels: `policy_compliance_framework`, `policy_audit_required`, `policy_data_lineage_tracked`
  - Policy tags: `policy_gdpr-compliant`, `policy_soc2-compliant`, `policy_financial-data`

**Verify governance labels are deployed:**
```bash
# Check all table labels
python3 -c "from google.cloud import bigquery; client = bigquery.Client(project='<<YOUR_PROJECT_HERE>>'); table = client.get_table('<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices'); print('\\n'.join([f'{k}: {v}' for k,v in sorted(table.labels.items())]))"

# Or using bq CLI
bq show --format=prettyjson <<YOUR_PROJECT_HERE>>:crypto_data.bitcoin_prices | jq .labels
```

**What does NOT get created automatically (requires additional configuration)**:
- IAM policies (requires `policy-compile` + manual Terraform apply)
- Column-level access control (requires BigQuery Policy Tags - paid tier)
- Row-level security (requires BigQuery authorized views - paid tier)
- Data masking (requires BigQuery DLP - paid tier)

### 5. Verify Applied State

```bash
python3 -m fluid_build verify contract.fluid.yaml
```

**Multi-dimensional checks:**
- ✅ **Structure**: Dataset exists, table exists, region correct
- ✅ **Types**: Schema field types match contract
- ✅ **Constraints**: Required fields, data type validation
- ⚠️ **Location**: Metadata applied (labels/tags visible in GCP Console)

### 6. Execute Pipeline (5 iterations demo)

```bash
python3 -m fluid_build execute contract.fluid.yaml
```

**Execution behavior:**
- Runs ingestion script **5 times** with **5-second delays**
- Configured in `execution.trigger.iterations` and `delaySeconds`
- Each run fetches Bitcoin prices and inserts into BigQuery
- Demonstrates: manual trigger, configurable iterations, delay control

**Expected output:**
```
🚀 Starting execution: bitcoin_price_ingestion
Trigger: manual (5 iterations, 5s delay)

Iteration 1/5: Fetching Bitcoin prices...
✅ Inserted 1 row (price_usd: $43,521.32)
⏳ Waiting 5 seconds...

Iteration 2/5: Fetching Bitcoin prices...
✅ Inserted 1 row (price_usd: $43,518.45)
⏳ Waiting 5 seconds...

[... continues for 5 iterations ...]

✅ Execution complete: 5 successful runs
```

### 7. Verify Data Quality

After execution:

```bash
python3 -m fluid_build verify contract.fluid.yaml --dimension constraints
```

**Data quality checks:**
- ✅ `price_usd > 0` (positive prices)
- ✅ `price_timestamp <= NOW()` (no future timestamps)
- ✅ `ingestion_timestamp IS NOT NULL` (audit trail)
- ✅ All required fields present (no nulls)

---

## 📋 Complete Workflow Comparison

### Part A: Basic Infrastructure + Execution + Verification

```bash
# Infrastructure lifecycle
fluid validate contract.fluid.yaml
fluid apply contract.fluid.yaml
fluid verify contract.fluid.yaml
fluid execute contract.fluid.yaml
fluid verify contract.fluid.yaml  # Check for drift
```

**Focus**: Get it working (infrastructure, execution, drift detection)

### Part B: Governance-First Approach

```bash
# 1. Governance validation FIRST
fluid policy-check contract.fluid.yaml  # Validate governance logic
fluid validate contract.fluid.yaml       # Validate schema compliance

# 2. Preview IAM policies (optional)
fluid policy-compile contract.fluid.yaml --platform gcp --output-format terraform

# 3. Infrastructure lifecycle
fluid apply contract.fluid.yaml
fluid verify contract.fluid.yaml

# 4. Execute with quality checks
fluid execute contract.fluid.yaml

# 5. Verify data quality
fluid verify contract.fluid.yaml --dimension constraints

# 6. Continuous governance monitoring (CI/CD)
fluid policy-check contract.fluid.yaml  # Scheduled in CI/CD
```

**Focus**: Get it right (compliance, access control, data quality, lifecycle management)

---

## 🛡️ Governance Policies Explained

### 1. Data Classification

```yaml
policy:
  classification: Internal
```

**Classification levels:**
- **Public**: Open data (e.g., public APIs, open datasets)
- **Internal**: Company-wide access (this example)
- **Confidential**: Restricted teams (e.g., finance, HR)
- **Restricted**: Highly sensitive (e.g., PII, PHI, PCI)

**Why it matters:**
- Determines default access controls
- Triggers compliance requirements
- Enables automated policy enforcement

### 2. Authentication Method

```yaml
policy:
  authn: iam
```

**Supported methods:**
- `iam`: GCP IAM (this example)
- `oidc`: OpenID Connect
- `oauth2`: OAuth 2.0
- `api_key`: API key authentication
- `jwt`: JSON Web Tokens
- `custom`: Custom authentication
- `none`: No authentication (public data)

**Why it matters:**
- Enforces authentication before access
- Integrates with identity providers
- Enables single sign-on (SSO)

### 3. Authorization (RBAC)

```yaml
policy:
  authz:
    readers:
      - group:data-analytics@company.com
      - group:finance-team@company.com
      - serviceAccount:looker@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com
    
    writers:
      - serviceAccount:data-pipeline@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com
      - group:data-engineering@company.com
```

**Why it matters:**
- Implements **least-privilege access** (RBAC)
- Separates read/write permissions
- Enables IAM policy automation via `policy-compile`
- Supports service accounts for automation

**Generated IAM policies:**
```bash
# Readers get BigQuery Data Viewer role
gcloud projects add-iam-policy-binding <<YOUR_PROJECT_HERE>> \
  --member="group:data-analytics@company.com" \
  --role="roles/bigquery.dataViewer"

# Writers get BigQuery Data Editor role
gcloud projects add-iam-policy-binding <<YOUR_PROJECT_HERE>> \
  --member="serviceAccount:data-pipeline@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"
```

### 4. Column-Level Access Control

```yaml
policy:
  authz:
    columnRestrictions:
      - principal: "group:interns@company.com"
        columns:
          - market_cap_usd
          - volume_24h_usd
        access: deny
```

**Why it matters:**
- Hides sensitive columns from specific users
- Enables fine-grained access control
- Supports compliance requirements (e.g., GDPR Article 25 - data minimization)

**Implementation:**
- Free tier: Document only (not enforced)
- Paid tier: Use BigQuery Policy Tags + Data Catalog

### 5. Privacy Controls (Row-Level Security)

```yaml
policy:
  privacy:
    rowLevelPolicy:
      expression: "ingestion_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
```

**Why it matters:**
- Restricts rows based on user context
- Implements data retention policies
- Enables time-based access control

**Example use cases:**
- Show only last 30 days of data to analysts
- Filter rows by user's department (e.g., `department = SESSION_USER().department`)
- Implement GDPR "right to be forgotten" (exclude deleted records)

**Implementation:**
- Free tier: Document only (not enforced)
- Paid tier: Use BigQuery Authorized Views with policy expressions

### 6. Policy Tags & Labels

```yaml
policy:
  tags:
    - gdpr-compliant
    - soc2-compliant
    - financial-data
  
  labels:
    compliance_framework: "GDPR,SOC2,ISO27001"
    audit_required: "true"
    data_lineage_tracked: "true"
```

**Why it matters:**
- Enables **policy automation** (e.g., auto-apply encryption based on tags)
- Supports **compliance reporting** (e.g., "show all GDPR-compliant datasets")
- Enables **cost allocation** (e.g., charge finance team for their data usage)
- Facilitates **data discovery** (e.g., search by compliance framework)

---

## 🔍 Policy Validation Workflow

### policy-check: Validate Governance Logic

```bash
python3 -m fluid_build policy-check contract.fluid.yaml
```

**What it checks:**
1. **Data Sensitivity & Privacy**: Classification present, PII documented, encryption specified
2. **Access Control & Authorization**: Readers/writers defined, separation of duties
3. **Data Quality Standards**: SLA defined, validation rules present
4. **Lifecycle Management**: Retention policy, archival/backup strategy
5. **Schema Evolution Control**: Breaking change rules, allowed operations
6. **Compliance & Audit**: Compliance frameworks, audit logging

**Score calculation:**
- Each pillar worth ~16.67 points
- 100/100 = All governance requirements met
- 80-99 = Some optional policies missing
- < 80 = Critical policies missing

### validate: Validate Schema Compliance

```bash
python3 -m fluid_build validate contract.fluid.yaml
```

**What it checks:**
- JSON schema compliance (v0.5.7)
- Required fields present
- Data types correct
- Enum values valid
- No additional properties (strict mode)

**Difference between policy-check and validate:**

| Command | Purpose | Validates |
|---------|---------|-----------|
| `policy-check` | Governance logic | Policy completeness, best practices, compliance |
| `validate` | Schema compliance | JSON structure, data types, required fields |

**Example:**
- `contract.fluid.yaml` → ✅ Both pass (schema-compliant governance)
- `contract.fluid.yaml.full` → ❌ validate fails, ✅ policy-check passes (comprehensive but not schema-compliant)

---

## 💡 Benefits of Governance-First Approach

### 1. Compliance by Design

```yaml
# Part A: Add compliance AFTER data is exposed (reactive)
# - Data already in production
# - Compliance issues discovered during audit
# - Expensive retroactive fixes

# Part B: Build compliance INTO the contract (proactive)
policy:
  classification: Internal
  compliance:
    frameworks: [GDPR, SOC2, ISO27001]
```

**Result**: Pass audits, avoid fines, reduce compliance debt

### 2. Access Control Automation

```yaml
# Part A: Manually configure IAM policies (error-prone)
# $ gcloud projects add-iam-policy-binding ... (forgotten, misconfigured)

# Part B: Declare access in contract, generate policies
policy:
  authz:
    readers: [group:data-analytics@company.com]
# $ fluid policy-compile → generates Terraform → automated deployment
```

**Result**: No forgotten permissions, consistent RBAC, audit trail

### 3. Data Quality SLAs

```yaml
# Part A: Implicit quality expectations (undocumented)
# - Consumers don't know data freshness
# - No validation rules
# - Silent data quality issues

# Part B: Explicit SLAs and quality checks
x-governance:
  data_quality:
    sla:
      availability: 99.9
      freshness_minutes: 15
    validation_rules:
      - rule: "price_usd > 0"
```

**Result**: Measurable SLAs, automated validation, trust in data

### 4. Schema Evolution Safety

```yaml
# Part A: Schema changes break downstream consumers
# - Drop field → pipelines crash
# - Change type → data loss
# - No approval process

# Part B: Governed schema evolution
x-governance:
  schema_evolution:
    breaking_changes_allowed: false
    forbidden_operations: [drop_field, change_field_type]
```

**Result**: Backward compatibility, safe schema changes, no surprise breakage

---

## 🎓 Key Takeaways

### Part A → Part B Evolution

| Principle | Part A | Part B |
|-----------|--------|--------|
| **Documentation** | Code = documentation | Contract = documentation |
| **Access Control** | Ad-hoc IAM changes | Declared policies → automated IaC |
| **Data Quality** | Hope for the best | Explicit SLAs + validation |
| **Compliance** | Post-hoc audits | Compliance by design |
| **Schema Changes** | YOLO deployments | Governed evolution |

### When to Use Part B Approach

✅ **Use Part B governance when:**
- Building production data products
- Handling regulated data (finance, healthcare, PII)
- Multiple teams accessing data (need RBAC)
- Compliance requirements (GDPR, SOC2, HIPAA)
- Data quality is critical (SLAs needed)
- Long-lived datasets (retention policies)

❌ **Part A is sufficient when:**
- Quick prototypes or POCs
- Internal-only data pipelines
- Single-user/single-team access
- No compliance requirements
- Short-lived datasets
- Public data with no sensitivity

---

## 📚 Further Reading

### FLUID Documentation

- [Policy-Check Command](../../docs/cli/policy-check.md) - Governance validation
- [Policy-Compile Command](../../docs/cli/policy-compile.md) - IAM policy generation
- [Declarative Execution Flow](../../docs/concepts/declarative-execution-flow.md) - Complete workflow
- [Verify Command](../../docs/cli/verify.md) - Multi-dimensional drift detection

### Governance Concepts

- [Data Classification Standards](../../docs/concepts/data-classification.md)
- [RBAC Best Practices](../../docs/concepts/rbac.md)
- [Schema Evolution Policies](../../docs/concepts/schema-evolution.md)
- [GDPR Compliance Guide](../../docs/compliance/gdpr.md)

### GCP Resources

- [BigQuery IAM Roles](https://cloud.google.com/bigquery/docs/access-control)
- [BigQuery Policy Tags](https://cloud.google.com/bigquery/docs/column-level-security)
- [BigQuery Row-Level Security](https://cloud.google.com/bigquery/docs/row-level-security)
- [GCP Compliance](https://cloud.google.com/security/compliance)

---

## 🆘 Troubleshooting

### Schema Validation Fails

```bash
❌ Invalid FLUID contract (13 error(s))
1. root: Additional properties not allowed ('policy' unexpected)
```

**Solution**: Move `policy` from root to `exposes[].policy`:
```yaml
# ❌ Wrong
policy:
  sensitivity: ...

# ✅ Correct
exposes:
  - exposeId: my_table
    policy:
      classification: Internal
```

### Policy-Check Passes but Validate Fails

**Explanation**: Two different validation layers:
- `policy-check` → Validates governance logic (can use custom fields)
- `validate` → Validates JSON schema v0.5.7 (strict)

**Solution**: Use schema-compliant contract for deployment, keep `.full` version for documentation

### IAM Policies Not Applied

**Explanation**: `apply` creates infrastructure but does NOT apply IAM policies (free tier limitation).

**Solution**: Use `policy-compile` to generate Terraform/gcloud commands, then apply manually:
```bash
fluid policy-compile contract.fluid.yaml --platform gcp > iam_policies.tf
terraform apply
```

### Column Restrictions Not Enforced

**Explanation**: Column-level access control requires BigQuery Policy Tags (paid tier feature).

**Solution**:
- Free tier: Document in contract (governance metadata)
- Paid tier: Use BigQuery Data Catalog + Policy Tags

---

## 🚀 Next Steps

1. **Deploy IAM Policies** (requires paid tier):
   ```bash
   fluid policy-compile contract.fluid.yaml --platform gcp --output-format terraform | terraform apply
   ```

2. **Enable Column-Level Security** (requires paid tier):
   - Create BigQuery Policy Tags in Data Catalog
   - Apply tags to sensitive columns
   - Configure masking strategies

3. **Implement Row-Level Security** (requires paid tier):
   - Create authorized views
   - Implement policy expressions
   - Test with different user roles

4. **Set Up Monitoring**:
   - Create Cloud Monitoring dashboards
   - Set up alerts for SLA violations
   - Track data quality metrics

5. **Integrate CI/CD**:
   - Add `policy-check` to PR checks
   - Add `validate` to deployment pipeline
   - Automate `policy-compile` + Terraform apply

---

## 📝 License

Same as Part A - see [LICENSE](../../LICENSE)
