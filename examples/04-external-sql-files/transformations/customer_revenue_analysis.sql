-- Customer Revenue Analysis
-- Calculates revenue metrics, retention, and value segments

WITH monthly_revenue AS (
  -- Calculate monthly revenue per customer
  SELECT
    customer_id,
    DATE_TRUNC('month', transaction_date) as revenue_month,
    SUM(amount) as monthly_amount,
    COUNT(*) as monthly_transactions
  FROM transactions
  WHERE status = 'completed'
  GROUP BY customer_id, DATE_TRUNC('month', transaction_date)
),

customer_lifetime_value AS (
  -- Aggregate customer lifetime metrics
  SELECT
    customer_id,
    COUNT(DISTINCT revenue_month) as active_months,
    SUM(monthly_amount) as total_revenue,
    AVG(monthly_amount) as avg_monthly_revenue,
    MIN(revenue_month) as first_revenue_month,
    MAX(revenue_month) as last_revenue_month
  FROM monthly_revenue
  GROUP BY customer_id
)

-- Final output: enriched customer profiles
SELECT
  c.customer_id,
  c.name as customer_name,
  c.email,
  c.country,
  c.signup_date,
  c.subscription_tier,
  
  -- Revenue metrics
  COALESCE(clv.total_revenue, 0) as lifetime_value,
  COALESCE(clv.avg_monthly_revenue, 0) as avg_monthly_revenue,
  COALESCE(clv.active_months, 0) as active_months,
  
  -- Engagement metrics
  CASE
    WHEN clv.active_months >= 4 THEN 'Highly Engaged'
    WHEN clv.active_months >= 2 THEN 'Engaged'
    WHEN clv.active_months >= 1 THEN 'New'
    ELSE 'No Activity'
  END as engagement_level,
  
  -- Value segment
  CASE
    WHEN COALESCE(clv.total_revenue, 0) >= 500 THEN 'High Value'
    WHEN COALESCE(clv.total_revenue, 0) >= 200 THEN 'Medium Value'
    WHEN COALESCE(clv.total_revenue, 0) > 0 THEN 'Low Value'
    ELSE 'No Revenue'
  END as value_segment,
  
  -- Dates
  clv.first_revenue_month,
  clv.last_revenue_month

FROM customers c
LEFT JOIN customer_lifetime_value clv ON c.customer_id = clv.customer_id
ORDER BY lifetime_value DESC, customer_name
