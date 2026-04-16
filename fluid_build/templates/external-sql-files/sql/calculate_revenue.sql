-- Calculate revenue for each sale
-- This transformation can be reused across multiple outputs

SELECT 
  sale_id,
  product_id,
  customer_id,
  sale_date,
  quantity,
  unit_price,
  discount_pct,
  ROUND(quantity * unit_price, 2) AS gross_revenue,
  ROUND(quantity * unit_price * (discount_pct / 100.0), 2) AS discount_amount,
  ROUND(quantity * unit_price * (1 - discount_pct / 100.0), 2) AS net_revenue
FROM {{ ref('raw_sales') }}
