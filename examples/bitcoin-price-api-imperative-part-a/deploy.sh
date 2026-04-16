#!/bin/bash
# Deploy Bitcoin Price Ingestion to GCP Cloud Functions
# 
# Prerequisites:
# - gcloud CLI installed and authenticated
# - GCP project created with billing enabled
# - BigQuery API enabled
# - Cloud Functions API enabled
# - Cloud Scheduler API enabled

set -e

# Configuration
PROJECT_ID="${GCP_PROJECT_ID:-your-project-id}"
REGION="${GCP_REGION:-us-central1}"
FUNCTION_NAME="bitcoin-price-ingestion"
DATASET_ID="crypto_data"
TABLE_ID="bitcoin_prices"
SCHEDULE="*/5 * * * *"  # Every 5 minutes
SERVICE_ACCOUNT="${FUNCTION_NAME}-sa@${PROJECT_ID}.iam.gserviceaccount.com"

echo "🚀 Deploying Bitcoin Price Ingestion to GCP"
echo "============================================"
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Function: ${FUNCTION_NAME}"
echo "Schedule: Every 5 minutes"
echo ""

# Step 1: Enable required APIs
echo "📦 Enabling required APIs..."
gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    bigquery.googleapis.com \
    cloudresourcemanager.googleapis.com \
    --project="${PROJECT_ID}"

# Step 2: Create BigQuery dataset
echo "📊 Creating BigQuery dataset..."
bq --project_id="${PROJECT_ID}" mk -d \
    --location=US \
    --description="Cryptocurrency price data" \
    "${DATASET_ID}" || echo "Dataset already exists"

# Step 3: Create service account
echo "👤 Creating service account..."
gcloud iam service-accounts create "${FUNCTION_NAME}-sa" \
    --display-name="Bitcoin Price Ingestion Service Account" \
    --project="${PROJECT_ID}" || echo "Service account already exists"

# Step 4: Grant permissions
echo "🔐 Granting permissions..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/bigquery.jobUser"

# Step 5: Deploy Cloud Function
echo "☁️  Deploying Cloud Function..."
gcloud functions deploy "${FUNCTION_NAME}" \
    --gen2 \
    --runtime=python311 \
    --region="${REGION}" \
    --source=. \
    --entry-point=bitcoin_price_ingestion \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${SERVICE_ACCOUNT}" \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},DATASET_ID=${DATASET_ID},TABLE_ID=${TABLE_ID}" \
    --memory=256MB \
    --timeout=60s \
    --max-instances=1 \
    --project="${PROJECT_ID}"

# Step 6: Get function URL
FUNCTION_URL=$(gcloud functions describe "${FUNCTION_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(serviceConfig.uri)')

echo "✅ Function deployed: ${FUNCTION_URL}"

# Step 7: Create Cloud Scheduler job
echo "⏰ Creating Cloud Scheduler job..."
gcloud scheduler jobs create http "${FUNCTION_NAME}-trigger" \
    --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --uri="${FUNCTION_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SERVICE_ACCOUNT}" \
    --project="${PROJECT_ID}" || \
gcloud scheduler jobs update http "${FUNCTION_NAME}-trigger" \
    --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --uri="${FUNCTION_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SERVICE_ACCOUNT}" \
    --project="${PROJECT_ID}"

echo ""
echo "✅ Deployment Complete!"
echo "======================="
echo ""
echo "📊 BigQuery Table: ${PROJECT_ID}.${DATASET_ID}.${TABLE_ID}"
echo "☁️  Cloud Function: ${FUNCTION_NAME}"
echo "⏰ Schedule: Every 5 minutes"
echo ""
echo "🔍 Monitor your function:"
echo "   gcloud functions logs read ${FUNCTION_NAME} --region=${REGION} --project=${PROJECT_ID}"
echo ""
echo "🧪 Test your function:"
echo "   gcloud functions call ${FUNCTION_NAME} --region=${REGION} --project=${PROJECT_ID}"
echo ""
echo "📈 Query your data:"
echo "   bq query --use_legacy_sql=false 'SELECT * FROM \`${PROJECT_ID}.${DATASET_ID}.${TABLE_ID}\` ORDER BY price_timestamp DESC LIMIT 10'"
echo ""
