# Bitcoin Declarative Flow - Step-by-Step Walkthrough

**Date**: December 8, 2025  
**Example**: `bitcoin-price-api-declarative-part-a`  
**Purpose**: Guided walkthrough with GCP console links and observability improvements

---

## 📋 Complete Command Reference

### Step 1: Project Structure
```bash
# View project files
ls -la
tree  # (if available)

# Key files
# - contract.fluid.yaml (declarative infrastructure)
# - runtime/ingest.py (imperative data logic)
```

### Step 2: Examine Contract
```bash
# View the contract
cat contract.fluid.yaml

# Key sections:
# - fluidVersion: 0.5.7
# - builds: References runtime/ingest.py
# - exposes: Defines BigQuery table schema
# - execution: Schedule every 15 minutes
```

### Step 3: Validate Contract
```bash
python -m fluid_build validate contract.fluid.yaml

# Expected output:
# ✅ Valid FLUID contract (schema v0.5.7)
# Metric: validation_errors=0count
```

### Step 4: Preview Changes (Dry Run)
```bash
python -m fluid_build apply contract.fluid.yaml --dry-run

# Shows:
# - bq.ensure_dataset
# - bq.ensure_table
```

### Step 5: Check GCP Authentication
```bash
# List authenticated accounts
gcloud auth list

# Check current project
gcloud config get-value project

# Should return: <<YOUR_PROJECT_HERE>>
```

### Step 6: Deploy Infrastructure
```bash
python -m fluid_build apply contract.fluid.yaml

# Creates:
# - Dataset: crypto_data
# - Table: bitcoin_prices (9 fields)
# Duration: ~5 seconds
```

### Step 7: Verify in GCP Console

**Open these URLs in your browser:**

1. **BigQuery Main Console**
   ```
   https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>
   ```

2. **Dataset 'crypto_data'**
   ```
   https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=!1m4!1m3!3m2!1s<<YOUR_PROJECT_HERE>>!2scrypto_data
   ```

3. **Table 'bitcoin_prices'**
   ```
   https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=!1m5!1m4!4m3!1s<<YOUR_PROJECT_HERE>>!2scrypto_data!3sbitcoin_prices
   ```

**What to verify:**
- ✅ Dataset 'crypto_data' exists
- ✅ Table 'bitcoin_prices' exists with 9 fields
- ✅ Schema matches contract definition
- ✅ Creation timestamp is recent

**CLI Verification:**
```bash
# Verify table from command line
python -c "from google.cloud import bigquery; \
  client = bigquery.Client(project='<<YOUR_PROJECT_HERE>>'); \
  table = client.get_table('<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices'); \
  print(f'Table: {table.table_id}'); \
  print(f'Schema fields: {len(table.schema)}'); \
  print(f'Created: {table.created}'); \
  print(f'Rows: {table.num_rows}')"
```

### Step 8: Examine Runtime Script
```bash
# View the Python data ingestion script
cat runtime/ingest.py

# Key functions:
# - fetch_bitcoin_price() - Calls CoinGecko API
# - load_to_bigquery() - Inserts data to BigQuery
# - main() - Orchestrates the flow
```

### Step 9: Test Runtime Script
```bash
python runtime/ingest.py

# Expected flow:
# 1. Configuration logged
# 2. API call to CoinGecko
# 3. Current BTC price displayed
# 4. BigQuery load attempt (fails on free tier streaming insert)
```

---

## 🔍 Observability Improvement Notes

### Note #1: Project Structure Visualization
**Current State**: No file tree visualization  
**Missing**: Clear project structure in logs  
**Improvement**:
```
✅ Add to 'fluid doctor':
   bitcoin-price-api-declarative-part-a/
   ├── contract.fluid.yaml
   ├── runtime/
   │   └── ingest.py
   ├── README.md
   └── HOW_IT_WORKS.md
```

---

### Note #2: Contract Summary in Validation
**Current State**: Only shows "Valid FLUID contract"  
**Missing**: What was actually validated?  
**Improvement**:
```
✅ Show parsed contract summary:
   
   📋 Contract Summary
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Product ID:   crypto.bitcoin_prices_gcp
   Name:         Bitcoin Price Index
   Domain:       finance
   
   Builds:       1 (bitcoin_price_ingestion)
   └─ Engine:    python
   └─ Schedule:  */15 * * * * (every 15 min)
   └─ Resources: 1 CPU, 2Gi memory
   
   Exposes:      1 (bitcoin_prices_table)
   └─ Platform:  gcp/bigquery
   └─ Schema:    9 fields
   └─ Location:  <<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices
   
   Validation Checks:
   ✓ Schema version: 0.5.7
   ✓ Required fields present
   ✓ External file exists: runtime/ingest.py
   ✓ Cron expression valid
   ✓ GCP naming conventions
```

---

### Note #3: Validation Checklist
**Current State**: Binary pass/fail  
**Missing**: Detailed validation steps  
**Improvement**:
```
✅ Show validation progress:
   
   Validating contract.fluid.yaml...
   ✓ YAML syntax valid
   ✓ Schema version 0.5.7 compatible
   ✓ Required metadata present
   ✓ Build references resolved (runtime/ingest.py exists)
   ✓ BigQuery identifiers valid
   ✓ Cron expression valid: */15 * * * *
   ✓ Resource specifications valid
   ✓ No circular dependencies
   
   ✅ All checks passed (8/8)
```

---

### Note #4: Empty Details in Dry Run
**Current State**: Details column shows `{}`  
**Missing**: What will actually be created?  
**Improvement**:
```
✅ Show detailed planned actions:
   
   📋 Planned Actions (2)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   
   1. bq.ensure_dataset
      Project:  <<YOUR_PROJECT_HERE>>
      Dataset:  crypto_data
      Location: US
      Labels:   layer=Gold, domain=finance
   
   2. bq.ensure_table
      Table:       bitcoin_prices
      Schema:      9 fields
      └─ price_timestamp (TIMESTAMP, NULLABLE)
      └─ price_usd (NUMERIC, NULLABLE)
      └─ price_eur (NUMERIC, NULLABLE)
      └─ price_gbp (NUMERIC, NULLABLE)
      └─ market_cap_usd (NUMERIC, NULLABLE)
      └─ volume_24h_usd (NUMERIC, NULLABLE)
      └─ price_change_24h_percent (NUMERIC, NULLABLE)
      └─ last_updated (TIMESTAMP, NULLABLE)
      └─ ingestion_timestamp (TIMESTAMP, NULLABLE)
      Partitioning: None
      Clustering:   None
```

---

### Note #5: Provider Discovery Spam
**Current State**: 20+ lines of provider registration logs  
**Missing**: Clean, concise output  
**Improvement**:
```
❌ Remove verbose logs:
   provider_registered_explicit
   provider_module_imported
   provider_duplicate_ignored
   provider_registered_by_subclass
   provider_discovery_complete

✅ Replace with:
   🔌 Initialized provider: gcp (<<YOUR_PROJECT_HERE>>, region: US)
```

---

### Note #6: Pre-flight Authentication Check
**Current State**: No auth verification before deployment  
**Missing**: Early failure detection  
**Improvement**:
```
✅ Add pre-flight checks to 'fluid apply':
   
   🔍 Pre-flight Checks
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   ✓ GCP authentication:
   ✓ Current project: <<YOUR_PROJECT_HERE>>
   ✓ Required permissions: BigQuery Admin
   ✓ API enabled: BigQuery API
   ✓ Quota available: 10 TB/day remaining
   ✓ Billing enabled: Yes
   
   All checks passed. Ready to deploy.

💡 Suggestion: Add 'fluid doctor --provider gcp' command
```

---

### Note #7: Deployment Progress Indicators
**Current State**: Silent during 5-second deployment  
**Missing**: Real-time progress feedback  
**Improvement**:
```
✅ Show step-by-step progress:
   
   🚀 Deploying infrastructure...
   
   ⏳ [1/2] Creating dataset 'crypto_data'...
   ✅ Dataset created (0.8s)
      📍 https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=...
   
   ⏳ [2/2] Creating table 'bitcoin_prices' with 9 fields...
   ✅ Table created (4.1s)
      📍 https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=...
   
   ✅ Deployment complete in 4.97s
```

---

### Note #8: Post-Deployment Verification
**Current State**: No automatic verification  
**Missing**: Confirmation that resources were actually created  
**Improvement**:
```
✅ Auto-verify after deployment:
   
   🔍 Verifying deployment...
   ✓ Dataset exists: crypto_data
   ✓ Table exists: bitcoin_prices
   ✓ Schema matches contract: 9 fields
   ✓ Permissions configured correctly
   
   📊 Resource Summary:
   Dataset:    <<YOUR_PROJECT_HERE>>.crypto_data
   Table:      bitcoin_prices
   Rows:       0
   Size:       0 bytes
   Created:    2025-12-08 13:17:30 UTC
   
   🌐 GCP Console:
   https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=...

💡 Suggestion: Add 'fluid apply --verify' flag
```

---

### Note #9: Runtime Script Logging
**Current State**: Good structured logging with emojis  
**Strengths**:
- ✅ Clear configuration display
- ✅ API call success/failure
- ✅ Current Bitcoin price shown
- ✅ Error messages are descriptive

**Missing**: Log aggregation and metrics  
**Improvement**:
```
✅ Integrate with GCP Cloud Logging:
   
   import google.cloud.logging
   
   # Setup Cloud Logging
   logging_client = google.cloud.logging.Client()
   logging_client.setup_logging()
   
   # All logs now sent to:
   # https://console.cloud.google.com/logs
   
   # Query logs with:
   resource.type="cloud_function"
   resource.labels.function_name="bitcoin_price_ingestion"
   severity>=INFO
```

---

### Note #10: Metrics Export
**Current State**: No metrics exported  
**Missing**: Operational dashboards  
**Improvement**:
```
✅ Export custom metrics to Cloud Monitoring:
   
   from google.cloud import monitoring_v3
   
   # Export metrics:
   - bitcoin_price_usd (gauge)
   - api_call_latency_ms (histogram)
   - ingestion_success_count (counter)
   - ingestion_failure_count (counter)
   
   # View dashboard at:
   # https://console.cloud.google.com/monitoring/dashboards
   
   # Create alerts on:
   - API call failures > 5 in 5 minutes
   - Bitcoin price anomaly (>10% change)
   - Ingestion lag > 1 hour
```

---

### Note #11: Distributed Tracing
**Current State**: No tracing  
**Missing**: End-to-end request flow visibility  
**Improvement**:
```
✅ Add Cloud Trace integration:
   
   from google.cloud import trace_v1
   
   # Trace flow:
   1. Cloud Scheduler trigger
   2. Cloud Function invocation
   3. CoinGecko API call
   4. BigQuery insert
   
   # View traces at:
   # https://console.cloud.google.com/traces
   
   # Shows:
   - Total latency breakdown
   - API call duration
   - BigQuery write duration
   - Network latency
```

---

### Note #13: Dataset Region Configuration ✅ FIXED
**Issue**: Dataset created in US region instead of specified `europe-west3`  
**Root Cause**: GCP planner (`fluid_build/providers/gcp/plan/planner.py`) was reading `properties.get("location")` instead of `properties.get("region")`  
**Fix Applied**:
```python
# Before (line 131 in planner.py)
"location": properties.get("location", "US")

# After (FIXED)
dataset_location = properties.get("region") or properties.get("location", "US")
"location": dataset_location
```

**Verification**:
```bash
# Contract specifies region
region: europe-west3

# Deployment log shows correct region
DEBUG BigQuery Action: ensure_dataset called with location=europe-west3

# Dataset created in correct region
$ python -c "from google.cloud import bigquery; ..."
Dataset region: europe-west3 ✅
```

**Impact**: 
- ✅ GDPR compliance (EU data in EU region)
- ✅ Data residency requirements met
- ✅ Proper multi-region architecture support

**Schema Compatibility**: FLUID schema v0.5.7 supports `binding.location.region` property

---

### Note #12: Error Alerting
**Current State**: Errors only in logs  
**Missing**: Proactive notifications  
**Improvement**:
```
✅ Configure Cloud Monitoring alerts:
   
   Alert 1: API Call Failures
   Condition: error_count > 3 in 5 minutes
   Channel:   Slack #data-alerts
   
   Alert 2: Data Freshness
   Condition: last_ingestion_timestamp > 30 minutes ago
   Channel:   PagerDuty
   
   Alert 3: Bitcoin Price Anomaly
   Condition: price_change_24h > 15%
   Channel:   Email to data-team@company.com
   
   # Configure at:
   # https://console.cloud.google.com/monitoring/alerting
```

---

## 📊 Observability Maturity Roadmap

### Phase 1: Basic (Current State)
- ✅ Stdout logging
- ✅ Validation metrics
- ✅ Deployment duration

### Phase 2: Enhanced (Proposed)
- 🔲 Structured JSON logging
- 🔲 Cloud Logging integration
- 🔲 Deployment progress indicators
- 🔲 Post-deployment verification
- 🔲 Detailed dry-run output

### Phase 3: Production-Ready (Advanced)
- 🔲 Custom metrics export
- 🔲 Cloud Monitoring dashboards
- 🔲 Distributed tracing
- 🔲 Alerting policies
- 🔲 SLO tracking
- 🔲 Cost analysis
- 🔲 Security audit logs

---

## 🎯 Quick Command Cheat Sheet

```bash
# Validate
python -m fluid_build validate contract.fluid.yaml

# Dry run
python -m fluid_build apply contract.fluid.yaml --dry-run

# Deploy
python -m fluid_build apply contract.fluid.yaml

# Verify (manual)
python -c "from google.cloud import bigquery; \
  client = bigquery.Client(project='<<YOUR_PROJECT_HERE>>'); \
  table = client.get_table('<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices'); \
  print(f'✅ {table.dataset_id}.{table.table_id}: {len(table.schema)} fields, {table.num_rows} rows')"

# Run ingestion
python runtime/ingest.py

# Check logs (when deployed)
gcloud logging read "resource.type=cloud_function \
  AND resource.labels.function_name=bitcoin_price_ingestion" \
  --limit 50 --format json
```

---

## 🌐 Essential GCP Console Links

| Resource | URL |
|----------|-----|
| BigQuery Main | https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>> |
| Dataset | https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=!1m4!1m3!3m2!1s<<YOUR_PROJECT_HERE>>!2scrypto_data |
| Table | https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>&ws=!1m5!1m4!4m3!1s<<YOUR_PROJECT_HERE>>!2scrypto_data!3sbitcoin_prices |
| Cloud Logging | https://console.cloud.google.com/logs?project=<<YOUR_PROJECT_HERE>> |
| Cloud Monitoring | https://console.cloud.google.com/monitoring?project=<<YOUR_PROJECT_HERE>> |
| Cloud Trace | https://console.cloud.google.com/traces?project=<<YOUR_PROJECT_HERE>> |
| Cloud Composer | https://console.cloud.google.com/composer?project=<<YOUR_PROJECT_HERE>> |
| IAM & Admin | https://console.cloud.google.com/iam-admin?project=<<YOUR_PROJECT_HERE>> |

---

## 💡 Key Takeaways

1. **Declarative = Infrastructure** - YAML contract creates BigQuery resources
2. **Imperative = Data Logic** - Python script handles API calls and transformations
3. **Separation of Concerns** - Each layer has clear responsibilities
4. **Observability Gaps** - Current implementation lacks production-grade monitoring
5. **Free Tier Limits** - Streaming inserts blocked, use batch loading instead
6. **GCP Console Essential** - Visual verification complements CLI commands
7. **Region Configuration** - ✅ Fixed! Datasets now properly respect `region` property in contracts

---

**Next Steps:**
1. ✅ ~~Region configuration support~~ (COMPLETED - Note #13)
2. Implement observability improvements from Notes #2-12
3. Create Part B with paid account features (Cloud Composer, streaming inserts)
4. Build monitoring dashboard in Cloud Monitoring
5. Set up alerting policies for production
