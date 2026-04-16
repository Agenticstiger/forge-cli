# Bitcoin Price Ingestion - GCP Deployment Guide

Deploy this Bitcoin price ingestion pipeline to Google Cloud Platform with Cloud Functions, BigQuery, and Cloud Scheduler.

## 🏗️ Architecture

```
Cloud Scheduler (every 5 min)
    ↓
Cloud Function (Python 3.11)
    ↓
CoinGecko API
    ↓
BigQuery Table (partitioned by day)
```

## 📋 Prerequisites

1. **GCP Project**
   ```bash
   gcloud projects create bitcoin-price-prod --name="Bitcoin Price Pipeline"
   gcloud config set project bitcoin-price-prod
   ```

2. **Enable Billing**
   - Visit: https://console.cloud.google.com/billing
   - Link billing account to project

3. **Install gcloud CLI**
   - Download: https://cloud.google.com/sdk/docs/install
   - Authenticate:
     ```bash
     gcloud auth login
     gcloud auth application-default login
     ```

4. **Set Environment Variables**
   ```bash
   export GCP_PROJECT_ID="bitcoin-price-prod"
   export GCP_REGION="us-central1"
   ```

   PowerShell:
   ```powershell
   $env:GCP_PROJECT_ID="bitcoin-price-prod"
   $env:GCP_REGION="us-central1"
   ```

## 🚀 Quick Deployment

### Option 1: Automated Script (Recommended)

**Linux/Mac:**
```bash
chmod +x deploy.sh
./deploy.sh
```

**Windows PowerShell:**
```powershell
.\deploy.ps1
```

### Option 2: Manual Deployment

#### Step 1: Enable APIs
```bash
gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    bigquery.googleapis.com \
    --project="${GCP_PROJECT_ID}"
```

#### Step 2: Create BigQuery Dataset
```bash
bq mk -d \
    --location=US \
    --description="Cryptocurrency price data" \
    crypto_data
```

#### Step 3: Create Service Account
```bash
gcloud iam service-accounts create bitcoin-price-ingestion-sa \
    --display-name="Bitcoin Price Ingestion"

# Grant BigQuery permissions
gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
    --member="serviceAccount:bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
    --member="serviceAccount:bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/bigquery.jobUser"
```

#### Step 4: Deploy Cloud Function
```bash
gcloud functions deploy bitcoin-price-ingestion \
    --gen2 \
    --runtime=python311 \
    --region=us-central1 \
    --source=. \
    --entry-point=bitcoin_price_ingestion \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account=bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com \
    --set-env-vars="GCP_PROJECT_ID=${GCP_PROJECT_ID},DATASET_ID=crypto_data,TABLE_ID=bitcoin_prices" \
    --memory=256MB \
    --timeout=60s \
    --max-instances=1
```

#### Step 5: Create Cloud Scheduler Job
```bash
# Get function URL
FUNCTION_URL=$(gcloud functions describe bitcoin-price-ingestion \
    --region=us-central1 \
    --format='value(serviceConfig.uri)')

# Create scheduler job (every 5 minutes)
gcloud scheduler jobs create http bitcoin-price-ingestion-trigger \
    --location=us-central1 \
    --schedule="*/5 * * * *" \
    --uri="${FUNCTION_URL}" \
    --http-method=POST \
    --oidc-service-account-email=bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com
```

## 🧪 Testing

### Test Cloud Function Manually
```bash
gcloud functions call bitcoin-price-ingestion \
    --region=us-central1 \
    --project=${GCP_PROJECT_ID}
```

Expected output:
```json
{
  "success": true,
  "rows_loaded": 1,
  "timestamp": "2025-12-05T20:00:00"
}
```

### Test Scheduler Job
```bash
gcloud scheduler jobs run bitcoin-price-ingestion-trigger \
    --location=us-central1
```

### View Logs
```bash
# Function logs
gcloud functions logs read bitcoin-price-ingestion \
    --region=us-central1 \
    --limit=50

# Scheduler logs
gcloud scheduler jobs logs bitcoin-price-ingestion-trigger \
    --location=us-central1 \
    --limit=20
```

## 📊 Query BigQuery

### Recent Prices
```sql
SELECT 
    price_timestamp,
    price_usd,
    price_change_24h_percent,
    market_cap_usd,
    ingestion_timestamp
FROM `${GCP_PROJECT_ID}.crypto_data.bitcoin_prices`
ORDER BY price_timestamp DESC
LIMIT 10;
```

### Price Changes Over Time
```sql
SELECT 
    DATE(price_timestamp) as date,
    MIN(price_usd) as min_price,
    MAX(price_usd) as max_price,
    AVG(price_usd) as avg_price,
    STDDEV(price_usd) as price_volatility
FROM `${GCP_PROJECT_ID}.crypto_data.bitcoin_prices`
GROUP BY date
ORDER BY date DESC
LIMIT 30;
```

### Hourly Statistics
```sql
SELECT 
    TIMESTAMP_TRUNC(price_timestamp, HOUR) as hour,
    COUNT(*) as records,
    AVG(price_usd) as avg_price,
    AVG(volume_24h_usd) as avg_volume
FROM `${GCP_PROJECT_ID}.crypto_data.bitcoin_prices`
WHERE price_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY hour
ORDER BY hour DESC;
```

## 📈 Monitoring

### View in Console
- **Cloud Functions**: https://console.cloud.google.com/functions
- **Cloud Scheduler**: https://console.cloud.google.com/cloudscheduler
- **BigQuery**: https://console.cloud.google.com/bigquery

### Set Up Alerts

Create alert for function failures:
```bash
gcloud alpha monitoring policies create \
    --notification-channels=CHANNEL_ID \
    --display-name="Bitcoin Ingestion Failures" \
    --condition-display-name="Error rate > 0" \
    --condition-threshold-value=0 \
    --condition-threshold-duration=60s
```

### Cost Monitoring
```bash
# View current month costs
gcloud billing accounts list
gcloud billing projects describe ${GCP_PROJECT_ID}
```

**Expected Costs** (approximate):
- Cloud Functions: ~$0.40/month (288 invocations/day × 30 days)
- BigQuery Storage: ~$0.02/month (first 10GB free)
- Cloud Scheduler: ~$0.10/month (first 3 jobs free)
- **Total**: ~$0.50/month

## 🔧 Configuration

### Change Update Frequency

**Every minute:**
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="* * * * *" \
    --location=us-central1
```

**Every hour:**
```bash
gcloud scheduler jobs update http bitcoin-price-ingestion-trigger \
    --schedule="0 * * * *" \
    --location=us-central1
```

**Custom cron:** Use [crontab.guru](https://crontab.guru/) to build expressions

### Update Environment Variables
```bash
gcloud functions deploy bitcoin-price-ingestion \
    --update-env-vars="NEW_VAR=value" \
    --region=us-central1
```

## 🔒 Security

### Least Privilege Service Account
The deployment uses a dedicated service account with only required permissions:
- `roles/bigquery.dataEditor` - Write to BigQuery tables
- `roles/bigquery.jobUser` - Execute BigQuery jobs

### Network Security
- Cloud Function has no public endpoint (requires authentication)
- Only Cloud Scheduler (via OIDC) can invoke the function

### Secrets Management
For API keys (if CoinGecko Pro):
```bash
# Store API key in Secret Manager
gcloud secrets create coingecko-api-key --data-file=- <<< "your-api-key"

# Grant access to service account
gcloud secrets add-iam-policy-binding coingecko-api-key \
    --member="serviceAccount:bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# Update function to use secret
gcloud functions deploy bitcoin-price-ingestion \
    --set-secrets="COINGECKO_API_KEY=coingecko-api-key:latest"
```

## 🧹 Cleanup

### Delete All Resources
```bash
# Delete scheduler job
gcloud scheduler jobs delete bitcoin-price-ingestion-trigger \
    --location=us-central1 \
    --quiet

# Delete function
gcloud functions delete bitcoin-price-ingestion \
    --region=us-central1 \
    --quiet

# Delete BigQuery dataset (and all tables)
bq rm -r -f -d crypto_data

# Delete service account
gcloud iam service-accounts delete \
    bitcoin-price-ingestion-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com \
    --quiet
```

## 🐛 Troubleshooting

### Function Returns 500 Error
```bash
# Check logs
gcloud functions logs read bitcoin-price-ingestion --limit=100

# Common issues:
# 1. BigQuery permissions - verify service account roles
# 2. API timeout - increase --timeout value
# 3. Network issues - check VPC settings
```

### Scheduler Job Not Running
```bash
# Verify job exists
gcloud scheduler jobs describe bitcoin-price-ingestion-trigger \
    --location=us-central1

# Check job logs
gcloud scheduler jobs logs bitcoin-price-ingestion-trigger \
    --location=us-central1

# Manually trigger
gcloud scheduler jobs run bitcoin-price-ingestion-trigger \
    --location=us-central1
```

### No Data in BigQuery
```bash
# Verify table exists
bq show crypto_data.bitcoin_prices

# Check recent function invocations
gcloud functions logs read bitcoin-price-ingestion \
    --limit=10 \
    | grep -i "success"

# Test function manually
gcloud functions call bitcoin-price-ingestion --region=us-central1
```

## 📚 Next Steps

1. **Add More Cryptocurrencies**: Modify script to fetch ETH, BNB, etc.
2. **Build Dashboards**: Use Looker Studio or Tableau
3. **Set Up Alerts**: Price threshold notifications via Cloud Monitoring
4. **Add ML Models**: Price prediction using Vertex AI
5. **Create APIs**: Expose data via Cloud Run + FastAPI

## 🔗 Resources

- [Cloud Functions Documentation](https://cloud.google.com/functions/docs)
- [Cloud Scheduler Documentation](https://cloud.google.com/scheduler/docs)
- [BigQuery Documentation](https://cloud.google.com/bigquery/docs)
- [CoinGecko API Docs](https://www.coingecko.com/en/api/documentation)
- [FLUID Framework](https://github.com/yourorg/fluid-framework)
