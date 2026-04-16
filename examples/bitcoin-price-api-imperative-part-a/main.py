"""
Cloud Function Entry Point

This is the main entry point for GCP Cloud Functions.
Cloud Functions expects a file named 'main.py' with a function to invoke.
"""

from ingest_bitcoin_prices_bigquery import main as ingest_main


# Export the function with the expected name
def bitcoin_price_ingestion(request):
    """
    Cloud Function entry point for Bitcoin price ingestion.

    This function is triggered by Cloud Scheduler and ingests Bitcoin
    price data into BigQuery.

    Args:
        request: Flask request object (unused for scheduled triggers)

    Returns:
        Response tuple (body, status_code)
    """
    return ingest_main(request)
