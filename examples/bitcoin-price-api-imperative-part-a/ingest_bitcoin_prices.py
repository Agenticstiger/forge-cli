"""
Bitcoin Price Ingestion Script

Fetches Bitcoin prices from CoinGecko API and saves locally as CSV.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add parent directories to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd

from fluid_build.util.network import safe_get

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Configuration
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
OUTPUT_DIR = Path("runtime")
OUTPUT_FILE = OUTPUT_DIR / "bitcoin_prices.csv"


def fetch_bitcoin_price():
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


def transform_to_dataframe(raw_data):
    """Transform API response into DataFrame."""
    now = datetime.utcnow()
    last_updated = datetime.fromtimestamp(raw_data["last_updated_at"])

    # Create structured record
    record = {
        "price_timestamp": last_updated,
        "price_usd": float(raw_data["usd"]),
        "price_eur": float(raw_data["eur"]),
        "price_gbp": float(raw_data["gbp"]),
        "market_cap_usd": float(raw_data.get("usd_market_cap", 0)),
        "volume_24h_usd": float(raw_data.get("usd_24h_vol", 0)),
        "price_change_24h_percent": float(raw_data.get("usd_24h_change", 0)),
        "last_updated": last_updated,
        "ingestion_timestamp": now,
    }

    df = pd.DataFrame([record])

    logger.info("🔄 Transformed data:")
    logger.info(f"   💰 Price: ${record['price_usd']:,.2f} USD")
    logger.info(f"   📊 24h Change: {record['price_change_24h_percent']:.2f}%")
    logger.info(f"   📈 Market Cap: ${record['market_cap_usd']/1e9:.2f}B")
    logger.info(f"   💱 Volume 24h: ${record['volume_24h_usd']/1e9:.2f}B")

    return df


def save_to_csv(df):
    """Save DataFrame to CSV (append mode)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Check if file exists
    file_exists = OUTPUT_FILE.exists()

    # Append or create new file
    df.to_csv(OUTPUT_FILE, mode="a" if file_exists else "w", header=not file_exists, index=False)

    logger.info(f"💾 Saved to {OUTPUT_FILE}")

    # Show record count
    if file_exists:
        total_records = len(pd.read_csv(OUTPUT_FILE))
        logger.info(f"📊 Total records in file: {total_records}")


def main():
    """Main entry point."""
    try:
        logger.info("🚀 Starting Bitcoin price ingestion...")

        # Fetch data
        raw_data = fetch_bitcoin_price()

        # Transform
        df = transform_to_dataframe(raw_data)

        # Save
        save_to_csv(df)

        logger.info("✅ Ingestion completed successfully!")

        return 0

    except Exception as e:
        logger.error(f"❌ Ingestion failed: {str(e)}")
        return 1


if __name__ == "__main__":
    exit(main())
