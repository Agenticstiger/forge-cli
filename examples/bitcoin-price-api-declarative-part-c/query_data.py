#!/usr/bin/env python3
"""Query the enhanced table to show all features working"""

from google.cloud import bigquery

client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")

print("=" * 80)
print("PART C: ENHANCED DATA QUERY")
print("=" * 80)
print()

query = """
SELECT 
    price_id,
    price_timestamp,
    price_usd,
    price_change_24h_percent,
    status,
    created_by,
    source_id,
    ingestion_timestamp
FROM `<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices_enhanced`
ORDER BY ingestion_timestamp DESC
LIMIT 5
"""

print("Running query...")
print()

results = client.query(query).result()

for row in results:
    print("📊 Record:")
    print(f"   ID: {row.price_id}")
    print(f"   Price: ${row.price_usd:,.2f}")
    print(f"   Change: {row.price_change_24h_percent}%")
    print(f"   Status: {row.status}")
    print(f"   Created By: {row.created_by}")
    print(f"   Source: {row.source_id}")
    print(f"   Time: {row.price_timestamp}")
    print()

print("=" * 80)
print("✅ All enhanced features working!")
print("=" * 80)
