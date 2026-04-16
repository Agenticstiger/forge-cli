# Incremental Processing - Efficient Data Updates

**Time**: 10 min | **Difficulty**: Intermediate | **Track**: Foundation

## Overview

Master incremental data processing with watermark tracking, merge strategies, and upsert patterns. Process only new/changed data for efficient large-scale pipelines instead of reprocessing everything.

## Key Concepts

- **Watermark Tracking**: Remember what you've processed
- **Incremental Loads**: Process only new/changed records
- **Merge Strategies**: Update existing, insert new (UPSERT)
- **Change Data Capture**: Detect and process changes

## Quick Start

```bash
fluid init my-incremental --template incremental-processing
cd my-incremental
fluid apply --local  # First run: full load
fluid apply --local  # Subsequent runs: incremental
```

## How It Works

### First Run (Full Load)
```
Raw Data (10 records) → Process All → Target Table (10 records)
Watermark: 2024-01-14 13:30:00
```

### Second Run (Incremental)
```
Raw Data (5 new records since watermark) → Process Only New → Merge into Target
Watermark Updated: 2024-01-15 10:45:00
```

## Watermark Pattern

### 1. Track Last Processed Timestamp
```sql
-- processing_watermark table
table_name      | last_processed_at       | updated_at
transactions    | 2024-01-14 13:30:00     | 2024-01-15 10:00:00
```

### 2. Query Only New Records
```sql
SELECT *
FROM raw_transactions
WHERE updated_at > (
  SELECT last_processed_at 
  FROM processing_watermark
  WHERE table_name = 'transactions'
)
```

### 3. Update Watermark After Success
```sql
UPDATE processing_watermark
SET last_processed_at = (SELECT MAX(updated_at) FROM processed_records)
WHERE table_name = 'transactions'
```

## Merge Strategies

### APPEND (Insert Only)
Best for: Immutable event data
```yaml
outputs:
  - name: event_log
    materialization: incremental
    incremental_strategy: append
```

### MERGE (Upsert)
Best for: Dimension tables with updates
```yaml
outputs:
  - name: customer_dim
    materialization: incremental
    unique_key: customer_id
    incremental_strategy: merge  # UPDATE if exists, INSERT if new
```

### DELETE+INSERT
Best for: Partitioned data
```yaml
outputs:
  - name: daily_facts
    materialization: incremental
    unique_key: date
    incremental_strategy: delete+insert
```

## Template Logic

### is_incremental() Check
```sql
{% if is_incremental() %}
  -- Incremental: only process new records
  WHERE updated_at > (SELECT MAX(last_processed_at) FROM watermark)
{% else %}
  -- Full refresh: process everything
{% endif %}
```

### Run Modes

**Full Refresh** (first run or manual):
```bash
fluid apply --local --full-refresh
```

**Incremental** (default after first run):
```bash
fluid apply --local  # Processes only new data
```

## Performance Benefits

### Without Incremental
```
Day 1: Process 1M records (10 min)
Day 2: Process 1M records (10 min) ← Wasteful!
Day 3: Process 1M records (10 min) ← Wasteful!
```

### With Incremental
```
Day 1: Process 1M records (10 min)
Day 2: Process 10K new (30 sec) ✓ 20x faster!
Day 3: Process 15K new (45 sec) ✓ 13x faster!
```

## Advanced Patterns

### Late-Arriving Data
Handle records that arrive out of order:
```sql
WHERE updated_at > (
  SELECT last_processed_at - INTERVAL '1 hour'  -- Look back 1 hour
  FROM processing_watermark
)
```

### Soft Deletes
Track deletions in source:
```sql
SELECT 
  *,
  CASE WHEN deleted_at IS NOT NULL THEN true ELSE false END as is_deleted
FROM source
WHERE updated_at > watermark OR deleted_at > watermark
```

### Multi-Column Watermarks
Use composite keys:
```sql
WHERE (partition_date, updated_at) > (
  SELECT (last_partition, last_timestamp) FROM watermark
)
```

## Monitoring Incremental Loads

### Track Load Metrics
```sql
SELECT 
  run_date,
  records_processed,
  records_inserted,
  records_updated,
  processing_time_seconds
FROM load_metrics
ORDER BY run_date DESC
LIMIT 10
```

### Detect Anomalies
```sql
-- Alert if processed count drops significantly
SELECT 
  run_date,
  records_processed,
  AVG(records_processed) OVER (ORDER BY run_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) as avg_last_7_days
FROM load_metrics
WHERE records_processed < avg_last_7_days * 0.5  -- 50% drop
```

## Troubleshooting

### Issue: No new records processed

**Check watermark**:
```sql
SELECT * FROM processing_watermark WHERE table_name = 'transactions';
```

**Reset watermark** (to reprocess):
```sql
UPDATE processing_watermark 
SET last_processed_at = '2024-01-01 00:00:00'
WHERE table_name = 'transactions';
```

### Issue: Duplicate records

**Solution**: Ensure unique_key is set
```yaml
outputs:
  - name: my_table
    unique_key: id  # Required for merge strategy
```

### Issue: Watermark not updating

**Solution**: Check dependencies - watermark update must run AFTER data load
```yaml
outputs:
  - name: update_watermark
    depends_on: [data_load]  # Ensure proper order
```

## Success Criteria

- [ ] Watermark table created and initialized
- [ ] First run loads all records (full refresh)
- [ ] Second run processes only new records
- [ ] Watermark updates after successful load
- [ ] Merge strategy prevents duplicates
- [ ] Incremental runs faster than full refresh

## Next Steps

- **009-testing-your-contract**: Test incremental logic
- **011-first-dag**: Schedule incremental runs
- **012-pipeline-orchestration**: Backfill strategies

**Pro Tip**: Always include `updated_at` timestamp in source data for reliable incremental processing.
