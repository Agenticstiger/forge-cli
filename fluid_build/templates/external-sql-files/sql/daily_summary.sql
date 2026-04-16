-- Daily sales summary aggregation
-- Reusable for reporting and dashboards

SELECT 
  sale_date,
  COUNT(DISTINCT sale_id) AS total_transactions,
  COUNT(DISTINCT customer_id) AS unique_customers,
  SUM(quantity) AS total_units_sold,
  ROUND(SUM(gross_revenue), 2) AS total_gross_revenue,
  ROUND(SUM(discount_amount), 2) AS total_discounts,
  ROUND(SUM(net_revenue), 2) AS total_net_revenue,
  ROUND(AVG(net_revenue), 2) AS avg_transaction_value
FROM {{ ref('sales_with_revenue') }}
GROUP BY sale_date
ORDER BY sale_date
