"""
Bitcoin Price Ingestion Script - BigQuery Edition

Fetches Bitcoin prices from CoinGecko API and loads into BigQuery.
Designed to run on GCP Cloud Functions with Cloud Scheduler.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Add parent directories to path for imports (for local testing)
try:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from fluid_build.util.network import safe_get
except ImportError:
    # Fallback to requests if fluid_build not available (Cloud Functions)
    import requests

    safe_get = requests.get

import pandas as pd
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Configuration from environment variables
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-project-id")
DATASET_ID = os.environ.get("DATASET_ID", "crypto_data")
TABLE_ID = os.environ.get("TABLE_ID", "bitcoin_prices")
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"


def fetch_bitcoin_price() -> Dict[str, Any]:
    """Fetch current Bitcoin price from CoinGecko API."""
    params = {
        "ids": "bitcoin",
        "vs_currencies": "usd,eur,gbp",
        "include_market_cap": "true",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }

    logger.info("📡 Fetching Bitcoin price from CoinGecko API...")

    try:
        response = safe_get(COINGECKO_API_URL, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()
        logger.info("✅ Successfully fetched data")

        return data["bitcoin"]

    except Exception as e:
        logger.error(f"❌ Failed to fetch Bitcoin price: {e}")
        raise


def transform_to_records(raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transform API response into BigQuery-ready records."""
    now = datetime.utcnow()
    last_updated = datetime.fromtimestamp(raw_data["last_updated_at"])

    # Create structured record
    record = {
        "price_timestamp": last_updated.isoformat(),
        "price_usd": float(raw_data["usd"]),
        "price_eur": float(raw_data["eur"]),
        "price_gbp": float(raw_data["gbp"]),
        "market_cap_usd": float(raw_data.get("usd_market_cap", 0)),
        "volume_24h_usd": float(raw_data.get("usd_24h_vol", 0)),
        "price_change_24h_percent": float(raw_data.get("usd_24h_change", 0)),
        "last_updated": last_updated.isoformat(),
        "ingestion_timestamp": now.isoformat(),
    }

    logger.info("🔄 Transformed data:")
    logger.info(f"   💰 Price: ${record['price_usd']:,.2f} USD")
    logger.info(f"   📊 24h Change: {record['price_change_24h_percent']:.2f}%")
    logger.info(f"   📈 Market Cap: ${record['market_cap_usd']/1e9:.2f}B")
    logger.info(f"   💱 Volume 24h: ${record['volume_24h_usd']/1e9:.2f}B")

    return [record]


def create_table_if_not_exists(client: bigquery.Client, table_ref: str):
    """Create BigQuery table if it doesn't exist."""
    schema = [
        bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("price_eur", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("price_gbp", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("market_cap_usd", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("volume_24h_usd", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("price_change_24h_percent", "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("last_updated", "TIMESTAMP", mode="NULLABLE"),
        bigquery.SchemaField("ingestion_timestamp", "TIMESTAMP", mode="REQUIRED"),
    ]

    try:
        client.get_table(table_ref)
        logger.info(f"✅ Table {table_ref} already exists")
    except NotFound:
        logger.info(f"📋 Creating table {table_ref}...")

        table = bigquery.Table(table_ref, schema=schema)

        # Configure partitioning by ingestion_timestamp (daily)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="ingestion_timestamp",
        )

        # Configure clustering for query optimization
        table.clustering_fields = ["price_timestamp"]

        client.create_table(table)
        logger.info(f"✅ Created table {table_ref}")


def load_to_bigquery(records: List[Dict[str, Any]]) -> int:
    """Load records into BigQuery."""
    client = bigquery.Client(project=PROJECT_ID)
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

    # Create table if needed
    create_table_if_not_exists(client, table_ref)

    # Configure load job
    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("price_timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("price_usd", "FLOAT64", mode="REQUIRED"),
            bigquery.SchemaField("price_eur", "FLOAT64", mode="REQUIRED"),
            bigquery.SchemaField("price_gbp", "FLOAT64", mode="REQUIRED"),
            bigquery.SchemaField("market_cap_usd", "FLOAT64"),
            bigquery.SchemaField("volume_24h_usd", "FLOAT64"),
            bigquery.SchemaField("price_change_24h_percent", "FLOAT64"),
            bigquery.SchemaField("last_updated", "TIMESTAMP"),
            bigquery.SchemaField("ingestion_timestamp", "TIMESTAMP", mode="REQUIRED"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )

    # Load data
    logger.info(f"💾 Loading {len(records)} record(s) to {table_ref}...")

    job = client.load_table_from_json(records, table_ref, job_config=job_config)

    # Wait for completion
    job.result()

    logger.info(f"✅ Loaded {len(records)} rows to {table_ref}")

    # Get row count
    query = f"SELECT COUNT(*) as total FROM `{table_ref}`"
    result = list(client.query(query).result())
    total_rows = result[0].total

    logger.info(f"📊 Total rows in table: {total_rows}")

    return len(records)


def main(request=None):
    """
    Main entry point for Cloud Function.

    Args:
        request: Flask request object (unused for scheduled functions)

    Returns:
        Tuple of (response_body, status_code)
    """
    try:
        logger.info("🚀 Starting Bitcoin price ingestion...")

        # Fetch data
        raw_data = fetch_bitcoin_price()

        # Transform
        records = transform_to_records(raw_data)

        # Load to BigQuery
        rows_loaded = load_to_bigquery(records)

        logger.info("✅ Ingestion completed successfully!")

        return {
            "success": True,
            "rows_loaded": rows_loaded,
            "timestamp": datetime.utcnow().isoformat(),
        }, 200

    except Exception as e:
        logger.error(f"❌ Ingestion failed: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e), "timestamp": datetime.utcnow().isoformat()}, 500


# For local testing
if __name__ == "__main__":
    result, status = main()
    print(f"Result: {result}")
    print(f"Status: {status}")
