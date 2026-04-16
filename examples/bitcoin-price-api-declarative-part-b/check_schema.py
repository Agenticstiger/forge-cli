from google.cloud import bigquery

client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
table = client.get_table("<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices")

print("Table schema:")
for field in table.schema:
    print(f"  {field.name}: {field.field_type}")
