# Data Quality Validation - Comprehensive Quality Checks

**Time to Complete**: 6 minutes  
**Difficulty**: Beginner  
**Track**: Foundation  

## Overview

Master data quality validation with comprehensive checks for completeness, accuracy, consistency, and validity. This template demonstrates a multi-level validation strategy that catches data issues before they impact production.

## What You'll Learn

- **Multi-level validations**: ERROR, WARNING, and INFO levels
- **Data quality scoring**: Calculate quality scores per record
- **Data profiling**: Detect outliers and anomalies
- **Quality reporting**: Generate data quality dashboards
- **Best practices**: When to block vs. warn on data issues

## Quick Start

### 1. Create Project from Template

```bash
fluid init my-quality-checks --template data-quality-validation
cd my-quality-checks
```

### 2. Explore the Sample Data

The sample data intentionally includes quality issues:
- ❌ Row 6: Missing product name
- ❌ Row 7: Negative price (-$50)
- ❌ Row 8: Negative stock (-5 units)
- ❌ Row 9: Invalid supplier ID (999, outside 100-200 range)
- ❌ Row 10: Missing last_updated date

```bash
cat data/products.csv
```

### 3. Run Validation

```bash
# Validate contract and run checks
fluid validate

# Execute pipeline (some warnings expected)
fluid apply --local

# View quality report
fluid query "SELECT * FROM data_quality_report"
```

## Expected Output

### Validated Products Table
Products with quality scores:
```
product_id | product_name         | price    | stock | quality_score
1          | Laptop Pro 15        | 1299.99  | 45    | 100
2          | Wireless Mouse       | 29.99    | 150   | 100
6          | NULL                 | 0.00     | 50    | 40
7          | Monitor 27 inch      | -50.00   | 25    | 60
8          | Keyboard Mechanical  | 89.99    | -5    | 80
9          | Desk Lamp            | 45.99    | 80    | 80
10         | Notebook Set         | 12.99    | 500   | 80
```

### Data Quality Report
Summary statistics:
```
total_records         | 10
perfect_records       | 5
good_quality_records  | 7
poor_quality_records  | 3
avg_quality_score     | 82.00
worst_quality_score   | 40
missing_names         | 1
invalid_prices        | 2
negative_stock        | 1
missing_dates         | 1
```

### Validation Results
```
✅ PASS (ERROR)   - no_null_product_ids: 0 issues
✅ PASS (ERROR)   - unique_product_ids: 0 duplicates
✅ PASS (ERROR)   - valid_categories: 0 invalid
⚠️  WARN (WARNING) - check_missing_product_names: 1 missing
⚠️  WARN (WARNING) - check_negative_prices: 1 negative
⚠️  WARN (WARNING) - check_zero_prices: 1 zero price
⚠️  WARN (WARNING) - check_negative_stock: 1 negative
⚠️  WARN (WARNING) - check_supplier_range: 1 invalid
⚠️  WARN (WARNING) - check_stale_data: 1 stale
ℹ️  INFO (INFO)    - price_distribution_check: No outliers
ℹ️  INFO (INFO)    - minimum_record_count: Sufficient data
```

## Understanding Validation Levels

### ERROR Level - Critical (Blocks Pipeline)

Use for violations that make data **unusable**:
- Missing required fields (IDs, keys)
- Duplicate primary keys
- Invalid enum values
- Referential integrity violations

```yaml
validations:
  - name: no_null_product_ids
    level: error  # Blocks execution
    query: |
      SELECT COUNT(*) as invalid_count
      FROM {{ ref('raw_products') }}
      WHERE product_id IS NULL
    expect:
      invalid_count: 0
```

### WARNING Level - Important (Logs but Continues)

Use for issues that need **attention** but don't break the pipeline:
- Missing optional fields
- Out-of-range values
- Unusual patterns
- Stale data

```yaml
validations:
  - name: check_negative_prices
    level: warning  # Logs but continues
    query: |
      SELECT COUNT(*) as invalid_count
      FROM {{ ref('raw_products') }}
      WHERE price < 0
    expect:
      invalid_count: 0
```

### INFO Level - Monitoring (Tracking Only)

Use for **monitoring trends** and data profiling:
- Statistical outliers
- Record count checks
- Data distribution patterns
- Performance metrics

```yaml
validations:
  - name: price_distribution_check
    level: info  # Track only
    query: |
      SELECT 
        CASE 
          WHEN MAX(price) > (AVG(price) + 3 * STDDEV(price))
          THEN 1 ELSE 0
        END as has_outliers
      FROM {{ ref('raw_products') }}
```

## Data Quality Scoring

Each record gets a quality score (0-100) based on:

```sql
-- 20 points per quality dimension
CAST(
  (CASE WHEN product_name IS NOT NULL THEN 20 ELSE 0 END +      -- Completeness
   CASE WHEN price > 0 THEN 20 ELSE 0 END +                      -- Validity
   CASE WHEN stock_quantity >= 0 THEN 20 ELSE 0 END +            -- Accuracy
   CASE WHEN supplier_id BETWEEN 100 AND 200 THEN 20 ELSE 0 END +-- Consistency
   CASE WHEN last_updated IS NOT NULL THEN 20 ELSE 0 END)        -- Freshness
AS INTEGER) AS data_quality_score
```

**Quality Categories**:
- **100**: Perfect (all checks pass)
- **80-99**: Good (minor issues)
- **60-79**: Fair (multiple issues)
- **<60**: Poor (critical issues)

## Customization Ideas

### 1. Add Custom Business Rules

```yaml
validations:
  - name: luxury_product_check
    level: warning
    query: |
      SELECT COUNT(*) as invalid_count
      FROM {{ ref('raw_products') }}
      WHERE category = 'Electronics'
        AND price < 10  -- Electronics should cost more
    expect:
      invalid_count: 0
```

### 2. Cross-Table Validations

```yaml
validations:
  - name: orphan_products_check
    level: error
    query: |
      SELECT COUNT(*) as orphan_count
      FROM {{ ref('raw_products') }} p
      LEFT JOIN {{ ref('suppliers') }} s 
        ON p.supplier_id = s.supplier_id
      WHERE s.supplier_id IS NULL
    expect:
      orphan_count: 0
```

### 3. Time-Series Checks

```yaml
validations:
  - name: daily_volume_check
    level: warning
    query: |
      SELECT COUNT(*) as anomaly_count
      FROM (
        SELECT 
          last_updated,
          COUNT(*) as daily_count,
          AVG(COUNT(*)) OVER () as avg_daily_count
        FROM {{ ref('raw_products') }}
        GROUP BY last_updated
      )
      WHERE daily_count < avg_daily_count * 0.5  -- 50% drop
    expect:
      anomaly_count: 0
```

### 4. Add Quality Quarantine Table

```yaml
outputs:
  - name: quarantined_products
    type: table
    transformation: |
      SELECT *
      FROM {{ ref('validated_products') }}
      WHERE data_quality_score < 80  -- Failed quality threshold
```

## Best Practices

### 1. Start Simple, Add Incrementally

Begin with critical validations:
```yaml
validations:
  - name: primary_key_check  # Start here
  - name: not_null_check     # Then add
  - name: range_check        # Keep adding
```

### 2. Use Descriptive Messages

```yaml
validations:
  - name: check_prices
    message: "Found {invalid_count} products with negative prices. Check supplier data feeds."
```

### 3. Test Validations with Bad Data

Create test cases with intentional issues:
```csv
# test_bad_data.csv
product_id,price,stock
1,-100,50     # Should fail negative price check
2,50,-10      # Should fail negative stock check
```

### 4. Monitor Validation Trends

Track validation failures over time:
```sql
SELECT 
  validation_date,
  validation_name,
  failure_count
FROM validation_history
ORDER BY validation_date DESC
```

## Troubleshooting

### Issue: "Validation blocking pipeline"

**Solution**: Check validation level
```bash
# See which validation failed
fluid validate --verbose

# Temporarily change to warning
sed -i 's/level: error/level: warning/' contract.fluid.yaml
```

### Issue: "Too many warnings"

**Solution**: Set quality threshold
```yaml
monitoring:
  data_quality_threshold: 80  # Fail if avg score < 80
  max_warnings: 5             # Alert if >5 warnings
```

### Issue: "Validation query too slow"

**Solution**: Add indexes or sample data
```yaml
validations:
  - name: sample_quality_check
    query: |
      SELECT COUNT(*) as invalid_count
      FROM (
        SELECT * FROM {{ ref('raw_products') }}
        LIMIT 1000  -- Sample for large datasets
      )
```

## Success Criteria

- [ ] All 11 validations defined (3 ERROR, 7 WARNING, 1 INFO)
- [ ] Data quality score calculated for each record
- [ ] Quality report generated with summary statistics
- [ ] Critical validations (ERROR) passing
- [ ] Warning validations identifying known issues
- [ ] Quality threshold monitoring configured
- [ ] Validation messages are clear and actionable

## Next Steps

### Related Templates

- **009-testing-your-contract**: Unit test validations
- **006-multiple-outputs**: Create quality vs quarantine outputs
- **011-first-dag**: Schedule daily quality checks
- **012-pipeline-orchestration**: Quality-gated deployments

### Advanced Patterns

1. **Great Expectations Integration**: Use GE library for advanced profiling
2. **ML-Based Anomaly Detection**: Detect unusual patterns automatically
3. **Data Quality Dashboards**: Visualize trends in BI tools
4. **Automated Data Cleansing**: Auto-fix common quality issues

## Resources

- [FLUID Validation Guide](https://docs.fluid.io/validations)
- [Data Quality Frameworks](https://docs.fluid.io/data-quality)
- [Monitoring Best Practices](https://docs.fluid.io/monitoring)

---

**Pro Tip**: Use ERROR for data that breaks your pipeline, WARNING for data that needs investigation, and INFO for tracking trends.
