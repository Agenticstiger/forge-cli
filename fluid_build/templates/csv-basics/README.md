# CSV to Data Product - Template

**Time to Complete**: 5 minutes  
**Difficulty**: Beginner  
**Tutorial**: [002-csv-to-data-product.md](../../docs/docs/quickstart/002-csv-to-data-product.md)

## Overview

Learn how to ingest CSV files and transform them into validated, production-ready data products. This template includes realistic customer data and shows best practices for schema definition, data validation, and enrichment.

## What You'll Learn

- ✅ Define input sources (CSV files)
- ✅ Schema validation and type mapping
- ✅ Data transformations (filtering, enrichment)
- ✅ Data quality validations
- ✅ Customer segmentation patterns

## Quick Start

```bash
# Create project from this template
fluid init customer-data --template csv-basics

# Navigate to project
cd customer-data

# Preview the sample data
cat data/customers.csv | head -5

# Validate the contract
fluid validate

# Run locally
fluid apply --local

# Query the results
fluid query "SELECT * FROM customers_clean ORDER BY total_purchases DESC LIMIT 5"
```

## Sample Data Included

The template includes `data/customers.csv` with 10 sample customer records:

```csv
id,name,email,signup_date,status,total_purchases,last_purchase_date
1,Alice Johnson,alice.johnson@email.com,2024-01-15,active,15,2026-01-10
2,Bob Smith,bob.smith@email.com,2024-02-20,active,8,2026-01-12
...
```

## Expected Output

After running `fluid apply --local`, you'll get a `customers_clean` table:

```
┌─────────────┬─────────────────┬───────────────────────────┬──────────────┬─────────────────┬──────────────────┐
│ customer_id │ full_name       │ email                     │ signup_date  │ customer_segment│ is_active        │
├─────────────┼─────────────────┼───────────────────────────┼──────────────┼─────────────────┼──────────────────┤
│ 4           │ David Brown     │ david.brown@email.com     │ 2024-04-05   │ VIP             │ true             │
│ 8           │ Henry Wilson    │ henry.w@email.com         │ 2024-08-30   │ Loyal           │ true             │
│ 1           │ Alice Johnson   │ alice.johnson@email.com   │ 2024-01-15   │ Loyal           │ true             │
│ 5           │ Eve Davis       │ eve.davis@email.com       │ 2024-05-12   │ Loyal           │ true             │
│ 9           │ Ivy Martinez    │ ivy.martinez@email.com    │ 2024-09-14   │ Regular         │ true             │
└─────────────┴─────────────────┴───────────────────────────┴──────────────┴─────────────────┴──────────────────┘
```

## Contract Walkthrough

### Input Definition

```yaml
inputs:
  - name: raw_customers
    type: csv
    location: "data/customers.csv"
    schema:
      - name: email
        type: VARCHAR
        constraints:
          - type: not_null  # Email is required
```

**Key Concepts**:
- `type: csv` tells FLUID this is a CSV file
- `location` is relative to the contract file
- `schema` defines expected columns and types
- `constraints` add validation rules

### Transformation

```yaml
transformations:
  - name: clean_and_enrich_customers
    output: customers_clean
    sql: |
      SELECT
        id AS customer_id,
        LOWER(TRIM(email)) AS email,  -- Standardize emails
        CASE
          WHEN total_purchases >= 20 THEN 'VIP'
          WHEN total_purchases >= 10 THEN 'Loyal'
          ELSE 'Regular'
        END AS customer_segment  -- Derive new field
      FROM {{ ref('raw_customers') }}
```

**Key Concepts**:
- `{{ ref('raw_customers') }}` references the input
- SQL transformations clean and enrich data
- Derived fields add business logic

### Validations

```yaml
validations:
  - name: no_null_emails
    query: |
      SELECT COUNT(*) AS null_email_count
      FROM {{ ref('customers_clean') }}
      WHERE email IS NULL
    expect: null_email_count = 0
```

**Key Concepts**:
- Validations run after transformations
- `expect` defines success criteria
- Failed validations block deployment

## Customization Ideas

### 1. Add Your Own CSV Data

Replace `data/customers.csv` with your own data:
```bash
# Copy your CSV file
cp /path/to/your/data.csv data/customers.csv

# Update schema in contract.fluid.yaml
```

### 2. Add More Transformations

```yaml
- name: high_value_customers
  output: vip_customers
  sql: |
    SELECT *
    FROM {{ ref('customers_clean') }}
    WHERE customer_segment = 'VIP'
```

### 3. Add Data Quality Checks

```yaml
validations:
  - name: email_format_check
    query: |
      SELECT COUNT(*) AS invalid_emails
      FROM {{ ref('customers_clean') }}
      WHERE email NOT LIKE '%@%'
    expect: invalid_emails = 0
```

## Common Patterns

### Customer Segmentation

```sql
CASE
  WHEN total_purchases >= 20 THEN 'VIP'
  WHEN total_purchases >= 10 THEN 'Loyal'
  WHEN total_purchases >= 5 THEN 'Regular'
  ELSE 'New'
END AS customer_segment
```

### Email Standardization

```sql
LOWER(TRIM(email)) AS email
```

### Recency Calculation

```sql
DATEDIFF('day', last_purchase_date, CURRENT_DATE) AS days_since_last_purchase
```

## Next Steps

### 003 - Multi-Source Joins (8 minutes)
Join customer data with order data:
```bash
fluid init customer-orders --template multi-source
```

### 004 - External SQL Files (10 minutes)
Organize SQL in separate files for maintainability:
```bash
fluid init sql-project --template external-sql
```

### 005 - Data Quality Validation (12 minutes)
Advanced validation patterns:
```bash
fluid init quality-checks --template data-quality
```

## Troubleshooting

### CSV File Not Found
```bash
# Ensure CSV is in the correct location
ls data/customers.csv

# Check contract.fluid.yaml has correct path
# location: "data/customers.csv"
```

### Schema Mismatch
```
Error: Column 'xyz' not found in CSV

# Solution: Check CSV headers match schema definition
head -1 data/customers.csv
```

### Data Type Errors
```
Error: Cannot convert 'abc' to INTEGER

# Solution: Verify CSV data matches schema types
# Or cast in SQL: CAST(id AS INTEGER)
```

## Success Criteria

✅ CSV data loads successfully  
✅ Schema validation passes  
✅ All transformations run without errors  
✅ Data quality validations pass  
✅ Output table has derived fields  

## Resources

- 📖 [Input Sources Documentation](../../docs/docs/features/inputs.md)
- 🎓 [CSV Ingestion Best Practices](../../docs/docs/guides/csv-best-practices.md)
- 💬 [Community Forum](https://community.fluiddata.io)

---

**Great job!** 🎉 You've learned how to transform CSV data into validated data products.
