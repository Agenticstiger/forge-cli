#!/usr/bin/env python3
"""
Setup BigQuery dataset and table for Bitcoin price ingestion.
Part 1: Manual ingestion (no Cloud Functions needed)
"""

import sys

from google.cloud import bigquery


def setup_bigquery(project_id: str = "<<YOUR_PROJECT_HERE>>"):
    """Create BigQuery dataset and table."""

    client = bigquery.Client(project=project_id)

    # Create dataset
    dataset_id = f"{project_id}.crypto_data"

    try:
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = "US"
        dataset.description = "Cryptocurrency price data"
        dataset = client.create_dataset(dataset, exists_ok=True)
        print(f"✅ Dataset {dataset_id} created or already exists")
    except Exception as e:
        print(f"❌ Error creating dataset: {e}")
        return False

    # Create table schema
    table_id = f"{dataset_id}.bitcoin_prices"

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
        table = bigquery.Table(table_id, schema=schema)

        # Delete existing table to handle schema changes
        try:
            client.delete_table(table_id)
            print(f"🗑️  Deleted existing table {table_id}")
        except Exception:
            pass  # Table doesn't exist, that's fine

        # Create table with schema
        table = bigquery.Table(table_id, schema=schema)

        # Add partitioning by day
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field="price_timestamp"
        )

        table = client.create_table(table)
        print(f"✅ Table {table_id} created")
        print("   - Partitioned by: price_timestamp (daily)")
        print(f"   - Location: {dataset.location}")

        return True

    except Exception as e:
        print(f"❌ Error creating table: {e}")
        return False


if __name__ == "__main__":
    project_id = sys.argv[1] if len(sys.argv) > 1 else "<<YOUR_PROJECT_HERE>>"

    print("🚀 Setting up BigQuery for Bitcoin price ingestion")
    print(f"   Project: {project_id}")
    print()

    if setup_bigquery(project_id):
        print()
        print("✅ Setup complete! Ready to ingest Bitcoin prices.")
        print()
        print("Next steps:")
        print("  1. Run: python ingest_bitcoin_prices_bigquery.py")
        print("  2. Query data in BigQuery console")
        print(f"     https://console.cloud.google.com/bigquery?project={project_id}")
    else:
        print()
        print("❌ Setup failed. Check errors above.")
        sys.exit(1)
