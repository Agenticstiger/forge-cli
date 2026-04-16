#!/usr/bin/env python3
"""
Verify Policy Tags, Data Masking, and Column Restrictions Implementation
"""

import sys

from google.cloud import bigquery


def main():
    project = "<<YOUR_PROJECT_HERE>>"
    dataset = "crypto_data"
    table_name = "bitcoin_prices_enhanced"

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.{table_name}"

    print("=" * 80)
    print("POLICY FEATURES VERIFICATION")
    print("=" * 80)
    print()

    # Get table
    try:
        table = client.get_table(table_ref)
    except Exception as e:
        print(f"❌ Failed to get table: {e}")
        return 1

    print(f"📋 Table: {table_name}")
    print(f"   Created: {table.created}")
    print(f"   Rows: {table.num_rows}")
    print()

    # Check for policy tags
    print("=" * 80)
    print("PHASE 1: POLICY TAGS")
    print("=" * 80)
    print()

    policy_tag_count = 0
    for field in table.schema:
        if field.policy_tags and field.policy_tags.names:
            policy_tag_count += 1
            print(f"✅ {field.name}")
            print(f"   Policy Tags: {field.policy_tags.names}")
            print()

    if policy_tag_count == 0:
        print("ℹ️  No policy tags attached (Data Catalog API may not be enabled)")
        print("   Run: gcloud services enable datacatalog.googleapis.com")
        print()
    else:
        print(f"✅ Found {policy_tag_count} fields with policy tags")
        print()

    # Check for masking (via descriptions or data policies)
    print("=" * 80)
    print("PHASE 2: DATA MASKING")
    print("=" * 80)
    print()

    # Data masking policies would be checked via DataPolicyService API
    # For now, show which fields are marked for masking in the contract
    masked_fields = ["ingestion_metadata", "last_updated"]

    for field_name in masked_fields:
        field = next((f for f in table.schema if f.name == field_name), None)
        if field:
            print(f"ℹ️  {field_name}")
            print("   Contract defines masking strategy (hash/tokenize)")
            print("   To apply: Enable bigquerydatapolicy.googleapis.com")
            print()

    # Check for restricted views
    print("=" * 80)
    print("PHASE 3: COLUMN ACCESS RESTRICTIONS")
    print("=" * 80)
    print()

    # List views in dataset
    try:
        tables = list(client.list_tables(f"{project}.{dataset}"))
        restricted_views = [
            t for t in tables if "_restricted" in t.table_id and table_name in t.table_id
        ]

        if restricted_views:
            print(f"✅ Found {len(restricted_views)} restricted views:")
            for view in restricted_views:
                print(f"   - {view.table_id}")

                # Get view details
                view_obj = client.get_table(f"{project}.{dataset}.{view.table_id}")
                print(
                    f"     Columns: {len(view_obj.schema)} (vs {len(table.schema)} in base table)"
                )
                print(f"     Description: {view_obj.description}")
                print()
        else:
            print("ℹ️  No restricted views found")
            print("   Contract defines column restrictions that will be applied on next apply")
            print()
    except Exception as e:
        print(f"⚠️  Failed to check for restricted views: {e}")
        print()

    # Summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()

    features = []
    if policy_tag_count > 0:
        features.append(f"✅ Policy Tags: {policy_tag_count} fields")
    else:
        features.append("⚠️  Policy Tags: Enable datacatalog.googleapis.com")

    features.append("ℹ️  Data Masking: Enable bigquerydatapolicy.googleapis.com")

    if restricted_views:
        features.append(f"✅ Column Restrictions: {len(restricted_views)} views created")
    else:
        features.append("ℹ️  Column Restrictions: Will be created on next apply")

    for feature in features:
        print(feature)

    print()
    print("=" * 80)
    print("NEXT STEPS")
    print("=" * 80)
    print()
    print("To enable full policy features:")
    print()
    print("1. Enable Data Catalog API:")
    print("   gcloud services enable datacatalog.googleapis.com --project=<<YOUR_PROJECT_HERE>>")
    print()
    print("2. Enable Data Policy API:")
    print(
        "   gcloud services enable bigquerydatapolicy.googleapis.com --project=<<YOUR_PROJECT_HERE>>"
    )
    print()
    print("3. Re-apply contract:")
    print("   python -m fluid_build apply contract.fluid.yaml")
    print()
    print("4. Verify policy tags:")
    print("   bq show --schema <<YOUR_PROJECT_HERE>>:crypto_data.bitcoin_prices_enhanced")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
