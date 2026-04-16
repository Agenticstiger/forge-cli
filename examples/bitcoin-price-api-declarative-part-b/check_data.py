from google.cloud import bigquery

client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
results = list(
    client.query(
        "SELECT COUNT(*) as count FROM `<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices`"
    ).result()
)
dataset = client.get_dataset("<<YOUR_PROJECT_HERE>>.crypto_data")

print(f"Total rows: {results[0].count}")
print(f"Dataset region: {dataset.location}")
