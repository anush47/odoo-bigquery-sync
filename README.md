# Odoo to BigQuery Sync

Automatically sync Odoo records to BigQuery using Cloud Run Jobs.

## What is This?

This is a serverless ETL pipeline that extracts data from Odoo (an open-source ERP system) and loads it into Google BigQuery for analytics and reporting. It runs as a Cloud Run Job, making it cost-effective and scalable - you only pay when it's actively syncing data.

The system handles the complexities of Odoo's data model (arrays, objects, boolean quirks) and automatically converts records into BigQuery-compatible format. It supports both one-time historical syncs and ongoing incremental syncs.

### Common Use Cases

- **Business Intelligence**: Sync sales orders, customers, invoices to BigQuery for dashboarding in Looker/Data Studio
- **Data Warehousing**: Archive Odoo data for long-term analysis and compliance
- **Cross-System Integration**: Combine Odoo data with other sources (CRM, marketing tools) in BigQuery
- **Backup & Recovery**: Create read-only copies of critical Odoo data outside the operational system
- **Performance Optimization**: Move heavy analytical queries from Odoo to BigQuery

## Features

- **Flexible Sync Modes**: Full historical sync or incremental by days
- **Batch Processing**: Handles large datasets efficiently with pagination
- **Deduplication**: Prevents duplicate records using ID checking and insertId
- **Auto-Sanitization**: Converts Odoo arrays/objects to JSON strings for BigQuery
- **Optional Deletion**: Delete records from Odoo after successful sync
- **Cloud Native**: Runs as Cloud Run Job with service account authentication
- **Resumable**: Safety limits allow resuming large syncs across multiple runs
- **Ordered Sync**: Processes records from oldest to newest for consistency

## Configuration

### Environment Variables (.env)

```bash
# Odoo Configuration
ODOO_URL=https://your-odoo-instance.com
ODOO_DB=your-database
ODOO_USERNAME=admin
ODOO_PASSWORD=your-password
ODOO_MODEL=sale.order

# BigQuery Configuration
BQ_TABLE_ID=project_id.dataset.table_name

# Sync Settings
BATCH_LIMIT=1000              # Records per batch
BUFFER_MINUTES=2              # Time buffer for real-time sync
LOOKBACK_DAYS=-1              # -1 for all records, or number of days to look back
DELETE_SYNCED_RECORDS=false   # true to delete from Odoo after sync, false to keep

# Environment Mode
ENVIRONMENT=cloud  # or 'local' for development

# Cloud Storage (for checkpoint in cloud mode)
GCS_BUCKET=your-bucket-name
STATE_FILE=sync_state.json

# Local Development Only (comment out for cloud)
# GOOGLE_APPLICATION_CREDENTIALS=key.json
```

## Setup & Deployment

### 1. Prerequisites

Run the setup script to create service accounts and permissions:

```bash
bash deploy.sh
```

This creates:

- Service account: `odoo-bq-sync@arvautomation.iam.gserviceaccount.com`
- BigQuery permissions (dataEditor, jobUser)
- GCS bucket for state management

### 2. Deploy to Cloud Run

Build and deploy using Cloud Build:

```bash
gcloud builds submit --config cloudbuild.yaml
```

This will:

- Build Docker image in the cloud (no local Docker needed)
- Push to Container Registry (`gcr.io`)
- Deploy as Cloud Run Job in `australia-southeast1`

### 3. Set Environment Variables

**Important:** In Cloud Run, set environment variables directly in the job (not in .env):

```bash
gcloud run jobs update odoo-bq-sync \
  --region=australia-southeast1 \
  --set-env-vars="ENVIRONMENT=cloud,\
ODOO_URL=https://your-odoo-instance.com,\
ODOO_DB=your-database,\
ODOO_USERNAME=admin,\
ODOO_PASSWORD=your-password,\
ODOO_MODEL=sale.order,\
BQ_TABLE_ID=project.dataset.table,\
BATCH_LIMIT=1000,\
BUFFER_MINUTES=2,\
LOOKBACK_DAYS=-1,\
DELETE_SYNCED_RECORDS=false,\
GCS_BUCKET=your-bucket,\
STATE_FILE=sync_state.json" \
  --project=arvautomation
```

Or set them in Cloud Console:
1. Go to Cloud Run → Jobs → odoo-bq-sync
2. Click "Edit"
3. Add variables in "Variables & Secrets" tab

### 4. Create BigQuery Table

First run will detect missing table and print CREATE TABLE SQL:

```bash
gcloud run jobs execute odoo-bq-sync \
  --region=australia-southeast1 \
  --project=arvautomation
```

Check logs for the one-line SQL, copy it, and run in BigQuery console.

### 5. Run Sync

After table is created, run again to sync data:

```bash
gcloud run jobs execute odoo-bq-sync \
  --region=australia-southeast1 \
  --project=arvautomation
```

### 6. View Logs

```bash
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=odoo-bq-sync" \
  --limit=50 \
  --project=arvautomation \
  --format="table(timestamp, textPayload)"
```

Or view in console: https://console.cloud.google.com/run/jobs/details/australia-southeast1/odoo-bq-sync

## Local Development

For local testing:

1. Update `.env`:

   ```bash
   ENVIRONMENT=local
   GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account-key.json
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run:
   ```bash
   python main.py
   ```

## How It Works

1. **Checks** BigQuery table exists (if not, generates CREATE TABLE SQL and exits)
2. **Fetches existing IDs** from BigQuery to avoid duplicates
3. **Determines date range** based on `LOOKBACK_DAYS` (-1 for all records)
4. **Fetches records in batches** from Odoo (ordered by create_date ascending)
5. **Filters out duplicates** by comparing with existing IDs
6. **Sanitizes** data:
   - Arrays/objects → JSON strings
   - Booleans → strings ("true"/"false")
   - Empty strings → NULL
7. **Inserts** records to BigQuery with insertId for deduplication
8. **Handles errors gracefully**:
   - Failed records are logged with details
   - Successful records continue processing
   - Only successful records are deleted from Odoo (if enabled)
9. **Processes** up to 100 batches per run (100k records at default batch size)
10. **Prints summary** with totals: fetched, inserted, failed, skipped, deleted

### Lookback Period

- **`LOOKBACK_DAYS=-1`**: Syncs ALL records from Odoo (full historical sync)
- **`LOOKBACK_DAYS=7`**: Syncs only records created in the last 7 days
- **`LOOKBACK_DAYS=0`**: Syncs only today's records

### Deduplication

Records are deduplicated in two ways:

1. **Pre-insert check**: Queries existing IDs from BigQuery before sync
2. **InsertId**: Uses `{model}_{id}` as insertId to prevent BigQuery duplicates

### Batch Processing

- Processes records in batches (default: 1000 per batch)
- Safety limit: 100 batches per run (prevents timeout on very large syncs)
- Ordered by `create_date asc` to sync oldest records first
- For large syncs, run multiple times - it will continue from where it stopped

## Common Use Cases

### Initial Full Sync

```bash
LOOKBACK_DAYS=-1
DELETE_SYNCED_RECORDS=false
BATCH_LIMIT=1000
```

Syncs all historical records from Odoo, keeps them in Odoo.

### Incremental Daily Sync

```bash
LOOKBACK_DAYS=1
DELETE_SYNCED_RECORDS=false
BATCH_LIMIT=1000
```

Syncs only yesterday's and today's records, keeps them in Odoo.

### Archive and Delete

```bash
LOOKBACK_DAYS=30
DELETE_SYNCED_RECORDS=true
BATCH_LIMIT=500
```

Syncs last 30 days and deletes from Odoo after successful sync (archival mode).

### Real-time Sync (Scheduled)

```bash
LOOKBACK_DAYS=0
DELETE_SYNCED_RECORDS=false
BATCH_LIMIT=100
```

When scheduled every hour, syncs only today's records. Small batch for fast execution.

## Troubleshooting

### Build fails

Check Cloud Build logs:

```bash
gcloud builds list --limit=5 --project=arvautomation
```

### Job execution fails

Check job logs:

```bash
gcloud run jobs executions list \
  --job=odoo-bq-sync \
  --region=australia-southeast1 \
  --project=arvautomation
```

### Permission errors

Ensure service account has:

- `roles/bigquery.dataEditor`
- `roles/bigquery.jobUser`
- `objectAdmin` on GCS bucket

### Table doesn't exist

Run the generated CREATE TABLE SQL in BigQuery console first, or check the logs for the generated SQL statement.

## Architecture

**Cloud Mode:**

```
Cloud Scheduler (optional)
    ↓
Cloud Run Job
    ├─► Odoo (XML-RPC)
    ├─► BigQuery (insert)
    └─► GCS (checkpoint)
```

**Local Mode:**

```
main.py
    ├─► Odoo (XML-RPC)
    ├─► BigQuery (insert via service account key)
    └─► Local File (checkpoint)
```

## Project Structure

```
odoo_bq_sync/
├── main.py              # Main sync script
├── requirements.txt     # Python dependencies
├── Dockerfile          # Container definition
├── cloudbuild.yaml     # Cloud Build configuration
├── deploy.sh           # Setup script
├── .env               # Environment configuration
├── .env.example       # Environment template
└── .gcloudignore      # Files to exclude from build
```

## Re-deploy After Changes

Simply run Cloud Build again:

```bash
gcloud builds submit --config cloudbuild.yaml
```

## License

MIT
