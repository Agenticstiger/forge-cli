# Bitcoin Price Ingestion - Imperative Python (Part A)

**Part**: A - Free Tier GCP Setup  
**Approach**: Imperative Python SDK  
**Level**: Intermediate  
**Future**: Part B will demonstrate advanced features with paid accounts

---

## 📁 Project Structure

```
bitcoin-price-api-imperative-part-a/
├── contract.fluid.yaml              # Local CSV contract
├── contract-bigquery.fluid.yaml     # BigQuery contract
├── ingest_bitcoin_prices.py         # Local CSV ingestion
├── ingest_bitcoin_prices_bigquery.py # BigQuery ingestion
├── analyze_prices.py                # Analytics script
├── main.py                          # Cloud Function entry point
├── requirements.txt                 # Local dependencies
├── requirements-gcp.txt             # GCP dependencies
├── deploy.sh                        # Linux/Mac deployment
├── deploy.ps1                       # Windows deployment
├── .gcloudignore                    # GCP deployment exclusions
└── README-GCP.md                    # Detailed GCP guide
```

## 🎯 Two Deployment Options

### Option 1: Local Development (CSV)

**Use Case**: Testing, development, small-scale data collection

**Files**:
- `contract.fluid.yaml` - Local file binding
- `ingest_bitcoin_prices.py` - Saves to CSV
- `requirements.txt` - Minimal dependencies

**Run**:
```bash
pip install -r requirements.txt
python ingest_bitcoin_prices.py
python analyze_prices.py
```

**Output**: `runtime/bitcoin_prices.csv`

---

### Option 2: Production on GCP (BigQuery)

**Use Case**: Production, scheduled ingestion, analytics at scale

**Architecture**:
```
Cloud Scheduler → Cloud Function → CoinGecko API → BigQuery
     (5 min)       (Python 3.11)     (REST API)    (Partitioned)
```

**Files**:
- `contract-bigquery.fluid.yaml` - BigQuery binding with partitioning
- `ingest_bitcoin_prices_bigquery.py` - BigQuery loader
- `main.py` - Cloud Function wrapper
- `requirements-gcp.txt` - GCP libraries
- `deploy.sh` / `deploy.ps1` - Automated deployment

**Deploy**:
```bash
# Set your project
export GCP_PROJECT_ID="your-project-id"

# Deploy everything
./deploy.sh

# Or manually
gcloud functions deploy bitcoin-price-ingestion \
    --runtime=python311 \
    --trigger-http \
    --entry-point=bitcoin_price_ingestion
```

**Cost**: ~$0.50/month

---

## 🚀 Quick Start Guide

### Step 1: Clone and Test Locally

```bash
cd examples/bitcoin-price-api
pip install -r requirements.txt
python ingest_bitcoin_prices.py
```

Expected output:
```
INFO - 🚀 Starting Bitcoin price ingestion...
INFO - 📡 Fetching Bitcoin price from CoinGecko API...
INFO - ✅ Successfully fetched data
INFO - 💰 Price: $89,561.00 USD
INFO - ✅ Ingestion completed successfully!
```

### Step 2: Validate Contract

```bash
cd ../..
python -m fluid_build validate examples/bitcoin-price-api/contract.fluid.yaml
```

Expected:
```
✅ Valid FLUID contract (schema v0.5.7)
```

### Step 3: Deploy to GCP

```bash
cd examples/bitcoin-price-api

# Set environment variables
export GCP_PROJECT_ID="my-project"
export GCP_REGION="us-central1"

# Deploy
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. ✅ Enable required GCP APIs
2. ✅ Create BigQuery dataset and table
3. ✅ Set up service account with IAM
4. ✅ Deploy Cloud Function
5. ✅ Create Cloud Scheduler job (every 5 minutes)

### Step 4: Verify Deployment

```bash
# Test function
gcloud functions call bitcoin-price-ingestion --region=us-central1

# Check logs
gcloud functions logs read bitcoin-price-ingestion --limit=20

# Query data
bq query --use_legacy_sql=false \
  'SELECT * FROM `my-project.crypto_data.bitcoin_prices` 
   ORDER BY price_timestamp DESC LIMIT 10'
```

---

## 📊 Data Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `price_timestamp` | TIMESTAMP | Yes | When price was recorded |
| `price_usd` | NUMERIC | Yes | Bitcoin price in USD |
| `price_eur` | NUMERIC | Yes | Bitcoin price in EUR |
| `price_gbp` | NUMERIC | Yes | Bitcoin price in GBP |
| `market_cap_usd` | NUMERIC | No | Total market cap |
| `volume_24h_usd` | NUMERIC | No | 24h trading volume |
| `price_change_24h_percent` | NUMERIC | No | 24h price change % |
| `last_updated` | TIMESTAMP | Yes | API update time |
| `ingestion_timestamp` | TIMESTAMP | Yes | When loaded to BigQuery |

**BigQuery Optimizations**:
- ✅ Partitioned by `ingestion_timestamp` (daily)
- ✅ Clustered by `price_timestamp`
- ✅ Automatic table creation
- ✅ Schema validation

---

## 📈 Example Queries

### Current Price
```sql
SELECT 
    price_timestamp,
    price_usd,
    price_change_24h_percent,
    market_cap_usd / 1e9 as market_cap_billions
FROM `my-project.crypto_data.bitcoin_prices`
ORDER BY price_timestamp DESC
LIMIT 1;
```

### Price Changes Over 24 Hours
```sql
SELECT 
    TIMESTAMP_TRUNC(price_timestamp, HOUR) as hour,
    AVG(price_usd) as avg_price,
    MIN(price_usd) as min_price,
    MAX(price_usd) as max_price,
    STDDEV(price_usd) as volatility
FROM `my-project.crypto_data.bitcoin_prices`
WHERE price_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY hour
ORDER BY hour;
```

### Price Volatility Trend
```sql
WITH daily_stats AS (
  SELECT 
    DATE(price_timestamp) as date,
    MIN(price_usd) as min_price,
    MAX(price_usd) as max_price,
    AVG(price_usd) as avg_price
  FROM `my-project.crypto_data.bitcoin_prices`
  GROUP BY date
)
SELECT 
    date,
    avg_price,
    (max_price - min_price) as daily_range,
    ((max_price - min_price) / avg_price * 100) as volatility_percent
FROM daily_stats
ORDER BY date DESC
LIMIT 30;
```

---

## 🔧 Configuration

### Change Update Frequency

Edit the schedule in Cloud Scheduler:

**Every minute** (high frequency):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="* * * * *" \
    --location=us-central1
```

**Every 15 minutes** (moderate):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="*/15 * * * *" \
    --location=us-central1
```

**Every hour** (low frequency):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="0 * * * *" \
    --location=us-central1
```

### Add More Cryptocurrencies

Modify `ingest_bitcoin_prices_bigquery.py`:

```python
params = {
    'ids': 'bitcoin,ethereum,binancecoin',  # Add more
    'vs_currencies': 'usd,eur,gbp',
    ...
}
```

---

## 💰 Cost Breakdown

| Service | Usage | Cost/Month |
|---------|-------|------------|
| Cloud Functions | 8,640 invocations (5 min intervals) | $0.40 |
| BigQuery Storage | < 1 GB data | $0.02 |
| BigQuery Queries | ~100 queries | Free (1TB/month) |
| Cloud Scheduler | 1 job | $0.10 |
| Network Egress | Minimal | < $0.01 |
| **Total** | | **~$0.50** |

**Free Tier Benefits**:
- First 2 million Cloud Function invocations free
- First 10 GB BigQuery storage free
- First 1 TB BigQuery queries free
- First 3 Cloud Scheduler jobs free

---

## 🔒 Security Best Practices

### 1. Service Account Permissions

The deployment uses **least privilege**:
```bash
# Only these roles granted
roles/bigquery.dataEditor   # Write to tables
roles/bigquery.jobUser       # Execute queries
```

### 2. Network Security

- Cloud Function has **no public endpoint**
- Only Cloud Scheduler can invoke (via OIDC token)
- BigQuery access via service account only

### 3. API Key Management

For CoinGecko Pro (if needed):
```bash
# Store in Secret Manager
echo -n "your-api-key" | gcloud secrets create coingecko-api-key --data-file=-

# Grant access
gcloud secrets add-iam-policy-binding coingecko-api-key \
    --member="serviceAccount:bitcoin-price-ingestion-sa@PROJECT.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# Use in function
gcloud functions deploy bitcoin-price-ingestion \
    --set-secrets="COINGECKO_API_KEY=coingecko-api-key:latest"
```

---

## 🐛 Troubleshooting

### Function Returns 500 Error

**Check logs**:
```bash
gcloud functions logs read bitcoin-price-ingestion --limit=100
```

**Common causes**:
1. BigQuery permissions → Verify IAM roles
2. Network timeout → Increase `--timeout=120s`
3. API rate limit → Add retry logic with `tenacity`

### No Data in BigQuery

**Verify table exists**:
```bash
bq show crypto_data.bitcoin_prices
```

**Check recent invocations**:
```bash
gcloud scheduler jobs logs bitcoin-price-ingestion-trigger --limit=10
```

**Manually trigger**:
```bash
gcloud functions call bitcoin-price-ingestion --region=us-central1
```

### Scheduler Not Running

**Check status**:
```bash
gcloud scheduler jobs describe bitcoin-price-ingestion-trigger \
    --location=us-central1
```

**Resume if paused**:
```bash
gcloud scheduler jobs resume bitcoin-price-ingestion-trigger \
    --location=us-central1
```

---

## 📚 Related Documentation

- [Bitcoin Price API Quickstart](../../docs/docs/quickstart/bitcoin-price-api.md)
- [GCP Deployment Guide](README-GCP.md)
- [FLUID Contract Reference](../../docs/docs/reference/contract-spec.md)
- [CoinGecko API Docs](https://www.coingecko.com/en/api/documentation)

---

## 🎉 Success Checklist

- [ ] Local ingestion works (`python ingest_bitcoin_prices.py`)
- [ ] Contract validates (`fluid validate contract.fluid.yaml`)
- [ ] GCP project created and billed
- [ ] Deploy script completes successfully
- [ ] Cloud Function responds to test call
- [ ] BigQuery table contains data
- [ ] Cloud Scheduler triggers every 5 minutes
- [ ] Analytics queries return results

**Next**: Build dashboards, add ML models, set up alerts! 🚀
