# Deploy Bitcoin Price Ingestion to GCP Cloud Functions
# 
# Prerequisites:
# - gcloud CLI installed and authenticated
# - GCP project created with billing enabled
# - BigQuery API enabled
# - Cloud Functions API enabled
# - Cloud Scheduler API enabled

$ErrorActionPreference = "Stop"

# Configuration
$PROJECT_ID = if ($env:GCP_PROJECT_ID) { $env:GCP_PROJECT_ID } else { "your-project-id" }
$REGION = if ($env:GCP_REGION) { $env:GCP_REGION } else { "us-central1" }
$FUNCTION_NAME = "bitcoin-price-ingestion"
$DATASET_ID = "crypto_data"
$TABLE_ID = "bitcoin_prices"
$SCHEDULE = "*/5 * * * *"  # Every 5 minutes
$SERVICE_ACCOUNT = "${FUNCTION_NAME}-sa@${PROJECT_ID}.iam.gserviceaccount.com"

Write-Host "🚀 Deploying Bitcoin Price Ingestion to GCP" -ForegroundColor Green
Write-Host "============================================"
Write-Host "Project: ${PROJECT_ID}"
Write-Host "Region: ${REGION}"
Write-Host "Function: ${FUNCTION_NAME}"
Write-Host "Schedule: Every 5 minutes"
Write-Host ""

# Step 1: Enable required APIs
Write-Host "📦 Enabling required APIs..." -ForegroundColor Yellow
gcloud services enable `
    cloudfunctions.googleapis.com `
    cloudscheduler.googleapis.com `
    bigquery.googleapis.com `
    cloudresourcemanager.googleapis.com `
    --project="$PROJECT_ID"

# Step 2: Create BigQuery dataset
Write-Host "📊 Creating BigQuery dataset..." -ForegroundColor Yellow
try {
    bq --project_id="$PROJECT_ID" mk -d `
        --location=US `
        --description="Cryptocurrency price data" `
        "$DATASET_ID"
} catch {
    Write-Host "Dataset already exists" -ForegroundColor Gray
}

# Step 3: Create service account
Write-Host "👤 Creating service account..." -ForegroundColor Yellow
try {
    gcloud iam service-accounts create "${FUNCTION_NAME}-sa" `
        --display-name="Bitcoin Price Ingestion Service Account" `
        --project="$PROJECT_ID"
} catch {
    Write-Host "Service account already exists" -ForegroundColor Gray
}

# Step 4: Grant permissions
Write-Host "🔐 Granting permissions..." -ForegroundColor Yellow
gcloud projects add-iam-policy-binding "$PROJECT_ID" `
    --member="serviceAccount:$SERVICE_ACCOUNT" `
    --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" `
    --member="serviceAccount:$SERVICE_ACCOUNT" `
    --role="roles/bigquery.jobUser"

# Step 5: Deploy Cloud Function
Write-Host "☁️  Deploying Cloud Function..." -ForegroundColor Yellow
gcloud functions deploy "$FUNCTION_NAME" `
    --gen2 `
    --runtime=python311 `
    --region="$REGION" `
    --source=. `
    --entry-point=bitcoin_price_ingestion `
    --trigger-http `
    --no-allow-unauthenticated `
    --service-account="$SERVICE_ACCOUNT" `
    --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,DATASET_ID=$DATASET_ID,TABLE_ID=$TABLE_ID" `
    --memory=256MB `
    --timeout=60s `
    --max-instances=1 `
    --project="$PROJECT_ID"

# Step 6: Get function URL
$FUNCTION_URL = gcloud functions describe "$FUNCTION_NAME" `
    --region="$REGION" `
    --project="$PROJECT_ID" `
    --format='value(serviceConfig.uri)'

Write-Host "✅ Function deployed: ${FUNCTION_URL}" -ForegroundColor Green

# Step 7: Create Cloud Scheduler job
Write-Host "⏰ Creating Cloud Scheduler job..." -ForegroundColor Yellow
try {
    gcloud scheduler jobs create http "${FUNCTION_NAME}-trigger" `
        --location="$REGION" `
        --schedule="$SCHEDULE" `
        --uri="$FUNCTION_URL" `
        --http-method=POST `
        --oidc-service-account-email="$SERVICE_ACCOUNT" `
        --project="$PROJECT_ID"
} catch {
    Write-Host "Job exists, updating..." -ForegroundColor Gray
    gcloud scheduler jobs update http "${FUNCTION_NAME}-trigger" `
        --location="$REGION" `
        --schedule="$SCHEDULE" `
        --uri="$FUNCTION_URL" `
        --http-method=POST `
        --oidc-service-account-email="$SERVICE_ACCOUNT" `
        --project="$PROJECT_ID"
}

Write-Host ""
Write-Host "✅ Deployment Complete!" -ForegroundColor Green
Write-Host "======================="
Write-Host ""
Write-Host "📊 BigQuery Table: ${PROJECT_ID}.${DATASET_ID}.${TABLE_ID}"
Write-Host "☁️  Cloud Function: ${FUNCTION_NAME}"
Write-Host "⏰ Schedule: Every 5 minutes"
Write-Host ""
Write-Host "🔍 Monitor your function:" -ForegroundColor Cyan
Write-Host "   gcloud functions logs read $FUNCTION_NAME --region=$REGION --project=$PROJECT_ID"
Write-Host ""
Write-Host "🧪 Test your function:" -ForegroundColor Cyan
Write-Host "   gcloud functions call $FUNCTION_NAME --region=$REGION --project=$PROJECT_ID"
Write-Host ""
Write-Host "📈 Query your data:" -ForegroundColor Cyan
Write-Host "   bq query --use_legacy_sql=false 'SELECT * FROM ``${PROJECT_ID}.${DATASET_ID}.${TABLE_ID}`` ORDER BY price_timestamp DESC LIMIT 10'"
Write-Host ""
