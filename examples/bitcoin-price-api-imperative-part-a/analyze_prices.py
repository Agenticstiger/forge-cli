"""
Bitcoin Price Analytics

Query and analyze the Bitcoin price data.
"""

from pathlib import Path

import pandas as pd

# Load data
data_file = Path("runtime/bitcoin_prices.csv")

if not data_file.exists():
    print("❌ No data found. Run ingest_bitcoin_prices.py first!")
    exit(1)

# Read CSV
df = pd.read_csv(data_file)
df["ingestion_timestamp"] = pd.to_datetime(df["ingestion_timestamp"])
df["price_timestamp"] = pd.to_datetime(df["price_timestamp"])

print("=" * 70)
print("📊 BITCOIN PRICE ANALYTICS")
print("=" * 70)
print()

# Summary statistics
print("💰 CURRENT PRICE")
print("-" * 70)
latest = df.iloc[-1]
print(f"   USD: ${latest['price_usd']:,.2f}")
print(f"   EUR: €{latest['price_eur']:,.2f}")
print(f"   GBP: £{latest['price_gbp']:,.2f}")
print(f"   24h Change: {latest['price_change_24h_percent']:.2f}%")
print()

# Market stats
print("📈 MARKET STATISTICS")
print("-" * 70)
print(f"   Market Cap: ${latest['market_cap_usd']/1e9:.2f}B")
print(f"   24h Volume: ${latest['volume_24h_usd']/1e9:.2f}B")
print(f"   Last Updated: {latest['price_timestamp']}")
print()

# Historical data
print("📋 DATA SUMMARY")
print("-" * 70)
print(f"   Total Records: {len(df)}")
print(f"   Date Range: {df['ingestion_timestamp'].min()} to {df['ingestion_timestamp'].max()}")
print()

if len(df) > 1:
    print("📊 PRICE STATISTICS (USD)")
    print("-" * 70)
    print(f"   Min: ${df['price_usd'].min():,.2f}")
    print(f"   Max: ${df['price_usd'].max():,.2f}")
    print(f"   Avg: ${df['price_usd'].mean():,.2f}")
    print(f"   Std Dev: ${df['price_usd'].std():,.2f}")
    print()

# Recent data
print("📝 RECENT RECORDS (Last 5)")
print("-" * 70)
recent = df.tail(5)[
    ["ingestion_timestamp", "price_usd", "price_change_24h_percent", "volume_24h_usd"]
]
print(recent.to_string(index=False))
print()

print("=" * 70)
print("✅ Analytics complete!")
print("=" * 70)
