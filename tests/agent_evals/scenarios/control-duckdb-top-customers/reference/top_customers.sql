SELECT customer_id, SUM(amount) AS total_amount
FROM read_csv_auto('data/orders.csv')
GROUP BY customer_id
ORDER BY total_amount DESC
LIMIT 5;
