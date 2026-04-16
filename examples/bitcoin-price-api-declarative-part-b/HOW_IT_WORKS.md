# Declarative Contracts + Runtime Logic: How They Work Together

**TL;DR**: Declarative YAML handles infrastructure. Imperative code handles data transformation. Both are needed!

**Part A Status**: ✅ Working (GCP free tier has streaming insert limitation - use batch load instead)

---

## The Confusion

When you see this in `contract.fluid.yaml`:

```yaml
# THIS VERSION (Part A) has NO builds section
# Infrastructure only - data ingestion happens separately
exposes:
  - exposeId: bitcoin_prices_table
    kind: table
    contract:
      schema: [...]  # Schema definition
```

**Question**: "How does the Bitcoin API actually get called?"

**Answer**: By the Python script at `runtime/ingest.py` - which YOU run manually OR schedule!

---

## Part A: Simplified Approach (Free Tier)

### Phase 1: Infrastructure Creation (Declarative) ✅

**What**: `fluid apply contract.fluid.yaml --provider gcp`

**Does**:
```python
# FLUID internally executes (you don't write this):
from google.cloud import bigquery

client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")

# Create dataset
dataset = bigquery.Dataset("<<YOUR_PROJECT_HERE>>.crypto_data")
client.create_dataset(dataset)

# Create table with schema from contract
schema = [
    bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
    # ... fields from contract.fluid.yaml
]
table = bigquery.Table("<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices", schema=schema)
client.create_table(table)
```

**Result**: Empty BigQuery table exists with the correct schema

# Generate Cloud Composer DAG that runs runtime/ingest.py every 15 minutes
# (DAG generation happens here too)
```

**Result**: Empty BigQuery table ready to receive data

---

### Phase 2: Data Ingestion (Imperative) ✅

**What**: `python runtime/ingest.py` (runs every 15 min via Composer)

**Does**:
```python
# YOU write this code in runtime/ingest.py:
import requests
from google.cloud import bigquery

# ACTUAL API CALL #1: Fetch Bitcoin data
response = requests.get(
    "https://api.coingecko.com/api/v3/simple/price",
    params={
        'ids': 'bitcoin',
        'vs_currencies': 'usd,eur,gbp',
        'include_market_cap': 'true',
        'include_24hr_vol': 'true'
    }
)
data = response.json()

# Transform to match contract schema
record = {
    'price_timestamp': datetime.utcnow().isoformat(),
    'price_usd': data['bitcoin']['usd'],
    'price_eur': data['bitcoin']['eur'],
    'price_gbp': data['bitcoin']['gbp'],
    'market_cap_usd': data['bitcoin']['usd_market_cap'],
    'volume_24h_usd': data['bitcoin']['usd_24h_vol'],
    # ...
}

# ACTUAL API CALL #2: Load to BigQuery
client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
client.insert_rows_json(
    "<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices",
    [record]
)
```

**Result**: Data flowing from CoinGecko → BigQuery

---

## Visual Flow

```
┌─────────────────────────────────────────────────────────────┐
│ YOU WRITE                                                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ contract.fluid.yaml                                         │
│   exposes:                                                  │
│     - binding:                                              │
│         platform: gcp                                       │
│         format: bigquery_table                              │
│       contract:                                             │
│         schema: [...]                                       │
│                                                             │
│   builds:                                                   │
│     - script: runtime/ingest.py  ← Points to your code     │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ runtime/ingest.py                                           │
│   def fetch_bitcoin_price():                                │
│     response = requests.get(COINGECKO_API)  ← Real call!   │
│                                                             │
│   def load_to_bigquery(record):                             │
│     client.insert_rows_json(...)  ← Real call!              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ FLUID EXECUTES                                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ $ fluid apply contract.fluid.yaml --provider gcp            │
│                                                             │
│ Phase 1: Infrastructure (one-time)                          │
│   ✅ Create dataset: crypto_data                            │
│   ✅ Create table: bitcoin_prices (with your schema)        │
│   ✅ Deploy Composer DAG (calls runtime/ingest.py)          │
│                                                             │
│ Phase 2: Scheduling (recurring)                             │
│   ⏰ Every 15 minutes:                                       │
│      → Run: python runtime/ingest.py                        │
│      → Which calls CoinGecko API                            │
│      → Which loads to BigQuery                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## What FLUID Does vs What You Do

| Task | Who Does It | How |
|------|-------------|-----|
| **Create dataset** | FLUID | Reads contract YAML → Calls BigQuery API |
| **Create table** | FLUID | Reads schema from contract → Calls BigQuery API |
| **Define schema** | YOU | Write in `contract.yaml` under `schema:` |
| **Fetch Bitcoin data** | YOU | Write in `runtime/ingest.py` |
| **Transform data** | YOU | Write in `runtime/ingest.py` |
| **Load to BigQuery** | YOU | Write in `runtime/ingest.py` |
| **Schedule execution** | FLUID | Generates Composer DAG from contract |
| **Monitor execution** | FLUID + GCP | Composer UI + Cloud Monitoring |

---

## Complete Example: Bitcoin Contract

### 1. Contract (Declarative Infrastructure)

```yaml
# contract.fluid.yaml
fluidVersion: "0.5.7"

# WHERE to store data (FLUID creates this)
exposes:
  - exposeId: bitcoin_prices_table
    binding:
      platform: gcp
      format: bigquery_table
      location:
        project: <<YOUR_PROJECT_HERE>>
        dataset: crypto_data
        table: bitcoin_prices
    
    # WHAT schema to use (FLUID enforces this)
    contract:
      schema:
        - name: price_timestamp
          type: TIMESTAMP
          required: true
        - name: price_usd
          type: FLOAT64
          required: true

# HOW to get data (YOU write the script)
builds:
  - id: bitcoin_price_ingestion
    pattern: python-script
    properties:
      script: runtime/ingest.py  # ← Your imperative code!
      schedule:
        cron: "*/15 * * * *"      # ← FLUID schedules this
```

### 2. Runtime (Imperative Data Logic)

```python
# runtime/ingest.py
import requests
from google.cloud import bigquery
from datetime import datetime

def fetch_bitcoin_price():
    """YOU write this - FLUID doesn't"""
    
    # ACTUAL API CALL to CoinGecko
    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={'ids': 'bitcoin', 'vs_currencies': 'usd'}
    )
    
    data = response.json()
    
    return {
        'price_timestamp': datetime.utcnow().isoformat(),
        'price_usd': data['bitcoin']['usd']
    }

def load_to_bigquery(record):
    """YOU write this - FLUID doesn't"""
    
    client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
    
    # ACTUAL API CALL to BigQuery
    client.insert_rows_json(
        "<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices",
        [record]
    )

def main():
    """Entry point - called by Composer DAG"""
    record = fetch_bitcoin_price()
    load_to_bigquery(record)

if __name__ == "__main__":
    main()
```

### 3. Deployment

```bash
# Step 1: FLUID creates infrastructure
$ fluid apply contract.fluid.yaml --provider gcp

# FLUID does:
# ✅ Creates dataset crypto_data
# ✅ Creates table bitcoin_prices with your schema
# ✅ Deploys Composer DAG that runs runtime/ingest.py every 15 min

# Step 2: Runtime executes (automatically every 15 min)
# Composer calls: python runtime/ingest.py
# Your code does:
# ✅ Calls CoinGecko API (fetch_bitcoin_price)
# ✅ Transforms data
# ✅ Loads to BigQuery (load_to_bigquery)
```

---

## Why This Design?

**Separation of Concerns**:

1. **Infrastructure** (declarative) - Slow-changing, reviewed carefully
   - Dataset/table definitions
   - Schema contracts
   - Access controls
   - Scheduling parameters

2. **Data Logic** (imperative) - Fast-changing, needs flexibility
   - API calls
   - Data transformations
   - Error handling
   - Business rules

**Benefits**:
- ✅ Infrastructure changes go through contract validation
- ✅ Data logic can iterate quickly without redeploying infrastructure
- ✅ Schema is single source of truth (contract enforces it)
- ✅ Different people can own different concerns (platform team vs data team)

---

## Common Confusion Points

### ❓ "I thought declarative means no code?"

**Answer**: Declarative means no code **for infrastructure**. You still need code for data transformation!

Think of it like Terraform:
- Terraform HCL = Declarative infrastructure (VMs, networks)
- Application code = Imperative logic (API calls, business rules)

FLUID contracts = Declarative infrastructure (tables, schemas)
Runtime scripts = Imperative logic (API calls, transformations)

---

### ❓ "So where's the actual API call in the YAML?"

**Answer**: It's not in the YAML! The YAML just **references** the script:

```yaml
builds:
  - script: runtime/ingest.py  # ← Points to Python file
```

The Python file contains the ACTUAL API calls:

```python
# runtime/ingest.py
response = requests.get("https://api.coingecko.com/...")  # ← HERE!
```

---

### ❓ "What if I want everything in YAML?"

**Answer**: Not possible (and not desirable)! Data transformation is complex and needs a full programming language.

Some tools try to do "SQL-only" pipelines, but you hit limitations fast:
- ❌ Can't call REST APIs from SQL
- ❌ Can't handle complex error handling
- ❌ Can't do custom transformations
- ❌ Can't integrate with external systems

FLUID's approach:
- ✅ YAML for structure (infrastructure, schema)
- ✅ Python for logic (transformations, API calls)

---

### ❓ "Why not just use Airflow directly?"

**Answer**: You could! But then you'd manually:
- ❌ Write BigQuery table creation code
- ❌ Manage schema in multiple places
- ❌ Handle IAM permissions manually
- ❌ Write boilerplate for error handling
- ❌ Deploy DAG manually to Composer

FLUID handles all that plumbing:
- ✅ Generate BigQuery infrastructure from contract
- ✅ Single source of truth for schema
- ✅ Auto-generated Composer DAG
- ✅ Built-in retry/error handling
- ✅ One-command deployment

---

## Complete Workflow

```bash
# 1. Write declarative contract
$ cat contract.fluid.yaml
exposes:
  - binding: {platform: gcp, format: bigquery_table}
    contract: {schema: [...]}
builds:
  - script: runtime/ingest.py

# 2. Write imperative runtime
$ cat runtime/ingest.py
def main():
    data = requests.get(API_URL)     # ← ACTUAL API CALL
    load_to_bigquery(data)           # ← ACTUAL LOAD

# 3. Deploy everything
$ fluid apply contract.fluid.yaml --provider gcp

# Creates:
# ✅ BigQuery table (from contract)
# ✅ Composer DAG (from builds section)
# ✅ Schedule (from cron expression)

# 4. Data flows automatically
# Every 15 minutes:
#   Composer → python runtime/ingest.py → CoinGecko API → BigQuery
```

---

## GCP Free Tier Limitations (Part A)

⚠️ **Important**: The free tier has restrictions on data loading methods:

### What Works ✅
- Creating datasets and tables
- Running the Python script locally
- Fetching data from CoinGecko API
- Batch loading via `bq load` command
- Loading from CSV/JSON files

### What Doesn't Work ❌
- **Streaming inserts** (`insert_rows_json`) - Blocked by free tier
- **Cloud Composer** - Requires billing account (Part B)
- **Scheduled execution** - Requires billing account (Part B)

### Part A Workaround
```python
# Instead of streaming insert (blocked):
# client.insert_rows_json(...)  ❌

# Use batch load from file (allowed):
# 1. Save data to local file
import json
with open('bitcoin_data.json', 'w') as f:
    json.dump([record], f)

# 2. Load via bq CLI
os.system('bq load --source_format=NEWLINE_DELIMITED_JSON '
          'crypto_data.bitcoin_prices bitcoin_data.json')  ✅
```

### Part B (Coming Soon)
With a paid account, you'll get:
- Streaming inserts for real-time ingestion
- Cloud Composer for scheduled execution
- Dataflow for large-scale transformations
- Advanced monitoring and alerting

---

## Summary

**Declarative contracts answer**:
- WHAT infrastructure to create (dataset, table)
- WHAT schema to enforce (field names, types)
- WHEN to run (schedule)
- WHERE to deploy (GCP, AWS, Snowflake)

**Imperative runtime answers**:
- HOW to fetch data (API calls)
- HOW to transform data (business logic)
- HOW to handle errors (retries, alerts)
- HOW to load data (insertion logic)

**Both are needed. FLUID orchestrates both.**

---

## See It In Action (Part A)

```bash
# 1. Deploy infrastructure (creates table with schema)
cd examples/bitcoin-price-api-declarative-part-a
fluid apply contract.fluid.yaml

# Output:
# ✅ Data product deployed successfully
# ✓ Applied 1 action(s) successfully

# 2. Test API connection (no data loading due to free tier)
python runtime/ingest.py

# Output:
# 📡 Calling CoinGecko API...
# ✅ API call successful
#    Current BTC price: $91,890.00
# ⚠️  Streaming insert blocked (free tier limit)

# 3. Verify table was created
python -c "from google.cloud import bigquery; \
  client = bigquery.Client(project='<<YOUR_PROJECT_HERE>>'); \
  table = client.get_table('crypto_data.bitcoin_prices'); \
  print(f'✅ Table exists with {len(table.schema)} fields')"
```

**Now you see both the declarative infrastructure AND the imperative data flow in action!**
