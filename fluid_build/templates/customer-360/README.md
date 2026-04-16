# Customer 360 Analytics - Production Showcase Template

**Time to Complete**: 10 minutes  
**Difficulty**: Intermediate  
**Track**: Intermediate  

## Overview

This template demonstrates a production-ready Customer 360 analytics pipeline using FLUID. It showcases:

- **Multi-source integration** (customers, orders, interactions)
- **RFM Analysis** (Recency, Frequency, Monetary scoring)
- **Customer Lifetime Value** (CLV) calculations
- **Churn risk prediction** and customer segmentation
- **Automated DAG generation** for production deployment
- **Complete data validation** suite

This is the "copy-paste-customize" template for building customer analytics pipelines.

## What You'll Build

A complete Customer 360 analytics pipeline that:
1. Ingests customer, order, and interaction data
2. Calculates RFM scores and segments customers
3. Computes customer lifetime value with engagement factors
4. Identifies high-value customers and churn risks
5. Auto-generates an Airflow DAG for daily updates

## Quick Start

### 1. Create Project from Template

```bash
fluid init my-customer-360 --template customer-360
cd my-customer-360
```

### 2. Explore the Sample Data

```bash
# 10 customers across multiple countries and industries
head data/customers.csv

# 20 orders with different product categories
head data/orders.csv

# 16 customer interactions (support + marketing)
head data/interactions.csv
```

### 3. Run Locally with DuckDB

```bash
# Validate contract
fluid validate

# Execute pipeline locally
fluid apply --local

# Query results
fluid query "SELECT * FROM customer_360_rfm ORDER BY customer_lifetime_value DESC LIMIT 5"
```

### 4. Generate Airflow DAG

```bash
# Generate DAG from contract
fluid generate-dag --output dags/

# Start local Airflow (requires Docker)
fluid airflow start

# Access Airflow UI at http://localhost:8080
# Username: admin, Password: admin
```

## Expected Output

### Customer Base Table
Basic customer information with signup tracking:
```
customer_id | full_name      | email                    | country | industry    | days_since_signup
1           | Alice Johnson  | alice.johnson@email.com  | USA     | Technology  | 420
2           | Bob Smith      | bob.smith@email.com      | USA     | Finance     | 384
...
```

### Customer Purchase Summary
Order metrics per customer:
```
customer_id | total_orders | total_revenue | avg_order_value | days_since_last_purchase
1           | 3            | 1199.91       | 399.97          | 35
2           | 3            | 4149.97       | 1383.32         | 30
...
```

### Customer Engagement Summary
Interaction metrics and sentiment:
```
customer_id | total_interactions | support_interactions | avg_sentiment_score
1           | 2                  | 1                    | 8.5
2           | 2                  | 1                    | 7.5
...
```

### Customer 360 RFM Table (Main Output)
Complete customer profiles with RFM scoring:
```
customer_id | full_name     | rfm_score | customer_segment    | customer_lifetime_value | churn_risk
1           | Alice Johnson | 545       | Champions           | 1559.89                 | Low
2           | Bob Smith     | 545       | Champions           | 4773.46                 | Low
7           | Grace Lee     | 543       | Loyal Customers     | 3577.35                 | Low
...
```

### High Value Customers View
Champions and loyal customers with low churn risk:
```
customer_id | full_name     | customer_segment | customer_lifetime_value | engagement_level
1           | Alice Johnson | Champions        | 1559.89                 | Medium
2           | Bob Smith     | Champions        | 4773.46                 | Medium
7           | Grace Lee     | Loyal Customers  | 3577.35                 | Medium
...
```

## Understanding the Pipeline

### Data Flow

```
┌─────────────┐   ┌──────────┐   ┌─────────────────┐
│  Customers  │   │  Orders  │   │  Interactions   │
└──────┬──────┘   └────┬─────┘   └────────┬────────┘
       │               │                   │
       └───────────────┴───────────────────┘
                       │
                       ▼
              ┌────────────────┐
              │ Customer Base  │ (Extract Phase)
              └────────┬───────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
       ▼               ▼               ▼
┌──────────┐  ┌────────────┐  ┌──────────────┐
│ Purchase │  │ Engagement │  │   (More      │
│ Summary  │  │  Summary   │  │  Summaries)  │
└────┬─────┘  └─────┬──────┘  └──────┬───────┘
     │              │                 │
     └──────────────┴─────────────────┘
                    │
                    ▼ (Aggregate Phase)
          ┌─────────────────────┐
          │ Customer 360 + RFM  │
          └──────────┬──────────┘
                     │
                     ▼
          ┌─────────────────────┐
          │ High Value Customers│ (View)
          └─────────────────────┘
                     │
                     ▼ (Validate Phase)
          ┌─────────────────────┐
          │   4 Validations     │
          └─────────────────────┘
```

### RFM Scoring Explained

**Recency** (R) - How recently did they purchase?
- Score 5: Last 30 days
- Score 4: 31-60 days
- Score 3: 61-90 days
- Score 2: 91-180 days
- Score 1: 180+ days

**Frequency** (F) - How often do they purchase?
- Score 5: 10+ orders
- Score 4: 5-9 orders
- Score 3: 3-4 orders
- Score 2: 1-2 orders
- Score 1: 0 orders

**Monetary** (M) - How much do they spend?
- Score 5: $5000+
- Score 4: $2000-$4999
- Score 3: $1000-$1999
- Score 2: $500-$999
- Score 1: <$500

**Customer Segments**:
- **Champions** (R≥4, F≥4, M≥4): Best customers
- **Loyal Customers** (R≥3, F≥3, M≥3): Regular buyers
- **Promising** (R≥4, F≤2): New with potential
- **At Risk** (R≤2, F≥3): Loyal but haven't purchased recently
- **Lost** (R≤2, F≤2): Churned customers
- **Potential Loyalists**: Everyone else

### Customer Lifetime Value Formula

```
CLV = Monetary × (1 + Frequency × 0.1) × (AvgSentiment / 10)
```

This factors in:
- Total revenue (monetary)
- Purchase frequency (repeat business value)
- Customer sentiment (engagement quality)

### Churn Risk Assessment

- **High Risk**: No purchase in 180+ days AND ≤2 total orders
- **Medium Risk**: No purchase in 90+ days AND ≤3 total orders
- **Low Risk**: All other customers

## Customization for Your Data

### 1. Replace Sample Data

Connect to your real data sources:

```yaml
inputs:
  - name: raw_customers
    type: postgres  # Or mysql, bigquery, snowflake, etc.
    connection: ${POSTGRES_CONN}
    table: production.customers
    schema:
      # Keep the same schema or customize
```

### 2. Adjust RFM Thresholds

Modify scoring based on your business:

```sql
-- Example: Adjust recency scoring for B2B (longer cycles)
CASE 
  WHEN recency_days <= 60 THEN 5    -- Changed from 30
  WHEN recency_days <= 120 THEN 4   -- Changed from 60
  WHEN recency_days <= 180 THEN 3   -- Changed from 90
  WHEN recency_days <= 365 THEN 2   -- Changed from 180
  ELSE 1
END AS r_score
```

### 3. Add More Interaction Types

Extend the engagement analysis:

```yaml
inputs:
  - name: raw_web_events
    type: bigquery
    table: analytics.web_events
  
  - name: raw_email_campaigns
    type: snowflake
    table: marketing.email_engagement
```

### 4. Customize CLV Formula

Adjust the calculation for your business model:

```sql
-- Example: Add margin and retention rate
ROUND(
  monetary 
  * 0.25  -- 25% margin
  * (1 + (frequency * 0.1)) 
  * (avg_sentiment / 10.0)
  * 2.5  -- Average 2.5 year retention
, 2) AS customer_lifetime_value
```

### 5. Change Orchestration Schedule

Update for your reporting cadence:

```yaml
orchestration:
  schedule: "0 6 * * MON"  # Weekly on Monday at 6 AM
  # Or "0 */6 * * *" for every 6 hours
```

## Deploy to Production

### Option 1: Deploy to Cloud (BigQuery)

```bash
# Configure BigQuery connection
export BIGQUERY_PROJECT="my-project"
export BIGQUERY_DATASET="customer_analytics"

# Deploy contract
fluid deploy --target bigquery --project ${BIGQUERY_PROJECT}

# Generate and deploy DAG
fluid generate-dag --output ~/airflow/dags/
```

### Option 2: Deploy to Snowflake

```bash
# Configure Snowflake connection
export SNOWFLAKE_ACCOUNT="abc12345"
export SNOWFLAKE_DATABASE="ANALYTICS"
export SNOWFLAKE_SCHEMA="CUSTOMER_360"

# Deploy
fluid deploy --target snowflake \
  --account ${SNOWFLAKE_ACCOUNT} \
  --database ${SNOWFLAKE_DATABASE}
```

### Option 3: Kubernetes with Airflow

```bash
# Generate DAG
fluid generate-dag --output ./dags/

# Deploy to Kubernetes
kubectl apply -f k8s/airflow-deployment.yaml

# Copy DAG to Airflow
kubectl cp ./dags/customer_360_dag.py \
  airflow-scheduler:/opt/airflow/dags/
```

## Validation & Monitoring

The pipeline includes 4 built-in validations:

1. **Revenue Consistency** (ERROR level)
   - Ensures no negative revenue or order values
   - Blocks pipeline on failure

2. **Valid RFM Scores** (ERROR level)
   - Verifies all scores are 3-digit strings with values 1-5
   - Example valid: "545", "233"

3. **Sentiment Range Check** (WARNING level)
   - Ensures sentiment scores between 0-10
   - Logs warning but doesn't block

4. **CLV Calculation Check** (WARNING level)
   - Validates that CLV ≥ monetary value
   - Ensures formula doesn't produce invalid results

Monitor via Airflow UI:
- Task success/failure rates
- SLA violations (1 hour default)
- Data quality trends over time

## Troubleshooting

### Issue: "No customer data found"

**Solution**: Verify your data paths
```bash
ls -la data/
# Should show customers.csv, orders.csv, interactions.csv
```

### Issue: "Invalid RFM scores"

**Solution**: Check date calculations
```bash
fluid query "SELECT MAX(order_date) FROM raw_orders"
# Ensure dates are recent relative to 'now'
```

### Issue: "DAG not appearing in Airflow"

**Solution**: Verify DAG file location
```bash
# Check DAG was generated
ls -la dags/customer_360_dag.py

# Verify Airflow can see DAGs folder
docker exec airflow-scheduler ls /opt/airflow/dags/
```

### Issue: "Validation failures"

**Solution**: Run validations individually
```bash
# Test each validation query
fluid query --validation revenue_consistency
fluid query --validation valid_rfm_scores
```

## Success Criteria

- [ ] All 3 input sources loaded successfully
- [ ] 5 output tables/views created
- [ ] All 10 customers have RFM scores
- [ ] Customer segments assigned (Champions, Loyal, etc.)
- [ ] CLV values calculated for all customers
- [ ] All 4 validations passing
- [ ] High-value customers view shows ≥1 customer
- [ ] DAG generated successfully (if using orchestration)
- [ ] Pipeline runs in <5 minutes locally
- [ ] Results match expected sample output

## Next Steps

### Related Templates

- **004-external-sql-files**: Move RFM logic to separate SQL files
- **005-data-quality-validation**: Add advanced data quality checks
- **009-testing-your-contract**: Write unit tests for RFM logic
- **012-pipeline-orchestration**: Advanced DAG configuration
- **021-bigquery-deployment**: Deploy to Google Cloud
- **024-redshift-deployment**: Deploy to AWS

### Enhancement Ideas

1. **Add Product Affinity Analysis**
   - Which product categories do customers buy together?
   - Use for cross-sell recommendations

2. **Implement Cohort Analysis**
   - Group customers by signup month
   - Track retention rates over time

3. **Build Churn Prediction Model**
   - Use ML to predict churn probability
   - Integrate with template 038-ml-model-deployment

4. **Add Real-time Scoring**
   - Stream customer events via Kafka
   - Update RFM scores in real-time
   - See template 034-streaming-data

5. **Create Customer Health Dashboard**
   - Export to BI tool (Tableau, Looker, Power BI)
   - Visualize RFM segments and trends

## Architecture Patterns Demonstrated

This template showcases several production best practices:

- **Layered Data Architecture**: Extract → Transform → Aggregate → Validate
- **Separation of Concerns**: Raw data, business logic, analytics separated
- **Data Quality**: Validation at every layer
- **Incremental Processing**: Daily refreshes with orchestration
- **Monitoring**: SLA tracking, alerting, metrics
- **Cloud-Ready**: Can deploy to any cloud platform
- **Self-Documenting**: Schema definitions inline with transformations

## Resources

- [FLUID Documentation](https://docs.fluid.io)
- [RFM Analysis Guide](https://docs.fluid.io/guides/rfm-analysis)
- [Customer Lifetime Value](https://docs.fluid.io/guides/clv-calculation)
- [Airflow Integration](https://docs.fluid.io/integrations/airflow)
- [Deployment Guide](https://docs.fluid.io/deployment)
- [Best Practices](https://docs.fluid.io/best-practices)

---

**Questions?** Check our [Slack community](https://fluid-community.slack.com) or [GitHub Discussions](https://github.com/fluid-io/fluid/discussions)
