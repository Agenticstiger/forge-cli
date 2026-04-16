#!/usr/bin/env python3
"""Check if policy tags are attached to table fields"""

from google.cloud import bigquery

client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")
table = client.get_table("crypto_data.bitcoin_prices_enhanced")

print("=" * 80)
print("POLICY TAGS ON TABLE FIELDS")
print("=" * 80)
print()

has_tags = False
for field in table.schema:
    if field.policy_tags and field.policy_tags.names:
        has_tags = True
        print(f"✅ {field.name}")
        print(f"   Policy Tags: {field.policy_tags.names}")
        print()

if not has_tags:
    print("⚠️  No policy tags found on any fields")
    print()
    print("This means the table needs to be updated with the new schema.")
    print("The schema update was 'additive only', so policy tags weren't applied.")
    print()
    print("To fix: Drop and recreate the table:")
    print(
        "  1. Backup data: bq extract crypto_data.bitcoin_prices_enhanced gs://bucket/backup.json"
    )
    print("  2. Drop table: bq rm -t crypto_data.bitcoin_prices_enhanced")
    print("  3. Re-apply: python -m fluid_build apply contract.fluid.yaml")
    print("  4. Restore data if needed")
    print()

print("=" * 80)
print("TAXONOMY VERIFICATION")
print("=" * 80)
print()

# Check if taxonomies exist
from google.cloud import datacatalog_v1

dc_client = datacatalog_v1.PolicyTagManagerClient()
parent = "projects/<<YOUR_PROJECT_HERE>>/locations/us"

try:
    print("Listing taxonomies...")
    count = 0
    for taxonomy in dc_client.list_taxonomies(parent=parent):
        count += 1
        print(f"\n✅ Taxonomy: {taxonomy.display_name}")
        print(f"   Name: {taxonomy.name}")
        print("   Policy Tags:")

        for tag in dc_client.list_policy_tags(parent=taxonomy.name):
            print(f"      - {tag.display_name} ({tag.name})")

    if count == 0:
        print("⚠️  No taxonomies found")
    else:
        print(f"\n✅ Found {count} taxonomies")
except Exception as e:
    print(f"❌ Failed to list taxonomies: {e}")

print()
