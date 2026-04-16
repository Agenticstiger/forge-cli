#!/usr/bin/env python3
"""Load Bitcoin data and query results from BigQuery."""

import json
from datetime import datetime

import requests
from google.cloud import bigquery

# Fetch Bitcoin data
print("Fetching Bitcoin price...")
resp = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin")
data = resp.json()

row = {
    "price_timestamp": datetime.utcnow().isoformat(),
    "price_usd": data["market_data"]["current_price"]["usd"],
    "price_eur": data["market_data"]["current_price"]["eur"],
    "price_gbp": data["market_data"]["current_price"]["gbp"],
    "market_cap_usd": data["market_data"]["market_cap"]["usd"],
    "volume_24h_usd": data["market_data"]["total_volume"]["usd"],
    "price_change_24h_percent": data["market_data"]["price_change_percentage_24h"],
    "last_updated": data["market_data"]["last_updated"],
    "ingestion_timestamp": datetime.utcnow().isoformat(),
}

print(f"Current BTC price: ${row['price_usd']:,.2f}")

# Save to JSON file
with open("temp_data.json", "w") as f:
    json.dump(row, f)

# Load to BigQuery (batch mode - free tier compatible)
print("\nLoading to BigQuery...")
client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
job_config = bigquery.LoadJobConfig(
    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, write_disposition="WRITE_APPEND"
)

with open("temp_data.json", "rb") as f:
    job = client.load_table_from_file(
        f, "<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices", job_config=job_config
    )
    job.result()

print("✅ Data loaded successfully")

# Query results
print("\n" + "=" * 80)
print("BITCOIN PRICE DATA FROM BIGQUERY")
print("=" * 80)

query = """
SELECT 
    price_timestamp,
    ROUND(price_usd, 2) as price_usd,
    ROUND(price_eur, 2) as price_eur,
    ROUND(price_gbp, 2) as price_gbp,
    ROUND(market_cap_usd, 0) as market_cap_usd,
    ROUND(volume_24h_usd, 0) as volume_24h_usd,
    ROUND(price_change_24h_percent, 2) as price_change_24h_percent,
    last_updated,
    ingestion_timestamp
FROM `<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices` 
ORDER BY price_timestamp DESC 
LIMIT 5
"""

results = client.query(query).result()

for row in results:
    print(
        f"{row.price_timestamp} | BTC | ${row.price_usd:>12,.2f} | "
        f"24h: {row.price_change_24h_percent:+6.2f}% | "
        f"Vol: ${row.volume_24h_usd:>15,.0f}"
    )

print("=" * 80)

# Verify region
dataset = client.get_dataset("<<YOUR_PROJECT_HERE>>.crypto_data")
print(f"\n✅ Dataset region: {dataset.location}")
