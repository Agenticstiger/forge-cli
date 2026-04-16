# Your First DAG - Template

**Time to Complete**: 15 minutes  
**Difficulty**: Intermediate  
**Tutorial**: [011-your-first-dag.md](../../docs/docs/quickstart/011-your-first-dag.md)

## 🌟 FLAGSHIP FEATURE

This template showcases **FLUID 0.7.1's killer feature**: automatic Airflow DAG generation!

**Write your contract once → Get a production-ready Airflow DAG automatically**

No Airflow code required. No Python DAG files to maintain. Just pure contract-driven development.

## What You'll Learn

- ✅ How FLUID converts contracts to Airflow DAGs
- ✅ Orchestration configuration in contracts
- ✅ Execution phases and task dependencies
- ✅ Zero Airflow code - it's all auto-generated
- ✅ Deploy to local Airflow in one command
- ✅ Monitor pipeline execution

## Quick Start

```bash
# Create project from this template
fluid init my-pipeline --template first-dag

# Navigate to project
cd my-pipeline

# Generate the Airflow DAG (this is the magic!)
fluid generate-dag

# Start local Airflow (requires Docker)
docker compose --profile airflow up -d

# Wait 30 seconds for Airflow to start
sleep 30

# Visit Airflow UI
open http://localhost:8081
# Default credentials: admin/admin
```

## The Magic: Contract → DAG

### Your Contract (What You Write)

```yaml
orchestration:
  enabled: true
  schedule: "0 2 * * *"  # Daily at 2 AM

outputs:
  - name: customers_cleaned
    execution_phase: extract
    
  - name: customer_order_summary
    execution_phase: transform
    depends_on:
      - customers_cleaned
      - orders_cleaned
      
  - name: sales_by_country
    execution_phase: aggregate
    depends_on:
      - customer_order_summary
```

### Generated DAG (What FLUID Creates)

```python
# dags/sales_analytics_pipeline_dag.py (auto-generated!)

from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta

default_args = {
    'owner': 'analytics-team',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    'sales_analytics_pipeline',
    default_args=default_args,
    schedule_interval='0 2 * * *',
    catchup=False,
    tags=['sales', 'analytics', 'auto-generated'],
) as dag:
    
    # Extract phase
    extract_customers = PythonOperator(
        task_id='extract_customers_cleaned',
        python_callable=run_transformation,
        op_kwargs={'output': 'customers_cleaned'},
    )
    
    extract_orders = PythonOperator(
        task_id='extract_orders_cleaned',
        python_callable=run_transformation,
        op_kwargs={'output': 'orders_cleaned'},
    )
    
    # Transform phase
    transform_summary = PythonOperator(
        task_id='transform_customer_order_summary',
        python_callable=run_transformation,
        op_kwargs={'output': 'customer_order_summary'},
    )
    
    # Aggregate phase
    aggregate_sales = PythonOperator(
        task_id='aggregate_sales_by_country',
        python_callable=run_transformation,
        op_kwargs={'output': 'sales_by_country'},
    )
    
    # Validate phase
    validate_data = PythonOperator(
        task_id='validate_data',
        python_callable=run_validations,
    )
    
    # Define task dependencies (auto-detected from contract!)
    [extract_customers, extract_orders] >> transform_summary
    transform_summary >> aggregate_sales
    aggregate_sales >> validate_data
```

**You never write this Python code - FLUID generates it from your contract!**

## Expected DAG Structure

```
┌─────────────────────────────────────────────────────────┐
│                   DAG: sales_analytics_pipeline         │
│                   Schedule: 0 2 * * * (Daily at 2 AM)  │
└─────────────────────────────────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │   EXTRACT PHASE (Parallel)    │
              ├───────────────┬───────────────┤
              │ clean_customers│ clean_orders │
              └───────┬────────┴───────┬───────┘
                      │                │
                      └────────┬───────┘
                               ▼
              ┌────────────────────────────────┐
              │      TRANSFORM PHASE           │
              │  summarize_customer_orders     │
              └────────────────┬───────────────┘
                               ▼
              ┌────────────────────────────────┐
              │      AGGREGATE PHASE           │
              │   aggregate_sales_by_country   │
              └────────────────┬───────────────┘
                               ▼
              ┌────────────────────────────────┐
              │      VALIDATE PHASE            │
              │   Run data quality checks      │
              └────────────────────────────────┘
```

## Key Concepts

### 1. Orchestration Configuration

```yaml
orchestration:
  enabled: true              # Turn on DAG generation
  schedule: "0 2 * * *"      # Cron expression
  start_date: "2024-01-01"   # When DAG starts
  catchup: false             # Don't backfill
  max_active_runs: 1         # One run at a time
```

### 2. Execution Phases

```yaml
outputs:
  - name: customers_cleaned
    execution_phase: extract    # Phase 1
    
  - name: customer_summary
    execution_phase: transform  # Phase 2
    depends_on:
      - customers_cleaned
```

**Available Phases**:
- `extract` - Load and clean raw data
- `transform` - Business logic and joins
- `aggregate` - Summary calculations
- `validate` - Data quality checks
- `publish` - Final outputs

### 3. Task Dependencies

```yaml
depends_on:
  - customers_cleaned
  - orders_cleaned
```

FLUID automatically creates Airflow task dependencies based on your `depends_on` declarations!

### 4. Monitoring

```yaml
monitoring:
  enabled: true
  sla_seconds: 3600
  email_on_failure: true
  retries: 2
  retry_delay_seconds: 300
```

## Running Your DAG

### Local Airflow (Recommended for Learning)

```bash
# 1. Generate DAG
fluid generate-dag

# 2. Start Airflow
docker compose --profile airflow up -d

# 3. Access Airflow UI
open http://localhost:8081
# Login: admin / admin

# 4. Enable your DAG
# Click the toggle switch next to "sales_analytics_pipeline"

# 5. Trigger manually (don't wait for schedule)
# Click the play button → "Trigger DAG"

# 6. Monitor execution
# Click on the DAG name to see the graph view
# Watch tasks turn green as they complete
```

### Production Deployment

```bash
# Deploy to production Airflow
fluid deploy --env prod --provider airflow

# Or deploy to cloud-managed Airflow
fluid deploy --env prod --provider composer  # GCP Cloud Composer
fluid deploy --env prod --provider mwaa      # AWS Managed Airflow
```

## Expected Output

After the DAG runs successfully:

### sales_by_country table
```
┌────────────┬─────────────────┬──────────────┬───────────────┐
│ country    │ total_customers │ total_orders │ total_revenue │
├────────────┼─────────────────┼──────────────┼───────────────┤
│ USA        │ 2               │ 3            │ 1700.00       │
│ UK         │ 1               │ 2            │ 2100.75       │
│ AUSTRALIA  │ 1               │ 1            │ 2250.00       │
│ GERMANY    │ 1               │ 1            │ 1800.25       │
│ JAPAN      │ 1               │ 2            │ 1550.50       │
└────────────┴─────────────────┴──────────────┴───────────────┘
```

## Customization Ideas

### 1. Change Schedule

```yaml
orchestration:
  schedule: "*/15 * * * *"  # Every 15 minutes
  # or
  schedule: "0 0 * * 0"     # Weekly on Sunday
  # or
  schedule: "@hourly"       # Every hour
```

### 2. Add More Phases

```yaml
outputs:
  - name: ml_features
    execution_phase: ml_prep
    depends_on:
      - customer_order_summary
```

### 3. Add Notifications

```yaml
monitoring:
  email_on_failure: true
  email_on_success: false
  slack_webhook: "https://hooks.slack.com/..."
```

### 4. Add Data Quality Gates

```yaml
validations:
  - name: critical_check
    execution_phase: validate
    block_downstream: true  # Stop pipeline if this fails
```

## Troubleshooting

### DAG Not Appearing in Airflow

```bash
# Check DAG was generated
ls dags/

# Check Airflow can find DAGs
docker compose exec airflow-webserver airflow dags list

# Check for errors
docker compose logs airflow-scheduler
```

### Tasks Failing

```bash
# View task logs in Airflow UI
# Click task → View Log

# Or check logs in terminal
docker compose logs airflow-worker
```

### DAG Parse Errors

```bash
# Validate DAG file
python dags/sales_analytics_pipeline_dag.py

# Re-generate DAG
fluid generate-dag --force
```

## Comparison: Before vs After FLUID

### Before FLUID (Traditional Airflow)

1. Write Python DAG file (100-300 lines)
2. Define operators manually
3. Hard-code dependencies
4. Write SQL transformations
5. Add error handling
6. Configure retries
7. Set up monitoring
8. Test locally
9. Deploy to Airflow

**Time**: 2-4 hours  
**Maintenance**: High (code drift from docs)

### After FLUID (0.7.1)

1. Write contract.fluid.yaml (50 lines)
2. Run `fluid generate-dag`

**Time**: 15 minutes  
**Maintenance**: Zero (DAG auto-syncs with contract)

## Next Steps

### 012 - Pipeline Orchestration (20 minutes)
Complete production pipeline setup:
```bash
fluid init production-pipeline --template local-pipeline
```

### 013 - Customer 360 Analytics (10 minutes)
Production-ready customer analytics:
```bash
fluid init customer360 --template customer-360
```

### 019 - Scheduling & Triggers (12 minutes)
Advanced scheduling patterns:
```bash
fluid init schedule-demo --template scheduling
```

## Success Criteria

✅ DAG generated successfully  
✅ Airflow started and accessible  
✅ DAG appears in Airflow UI  
✅ All tasks execute successfully  
✅ Task dependencies are correct  
✅ Validations pass  
✅ Output tables created  

## Resources

- 📖 [DAG Generation Documentation](../../docs/docs/features/dag-generation.md)
- 🎓 [Airflow Concepts](https://airflow.apache.org/docs/apache-airflow/stable/concepts/)
- 💬 [Community Forum](https://community.fluiddata.io)
- 🎥 [Video Tutorial](https://www.youtube.com/watch?v=...)

---

**🎉 Congratulations!** You've just experienced FLUID's killer feature: **Contract → DAG in minutes, not hours!**

This is the future of data engineering: **declare what you want, not how to build it.**
