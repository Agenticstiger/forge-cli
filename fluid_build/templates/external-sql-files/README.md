# External SQL Files - Modular SQL Organization

**Time to Complete**: 5 minutes  
**Difficulty**: Beginner  
**Track**: Foundation  

## Overview

Learn how to organize complex SQL transformations in external files for better code organization, maintainability, and team collaboration. This template demonstrates best practices for structuring SQL logic across multiple files.

## Why Use External SQL Files?

**Benefits**:
- **Better organization**: Keep contract.yaml clean and focused on structure
- **Reusability**: Share SQL logic across multiple projects
- **Version control**: Track SQL changes independently
- **Testing**: Test SQL files in isolation
- **Collaboration**: Multiple team members can work on different SQL files
- **IDE support**: Full SQL syntax highlighting and linting

## Quick Start

### 1. Create Project from Template

```bash
fluid init my-sales-analytics --template external-sql-files
cd my-sales-analytics
```

### 2. Explore the File Structure

```
my-sales-analytics/
├── contract.fluid.yaml
├── data/
│   └── sales.csv
└── sql/
    ├── calculate_revenue.sql
    └── daily_summary.sql
```

### 3. Run Locally

```bash
# Validate contract
fluid validate

# Execute pipeline
fluid apply --local

# Query results
fluid query "SELECT * FROM daily_sales_summary"
```

## Expected Output

### Sales with Revenue Table
Revenue calculations from external SQL file:
```
sale_id | product_id | quantity | gross_revenue | discount_amount | net_revenue
1       | 101        | 5        | 149.95        | 0.00            | 149.95
2       | 102        | 3        | 149.97        | 15.00           | 134.97
3       | 101        | 2        | 59.98         | 3.00            | 56.98
...
```

### Daily Sales Summary Table
Aggregated from external SQL file:
```
sale_date   | total_transactions | unique_customers | total_net_revenue
2024-01-05  | 1                  | 1                | 149.95
2024-01-06  | 1                  | 1                | 134.97
2024-01-07  | 1                  | 1                | 56.98
...
```

## Understanding the Pattern

### Contract References SQL Files

Instead of inline SQL in `contract.fluid.yaml`:

```yaml
outputs:
  - name: sales_with_revenue
    type: table
    sql_file: ./sql/calculate_revenue.sql  # Reference external file
```

### SQL Files Use Jinja2 Templates

The SQL files support `{{ ref() }}` for dependencies:

```sql
-- sql/calculate_revenue.sql
SELECT 
  sale_id,
  ROUND(quantity * unit_price, 2) AS gross_revenue,
  ROUND(quantity * unit_price * (1 - discount_pct / 100.0), 2) AS net_revenue
FROM {{ ref('raw_sales') }}
```

### SQL Files Can Reference Other Outputs

Build layered transformations:

```sql
-- sql/daily_summary.sql
SELECT 
  sale_date,
  SUM(net_revenue) AS total_net_revenue
FROM {{ ref('sales_with_revenue') }}  -- References previous output
GROUP BY sale_date
```

## Best Practices for SQL Files

### 1. One Logical Transformation Per File

```
sql/
├── clean_customers.sql       # Data cleaning
├── calculate_metrics.sql     # Business logic
└── create_aggregates.sql     # Aggregations
```

### 2. Add Comments and Documentation

```sql
-- calculate_revenue.sql
-- Purpose: Calculate gross, discount, and net revenue per transaction
-- Inputs: raw_sales (from CSV)
-- Outputs: sales_with_revenue table
-- Owner: analytics-team

SELECT 
  sale_id,
  -- Gross revenue before discounts
  ROUND(quantity * unit_price, 2) AS gross_revenue,
  ...
```

### 3. Use CTEs for Readability

```sql
WITH revenue_calculations AS (
  SELECT 
    sale_id,
    quantity * unit_price AS gross_revenue
  FROM {{ ref('raw_sales') }}
),
discount_applications AS (
  SELECT 
    sale_id,
    gross_revenue * (discount_pct / 100.0) AS discount_amount
  FROM revenue_calculations
)
SELECT * FROM discount_applications
```

## Success Criteria

- [ ] SQL files created in `sql/` directory
- [ ] Contract references SQL files via `sql_file:` property
- [ ] SQL files use `{{ ref() }}` for dependencies
- [ ] All transformations execute successfully
- [ ] Output tables match expected schema
- [ ] SQL files are properly commented
- [ ] File structure is organized and logical

## Next Steps

### Related Templates

- **005-data-quality-validation**: Add validations to SQL transformations
- **009-testing-your-contract**: Write comprehensive tests for SQL files
- **010-contract-documentation**: Auto-generate docs from SQL comments
- **006-multiple-outputs**: Create many outputs from organized SQL files

## Resources

- [FLUID SQL File Documentation](https://docs.fluid.io/sql-files)
- [Jinja2 Template Guide](https://docs.fluid.io/jinja2)
- [SQL Best Practices](https://docs.fluid.io/sql-best-practices)

---

**Pro Tip**: Start with inline SQL in contracts, then extract to external files as complexity grows.
