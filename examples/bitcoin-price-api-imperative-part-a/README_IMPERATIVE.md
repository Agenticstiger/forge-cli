# Bitcoin Price API - Imperative Python Approach

**Approach**: Direct Python scripts with explicit BigQuery API calls  
**Philosophy**: "I want full control over every step"  
**Time to Complete**: 15 minutes  
**Level**: Intermediate

> **Compare**: See [Declarative FLUID Version](../bitcoin-price-api-fluid/) for contract-driven approach, or read the [Full Comparison Guide](../../docs/docs/quickstart/09-bitcoin-price-comparison.md)

---

## 🎯 About This Example

This is the **imperative implementation** of the Bitcoin Price API data product. It demonstrates:

✅ **Direct Control**: Explicit Python scripts for every operation  
✅ **Learning GCP**: Hands-on experience with BigQuery Python client  
✅ **No Framework**: Pure Python + GCP SDK (no FLUID CLI needed)  
✅ **Transparency**: See every API call and transformation  

**Tradeoffs**:
- ❌ More code to maintain (348 lines across 3 scripts)
- ❌ Schema defined in multiple places
- ❌ Manual infrastructure setup
- ❌ Provider-locked (GCP-specific)

---

## 📊 Imperative vs Declarative

| Aspect | **This (Imperative)** | [Declarative FLUID](../bitcoin-price-api-fluid/) |
|--------|---------------------|--------------------------------------------------|
| **Philosophy** | Script-driven | Contract-driven |
| **Setup** | 3 manual scripts | 1 `fluid apply` command |
| **Schema** | Defined in 3 places | Defined once in contract |
| **Portability** | GCP-locked | Swap providers easily |
| **Learning** | Python + GCP APIs | FLUID patterns |
| **Best For** | Prototypes, learning | Production, teams |

👉 **[See detailed comparison](../../docs/docs/quickstart/09-bitcoin-price-comparison.md)**

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- GCP account with BigQuery API enabled
- `gcloud` CLI authenticated
- Virtual environment recommended

### Part 1: Manual Ingestion (No Billing Required)

**Step 1: Install Dependencies**
```bash
cd examples/bitcoin-price-api
pip install google-cloud-bigquery requests db-dtypes
```

**Step 2: Create BigQuery Infrastructure**
```bash
python setup_bigquery.py
```

This creates:
- Dataset: `<<YOUR_PROJECT_HERE>>.crypto_data`
- Table: `bitcoin_prices` with 9 fields
- Daily partitioning on `price_timestamp`

**Step 3: Ingest Bitcoin Price Data**
```bash
python ingest_bitcoin_prices_bigquery.py
```

Fetches from CoinGecko API and loads to BigQuery.

**Step 4: Query Results**
```bash
python query_bigquery.py
```

Or use BigQuery console: [View Table](https://console.cloud.google.com/bigquery?project=<<YOUR_PROJECT_HERE>>)

**Step 5: Build Historical Data**
```bash
# Run multiple times to accumulate data
for i in {1..5}; do
  python ingest_bitcoin_prices_bigquery.py
  sleep 60
done
```

---

## 📂 File Structure

```
bitcoin-price-api/
├── setup_bigquery.py              # Creates dataset and table (82 lines)
├── ingest_bitcoin_prices_bigquery.py  # Fetches and loads data (223 lines)
├── query_bigquery.py              # Query utility (43 lines)
├── contract.fluid.yaml            # (Unused in imperative mode)
└── README.md                      # This file
```

**Total Python Code**: 348 lines

**Compare with**: [Declarative version](../bitcoin-price-api-fluid/) - 180 lines of YAML

---

## 🔍 Code Walkthrough

### setup_bigquery.py

**Purpose**: Explicitly create BigQuery infrastructure

```python
from google.cloud import bigquery

def setup_bigquery(project_id: str = "<<YOUR_PROJECT_HERE>>"):
    client = bigquery.Client(project=project_id)
    
    # Create dataset
    dataset_id = f"{project_id}.crypto_data"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    dataset = client.create_dataset(dataset, exists_ok=True)
    
    # Define schema (hardcoded here)
    schema = [
        bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
        # ... 7 more fields
    ]
    
    # Create table with partitioning
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="price_timestamp"
    )
    table = client.create_table(table)
```

**Key Points**:
- Direct BigQuery API calls
- Schema defined here (first of 3 places)
- Manual partitioning setup
- Idempotent with `exists_ok=True`

### ingest_bitcoin_prices_bigquery.py

**Purpose**: Fetch from CoinGecko API and load to BigQuery

**Three stages**:

1. **Fetch**:
```python
def fetch_bitcoin_price() -> Dict[str, Any]:
    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={'ids': 'bitcoin', ...}
    )
    return response.json()['bitcoin']
```

2. **Transform**:
```python
def transform_to_records(raw_data: Dict[str, Any]) -> List[Dict]:
    return [{
        'price_timestamp': datetime.now(),
        'price_usd': float(raw_data['usd']),
        'price_eur': float(raw_data['eur']),
        # ... transform all fields
    }]
```

3. **Load**:
```python
def load_to_bigquery(records: List[Dict]) -> int:
    client = bigquery.Client(project=PROJECT_ID)
    
    # Schema defined again (second place)
    job_config = bigquery.LoadJobConfig(
        schema=[...],  # Duplicate schema
        write_disposition="WRITE_APPEND"
    )
    
    job = client.load_table_from_json(
        records, table_ref, job_config=job_config
    )
    return len(records)
```

**Key Points**:
- Manual orchestration of fetch → transform → load
- Schema hardcoded again in `job_config`
- Explicit error handling
- Direct control over BigQuery load job

### query_bigquery.py

**Purpose**: Simple query utility

```python
def query_bitcoin_prices(project_id: str = "<<YOUR_PROJECT_HERE>>"):
    client = bigquery.Client(project=project_id)
    
    query = """
    SELECT * FROM crypto_data.bitcoin_prices
    ORDER BY price_timestamp DESC
    LIMIT 10
    """
    
    results = client.query(query).result()
    # Format and print results
```

---

## 🔧 Schema Management: The Pain Point

**Problem**: Schema defined in **3 separate places**

1. **`setup_bigquery.py` (line 31)**:
```python
schema = [
    bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
    # ...
]
```

2. **`ingest_bitcoin_prices_bigquery.py` table creation (line 97)**:
```python
schema = [
    bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
    # ... duplicate!
]
```

3. **`ingest_bitcoin_prices_bigquery.py` load job (line 148)**:
```python
job_config = bigquery.LoadJobConfig(
    schema=[
        bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
        # ... duplicate again!
    ]
)
```

**Adding a new field**? Update all 3 places! 😩

**Declarative solution**: Schema in contract once, FLUID uses it everywhere.

---

## 💰 Cost Analysis

### Part 1: Manual Ingestion
- **BigQuery Storage**: FREE (< 10 GB)
- **BigQuery Queries**: FREE (< 1 TB)
- **CoinGecko API**: FREE (free tier)
- **Total**: **$0.00/month**

### Part 2: Cloud Functions Automation (Requires Billing)
See [Part 2 Guide](./README-GCP.md) for Cloud Functions + Scheduler deployment.

**Estimated**: ~$0.30-$0.50/month

---

## ✅ Advantages of This Approach

### 1. **Learning Value**
- See every BigQuery API call
- Understand client library patterns
- Direct experience with GCP services

### 2. **Full Control**
- Customize every step
- Add custom retry logic
- Implement specific error handling
- No framework abstractions

### 3. **No Dependencies**
- Just Python + `google-cloud-bigquery`
- No FLUID CLI installation needed
- No learning curve for framework

### 4. **Debugging**
- Step through code in debugger
- Print statements everywhere
- Clear execution flow

---

## ❌ Disadvantages of This Approach

### 1. **Code Duplication**
- Schema defined 3 times
- Configuration scattered
- Error-prone maintenance

### 2. **Provider Lock-in**
- GCP BigQuery specific
- Migrating to Azure = rewrite everything
- Hard to support multiple clouds

### 3. **No Built-in Testing**
- Write your own quality tests
- Manual validation required
- No contract-based validation

### 4. **Manual Everything**
- Infrastructure setup is manual
- Deployment is manual
- Environment management is manual

### 5. **Scaling Challenges**
- Adding fields = update 3 places
- Adding environments = duplicate configs
- Team collaboration = merge conflicts

---

## 🔄 Evolution Path

### Phase 1: Start Here (Imperative)
- Learn the mechanics
- Understand BigQuery
- Build working prototype

### Phase 2: Extract Patterns
- Notice repeated code
- Schema duplication pain
- Manual steps frustration

### Phase 3: Move to Declarative
- Try [FLUID version](../bitcoin-price-api-fluid/)
- Compare effort
- Appreciate abstraction

### Phase 4: Hybrid Approach
- Use FLUID for standard patterns
- Keep imperative for edge cases
- Best of both worlds

---

## 🚀 Part 2: Cloud Functions Automation

To automate this pipeline with Cloud Functions + Scheduler:

See **[Part 2: GCP Deployment Guide](./README-GCP.md)**

**What you'll add**:
- `main.py` - Cloud Function entry point
- `deploy.sh` - Automated deployment script
- Cloud Scheduler job (5-minute intervals)
- IAM configuration

**Note**: Requires GCP billing enabled

---

## 🆚 When to Use This Approach

### ✅ Use Imperative When:
- **Learning**: First time with GCP/BigQuery
- **Prototyping**: Quick one-off experiments
- **Custom Logic**: Beyond standard patterns
- **Debugging**: Need to see every step
- **Team Preference**: Python-first culture
- **Simple Pipelines**: Single environment, no complexity

### ❌ Consider Declarative When:
- **Production**: Long-term maintained pipelines
- **Multi-Environment**: dev/staging/prod
- **Team Collaboration**: Contracts as docs
- **Provider Flexibility**: Might switch clouds
- **Quality First**: Built-in testing needed
- **Scale**: Many similar pipelines

---

## 📚 Related Examples

### Imperative Comparisons
- [Python BigQuery Tutorial](https://cloud.google.com/bigquery/docs/quickstarts/quickstart-client-libraries) - Official GCP docs
- [Pandas to BigQuery](https://cloud.google.com/bigquery/docs/samples/bigquery-load-table-dataframe) - Dataframe patterns

### Declarative FLUID Versions
- [Bitcoin Price API (FLUID)](../bitcoin-price-api-fluid/) - Same problem, declarative
- [High Value Churn](../local/high_value_churn/) - FLUID embedded-logic
- [Customer 360](../customer360/) - Advanced FLUID patterns

### Comparison Guides
- [Imperative vs Declarative Deep Dive](../../docs/docs/quickstart/09-bitcoin-price-comparison.md)
- [When to Use Each Approach](../../docs/docs/quickstart/09-bitcoin-price-comparison.md#-when-should-you-choose)

---

## 📖 Full Documentation

### Quick Start Guides
- **[Part 1: Manual BigQuery Ingestion](../../docs/docs/quickstart/09a-bitcoin-price-api-part1.md)** - This approach
- **[Part 2: Cloud Functions Automation](../../docs/docs/quickstart/09b-bitcoin-price-api-part2.md)** - Add scheduling
- **[Comparison Guide](../../docs/docs/quickstart/09-bitcoin-price-comparison.md)** - Imperative vs Declarative

### Technical Resources
- [CoinGecko API](https://www.coingecko.com/en/api/documentation)
- [BigQuery Python Client](https://cloud.google.com/python/docs/reference/bigquery/latest)
- [BigQuery Partitioning](https://cloud.google.com/bigquery/docs/partitioned-tables)

---

## 🎓 Learning Takeaways

After completing this example, you understand:

✅ **BigQuery Basics**
- Creating datasets and tables via API
- Schema definition and type mapping
- Partitioning and clustering strategies

✅ **Python Patterns**
- REST API consumption
- Data transformation pipelines
- BigQuery client library usage

✅ **Production Considerations**
- Error handling and logging
- Idempotent operations
- Schema evolution challenges

✅ **Tradeoffs**
- Control vs abstraction
- Flexibility vs maintainability
- Learning curve vs productivity

**Next**: Try the [declarative version](../bitcoin-price-api-fluid/) to compare approaches!

---

## 💡 Philosophy

> **"Imperative = I drive the car manually."**  
> **"Declarative = Autonomous driving to destination."**

Both are valid. This example teaches you to **drive manually** so you:
- Understand the mechanics
- Appreciate automation later
- Know when to take control

After mastering this, you'll better appreciate what frameworks like FLUID abstract away.

**Ready to compare?** → [Try the Declarative Version](../bitcoin-price-api-fluid/)
