# Multiple Outputs - Bronze/Silver/Gold Architecture

**Time**: 6 min | **Difficulty**: Beginner | **Track**: Foundation

## Overview

Master multi-layered data architecture with bronze/silver/gold patterns. Create 10 outputs (7 tables + 3 views) from a single contract, demonstrating layered transformations and optimal output design.

## Architecture Pattern

```
RAW DATA → BRONZE → SILVER → GOLD → VIEWS
(Landing)  (Append)  (Clean)  (Aggregate)  (Quick access)
```

**Layers**:
- **Bronze**: Raw data with load timestamp (1 table)
- **Silver**: Cleaned, standardized data (3 tables)
- **Gold**: Business metrics and KPIs (2 tables)
- **Views**: Quick access patterns (3 views)

## Quick Start

```bash
fluid init my-analytics --template multiple-outputs
cd my-analytics
fluid apply --local
fluid query "SELECT * FROM conversion_funnel"
```

## 10 Outputs Created

### Bronze Layer (Raw + Timestamp)
1. `bronze_events` - Raw events with loaded_at

### Silver Layer (Cleaned Data)
2. `silver_events_cleaned` - Standardized events
3. `silver_sessions` - Session aggregates
4. `silver_user_activity` - User summaries

### Gold Layer (Business Metrics)
5. `gold_daily_metrics` - Daily KPIs
6. `gold_device_metrics` - Device performance

### Views (Analytics Ready)
7. `high_value_users` - Users with purchases
8. `active_sessions` - Sessions with 3+ events
9. `conversion_funnel` - Homepage → Products → Purchase

## Tables vs Views

**Use Tables When**:
- Data changes infrequently
- Query performance is critical
- Need historical snapshots
- Complex transformations

**Use Views When**:
- Real-time data needed
- Simple filtering/sorting
- Storage is limited
- Query logic changes often

## Sample Output

### Conversion Funnel (View)
```
step      | users | conversion_pct
Homepage  | 5     | 100.00
Products  | 3     | 60.00
Purchase  | 2     | 40.00
```

### Gold Daily Metrics (Table)
```
metric_date | total_events | unique_users | purchases
2024-01-15  | 12           | 5            | 2
```

## Customization

Add more layers:
```yaml
outputs:
  - name: platinum_executive_summary
    depends_on: [gold_daily_metrics]
    type: view
```

## Success Criteria

- [ ] 7 tables created (bronze/silver/gold)
- [ ] 3 views created (analytics)
- [ ] Dependencies respected (silver depends on bronze)
- [ ] Conversion funnel shows 3 steps
- [ ] All outputs queryable

## Next Steps

- **007-environment-configuration**: Add env-specific outputs
- **008-incremental-processing**: Make layers incremental
- **011-first-dag**: Auto-generate DAG with execution phases

**Pro Tip**: Start with bronze → silver → gold, add views last for performance.
