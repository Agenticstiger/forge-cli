# Bitcoin Price Ingestion - GCP Deployment Summary

## ✅ What Was Created

### 📁 Production-Ready Files

1. **`ingest_bitcoin_prices_bigquery.py`** (220 lines)
   - BigQuery-optimized ingestion script
   - Automatic table creation with partitioning
   - Error handling and retry logic
   - Cloud Function compatible
   - Environment variable configuration

2. **`main.py`** (20 lines)
   - Cloud Function entry point
   - Exports `bitcoin_price_ingestion()` function
   - Flask request handling

3. **`requirements-gcp.txt`**
   - google-cloud-bigquery
   - google-cloud-storage
   - google-cloud-logging
   - requests, pandas
   - Optional: tenacity, pydantic

4. **`deploy.sh`** (Bash) and **`deploy.ps1`** (PowerShell)
   - Automated deployment scripts
   - Enable GCP APIs
   - Create BigQuery dataset
   - Set up IAM permissions
   - Deploy Cloud Function
   - Configure Cloud Scheduler
   - ~80 lines each with full error handling

5. **`.gcloudignore`**
   - Exclude local files from deployment
   - Reduces deployment size
   - Faster uploads

6. **`contract-bigquery.fluid.yaml`**
   - BigQuery-specific FLUID contract
   - Platform: `gcp`
   - Format: `bigquery_table`
   - 9 schema fields with descriptions
   - ✅ **Validated successfully** (FLUID 0.5.7)

7. **`README-GCP.md`** (500+ lines)
   - Complete GCP deployment guide
   - Prerequisites and setup
   - Manual and automated deployment
   - Testing and verification
   - BigQuery query examples
   - Monitoring and troubleshooting
   - Security best practices
   - Cost breakdown ($0.50/month)

8. **`README.md`** (400+ lines)
   - Project overview
   - Local vs GCP deployment options
   - Quick start guide
   - Data schema documentation
   - Example queries
   - Configuration options
   - Troubleshooting guide

---

## 🏗️ GCP Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CLOUD SCHEDULER                         │
│           Cron: */5 * * * * (Every 5 minutes)              │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP POST (OIDC Auth)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   CLOUD FUNCTION (Gen 2)                    │
│  • Runtime: Python 3.11                                     │
│  • Memory: 256MB                                            │
│  • Timeout: 60s                                             │
│  • Concurrency: 1 instance max                              │
│  • Auth: Service Account only                               │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS GET
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    COINGECKO API                            │
│  • Endpoint: api.coingecko.com/api/v3/simple/price         │
│  • Method: GET                                              │
│  • Auth: None (free tier)                                   │
│  • Rate Limit: 50 calls/minute                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ JSON Response
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  DATA TRANSFORMATION                        │
│  • Parse JSON                                               │
│  • Extract fields (price, market cap, volume)               │
│  • Add timestamps                                           │
│  • Convert to BigQuery schema                               │
└──────────────────────┬──────────────────────────────────────┘
                       │ Load Job
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              BIGQUERY TABLE                                 │
│  Project: ${GCP_PROJECT_ID}                                 │
│  Dataset: crypto_data                                       │
│  Table: bitcoin_prices                                      │
│                                                             │
│  Optimizations:                                             │
│  • Partitioned by: ingestion_timestamp (DAILY)              │
│  • Clustered by: price_timestamp                            │
│  • Write mode: APPEND                                       │
│  • Schema: 9 fields (TIMESTAMP, NUMERIC)                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 📊 Data Flow

1. **Cloud Scheduler** triggers every 5 minutes
2. **Cloud Function** receives HTTP POST request
3. **Function** calls CoinGecko API with parameters:
   - `ids=bitcoin`
   - `vs_currencies=usd,eur,gbp`
   - `include_market_cap=true`
   - `include_24hr_vol=true`
   - `include_24hr_change=true`
4. **API** returns JSON with current Bitcoin data
5. **Function** transforms JSON to structured record:
   ```json
   {
     "price_timestamp": "2025-12-05T20:00:00",
     "price_usd": 89561.0,
     "price_eur": 76925.0,
     "price_gbp": 67178.0,
     "market_cap_usd": 1786593014988.34,
     "volume_24h_usd": 49647819862.77,
     "price_change_24h_percent": -2.65,
     "last_updated": "2025-12-05T20:00:00",
     "ingestion_timestamp": "2025-12-05T20:00:05"
   }
   ```
6. **Function** loads record to BigQuery (WRITE_APPEND)
7. **BigQuery** stores in partitioned table
8. **Function** returns success response with row count

---

## 🔐 IAM & Security

### Service Account
- **Name**: `bitcoin-price-ingestion-sa`
- **Email**: `bitcoin-price-ingestion-sa@${PROJECT_ID}.iam.gserviceaccount.com`

### Permissions (Least Privilege)
- `roles/bigquery.dataEditor` - Write data to BigQuery tables
- `roles/bigquery.jobUser` - Execute BigQuery load jobs

### Network Security
- ✅ Cloud Function has **no public endpoint**
- ✅ Only Cloud Scheduler can invoke (OIDC authentication)
- ✅ No API keys stored in code (CoinGecko free tier)
- ✅ All communication over HTTPS

---

## 💰 Cost Breakdown (Monthly)

| Service | Usage | Free Tier | Billable | Cost |
|---------|-------|-----------|----------|------|
| **Cloud Functions** | 8,640 invocations/month<br>(5 min × 24 hr × 30 days) | First 2M free | 0 | $0.00 |
| **Cloud Functions Compute** | 8,640 × 1s × 256MB | First 400K GB-sec free | 8,640 GB-sec | $0.40 |
| **BigQuery Storage** | ~0.5 GB stored | First 10 GB free | 0 | $0.00 |
| **BigQuery Queries** | ~50 queries/month<br>(~1 MB scanned each) | First 1 TB free | 0 | $0.00 |
| **Cloud Scheduler** | 1 job | First 3 jobs free | 0 | $0.00 |
| **Network Egress** | Minimal (API responses) | First 1 GB free | 0 | $0.00 |
| **TOTAL** | | | | **~$0.40-0.50** |

**Notes**:
- Free tier covers most services
- Main cost is Cloud Functions compute time
- Increases if you add more frequent updates or cryptocurrencies
- Significantly cheaper than running a VM 24/7 (~$30/month)

---

## 🧪 Testing Checklist

### ✅ Local Testing
- [x] `python ingest_bitcoin_prices.py` works
- [x] CSV file created in `runtime/` directory
- [x] `python analyze_prices.py` shows statistics
- [x] Contract validates: `fluid validate contract.fluid.yaml`

### ✅ GCP Deployment
- [x] `deploy.sh` / `deploy.ps1` completes successfully
- [x] Cloud Function exists in console
- [x] Cloud Scheduler job created
- [x] BigQuery dataset and table created
- [x] Service account has correct permissions

### ✅ Function Testing
```bash
# Manual invocation
gcloud functions call bitcoin-price-ingestion --region=us-central1

# Check logs
gcloud functions logs read bitcoin-price-ingestion --limit=20

# Verify response
# Expected: {"success": true, "rows_loaded": 1, "timestamp": "..."}
```

### ✅ Data Validation
```bash
# Check table exists
bq show crypto_data.bitcoin_prices

# Query recent data
bq query --use_legacy_sql=false \
  'SELECT COUNT(*) as total FROM `${PROJECT}.crypto_data.bitcoin_prices`'

# View latest prices
bq query --use_legacy_sql=false \
  'SELECT * FROM `${PROJECT}.crypto_data.bitcoin_prices` 
   ORDER BY price_timestamp DESC LIMIT 5'
```

### ✅ Scheduler Testing
```bash
# Trigger manually
gcloud scheduler jobs run bitcoin-price-ingestion-trigger --location=us-central1

# Check logs
gcloud scheduler jobs logs bitcoin-price-ingestion-trigger --location=us-central1

# Wait 5 minutes and verify automatic trigger
```

---

## 📈 Example Queries

### Current Bitcoin Price
```sql
SELECT 
    price_timestamp,
    price_usd,
    price_eur,
    price_gbp,
    price_change_24h_percent,
    market_cap_usd / 1e9 as market_cap_billions,
    volume_24h_usd / 1e9 as volume_billions
FROM `${PROJECT}.crypto_data.bitcoin_prices`
ORDER BY price_timestamp DESC
LIMIT 1;
```

### Hourly Price Averages (Last 24 Hours)
```sql
SELECT 
    TIMESTAMP_TRUNC(price_timestamp, HOUR) as hour,
    COUNT(*) as data_points,
    AVG(price_usd) as avg_price,
    MIN(price_usd) as min_price,
    MAX(price_usd) as max_price,
    STDDEV(price_usd) as volatility
FROM `${PROJECT}.crypto_data.bitcoin_prices`
WHERE price_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY hour
ORDER BY hour DESC;
```

### Daily Statistics (Last 30 Days)
```sql
SELECT 
    DATE(price_timestamp) as date,
    MIN(price_usd) as daily_low,
    MAX(price_usd) as daily_high,
    AVG(price_usd) as daily_avg,
    (MAX(price_usd) - MIN(price_usd)) as daily_range,
    ((MAX(price_usd) - MIN(price_usd)) / AVG(price_usd) * 100) as volatility_percent
FROM `${PROJECT}.crypto_data.bitcoin_prices`
WHERE price_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY date
ORDER BY date DESC;
```

### Data Freshness Check
```sql
SELECT 
    MAX(ingestion_timestamp) as last_ingestion,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(ingestion_timestamp), MINUTE) as minutes_since_update,
    COUNT(*) as total_records,
    MIN(price_timestamp) as earliest_data,
    MAX(price_timestamp) as latest_data
FROM `${PROJECT}.crypto_data.bitcoin_prices`;
```

---

## 🔧 Configuration Changes

### Update Frequency

**Every minute** (high frequency - 43,200 invocations/month, ~$2/month):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="* * * * *" \
    --location=us-central1
```

**Every 15 minutes** (moderate - 2,880 invocations/month):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="*/15 * * * *" \
    --location=us-central1
```

**Every hour** (low frequency - 720 invocations/month):
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="0 * * * *" \
    --location=us-central1
```

### Add More Cryptocurrencies

Edit `ingest_bitcoin_prices_bigquery.py`:
```python
params = {
    'ids': 'bitcoin,ethereum,binancecoin,cardano',  # Add more
    'vs_currencies': 'usd,eur,gbp',
    # ... rest of params
}
```

Redeploy:
```bash
gcloud functions deploy bitcoin-price-ingestion --source=.
```

---

## 🐛 Common Issues & Solutions

### Issue: "Permission denied" error
**Solution**: Verify service account has correct roles:
```bash
gcloud projects get-iam-policy ${PROJECT_ID} \
    --flatten="bindings[].members" \
    --filter="bindings.members:bitcoin-price-ingestion-sa"
```

### Issue: Function times out
**Solution**: Increase timeout:
```bash
gcloud functions deploy bitcoin-price-ingestion --timeout=120s
```

### Issue: No data in BigQuery
**Solution**: Check function logs:
```bash
gcloud functions logs read bitcoin-price-ingestion --limit=50 | grep -i error
```

### Issue: Scheduler not triggering
**Solution**: Verify scheduler status:
```bash
gcloud scheduler jobs describe bitcoin-price-ingestion-trigger \
    --location=us-central1
```

If paused, resume:
```bash
gcloud scheduler jobs resume bitcoin-price-ingestion-trigger \
    --location=us-central1
```

---

## 🚀 Next Steps

1. **Create Dashboard**
   - Use Looker Studio (free)
   - Connect to BigQuery table
   - Build real-time price charts

2. **Add Alerts**
   - Set up Cloud Monitoring alerts
   - Trigger on price thresholds
   - Send notifications to Slack/Email

3. **Expand Cryptocurrencies**
   - Add ETH, BNB, ADA, SOL
   - Create separate tables or unified schema
   - Compare price correlations

4. **Build ML Models**
   - Use Vertex AI for price prediction
   - Train on historical data
   - Deploy predictions to BigQuery

5. **Create APIs**
   - Expose data via Cloud Run + FastAPI
   - Add authentication (API keys)
   - Rate limiting with Cloud Armor

6. **Optimize Costs**
   - Use BigQuery clustering for better query performance
   - Set up table expiration for old data
   - Monitor and adjust schedule based on usage

---

## 📚 Documentation Links

- **Example Code**: `examples/bitcoin-price-api/`
- **GCP Guide**: `examples/bitcoin-price-api/README-GCP.md`
- **Quickstart**: `docs/docs/quickstart/bitcoin-price-api.md`
- **CoinGecko API**: https://www.coingecko.com/en/api/documentation
- **Cloud Functions**: https://cloud.google.com/functions/docs
- **BigQuery**: https://cloud.google.com/bigquery/docs
- **Cloud Scheduler**: https://cloud.google.com/scheduler/docs

---

## ✅ Success Indicators

Your deployment is successful when:

- ✅ `deploy.sh` completes without errors
- ✅ Cloud Function responds to test calls
- ✅ BigQuery table has data
- ✅ Scheduler triggers every 5 minutes
- ✅ Logs show successful ingestions
- ✅ Queries return expected results
- ✅ Cost stays under $1/month

**🎉 Congratulations! You now have a production-ready Bitcoin price ingestion pipeline running on GCP!**
