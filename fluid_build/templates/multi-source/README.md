# Multi-Source Joins - Template

**Time to Complete**: 8 minutes  
**Difficulty**: Beginner  
**Tutorial**: [003-multi-source-joins.md](../../docs/docs/quickstart/003-multi-source-joins.md)

## Overview

Learn how to join multiple data sources to create comprehensive analytics. This template demonstrates joining customer and order data to calculate revenue metrics, customer lifetime value, and customer segmentation - a common real-world pattern.

## What You'll Learn

- ✅ Multiple input source definitions
- ✅ SQL JOIN operations (LEFT JOIN, INNER JOIN)
- ✅ Aggregation functions (COUNT, SUM, AVG)
- ✅ Common Table Expressions (CTEs)
- ✅ Customer revenue analytics
- ✅ Customer tier segmentation

## Quick Start

```bash
# Create project from this template
fluid init customer-analytics --template multi-source

# Navigate to project
cd customer-analytics

# Preview the data
cat data/customers.csv | head -5
cat data/orders.csv | head -5

# Validate the contract
fluid validate

# Run locally
fluid apply --local

# Query the results
fluid query "SELECT * FROM customer_revenue ORDER BY total_revenue DESC"
fluid query "SELECT * FROM vip_customers"
```

## Sample Data Included

### customers.csv (8 customers)
```csv
id,name,email,signup_date,status
1,Alice Johnson,alice.johnson@email.com,2024-01-15,active
2,Bob Smith,bob.smith@email.com,2024-02-20,active
...
```

### orders.csv (22 orders)
```csv
order_id,customer_id,order_date,product_name,quantity,unit_price,total_amount
1001,1,2024-02-01,Laptop,1,999.99,999.99
1002,1,2024-03-15,Mouse,2,24.99,49.98
...
```

## Expected Output

### customer_revenue table
```
┌─────────────┬─────────────────┬───────────────┬──────────────┬────────────┬───────────────┐
│ customer_id │ customer_name   │ total_revenue │ total_orders │ avg_order  │ customer_tier │
├─────────────┼─────────────────┼───────────────┼──────────────┼────────────┼───────────────┤
│ 4           │ David Brown     │ 2959.86       │ 5            │ 591.97     │ Platinum      │
│ 1           │ Alice Johnson   │ 2129.95       │ 5            │ 425.99     │ Platinum      │
│ 8           │ Henry Wilson    │ 1429.95       │ 4            │ 357.49     │ Gold          │
│ 5           │ Eve Davis       │ 1299.98       │ 2            │ 649.99     │ Gold          │
│ 2           │ Bob Smith       │ 1599.97       │ 3            │ 533.32     │ Gold          │
└─────────────┴─────────────────┴───────────────┴──────────────┴────────────┴───────────────┘
```

### vip_customers view (filtered)
Only Platinum and Gold tier customers

## Contract Walkthrough

### Multiple Inputs

```yaml
inputs:
  - name: raw_customers
    type: csv
    location: "data/customers.csv"
    
  - name: raw_orders
    type: csv
    location: "data/orders.csv"
```

**Key Concepts**:
- Multiple inputs defined in same contract
- Each input has its own schema
- Inputs can be joined in transformations

### JOIN Pattern

```sql
SELECT
  c.id AS customer_id,
  c.name AS customer_name,
  COUNT(o.order_id) AS total_orders,
  SUM(o.total_amount) AS total_revenue
FROM {{ ref('raw_customers') }} c
LEFT JOIN {{ ref('raw_orders') }} o
  ON c.id = o.customer_id
GROUP BY c.id, c.name
```

**Key Concepts**:
- `LEFT JOIN` keeps all customers, even those with no orders
- `{{ ref('input_name') }}` references inputs
- `GROUP BY` required for aggregations
- `COUNT`, `SUM` are aggregation functions

### Customer Segmentation

```sql
CASE
  WHEN total_revenue >= 2000 THEN 'Platinum'
  WHEN total_revenue >= 1000 THEN 'Gold'
  WHEN total_revenue >= 500 THEN 'Silver'
  WHEN total_revenue > 0 THEN 'Bronze'
  ELSE 'No Purchases'
END AS customer_tier
```

**Key Concepts**:
- `CASE` statements create derived fields
- Tiering based on business rules
- Multiple conditions evaluated in order

### Multiple Outputs

```yaml
outputs:
  - name: customer_revenue    # Main analytics table
    materialization: table
    
  - name: vip_customers       # Filtered view
    materialization: view
```

**Key Concepts**:
- One contract can create multiple outputs
- `table` materialization stores data
- `view` materialization is a query
- Views reference other outputs

## Common Analytics Patterns

### Customer Lifetime Value (CLV)
```sql
SUM(o.total_amount) AS total_revenue
```

### Purchase Frequency
```sql
COUNT(o.order_id) AS total_orders
```

### Average Order Value (AOV)
```sql
AVG(o.total_amount) AS avg_order_value
-- or
SUM(o.total_amount) / COUNT(o.order_id)
```

### Customer Tenure
```sql
DATEDIFF('day', signup_date, CURRENT_DATE) AS days_as_customer
```

### Recency
```sql
MAX(o.order_date) AS last_order_date,
DATEDIFF('day', MAX(o.order_date), CURRENT_DATE) AS days_since_last_order
```

## Customization Ideas

### 1. Add Product Analytics

```yaml
- name: product_revenue
  output: product_revenue
  sql: |
    SELECT
      product_name,
      COUNT(DISTINCT customer_id) AS unique_customers,
      COUNT(order_id) AS total_orders,
      SUM(total_amount) AS total_revenue
    FROM {{ ref('raw_orders') }}
    GROUP BY product_name
    ORDER BY total_revenue DESC
```

### 2. Add Time-Based Analytics

```yaml
- name: monthly_revenue
  output: monthly_revenue
  sql: |
    SELECT
      DATE_TRUNC('month', order_date) AS month,
      COUNT(DISTINCT customer_id) AS active_customers,
      COUNT(order_id) AS total_orders,
      SUM(total_amount) AS monthly_revenue
    FROM {{ ref('raw_orders') }}
    GROUP BY DATE_TRUNC('month', order_date)
    ORDER BY month
```

### 3. Add Cohort Analysis

```yaml
- name: customer_cohorts
  output: customer_cohorts
  sql: |
    WITH cohort AS (
      SELECT
        id,
        DATE_TRUNC('month', signup_date) AS cohort_month
      FROM {{ ref('raw_customers') }}
    )
    SELECT
      c.cohort_month,
      COUNT(DISTINCT c.id) AS cohort_size,
      COUNT(DISTINCT o.customer_id) AS active_customers,
      SUM(o.total_amount) AS cohort_revenue
    FROM cohort c
    LEFT JOIN {{ ref('raw_orders') }} o ON c.id = o.customer_id
    GROUP BY c.cohort_month
```

## Next Steps

### 004 - External SQL Files (10 minutes)
Organize complex SQL in separate files:
```bash
fluid init sql-organized --template external-sql
```

### 013 - Customer 360 Analytics (10 minutes)
Production-ready customer analytics with DAG:
```bash
fluid init customer360 --template customer-360
```

### 016 - Data Transformation Patterns (18 minutes)
Learn advanced SQL patterns:
```bash
fluid init advanced-transforms --template transform-patterns
```

## Troubleshooting

### No Results in JOIN
```sql
-- Use LEFT JOIN to see all customers
LEFT JOIN {{ ref('raw_orders') }} o

-- Use INNER JOIN to see only customers with orders
INNER JOIN {{ ref('raw_orders') }} o
```

### Aggregation Errors
```
Error: column "name" must appear in GROUP BY

-- Solution: Include all non-aggregated columns in GROUP BY
GROUP BY c.id, c.name, c.email
```

### Division by Zero
```sql
-- Protect against division by zero
CASE
  WHEN total_orders = 0 THEN 0
  ELSE total_revenue / total_orders
END AS avg_order_value

-- Or use COALESCE
COALESCE(total_revenue / NULLIF(total_orders, 0), 0)
```

## Success Criteria

✅ Both CSV files load successfully  
✅ JOIN produces expected results  
✅ Aggregations calculate correctly  
✅ Customer tiers assigned properly  
✅ All validations pass  
✅ VIP customers view works  

## Resources

- 📖 [SQL JOIN Documentation](../../docs/docs/guides/sql-joins.md)
- 🎓 [Customer Analytics Patterns](../../docs/docs/patterns/customer-analytics.md)
- 💬 [Community Forum](https://community.fluiddata.io)

---

**Excellent work!** 🎉 You've mastered multi-source data integration and customer analytics.
