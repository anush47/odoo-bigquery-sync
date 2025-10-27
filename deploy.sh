#!/bin/bash

# Odoo to BigQuery Cloud Run Setup Script
set -e

echo "🚀 Setting up Cloud Run prerequisites"

# Configuration
PROJECT_ID="arvautomation"
REGION="australia-southeast1"
SERVICE_NAME="odoo-bq-sync"
GCS_BUCKET="arv_checkpoint"

echo "📋 Configuration:"
echo "  Project: $PROJECT_ID"
echo "  Region: $REGION"
echo "  Service: $SERVICE_NAME"
echo ""

# Step 1: Enable APIs
echo "🔧 Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  --project=$PROJECT_ID

# Step 2: Get or create service account
echo "👤 Setting up service account..."
SERVICE_ACCOUNT_NAME="odoo-bq-sync"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create $SERVICE_ACCOUNT_NAME \
  --project=$PROJECT_ID \
  --display-name="Odoo BigQuery Sync" 2>/dev/null || echo "Service account already exists"

# Step 3: Grant permissions
echo "🔑 Granting BigQuery permissions..."
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/bigquery.dataEditor" \
  --condition=None

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/bigquery.jobUser" \
  --condition=None

# Step 4: Create GCS bucket (if not exists)
echo "🗄️ Setting up GCS bucket..."
gsutil mb -p $PROJECT_ID -l $REGION gs://$GCS_BUCKET 2>/dev/null || echo "Bucket already exists"

# Grant storage permissions
echo "🔑 Granting GCS permissions..."
gsutil iam ch serviceAccount:$SERVICE_ACCOUNT:objectAdmin gs://$GCS_BUCKET

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Deploy: gcloud builds submit --config cloudbuild.yaml"
echo "  2. Run once manually (see below)"
echo ""
