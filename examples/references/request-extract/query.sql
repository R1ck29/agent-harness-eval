SELECT COUNT(*) AS 件数, SUM(amount) AS 合計金額
FROM sales
WHERE date >= '2026-03-01' AND date < '2026-04-01'
  AND store = '新宿店'
  AND amount >= 10000;
