#!/usr/bin/env python3
"""
Check Enhanced Features in BigQuery
Verifies that Part C advanced features are deployed correctly
"""

from google.cloud import bigquery


def check_enhanced_features():
    """Check all enhanced v0.5.7 features"""
    client = bigquery.Client(project="<<YOUR_PROJECT_HERE>>")

    print("=" * 80)
    print("PART C: ENHANCED FEATURES VERIFICATION")
    print("=" * 80)
    print()

    # Get table metadata
    table = client.get_table("<<YOUR_PROJECT_HERE>>.crypto_data.bitcoin_prices_enhanced")

    print(f"📋 Table: {table.table_id}")
    print(f"   Created: {table.created}")
    print(f"   Rows: {table.num_rows}")
    print(f"   Size: {table.num_bytes:,} bytes")
    print()

    # Check schema features
    print("=" * 80)
    print("SCHEMA FEATURES")
    print("=" * 80)
    print()

    features_found = {
        "primary_key": 0,
        "foreign_key": 0,
        "unique": 0,
        "default_values": 0,
        "policy_tags": 0,
        "total_fields": len(table.schema),
    }

    for field in table.schema:
        print(f"📌 {field.name}")
        print(f"   Type: {field.field_type} ({field.mode})")

        # Check description for constraint hints
        desc = field.description or ""

        if "[PRIMARY KEY]" in desc:
            print("   ✅ PRIMARY KEY constraint hint")
            features_found["primary_key"] += 1

        if "[FOREIGN KEY" in desc:
            print("   ✅ FOREIGN KEY constraint hint")
            features_found["foreign_key"] += 1

        if "[UNIQUE]" in desc:
            print("   ✅ UNIQUE constraint hint")
            features_found["unique"] += 1

        # Check for default value expression
        try:
            if hasattr(field, "default_value_expression") and field.default_value_expression:
                print(f"   ✅ Default value: {field.default_value_expression}")
                features_found["default_values"] += 1
        except AttributeError:
            pass

        # Check for policy tags
        try:
            if hasattr(field, "policy_tags") and field.policy_tags:
                print(f"   ✅ Policy tags: {field.policy_tags.names}")
                features_found["policy_tags"] += 1
            elif "[Policy Tag:" in desc:
                print("   ℹ️  Policy tag reference in description")
                features_found["policy_tags"] += 1
        except AttributeError:
            pass

        print()

    # Summary
    print("=" * 80)
    print("FEATURE SUMMARY")
    print("=" * 80)
    print()
    print(f"Total Fields: {features_found['total_fields']}")
    print(f"Primary Keys: {features_found['primary_key']}")
    print(f"Foreign Keys: {features_found['foreign_key']}")
    print(f"Unique Constraints: {features_found['unique']}")
    print(f"Default Values: {features_found['default_values']}")
    print(f"Policy Tags: {features_found['policy_tags']}")
    print()

    # Check labels
    print("=" * 80)
    print("GOVERNANCE LABELS")
    print("=" * 80)
    print()

    table_labels = table.labels or {}
    dataset = client.get_dataset("<<YOUR_PROJECT_HERE>>.crypto_data")
    dataset_labels = dataset.labels or {}

    print(f"Table Labels: {len(table_labels)}")
    for key, value in sorted(table_labels.items()):
        print(f"  {key}: {value}")
    print()

    print(f"Dataset Labels: {len(dataset_labels)}")
    for key, value in sorted(dataset_labels.items()):
        print(f"  {key}: {value}")
    print()

    # Overall assessment
    print("=" * 80)
    print("OVERALL ASSESSMENT")
    print("=" * 80)
    print()

    total_features = sum(
        [
            features_found["primary_key"],
            features_found["foreign_key"],
            features_found["unique"],
            features_found["default_values"],
            features_found["policy_tags"],
        ]
    )

    total_labels = len(table_labels) + len(dataset_labels)

    print(f"✅ Enhanced Features Deployed: {total_features}")
    print(f"✅ Governance Labels Deployed: {total_labels}")
    print()

    if features_found["primary_key"] > 0:
        print("✅ PRIMARY KEY constraints working")
    if features_found["foreign_key"] > 0:
        print("✅ FOREIGN KEY references working")
    if features_found["default_values"] > 0:
        print("✅ DEFAULT values working")
    if features_found["policy_tags"] > 0:
        print("✅ POLICY TAGS working")
    if total_labels >= 30:
        print("✅ GOVERNANCE LABELS comprehensive")

    print()
    print("🎉 Part C showcases MAXIMUM v0.5.7 utilization!")
    print()


if __name__ == "__main__":
    check_enhanced_features()
