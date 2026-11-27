WITH first_rows AS (
  SELECT o.* FROM orders o
  WHERE o.id = (SELECT MIN(id) FROM orders WHERE order_no = o.order_no)
)
SELECT order_no, customer, amount
FROM first_rows
WHERE ordered_at >= '2026-03-01' AND ordered_at < '2026-04-01'
  AND status <> 'cancelled'
  AND amount IS NOT NULL AND amount >= 5000
ORDER BY amount DESC, order_no ASC;
