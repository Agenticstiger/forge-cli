#!/usr/bin/env python3
"""Query Bitcoin prices from BigQuery."""

from google.cloud import bigquery


def query_bitcoin_prices(project_id: str = "<<YOUR_PROJECT_HERE>>"):
    """Query recent Bitcoin prices."""
    client = bigquery.Client(project=project_id)

    query = """
    SELECT 
        price_timestamp,
        price_usd,
        price_eur,
        price_gbp,
        price_change_24h_percent,
        market_cap_usd,
        volume_24h_usd
    FROM crypto_data.bitcoin_prices
    ORDER BY price_timestamp DESC
    LIMIT 10
    """

    print("📊 Querying Bitcoin prices from BigQuery...\n")

    results = client.query(query).result()

    print(
        f"{'Timestamp':<25} {'Price USD':<15} {'24h Change %':<15} {'Market Cap':<20} {'Volume 24h':<20}"
    )
    print("-" * 105)

    for row in results:
        timestamp = row.price_timestamp.strftime("%Y-%m-%d %H:%M:%S")
        price = f"${row.price_usd:,.2f}"
        change = f"{row.price_change_24h_percent:.2f}%" if row.price_change_24h_percent else "N/A"
        mcap = f"${row.market_cap_usd/1e9:,.2f}B" if row.market_cap_usd else "N/A"
        volume = f"${row.volume_24h_usd/1e9:,.2f}B" if row.volume_24h_usd else "N/A"

        print(f"{timestamp:<25} {price:<15} {change:<15} {mcap:<20} {volume:<20}")


if __name__ == "__main__":
    query_bitcoin_prices()
