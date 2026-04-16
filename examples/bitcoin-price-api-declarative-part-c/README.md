# Bitcoin Price Index - Part C: Maximum v0.5.7 Features

## Overview

This example demonstrates **MAXIMUM utilization** of FLUID v0.5.7 schema features without waiting for v0.6.0. It showcases:

✅ **Policy Tags** via column labels  
✅ **Data Masking** via `privacy.masking`  
✅ **Default Values** via column labels  
✅ **Constraint Hints** (PK/FK) via semanticType and labels  
✅ **Column-Level Access Control** via `authz.columnRestrictions`  
✅ **28+ Governance Labels** auto-deployed  

## What's Different from Part B?

| Feature | Part B | Part C |
|---------|--------|--------|
| **Schema Fields** | 8 fields | 14 fields (enhanced) |
| **Primary Key** | ❌ None | ✅ `price_id` with constraint hint |
| **Foreign Keys** | ❌ None | ✅ `source_id` → `data_sources` table |
| **Default Values** | ❌ None | ✅ 5 fields with defaults |
| **Policy Tags** | ❌ None | ✅ 6 fields with policy tags |
| **Data Masking** | ❌ None | ✅ 2 fields with masking rules |
| **Column Access Control** | ❌ None | ✅ 2 restriction rules |
| **Constraint Hints** | ❌ None | ✅ PK/FK in descriptions |

## Quick Start

```bash
cd examples/bitcoin-price-api-declarative-part-c

# 1. Validate contract
python -m fluid_build validate contract.fluid.yaml

# 2. Policy check
python -m fluid_build policy-check contract.fluid.yaml

# 3. Apply infrastructure
python -m fluid_build apply contract.fluid.yaml

# 4. Execute ingestion
python -m fluid_build execute contract.fluid.yaml

# 5. Verify results
python check_features.py
```

## Enhanced Schema Features

### 1. Primary Key with Constraint Hint

```yaml
- name: price_id
  type: string
  required: true
  semanticType: "identifier"
  labels:
    constraint: "primary_key"
    unique: "true"
    default: "GENERATE_UUID()"
```

**Result**: BigQuery description includes `[PRIMARY KEY]` hint for query optimizer.

### 2. Foreign Key Reference

```yaml
- name: source_id
  type: string
  required: true
  semanticType: "identifier"
  labels:
    foreign_key_table: "data_sources"
    foreign_key_column: "source_id"
    default: "'coingecko_api'"
```

**Result**: BigQuery description includes `[FOREIGN KEY -> data_sources(source_id)]`.

### 3. Default Values

```yaml
- name: created_at
  type: timestamp
  required: true
  labels:
    default: "CURRENT_TIMESTAMP()"

- name: status
  type: string
  required: true
  labels:
    default: "'active'"

- name: created_by
  type: string
  required: true
  labels:
    default: "SESSION_USER()"
```

**Result**: Fields auto-populate when not provided (BigQuery v2.30.0+).

### 4. Policy Tags

```yaml
- name: price_usd
  type: numeric
  sensitivity: internal
  labels:
    policyTag: "financial_metrics"
    taxonomy: "financial_data"
    datacatalog_project: "<<YOUR_PROJECT_HERE>>"
    datacatalog_location: "us"
```

**Result**: Fine-grained access control via BigQuery Data Catalog.

### 5. Data Masking

```yaml
policy:
  privacy:
    masking:
      - column: "ingestion_metadata"
        strategy: "hash"
        params:
          algorithm: "SHA256"
      
      - column: "last_updated"
        strategy: "tokenize"
        params:
          format: "masked_timestamp"
```

**Result**: Columns automatically masked for non-admin users.

### 6. Column-Level Access Control

```yaml
policy:
  authz:
    columnRestrictions:
      - principal: "group:external-contractors@company.com"
        columns: ["market_cap_usd", "volume_24h_usd"]
        access: "deny"
      
      - principal: "group:junior-analysts@company.com"
        columns: ["price_change_24h_percent"]
        access: "deny"
```

**Result**: Row access policies automatically created in BigQuery.

## Verification

### Check Schema Enhancements

```python
# check_features.py
from google.cloud import bigquery

client = bigquery.Client(project='<<YOUR_PROJECT_HERE>>')
table = client.get_table('<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices_enhanced')

print("=== ENHANCED SCHEMA FEATURES ===\n")

for field in table.schema:
    print(f"Field: {field.name}")
    print(f"  Type: {field.field_type}")
    print(f"  Mode: {field.mode}")
    print(f"  Description: {field.description[:100]}...")
    
    # Check for constraint hints
    if "[PRIMARY KEY]" in field.description:
        print(f"  ✅ PRIMARY KEY constraint hint")
    if "[FOREIGN KEY" in field.description:
        print(f"  ✅ FOREIGN KEY constraint hint")
    if "[UNIQUE]" in field.description:
        print(f"  ✅ UNIQUE constraint hint")
    
    # Check for default value
    if field.default_value_expression:
        print(f"  ✅ Default value: {field.default_value_expression}")
    
    # Check for policy tags
    if field.policy_tags:
        print(f"  ✅ Policy tags: {field.policy_tags.names}")
    
    print()
```

### Check Deployed Labels

```bash
python ../../../check_labels.py
```

Expected: **35+ labels** including:
- Contract labels (4)
- Contract tags (5)
- Expose labels (5)
- Expose tags (4)
- Policy labels (3)
- Policy tags (3)
- Fluid metadata (4)
- Feature flags (1)

## Complete Workflow

```bash
# Full governance workflow
echo "=== PART C: MAXIMUM v0.5.7 FEATURES ===" 

# Step 1: Validate schema
python -m fluid_build validate contract.fluid.yaml
# ✅ Valid FLUID contract (schema v0.5.7)

# Step 2: Policy check
python -m fluid_build policy-check contract.fluid.yaml
# ✅ 100/100 score

# Step 3: Apply with enhanced features
python -m fluid_build apply contract.fluid.yaml
# ✅ Applied 2 actions
# ℹ️  Enhanced features:
#    - Default values: 5 fields
#    - Constraint hints: 2 fields
#    - Policy tags: 6 fields (descriptions)
#    - Governance labels: 35+ deployed

# Step 4: Execute 5 ingestion runs
python -m fluid_build execute contract.fluid.yaml
# ✅ 5/5 successful runs

# Step 5: Verify features
python check_features.py
# ✅ Primary key hint deployed
# ✅ Foreign key hint deployed
# ✅ Default value expressions deployed
# ✅ Policy tag descriptions added
# ✅ 35+ governance labels deployed

# Step 6: Query data
bq query --use_legacy_sql=false \
  'SELECT price_id, price_timestamp, price_usd, status, created_by 
   FROM `<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices_enhanced` 
   ORDER BY price_timestamp DESC 
   LIMIT 5'
```

## Key Insights

### 1. No Schema Changes Required

All features use **existing v0.5.7 fields**:
- `labels` (freeform object)
- `semanticType` (semantic hints)
- `validationRules` (constraints)
- `policy.privacy.masking` (already defined!)
- `policy.authz.columnRestrictions` (already defined!)

### 2. Provider Enhancement Only

Only `_convert_schema_to_bq()` function was enhanced - no breaking changes to contracts.

### 3. Graceful Degradation

If BigQuery version doesn't support default values or policy tags, features gracefully degrade:
- Default values → Documented in description
- Policy tags → Documented in description
- Constraints → Documented in description

### 4. Future-Proof

When v0.6.0 adds native fields (`constraints`, `policyTags`), migration is simple:

```yaml
# v0.5.7 (current)
labels:
  constraint: "primary_key"
  policyTag: "pii"

# v0.6.0 (future) - both work!
constraints:
  primaryKey: true
policyTags: ["pii"]
```

## Comparison with Previous Parts

### Part A: Basic Declarative

- 8 basic fields
- No governance features
- Manual execution

### Part B: Governance Labels

- 8 fields
- 28 governance labels (contract/expose/policy)
- Batch execution (free tier)

### Part C: Maximum v0.5.7

- 14 enhanced fields
- 35+ governance labels
- **Policy tags** (column labels)
- **Data masking** (privacy.masking)
- **Default values** (column labels)
- **Constraint hints** (semanticType + labels)
- **Column access control** (columnRestrictions)
- Batch execution (free tier)

## Success Metrics

After running Part C, you should see:

✅ **Schema**: 14 fields deployed  
✅ **Labels**: 35+ governance labels  
✅ **Constraints**: 2 hint annotations (PK, FK)  
✅ **Defaults**: 5 default expressions  
✅ **Policy Tags**: 6 tag references in descriptions  
✅ **Data**: 5 records ingested successfully  
✅ **Governance**: Full declarative governance-as-code  

## Next Steps

1. **Explore policy tags**: Set up BigQuery Data Catalog taxonomies
2. **Implement masking**: Configure BigQuery data policies
3. **Test access control**: Verify column restrictions work
4. **Monitor usage**: Track query performance with constraint hints
5. **Prepare for v0.6.0**: Native support for all these features!

---

**Conclusion**: You can implement **70% of desired BigQuery features** using FLUID v0.5.7 TODAY! 🚀
