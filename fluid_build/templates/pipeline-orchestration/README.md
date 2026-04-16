# Pipeline Orchestration - Production-Grade DAGs

**Time**: 12 min | **Difficulty**: Intermediate | **Track**: Intermediate

## Overview

Master advanced pipeline orchestration with complex DAG patterns, parallel execution, task groups, and production-grade monitoring. Build enterprise-ready data pipelines.

## Key Features

- **Task Groups**: Organize related tasks
- **Parallel Execution**: Run independent tasks simultaneously
- **Priority Weights**: Control task execution order
- **Resource Pools**: Manage concurrency
- **SLA Monitoring**: Track performance
- **Callbacks**: Custom notifications
- **Backfill Support**: Historical data processing

## Quick Start

```bash
fluid init my-orchestration --template pipeline-orchestration
cd my-orchestration

# Generate DAG
fluid generate-dag --output dags/

# Start Airflow
docker-compose up -d

# Access UI: http://localhost:8080
# Trigger DAG manually or wait for schedule
```

## DAG Structure

```
┌─────────────────────────────────────────────────┐
│          DATA INGESTION (Parallel)              │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐     │
│  │  Sales   │  │ Inventory │  │Customers │     │
│  │ Cleaned  │  │  Cleaned  │  │ Cleaned  │     │
│  └────┬─────┘  └─────┬─────┘  └────┬─────┘     │
└───────┼──────────────┼─────────────┼───────────┘
        │              │             │
        └──────┬───────┴─────────────┘
               │
        ┌──────▼──────────────────────────────────┐
        │   TRANSFORMATION (Sequential)           │
        │          ┌──────────────┐               │
        │          │    Sales     │               │
        │          │   Enriched   │               │
        │          └──────┬───────┘               │
        └─────────────────┼─────────────────────────┘
                          │
        ┌─────────────────┼─────────────────────────┐
        │    AGGREGATION (Parallel)                 │
        │  ┌───────┐  ┌─────────┐  ┌────────┐      │
        │  │Region │  │Category │  │  Date  │      │
        │  │ Sales │  │  Sales  │  │ Sales  │      │
        │  └───┬───┘  └────┬────┘  └───┬────┘      │
        └──────┼───────────┼───────────┼───────────┘
               └───────────┴───────────┘
                          │
        ┌─────────────────▼─────────────────────────┐
        │      QUALITY CHECKS & PUBLISH             │
        │    ┌──────────┐     ┌──────────┐          │
        │    │Validation│     │Executive │          │
        │    │  Checks  │────►│ Summary  │          │
        │    └──────────┘     └──────────┘          │
        └───────────────────────────────────────────┘
```

## Task Groups

### Define Groups
```yaml
task_groups:
  - name: data_ingestion
    description: "Parallel data ingestion"
    parallel: true
  
  - name: data_transformation
    description: "Sequential transformations"
    parallel: false
    depends_on: [data_ingestion]
```

### Assign Tasks to Groups
```yaml
outputs:
  - name: sales_cleaned
    task_group: data_ingestion  # Runs in parallel with other ingestion tasks
```

## Parallel vs Sequential

### Parallel Execution (3 tasks run simultaneously)
```yaml
task_groups:
  - name: aggregations
    parallel: true  # All tasks in group run at once
```

### Sequential Execution (tasks run one after another)
```yaml
task_groups:
  - name: data_transformation
    parallel: false  # Tasks run in dependency order
```

## Advanced Features

### Priority Weights
Control which tasks run first:
```yaml
outputs:
  - name: critical_table
    priority_weight: 10  # Higher = runs first
  
  - name: reporting_table
    priority_weight: 1   # Lower = runs later
```

### Resource Pools
Limit concurrent execution:
```yaml
outputs:
  - name: heavy_aggregation
    pool: aggregation_pool  # Max 3 concurrent
    pool_slots: 2           # Uses 2 slots
```

### Custom Retries per Task
```yaml
outputs:
  - name: flaky_external_api
    retries: 10                    # Override default
    retry_delay_minutes: 2
    retry_exponential_backoff: true
    max_retry_delay_minutes: 30
```

### SLA Monitoring
```yaml
outputs:
  - name: time_critical_report
    sla_minutes: 15  # Alert if takes >15 min
```

## Callbacks & Notifications

### On Success
```yaml
callbacks:
  on_success:
    - type: slack
      webhook: ${SLACK_WEBHOOK}
      message: "✅ Pipeline completed"
```

### On Failure
```yaml
callbacks:
  on_failure:
    - type: email
      to: ["oncall@example.com"]
    
    - type: pagerduty
      integration_key: ${PAGERDUTY_KEY}
```

### On SLA Miss
```yaml
monitoring:
  alert_on_sla_miss: true
  sla_hours: 2
```

## Backfill Strategies

### Configuration
```yaml
backfill:
  enabled: true
  max_backfill_days: 90     # How far back to allow
  chunk_size_days: 7        # Process 7 days at a time
  parallel_backfills: 3     # Run 3 chunks in parallel
```

### Run Backfill
```bash
# Backfill specific date range
airflow dags backfill complex-orchestration \
  --start-date 2024-01-01 \
  --end-date 2024-01-31

# Backfill with dry-run
airflow dags backfill complex-orchestration \
  --start-date 2024-01-01 \
  --end-date 2024-01-07 \
  --dry-run
```

## Monitoring & Metrics

### Built-in Metrics
- Execution time per task
- Success/failure rates
- SLA violations
- Resource utilization

### Custom Metrics
```yaml
monitoring:
  custom_metrics:
    - name: total_revenue_processed
      query: "SELECT SUM(total_revenue) FROM sales_by_region"
    
    - name: pipeline_efficiency
      query: "SELECT records_per_minute FROM performance_stats"
```

### View Metrics
```bash
# Airflow UI → Admin → Metrics
# Or via API
curl http://localhost:8080/api/v1/dags/complex-orchestration/dagRuns
```

## Production Best Practices

### 1. Use Task Groups for Organization
✅ Good:
```yaml
task_groups:
  - name: bronze_layer
  - name: silver_layer
  - name: gold_layer
```

❌ Bad: Flat structure with 50+ tasks

### 2. Set Appropriate Timeouts
```yaml
orchestration:
  dagrun_timeout_minutes: 120      # Entire DAG
  
outputs:
  - name: long_running_task
    execution_timeout_minutes: 60  # Individual task
```

### 3. Implement Circuit Breakers
```yaml
validations:
  - name: data_volume_check
    level: error
    query: |
      SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END
      FROM {{ ref('sales_cleaned') }}
```

### 4. Use Idempotent Operations
Ensure re-runs produce same results:
```sql
-- Use MERGE instead of INSERT
MERGE INTO target USING source ON key
WHEN MATCHED THEN UPDATE
WHEN NOT MATCHED THEN INSERT
```

## Troubleshooting

### Issue: Tasks not running in parallel

**Solution**: Check task group configuration
```yaml
task_groups:
  - name: my_group
    parallel: true  # Ensure this is set
```

### Issue: SLA violations

**Solution**: Optimize or increase SLA
```bash
# Check task durations
airflow tasks list complex-orchestration --tree

# Adjust SLA
sla_minutes: 120  # Increase if needed
```

### Issue: DAG not appearing

**Solution**: Validate syntax
```bash
# Check for errors
fluid generate-dag --validate

# View Airflow logs
docker logs airflow-scheduler
```

## Success Criteria

- [ ] DAG generated with task groups
- [ ] Parallel tasks execute simultaneously
- [ ] Sequential dependencies respected
- [ ] SLA monitoring configured
- [ ] Callbacks working (test with failure)
- [ ] Backfill capability verified
- [ ] All validations passing
- [ ] Execution completes in <10 minutes

## Next Steps

- **014-api-data-integration**: Add API sources to orchestration
- **021-bigquery-deployment**: Deploy orchestrated pipeline to cloud
- **034-streaming-data**: Real-time orchestration patterns

**Pro Tip**: Start simple with sequential tasks, then optimize with parallelization where safe.
