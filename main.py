import os
import json
import xmlrpc.client
from datetime import datetime, timedelta, UTC
from google.cloud import bigquery, storage
from dotenv import load_dotenv

# Load .env only if ENVIRONMENT is not already set (local mode)
# In Cloud Run, environment variables are set directly, so .env is not needed
if "ENVIRONMENT" not in os.environ:
    # Local mode - load from .env file
    if os.path.exists(".env"):
        load_dotenv()
        print("📝 Loaded configuration from .env file")
else:
    # Cloud mode - environment variables already set by Cloud Run
    print("☁️ Using Cloud Run environment variables")

# In cloud mode, remove GOOGLE_APPLICATION_CREDENTIALS to use service account
if os.environ.get("ENVIRONMENT", "local") == "cloud":
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

# ---------------- CONFIG ----------------
ODOO_URL = os.environ["ODOO_URL"]
DB = os.environ["ODOO_DB"]
USERNAME = os.environ["ODOO_USERNAME"]
PASSWORD = os.environ["ODOO_PASSWORD"]
ODOO_MODEL = os.environ.get("ODOO_MODEL", "sale.order")
BQ_TABLE_ID = os.environ["BQ_TABLE_ID"]
STATE_FILE = os.environ.get("STATE_FILE", f"sync_state_{ODOO_MODEL.replace('.', '_')}.json")
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", 1000))
BUFFER_MINUTES = int(os.environ.get("BUFFER_MINUTES", 2))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", -1))
DELETE_SYNCED_RECORDS = os.environ.get("DELETE_SYNCED_RECORDS", "false").lower() == "true"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
GCS_BUCKET = os.environ.get("GCS_BUCKET", None)
# ----------------------------------------


# --- Extract project ID from BQ_TABLE_ID ---
BQ_PROJECT_ID = BQ_TABLE_ID.split('.')[0] if '.' in BQ_TABLE_ID else None

# --- Odoo connection ---
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USERNAME, PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# --- BigQuery client ---
bq_client = bigquery.Client(project=BQ_PROJECT_ID)

# --- GCS client (for cloud checkpoint) ---
gcs_client = storage.Client(project=BQ_PROJECT_ID) if ENVIRONMENT == "cloud" and GCS_BUCKET else None

# --- Checkpoint handling ---
def get_last_synced_time():
    if ENVIRONMENT == "cloud" and GCS_BUCKET:
        try:
            blob = gcs_client.bucket(GCS_BUCKET).blob(STATE_FILE)
            if blob.exists():
                content = blob.download_as_text()
                data = json.loads(content)
                return datetime.fromisoformat(data.get("last_synced"))
        except Exception as e:
            print(f"⚠️ Failed to read checkpoint from GCS: {e}")
        return datetime.now(UTC) - timedelta(days=1)
    else:
        if not os.path.exists(STATE_FILE):
            return datetime.now(UTC) - timedelta(days=1)
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return datetime.fromisoformat(data.get("last_synced"))

def update_last_synced_time(ts):
    if ENVIRONMENT == "cloud" and GCS_BUCKET:
        try:
            blob = gcs_client.bucket(GCS_BUCKET).blob(STATE_FILE)
            blob.upload_from_string(json.dumps({"last_synced": ts.isoformat()}))
        except Exception as e:
            print(f"⚠️ Failed to write checkpoint to GCS: {e}")
    else:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_synced": ts.isoformat()}, f)

# --- Fetch fields dynamically ---
def get_model_fields(model):
    try:
        fields_info = models.execute_kw(DB, uid, PASSWORD, model, 'fields_get', [], {'attributes': ['string', 'type']})
        return list(fields_info.keys())
    except Exception as e:
        print(f"❌ Error fetching fields for model {model}: {e}")
        return []

# --- Fetch records ---
def fetch_records():
    last_synced = get_last_synced_time()
    now = datetime.now(UTC)
    buffer_time = now - timedelta(minutes=BUFFER_MINUTES)

    print(f"📅 Date range: {last_synced.strftime('%Y-%m-%d %H:%M:%S')} to {buffer_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # TEMPORARY: Remove date filter for testing - fetch any records
    domain = []

    print(f"🔍 Domain filter: {domain} (NO DATE FILTER - TESTING MODE)")

    fields = get_model_fields(ODOO_MODEL)
    if not fields:
        print(f"No fields found for model {ODOO_MODEL}, aborting.")
        return [], last_synced

    try:
        # First, check total count of all records
        total_count = models.execute_kw(DB, uid, PASSWORD, ODOO_MODEL, 'search_count', [[]])
        print(f"📊 Total {ODOO_MODEL} records in Odoo: {total_count}")

        # Then fetch with date filter (limited to 1 for testing)
        records = models.execute_kw(
            DB, uid, PASSWORD,
            ODOO_MODEL, 'search_read',
            [domain],
            {'fields': fields, 'limit': 1}  # TESTING: Only fetch 1 record
        )
        return records, buffer_time
    except Exception as e:
        print(f"❌ Error fetching records from Odoo: {e}")
        return [], last_synced

# --- Push to BigQuery ---
def sync_to_bigquery(records):
    if not records:
        print("No new records to sync.")
        return True
    try:
        errors = bq_client.insert_rows_json(BQ_TABLE_ID, records)
        if errors:
            print("⚠️ BigQuery insert errors:", errors)
            return False
        print(f"✅ Inserted {len(records)} rows into BigQuery.")
        return True
    except Exception as e:
        print("❌ BigQuery error:", e)
        return False

# --- Delete after successful sync ---
def delete_synced_records(records):
    ids = [r['id'] for r in records]
    try:
        models.execute_kw(DB, uid, PASSWORD, ODOO_MODEL, 'unlink', [ids])
        print(f"🧹 Deleted {len(ids)} {ODOO_MODEL} records from Odoo.")
    except Exception as e:
        print(f"⚠️ Failed to delete {ODOO_MODEL} records: {e}")

def python_type_to_bq(value):
    """Infer BigQuery type from Python value

    Note: Odoo uses False for NULL/empty values and has inconsistent boolean fields
    We treat ALL booleans as STRING to avoid type conflicts
    """
    # Treat None as STRING (safest default)
    if value is None:
        return "STRING"
    # Treat ALL booleans as STRING (Odoo quirk - inconsistent types)
    if isinstance(value, bool):
        return "STRING"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, list):
        return "STRING"  # Store as JSON string
    if isinstance(value, dict):
        return "STRING"  # Store as JSON string
    return "STRING"  # Default

def generate_create_table_sql(table_id, record):
    """Generate CREATE TABLE SQL from actual record data"""
    parts = table_id.split('.')
    if len(parts) != 3:
        return None

    project_id, dataset_id, table_name = parts

    fields = []
    for key, value in record.items():
        bq_type = python_type_to_bq(value)
        fields.append(f"  {key} {bq_type}")

    sql = f"CREATE TABLE `{project_id}.{dataset_id}.{table_name}` (\n"
    sql += ",\n".join(fields)
    sql += "\n);"

    return sql

def sanitize_record_for_bq(record):
    """Convert and sanitize record values for BigQuery compatibility"""
    sanitized = {}
    for key, value in record.items():
        # Handle None/null values
        if value is None:
            sanitized[key] = None
        # Convert empty lists/dicts to None
        elif isinstance(value, (list, dict)) and not value:
            sanitized[key] = None
        # Convert lists and dicts to JSON strings
        elif isinstance(value, (list, dict)):
            sanitized[key] = json.dumps(value)
        # Convert booleans to strings to avoid type conflicts
        # Odoo uses False for NULL and has inconsistent boolean fields
        # So we convert both True and False to strings for consistency
        elif isinstance(value, bool):
            sanitized[key] = "true" if value else "false"
        # Convert empty strings to None
        elif isinstance(value, str) and value.strip() == '':
            sanitized[key] = None
        # Keep other values as-is
        else:
            sanitized[key] = value
    return sanitized

def get_existing_ids():
    """Get list of IDs already in BigQuery to avoid duplicates"""
    try:
        query = f"SELECT id FROM `{BQ_TABLE_ID}`"
        query_job = bq_client.query(query)
        results = query_job.result()
        existing_ids = {row.id for row in results}
        print(f"📊 Found {len(existing_ids)} existing records in BigQuery")
        return existing_ids
    except Exception as e:
        print(f"⚠️ Could not fetch existing IDs (table might be empty): {e}")
        return set()

def fetch_records_batch(offset, limit, date_filter=None):
    """Fetch a batch of records from Odoo with pagination"""
    try:
        domain = []
        if date_filter:
            domain = [
                ('create_date', '>', date_filter['from'].strftime('%Y-%m-%d %H:%M:%S')),
                ('create_date', '<=', date_filter['to'].strftime('%Y-%m-%d %H:%M:%S'))
            ]

        # Order by create_date to sync from oldest first
        records = models.execute_kw(
            DB, uid, PASSWORD,
            ODOO_MODEL, 'search_read',
            [domain],
            {
                'limit': limit,
                'offset': offset,
                'order': 'create_date asc'  # Start from oldest
            }
        )
        return records
    except Exception as e:
        print(f"❌ Error fetching batch from Odoo: {e}")
        return []

# --- Main runner ---
def run_sync():
    print(f"🔁 Starting Odoo to BigQuery sync")
    print(f"📦 Model: {ODOO_MODEL}")
    print(f"🎯 Target: {BQ_TABLE_ID}")
    print(f"📅 Lookback: {'All records' if LOOKBACK_DAYS == -1 else f'{LOOKBACK_DAYS} days'}")
    print(f"🗑️  Delete after sync: {DELETE_SYNCED_RECORDS}")
    print(f"📦 Batch size: {BATCH_LIMIT}\n")

    # Step 1: Check if table exists
    print(f"🔍 Checking if table exists...")
    table_exists = False
    try:
        table = bq_client.get_table(BQ_TABLE_ID)
        print(f"✅ Table found: {table.project}.{table.dataset_id}.{table.table_id}")
        print(f"📊 Table has {len(table.schema)} fields\n")
        table_exists = True
    except Exception as e:
        print(f"❌ Table not found: {e}")
        print(f"💡 Generating CREATE TABLE SQL...\n")

        # Fetch one sample record to generate schema
        try:
            print(f"📥 Fetching sample record from Odoo...")
            sample = models.execute_kw(
                DB, uid, PASSWORD,
                ODOO_MODEL, 'search_read',
                [[]],
                {'limit': 1}
            )

            if sample:
                print(f"✅ Fetched sample record\n")
                print(f"{'='*70}")
                print(f"🔨 GENERATED CREATE TABLE SQL (formatted)")
                print(f"{'='*70}")
                create_sql = generate_create_table_sql(BQ_TABLE_ID, sample[0])
                if create_sql:
                    print(create_sql)
                print(f"{'='*70}\n")

                # Print one-line version for easy copying from logs
                print(f"{'='*70}")
                print(f"📋 ONE-LINE VERSION (copy from logs)")
                print(f"{'='*70}")
                if create_sql:
                    one_line_sql = ' '.join(create_sql.split())
                    print(one_line_sql)
                print(f"{'='*70}\n")

                print(f"💡 Copy the one-line SQL above and run it in BigQuery console")
                print(f"💡 Then run this script again to sync data")
            else:
                print(f"❌ No records found in Odoo to generate schema")
        except Exception as schema_error:
            print(f"❌ Error generating schema: {schema_error}")

        return

    # Step 2: Get existing IDs to avoid duplicates
    print(f"📊 Fetching existing IDs from BigQuery...")
    existing_ids = get_existing_ids()
    print()

    # Step 3: Determine date range
    date_filter = None
    if LOOKBACK_DAYS != -1:
        now = datetime.now(UTC)
        from_date = now - timedelta(days=LOOKBACK_DAYS)
        to_date = now - timedelta(minutes=BUFFER_MINUTES)
        date_filter = {'from': from_date, 'to': to_date}
        print(f"📅 Syncing records from {from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')}\n")
    else:
        print(f"📅 Syncing ALL records from Odoo\n")

    # Step 4: Fetch and sync in batches
    offset = 0
    total_fetched = 0
    total_inserted = 0
    total_skipped = 0
    total_deleted = 0
    total_failed = 0

    while True:
        print(f"📥 Fetching batch at offset {offset}...")
        batch = fetch_records_batch(offset, BATCH_LIMIT, date_filter)

        if not batch:
            print(f"✅ No more records to fetch\n")
            break

        total_fetched += len(batch)
        print(f"✅ Fetched {len(batch)} records (total: {total_fetched})")

        # Filter out duplicates
        new_records = [r for r in batch if r['id'] not in existing_ids]
        skipped = len(batch) - len(new_records)
        total_skipped += skipped

        if skipped > 0:
            print(f"⏭️  Skipped {skipped} duplicate records")

        if new_records:
            # Sanitize records
            sanitized_records = [sanitize_record_for_bq(r) for r in new_records]

            # Insert to BigQuery with insertId for deduplication
            print(f"📤 Inserting {len(sanitized_records)} records to BigQuery...")
            try:
                # Use insert_id based on Odoo record ID for deduplication
                rows_to_insert = [
                    {
                        "json": record,
                        "insertId": f"{ODOO_MODEL}_{record['id']}"
                    }
                    for record in sanitized_records
                ]

                errors = bq_client.insert_rows_json(BQ_TABLE_ID, [r["json"] for r in rows_to_insert], row_ids=[r["insertId"] for r in rows_to_insert])

                if errors:
                    # Get the set of failed indices
                    failed_indices = {err['index'] for err in errors}
                    failed_count = len(failed_indices)
                    success_count = len(sanitized_records) - failed_count

                    print(f"⚠️ {failed_count} records failed, {success_count} succeeded")

                    # Log detailed errors
                    print(f"❌ Failed records details:")
                    for error in errors[:10]:  # Show first 10 errors
                        idx = error.get('index', 'unknown')
                        record_id = new_records[idx]['id'] if idx != 'unknown' and idx < len(new_records) else 'unknown'
                        error_msgs = error.get('errors', [])
                        for err_detail in error_msgs:
                            location = err_detail.get('location', 'unknown')
                            reason = err_detail.get('reason', 'unknown')
                            message = err_detail.get('message', 'unknown')
                            print(f"  Record ID {record_id}: {location} - {reason}: {message}")

                    if len(errors) > 10:
                        print(f"  ... and {len(errors) - 10} more errors")

                    # Track which records succeeded
                    succeeded_records = [r for i, r in enumerate(new_records) if i not in failed_indices]

                    # Add only successful records to existing_ids
                    for r in succeeded_records:
                        existing_ids.add(r['id'])

                    inserted_count = success_count
                    total_failed += failed_count

                    # Delete only successfully synced records from Odoo
                    if DELETE_SYNCED_RECORDS and inserted_count > 0:
                        ids_to_delete = [r['id'] for r in succeeded_records]
                        print(f"🗑️  Deleting {len(ids_to_delete)} successfully synced records from Odoo...")
                        try:
                            models.execute_kw(DB, uid, PASSWORD, ODOO_MODEL, 'unlink', [ids_to_delete])
                            total_deleted += len(ids_to_delete)
                            print(f"✅ Deleted {len(ids_to_delete)} records from Odoo")
                        except Exception as e:
                            print(f"⚠️ Failed to delete records: {e}")

                else:
                    inserted_count = len(sanitized_records)
                    print(f"✅ Inserted {inserted_count} records")

                    # Add to existing_ids to avoid re-inserting in next batches
                    for r in new_records:
                        existing_ids.add(r['id'])

                    # Delete from Odoo if configured
                    if DELETE_SYNCED_RECORDS and inserted_count > 0:
                        ids_to_delete = [r['id'] for r in new_records]
                        print(f"🗑️  Deleting {len(ids_to_delete)} records from Odoo...")
                        try:
                            models.execute_kw(DB, uid, PASSWORD, ODOO_MODEL, 'unlink', [ids_to_delete])
                            total_deleted += len(ids_to_delete)
                            print(f"✅ Deleted {len(ids_to_delete)} records from Odoo")
                        except Exception as e:
                            print(f"⚠️ Failed to delete records: {e}")

                total_inserted += inserted_count

            except Exception as e:
                print(f"❌ BigQuery insert error: {e}")
                print(f"⚠️ Skipping this batch and continuing...")

        # Move to next batch
        offset += BATCH_LIMIT
        print()

        # Safety limit: max 100 batches per run (100k records at 1000 batch size)
        if offset >= BATCH_LIMIT * 100:
            print(f"⚠️ Reached safety limit of 100 batches. Run again to continue.")
            break

    # Step 5: Print summary
    print(f"{'='*70}")
    print(f"📊 SYNC SUMMARY")
    print(f"{'='*70}")
    print(f"Total fetched: {total_fetched}")
    print(f"Total inserted: {total_inserted}")
    print(f"Total failed: {total_failed}")
    print(f"Total skipped (duplicates): {total_skipped}")
    if DELETE_SYNCED_RECORDS:
        print(f"Total deleted from Odoo: {total_deleted}")
    print(f"{'='*70}\n")

    if total_failed > 0:
        print(f"⚠️ Sync completed with {total_failed} failures (check logs above for details)")
    else:
        print(f"✅ Sync completed successfully")

if __name__ == "__main__":
    run_sync()
